/*===- bcir_stateful_probe.c - Incremental Decode, Activation Probe & SSP-Live ---===
 * Pure ISO C11 (-std=c11 -O3 -ffp-contract=off -fno-math-errno), zero hot-path mallocs.
 * Reads live weights & exact tensor offsets directly from BCIR's ABI 6 bcir_ext_model.
 *===---------------------------------------------------------------------------===*/
#include "bcir_stateful_probe.h"
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MHC_SK_ITERS 20

static inline float sp_sigmoid_f(float x) {
  if (x >= 0.0f) {
    float z = expf(-x);
    return 1.0f / (1.0f + z);
  } else {
    float z = expf(x);
    return z / (1.0f + z);
  }
}

static inline double sp_sigmoid_d(double x) {
  if (x >= 0.0) {
    double z = exp(-x);
    return 1.0 / (1.0 + z);
  } else {
    double z = exp(x);
    return z / (1.0 + z);
  }
}

static void sp_rmsnorm(size_t n, const float *x, const float *g, double eps, float *y) {
  double ss = 0.0;
  for (size_t i = 0; i < n; ++i) ss += (double)x[i] * (double)x[i];
  double inv = 1.0 / sqrt(ss / (double)n + eps);
  for (size_t i = 0; i < n; ++i) y[i] = (float)((double)x[i] * inv * (double)g[i]);
}

static void sp_l2norm(size_t n, const float *x, float *y) {
  double ss = 1e-6;
  for (size_t i = 0; i < n; ++i) ss += (double)x[i] * (double)x[i];
  double inv = 1.0 / sqrt(ss);
  for (size_t i = 0; i < n; ++i) y[i] = (float)((double)x[i] * inv);
}

/* y[out_dim] = W[out_dim, in_dim] @ x[in_dim] (deterministic 4-row x 8-lane unrolled) */
static void sp_matvec(size_t out_dim, size_t in_dim, const float *W, const float *x, float *y) {
  size_t r = 0;
  for (; r + 3u < out_dim; r += 4u) {
    const float *w0 = W + r * in_dim;
    const float *w1 = w0 + in_dim;
    const float *w2 = w1 + in_dim;
    const float *w3 = w2 + in_dim;
    float s0[8] = {0}, s1[8] = {0}, s2[8] = {0}, s3[8] = {0};
    size_t c = 0;
    for (; c + 7u < in_dim; c += 8u) {
      for (size_t k = 0; k < 8u; ++k) {
        float xk = x[c + k];
        s0[k] += w0[c + k] * xk;
        s1[k] += w1[c + k] * xk;
        s2[k] += w2[c + k] * xk;
        s3[k] += w3[c + k] * xk;
      }
    }
    double a0 = (double)s0[0] + s0[1] + s0[2] + s0[3] + s0[4] + s0[5] + s0[6] + s0[7];
    double a1 = (double)s1[0] + s1[1] + s1[2] + s1[3] + s1[4] + s1[5] + s1[6] + s1[7];
    double a2 = (double)s2[0] + s2[1] + s2[2] + s2[3] + s2[4] + s2[5] + s2[6] + s2[7];
    double a3 = (double)s3[0] + s3[1] + s3[2] + s3[3] + s3[4] + s3[5] + s3[6] + s3[7];
    for (; c < in_dim; ++c) {
      double xc = (double)x[c];
      a0 += (double)w0[c] * xc;
      a1 += (double)w1[c] * xc;
      a2 += (double)w2[c] * xc;
      a3 += (double)w3[c] * xc;
    }
    y[r] = (float)a0;
    y[r + 1u] = (float)a1;
    y[r + 2u] = (float)a2;
    y[r + 3u] = (float)a3;
  }
  for (; r < out_dim; ++r) {
    const float *wr = W + r * in_dim;
    double acc = 0.0;
    for (size_t c = 0; c < in_dim; ++c) acc += (double)wr[c] * (double)x[c];
    y[r] = (float)acc;
  }
}

static void sp_mhc_sinkhorn(size_t C, const float *raw, double H[16]) {
  memset(H, 0, sizeof(double) * 16u);
  double mx = -1e30;
  for (size_t i = 0; i < C * C; ++i) {
    double v = (double)raw[i];
    if (v > mx) mx = v;
  }
  for (size_t i = 0; i < C * C; ++i) H[i] = exp((double)raw[i] - mx);
  for (int it = 0; it < MHC_SK_ITERS; ++it) {
    for (size_t r = 0; r < C; ++r) {
      double s = 0.0;
      for (size_t c = 0; c < C; ++c) s += H[r * C + c];
      double inv = 1.0 / (s > 1e-30 ? s : 1e-30);
      for (size_t c = 0; c < C; ++c) H[r * C + c] *= inv;
    }
    for (size_t c = 0; c < C; ++c) {
      double s = 0.0;
      for (size_t r = 0; r < C; ++r) s += H[r * C + c];
      double inv = 1.0 / (s > 1e-30 ? s : 1e-30);
      for (size_t r = 0; r < C; ++r) H[r * C + c] *= inv;
    }
  }
}

static void sp_apply_rope_vec(size_t pos, size_t n_heads, size_t hd, double rope_base, float *x) {
  size_t half = hd / 2u;
  for (size_t h = 0; h < n_heads; ++h) {
    float *xh = x + h * hd;
    for (size_t i = 0; i < half; ++i) {
      double freq = pow(rope_base, -2.0 * (double)i / (double)hd);
      double ang = (double)pos * freq;
      double c = cos(ang), s = sin(ang);
      double x0 = (double)xh[i], x1 = (double)xh[i + half];
      xh[i] = (float)(x0 * c - x1 * s);
      xh[i + half] = (float)(x0 * s + x1 * c);
    }
  }
}

typedef struct layer_offs {
  uint32_t mixer, use_rope;
  size_t norm1, canon_a;
  size_t wq, wk, wv, q_norm, k_norm, wo;
  size_t walpha, balpha, wbeta, bbeta;
  size_t wa1, wa2, ba_chan, we1, we2, be_chan, ww1, ww2, bw_chan, o_norm;
  size_t wq_c, wq_r, w_dkv, norm_kv, w_uk, w_uv, w_kr;
  size_t wg1, wg2, bg;
  size_t mhc_hres1, mhc_hpre1, mhc_hpost1, mhc_hres2, mhc_hpre2, mhc_hpost2;
  size_t norm2, canon_c, wgate, wup, wdown;
} layer_offs;

typedef struct layer_state {
  float *canon_a_hist;   /* [4 * d] */
  float *canon_c_hist;   /* [4 * d] */
  double *rec_S;         /* [H * dh * dh] for DeltaNet / KDA / GDN-2 */
  double *rec_S_prev;    /* [H * dh * dh] previous step for echo cosine */
  float *k_cache;        /* [max_seq * kvd] for GQA/SWA OR [max_seq * d] for MLA */
  float *v_cache;        /* [max_seq * kvd] for GQA/SWA OR [max_seq * d] for MLA */
  float *kr_cache;       /* [max_seq * dr] for RoPE MLA */
} layer_state;

struct bcir_stateful_ctx {
  bcir_ext_spec sp;
  uint32_t max_seq;
  uint32_t pos;
  size_t n_params;
  size_t mhc_c;
  size_t gate_rank;
  const float *W;
  size_t off_embed, off_final_norm, off_lm_head;
  layer_offs loff[BCIR_EXT_MAX_LAYERS];
  layer_state lst[BCIR_EXT_MAX_LAYERS];
  uint32_t *tok_hist;    /* [max_seq] */
  /* Reusable scratch (zero hot-path mallocs) */
  float *mhc_cur, *mhc_mid;
  float *x_in, *n1_raw, *n1, *x2, *n2_raw, *n2;
  float *q, *k, *v, *q_r, *k_r, *c_raw, *c_hat;
  float *g_mid, *g_val, *a_mid, *a_pre, *e_mid, *e_pre, *w_mid, *w_pre;
  float *alpha_buf, *erase_buf, *write_buf;
  float *ctx_raw, *ctx_buf, *mix_out;
  float *gate_buf, *up_buf, *hid_buf, *mlp_out, *x_final, *norm_final;
  double *scores, *kS_buf;
};

static int lookup_off(const bcir_ext_model *m, const char *name, size_t *out) {
  size_t cnt = bcir_ext_tensor_count(m);
  bcir_ext_tensor_info ti;
  for (size_t i = 0; i < cnt; ++i) {
    if (bcir_ext_tensor_info_at(m, i, &ti) != 0) return 0;
    if (strcmp(ti.name, name) == 0) {
      *out = ti.offset;
      return 1;
    }
  }
  return 0;
}

bcir_stateful_ctx *bcir_stateful_create(const bcir_ext_model *m,
                                        const bcir_ext_spec *spec,
                                        uint32_t max_seq) {
  if (!m || !spec || max_seq == 0u || spec->layers > BCIR_EXT_MAX_LAYERS) return NULL;
  bcir_stateful_ctx *c = (bcir_stateful_ctx *)calloc(1u, sizeof(*c));
  if (!c) return NULL;
  c->sp = *spec;
  c->max_seq = max_seq;
  c->n_params = bcir_ext_param_count(m);
  c->W = bcir_ext_weights_ptr((bcir_ext_model *)m);
  c->mhc_c = (spec->mhc_streams > 1u) ? spec->mhc_streams : 1u;
  size_t d = spec->width, H = spec->heads, KH = spec->kvheads, dh = d / H;
  size_t kvd = KH * dh, ff = spec->ff, dc = spec->mla_dc ? spec->mla_dc : (d / 4u);
  size_t dr = spec->mla_dr ? spec->mla_dr : (dh / 2u);
  c->sp.mla_dc = (uint32_t)dc;
  c->sp.mla_dr = (uint32_t)dr;
  c->gate_rank = spec->gate_rank ? spec->gate_rank : (dh > 16u ? dh : 16u);

  if (!lookup_off(m, "embed", &c->off_embed) ||
      !lookup_off(m, "final_norm", &c->off_final_norm)) {
    bcir_stateful_destroy(c);
    return NULL;
  }
  if (spec->tied) c->off_lm_head = c->off_embed;
  else if (!lookup_off(m, "lm_head", &c->off_lm_head)) {
    bcir_stateful_destroy(c);
    return NULL;
  }

  char nm[48];
  for (uint32_t l = 0; l < spec->layers; ++l) {
    layer_offs *lo = &c->loff[l];
    layer_state *ls = &c->lst[l];
    lo->mixer = spec->layer_mixer[l];
    lo->use_rope = spec->layer_use_rope[l];
    snprintf(nm, sizeof(nm), "L%u.norm1", l); lookup_off(m, nm, &lo->norm1);
    if (spec->canon_mode >= BCIR_CANON_A) {
      snprintf(nm, sizeof(nm), "L%u.canon_a", l); lookup_off(m, nm, &lo->canon_a);
      ls->canon_a_hist = (float *)calloc(BCIR_EXT_CANON_K * d, sizeof(float));
    }
    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      snprintf(nm, sizeof(nm), "L%u.wq", l); lookup_off(m, nm, &lo->wq);
      snprintf(nm, sizeof(nm), "L%u.wk", l); lookup_off(m, nm, &lo->wk);
      snprintf(nm, sizeof(nm), "L%u.wv", l); lookup_off(m, nm, &lo->wv);
      if (spec->qk_norm) {
        snprintf(nm, sizeof(nm), "L%u.q_norm", l); lookup_off(m, nm, &lo->q_norm);
        snprintf(nm, sizeof(nm), "L%u.k_norm", l); lookup_off(m, nm, &lo->k_norm);
      }
      snprintf(nm, sizeof(nm), "L%u.wo", l); lookup_off(m, nm, &lo->wo);
      ls->k_cache = (float *)calloc((size_t)max_seq * kvd, sizeof(float));
      ls->v_cache = (float *)calloc((size_t)max_seq * kvd, sizeof(float));
    } else if (lo->mixer == BCIR_MIXER_DELTANET) {
      snprintf(nm, sizeof(nm), "L%u.wq", l); lookup_off(m, nm, &lo->wq);
      snprintf(nm, sizeof(nm), "L%u.wk", l); lookup_off(m, nm, &lo->wk);
      snprintf(nm, sizeof(nm), "L%u.wv", l); lookup_off(m, nm, &lo->wv);
      snprintf(nm, sizeof(nm), "L%u.walpha", l); lookup_off(m, nm, &lo->walpha);
      snprintf(nm, sizeof(nm), "L%u.balpha", l); lookup_off(m, nm, &lo->balpha);
      snprintf(nm, sizeof(nm), "L%u.wbeta", l); lookup_off(m, nm, &lo->wbeta);
      snprintf(nm, sizeof(nm), "L%u.bbeta", l); lookup_off(m, nm, &lo->bbeta);
      snprintf(nm, sizeof(nm), "L%u.wo", l); lookup_off(m, nm, &lo->wo);
      ls->rec_S = (double *)calloc(H * dh * dh, sizeof(double));
      ls->rec_S_prev = (double *)calloc(H * dh * dh, sizeof(double));
    } else if (lo->mixer == BCIR_MIXER_KDA || lo->mixer == BCIR_MIXER_GDN2) {
      snprintf(nm, sizeof(nm), "L%u.wq", l); lookup_off(m, nm, &lo->wq);
      snprintf(nm, sizeof(nm), "L%u.wk", l); lookup_off(m, nm, &lo->wk);
      snprintf(nm, sizeof(nm), "L%u.wv", l); lookup_off(m, nm, &lo->wv);
      snprintf(nm, sizeof(nm), "L%u.wa1", l); lookup_off(m, nm, &lo->wa1);
      snprintf(nm, sizeof(nm), "L%u.wa2", l); lookup_off(m, nm, &lo->wa2);
      snprintf(nm, sizeof(nm), "L%u.ba_chan", l); lookup_off(m, nm, &lo->ba_chan);
      if (lo->mixer == BCIR_MIXER_KDA) {
        snprintf(nm, sizeof(nm), "L%u.wbeta", l); lookup_off(m, nm, &lo->wbeta);
        snprintf(nm, sizeof(nm), "L%u.bbeta", l); lookup_off(m, nm, &lo->bbeta);
      } else {
        snprintf(nm, sizeof(nm), "L%u.we1", l); lookup_off(m, nm, &lo->we1);
        snprintf(nm, sizeof(nm), "L%u.we2", l); lookup_off(m, nm, &lo->we2);
        snprintf(nm, sizeof(nm), "L%u.be_chan", l); lookup_off(m, nm, &lo->be_chan);
        snprintf(nm, sizeof(nm), "L%u.ww1", l); lookup_off(m, nm, &lo->ww1);
        snprintf(nm, sizeof(nm), "L%u.ww2", l); lookup_off(m, nm, &lo->ww2);
        snprintf(nm, sizeof(nm), "L%u.bw_chan", l); lookup_off(m, nm, &lo->bw_chan);
      }
      snprintf(nm, sizeof(nm), "L%u.o_norm", l); lookup_off(m, nm, &lo->o_norm);
      snprintf(nm, sizeof(nm), "L%u.wo", l); lookup_off(m, nm, &lo->wo);
      ls->rec_S = (double *)calloc(H * dh * dh, sizeof(double));
      ls->rec_S_prev = (double *)calloc(H * dh * dh, sizeof(double));
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      snprintf(nm, sizeof(nm), "L%u.wq_c", l); lookup_off(m, nm, &lo->wq_c);
      if (spec->qk_norm) {
        snprintf(nm, sizeof(nm), "L%u.q_norm", l); lookup_off(m, nm, &lo->q_norm);
      }
      if (lo->use_rope) {
        snprintf(nm, sizeof(nm), "L%u.wq_r", l); lookup_off(m, nm, &lo->wq_r);
        snprintf(nm, sizeof(nm), "L%u.w_kr", l); lookup_off(m, nm, &lo->w_kr);
        ls->kr_cache = (float *)calloc((size_t)max_seq * dr, sizeof(float));
      }
      snprintf(nm, sizeof(nm), "L%u.w_dkv", l); lookup_off(m, nm, &lo->w_dkv);
      snprintf(nm, sizeof(nm), "L%u.norm_kv", l); lookup_off(m, nm, &lo->norm_kv);
      snprintf(nm, sizeof(nm), "L%u.w_uk", l); lookup_off(m, nm, &lo->w_uk);
      snprintf(nm, sizeof(nm), "L%u.w_uv", l); lookup_off(m, nm, &lo->w_uv);
      snprintf(nm, sizeof(nm), "L%u.wo", l); lookup_off(m, nm, &lo->wo);
      ls->k_cache = (float *)calloc((size_t)max_seq * d, sizeof(float));
      ls->v_cache = (float *)calloc((size_t)max_seq * d, sizeof(float));
    }
    if (spec->gate_mode == BCIR_GATE_G1_HEADWISE) {
      snprintf(nm, sizeof(nm), "L%u.wg1", l); lookup_off(m, nm, &lo->wg1);
      snprintf(nm, sizeof(nm), "L%u.bg", l); lookup_off(m, nm, &lo->bg);
    } else if (spec->gate_mode == BCIR_GATE_G1_LOWRANK) {
      snprintf(nm, sizeof(nm), "L%u.wg1", l); lookup_off(m, nm, &lo->wg1);
      snprintf(nm, sizeof(nm), "L%u.wg2", l); lookup_off(m, nm, &lo->wg2);
      snprintf(nm, sizeof(nm), "L%u.bg", l); lookup_off(m, nm, &lo->bg);
    }
    if (c->mhc_c > 1u) {
      snprintf(nm, sizeof(nm), "L%u.mhc_hres1", l); lookup_off(m, nm, &lo->mhc_hres1);
      snprintf(nm, sizeof(nm), "L%u.mhc_hpre1", l); lookup_off(m, nm, &lo->mhc_hpre1);
      snprintf(nm, sizeof(nm), "L%u.mhc_hpost1", l); lookup_off(m, nm, &lo->mhc_hpost1);
      snprintf(nm, sizeof(nm), "L%u.mhc_hres2", l); lookup_off(m, nm, &lo->mhc_hres2);
      snprintf(nm, sizeof(nm), "L%u.mhc_hpre2", l); lookup_off(m, nm, &lo->mhc_hpre2);
      snprintf(nm, sizeof(nm), "L%u.mhc_hpost2", l); lookup_off(m, nm, &lo->mhc_hpost2);
    }
    snprintf(nm, sizeof(nm), "L%u.norm2", l); lookup_off(m, nm, &lo->norm2);
    if (spec->canon_mode >= BCIR_CANON_AC) {
      snprintf(nm, sizeof(nm), "L%u.canon_c", l); lookup_off(m, nm, &lo->canon_c);
      ls->canon_c_hist = (float *)calloc(BCIR_EXT_CANON_K * d, sizeof(float));
    }
    snprintf(nm, sizeof(nm), "L%u.wgate", l); lookup_off(m, nm, &lo->wgate);
    snprintf(nm, sizeof(nm), "L%u.wup", l); lookup_off(m, nm, &lo->wup);
    snprintf(nm, sizeof(nm), "L%u.wdown", l); lookup_off(m, nm, &lo->wdown);
  }

  c->tok_hist = (uint32_t *)calloc(max_seq, sizeof(uint32_t));
  c->mhc_cur = (float *)calloc(c->mhc_c * d, sizeof(float));
  c->mhc_mid = (float *)calloc(c->mhc_c * d, sizeof(float));
  c->x_in = (float *)calloc(d, sizeof(float));
  c->n1_raw = (float *)calloc(d, sizeof(float));
  c->n1 = (float *)calloc(d, sizeof(float));
  c->x2 = (float *)calloc(d, sizeof(float));
  c->n2_raw = (float *)calloc(d, sizeof(float));
  c->n2 = (float *)calloc(d, sizeof(float));
  c->q = (float *)calloc(d, sizeof(float));
  c->k = (float *)calloc(d, sizeof(float));
  c->v = (float *)calloc(d, sizeof(float));
  c->q_r = (float *)calloc(H * dr + 1u, sizeof(float));
  c->k_r = (float *)calloc(dr + 1u, sizeof(float));
  c->c_raw = (float *)calloc(dc + 1u, sizeof(float));
  c->c_hat = (float *)calloc(dc + 1u, sizeof(float));
  c->g_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->g_val = (float *)calloc(d + 1u, sizeof(float));
  c->a_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->a_pre = (float *)calloc(d + 1u, sizeof(float));
  c->e_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->e_pre = (float *)calloc(d + 1u, sizeof(float));
  c->w_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->w_pre = (float *)calloc(d + 1u, sizeof(float));
  c->alpha_buf = (float *)calloc(d + 1u, sizeof(float));
  c->erase_buf = (float *)calloc(d + 1u, sizeof(float));
  c->write_buf = (float *)calloc(d + 1u, sizeof(float));
  c->ctx_raw = (float *)calloc(d, sizeof(float));
  c->ctx_buf = (float *)calloc(d, sizeof(float));
  c->mix_out = (float *)calloc(d, sizeof(float));
  c->gate_buf = (float *)calloc(ff, sizeof(float));
  c->up_buf = (float *)calloc(ff, sizeof(float));
  c->hid_buf = (float *)calloc(ff, sizeof(float));
  c->mlp_out = (float *)calloc(d, sizeof(float));
  c->x_final = (float *)calloc(d, sizeof(float));
  c->norm_final = (float *)calloc(d, sizeof(float));
  c->scores = (double *)calloc(max_seq, sizeof(double));
  c->kS_buf = (double *)calloc(dh, sizeof(double));
  return c;
}

void bcir_stateful_destroy(bcir_stateful_ctx *c) {
  if (!c) return;
  for (uint32_t l = 0; l < c->sp.layers; ++l) {
    free(c->lst[l].canon_a_hist);
    free(c->lst[l].canon_c_hist);
    free(c->lst[l].rec_S);
    free(c->lst[l].rec_S_prev);
    free(c->lst[l].k_cache);
    free(c->lst[l].v_cache);
    free(c->lst[l].kr_cache);
  }
  free(c->tok_hist);
  free(c->mhc_cur); free(c->mhc_mid);
  free(c->x_in); free(c->n1_raw); free(c->n1);
  free(c->x2); free(c->n2_raw); free(c->n2);
  free(c->q); free(c->k); free(c->v);
  free(c->q_r); free(c->k_r); free(c->c_raw); free(c->c_hat);
  free(c->g_mid); free(c->g_val);
  free(c->a_mid); free(c->a_pre); free(c->e_mid); free(c->e_pre);
  free(c->w_mid); free(c->w_pre);
  free(c->alpha_buf); free(c->erase_buf); free(c->write_buf);
  free(c->ctx_raw); free(c->ctx_buf); free(c->mix_out);
  free(c->gate_buf); free(c->up_buf); free(c->hid_buf);
  free(c->mlp_out); free(c->x_final); free(c->norm_final);
  free(c->scores); free(c->kS_buf);
  free(c);
}

void bcir_stateful_reset(bcir_stateful_ctx *c) {
  if (!c) return;
  c->pos = 0u;
  size_t d = c->sp.width, H = c->sp.heads, dh = d / H;
  for (uint32_t l = 0; l < c->sp.layers; ++l) {
    layer_state *ls = &c->lst[l];
    if (ls->canon_a_hist) memset(ls->canon_a_hist, 0, BCIR_EXT_CANON_K * d * sizeof(float));
    if (ls->canon_c_hist) memset(ls->canon_c_hist, 0, BCIR_EXT_CANON_K * d * sizeof(float));
    if (ls->rec_S) memset(ls->rec_S, 0, H * dh * dh * sizeof(double));
    if (ls->rec_S_prev) memset(ls->rec_S_prev, 0, H * dh * dh * sizeof(double));
  }
}

void bcir_stateful_sync_weights(bcir_stateful_ctx *c, const bcir_ext_model *m) {
  if (!c || !m) return;
  c->W = bcir_ext_weights_ptr((bcir_ext_model *)m);
}

/* Check if the last `ng` tokens ending at pos (inclusive) already appeared in lookback window */
static int detect_recent_echo(const uint32_t *hist, uint32_t pos, uint32_t ng, uint32_t win) {
  if (ng < 2u || pos + 1u < ng * 2u) return 0;
  uint32_t start = (pos > win) ? (pos - win) : 0u;
  uint32_t cur_s = pos + 1u - ng;
  for (uint32_t j = start; j + ng <= cur_s; ++j) {
    int match = 1;
    for (uint32_t k = 0; k < ng; ++k) {
      if (hist[j + k] != hist[cur_s + k]) { match = 0; break; }
    }
    if (match) return 1;
  }
  return 0;
}

static void canon_step(size_t d, uint32_t pos, const float *x, const float *w, float *hist, float *y) {
  uint32_t slot = pos % BCIR_EXT_CANON_K;
  memcpy(hist + (size_t)slot * d, x, d * sizeof(float));
  uint32_t kmax = (pos + 1u < BCIR_EXT_CANON_K) ? (pos + 1u) : BCIR_EXT_CANON_K;
  for (size_t j = 0; j < d; ++j) {
    double acc = (double)x[j];
    for (uint32_t k = 0; k < kmax; ++k) {
      uint32_t pslot = (pos - k) % BCIR_EXT_CANON_K;
      acc += (double)w[(size_t)k * d + j] * (double)hist[(size_t)pslot * d + j];
    }
    y[j] = (float)acc;
  }
}

int bcir_stateful_step(bcir_stateful_ctx *c,
                       uint32_t token_id,
                       int in_generation,
                       const bcir_ssp_config *ssp,
                       float *out_logits,
                       bcir_probe_step_out *probe) {
  if (!c || token_id >= c->sp.vocab || c->pos >= c->max_seq) return 1;
  const uint32_t pos = c->pos;
  c->tok_hist[pos] = token_id;

  const size_t d = c->sp.width, H = c->sp.heads, KH = c->sp.kvheads, dh = d / H;
  const size_t kvd = KH * dh, ff = c->sp.ff, V = c->sp.vocab;
  const size_t dc = c->sp.mla_dc, dr = c->sp.mla_dr, rg = c->gate_rank, mC = c->mhc_c;
  const double eps = c->sp.rms_eps;
  const float *W = c->W;

  int echo_active = 0;
  int apply_ssp = 0;
  if (ssp && in_generation) {
    uint32_t ng = ssp->echo_ngram ? ssp->echo_ngram : 4u;
    uint32_t ew = ssp->echo_window ? ssp->echo_window : 48u;
    echo_active = detect_recent_echo(c->tok_hist, pos, ng, ew);
    apply_ssp = ssp->adaptive_echo_only ? echo_active : 1;
  }

  const float *erow = W + c->off_embed + (size_t)token_id * d;
  if (mC > 1u) {
    for (size_t s = 0; s < mC; ++s) memcpy(c->mhc_cur + s * d, erow, d * sizeof(float));
  } else {
    memcpy(c->x_in, erow, d * sizeof(float));
  }

  for (uint32_t l = 0; l < c->sp.layers; ++l) {
    const layer_offs *lo = &c->loff[l];
    layer_state *ls = &c->lst[l];
    double hres1[16], hpre1[4], hpost1[4], hres2[16], hpre2[4], hpost2[4];

    if (mC > 1u) {
      sp_mhc_sinkhorn(mC, W + lo->mhc_hres1, hres1);
      for (size_t s = 0; s < mC; ++s) {
        hpre1[s] = sp_sigmoid_d((double)W[lo->mhc_hpre1 + s]);
        hpost1[s] = 2.0 * sp_sigmoid_d((double)W[lo->mhc_hpost1 + s]);
      }
      for (size_t j = 0; j < d; ++j) {
        double acc = 0.0;
        for (size_t s = 0; s < mC; ++s) acc += hpre1[s] * (double)c->mhc_cur[s * d + j];
        c->x_in[j] = (float)acc;
      }
    }

    sp_rmsnorm(d, c->x_in, W + lo->norm1, eps, c->n1_raw);
    if (c->sp.canon_mode >= BCIR_CANON_A) {
      canon_step(d, pos, c->n1_raw, W + lo->canon_a, ls->canon_a_hist, c->n1);
    } else {
      memcpy(c->n1, c->n1_raw, d * sizeof(float));
    }

    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      sp_matvec(d, d, W + lo->wq, c->n1, c->q);
      sp_matvec(kvd, d, W + lo->wk, c->n1, c->k);
      sp_matvec(kvd, d, W + lo->wv, c->n1, c->v);
      if (c->sp.qk_norm) {
        for (size_t h = 0; h < H; ++h)
          sp_rmsnorm(dh, c->q + h * dh, W + lo->q_norm, eps, c->q + h * dh);
        for (size_t h = 0; h < KH; ++h)
          sp_rmsnorm(dh, c->k + h * dh, W + lo->k_norm, eps, c->k + h * dh);
      }
      if (lo->use_rope) {
        sp_apply_rope_vec(pos, H, dh, c->sp.rope_base, c->q);
        sp_apply_rope_vec(pos, KH, dh, c->sp.rope_base, c->k);
      }
      memcpy(ls->k_cache + (size_t)pos * kvd, c->k, kvd * sizeof(float));
      memcpy(ls->v_cache + (size_t)pos * kvd, c->v, kvd * sizeof(float));

      uint32_t j0 = 0u;
      if (lo->mixer == BCIR_MIXER_SWA && pos + 1u > c->sp.swa_window)
        j0 = pos + 1u - c->sp.swa_window;
      double scale = 1.0 / sqrt((double)dh);
      size_t ratio = H / KH;
      for (size_t h = 0; h < H; ++h) {
        size_t kh = h / ratio;
        const float *qh = c->q + h * dh;
        double mx = -1e30;
        for (uint32_t j = j0; j <= pos; ++j) {
          const float *kj = ls->k_cache + (size_t)j * kvd + kh * dh;
          double s = 0.0;
          for (size_t a = 0; a < dh; ++a) s += (double)qh[a] * (double)kj[a];
          s *= scale;
          if (apply_ssp && ssp->mla_repeat_logit_bias != 0.0f && j < pos && c->tok_hist[j] == token_id) {
            s += (double)ssp->mla_repeat_logit_bias;
          }
          c->scores[j] = s;
          if (s > mx) mx = s;
        }
        double sum = 0.0;
        for (uint32_t j = j0; j <= pos; ++j) { c->scores[j] = exp(c->scores[j] - mx); sum += c->scores[j]; }
        double inv = 1.0 / sum;
        double rep_mass = 0.0, ent = 0.0;
        for (uint32_t j = j0; j <= pos; ++j) {
          double p = c->scores[j] * inv;
          c->scores[j] = p;
          if (p > 1e-12) ent -= p * log(p);
          if (j < pos && c->tok_hist[j] == token_id) rep_mass += p;
        }
        if (probe) {
          if (probe->attn_repeat) probe->attn_repeat[l * H + h] = (float)rep_mass;
          if (probe->attn_entropy) probe->attn_entropy[l * H + h] = (float)ent;
        }
        float *yh = c->ctx_raw + h * dh;
        for (size_t a = 0; a < dh; ++a) {
          double acc = 0.0;
          for (uint32_t j = j0; j <= pos; ++j)
            acc += c->scores[j] * (double)ls->v_cache[(size_t)j * kvd + kh * dh + a];
          yh[a] = (float)acc;
        }
      }
    } else if (lo->mixer == BCIR_MIXER_DELTANET) {
      sp_matvec(d, d, W + lo->wq, c->n1, c->q);
      sp_matvec(d, d, W + lo->wk, c->n1, c->k);
      sp_matvec(d, d, W + lo->wv, c->n1, c->v);
      for (size_t h = 0; h < H; ++h) {
        sp_l2norm(dh, c->q + h * dh, c->q + h * dh);
        sp_l2norm(dh, c->k + h * dh, c->k + h * dh);
      }
      sp_matvec(H, d, W + lo->walpha, c->n1, c->a_pre);
      sp_matvec(H, d, W + lo->wbeta, c->n1, c->e_pre);
      const float *ba = W + lo->balpha, *bb = W + lo->bbeta;
      float ashift = apply_ssp ? ssp->gdn_alpha_shift : 0.0f;
      float eshift = apply_ssp ? ssp->gdn_erase_shift : 0.0f;
      double damp = (apply_ssp && echo_active && ssp->state_echo_damp > 0.0f)
                        ? (1.0 - (double)ssp->state_echo_damp) : 1.0;
      for (size_t h = 0; h < H; ++h) {
        double al = (double)sp_sigmoid_f(c->a_pre[h] + ba[h] + ashift);
        double be = (double)sp_sigmoid_f(c->e_pre[h] + bb[h] + eshift);
        double *S = ls->rec_S + h * dh * dh;
        double *Sp = ls->rec_S_prev + h * dh * dh;
        memcpy(Sp, S, dh * dh * sizeof(double));
        const float *qh = c->q + h * dh, *kh = c->k + h * dh, *vh = c->v + h * dh;
        for (size_t j = 0; j < dh; ++j) {
          double acc = 0.0;
          for (size_t a = 0; a < dh; ++a) acc += (double)kh[a] * (al * damp * S[a * dh + j]);
          c->kS_buf[j] = acc;
        }
        double sn = 0.0, spn = 0.0, dot = 0.0;
        for (size_t a = 0; a < dh; ++a) {
          for (size_t j = 0; j < dh; ++j) {
            double nv = al * damp * S[a * dh + j] + be * (double)kh[a] * ((double)vh[j] - c->kS_buf[j]);
            S[a * dh + j] = nv;
            sn += nv * nv; spn += Sp[a * dh + j] * Sp[a * dh + j]; dot += nv * Sp[a * dh + j];
          }
        }
        if (probe) {
          if (probe->state_norm) probe->state_norm[l * H + h] = (float)sqrt(sn);
          if (probe->state_cos_echo)
            probe->state_cos_echo[l * H + h] = (float)((sn > 1e-18 && spn > 1e-18) ? (dot / sqrt(sn * spn)) : 0.0);
        }
        float *yh = c->ctx_raw + h * dh;
        for (size_t j = 0; j < dh; ++j) {
          double acc = 0.0;
          for (size_t a = 0; a < dh; ++a) acc += (double)qh[a] * S[a * dh + j];
          yh[j] = (float)acc;
        }
      }
    } else if (lo->mixer == BCIR_MIXER_KDA || lo->mixer == BCIR_MIXER_GDN2) {
      int is_g2 = (lo->mixer == BCIR_MIXER_GDN2);
      sp_matvec(d, d, W + lo->wq, c->n1, c->q);
      sp_matvec(d, d, W + lo->wk, c->n1, c->k);
      sp_matvec(d, d, W + lo->wv, c->n1, c->v);
      for (size_t h = 0; h < H; ++h) {
        sp_l2norm(dh, c->q + h * dh, c->q + h * dh);
        sp_l2norm(dh, c->k + h * dh, c->k + h * dh);
      }
      sp_matvec(rg, d, W + lo->wa1, c->n1, c->a_mid);
      sp_matvec(d, rg, W + lo->wa2, c->a_mid, c->a_pre);
      float ashift = apply_ssp ? ssp->gdn_alpha_shift : 0.0f;
      float eshift = apply_ssp ? ssp->gdn_erase_shift : 0.0f;
      float wshift = apply_ssp ? ssp->gdn_write_shift : 0.0f;
      const float *bac = W + lo->ba_chan;
      for (size_t j = 0; j < d; ++j) c->alpha_buf[j] = sp_sigmoid_f(c->a_pre[j] + bac[j] + ashift);
      if (is_g2) {
        sp_matvec(rg, d, W + lo->we1, c->n1, c->e_mid);
        sp_matvec(d, rg, W + lo->we2, c->e_mid, c->e_pre);
        const float *bec = W + lo->be_chan;
        for (size_t j = 0; j < d; ++j) c->erase_buf[j] = sp_sigmoid_f(c->e_pre[j] + bec[j] + eshift);
        sp_matvec(rg, d, W + lo->ww1, c->n1, c->w_mid);
        sp_matvec(d, rg, W + lo->ww2, c->w_mid, c->w_pre);
        const float *bwc = W + lo->bw_chan;
        for (size_t j = 0; j < d; ++j) c->write_buf[j] = sp_sigmoid_f(c->w_pre[j] + bwc[j] + wshift);
      } else {
        sp_matvec(H, d, W + lo->wbeta, c->n1, c->e_pre);
        const float *bb = W + lo->bbeta;
        for (size_t h = 0; h < H; ++h) c->erase_buf[h] = sp_sigmoid_f(c->e_pre[h] + bb[h] + eshift);
      }
      if (probe) {
        if (probe->gdn_alpha) memcpy(probe->gdn_alpha + l * d, c->alpha_buf, d * sizeof(float));
        if (probe->gdn_erase && is_g2) memcpy(probe->gdn_erase + l * d, c->erase_buf, d * sizeof(float));
        if (probe->gdn_write && is_g2) memcpy(probe->gdn_write + l * d, c->write_buf, d * sizeof(float));
      }
      double damp = (apply_ssp && echo_active && ssp->state_echo_damp > 0.0f)
                        ? (1.0 - (double)ssp->state_echo_damp) : 1.0;
      for (size_t h = 0; h < H; ++h) {
        double *S = ls->rec_S + h * dh * dh;
        double *Sp = ls->rec_S_prev + h * dh * dh;
        memcpy(Sp, S, dh * dh * sizeof(double));
        const float *qh = c->q + h * dh, *kh = c->k + h * dh, *vh = c->v + h * dh;
        const float *alh = c->alpha_buf + h * dh;
        const float *erh = is_g2 ? (c->erase_buf + h * dh) : NULL;
        const float *wrh = is_g2 ? (c->write_buf + h * dh) : NULL;
        double bt_s = is_g2 ? 0.0 : (double)c->erase_buf[h];
        for (size_t j = 0; j < dh; ++j) {
          double acc = 0.0;
          for (size_t a = 0; a < dh; ++a) {
            double ba = is_g2 ? (double)erh[a] : bt_s;
            acc += ba * (double)kh[a] * ((double)alh[a] * damp) * S[a * dh + j];
          }
          c->kS_buf[j] = acc;
        }
        double sn = 0.0, spn = 0.0, dot = 0.0;
        for (size_t a = 0; a < dh; ++a) {
          double ala = (double)alh[a] * damp, ka = (double)kh[a];
          for (size_t j = 0; j < dh; ++j) {
            double wd = is_g2 ? (double)wrh[j] : bt_s;
            double nv = ala * S[a * dh + j] - ka * c->kS_buf[j] + ka * wd * (double)vh[j];
            S[a * dh + j] = nv;
            sn += nv * nv; spn += Sp[a * dh + j] * Sp[a * dh + j]; dot += nv * Sp[a * dh + j];
          }
        }
        if (probe) {
          if (probe->state_norm) probe->state_norm[l * H + h] = (float)sqrt(sn);
          if (probe->state_cos_echo)
            probe->state_cos_echo[l * H + h] = (float)((sn > 1e-18 && spn > 1e-18) ? (dot / sqrt(sn * spn)) : 0.0);
        }
        float *yh = c->ctx_raw + h * dh;
        for (size_t j = 0; j < dh; ++j) {
          double acc = 0.0;
          for (size_t a = 0; a < dh; ++a) acc += (double)qh[a] * S[a * dh + j];
          yh[j] = (float)acc;
        }
        sp_rmsnorm(dh, yh, W + lo->o_norm, eps, yh);
      }
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      sp_matvec(d, d, W + lo->wq_c, c->n1, c->q);
      if (c->sp.qk_norm) {
        for (size_t h = 0; h < H; ++h)
          sp_rmsnorm(dh, c->q + h * dh, W + lo->q_norm, eps, c->q + h * dh);
      }
      sp_matvec(dc, d, W + lo->w_dkv, c->n1, c->c_raw);
      sp_rmsnorm(dc, c->c_raw, W + lo->norm_kv, eps, c->c_hat);
      sp_matvec(d, dc, W + lo->w_uk, c->c_hat, c->k);
      sp_matvec(d, dc, W + lo->w_uv, c->c_hat, c->v);
      memcpy(ls->k_cache + (size_t)pos * d, c->k, d * sizeof(float));
      memcpy(ls->v_cache + (size_t)pos * d, c->v, d * sizeof(float));
      if (lo->use_rope) {
        sp_matvec(H * dr, d, W + lo->wq_r, c->n1, c->q_r);
        sp_matvec(dr, d, W + lo->w_kr, c->n1, c->k_r);
        sp_apply_rope_vec(pos, H, dr, c->sp.rope_base, c->q_r);
        sp_apply_rope_vec(pos, 1u, dr, c->sp.rope_base, c->k_r);
        memcpy(ls->kr_cache + (size_t)pos * dr, c->k_r, dr * sizeof(float));
      }
      double scale = 1.0 / sqrt((double)(dh + (lo->use_rope ? dr : 0u)));
      for (size_t h = 0; h < H; ++h) {
        const float *qh = c->q + h * dh;
        const float *qrh = lo->use_rope ? (c->q_r + h * dr) : NULL;
        double mx = -1e30;
        for (uint32_t j = 0; j <= pos; ++j) {
          const float *kj = ls->k_cache + (size_t)j * d + h * dh;
          double s = 0.0;
          for (size_t a = 0; a < dh; ++a) s += (double)qh[a] * (double)kj[a];
          if (lo->use_rope) {
            const float *krj = ls->kr_cache + (size_t)j * dr;
            for (size_t a = 0; a < dr; ++a) s += (double)qrh[a] * (double)krj[a];
          }
          s *= scale;
          if (apply_ssp && ssp->mla_repeat_logit_bias != 0.0f && j < pos && c->tok_hist[j] == token_id) {
            s += (double)ssp->mla_repeat_logit_bias;
          }
          c->scores[j] = s;
          if (s > mx) mx = s;
        }
        double sum = 0.0;
        for (uint32_t j = 0; j <= pos; ++j) { c->scores[j] = exp(c->scores[j] - mx); sum += c->scores[j]; }
        double inv = 1.0 / sum;
        double rep_mass = 0.0, ent = 0.0;
        for (uint32_t j = 0; j <= pos; ++j) {
          double p = c->scores[j] * inv;
          c->scores[j] = p;
          if (p > 1e-12) ent -= p * log(p);
          if (j < pos && c->tok_hist[j] == token_id) rep_mass += p;
        }
        if (probe) {
          if (probe->attn_repeat) probe->attn_repeat[l * H + h] = (float)rep_mass;
          if (probe->attn_entropy) probe->attn_entropy[l * H + h] = (float)ent;
        }
        float *yh = c->ctx_raw + h * dh;
        for (size_t a = 0; a < dh; ++a) {
          double acc = 0.0;
          for (uint32_t j = 0; j <= pos; ++j)
            acc += c->scores[j] * (double)ls->v_cache[(size_t)j * d + h * dh + a];
          yh[a] = (float)acc;
        }
      }
    }

    /* Optional G1 SDPA Sigmoid Output Gate */
    if (c->sp.gate_mode == BCIR_GATE_G1_HEADWISE) {
      sp_matvec(H, d, W + lo->wg1, c->n1, c->g_val);
      const float *bg = W + lo->bg;
      for (size_t h = 0; h < H; ++h) {
        float extra = (apply_ssp && ssp->enable_g1_bias && l + 1u == c->sp.layers && h < BCIR_PROBE_MAX_HEADS)
                          ? ssp->g1_head_bias[h] : 0.0f;
        float gv = sp_sigmoid_f(c->g_val[h] + bg[h] + extra);
        if (probe && probe->g1_gates) probe->g1_gates[l * H + h] = gv;
        for (size_t k = 0; k < dh; ++k) c->ctx_buf[h * dh + k] = c->ctx_raw[h * dh + k] * gv;
      }
    } else if (c->sp.gate_mode == BCIR_GATE_G1_LOWRANK) {
      sp_matvec(rg, d, W + lo->wg1, c->n1, c->g_mid);
      sp_matvec(d, rg, W + lo->wg2, c->g_mid, c->g_val);
      const float *bg = W + lo->bg;
      for (size_t j = 0; j < d; ++j) {
        float gv = sp_sigmoid_f(c->g_val[j] + bg[j]);
        c->ctx_buf[j] = c->ctx_raw[j] * gv;
      }
    } else {
      memcpy(c->ctx_buf, c->ctx_raw, d * sizeof(float));
    }

    /* Output projection + Residual #1 */
    sp_matvec(d, d, W + lo->wo, c->ctx_buf, c->mix_out);
    if (mC > 1u) {
      sp_mhc_sinkhorn(mC, W + lo->mhc_hres2, hres2);
      for (size_t s = 0; s < mC; ++s) {
        hpre2[s] = sp_sigmoid_d((double)W[lo->mhc_hpre2 + s]);
        hpost2[s] = 2.0 * sp_sigmoid_d((double)W[lo->mhc_hpost2 + s]);
      }
      for (size_t r = 0; r < mC; ++r) {
        for (size_t j = 0; j < d; ++j) {
          double acc = hpost1[r] * (double)c->mix_out[j];
          for (size_t s = 0; s < mC; ++s) acc += hres1[r * mC + s] * (double)c->mhc_cur[s * d + j];
          c->mhc_mid[r * d + j] = (float)acc;
        }
      }
      for (size_t j = 0; j < d; ++j) {
        double acc = 0.0;
        for (size_t s = 0; s < mC; ++s) acc += hpre2[s] * (double)c->mhc_mid[s * d + j];
        c->x2[j] = (float)acc;
      }
    } else {
      for (size_t j = 0; j < d; ++j) c->x2[j] = c->x_in[j] + c->mix_out[j];
    }

    /* Pre-MLP RMSNorm + Canon-C + SwiGLU MLP */
    sp_rmsnorm(d, c->x2, W + lo->norm2, eps, c->n2_raw);
    if (c->sp.canon_mode >= BCIR_CANON_AC) {
      canon_step(d, pos, c->n2_raw, W + lo->canon_c, ls->canon_c_hist, c->n2);
    } else {
      memcpy(c->n2, c->n2_raw, d * sizeof(float));
    }
    sp_matvec(ff, d, W + lo->wgate, c->n2, c->gate_buf);
    sp_matvec(ff, d, W + lo->wup, c->n2, c->up_buf);
    for (size_t j = 0; j < ff; ++j) {
      float g = c->gate_buf[j];
      c->hid_buf[j] = (g * sp_sigmoid_f(g)) * c->up_buf[j];
    }
    if (probe && probe->mlp_hidden) memcpy(probe->mlp_hidden + l * ff, c->hid_buf, ff * sizeof(float));
    sp_matvec(d, ff, W + lo->wdown, c->hid_buf, c->mlp_out);

    /* Residual #2 */
    if (mC > 1u) {
      for (size_t r = 0; r < mC; ++r) {
        for (size_t j = 0; j < d; ++j) {
          double acc = hpost2[r] * (double)c->mlp_out[j];
          for (size_t s = 0; s < mC; ++s) acc += hres2[r * mC + s] * (double)c->mhc_mid[s * d + j];
          c->mhc_cur[r * d + j] = (float)acc;
        }
      }
      if (probe && probe->resid_post_mlp) {
        double inv_c = 1.0 / (double)mC;
        for (size_t j = 0; j < d; ++j) {
          double acc = 0.0;
          for (size_t s = 0; s < mC; ++s) acc += (double)c->mhc_cur[s * d + j];
          probe->resid_post_mlp[l * d + j] = (float)(acc * inv_c);
        }
      }
    } else {
      for (size_t j = 0; j < d; ++j) c->x_in[j] = c->x2[j] + c->mlp_out[j];
      if (probe && probe->resid_post_mlp) memcpy(probe->resid_post_mlp + l * d, c->x_in, d * sizeof(float));
    }
  }

  if (mC > 1u) {
    double inv_c = 1.0 / (double)mC;
    for (size_t j = 0; j < d; ++j) {
      double acc = 0.0;
      for (size_t s = 0; s < mC; ++s) acc += (double)c->mhc_cur[s * d + j];
      c->x_final[j] = (float)(acc * inv_c);
    }
  } else {
    memcpy(c->x_final, c->x_in, d * sizeof(float));
  }
  sp_rmsnorm(d, c->x_final, W + c->off_final_norm, eps, c->norm_final);
  sp_matvec(V, d, W + c->off_lm_head, c->norm_final, out_logits);
  c->pos = pos + 1u;
  return 0;
}

static uint32_t select_next_token(const float *raw_logits,
                                  size_t V,
                                  const uint32_t *hist,
                                  uint32_t hist_len,
                                  const bcir_ssp_config *ssp,
                                  float *out_ent,
                                  uint8_t *out_echo) {
  double logits[512];
  if (V > 512u) V = 512u;
  for (size_t v = 0; v < V; ++v) logits[v] = (double)raw_logits[v];

  if (ssp) {
    uint32_t w = ssp->rep_window ? ssp->rep_window : 32u;
    uint32_t start = (hist_len > w) ? (hist_len - w) : 0u;
    if (ssp->rep_penalty > 1.0f || ssp->presence_penalty > 0.0f) {
      uint8_t seen[512];
      memset(seen, 0, V);
      for (uint32_t i = start; i < hist_len; ++i) {
        if (hist[i] < V) seen[hist[i]] = 1u;
      }
      for (size_t v = 0; v < V; ++v) {
        if (!seen[v]) continue;
        if (ssp->rep_penalty > 1.0f) {
          if (logits[v] > 0.0) logits[v] /= (double)ssp->rep_penalty;
          else logits[v] *= (double)ssp->rep_penalty;
        }
        if (ssp->presence_penalty > 0.0f) logits[v] -= (double)ssp->presence_penalty;
      }
    }
    if (ssp->ngram_block_penalty > 0.0f) {
      uint32_t ng = ssp->echo_ngram ? ssp->echo_ngram : 4u;
      if (ng >= 2u && hist_len >= ng) {
        uint32_t ew = ssp->echo_window ? ssp->echo_window : 64u;
        uint32_t es = (hist_len > ew) ? (hist_len - ew) : 0u;
        uint32_t tail = hist_len - (ng - 1u);
        for (uint32_t j = es; j + ng <= hist_len; ++j) {
          int match = 1;
          for (uint32_t k = 0; k + 1u < ng; ++k) {
            if (hist[j + k] != hist[tail + k]) { match = 0; break; }
          }
          if (match) {
            uint32_t banned = hist[j + ng - 1u];
            if (banned < V) logits[banned] -= (double)ssp->ngram_block_penalty;
          }
        }
      }
    }
  }

  double mx = -1e30;
  uint32_t best = 0u;
  for (size_t v = 0; v < V; ++v) {
    if (logits[v] > mx) { mx = logits[v]; best = (uint32_t)v; }
  }
  if (out_ent) {
    double s = 0.0, ent = 0.0;
    for (size_t v = 0; v < V; ++v) s += exp(logits[v] - mx);
    for (size_t v = 0; v < V; ++v) {
      double p = exp(logits[v] - mx) / s;
      if (p > 1e-12) ent -= p * log(p);
    }
    *out_ent = (float)ent;
  }
  if (out_echo) {
    uint32_t ng = (ssp && ssp->echo_ngram) ? ssp->echo_ngram : 4u;
    uint32_t ew = (ssp && ssp->echo_window) ? ssp->echo_window : 48u;
    /* check if appending `best` creates a repeated n-gram */
    int found = 0;
    if (ng >= 2u && hist_len >= ng) {
      uint32_t es = (hist_len > ew) ? (hist_len - ew) : 0u;
      uint32_t tail = hist_len - (ng - 1u);
      for (uint32_t j = es; j + ng <= hist_len; ++j) {
        int match = (hist[j + ng - 1u] == best);
        for (uint32_t k = 0; match && k + 1u < ng; ++k) {
          if (hist[j + k] != hist[tail + k]) match = 0;
        }
        if (match) { found = 1; break; }
      }
    }
    *out_echo = (uint8_t)found;
  }
  return best;
}

static bcir_stateful_ctx *bcir_stateful_clone(const bcir_stateful_ctx *src) {
  bcir_stateful_ctx *c = (bcir_stateful_ctx *)calloc(1u, sizeof(*c));
  if (!c) return NULL;
  c->sp = src->sp;
  c->max_seq = src->max_seq;
  c->n_params = src->n_params;
  c->mhc_c = src->mhc_c;
  c->gate_rank = src->gate_rank;
  c->W = src->W;
  c->off_embed = src->off_embed;
  c->off_final_norm = src->off_final_norm;
  c->off_lm_head = src->off_lm_head;
  memcpy(c->loff, src->loff, sizeof(c->loff));

  size_t d = c->sp.width, H = c->sp.heads, KH = c->sp.kvheads, dh = d / H;
  size_t kvd = KH * dh, ff = c->sp.ff, dc = c->sp.mla_dc, dr = c->sp.mla_dr;
  uint32_t max_seq = c->max_seq;
  for (uint32_t l = 0; l < c->sp.layers; ++l) {
    const layer_offs *lo = &c->loff[l];
    layer_state *ls = &c->lst[l];
    if (c->sp.canon_mode >= BCIR_CANON_A) ls->canon_a_hist = (float *)calloc(BCIR_EXT_CANON_K * d, sizeof(float));
    if (c->sp.canon_mode >= BCIR_CANON_AC) ls->canon_c_hist = (float *)calloc(BCIR_EXT_CANON_K * d, sizeof(float));
    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      ls->k_cache = (float *)calloc((size_t)max_seq * kvd, sizeof(float));
      ls->v_cache = (float *)calloc((size_t)max_seq * kvd, sizeof(float));
    } else if (lo->mixer == BCIR_MIXER_DELTANET || lo->mixer == BCIR_MIXER_KDA || lo->mixer == BCIR_MIXER_GDN2) {
      ls->rec_S = (double *)calloc(H * dh * dh, sizeof(double));
      ls->rec_S_prev = (double *)calloc(H * dh * dh, sizeof(double));
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      ls->k_cache = (float *)calloc((size_t)max_seq * d, sizeof(float));
      ls->v_cache = (float *)calloc((size_t)max_seq * d, sizeof(float));
      if (lo->use_rope) ls->kr_cache = (float *)calloc((size_t)max_seq * dr, sizeof(float));
    }
  }
  c->tok_hist = (uint32_t *)calloc(max_seq, sizeof(uint32_t));
  c->mhc_cur = (float *)calloc(c->mhc_c * d, sizeof(float));
  c->mhc_mid = (float *)calloc(c->mhc_c * d, sizeof(float));
  c->x_in = (float *)calloc(d, sizeof(float));
  c->n1_raw = (float *)calloc(d, sizeof(float));
  c->n1 = (float *)calloc(d, sizeof(float));
  c->x2 = (float *)calloc(d, sizeof(float));
  c->n2_raw = (float *)calloc(d, sizeof(float));
  c->n2 = (float *)calloc(d, sizeof(float));
  c->q = (float *)calloc(d, sizeof(float));
  c->k = (float *)calloc(d, sizeof(float));
  c->v = (float *)calloc(d, sizeof(float));
  c->q_r = (float *)calloc(H * dr + 1u, sizeof(float));
  c->k_r = (float *)calloc(dr + 1u, sizeof(float));
  c->c_raw = (float *)calloc(dc + 1u, sizeof(float));
  c->c_hat = (float *)calloc(dc + 1u, sizeof(float));
  c->g_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->g_val = (float *)calloc(d + 1u, sizeof(float));
  c->a_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->a_pre = (float *)calloc(d + 1u, sizeof(float));
  c->e_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->e_pre = (float *)calloc(d + 1u, sizeof(float));
  c->w_mid = (float *)calloc(c->gate_rank + 1u, sizeof(float));
  c->w_pre = (float *)calloc(d + 1u, sizeof(float));
  c->alpha_buf = (float *)calloc(d + 1u, sizeof(float));
  c->erase_buf = (float *)calloc(d + 1u, sizeof(float));
  c->write_buf = (float *)calloc(d + 1u, sizeof(float));
  c->ctx_raw = (float *)calloc(d, sizeof(float));
  c->ctx_buf = (float *)calloc(d, sizeof(float));
  c->mix_out = (float *)calloc(d, sizeof(float));
  c->gate_buf = (float *)calloc(ff, sizeof(float));
  c->up_buf = (float *)calloc(ff, sizeof(float));
  c->hid_buf = (float *)calloc(ff, sizeof(float));
  c->mlp_out = (float *)calloc(d, sizeof(float));
  c->x_final = (float *)calloc(d, sizeof(float));
  c->norm_final = (float *)calloc(d, sizeof(float));
  c->scores = (double *)calloc(max_seq, sizeof(double));
  c->kS_buf = (double *)calloc(dh, sizeof(double));
  return c;
}

typedef struct gen_worker_arg {
  bcir_stateful_ctx *ctx;
  const uint32_t *prompts;
  size_t p_begin, p_end;
  uint32_t prompt_len, gen_len;
  const bcir_ssp_config *ssp;
  uint32_t *out_tokens;
  float *out_entropy;
  uint8_t *out_echo;
  int rc;
} gen_worker_arg;

static void *gen_worker_fn(void *vp) {
  gen_worker_arg *a = (gen_worker_arg *)vp;
  float logits[512];
  size_t V = a->ctx->sp.vocab;
  uint32_t total = a->prompt_len + a->gen_len;
  a->rc = 0;
  for (size_t p = a->p_begin; p < a->p_end; ++p) {
    bcir_stateful_reset(a->ctx);
    const uint32_t *pr = a->prompts + p * a->prompt_len;
    uint32_t *ot = a->out_tokens + p * total;
    for (uint32_t t = 0; t < a->prompt_len; ++t) {
      ot[t] = pr[t];
      if (bcir_stateful_step(a->ctx, pr[t], 0, NULL, logits, NULL) != 0) { a->rc = 2; return NULL; }
    }
    for (uint32_t g = 0; g < a->gen_len; ++g) {
      float ent = 0.0f;
      uint8_t ec = 0u;
      uint32_t nxt = select_next_token(logits, V, ot, a->prompt_len + g, a->ssp, &ent, &ec);
      ot[a->prompt_len + g] = nxt;
      if (a->out_entropy) a->out_entropy[p * a->gen_len + g] = ent;
      if (a->out_echo) a->out_echo[p * a->gen_len + g] = ec;
      if (g + 1u < a->gen_len) {
        if (bcir_stateful_step(a->ctx, nxt, 1, a->ssp, logits, NULL) != 0) { a->rc = 3; return NULL; }
      }
    }
  }
  return NULL;
}

int bcir_stateful_generate_batch(bcir_stateful_ctx *c,
                                 const uint32_t *prompts,
                                 size_t n_prompts,
                                 uint32_t prompt_len,
                                 uint32_t gen_len,
                                 const bcir_ssp_config *ssp,
                                 uint32_t *out_tokens,
                                 float *out_entropy,
                                 uint8_t *out_echo) {
  if (!c || !prompts || !out_tokens || prompt_len == 0u || prompt_len + gen_len > c->max_seq)
    return 1;
  if (n_prompts >= 4u && c->sp.threads >= 2u) {
    size_t mid = n_prompts / 2u;
    bcir_stateful_ctx *c2 = bcir_stateful_clone(c);
    if (c2) {
      gen_worker_arg a1 = { c, prompts, 0u, mid, prompt_len, gen_len, ssp, out_tokens, out_entropy, out_echo, 0 };
      gen_worker_arg a2 = { c2, prompts, mid, n_prompts, prompt_len, gen_len, ssp, out_tokens, out_entropy, out_echo, 0 };
      pthread_t th;
      pthread_create(&th, NULL, gen_worker_fn, &a2);
      gen_worker_fn(&a1);
      pthread_join(th, NULL);
      bcir_stateful_destroy(c2);
      return (a1.rc != 0) ? a1.rc : a2.rc;
    }
  }
  gen_worker_arg a = { c, prompts, 0u, n_prompts, prompt_len, gen_len, ssp, out_tokens, out_entropy, out_echo, 0 };
  gen_worker_fn(&a);
  return a.rc;
}

int bcir_stateful_probe_contrast(bcir_stateful_ctx *c,
                                 const uint32_t *seqs,
                                 const uint8_t *mask_loop,
                                 size_t n_seqs,
                                 uint32_t seq_len,
                                 bcir_contrast_accum *acc) {
  if (!c || !seqs || !mask_loop || !acc || seq_len == 0u || seq_len > c->max_seq) return 1;
  size_t L = c->sp.layers, d = c->sp.width, ff = c->sp.ff, H = c->sp.heads;
  float *logits = (float *)calloc(c->sp.vocab, sizeof(float));
  float *resid = (float *)calloc(L * d, sizeof(float));
  float *mlph = (float *)calloc(L * ff, sizeof(float));
  float *g1 = (float *)calloc(L * H, sizeof(float));
  float *al = (float *)calloc(L * d, sizeof(float));
  float *er = (float *)calloc(L * d, sizeof(float));
  float *wr = (float *)calloc(L * d, sizeof(float));
  float *ste = (float *)calloc(L * H, sizeof(float));
  float *arep = (float *)calloc(L * H, sizeof(float));
  bcir_probe_step_out po;
  memset(&po, 0, sizeof(po));
  po.resid_post_mlp = resid;
  po.mlp_hidden = mlph;
  po.g1_gates = g1;
  po.gdn_alpha = al;
  po.gdn_erase = er;
  po.gdn_write = wr;
  po.state_cos_echo = ste;
  po.attn_repeat = arep;

  for (size_t i = 0; i < n_seqs; ++i) {
    bcir_stateful_reset(c);
    for (uint32_t t = 0; t < seq_len; ++t) {
      uint8_t m = mask_loop[i * seq_len + t];
      bcir_probe_step_out *pptr = (m == 0u || m == 1u) ? &po : NULL;
      if (bcir_stateful_step(c, seqs[i * seq_len + t], 0, NULL, logits, pptr) != 0) {
        free(logits); free(resid); free(mlph); free(g1); free(al); free(er); free(wr); free(ste); free(arep);
        return 2;
      }
      if (m == 1u) {
        acc->loop_tokens++;
        for (size_t k = 0; k < L * d; ++k) {
          acc->resid_loop[k] += (double)resid[k];
          acc->alpha_loop[k] += (double)al[k];
          acc->erase_loop[k] += (double)er[k];
          acc->write_loop[k] += (double)wr[k];
        }
        for (size_t k = 0; k < L * ff; ++k) {
          acc->mlp_hid_loop[k] += (double)mlph[k];
          acc->mlp_hid_abs_loop[k] += fabs((double)mlph[k]);
        }
        for (size_t k = 0; k < L * H; ++k) {
          acc->g1_loop[k] += (double)g1[k];
          acc->st_echo_loop[k] += (double)ste[k];
          acc->attn_rep_loop[k] += (double)arep[k];
        }
      } else if (m == 0u) {
        acc->clean_tokens++;
        for (size_t k = 0; k < L * d; ++k) {
          acc->resid_clean[k] += (double)resid[k];
          acc->alpha_clean[k] += (double)al[k];
          acc->erase_clean[k] += (double)er[k];
          acc->write_clean[k] += (double)wr[k];
        }
        for (size_t k = 0; k < L * ff; ++k) {
          acc->mlp_hid_clean[k] += (double)mlph[k];
          acc->mlp_hid_abs_clean[k] += fabs((double)mlph[k]);
        }
        for (size_t k = 0; k < L * H; ++k) {
          acc->g1_clean[k] += (double)g1[k];
          acc->st_echo_clean[k] += (double)ste[k];
          acc->attn_rep_clean[k] += (double)arep[k];
        }
      }
    }
  }
  free(logits); free(resid); free(mlph); free(g1); free(al); free(er); free(wr); free(ste); free(arep);
  return 0;
}
