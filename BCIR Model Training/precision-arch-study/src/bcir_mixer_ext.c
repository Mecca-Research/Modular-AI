/*===- bcir_mixer_ext.c - Pluggable Sequence-Mixer ABI (MIXER-EXT) Engine ---===
 * Implements GQA, SWA, Arch-1 Gated DeltaNet (chunkwise WY in FP64 + exact AD),
 * Arch-2 Hybrid-SWA (arbitrary per-layer mixer table), and Arch-4 MLA
 * (decoupled RoPE + kv_norm + absorbed decode equivalence) under BCIR Precision
 * Policy v0.1 (P1-P6) and TRAIN-OPT-1/2/4/6/7.
 *===------------------------------------------------------------------------===*/
#include "bcir_mixer_ext.h"
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e3 + (double)ts.tv_nsec * 1e-6;
}

static void *aligned_calloc(size_t n, size_t sz) {
  size_t bytes = n * sz;
  if (bytes == 0u) bytes = 64u;
  size_t ab = (bytes + 63u) & ~(size_t)63u;
  void *p = NULL;
  if (posix_memalign(&p, 64u, ab) != 0) return NULL;
  memset(p, 0, ab);
  return p;
}

static inline float sigmoid_f(float x) {
  if (x >= 0.0f) { float e = expf(-x); return 1.0f / (1.0f + e); }
  float e = expf(x); return e / (1.0f + e);
}

typedef struct layer_offsets {
  uint32_t mixer;
  uint32_t use_rope;
  size_t norm1, norm2;
  size_t wq, wk, wv, wo;
  size_t walpha, balpha, wbeta, bbeta;
  size_t wq_c, wq_r, w_dkv, norm_kv, w_uk, w_uv, w_kr;
  size_t wgate, wup, wdown;
} layer_offsets;

typedef struct layer_act {
  float *x_in;     /* [B*T*d] */
  float *n1;       /* [B*T*d] */
  /* GQA / SWA */
  float *q, *k, *v, *p;
  /* DeltaNet */
  float *q_raw, *k_raw, *q_inv, *k_inv;
  float *a_pre, *b_pre, *alpha, *beta;
  double *dn_sprev; /* [B*H*nchunks*dh*dh] */
  double *dn_W;     /* [B*H*T*dh] */
  /* MLA */
  float *q_c, *q_r, *c_raw, *c_hat, *k_c, *v_c, *k_r, *mla_p;
  /* Shared post-mixer & MLP */
  float *ctx;      /* [B*T*d] */
  float *x2;       /* [B*T*d] */
  float *n2;       /* [B*T*d] */
  float *gate;     /* [B*T*ff] */
  float *up;       /* [B*T*ff] */
  float *hidden;   /* [B*T*ff] */
} layer_act;

struct bcir_ext_model {
  bcir_ext_spec sp;
  bcir_prec_policy pol;
  bcir_prec_telemetry tel;
  bcir_ext_mem_stats mem;

  void *pool_mem;
  bcir_tensor_pool *pool;

  size_t n_params;
  float *weights;
  float *grad_tile; /* single-tensor FP32 scratch tile allocated only when opt uses grad_bf16 */
  size_t grad_tile_bytes;
  bcir_opt_state *opt;

  size_t n_tensors;
  bcir_ext_tensor_info *tensors;
  layer_offsets loff[BCIR_EXT_MAX_LAYERS];
  size_t off_embed, off_final_norm, off_lm_head;

  layer_act acts[BCIR_EXT_MAX_LAYERS];
  float *x_final;     /* [B*T*d] */
  float *norm_final;  /* [B*T*d] */
  float *logits;      /* [B*T*V] */

  float *rope_hd;     /* [T*dh] */
  float *rope_dr;     /* [T*dr] */

  /* Backward scratch buffers (allocated once for max B*T) */
  float *d_cur, *d_next, *d_n, *d_ctx, *d_gate, *d_up, *d_hid, *d_logits, *rms_inv;
  float *d_q, *d_k, *d_v, *d_a, *d_b;
  float *d_qc, *d_qr, *d_chat, *d_craw, *d_kc, *d_vc, *d_kr;

  void *gemm_scratch;
  size_t gemm_scratch_bytes;
};

static void add_tensor(bcir_ext_model *m, bcir_init_tensor_desc *idescs, size_t *cap,
                       const char *name, int layer, int region, size_t rows, size_t cols,
                       int init_role, float const_val, size_t *out_off) {
  size_t off = m->n_params;
  size_t idx = m->n_tensors;
  if (idx >= *cap) {
    *cap = (*cap == 0u) ? 64u : (*cap * 2u);
    m->tensors = (bcir_ext_tensor_info *)realloc(m->tensors, (*cap) * sizeof(bcir_ext_tensor_info));
  }
  bcir_ext_tensor_info *ti = &m->tensors[idx];
  ti->offset = off; ti->rows = rows; ti->cols = cols; ti->layer = layer; ti->region = region;
  snprintf(ti->name, sizeof(ti->name), "%s", name);
  idescs[idx].offset = off; idescs[idx].rows = rows; idescs[idx].cols = cols;
  idescs[idx].role = init_role; idescs[idx].const_val = const_val;
  m->n_tensors++;
  m->n_params += rows * cols;
  if (region == 1) m->mem.attn_params += rows * cols;
  else if (region == 2) m->mem.mlp_params += rows * cols;
  else m->mem.nonlin_params += rows * cols;
  if (out_off) *out_off = off;
}

static size_t max_sz(size_t a, size_t b) { return a > b ? a : b; }

bcir_ext_model *bcir_ext_create(const bcir_ext_spec *spec, const bcir_prec_policy *pol) {
  if (!spec || !spec->vocab || !spec->width || !spec->heads || !spec->kvheads ||
      !spec->layers || spec->layers > BCIR_EXT_MAX_LAYERS || !spec->ff ||
      !spec->batch || !spec->time || (spec->width % spec->heads) != 0u ||
      (spec->heads % spec->kvheads) != 0u) return NULL;
  double t0 = now_ms();
  bcir_ext_model *m = (bcir_ext_model *)calloc(1u, sizeof(bcir_ext_model));
  if (!m) return NULL;
  m->sp = *spec;
  if (m->sp.swa_window == 0u) m->sp.swa_window = 32u;
  if (m->sp.delta_chunk == 0u) m->sp.delta_chunk = 16u;
  if (m->sp.mla_dc == 0u) m->sp.mla_dc = m->sp.width / 2u;
  if (m->sp.mla_dr == 0u) m->sp.mla_dr = (m->sp.width / m->sp.heads) / 2u;
  if (m->sp.mla_dr < 2u) m->sp.mla_dr = 2u;
  if (m->sp.mla_dr & 1u) m->sp.mla_dr++;
  if (m->sp.threads == 0u) m->sp.threads = 1u;
  if (pol) m->pol = *pol; else bcir_prec_policy_default(&m->pol);
  bcir_prec_telemetry_reset(&m->tel);

  if (m->sp.threads > 1u) {
    size_t psz = bcir_tensor_pool_size(m->sp.threads);
    if (psz > 0u) {
      m->pool_mem = aligned_calloc(psz, 1u);
      m->pool = bcir_tensor_pool_init(m->pool_mem, psz, m->sp.threads);
    }
  }

  size_t d = m->sp.width, H = m->sp.heads, KH = m->sp.kvheads, dh = d / H;
  size_t kvd = KH * dh, ff = m->sp.ff, V = m->sp.vocab, B = m->sp.batch, T = m->sp.time;
  size_t dc = m->sp.mla_dc, dr = m->sp.mla_dr;

  size_t max_desc = 4u + (size_t)m->sp.layers * 16u;
  bcir_init_tensor_desc *idescs = (bcir_init_tensor_desc *)calloc(max_desc, sizeof(bcir_init_tensor_desc));
  size_t tcap = max_desc;
  m->tensors = (bcir_ext_tensor_info *)calloc(tcap, sizeof(bcir_ext_tensor_info));

  add_tensor(m, idescs, &tcap, "embed", -1, 0, V, d, 0, 0.0f, &m->off_embed);
  char nm[48];
  for (uint32_t l = 0; l < m->sp.layers; ++l) {
    layer_offsets *lo = &m->loff[l];
    lo->mixer = m->sp.layer_mixer[l];
    lo->use_rope = m->sp.layer_use_rope[l];
    snprintf(nm, sizeof(nm), "L%u.norm1", l);
    add_tensor(m, idescs, &tcap, nm, (int)l, 0, 1u, d, 1, 1.0f, &lo->norm1);
    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      snprintf(nm, sizeof(nm), "L%u.wq", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 2, 0.0f, &lo->wq);
      snprintf(nm, sizeof(nm), "L%u.wk", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, kvd, d, 2, 0.0f, &lo->wk);
      snprintf(nm, sizeof(nm), "L%u.wv", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, kvd, d, 2, 0.0f, &lo->wv);
      snprintf(nm, sizeof(nm), "L%u.wo", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 3, 0.0f, &lo->wo);
    } else if (lo->mixer == BCIR_MIXER_DELTANET) {
      snprintf(nm, sizeof(nm), "L%u.wq", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 2, 0.0f, &lo->wq);
      snprintf(nm, sizeof(nm), "L%u.wk", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 2, 0.0f, &lo->wk);
      snprintf(nm, sizeof(nm), "L%u.wv", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 2, 0.0f, &lo->wv);
      snprintf(nm, sizeof(nm), "L%u.walpha", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, H, d, 2, 0.0f, &lo->walpha);
      snprintf(nm, sizeof(nm), "L%u.balpha", l); add_tensor(m, idescs, &tcap, nm, (int)l, 0, 1u, H, 5, 2.5f, &lo->balpha);
      snprintf(nm, sizeof(nm), "L%u.wbeta", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, H, d, 2, 0.0f, &lo->wbeta);
      snprintf(nm, sizeof(nm), "L%u.bbeta", l); add_tensor(m, idescs, &tcap, nm, (int)l, 0, 1u, H, 5, -2.0f, &lo->bbeta);
      snprintf(nm, sizeof(nm), "L%u.wo", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 3, 0.0f, &lo->wo);
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      snprintf(nm, sizeof(nm), "L%u.wq_c", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 2, 0.0f, &lo->wq_c);
      snprintf(nm, sizeof(nm), "L%u.wq_r", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, H * dr, d, 2, 0.0f, &lo->wq_r);
      snprintf(nm, sizeof(nm), "L%u.w_dkv", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, dc, d, 2, 0.0f, &lo->w_dkv);
      snprintf(nm, sizeof(nm), "L%u.norm_kv", l); add_tensor(m, idescs, &tcap, nm, (int)l, 0, 1u, dc, 1, 1.0f, &lo->norm_kv);
      snprintf(nm, sizeof(nm), "L%u.w_uk", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, dc, 2, 0.0f, &lo->w_uk);
      snprintf(nm, sizeof(nm), "L%u.w_uv", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, dc, 2, 0.0f, &lo->w_uv);
      snprintf(nm, sizeof(nm), "L%u.w_kr", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, dr, d, 2, 0.0f, &lo->w_kr);
      snprintf(nm, sizeof(nm), "L%u.wo", l); add_tensor(m, idescs, &tcap, nm, (int)l, 1, d, d, 3, 0.0f, &lo->wo);
    }
    snprintf(nm, sizeof(nm), "L%u.norm2", l); add_tensor(m, idescs, &tcap, nm, (int)l, 0, 1u, d, 1, 1.0f, &lo->norm2);
    snprintf(nm, sizeof(nm), "L%u.wgate", l); add_tensor(m, idescs, &tcap, nm, (int)l, 2, ff, d, 2, 0.0f, &lo->wgate);
    snprintf(nm, sizeof(nm), "L%u.wup", l); add_tensor(m, idescs, &tcap, nm, (int)l, 2, ff, d, 2, 0.0f, &lo->wup);
    snprintf(nm, sizeof(nm), "L%u.wdown", l); add_tensor(m, idescs, &tcap, nm, (int)l, 2, d, ff, 3, 0.0f, &lo->wdown);
  }
  add_tensor(m, idescs, &tcap, "final_norm", -1, 0, 1u, d, 1, 1.0f, &m->off_final_norm);
  if (!m->sp.tied) add_tensor(m, idescs, &tcap, "lm_head", -1, 0, V, d, 0, 0.0f, &m->off_lm_head);
  else m->off_lm_head = m->off_embed;

  m->weights = (float *)aligned_calloc(m->n_params, sizeof(float));
  bcir_opt_tensor_shape *oshapes = (bcir_opt_tensor_shape *)calloc(m->n_tensors, sizeof(bcir_opt_tensor_shape));
  if (!m->sp.skip_init) {
    bcir_init_weights_c11(m->weights, m->n_params, idescs, m->n_tensors,
                          m->sp.layers, m->sp.width, m->sp.mup_d_base,
                          m->sp.seed, m->sp.init_policy, oshapes);
  } else {
    double mup_lr_scale = (m->sp.mup_d_base > 0u && m->sp.width > 0u) ? ((double)m->sp.mup_d_base / (double)m->sp.width) : 1.0;
    for (size_t s = 0; s < m->n_tensors; ++s) {
      oshapes[s].offset = idescs[s].offset; oshapes[s].rows = idescs[s].rows; oshapes[s].cols = idescs[s].cols;
      oshapes[s].apply_wd = (idescs[s].role == 1 || idescs[s].role == 4 || idescs[s].role == 5) ? 0 : 1;
      oshapes[s].lr_mult = (m->sp.init_policy == BCIR_INIT_MUP_V1 && (idescs[s].role == 2 || idescs[s].role == 3)) ? mup_lr_scale : 1.0;
    }
  }
  m->opt = bcir_opt_create(m->pol.opt_kind, m->n_params, oshapes, m->n_tensors);
  if (m->opt->grad_fp32 == NULL) {
    size_t max_t_elems = 0u;
    for (size_t s = 0; s < m->n_tensors; ++s) {
      size_t elems = m->tensors[s].rows * m->tensors[s].cols;
      if (elems > max_t_elems) max_t_elems = elems;
    }
    m->grad_tile_bytes = max_t_elems * sizeof(float);
    m->grad_tile = (float *)aligned_calloc(max_t_elems > 0u ? max_t_elems : 1u, sizeof(float));
  }
  free(idescs); free(oshapes);

  /* Allocate activations & track memory */
  size_t N = B * T, act_b = 0u, prob_b = 0u, kv_per_tok = 0u, rec_b = 0u;
  int has_dn = 0, has_mla = 0;
  for (uint32_t l = 0; l < m->sp.layers; ++l) {
    layer_act *a = &m->acts[l];
    uint32_t mx = m->loff[l].mixer;
    a->x_in = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
    a->n1 = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
    a->ctx = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
    a->x2 = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
    a->n2 = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
    a->gate = (float *)aligned_calloc(N * ff, 4u); act_b += N * ff * 4u;
    a->up = (float *)aligned_calloc(N * ff, 4u); act_b += N * ff * 4u;
    a->hidden = (float *)aligned_calloc(N * ff, 4u); act_b += N * ff * 4u;
    if (mx == BCIR_MIXER_GQA) {
      a->q = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
      a->k = (float *)aligned_calloc(N * kvd, 4u); act_b += N * kvd * 4u;
      a->v = (float *)aligned_calloc(N * kvd, 4u); act_b += N * kvd * 4u;
      a->p = (float *)aligned_calloc(B * H * T * T, 4u); prob_b += B * H * T * T * 4u;
      kv_per_tok += 2u * kvd * 4u;
    } else if (mx == BCIR_MIXER_SWA) {
      size_t W = m->sp.swa_window;
      a->q = (float *)aligned_calloc(N * d, 4u); act_b += N * d * 4u;
      a->k = (float *)aligned_calloc(N * kvd, 4u); act_b += N * kvd * 4u;
      a->v = (float *)aligned_calloc(N * kvd, 4u); act_b += N * kvd * 4u;
      a->p = (float *)aligned_calloc(B * H * T * W, 4u); prob_b += B * H * T * W * 4u;
      kv_per_tok += 2u * kvd * 4u; /* capped at W tokens in deploy */
    } else if (mx == BCIR_MIXER_DELTANET) {
      has_dn = 1;
      size_t C = m->sp.delta_chunk, nc = (T + C - 1u) / C;
      a->q_raw = (float *)aligned_calloc(N * d, 4u); a->k_raw = (float *)aligned_calloc(N * d, 4u);
      a->q = (float *)aligned_calloc(N * d, 4u); a->k = (float *)aligned_calloc(N * d, 4u);
      a->v = (float *)aligned_calloc(N * d, 4u);
      a->q_inv = (float *)aligned_calloc(N * H, 4u); a->k_inv = (float *)aligned_calloc(N * H, 4u);
      a->a_pre = (float *)aligned_calloc(N * H, 4u); a->b_pre = (float *)aligned_calloc(N * H, 4u);
      a->alpha = (float *)aligned_calloc(N * H, 4u); a->beta = (float *)aligned_calloc(N * H, 4u);
      a->dn_sprev = (double *)aligned_calloc(B * H * nc * dh * dh, sizeof(double));
      a->dn_W = (double *)aligned_calloc(B * H * T * dh, sizeof(double));
      act_b += (5u * N * d + 6u * N * H) * 4u;
      prob_b += (B * H * nc * dh * dh + B * H * T * dh) * sizeof(double);
      rec_b += H * dh * dh * sizeof(double);
    } else if (mx == BCIR_MIXER_MLA) {
      has_mla = 1;
      a->q_c = (float *)aligned_calloc(N * d, 4u); a->q_r = (float *)aligned_calloc(N * H * dr, 4u);
      a->c_raw = (float *)aligned_calloc(N * dc, 4u); a->c_hat = (float *)aligned_calloc(N * dc, 4u);
      a->k_c = (float *)aligned_calloc(N * d, 4u); a->v_c = (float *)aligned_calloc(N * d, 4u);
      a->k_r = (float *)aligned_calloc(N * dr, 4u);
      a->mla_p = (float *)aligned_calloc(B * H * T * T, 4u);
      act_b += (3u * N * d + N * H * dr + 2u * N * dc + N * dr) * 4u;
      prob_b += B * H * T * T * 4u;
      kv_per_tok += (dc + dr) * 4u;
    }
  }
  m->x_final = (float *)aligned_calloc(N * d, 4u);
  m->norm_final = (float *)aligned_calloc(N * d, 4u);
  m->logits = (float *)aligned_calloc(N * V, 4u);
  act_b += (2u * N * d + N * V) * 4u;

  m->rope_hd = (float *)aligned_calloc(T * dh, 4u);
  bcir_tensor_rope_table_pool(m->pool, T, dh, m->sp.rope_base, m->rope_hd);
  m->rope_dr = (float *)aligned_calloc(T * dr, 4u);
  bcir_tensor_rope_table_pool(m->pool, T, dr, m->sp.rope_base, m->rope_dr);

  size_t scr_b = m->grad_tile_bytes;
  m->d_cur = (float *)aligned_calloc(N * d, 4u); m->d_next = (float *)aligned_calloc(N * d, 4u);
  m->d_n = (float *)aligned_calloc(N * d, 4u); m->d_ctx = (float *)aligned_calloc(N * d, 4u);
  m->d_gate = (float *)aligned_calloc(N * ff, 4u); m->d_up = (float *)aligned_calloc(N * ff, 4u);
  m->d_hid = (float *)aligned_calloc(N * ff, 4u); m->d_logits = (float *)aligned_calloc(N * V, 4u);
  m->rms_inv = (float *)aligned_calloc(N, 4u);
  m->d_q = (float *)aligned_calloc(N * d, 4u); m->d_k = (float *)aligned_calloc(N * d, 4u);
  m->d_v = (float *)aligned_calloc(N * d, 4u);
  scr_b += (7u * N * d + 3u * N * ff + N * V + N) * 4u;
  if (has_dn) {
    m->d_a = (float *)aligned_calloc(N * H, 4u); m->d_b = (float *)aligned_calloc(N * H, 4u);
    scr_b += 2u * N * H * 4u;
  }
  if (has_mla) {
    m->d_qc = (float *)aligned_calloc(N * d, 4u); m->d_qr = (float *)aligned_calloc(N * H * dr, 4u);
    m->d_chat = (float *)aligned_calloc(N * dc, 4u); m->d_craw = (float *)aligned_calloc(N * dc, 4u);
    m->d_kc = (float *)aligned_calloc(N * d, 4u); m->d_vc = (float *)aligned_calloc(N * d, 4u);
    m->d_kr = (float *)aligned_calloc(N * dr, 4u);
    scr_b += (3u * N * d + N * H * dr + 2u * N * dc + N * dr) * 4u;
  }

  /* GEMM workspace sized for worst-case projection in active policy */
  size_t max_dim = max_sz(max_sz(d, ff), max_sz(V, dc));
  size_t gscr = 0u;
  int gkinds[4] = { m->pol.attn_fwd_gemm, m->pol.attn_bwd_gemm, m->pol.mlp_fwd_gemm, m->pol.mlp_bwd_gemm };
  for (int i = 0; i < 4; ++i) {
    gscr = max_sz(gscr, bcir_prec_mm_scratch_bytes(gkinds[i], N, max_dim, max_dim));
    gscr = max_sz(gscr, bcir_prec_mm_scratch_bytes(gkinds[i], max_dim, max_dim, N));
  }
  if (gscr > 0u) {
    m->gemm_scratch = aligned_calloc(gscr, 1u);
    m->gemm_scratch_bytes = gscr;
    scr_b += gscr;
  }

  m->mem.n_params = m->n_params;
  m->mem.weight_bytes = m->n_params * 4u;
  m->mem.opt_only_bytes = m->opt->opt_only_bytes;
  m->mem.persistent_bytes = m->opt->persistent_bytes;
  m->mem.activation_bytes = act_b;
  m->mem.mixer_prob_bytes = prob_b;
  m->mem.scratch_bytes = scr_b;
  m->mem.total_bytes = m->mem.persistent_bytes + act_b + prob_b + scr_b;
  m->mem.infer_kv_bytes_per_tok = kv_per_tok;
  m->mem.infer_rec_state_bytes = rec_b;
  m->mem.init_ms = now_ms() - t0;
  return m;
}

void bcir_ext_destroy(bcir_ext_model *m) {
  if (!m) return;
  if (m->pool) bcir_tensor_pool_stop(m->pool);
  free(m->pool_mem);
  bcir_opt_destroy(m->opt);
  free(m->weights); free(m->grad_tile); free(m->tensors);
  for (uint32_t l = 0; l < m->sp.layers; ++l) {
    layer_act *a = &m->acts[l];
    free(a->x_in); free(a->n1); free(a->q); free(a->k); free(a->v); free(a->p);
    free(a->q_raw); free(a->k_raw); free(a->q_inv); free(a->k_inv);
    free(a->a_pre); free(a->b_pre); free(a->alpha); free(a->beta);
    free(a->dn_sprev); free(a->dn_W);
    free(a->q_c); free(a->q_r); free(a->c_raw); free(a->c_hat);
    free(a->k_c); free(a->v_c); free(a->k_r); free(a->mla_p);
    free(a->ctx); free(a->x2); free(a->n2); free(a->gate); free(a->up); free(a->hidden);
  }
  free(m->x_final); free(m->norm_final); free(m->logits);
  free(m->rope_hd); free(m->rope_dr);
  free(m->d_cur); free(m->d_next); free(m->d_n); free(m->d_ctx);
  free(m->d_gate); free(m->d_up); free(m->d_hid); free(m->d_logits); free(m->rms_inv);
  free(m->d_q); free(m->d_k); free(m->d_v); free(m->d_a); free(m->d_b);
  free(m->d_qc); free(m->d_qr); free(m->d_chat); free(m->d_craw);
  free(m->d_kc); free(m->d_vc); free(m->d_kr); free(m->gemm_scratch);
  free(m);
}

void bcir_ext_get_mem_stats(const bcir_ext_model *m, bcir_ext_mem_stats *out) { if (m && out) *out = m->mem; }
void bcir_ext_get_telemetry(const bcir_ext_model *m, bcir_prec_telemetry *out) { if (m && out) *out = m->tel; }
void bcir_ext_reset_opt(bcir_ext_model *m) { if (m && m->opt) bcir_opt_reset(m->opt); }
float *bcir_ext_weights_ptr(bcir_ext_model *m) { return m ? m->weights : NULL; }
float *bcir_ext_grads_fp32_ptr(bcir_ext_model *m) {
  if (!m) return NULL;
  return m->opt->grad_fp32;
}
size_t bcir_ext_param_count(const bcir_ext_model *m) { return m ? m->n_params : 0u; }
size_t bcir_ext_tensor_count(const bcir_ext_model *m) { return m ? m->n_tensors : 0u; }
int bcir_ext_tensor_info_at(const bcir_ext_model *m, size_t idx, bcir_ext_tensor_info *out) {
  if (!m || !out || idx >= m->n_tensors) return 1;
  *out = m->tensors[idx]; return 0;
}

/* ==================== Linear Dispatch (Region Policy + Pair Fusion) ==================== */
static void ext_linear_fwd(bcir_ext_model *m, int region, size_t rows, size_t in, size_t out,
                           const float *x, const float *w, float *y) {
  int gk = (region == 1) ? m->pol.attn_fwd_gemm : (region == 2 ? m->pol.mlp_fwd_gemm : BCIR_GEMM_FP32_KBLOCK);
  if (region == 1) m->tel.gemm_calls_attn++; else if (region == 2) m->tel.gemm_calls_mlp++;
  bcir_prec_mm_pool(m->pool, gk, m->pol.use_vdp_portable, 0, 1, rows, out, in,
                    1.0f, x, w, 0.0f, y, m->gemm_scratch, m->gemm_scratch_bytes);
}

static void ext_linear_bwd(bcir_ext_model *m, int region, size_t rows, size_t in, size_t out,
                           const float *x, const float *w, const float *dy,
                           float *dx, float *dw, float dx_beta, float dw_beta) {
  int gk = (region == 1) ? m->pol.attn_bwd_gemm : (region == 2 ? m->pol.mlp_bwd_gemm : BCIR_GEMM_FP32_KBLOCK);
  if (region == 1) m->tel.gemm_calls_attn += 2u; else if (region == 2) m->tel.gemm_calls_mlp += 2u;
  if (gk == BCIR_GEMM_FP32_KBLOCK) {
    bcir_tensor_mm_call cx = { 0, 0, rows, in, out, 1.0f, dy, w, dx_beta, dx };
    bcir_tensor_mm_call cw = { 1, 0, out, in, rows, 1.0f, dy, x, dw_beta, dw };
    bcir_tensor_mm_pair_pool(m->pool, 2, &cx, &cw);
  } else {
    bcir_prec_mm_pool(m->pool, gk, m->pol.use_vdp_portable, 0, 0, rows, in, out,
                      1.0f, dy, w, dx_beta, dx, m->gemm_scratch, m->gemm_scratch_bytes);
    bcir_prec_mm_pool(m->pool, gk, m->pol.use_vdp_portable, 1, 0, out, in, rows,
                      1.0f, dy, x, dw_beta, dw, m->gemm_scratch, m->gemm_scratch_bytes);
  }
}

/* ==================== RMSNorm (with Rule P3 FP64 Guard) ==================== */
static void ext_rms_fwd(bcir_ext_model *m, size_t rows, size_t width, const float *x, const float *g, float *y) {
  if (m->pol.fp64_guard_mode == BCIR_FP64_GUARD_OFF) {
    bcir_tensor_rms_pool(m->pool, rows, width, (float)m->sp.rms_eps, x, g, y);
    return;
  }
  double eps = m->sp.rms_eps;
  int force64 = (m->pol.fp64_guard_mode == BCIR_FP64_GUARD_ALWAYS);
  double min_r = m->tel.min_rms_observed;
  uint64_t promos = 0u;
  for (size_t r = 0; r < rows; ++r) {
    const float *xr = x + r * width;
    float *yr = y + r * width;
    double ss = 0.0;
    for (size_t c = 0; c < width; ++c) { double v = (double)xr[c]; ss += v * v; }
    double rms = sqrt(ss / (double)width + eps);
    if (rms < min_r) min_r = rms;
    if (force64 || rms < m->pol.rmsnorm_cond_thresh) {
      promos++;
      double inv = 1.0 / rms;
      for (size_t c = 0; c < width; ++c) yr[c] = (float)((double)xr[c] * inv * (double)g[c]);
    } else {
      float inv = 1.0f / (float)rms;
      for (size_t c = 0; c < width; ++c) yr[c] = (xr[c] * inv) * g[c];
    }
  }
  m->tel.min_rms_observed = min_r;
  m->tel.fp64_norm_promotions += promos;
}

static void ext_rms_bwd(bcir_ext_model *m, size_t rows, size_t width, const float *x, const float *g,
                        const float *dy, float *dx, float *dg, int dx_add) {
  if (m->pol.fp64_guard_mode == BCIR_FP64_GUARD_OFF) {
    unsigned how = dx_add ? BCIR_TENSOR_RMSB_ADD : 0u;
    bcir_tensor_rms_backward_into_pool(m->pool, rows, width, (float)m->sp.rms_eps, x, g, dy, dx, dg, m->rms_inv, how);
    return;
  }
  double eps = m->sp.rms_eps;
  double *dg64 = (double *)calloc(width, sizeof(double));
  for (size_t r = 0; r < rows; ++r) {
    const float *xr = x + r * width, *dyr = dy + r * width;
    float *dxr = dx + r * width;
    double ss = 0.0;
    for (size_t c = 0; c < width; ++c) { double v = (double)xr[c]; ss += v * v; }
    double inv = 1.0 / sqrt(ss / (double)width + eps);
    double dot = 0.0;
    for (size_t c = 0; c < width; ++c) {
      double gdy = (double)g[c] * (double)dyr[c];
      dot += gdy * (double)xr[c];
      dg64[c] += (double)dyr[c] * ((double)xr[c] * inv);
    }
    double coeff = (dot * inv * inv) / (double)width;
    for (size_t c = 0; c < width; ++c) {
      double val = inv * ((double)g[c] * (double)dyr[c] - (double)xr[c] * coeff);
      dxr[c] = dx_add ? (float)((double)dxr[c] + val) : (float)val;
    }
  }
  for (size_t c = 0; c < width; ++c) dg[c] = (float)((double)dg[c] + dg64[c]);
  free(dg64);
}

/* ==================== SWA (Sliding Window Attention, O(T*W) storage) ==================== */
typedef struct swa_fwd_ctx {
  size_t B, T, H, KH, dh, W;
  const float *q, *k, *v;
  float *p, *y;
  int fp64_mode;
  double logit_thresh;
  double *max_logits;
  uint64_t *promos;
} swa_fwd_ctx;

static void swa_fwd_worker(void *vctx, size_t task) {
  swa_fwd_ctx *c = (swa_fwd_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H, kvh = h / (c->H / c->KH);
  size_t T = c->T, dh = c->dh, W = c->W;
  double scale = 1.0 / sqrt((double)dh);
  double max_obs = 0.0; uint64_t pr = 0u;
  double *scores = (double *)malloc(W * sizeof(double));
  for (size_t i = 0; i < T; ++i) {
    size_t j0 = (i + 1u > W) ? (i + 1u - W) : 0u;
    size_t len = i + 1u - j0;
    const float *qi = c->q + ((b * T + i) * c->H + h) * dh;
    double mx = -1e30;
    for (size_t u = 0; u < len; ++u) {
      size_t j = j0 + u;
      const float *kj = c->k + ((b * T + j) * c->KH + kvh) * dh;
      double s = 0.0;
      for (size_t d = 0; d < dh; ++d) s += (double)qi[d] * (double)kj[d];
      s *= scale;
      scores[u] = s;
      if (s > mx) mx = s;
      if (fabs(s) > max_obs) max_obs = fabs(s);
    }
    if (c->fp64_mode == BCIR_FP64_GUARD_ALWAYS || (c->fp64_mode == BCIR_FP64_GUARD_CONDITIONAL && fabs(mx) > c->logit_thresh)) pr++;
    double sum = 0.0;
    for (size_t u = 0; u < len; ++u) { scores[u] = exp(scores[u] - mx); sum += scores[u]; }
    double inv = 1.0 / sum;
    float *pi = c->p + ((b * c->H + h) * T + i) * W;
    for (size_t u = 0; u < len; ++u) pi[u] = (float)(scores[u] * inv);
    for (size_t u = len; u < W; ++u) pi[u] = 0.0f;
    float *yi = c->y + ((b * T + i) * c->H + h) * dh;
    for (size_t d = 0; d < dh; ++d) {
      double acc = 0.0;
      for (size_t u = 0; u < len; ++u) {
        size_t j = j0 + u;
        const float *vj = c->v + ((b * T + j) * c->KH + kvh) * dh;
        acc += (double)pi[u] * (double)vj[d];
      }
      yi[d] = (float)acc;
    }
  }
  free(scores);
  c->max_logits[task] = max_obs;
  c->promos[task] = pr;
}

typedef struct swa_bwd_ctx {
  size_t B, T, H, KH, dh, W;
  const float *q, *k, *v, *p, *dy;
  float *dq, *dk, *dv;
} swa_bwd_ctx;

static void swa_bwd_dq_worker(void *vctx, size_t task) {
  swa_bwd_ctx *c = (swa_bwd_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H, kvh = h / (c->H / c->KH);
  size_t T = c->T, dh = c->dh, W = c->W;
  double scale = 1.0 / sqrt((double)dh);
  double *dp = (double *)malloc(W * sizeof(double));
  for (size_t i = 0; i < T; ++i) {
    size_t j0 = (i + 1u > W) ? (i + 1u - W) : 0u;
    size_t len = i + 1u - j0;
    const float *dyi = c->dy + ((b * T + i) * c->H + h) * dh;
    const float *pi = c->p + ((b * c->H + h) * T + i) * W;
    double dot = 0.0;
    for (size_t u = 0; u < len; ++u) {
      size_t j = j0 + u;
      const float *vj = c->v + ((b * T + j) * c->KH + kvh) * dh;
      double s = 0.0;
      for (size_t d = 0; d < dh; ++d) s += (double)dyi[d] * (double)vj[d];
      dp[u] = s; dot += (double)pi[u] * s;
    }
    float *dqi = c->dq + ((b * T + i) * c->H + h) * dh;
    for (size_t d = 0; d < dh; ++d) {
      double acc = 0.0;
      for (size_t u = 0; u < len; ++u) {
        size_t j = j0 + u;
        const float *kj = c->k + ((b * T + j) * c->KH + kvh) * dh;
        double ds = (double)pi[u] * (dp[u] - dot) * scale;
        acc += ds * (double)kj[d];
      }
      dqi[d] = (float)acc;
    }
  }
  free(dp);
}

static void swa_bwd_dkv_worker(void *vctx, size_t task) {
  swa_bwd_ctx *c = (swa_bwd_ctx *)vctx;
  size_t b = task / c->KH, kvh = task % c->KH, grp = c->H / c->KH;
  size_t T = c->T, dh = c->dh, W = c->W;
  double scale = 1.0 / sqrt((double)dh);
  double *dk_loc = (double *)calloc(T * dh, sizeof(double));
  double *dv_loc = (double *)calloc(T * dh, sizeof(double));
  double *dp = (double *)malloc(W * sizeof(double));
  for (size_t g = 0; g < grp; ++g) {
    size_t h = kvh * grp + g;
    for (size_t i = 0; i < T; ++i) {
      size_t j0 = (i + 1u > W) ? (i + 1u - W) : 0u;
      size_t len = i + 1u - j0;
      const float *qi = c->q + ((b * T + i) * c->H + h) * dh;
      const float *dyi = c->dy + ((b * T + i) * c->H + h) * dh;
      const float *pi = c->p + ((b * c->H + h) * T + i) * W;
      double dot = 0.0;
      for (size_t u = 0; u < len; ++u) {
        size_t j = j0 + u;
        const float *vj = c->v + ((b * T + j) * c->KH + kvh) * dh;
        double s = 0.0;
        for (size_t d = 0; d < dh; ++d) s += (double)dyi[d] * (double)vj[d];
        dp[u] = s; dot += (double)pi[u] * s;
      }
      for (size_t u = 0; u < len; ++u) {
        size_t j = j0 + u;
        double pv = (double)pi[u];
        double ds = pv * (dp[u] - dot) * scale;
        double *dkj = dk_loc + j * dh, *dvj = dv_loc + j * dh;
        for (size_t d = 0; d < dh; ++d) {
          dkj[d] += ds * (double)qi[d];
          dvj[d] += pv * (double)dyi[d];
        }
      }
    }
  }
  for (size_t j = 0; j < T; ++j) {
    float *dkj = c->dk + ((b * T + j) * c->KH + kvh) * dh;
    float *dvj = c->dv + ((b * T + j) * c->KH + kvh) * dh;
    for (size_t d = 0; d < dh; ++d) { dkj[d] = (float)dk_loc[j * dh + d]; dvj[d] = (float)dv_loc[j * dh + d]; }
  }
  free(dp); free(dk_loc); free(dv_loc);
}

/* ==================== GQA FP64-checked Attention ==================== */
static void gqa_fwd_dispatch(bcir_ext_model *m, size_t B, size_t T, const float *q, const float *k,
                             const float *v, float *p, float *y, int *bad) {
  if (m->pol.fp64_guard_mode == BCIR_FP64_GUARD_OFF) {
    bcir_tensor_attention_contract_variant_scan_ws_pool(
        m->pool, 0, BCIR_TENSOR_ATTENTION_FUSED, B, T, m->sp.heads, m->sp.kvheads,
        m->sp.width / m->sp.heads, q, k, v, p, y, bad, NULL, 0u);
    return;
  }
  /* Reuse SWA kernel with W = T for exact FP64 softmax & telemetry */
  size_t nt = B * m->sp.heads;
  double *ml = (double *)calloc(nt, sizeof(double));
  uint64_t *pr = (uint64_t *)calloc(nt, sizeof(uint64_t));
  swa_fwd_ctx sc = { B, T, m->sp.heads, m->sp.kvheads, m->sp.width / m->sp.heads, T,
                     q, k, v, p, y, m->pol.fp64_guard_mode, m->pol.softmax_logit_thresh, ml, pr };
  bcir_tensor_pool_run(m->pool, swa_fwd_worker, &sc, nt);
  for (size_t i = 0; i < nt; ++i) {
    if (ml[i] > m->tel.max_softmax_logit) m->tel.max_softmax_logit = ml[i];
    m->tel.fp64_softmax_promotions += pr[i];
  }
  free(ml); free(pr);
}

static void gqa_bwd_dispatch(bcir_ext_model *m, size_t B, size_t T, const float *q, const float *k,
                             const float *v, float *p, const float *dy, float *dq, float *dk, float *dv) {
  if (m->pol.fp64_guard_mode == BCIR_FP64_GUARD_OFF) {
    bcir_tensor_attention_backward_contract_variant_ws_pool(
        m->pool, 0, BCIR_TENSOR_ATTENTION_FUSED, B, T, m->sp.heads, m->sp.kvheads,
        m->sp.width / m->sp.heads, q, k, v, p, dy, dq, dk, dv, NULL, 0u);
    return;
  }
  swa_bwd_ctx sc = { B, T, m->sp.heads, m->sp.kvheads, m->sp.width / m->sp.heads, T,
                     q, k, v, p, dy, dq, dk, dv };
  bcir_tensor_pool_run(m->pool, swa_bwd_dq_worker, &sc, B * m->sp.heads);
  bcir_tensor_pool_run(m->pool, swa_bwd_dkv_worker, &sc, B * m->sp.kvheads);
}

/* ==================== Arch-1 Gated DeltaNet (Chunkwise WY in FP64) ==================== */
static void dn_l2norm_fwd(size_t N, size_t H, size_t dh, const float *x, float *y, float *inv_out) {
  for (size_t i = 0; i < N * H; ++i) {
    const float *xi = x + i * dh; float *yi = y + i * dh;
    double ss = 1e-6;
    for (size_t d = 0; d < dh; ++d) ss += (double)xi[d] * (double)xi[d];
    double inv = 1.0 / sqrt(ss);
    inv_out[i] = (float)inv;
    for (size_t d = 0; d < dh; ++d) yi[d] = (float)((double)xi[d] * inv);
  }
}

static void dn_l2norm_bwd(size_t N, size_t H, size_t dh, const float *y, const float *inv_in,
                          const float *dy, float *dx) {
  for (size_t i = 0; i < N * H; ++i) {
    const float *yi = y + i * dh, *dyi = dy + i * dh; float *dxi = dx + i * dh;
    double inv = (double)inv_in[i], dot = 0.0;
    for (size_t d = 0; d < dh; ++d) dot += (double)dyi[d] * (double)yi[d];
    for (size_t d = 0; d < dh; ++d) dxi[d] = (float)(inv * ((double)dyi[d] - dot * (double)yi[d]));
  }
}

typedef struct dn_fwd_ctx {
  size_t B, T, H, dh, C, nc;
  const float *q, *k, *v, *alpha, *beta;
  double *sprev_all, *W_all;
  float *y;
  double *max_off;
  uint64_t *wy_cnt;
} dn_fwd_ctx;

static void dn_wy_fwd_worker(void *vctx, size_t task) {
  dn_fwd_ctx *c = (dn_fwd_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H;
  size_t T = c->T, H = c->H, dh = c->dh, C = c->C, nc = c->nc;
  double *S = (double *)calloc(dh * dh, sizeof(double));
  double *g = (double *)malloc(C * sizeof(double));
  double *gam = (double *)malloc(C * sizeof(double));
  double *A = (double *)malloc(C * C * sizeof(double));
  double *qS = (double *)malloc(dh * sizeof(double));
  double max_a = 0.0; uint64_t solves = 0u;
  for (size_t ci = 0; ci < nc; ++ci) {
    size_t t0 = ci * C, Lc = (t0 + C <= T) ? C : (T - t0);
    double *Sp = c->sprev_all + (task * nc + ci) * dh * dh;
    memcpy(Sp, S, dh * dh * sizeof(double));
    double gacc = 0.0;
    for (size_t i = 0; i < Lc; ++i) {
      double a = (double)c->alpha[(b * T + t0 + i) * H + h];
      gacc += log(a > 1e-12 ? a : 1e-12);
      g[i] = gacc; gam[i] = exp(gacc);
    }
    for (size_t i = 0; i < Lc; ++i) {
      const float *ki = c->k + ((b * T + t0 + i) * H + h) * dh;
      double bi = (double)c->beta[(b * T + t0 + i) * H + h];
      for (size_t j = 0; j < i; ++j) {
        const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
        double kk = 0.0;
        for (size_t d = 0; d < dh; ++d) kk += (double)ki[d] * (double)kj[d];
        double aij = bi * exp(g[i] - g[j]) * kk;
        A[i * C + j] = aij;
        if (fabs(aij) > max_a) max_a = fabs(aij);
      }
      double *Wi = c->W_all + (task * T + t0 + i) * dh;
      const float *vi = c->v + ((b * T + t0 + i) * H + h) * dh;
      for (size_t d = 0; d < dh; ++d) {
        double kS = 0.0;
        for (size_t a = 0; a < dh; ++a) kS += (double)ki[a] * Sp[a * dh + d];
        double val = bi * (double)vi[d] - bi * gam[i] * kS;
        for (size_t j = 0; j < i; ++j) val -= A[i * C + j] * c->W_all[(task * T + t0 + j) * dh + d];
        Wi[d] = val;
      }
    }
    solves++;
    for (size_t i = 0; i < Lc; ++i) {
      const float *qi = c->q + ((b * T + t0 + i) * H + h) * dh;
      for (size_t d = 0; d < dh; ++d) {
        double acc = 0.0;
        for (size_t a = 0; a < dh; ++a) acc += (double)qi[a] * Sp[a * dh + d];
        qS[d] = gam[i] * acc;
      }
      for (size_t j = 0; j <= i; ++j) {
        const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
        double qk = 0.0;
        for (size_t a = 0; a < dh; ++a) qk += (double)qi[a] * (double)kj[a];
        double fac = exp(g[i] - g[j]) * qk;
        const double *Wj = c->W_all + (task * T + t0 + j) * dh;
        for (size_t d = 0; d < dh; ++d) qS[d] += fac * Wj[d];
      }
      float *yi = c->y + ((b * T + t0 + i) * H + h) * dh;
      for (size_t d = 0; d < dh; ++d) yi[d] = (float)qS[d];
    }
    double glast = g[Lc - 1u], gam_last = gam[Lc - 1u];
    for (size_t a = 0; a < dh * dh; ++a) S[a] = gam_last * Sp[a];
    for (size_t j = 0; j < Lc; ++j) {
      double fac = exp(glast - g[j]);
      const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
      const double *Wj = c->W_all + (task * T + t0 + j) * dh;
      for (size_t a = 0; a < dh; ++a) {
        double fk = fac * (double)kj[a];
        for (size_t d = 0; d < dh; ++d) S[a * dh + d] += fk * Wj[d];
      }
    }
  }
  free(S); free(g); free(gam); free(A); free(qS);
  c->max_off[task] = max_a; c->wy_cnt[task] = solves;
}

typedef struct dn_bwd_ctx {
  size_t B, T, H, dh, C, nc;
  const float *q, *k, *v, *alpha, *beta, *dy;
  const double *sprev_all, *W_all;
  float *dq, *dk, *dv, *da_pre, *db_pre;
} dn_bwd_ctx;

static void dn_wy_bwd_worker(void *vctx, size_t task) {
  dn_bwd_ctx *c = (dn_bwd_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H;
  size_t T = c->T, H = c->H, dh = c->dh, C = c->C, nc = c->nc;
  double *dS = (double *)calloc(dh * dh, sizeof(double));
  double *dS_prev = (double *)malloc(dh * dh * sizeof(double));
  double *g = (double *)malloc(C * sizeof(double));
  double *gam = (double *)malloc(C * sizeof(double));
  double *dg = (double *)malloc(C * sizeof(double));
  double *dbet = (double *)malloc(C * sizeof(double));
  double *A = (double *)malloc(C * C * sizeof(double));
  double *dW = (double *)malloc(C * dh * sizeof(double));
  double *dR = (double *)malloc(C * dh * sizeof(double));
  double *dkc = (double *)malloc(C * dh * sizeof(double));
  double *kS = (double *)malloc(dh * sizeof(double));

  for (size_t ci = nc; ci-- > 0u; ) {
    size_t t0 = ci * C, Lc = (t0 + C <= T) ? C : (T - t0);
    const double *Sp = c->sprev_all + (task * nc + ci) * dh * dh;
    double gacc = 0.0;
    for (size_t i = 0; i < Lc; ++i) {
      double a = (double)c->alpha[(b * T + t0 + i) * H + h];
      gacc += log(a > 1e-12 ? a : 1e-12);
      g[i] = gacc; gam[i] = exp(gacc);
      dg[i] = 0.0; dbet[i] = 0.0;
    }
    memset(dW, 0, Lc * dh * sizeof(double));
    memset(dkc, 0, Lc * dh * sizeof(double));
    for (size_t i = 0; i < Lc; ++i) {
      const float *ki = c->k + ((b * T + t0 + i) * H + h) * dh;
      double bi = (double)c->beta[(b * T + t0 + i) * H + h];
      for (size_t j = 0; j < i; ++j) {
        const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
        double kk = 0.0;
        for (size_t d = 0; d < dh; ++d) kk += (double)ki[d] * (double)kj[d];
        A[i * C + j] = bi * exp(g[i] - g[j]) * kk;
      }
    }
    double glast = g[Lc - 1u], gam_last = gam[Lc - 1u];
    /* 1. From S_next = gam_last * Sp + sum_j exp(glast - g[j]) * k_j * W_j^T */
    {
      double sp_ds = 0.0;
      for (size_t a = 0; a < dh * dh; ++a) sp_ds += Sp[a] * dS[a];
      dg[Lc - 1u] += gam_last * sp_ds;
    }
    for (size_t j = 0; j < Lc; ++j) {
      double fac = exp(glast - g[j]);
      const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
      const double *Wj = c->W_all + (task * T + t0 + j) * dh;
      double uj = 0.0;
      for (size_t d = 0; d < dh; ++d) {
        double kd = 0.0;
        for (size_t a = 0; a < dh; ++a) kd += (double)kj[a] * dS[a * dh + d];
        dW[j * dh + d] += fac * kd;
        uj += fac * kd * Wj[d];
      }
      if (j < Lc - 1u) { dg[Lc - 1u] += uj; dg[j] -= uj; }
      for (size_t a = 0; a < dh; ++a) {
        double dsw = 0.0;
        for (size_t d = 0; d < dh; ++d) dsw += dS[a * dh + d] * Wj[d];
        dkc[j * dh + a] += fac * dsw;
      }
    }
    /* 2. From y_i = gam_i (q_i^T Sp) + sum_{j<=i} exp(g_i - g_j)(q_i^T k_j) W_j */
    for (size_t i = 0; i < Lc; ++i) {
      const float *qi = c->q + ((b * T + t0 + i) * H + h) * dh;
      const float *dyi = c->dy + ((b * T + t0 + i) * H + h) * dh;
      float *dqi = c->dq + ((b * T + t0 + i) * H + h) * dh;
      double dgi_sp = 0.0;
      for (size_t a = 0; a < dh; ++a) {
        double sd = 0.0;
        for (size_t d = 0; d < dh; ++d) sd += Sp[a * dh + d] * (double)dyi[d];
        dqi[a] = (float)(gam[i] * sd);
        dgi_sp += (double)qi[a] * sd;
      }
      dg[i] += gam[i] * dgi_sp;
      for (size_t j = 0; j <= i; ++j) {
        const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
        const double *Wj = c->W_all + (task * T + t0 + j) * dh;
        double qk = 0.0, wdy = 0.0;
        for (size_t a = 0; a < dh; ++a) qk += (double)qi[a] * (double)kj[a];
        for (size_t d = 0; d < dh; ++d) wdy += Wj[d] * (double)dyi[d];
        double eij = exp(g[i] - g[j]);
        double fq = eij * wdy, fw = eij * qk;
        for (size_t a = 0; a < dh; ++a) {
          dqi[a] = (float)((double)dqi[a] + fq * (double)kj[a]);
          dkc[j * dh + a] += fq * (double)qi[a];
        }
        for (size_t d = 0; d < dh; ++d) dW[j * dh + d] += fw * (double)dyi[d];
        if (j < i) { double tij = eij * qk * wdy; dg[i] += tij; dg[j] -= tij; }
      }
    }
    /* 3. Adjoint solve (I + A)^T dR = dW */
    for (size_t j = Lc; j-- > 0u; ) {
      for (size_t d = 0; d < dh; ++d) {
        double val = dW[j * dh + d];
        for (size_t i = j + 1u; i < Lc; ++i) val -= A[i * C + j] * dR[i * dh + d];
        dR[j * dh + d] = val;
      }
    }
    /* 4. Grads through A_{i,j} = beta_i * exp(g_i - g_j) * (k_i^T k_j) */
    for (size_t i = 0; i < Lc; ++i) {
      const float *ki = c->k + ((b * T + t0 + i) * H + h) * dh;
      double bi = (double)c->beta[(b * T + t0 + i) * H + h];
      for (size_t j = 0; j < i; ++j) {
        const float *kj = c->k + ((b * T + t0 + j) * H + h) * dh;
        const double *Wj = c->W_all + (task * T + t0 + j) * dh;
        double dA = 0.0;
        for (size_t d = 0; d < dh; ++d) dA -= dR[i * dh + d] * Wj[d];
        double kk = 0.0;
        for (size_t a = 0; a < dh; ++a) kk += (double)ki[a] * (double)kj[a];
        double eij = exp(g[i] - g[j]);
        dbet[i] += dA * eij * kk;
        dg[i] += dA * A[i * C + j]; dg[j] -= dA * A[i * C + j];
        double fac = dA * bi * eij;
        for (size_t a = 0; a < dh; ++a) { dkc[i * dh + a] += fac * (double)kj[a]; dkc[j * dh + a] += fac * (double)ki[a]; }
      }
    }
    /* 5. Grads through R_i = beta_i * v_i - beta_i * gam_i * (k_i^T Sp) */
    for (size_t i = 0; i < Lc; ++i) {
      const float *ki = c->k + ((b * T + t0 + i) * H + h) * dh;
      const float *vi = c->v + ((b * T + t0 + i) * H + h) * dh;
      float *dvi = c->dv + ((b * T + t0 + i) * H + h) * dh;
      double bi = (double)c->beta[(b * T + t0 + i) * H + h];
      double dr_p = 0.0, dr_v = 0.0;
      for (size_t d = 0; d < dh; ++d) {
        double p_d = 0.0;
        for (size_t a = 0; a < dh; ++a) p_d += (double)ki[a] * Sp[a * dh + d];
        kS[d] = p_d;
        dvi[d] = (float)(bi * dR[i * dh + d]);
        dr_p += dR[i * dh + d] * p_d;
        dr_v += dR[i * dh + d] * (double)vi[d];
      }
      dbet[i] += dr_v - gam[i] * dr_p;
      dg[i] -= bi * gam[i] * dr_p;
      for (size_t a = 0; a < dh; ++a) {
        double sr = 0.0;
        for (size_t d = 0; d < dh; ++d) sr += Sp[a * dh + d] * dR[i * dh + d];
        dkc[i * dh + a] -= bi * gam[i] * sr;
      }
    }
    /* Write dk, da_pre, db_pre */
    for (size_t i = 0; i < Lc; ++i) {
      float *dki = c->dk + ((b * T + t0 + i) * H + h) * dh;
      for (size_t a = 0; a < dh; ++a) dki[a] = (float)dkc[i * dh + a];
    }
    double sfx = 0.0;
    for (size_t s = Lc; s-- > 0u; ) {
      sfx += dg[s];
      double al = (double)c->alpha[(b * T + t0 + s) * H + h];
      double be = (double)c->beta[(b * T + t0 + s) * H + h];
      c->da_pre[(b * T + t0 + s) * H + h] = (float)((1.0 - al) * sfx);
      c->db_pre[(b * T + t0 + s) * H + h] = (float)(be * (1.0 - be) * dbet[s]);
    }
    /* 6. Propagate dS_prev */
    for (size_t a = 0; a < dh * dh; ++a) dS_prev[a] = gam_last * dS[a];
    for (size_t i = 0; i < Lc; ++i) {
      const float *qi = c->q + ((b * T + t0 + i) * H + h) * dh;
      const float *ki = c->k + ((b * T + t0 + i) * H + h) * dh;
      const float *dyi = c->dy + ((b * T + t0 + i) * H + h) * dh;
      double bi = (double)c->beta[(b * T + t0 + i) * H + h];
      for (size_t a = 0; a < dh; ++a) {
        double gq = gam[i] * (double)qi[a];
        double bgk = bi * gam[i] * (double)ki[a];
        for (size_t d = 0; d < dh; ++d)
          dS_prev[a * dh + d] += gq * (double)dyi[d] - bgk * dR[i * dh + d];
      }
    }
    memcpy(dS, dS_prev, dh * dh * sizeof(double));
  }
  free(dS); free(dS_prev); free(g); free(gam); free(dg); free(dbet);
  free(A); free(dW); free(dR); free(dkc); free(kS);
}

/* ==================== Arch-4 MLA (Decoupled RoPE Attention) ==================== */
typedef struct mla_att_ctx {
  size_t B, T, H, dh, dr;
  const float *qc, *qr, *kc, *vc, *kr, *dy;
  float *p, *y, *dqc, *dqr, *dkc, *dvc, *dkr;
  int fp64_mode;
  double logit_thresh;
  double *max_logits;
  uint64_t *promos;
} mla_att_ctx;

static void mla_fwd_worker(void *vctx, size_t task) {
  mla_att_ctx *c = (mla_att_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H, T = c->T, dh = c->dh, dr = c->dr;
  double scale = 1.0 / sqrt((double)(dh + dr));
  double *sc = (double *)malloc(T * sizeof(double));
  double max_obs = 0.0; uint64_t pr = 0u;
  for (size_t i = 0; i < T; ++i) {
    const float *qci = c->qc + ((b * T + i) * c->H + h) * dh;
    const float *qri = c->qr + ((b * T + i) * c->H + h) * dr;
    double mx = -1e30;
    for (size_t j = 0; j <= i; ++j) {
      const float *kcj = c->kc + ((b * T + j) * c->H + h) * dh;
      const float *krj = c->kr + (b * T + j) * dr;
      double s = 0.0;
      for (size_t d = 0; d < dh; ++d) s += (double)qci[d] * (double)kcj[d];
      for (size_t d = 0; d < dr; ++d) s += (double)qri[d] * (double)krj[d];
      s *= scale; sc[j] = s;
      if (s > mx) mx = s;
      if (fabs(s) > max_obs) max_obs = fabs(s);
    }
    if (c->fp64_mode == BCIR_FP64_GUARD_ALWAYS || (c->fp64_mode == BCIR_FP64_GUARD_CONDITIONAL && fabs(mx) > c->logit_thresh)) pr++;
    double sum = 0.0;
    for (size_t j = 0; j <= i; ++j) { sc[j] = exp(sc[j] - mx); sum += sc[j]; }
    double inv = 1.0 / sum;
    float *pi = c->p + ((b * c->H + h) * T + i) * T;
    for (size_t j = 0; j <= i; ++j) pi[j] = (float)(sc[j] * inv);
    for (size_t j = i + 1u; j < T; ++j) pi[j] = 0.0f;
    float *yi = c->y + ((b * T + i) * c->H + h) * dh;
    for (size_t d = 0; d < dh; ++d) {
      double acc = 0.0;
      for (size_t j = 0; j <= i; ++j) acc += (double)pi[j] * (double)c->vc[((b * T + j) * c->H + h) * dh + d];
      yi[d] = (float)acc;
    }
  }
  free(sc); c->max_logits[task] = max_obs; c->promos[task] = pr;
}

static void mla_bwd_bh_worker(void *vctx, size_t task) {
  mla_att_ctx *c = (mla_att_ctx *)vctx;
  size_t b = task / c->H, h = task % c->H, T = c->T, dh = c->dh, dr = c->dr;
  double scale = 1.0 / sqrt((double)(dh + dr));
  double *dp = (double *)malloc(T * sizeof(double));
  double *dkc_l = (double *)calloc(T * dh, sizeof(double));
  double *dvc_l = (double *)calloc(T * dh, sizeof(double));
  for (size_t i = 0; i < T; ++i) {
    const float *qci = c->qc + ((b * T + i) * c->H + h) * dh;
    const float *dyi = c->dy + ((b * T + i) * c->H + h) * dh;
    const float *pi = c->p + ((b * c->H + h) * T + i) * T;
    double dot = 0.0;
    for (size_t j = 0; j <= i; ++j) {
      const float *vcj = c->vc + ((b * T + j) * c->H + h) * dh;
      double s = 0.0;
      for (size_t d = 0; d < dh; ++d) s += (double)dyi[d] * (double)vcj[d];
      dp[j] = s; dot += (double)pi[j] * s;
    }
    float *dqci = c->dqc + ((b * T + i) * c->H + h) * dh;
    float *dqri = c->dqr + ((b * T + i) * c->H + h) * dr;
    for (size_t d = 0; d < dh; ++d) dqci[d] = 0.0f;
    for (size_t d = 0; d < dr; ++d) dqri[d] = 0.0f;
    for (size_t j = 0; j <= i; ++j) {
      double pv = (double)pi[j], ds = pv * (dp[j] - dot) * scale;
      const float *kcj = c->kc + ((b * T + j) * c->H + h) * dh;
      const float *krj = c->kr + (b * T + j) * dr;
      for (size_t d = 0; d < dh; ++d) {
        dqci[d] = (float)((double)dqci[d] + ds * (double)kcj[d]);
        dkc_l[j * dh + d] += ds * (double)qci[d];
        dvc_l[j * dh + d] += pv * (double)dyi[d];
      }
      for (size_t d = 0; d < dr; ++d) dqri[d] = (float)((double)dqri[d] + ds * (double)krj[d]);
    }
  }
  for (size_t j = 0; j < T; ++j) {
    float *dkcj = c->dkc + ((b * T + j) * c->H + h) * dh;
    float *dvcj = c->dvc + ((b * T + j) * c->H + h) * dh;
    for (size_t d = 0; d < dh; ++d) { dkcj[d] = (float)dkc_l[j * dh + d]; dvcj[d] = (float)dvc_l[j * dh + d]; }
  }
  free(dp); free(dkc_l); free(dvc_l);
}

static void mla_bwd_dkr_worker(void *vctx, size_t task) {
  mla_att_ctx *c = (mla_att_ctx *)vctx;
  size_t b = task, T = c->T, H = c->H, dh = c->dh, dr = c->dr;
  double scale = 1.0 / sqrt((double)(dh + dr));
  double *dp = (double *)malloc(T * sizeof(double));
  double *dkr_l = (double *)calloc(T * dr, sizeof(double));
  for (size_t h = 0; h < H; ++h) {
    for (size_t i = 0; i < T; ++i) {
      const float *qri = c->qr + ((b * T + i) * H + h) * dr;
      const float *dyi = c->dy + ((b * T + i) * H + h) * dh;
      const float *pi = c->p + ((b * H + h) * T + i) * T;
      double dot = 0.0;
      for (size_t j = 0; j <= i; ++j) {
        const float *vcj = c->vc + ((b * T + j) * H + h) * dh;
        double s = 0.0;
        for (size_t d = 0; d < dh; ++d) s += (double)dyi[d] * (double)vcj[d];
        dp[j] = s; dot += (double)pi[j] * s;
      }
      for (size_t j = 0; j <= i; ++j) {
        double ds = (double)pi[j] * (dp[j] - dot) * scale;
        for (size_t d = 0; d < dr; ++d) dkr_l[j * dr + d] += ds * (double)qri[d];
      }
    }
  }
  for (size_t j = 0; j < T; ++j)
    for (size_t d = 0; d < dr; ++d) c->dkr[(b * T + j) * dr + d] = (float)dkr_l[j * dr + d];
  free(dp); free(dkr_l);
}

/* ==================== Forward Pass ==================== */
static int ext_forward_internal(bcir_ext_model *m, const uint32_t *tokens, uint32_t active_T) {
  size_t B = m->sp.batch, T = active_T, N = B * T, d = m->sp.width, H = m->sp.heads, KH = m->sp.kvheads;
  size_t dh = d / H, kvd = KH * dh, ff = m->sp.ff, V = m->sp.vocab, dc = m->sp.mla_dc, dr = m->sp.mla_dr;
  const float *W = m->weights, *emb = W + m->off_embed;
  float *x = m->acts[0].x_in;
  for (size_t i = 0; i < N; ++i) {
    uint32_t tid = tokens[i];
    if (tid >= V) return 1;
    memcpy(x + i * d, emb + (size_t)tid * d, d * 4u);
  }
  int bad = 0;
  for (uint32_t l = 0; l < m->sp.layers; ++l) {
    const layer_offsets *lo = &m->loff[l];
    layer_act *a = &m->acts[l];
    float *x_out = (l + 1u < m->sp.layers) ? m->acts[l + 1u].x_in : m->x_final;
    ext_rms_fwd(m, N, d, a->x_in, W + lo->norm1, a->n1);
    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      ext_linear_fwd(m, 1, N, d, d, a->n1, W + lo->wq, a->q);
      ext_linear_fwd(m, 1, N, d, kvd, a->n1, W + lo->wk, a->k);
      ext_linear_fwd(m, 1, N, d, kvd, a->n1, W + lo->wv, a->v);
      if (lo->use_rope) {
        bcir_tensor_rope_lent_pool(m->pool, B, T, H, dh, m->rope_hd, a->q, 0);
        bcir_tensor_rope_lent_pool(m->pool, B, T, KH, dh, m->rope_hd, a->k, 0);
      }
      if (lo->mixer == BCIR_MIXER_GQA) {
        gqa_fwd_dispatch(m, B, T, a->q, a->k, a->v, a->p, a->ctx, &bad);
      } else {
        size_t nt = B * H;
        double *ml = (double *)calloc(nt, sizeof(double));
        uint64_t *pr = (uint64_t *)calloc(nt, sizeof(uint64_t));
        swa_fwd_ctx sc = { B, T, H, KH, dh, m->sp.swa_window, a->q, a->k, a->v, a->p, a->ctx,
                           m->pol.fp64_guard_mode, m->pol.softmax_logit_thresh, ml, pr };
        bcir_tensor_pool_run(m->pool, swa_fwd_worker, &sc, nt);
        for (size_t i = 0; i < nt; ++i) {
          if (ml[i] > m->tel.max_softmax_logit) m->tel.max_softmax_logit = ml[i];
          m->tel.fp64_softmax_promotions += pr[i];
        }
        free(ml); free(pr);
      }
      ext_linear_fwd(m, 1, N, d, d, a->ctx, W + lo->wo, a->x2);
    } else if (lo->mixer == BCIR_MIXER_DELTANET) {
      ext_linear_fwd(m, 1, N, d, d, a->n1, W + lo->wq, a->q_raw);
      ext_linear_fwd(m, 1, N, d, d, a->n1, W + lo->wk, a->k_raw);
      ext_linear_fwd(m, 1, N, d, d, a->n1, W + lo->wv, a->v);
      ext_linear_fwd(m, 1, N, d, H, a->n1, W + lo->walpha, a->a_pre);
      ext_linear_fwd(m, 1, N, d, H, a->n1, W + lo->wbeta, a->b_pre);
      const float *ba = W + lo->balpha, *bb = W + lo->bbeta;
      for (size_t i = 0; i < N; ++i) {
        for (size_t h = 0; h < H; ++h) {
          float ap = a->a_pre[i * H + h] + ba[h], bp = a->b_pre[i * H + h] + bb[h];
          a->a_pre[i * H + h] = ap; a->b_pre[i * H + h] = bp;
          a->alpha[i * H + h] = sigmoid_f(ap); a->beta[i * H + h] = sigmoid_f(bp);
        }
      }
      dn_l2norm_fwd(N, H, dh, a->q_raw, a->q, a->q_inv);
      dn_l2norm_fwd(N, H, dh, a->k_raw, a->k, a->k_inv);
      size_t C = m->sp.delta_chunk, nc = (T + C - 1u) / C, nt = B * H;
      double *mo = (double *)calloc(nt, sizeof(double));
      uint64_t *wc = (uint64_t *)calloc(nt, sizeof(uint64_t));
      dn_fwd_ctx dc_ctx = { B, T, H, dh, C, nc, a->q, a->k, a->v, a->alpha, a->beta,
                            a->dn_sprev, a->dn_W, a->ctx, mo, wc };
      bcir_tensor_pool_run(m->pool, dn_wy_fwd_worker, &dc_ctx, nt);
      for (size_t i = 0; i < nt; ++i) {
        if (mo[i] > m->tel.max_wy_offdiag) m->tel.max_wy_offdiag = mo[i];
        m->tel.fp64_wy_solves += wc[i];
      }
      free(mo); free(wc);
      ext_linear_fwd(m, 1, N, d, d, a->ctx, W + lo->wo, a->x2);
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      ext_linear_fwd(m, 1, N, d, d, a->n1, W + lo->wq_c, a->q_c);
      ext_linear_fwd(m, 1, N, d, H * dr, a->n1, W + lo->wq_r, a->q_r);
      ext_linear_fwd(m, 1, N, d, dc, a->n1, W + lo->w_dkv, a->c_raw);
      ext_rms_fwd(m, N, dc, a->c_raw, W + lo->norm_kv, a->c_hat);
      ext_linear_fwd(m, 1, N, dc, d, a->c_hat, W + lo->w_uk, a->k_c);
      ext_linear_fwd(m, 1, N, dc, d, a->c_hat, W + lo->w_uv, a->v_c);
      ext_linear_fwd(m, 1, N, d, dr, a->n1, W + lo->w_kr, a->k_r);
      if (lo->use_rope) {
        bcir_tensor_rope_lent_pool(m->pool, B, T, H, dr, m->rope_dr, a->q_r, 0);
        bcir_tensor_rope_lent_pool(m->pool, B, T, 1u, dr, m->rope_dr, a->k_r, 0);
      }
      size_t nt = B * H;
      double *ml = (double *)calloc(nt, sizeof(double));
      uint64_t *pr = (uint64_t *)calloc(nt, sizeof(uint64_t));
      mla_att_ctx mc = { B, T, H, dh, dr, a->q_c, a->q_r, a->k_c, a->v_c, a->k_r, NULL,
                         a->mla_p, a->ctx, NULL, NULL, NULL, NULL, NULL,
                         m->pol.fp64_guard_mode, m->pol.softmax_logit_thresh, ml, pr };
      bcir_tensor_pool_run(m->pool, mla_fwd_worker, &mc, nt);
      for (size_t i = 0; i < nt; ++i) {
        if (ml[i] > m->tel.max_softmax_logit) m->tel.max_softmax_logit = ml[i];
        m->tel.fp64_softmax_promotions += pr[i];
      }
      free(ml); free(pr);
      ext_linear_fwd(m, 1, N, d, d, a->ctx, W + lo->wo, a->x2);
    }
    bcir_tensor_add_pool(m->pool, N * d, a->x_in, a->x2);
    ext_rms_fwd(m, N, d, a->x2, W + lo->norm2, a->n2);
    ext_linear_fwd(m, 2, N, d, ff, a->n2, W + lo->wgate, a->gate);
    ext_linear_fwd(m, 2, N, d, ff, a->n2, W + lo->wup, a->up);
    bcir_tensor_silu_mul_pool(m->pool, N * ff, a->gate, a->up, a->hidden);
    ext_linear_fwd(m, 2, N, ff, d, a->hidden, W + lo->wdown, x_out);
    bcir_tensor_add_pool(m->pool, N * d, a->x2, x_out);
  }
  ext_rms_fwd(m, N, d, m->x_final, W + m->off_final_norm, m->norm_final);
  ext_linear_fwd(m, 0, N, d, V, m->norm_final, W + m->off_lm_head, m->logits);
  if (bad) { m->tel.nonfinite_events++; return 3; }
  return 0;
}

int bcir_ext_forward(bcir_ext_model *m, const uint32_t *tokens, uint32_t active_time, float *out_logits) {
  if (!m || !tokens || active_time == 0u || active_time > m->sp.time) return 1;
  int rc = ext_forward_internal(m, tokens, active_time);
  if (rc != 0) return rc;
  if (out_logits) memcpy(out_logits, m->logits, (size_t)m->sp.batch * active_time * m->sp.vocab * 4u);
  return 0;
}

int bcir_ext_evaluate(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                      size_t n_batches, uint32_t active_time, bcir_ext_eval_result *out) {
  if (!m || !tokens || !targets || !out || active_time == 0u || active_time > m->sp.time) return 1;
  double t0 = now_ms(), loss_sum = 0.0;
  uint64_t tok_cnt = 0u, hits = 0u, nf = 0u;
  size_t N = (size_t)m->sp.batch * active_time, V = m->sp.vocab;
  for (size_t bi = 0; bi < n_batches; ++bi) {
    const uint32_t *tk = tokens + bi * N, *tg = targets + bi * N;
    if (ext_forward_internal(m, tk, active_time) != 0) nf++;
    for (size_t i = 0; i < N; ++i) {
      uint32_t y = tg[i];
      if (y == BCIR_EXT_IGNORE) continue;
      if (y >= V) return 1;
      const float *row = m->logits + i * V;
      double mx = -1e30; uint32_t best = 0u;
      for (size_t v = 0; v < V; ++v) {
        double lv = (double)row[v];
        if (!isfinite(lv)) { nf++; break; }
        if (lv > mx) { mx = lv; best = (uint32_t)v; }
      }
      double se = 0.0;
      for (size_t v = 0; v < V; ++v) se += exp((double)row[v] - mx);
      double ce = (mx + log(se)) - (double)row[y];
      if (!isfinite(ce)) nf++; else loss_sum += ce;
      if (best == y) hits++;
      tok_cnt++;
    }
  }
  out->tokens = tok_cnt; out->nonfinite = nf;
  out->ce = (tok_cnt > 0u) ? (loss_sum / (double)tok_cnt) : 0.0;
  out->accuracy = (tok_cnt > 0u) ? ((double)hits / (double)tok_cnt) : 0.0;
  out->eval_ms = now_ms() - t0;
  return (nf > 0u) ? 3 : 0;
}

/* ==================== Deterministic Embedding Scatter ==================== */
typedef struct emb_scat_ctx {
  size_t N, d, col_chunk;
  const uint32_t *tokens;
  const float *dx;
  float *demb;
} emb_scat_ctx;

static void emb_scat_worker(void *vctx, size_t task) {
  emb_scat_ctx *c = (emb_scat_ctx *)vctx;
  size_t c0 = task * c->col_chunk, c1 = (c0 + c->col_chunk <= c->d) ? (c0 + c->col_chunk) : c->d;
  for (size_t i = 0; i < c->N; ++i) {
    float *dst = c->demb + (size_t)c->tokens[i] * c->d;
    const float *src = c->dx + i * c->d;
    for (size_t j = c0; j < c1; ++j) dst[j] += src[j];
  }
}

static float *ext_grad_target(bcir_ext_model *m, size_t off, size_t count) {
  if (m->opt->grad_fp32) return m->opt->grad_fp32 + off;
  memset(m->grad_tile, 0, count * sizeof(float));
  return m->grad_tile;
}

static void ext_grad_commit(bcir_ext_model *m, size_t off, size_t count, int accumulate) {
  if (!m->opt->grad_fp32) {
    bcir_opt_commit_grad_span_fp32(m->opt, off, count, m->grad_tile, accumulate);
  }
}

static void ext_linear_bwd_grad(bcir_ext_model *m, int region, size_t rows, size_t in, size_t out,
                                const float *x, const float *w, const float *dy,
                                float *dx, size_t w_off, float dx_beta, int accumulate) {
  size_t cnt = out * in;
  float *dw = ext_grad_target(m, w_off, cnt);
  ext_linear_bwd(m, region, rows, in, out, x, w, dy, dx, dw, dx_beta, m->opt->grad_fp32 ? 1.0f : 0.0f);
  ext_grad_commit(m, w_off, cnt, accumulate);
}

static void ext_rms_bwd_grad(bcir_ext_model *m, size_t rows, size_t width, const float *x,
                             const float *g, const float *dy, float *dx, size_t g_off,
                             int dx_add, int accumulate) {
  float *dg = ext_grad_target(m, g_off, width);
  ext_rms_bwd(m, rows, width, x, g, dy, dx, dg, dx_add);
  ext_grad_commit(m, g_off, width, accumulate);
}

/* ==================== Loss + Full Backward Pass ==================== */
int bcir_ext_loss_backward(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                           uint32_t active_time, int accumulate,
                           double *out_loss, uint64_t *out_sup_tokens) {
  if (!m || !tokens || !targets || active_time == 0u || active_time > m->sp.time) return 1;
  size_t B = m->sp.batch, T = active_time, N = B * T, d = m->sp.width, H = m->sp.heads, KH = m->sp.kvheads;
  size_t dh = d / H, kvd = KH * dh, ff = m->sp.ff, V = m->sp.vocab, dc = m->sp.mla_dc, dr = m->sp.mla_dr;

  uint64_t sup = 0u;
  for (size_t i = 0; i < N; ++i) {
    if (targets[i] != BCIR_EXT_IGNORE) {
      if (targets[i] >= V) return 1;
      sup++;
    }
  }
  if (out_sup_tokens) *out_sup_tokens = sup;
  if (sup == 0u) return 1; /* TRAIN-OPT-2: refuse all-masked batch cleanly */

  int frc = ext_forward_internal(m, tokens, active_time);
  if (frc != 0) return frc;

  if (!accumulate) bcir_opt_zero_grad(m->opt);

  /* Cross-entropy loss & d_logits in FP64 */
  double loss_sum = 0.0, inv_sup = 1.0 / (double)sup;
  for (size_t i = 0; i < N; ++i) {
    uint32_t y = targets[i];
    float *dl = m->d_logits + i * V;
    if (y == BCIR_EXT_IGNORE) { memset(dl, 0, V * 4u); continue; }
    const float *row = m->logits + i * V;
    double mx = -1e30;
    for (size_t v = 0; v < V; ++v) {
      double lv = (double)row[v];
      if (!isfinite(lv)) { m->tel.nonfinite_events++; return 3; }
      if (lv > mx) mx = lv;
    }
    double se = 0.0;
    for (size_t v = 0; v < V; ++v) se += exp((double)row[v] - mx);
    double lse = mx + log(se);
    loss_sum += lse - (double)row[y];
    double inv_se = 1.0 / se;
    for (size_t v = 0; v < V; ++v) {
      double p = exp((double)row[v] - mx) * inv_se;
      dl[v] = (float)((p - (v == y ? 1.0 : 0.0)) * inv_sup);
    }
  }
  if (out_loss) *out_loss = loss_sum * inv_sup;

  const float *W = m->weights;
  /* Head & final norm backward */
  ext_linear_bwd_grad(m, 0, N, d, V, m->norm_final, W + m->off_lm_head, m->d_logits,
                      m->d_n, m->off_lm_head, 0.0f, accumulate);
  ext_rms_bwd_grad(m, N, d, m->x_final, W + m->off_final_norm, m->d_n, m->d_cur,
                   m->off_final_norm, 0, accumulate);

  for (uint32_t l = m->sp.layers; l-- > 0u; ) {
    const layer_offsets *lo = &m->loff[l];
    layer_act *a = &m->acts[l];
    /* Residual copy: d_x2 starts as d_cur (saved in d_next) */
    memcpy(m->d_next, m->d_cur, N * d * 4u);
    /* MLP backward */
    ext_linear_bwd_grad(m, 2, N, ff, d, a->hidden, W + lo->wdown, m->d_cur, m->d_hid, lo->wdown, 0.0f, accumulate);
    bcir_tensor_swiglu_backward_pool(m->pool, N * ff, a->gate, a->up, m->d_hid, m->d_gate, m->d_up);
    ext_linear_bwd_grad(m, 2, N, d, ff, a->n2, W + lo->wgate, m->d_gate, m->d_n, lo->wgate, 0.0f, accumulate);
    ext_linear_bwd_grad(m, 2, N, d, ff, a->n2, W + lo->wup, m->d_up, m->d_n, lo->wup, 1.0f, accumulate);
    ext_rms_bwd_grad(m, N, d, a->x2, W + lo->norm2, m->d_n, m->d_next, lo->norm2, 1, accumulate);

    /* Mixer residual split: d_cur = d_next (skip path), plus norm1 backward into d_cur */
    memcpy(m->d_cur, m->d_next, N * d * 4u);
    ext_linear_bwd_grad(m, 1, N, d, d, a->ctx, W + lo->wo, m->d_next, m->d_ctx, lo->wo, 0.0f, accumulate);

    if (lo->mixer == BCIR_MIXER_GQA || lo->mixer == BCIR_MIXER_SWA) {
      if (lo->mixer == BCIR_MIXER_GQA) {
        gqa_bwd_dispatch(m, B, T, a->q, a->k, a->v, a->p, m->d_ctx, m->d_q, m->d_k, m->d_v);
      } else {
        swa_bwd_ctx sc = { B, T, H, KH, dh, m->sp.swa_window, a->q, a->k, a->v, a->p, m->d_ctx, m->d_q, m->d_k, m->d_v };
        bcir_tensor_pool_run(m->pool, swa_bwd_dq_worker, &sc, B * H);
        bcir_tensor_pool_run(m->pool, swa_bwd_dkv_worker, &sc, B * KH);
      }
      if (lo->use_rope) {
        bcir_tensor_rope_lent_pool(m->pool, B, T, H, dh, m->rope_hd, m->d_q, 1);
        bcir_tensor_rope_lent_pool(m->pool, B, T, KH, dh, m->rope_hd, m->d_k, 1);
      }
      ext_linear_bwd_grad(m, 1, N, d, d, a->n1, W + lo->wq, m->d_q, m->d_n, lo->wq, 0.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, kvd, a->n1, W + lo->wk, m->d_k, m->d_n, lo->wk, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, kvd, a->n1, W + lo->wv, m->d_v, m->d_n, lo->wv, 1.0f, accumulate);
    } else if (lo->mixer == BCIR_MIXER_DELTANET) {
      size_t C = m->sp.delta_chunk, nc = (T + C - 1u) / C;
      dn_bwd_ctx db = { B, T, H, dh, C, nc, a->q, a->k, a->v, a->alpha, a->beta, m->d_ctx,
                        a->dn_sprev, a->dn_W, m->d_q, m->d_k, m->d_v, m->d_a, m->d_b };
      bcir_tensor_pool_run(m->pool, dn_wy_bwd_worker, &db, B * H);
      dn_l2norm_bwd(N, H, dh, a->q, a->q_inv, m->d_q, m->d_q);
      dn_l2norm_bwd(N, H, dh, a->k, a->k_inv, m->d_k, m->d_k);
      {
        float *gba = ext_grad_target(m, lo->balpha, H);
        for (size_t i = 0; i < N; ++i)
          for (size_t h = 0; h < H; ++h) gba[h] += m->d_a[i * H + h];
        ext_grad_commit(m, lo->balpha, H, accumulate);
      }
      {
        float *gbb = ext_grad_target(m, lo->bbeta, H);
        for (size_t i = 0; i < N; ++i)
          for (size_t h = 0; h < H; ++h) gbb[h] += m->d_b[i * H + h];
        ext_grad_commit(m, lo->bbeta, H, accumulate);
      }
      ext_linear_bwd_grad(m, 1, N, d, d, a->n1, W + lo->wq, m->d_q, m->d_n, lo->wq, 0.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, d, a->n1, W + lo->wk, m->d_k, m->d_n, lo->wk, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, d, a->n1, W + lo->wv, m->d_v, m->d_n, lo->wv, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, H, a->n1, W + lo->walpha, m->d_a, m->d_n, lo->walpha, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, H, a->n1, W + lo->wbeta, m->d_b, m->d_n, lo->wbeta, 1.0f, accumulate);
    } else if (lo->mixer == BCIR_MIXER_MLA) {
      mla_att_ctx mc = { B, T, H, dh, dr, a->q_c, a->q_r, a->k_c, a->v_c, a->k_r, m->d_ctx,
                         a->mla_p, NULL, m->d_qc, m->d_qr, m->d_kc, m->d_vc, m->d_kr, 0, 0.0, NULL, NULL };
      bcir_tensor_pool_run(m->pool, mla_bwd_bh_worker, &mc, B * H);
      bcir_tensor_pool_run(m->pool, mla_bwd_dkr_worker, &mc, B);
      if (lo->use_rope) {
        bcir_tensor_rope_lent_pool(m->pool, B, T, H, dr, m->rope_dr, m->d_qr, 1);
        bcir_tensor_rope_lent_pool(m->pool, B, T, 1u, dr, m->rope_dr, m->d_kr, 1);
      }
      ext_linear_bwd_grad(m, 1, N, dc, d, a->c_hat, W + lo->w_uk, m->d_kc, m->d_chat, lo->w_uk, 0.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, dc, d, a->c_hat, W + lo->w_uv, m->d_vc, m->d_chat, lo->w_uv, 1.0f, accumulate);
      ext_rms_bwd_grad(m, N, dc, a->c_raw, W + lo->norm_kv, m->d_chat, m->d_craw, lo->norm_kv, 0, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, d, a->n1, W + lo->wq_c, m->d_qc, m->d_n, lo->wq_c, 0.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, H * dr, a->n1, W + lo->wq_r, m->d_qr, m->d_n, lo->wq_r, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, dc, a->n1, W + lo->w_dkv, m->d_craw, m->d_n, lo->w_dkv, 1.0f, accumulate);
      ext_linear_bwd_grad(m, 1, N, d, dr, a->n1, W + lo->w_kr, m->d_kr, m->d_n, lo->w_kr, 1.0f, accumulate);
    }
    ext_rms_bwd_grad(m, N, d, a->x_in, W + lo->norm1, m->d_n, m->d_cur, lo->norm1, 1, accumulate);
  }
  /* Deterministic embedding scatter */
  {
    size_t cc = 32u, nt = (d + cc - 1u) / cc;
    float *gemb = ext_grad_target(m, m->off_embed, V * d);
    emb_scat_ctx ec = { N, d, cc, tokens, m->d_cur, gemb };
    bcir_tensor_pool_run(m->pool, emb_scat_worker, &ec, nt);
    ext_grad_commit(m, m->off_embed, V * d, accumulate || m->sp.tied);
  }
  return 0;
}

int bcir_ext_train_loop(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                        const uint32_t *active_times_or_null, const double *lrs,
                        size_t steps, const bcir_adamw_params *opt_cfg,
                        bcir_ext_step_metrics *events) {
  if (!m || !tokens || !targets || !opt_cfg) return 1;
  size_t stride = (size_t)m->sp.batch * m->sp.time;
  for (size_t s = 0; s < steps; ++s) {
    double t0 = now_ms();
    uint32_t act_T = active_times_or_null ? active_times_or_null[s] : m->sp.time;
    const uint32_t *tk = tokens + s * stride, *tg = targets + s * stride;
    double loss = 0.0, gnorm = 0.0; uint64_t sup = 0u; int clipped = 0;
    int rc = bcir_ext_loss_backward(m, tk, tg, act_T, 0, &loss, &sup);
    if (rc == 0) {
      bcir_adamw_params cur = *opt_cfg;
      if (lrs) cur.lr = lrs[s];
      rc = bcir_opt_step(m->pool, m->opt, m->weights, NULL, &cur, 1.0, &gnorm, &clipped, &m->tel);
    }
    if (events) {
      events[s].loss = loss; events[s].grad_norm = gnorm; events[s].step_ms = now_ms() - t0;
      events[s].clipped = clipped; events[s].status = rc; events[s].step = m->opt->step;
      events[s].supervised_tokens = sup;
      events[s].fp64_promotions = m->tel.fp64_norm_promotions + m->tel.fp64_softmax_promotions + m->tel.fp64_wy_solves;
      events[s].nonfinite_events = m->tel.nonfinite_events;
    }
    if (rc != 0) return rc;
  }
  return 0;
}

/* ==================== Gate 0 Mathematical Identity Verifiers ==================== */
int bcir_deltanet_verify_wy_vs_recurrent(uint32_t B, uint32_t T, uint32_t H, uint32_t dh,
                                         uint32_t C, double *out_max_abs_diff) {
  size_t N = (size_t)B * T, nc = (T + C - 1u) / C;
  float *q = (float *)malloc(N * H * dh * 4u), *k = (float *)malloc(N * H * dh * 4u);
  float *v = (float *)malloc(N * H * dh * 4u), *al = (float *)malloc(N * H * 4u), *be = (float *)malloc(N * H * 4u);
  float *y_wy = (float *)calloc(N * H * dh, 4u);
  double *sprev = (double *)calloc((size_t)B * H * nc * dh * dh, sizeof(double));
  double *Wall = (double *)calloc((size_t)B * H * T * dh, sizeof(double));
  uint64_t r = 123456789ULL;
  for (size_t i = 0; i < N * H * dh; ++i) {
    r = r * 6364136223846793005ULL + 1ULL; q[i] = ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f);
    r = r * 6364136223846793005ULL + 1ULL; k[i] = ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f);
    r = r * 6364136223846793005ULL + 1ULL; v[i] = ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f);
  }
  float *qi = (float *)malloc(N * H * 4u), *ki = (float *)malloc(N * H * 4u);
  dn_l2norm_fwd(N, H, dh, q, q, qi); dn_l2norm_fwd(N, H, dh, k, k, ki);
  for (size_t i = 0; i < N * H; ++i) {
    r = r * 6364136223846793005ULL + 1ULL; al[i] = 0.7f + 0.28f * ((float)(r >> 40) / (float)(1 << 24));
    r = r * 6364136223846793005ULL + 1ULL; be[i] = 0.05f + 0.85f * ((float)(r >> 40) / (float)(1 << 24));
  }
  double *mo = (double *)calloc((size_t)B * H, sizeof(double));
  uint64_t *wc = (uint64_t *)calloc((size_t)B * H, sizeof(uint64_t));
  dn_fwd_ctx ctx = { B, T, H, dh, C, nc, q, k, v, al, be, sprev, Wall, y_wy, mo, wc };
  for (size_t t = 0; t < (size_t)B * H; ++t) dn_wy_fwd_worker(&ctx, t);

  /* Reference exact token-by-token recurrence in FP64 */
  double max_diff = 0.0;
  double *S = (double *)calloc(dh * dh, sizeof(double)), *kS = (double *)malloc(dh * sizeof(double));
  for (size_t b = 0; b < B; ++b) {
    for (size_t h = 0; h < H; ++h) {
      memset(S, 0, dh * dh * sizeof(double));
      for (size_t t = 0; t < T; ++t) {
        const float *qt = q + ((b * T + t) * H + h) * dh;
        const float *kt = k + ((b * T + t) * H + h) * dh;
        const float *vt = v + ((b * T + t) * H + h) * dh;
        double a = (double)al[(b * T + t) * H + h], bt = (double)be[(b * T + t) * H + h];
        for (size_t d = 0; d < dh; ++d) {
          double acc = 0.0; for (size_t j = 0; j < dh; ++j) acc += (double)kt[j] * S[j * dh + d];
          kS[d] = acc;
        }
        for (size_t j = 0; j < dh; ++j) {
          for (size_t d = 0; d < dh; ++d)
            S[j * dh + d] = a * (S[j * dh + d] - bt * (double)kt[j] * kS[d]) + bt * (double)kt[j] * (double)vt[d];
        }
        const float *yt = y_wy + ((b * T + t) * H + h) * dh;
        for (size_t d = 0; d < dh; ++d) {
          double yr = 0.0; for (size_t j = 0; j < dh; ++j) yr += (double)qt[j] * S[j * dh + d];
          double diff = fabs((double)yt[d] - (double)(float)yr);
          if (diff > max_diff) max_diff = diff;
        }
      }
    }
  }
  free(q); free(k); free(v); free(al); free(be); free(y_wy); free(sprev); free(Wall);
  free(qi); free(ki); free(mo); free(wc); free(S); free(kS);
  if (out_max_abs_diff) *out_max_abs_diff = max_diff;
  return (max_diff < 1e-5) ? 0 : 1;
}

int bcir_mla_verify_absorption(uint32_t T, uint32_t H, uint32_t dh, uint32_t dc, uint32_t dr,
                               double *out_max_abs_diff) {
  size_t d = (size_t)H * dh;
  float *qc = (float *)malloc(T * d * 4u), *qr = (float *)malloc(T * H * dr * 4u);
  float *chat = (float *)malloc(T * dc * 4u), *kr = (float *)malloc(T * dr * 4u);
  float *wuk = (float *)malloc(d * dc * 4u), *wuv = (float *)malloc(d * dc * 4u);
  uint64_t r = 987654321ULL;
  size_t n1 = T * d, n2 = T * H * dr, n3 = T * dc, n4 = T * dr, n5 = d * dc;
  for (size_t i = 0; i < n1; ++i) { r = r * 6364136223846793005ULL + 1; qc[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
  for (size_t i = 0; i < n2; ++i) { r = r * 6364136223846793005ULL + 1; qr[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
  for (size_t i = 0; i < n3; ++i) { r = r * 6364136223846793005ULL + 1; chat[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
  for (size_t i = 0; i < n4; ++i) { r = r * 6364136223846793005ULL + 1; kr[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
  for (size_t i = 0; i < n5; ++i) { r = r * 6364136223846793005ULL + 1; wuk[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
  for (size_t i = 0; i < n5; ++i) { r = r * 6364136223846793005ULL + 1; wuv[i] = 0.2f * ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }

  float *kc = (float *)calloc(T * d, 4u), *vc = (float *)calloc(T * d, 4u);
  bcir_tensor_mm(NULL, 0, 1, T, d, dc, 1.0f, chat, wuk, 0.0f, kc);
  bcir_tensor_mm(NULL, 0, 1, T, d, dc, 1.0f, chat, wuv, 0.0f, vc);

  double scale = 1.0 / sqrt((double)(dh + dr)), max_diff = 0.0;
  double *s_u = (double *)malloc(T * sizeof(double)), *s_a = (double *)malloc(T * sizeof(double));
  double *q_abs = (double *)malloc(dc * sizeof(double)), *c_ctx = (double *)malloc(dc * sizeof(double));
  for (size_t h = 0; h < H; ++h) {
    const float *wuk_h = wuk + h * dh * dc, *wuv_h = wuv + h * dh * dc;
    for (size_t i = 0; i < T; ++i) {
      const float *qci = qc + (i * H + h) * dh, *qri = qr + (i * H + h) * dr;
      /* Absorbed q_abs = W_uk_h^T @ q_c_i [dc] */
      for (size_t c = 0; c < dc; ++c) {
        double acc = 0.0; for (size_t a = 0; a < dh; ++a) acc += (double)wuk_h[a * dc + c] * (double)qci[a];
        q_abs[c] = acc;
      }
      double mu = -1e30, ma = -1e30;
      for (size_t j = 0; j <= i; ++j) {
        const float *kcj = kc + (j * H + h) * dh, *krj = kr + j * dr, *chj = chat + j * dc;
        double su = 0.0, sa = 0.0, sr = 0.0;
        for (size_t a = 0; a < dh; ++a) su += (double)qci[a] * (double)kcj[a];
        for (size_t c = 0; c < dc; ++c) sa += q_abs[c] * (double)chj[c];
        for (size_t a = 0; a < dr; ++a) sr += (double)qri[a] * (double)krj[a];
        s_u[j] = (su + sr) * scale; s_a[j] = (sa + sr) * scale;
        if (s_u[j] > mu) mu = s_u[j]; if (s_a[j] > ma) ma = s_a[j];
      }
      double zu = 0.0, za = 0.0;
      for (size_t j = 0; j <= i; ++j) { s_u[j] = exp(s_u[j] - mu); zu += s_u[j]; s_a[j] = exp(s_a[j] - ma); za += s_a[j]; }
      memset(c_ctx, 0, dc * sizeof(double));
      for (size_t j = 0; j <= i; ++j) {
        double pa = s_a[j] / za; const float *chj = chat + j * dc;
        for (size_t c = 0; c < dc; ++c) c_ctx[c] += pa * (double)chj[c];
      }
      for (size_t a = 0; a < dh; ++a) {
        double yu = 0.0, ya = 0.0;
        for (size_t j = 0; j <= i; ++j) yu += (s_u[j] / zu) * (double)vc[(j * H + h) * dh + a];
        for (size_t c = 0; c < dc; ++c) ya += (double)wuv_h[a * dc + c] * c_ctx[c];
        double diff = fabs(yu - ya);
        if (diff > max_diff) max_diff = diff;
      }
    }
  }
  free(qc); free(qr); free(chat); free(kr); free(wuk); free(wuv); free(kc); free(vc);
  free(s_u); free(s_a); free(q_abs); free(c_ctx);
  if (out_max_abs_diff) *out_max_abs_diff = max_diff;
  return (max_diff < 1e-6) ? 0 : 1;
}
