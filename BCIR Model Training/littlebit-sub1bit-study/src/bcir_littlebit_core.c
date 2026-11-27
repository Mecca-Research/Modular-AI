#include "bcir_littlebit.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e3 + (double)ts.tv_nsec * 1e-6;
}

static float round_fp16_scalar(float x) {
  _Float16 h = (_Float16)x;
  return (float)h;
}

static void round_fp16_vec(const float *src, float *dst, size_t n) {
  for (size_t i = 0; i < n; i++) dst[i] = round_fp16_scalar(src[i]);
}

static inline float bin_fwd(float x, uint32_t surr, double k) {
  if (surr == LB_SURR_SMOOTH_FWD_TEST) return (float)tanh(k * (double)x);
  return x >= 0.0f ? 1.0f : -1.0f;
}

static inline float bin_bwd(float x, uint32_t surr, double k, size_t *active_cnt) {
  if (surr == LB_SURR_UNCLIPPED_STE) { (*active_cnt)++; return 1.0f; }
  if (surr == LB_SURR_CLIPPED_STE) {
    if (x >= -1.0f && x <= 1.0f) { (*active_cnt)++; return 1.0f; }
    return 0.0f;
  }
  double kx = k * (double)x;
  if (fabs(kx) < 3.0) (*active_cnt)++;
  if (fabs(kx) > 18.0) return 0.0f;
  double t = tanh(kx);
  return (float)(k * (1.0 - t * t));
}

typedef struct proj_spans {
  lb_proj_cfg cfg;
  size_t off[10];
  size_t cnt[10];
  uint32_t nsub;
} proj_spans;

typedef struct layer_cache {
  float *n1, *q, *k, *v, *p, *ctx, *x2, *n2, *gate, *up, *hidden;
} layer_cache;

struct lb_model {
  lb_model_cfg cfg;
  size_t n_rows; /* batch * time */
  size_t kv_dim; /* kvheads * head_dim */
  size_t head_dim;
  size_t n_projs; /* layers * 7 */
  proj_spans *projs;
  size_t off_embed, off_final_norm, off_lm_head;
  size_t *off_norm1, *off_norm2;
  size_t total_params;
  uint8_t *param_is_scale; /* 1 for h,g,l scales, 0 otherwise */
  uint8_t *param_is_nonlin; /* 1 for embed, norms, lm_head */
  uint8_t *param_is_dense_proj; /* 1 for LB_MODE_DENSE_FP32 projections */
  uint8_t *param_is_latent; /* 1 for binary-quantized U,V,W */
  int8_t *prev_signs;       /* sign tracking for flip-rate telemetry */
  size_t n_latent_params;

  float *weights, *grads, *m1, *m2;
  uint64_t step;
  double b1_pow, b2_pow;

  /* Residual stream xs[layers+1][n_rows*width], final norm, logits */
  float *xs;
  layer_cache *caches;
  float *act_buf;
  float *final_n;
  float *logits;

  /* Reusable scratch for backward & factorized projections */
  float *rope_tbl;
  float *dx, *dx2, *dn1, *dn2, *dq, *dk, *dv, *dctx, *dgate, *dup, *dhidden, *dlogits;
  float *rms_inv;
  /* Factorized linear scratch (sized to max across all projections) */
  size_t max_din, max_dout, max_r, max_wmat;
  float *lin_xg, *lin_z, *lin_zl, *lin_y0, *lin_dy0, *lin_dzl, *lin_dz, *lin_dxg;
  float *lin_ub, *lin_vb, *lin_dub, *lin_dvb;
  float *lin_h_eff, *lin_g_eff, *lin_l_eff;
  float *teacher_logits;
  float *teacher_hiddens;

  void *pool_mem;
  bcir_tensor_pool *pool;
  void *attn_ws;
  size_t attn_ws_bytes;
  size_t total_alloc_bytes;
};

static void *xcalloc(size_t n, size_t sz, size_t *acc) {
  size_t bytes = n * sz;
  if (bytes == 0) bytes = 64;
  size_t aligned = (bytes + 63u) & ~(size_t)63u;
  void *p = aligned_alloc(64, aligned);
  if (p) { memset(p, 0, aligned); *acc += aligned; }
  return p;
}

static void lb_gemm(lb_model *m, int ta, int tb, size_t M, size_t N, size_t K,
                    float alpha, const float *A, const float *B, float beta, float *C) {
  if (m->cfg.use_kblock)
    bcir_tensor_mm_kblock_pool(m->pool, ta, tb, M, N, K, alpha, A, B, beta, C);
  else
    bcir_tensor_mm_fused_pool(m->pool, ta, tb, M, N, K, alpha, A, B, beta, C);
}

static void add_span(size_t *cursor, proj_spans *ps, uint32_t idx, size_t len) {
  ps->off[idx] = *cursor;
  ps->cnt[idx] = len;
  *cursor += len;
}

lb_model *lb_model_create(const lb_model_cfg *cfg, const lb_proj_cfg *projs) {
  if (!cfg || !projs || !cfg->width || !cfg->heads || !cfg->kvheads || !cfg->layers) return NULL;
  lb_model *m = (lb_model *)calloc(1, sizeof(*m));
  if (!m) return NULL;
  m->cfg = *cfg;
  m->n_rows = (size_t)cfg->batch * cfg->time;
  m->head_dim = cfg->width / cfg->heads;
  m->kv_dim = (size_t)cfg->kvheads * m->head_dim;
  m->n_projs = (size_t)cfg->layers * LB_PROJ_COUNT;
  m->b1_pow = 1.0; m->b2_pow = 1.0;

  m->projs = (proj_spans *)calloc(m->n_projs, sizeof(proj_spans));
  m->off_norm1 = (size_t *)calloc(cfg->layers, sizeof(size_t));
  m->off_norm2 = (size_t *)calloc(cfg->layers, sizeof(size_t));

  size_t cur = 0;
  m->off_embed = cur; cur += (size_t)cfg->vocab * cfg->width;
  for (uint32_t l = 0; l < cfg->layers; l++) {
    m->off_norm1[l] = cur; cur += cfg->width;
    for (uint32_t p = 0; p < 4; p++) {
      proj_spans *ps = &m->projs[l * LB_PROJ_COUNT + p];
      ps->cfg = projs[l * LB_PROJ_COUNT + p];
      size_t di = ps->cfg.din, do_ = ps->cfg.dout, r1 = ps->cfg.r1, r2 = ps->cfg.r2;
      if (di > m->max_din) m->max_din = di;
      if (do_ > m->max_dout) m->max_dout = do_;
      if (r1 > m->max_r) m->max_r = r1;
      if (r2 > m->max_r) m->max_r = r2;
      if (di * do_ > m->max_wmat) m->max_wmat = di * do_;
      if (ps->cfg.mode == LB_MODE_DENSE_FP32) {
        add_span(&cur, ps, 0, do_ * di); ps->nsub = 1;
      } else if (ps->cfg.mode == LB_MODE_LOWRANK_FP16) {
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1); ps->nsub = 2;
      } else if (ps->cfg.mode == LB_MODE_ONEBIT_FULL) {
        add_span(&cur, ps, 0, do_ * di); add_span(&cur, ps, 1, do_); add_span(&cur, ps, 2, di); ps->nsub = 3;
      } else if (ps->cfg.mode == LB_MODE_LITTLEBIT_1P) {
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1);
        add_span(&cur, ps, 2, do_); add_span(&cur, ps, 3, di); add_span(&cur, ps, 4, r1); ps->nsub = 5;
      } else { /* LB_MODE_LITTLEBIT_RES */
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1);
        add_span(&cur, ps, 2, do_); add_span(&cur, ps, 3, di); add_span(&cur, ps, 4, r1);
        add_span(&cur, ps, 5, do_ * r2); add_span(&cur, ps, 6, di * r2);
        add_span(&cur, ps, 7, do_); add_span(&cur, ps, 8, di); add_span(&cur, ps, 9, r2); ps->nsub = 10;
      }
    }
    m->off_norm2[l] = cur; cur += cfg->width;
    for (uint32_t p = 4; p < 7; p++) {
      proj_spans *ps = &m->projs[l * LB_PROJ_COUNT + p];
      ps->cfg = projs[l * LB_PROJ_COUNT + p];
      size_t di = ps->cfg.din, do_ = ps->cfg.dout, r1 = ps->cfg.r1, r2 = ps->cfg.r2;
      if (di > m->max_din) m->max_din = di;
      if (do_ > m->max_dout) m->max_dout = do_;
      if (r1 > m->max_r) m->max_r = r1;
      if (r2 > m->max_r) m->max_r = r2;
      if (di * do_ > m->max_wmat) m->max_wmat = di * do_;
      if (ps->cfg.mode == LB_MODE_DENSE_FP32) {
        add_span(&cur, ps, 0, do_ * di); ps->nsub = 1;
      } else if (ps->cfg.mode == LB_MODE_LOWRANK_FP16) {
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1); ps->nsub = 2;
      } else if (ps->cfg.mode == LB_MODE_ONEBIT_FULL) {
        add_span(&cur, ps, 0, do_ * di); add_span(&cur, ps, 1, do_); add_span(&cur, ps, 2, di); ps->nsub = 3;
      } else if (ps->cfg.mode == LB_MODE_LITTLEBIT_1P) {
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1);
        add_span(&cur, ps, 2, do_); add_span(&cur, ps, 3, di); add_span(&cur, ps, 4, r1); ps->nsub = 5;
      } else {
        add_span(&cur, ps, 0, do_ * r1); add_span(&cur, ps, 1, di * r1);
        add_span(&cur, ps, 2, do_); add_span(&cur, ps, 3, di); add_span(&cur, ps, 4, r1);
        add_span(&cur, ps, 5, do_ * r2); add_span(&cur, ps, 6, di * r2);
        add_span(&cur, ps, 7, do_); add_span(&cur, ps, 8, di); add_span(&cur, ps, 9, r2); ps->nsub = 10;
      }
    }
  }
  m->off_final_norm = cur; cur += cfg->width;
  m->off_lm_head = cfg->tied ? m->off_embed : cur;
  if (!cfg->tied) cur += (size_t)cfg->vocab * cfg->width;
  m->total_params = cur;

  size_t *ab = &m->total_alloc_bytes;
  m->weights = (float *)xcalloc(cur, sizeof(float), ab);
  m->grads   = (float *)xcalloc(cur, sizeof(float), ab);
  m->m1      = (float *)xcalloc(cur, sizeof(float), ab);
  m->m2      = (float *)xcalloc(cur, sizeof(float), ab);
  m->param_is_scale  = (uint8_t *)calloc(cur, 1);
  m->param_is_nonlin = (uint8_t *)calloc(cur, 1);
  m->param_is_dense_proj = (uint8_t *)calloc(cur, 1);
  m->param_is_latent = (uint8_t *)calloc(cur, 1);
  m->prev_signs      = (int8_t  *)calloc(cur, 1);

  /* Tag parameter roles */
  memset(m->param_is_nonlin + m->off_embed, 1, (size_t)cfg->vocab * cfg->width);
  memset(m->param_is_nonlin + m->off_final_norm, 1, cfg->width);
  if (!cfg->tied) memset(m->param_is_nonlin + m->off_lm_head, 1, (size_t)cfg->vocab * cfg->width);
  for (uint32_t l = 0; l < cfg->layers; l++) {
    memset(m->param_is_nonlin + m->off_norm1[l], 1, cfg->width);
    memset(m->param_is_nonlin + m->off_norm2[l], 1, cfg->width);
    for (uint32_t p = 0; p < LB_PROJ_COUNT; p++) {
      proj_spans *ps = &m->projs[l * LB_PROJ_COUNT + p];
      if (ps->cfg.mode == LB_MODE_DENSE_FP32) {
        memset(m->param_is_dense_proj + ps->off[0], 1, ps->cnt[0]);
      } else if (ps->cfg.mode == LB_MODE_ONEBIT_FULL) {
        memset(m->param_is_latent + ps->off[0], 1, ps->cnt[0]); m->n_latent_params += ps->cnt[0];
        memset(m->param_is_scale  + ps->off[1], 1, ps->cnt[1]);
        memset(m->param_is_scale  + ps->off[2], 1, ps->cnt[2]);
      } else if (ps->cfg.mode == LB_MODE_LITTLEBIT_1P || ps->cfg.mode == LB_MODE_LITTLEBIT_RES) {
        memset(m->param_is_latent + ps->off[0], 1, ps->cnt[0]); m->n_latent_params += ps->cnt[0];
        memset(m->param_is_latent + ps->off[1], 1, ps->cnt[1]); m->n_latent_params += ps->cnt[1];
        memset(m->param_is_scale  + ps->off[2], 1, ps->cnt[2]);
        memset(m->param_is_scale  + ps->off[3], 1, ps->cnt[3]);
        memset(m->param_is_scale  + ps->off[4], 1, ps->cnt[4]);
        if (ps->cfg.mode == LB_MODE_LITTLEBIT_RES) {
          memset(m->param_is_latent + ps->off[5], 1, ps->cnt[5]); m->n_latent_params += ps->cnt[5];
          memset(m->param_is_latent + ps->off[6], 1, ps->cnt[6]); m->n_latent_params += ps->cnt[6];
          memset(m->param_is_scale  + ps->off[7], 1, ps->cnt[7]);
          memset(m->param_is_scale  + ps->off[8], 1, ps->cnt[8]);
          memset(m->param_is_scale  + ps->off[9], 1, ps->cnt[9]);
        }
      }
    }
  }

  /* Activations & caches */
  size_t nd = m->n_rows * cfg->width, nk = m->n_rows * m->kv_dim;
  size_t nf = m->n_rows * cfg->ff, nv = m->n_rows * cfg->vocab;
  size_t att = (size_t)cfg->batch * cfg->heads * cfg->time * cfg->time;
  size_t la = 5 * nd + 2 * nk + att + 3 * nf;
  m->xs = (float *)xcalloc((size_t)(cfg->layers + 1) * nd, sizeof(float), ab);
  m->act_buf = (float *)xcalloc((size_t)cfg->layers * la, sizeof(float), ab);
  m->caches = (layer_cache *)calloc(cfg->layers, sizeof(layer_cache));
  for (uint32_t l = 0; l < cfg->layers; l++) {
    float *c = m->act_buf + l * la;
    m->caches[l].n1 = c; c += nd;
    m->caches[l].q  = c; c += nd;
    m->caches[l].k  = c; c += nk;
    m->caches[l].v  = c; c += nk;
    m->caches[l].p  = c; c += att;
    m->caches[l].ctx = c; c += nd;
    m->caches[l].x2 = c; c += nd;
    m->caches[l].n2 = c; c += nd;
    m->caches[l].gate = c; c += nf;
    m->caches[l].up = c; c += nf;
    m->caches[l].hidden = c;
  }
  m->final_n = (float *)xcalloc(nd, sizeof(float), ab);
  m->logits  = (float *)xcalloc(nv, sizeof(float), ab);

  /* Scratch */
  m->rope_tbl = (float *)xcalloc((size_t)cfg->time * m->head_dim, sizeof(float), ab);
  m->dx = (float *)xcalloc(nd, sizeof(float), ab);
  m->dx2 = (float *)xcalloc(nd, sizeof(float), ab);
  m->dn1 = (float *)xcalloc(nd, sizeof(float), ab);
  m->dn2 = (float *)xcalloc(nd, sizeof(float), ab);
  m->dq = (float *)xcalloc(nd, sizeof(float), ab);
  m->dk = (float *)xcalloc(nk, sizeof(float), ab);
  m->dv = (float *)xcalloc(nk, sizeof(float), ab);
  m->dctx = (float *)xcalloc(nd, sizeof(float), ab);
  m->dgate = (float *)xcalloc(nf, sizeof(float), ab);
  m->dup = (float *)xcalloc(nf, sizeof(float), ab);
  m->dhidden = (float *)xcalloc(nf, sizeof(float), ab);
  m->dlogits = (float *)xcalloc(nv, sizeof(float), ab);
  m->rms_inv = (float *)xcalloc(m->n_rows, sizeof(float), ab);

  size_t mr = m->max_r ? m->max_r : 1;
  size_t md = m->max_din > m->max_dout ? m->max_din : m->max_dout;
  m->lin_xg  = (float *)xcalloc(m->n_rows * m->max_din, sizeof(float), ab);
  m->lin_dxg = (float *)xcalloc(m->n_rows * m->max_din, sizeof(float), ab);
  m->lin_z   = (float *)xcalloc(m->n_rows * mr, sizeof(float), ab);
  m->lin_zl  = (float *)xcalloc(m->n_rows * mr, sizeof(float), ab);
  m->lin_dz  = (float *)xcalloc(m->n_rows * mr, sizeof(float), ab);
  m->lin_dzl = (float *)xcalloc(m->n_rows * mr, sizeof(float), ab);
  m->lin_y0  = (float *)xcalloc(m->n_rows * m->max_dout, sizeof(float), ab);
  m->lin_dy0 = (float *)xcalloc(m->n_rows * m->max_dout, sizeof(float), ab);
  m->lin_ub  = (float *)xcalloc(m->max_wmat > m->max_dout * mr ? m->max_wmat : m->max_dout * mr, sizeof(float), ab);
  m->lin_vb  = (float *)xcalloc(m->max_din * mr, sizeof(float), ab);
  m->lin_dub = (float *)xcalloc(m->max_wmat > m->max_dout * mr ? m->max_wmat : m->max_dout * mr, sizeof(float), ab);
  m->lin_dvb = (float *)xcalloc(m->max_din * mr, sizeof(float), ab);
  m->lin_h_eff = (float *)xcalloc(md, sizeof(float), ab);
  m->lin_g_eff = (float *)xcalloc(md, sizeof(float), ab);
  m->lin_l_eff = (float *)xcalloc(mr, sizeof(float), ab);
  m->teacher_logits  = (float *)xcalloc(nv, sizeof(float), ab);
  m->teacher_hiddens = (float *)xcalloc((size_t)cfg->layers * nd, sizeof(float), ab);

  /* Thread pool */
  if (cfg->threads > 1) {
    size_t psz = bcir_tensor_pool_size(cfg->threads);
    if (psz > 0) {
      m->pool_mem = xcalloc(psz, 1, ab);
      m->pool = bcir_tensor_pool_init(m->pool_mem, psz, cfg->threads);
    }
  }
  m->attn_ws_bytes = bcir_tensor_attention_pool_workspace(cfg->batch, cfg->time, cfg->heads, cfg->kvheads, m->head_dim, cfg->threads ? cfg->threads : 1);
  if (m->attn_ws_bytes) m->attn_ws = xcalloc(m->attn_ws_bytes, 1, ab);

  bcir_tensor_rope_table_pool(m->pool, cfg->time, m->head_dim, cfg->rope_base, m->rope_tbl);
  return m;
}

void lb_model_destroy(lb_model *m) {
  if (!m) return;
  if (m->pool) bcir_tensor_pool_stop(m->pool);
  free(m->pool_mem); free(m->attn_ws);
  free(m->projs); free(m->off_norm1); free(m->off_norm2);
  free(m->param_is_scale); free(m->param_is_nonlin); free(m->param_is_dense_proj);
  free(m->param_is_latent); free(m->prev_signs);
  free(m->weights); free(m->grads); free(m->m1); free(m->m2);
  free(m->xs); free(m->act_buf); free(m->caches); free(m->final_n); free(m->logits);
  free(m->rope_tbl); free(m->dx); free(m->dx2); free(m->dn1); free(m->dn2);
  free(m->dq); free(m->dk); free(m->dv); free(m->dctx); free(m->dgate); free(m->dup);
  free(m->dhidden); free(m->dlogits); free(m->rms_inv);
  free(m->lin_xg); free(m->lin_dxg); free(m->lin_z); free(m->lin_zl); free(m->lin_dz); free(m->lin_dzl);
  free(m->lin_y0); free(m->lin_dy0); free(m->lin_ub); free(m->lin_vb); free(m->lin_dub); free(m->lin_dvb);
  free(m->lin_h_eff); free(m->lin_g_eff); free(m->lin_l_eff);
  free(m->teacher_logits); free(m->teacher_hiddens);
  free(m);
}

size_t lb_model_param_count(const lb_model *m) { return m ? m->total_params : 0; }
size_t lb_model_total_bytes(const lb_model *m) { return m ? m->total_alloc_bytes : 0; }
float *lb_model_weights_ptr(lb_model *m) { return m ? m->weights : NULL; }
float *lb_model_grads_ptr(lb_model *m) { return m ? m->grads : NULL; }

int lb_model_tensor_span(const lb_model *m, int kind, uint32_t layer, uint32_t proj,
                         uint32_t sub_id, size_t *offset, size_t *count) {
  if (!m || !offset || !count) return -1;
  if (kind == 0) { *offset = m->off_embed; *count = (size_t)m->cfg.vocab * m->cfg.width; return 0; }
  if (kind == 1) { *offset = m->off_final_norm; *count = m->cfg.width; return 0; }
  if (kind == 2) { *offset = m->off_lm_head; *count = (size_t)m->cfg.vocab * m->cfg.width; return 0; }
  if (layer >= m->cfg.layers) return -1;
  if (kind == 10) { *offset = m->off_norm1[layer]; *count = m->cfg.width; return 0; }
  if (kind == 11) { *offset = m->off_norm2[layer]; *count = m->cfg.width; return 0; }
  if (kind == 20 && proj < LB_PROJ_COUNT) {
    const proj_spans *ps = &m->projs[layer * LB_PROJ_COUNT + proj];
    if (sub_id >= ps->nsub) return -1;
    *offset = ps->off[sub_id]; *count = ps->cnt[sub_id]; return 0;
  }
  return -1;
}

void lb_model_reset_opt(lb_model *m) {
  if (!m) return;
  memset(m->m1, 0, m->total_params * sizeof(float));
  memset(m->m2, 0, m->total_params * sizeof(float));
  m->step = 0; m->b1_pow = 1.0; m->b2_pow = 1.0;
  for (size_t i = 0; i < m->total_params; i++)
    if (m->param_is_latent[i]) m->prev_signs[i] = m->weights[i] >= 0.0f ? 1 : -1;
}

/* ---------- LittleBit Path Forward & Backward ---------- */
static void load_scale(const float *src, float *dst, size_t n, int enabled, int fp16_round) {
  if (!enabled) { for (size_t i = 0; i < n; i++) dst[i] = 1.0f; }
  else if (fp16_round) round_fp16_vec(src, dst, n);
  else memcpy(dst, src, n * sizeof(float));
}

static void lb_path_forward(lb_model *m, size_t N, size_t din, size_t dout, size_t r,
                            uint32_t scale_flags, uint32_t fp16_round,
                            const float *X, const float *U, const float *V,
                            const float *h, const float *g, const float *l,
                            float *Y, int accumulate_y) {
  uint32_t surr = m->cfg.surrogate;
  double sk = m->cfg.smooth_k;
  load_scale(g, m->lin_g_eff, din, (scale_flags & LB_SCALE_COL) != 0, fp16_round);
  load_scale(l, m->lin_l_eff, r,   (scale_flags & LB_SCALE_LATENT) != 0, fp16_round);
  load_scale(h, m->lin_h_eff, dout,(scale_flags & LB_SCALE_ROW) != 0, fp16_round);

  /* X_g = X * g */
  for (size_t i = 0; i < N; i++) {
    const float *xr = X + i * din; float *xgr = m->lin_xg + i * din;
    for (size_t c = 0; c < din; c++) xgr[c] = xr[c] * m->lin_g_eff[c];
  }
  /* V_b = bin(V), U_b = bin(U) */
  for (size_t i = 0; i < din * r; i++) m->lin_vb[i] = bin_fwd(V[i], surr, sk);
  for (size_t i = 0; i < dout * r; i++) m->lin_ub[i] = bin_fwd(U[i], surr, sk);

  /* Z = X_g @ V_b [N x r] */
  lb_gemm(m, 0, 0, N, r, din, 1.0f, m->lin_xg, m->lin_vb, 0.0f, m->lin_z);
  /* Z_l = Z * l */
  for (size_t i = 0; i < N; i++) {
    const float *zr = m->lin_z + i * r; float *zlr = m->lin_zl + i * r;
    for (size_t k = 0; k < r; k++) zlr[k] = zr[k] * m->lin_l_eff[k];
  }
  /* Y0 = Z_l @ U_b^T [N x dout] */
  lb_gemm(m, 0, 1, N, dout, r, 1.0f, m->lin_zl, m->lin_ub, 0.0f, m->lin_y0);
  /* Y (+)= Y0 * h */
  if (accumulate_y) {
    for (size_t i = 0; i < N; i++) {
      const float *y0r = m->lin_y0 + i * dout; float *yr = Y + i * dout;
      for (size_t j = 0; j < dout; j++) yr[j] += y0r[j] * m->lin_h_eff[j];
    }
  } else {
    for (size_t i = 0; i < N; i++) {
      const float *y0r = m->lin_y0 + i * dout; float *yr = Y + i * dout;
      for (size_t j = 0; j < dout; j++) yr[j] = y0r[j] * m->lin_h_eff[j];
    }
  }
}

static void lb_path_backward(lb_model *m, size_t N, size_t din, size_t dout, size_t r,
                             uint32_t scale_flags, uint32_t fp16_round,
                             const float *X, const float *dY,
                             const float *U, const float *V,
                             const float *h, const float *g, const float *l,
                             float *dX, float *dU, float *dV,
                             float *dh, float *dg, float *dl,
                             size_t *active_cnt) {
  uint32_t surr = m->cfg.surrogate;
  double sk = m->cfg.smooth_k;
  int use_row = (scale_flags & LB_SCALE_ROW) != 0;
  int use_col = (scale_flags & LB_SCALE_COL) != 0;
  int use_lat = (scale_flags & LB_SCALE_LATENT) != 0;
  load_scale(g, m->lin_g_eff, din, use_col, fp16_round);
  load_scale(l, m->lin_l_eff, r,   use_lat, fp16_round);
  load_scale(h, m->lin_h_eff, dout,use_row, fp16_round);

  /* Recompute path intermediates (zero activation bloat for factorized ops!) */
  for (size_t i = 0; i < N; i++) {
    const float *xr = X + i * din; float *xgr = m->lin_xg + i * din;
    for (size_t c = 0; c < din; c++) xgr[c] = xr[c] * m->lin_g_eff[c];
  }
  for (size_t i = 0; i < din * r; i++) m->lin_vb[i] = bin_fwd(V[i], surr, sk);
  for (size_t i = 0; i < dout * r; i++) m->lin_ub[i] = bin_fwd(U[i], surr, sk);
  lb_gemm(m, 0, 0, N, r, din, 1.0f, m->lin_xg, m->lin_vb, 0.0f, m->lin_z);
  for (size_t i = 0; i < N; i++) {
    const float *zr = m->lin_z + i * r; float *zlr = m->lin_zl + i * r;
    for (size_t k = 0; k < r; k++) zlr[k] = zr[k] * m->lin_l_eff[k];
  }
  lb_gemm(m, 0, 1, N, dout, r, 1.0f, m->lin_zl, m->lin_ub, 0.0f, m->lin_y0);

  /* dh += sum_i dY[i,j] * Y0[i,j]; dY0 = dY * h */
  for (size_t j = 0; j < dout; j++) {
    double acc = 0.0;
    float hj = m->lin_h_eff[j];
    for (size_t i = 0; i < N; i++) {
      float dyv = dY[i * dout + j];
      acc += (double)dyv * (double)m->lin_y0[i * dout + j];
      m->lin_dy0[i * dout + j] = dyv * hj;
    }
    if (use_row) dh[j] += (float)acc;
  }

  /* dU_b = dY0^T @ Z_l [dout x r] */
  lb_gemm(m, 1, 0, dout, r, N, 1.0f, m->lin_dy0, m->lin_zl, 0.0f, m->lin_dub);
  for (size_t i = 0; i < dout * r; i++)
    dU[i] += m->lin_dub[i] * bin_bwd(U[i], surr, sk, active_cnt);

  /* dZ_l = dY0 @ U_b [N x r] */
  lb_gemm(m, 0, 0, N, r, dout, 1.0f, m->lin_dy0, m->lin_ub, 0.0f, m->lin_dzl);

  /* dl += sum_i dZ_l[i,k] * Z[i,k]; dZ = dZ_l * l */
  for (size_t k = 0; k < r; k++) {
    double acc = 0.0;
    float lk = m->lin_l_eff[k];
    for (size_t i = 0; i < N; i++) {
      float dzlv = m->lin_dzl[i * r + k];
      acc += (double)dzlv * (double)m->lin_z[i * r + k];
      m->lin_dz[i * r + k] = dzlv * lk;
    }
    if (use_lat) dl[k] += (float)acc;
  }

  /* dV_b = X_g^T @ dZ [din x r] */
  lb_gemm(m, 1, 0, din, r, N, 1.0f, m->lin_xg, m->lin_dz, 0.0f, m->lin_dvb);
  for (size_t i = 0; i < din * r; i++)
    dV[i] += m->lin_dvb[i] * bin_bwd(V[i], surr, sk, active_cnt);

  /* dX_g = dZ @ V_b^T [N x din] */
  lb_gemm(m, 0, 1, N, din, r, 1.0f, m->lin_dz, m->lin_vb, 0.0f, m->lin_dxg);

  /* dg += sum_i dX_g[i,c] * X[i,c]; dX += dX_g * g */
  for (size_t c = 0; c < din; c++) {
    double acc = 0.0;
    float gc = m->lin_g_eff[c];
    for (size_t i = 0; i < N; i++) {
      float dxgv = m->lin_dxg[i * din + c];
      acc += (double)dxgv * (double)X[i * din + c];
      dX[i * din + c] += dxgv * gc;
    }
    if (use_col) dg[c] += (float)acc;
  }
}

static void proj_forward(lb_model *m, uint32_t layer, uint32_t p, const float *X, float *Y) {
  const proj_spans *ps = &m->projs[layer * LB_PROJ_COUNT + p];
  size_t N = m->n_rows, din = ps->cfg.din, dout = ps->cfg.dout;
  const float *W = m->weights;
  if (ps->cfg.mode == LB_MODE_DENSE_FP32) {
    lb_gemm(m, 0, 1, N, dout, din, 1.0f, X, W + ps->off[0], 0.0f, Y);
  } else if (ps->cfg.mode == LB_MODE_LOWRANK_FP16) {
    size_t r = ps->cfg.r1;
    const float *U = W + ps->off[0], *V = W + ps->off[1];
    if (ps->cfg.fp16_round) {
      round_fp16_vec(U, m->lin_ub, dout * r);
      round_fp16_vec(V, m->lin_vb, din * r);
      U = m->lin_ub; V = m->lin_vb;
    }
    lb_gemm(m, 0, 0, N, r, din, 1.0f, X, V, 0.0f, m->lin_z);
    lb_gemm(m, 0, 1, N, dout, r, 1.0f, m->lin_z, U, 0.0f, Y);
  } else if (ps->cfg.mode == LB_MODE_ONEBIT_FULL) {
    const float *Wm = W + ps->off[0], *h = W + ps->off[1], *g = W + ps->off[2];
    load_scale(g, m->lin_g_eff, din,  (ps->cfg.scale_flags & LB_SCALE_COL) != 0, ps->cfg.fp16_round);
    load_scale(h, m->lin_h_eff, dout, (ps->cfg.scale_flags & LB_SCALE_ROW) != 0, ps->cfg.fp16_round);
    for (size_t i = 0; i < N; i++)
      for (size_t c = 0; c < din; c++) m->lin_xg[i * din + c] = X[i * din + c] * m->lin_g_eff[c];
    for (size_t i = 0; i < dout * din; i++) m->lin_ub[i] = bin_fwd(Wm[i], m->cfg.surrogate, m->cfg.smooth_k);
    lb_gemm(m, 0, 1, N, dout, din, 1.0f, m->lin_xg, m->lin_ub, 0.0f, m->lin_y0);
    for (size_t i = 0; i < N; i++)
      for (size_t j = 0; j < dout; j++) Y[i * dout + j] = m->lin_y0[i * dout + j] * m->lin_h_eff[j];
  } else if (ps->cfg.mode == LB_MODE_LITTLEBIT_1P) {
    lb_path_forward(m, N, din, dout, ps->cfg.r1, ps->cfg.scale_flags, ps->cfg.fp16_round,
                    X, W + ps->off[0], W + ps->off[1], W + ps->off[2], W + ps->off[3], W + ps->off[4], Y, 0);
  } else { /* LB_MODE_LITTLEBIT_RES */
    lb_path_forward(m, N, din, dout, ps->cfg.r1, ps->cfg.scale_flags, ps->cfg.fp16_round,
                    X, W + ps->off[0], W + ps->off[1], W + ps->off[2], W + ps->off[3], W + ps->off[4], Y, 0);
    lb_path_forward(m, N, din, dout, ps->cfg.r2, ps->cfg.scale_flags, ps->cfg.fp16_round,
                    X, W + ps->off[5], W + ps->off[6], W + ps->off[7], W + ps->off[8], W + ps->off[9], Y, 1);
  }
}

static void proj_backward(lb_model *m, uint32_t layer, uint32_t p, const float *X, const float *dY,
                          float *dX, size_t *active_cnt) {
  const proj_spans *ps = &m->projs[layer * LB_PROJ_COUNT + p];
  size_t N = m->n_rows, din = ps->cfg.din, dout = ps->cfg.dout;
  const float *W = m->weights;
  float *G = m->grads;
  if (ps->cfg.mode == LB_MODE_DENSE_FP32) {
    lb_gemm(m, 0, 0, N, din, dout, 1.0f, dY, W + ps->off[0], 1.0f, dX);
    lb_gemm(m, 1, 0, dout, din, N, 1.0f, dY, X, 1.0f, G + ps->off[0]);
  } else if (ps->cfg.mode == LB_MODE_LOWRANK_FP16) {
    size_t r = ps->cfg.r1;
    const float *U = W + ps->off[0], *V = W + ps->off[1];
    if (ps->cfg.fp16_round) {
      round_fp16_vec(U, m->lin_ub, dout * r);
      round_fp16_vec(V, m->lin_vb, din * r);
      U = m->lin_ub; V = m->lin_vb;
    }
    lb_gemm(m, 0, 0, N, r, din, 1.0f, X, V, 0.0f, m->lin_z);
    lb_gemm(m, 0, 0, N, r, dout, 1.0f, dY, U, 0.0f, m->lin_dz);
    lb_gemm(m, 1, 0, dout, r, N, 1.0f, dY, m->lin_z, 1.0f, G + ps->off[0]);
    lb_gemm(m, 0, 1, N, din, r, 1.0f, m->lin_dz, V, 1.0f, dX);
    lb_gemm(m, 1, 0, din, r, N, 1.0f, X, m->lin_dz, 1.0f, G + ps->off[1]);
  } else if (ps->cfg.mode == LB_MODE_ONEBIT_FULL) {
    const float *Wm = W + ps->off[0], *h = W + ps->off[1], *g = W + ps->off[2];
    float *dWm = G + ps->off[0], *dh = G + ps->off[1], *dg = G + ps->off[2];
    int use_row = (ps->cfg.scale_flags & LB_SCALE_ROW) != 0;
    int use_col = (ps->cfg.scale_flags & LB_SCALE_COL) != 0;
    load_scale(g, m->lin_g_eff, din,  use_col, ps->cfg.fp16_round);
    load_scale(h, m->lin_h_eff, dout, use_row, ps->cfg.fp16_round);
    for (size_t i = 0; i < N; i++)
      for (size_t c = 0; c < din; c++) m->lin_xg[i * din + c] = X[i * din + c] * m->lin_g_eff[c];
    for (size_t i = 0; i < dout * din; i++) m->lin_ub[i] = bin_fwd(Wm[i], m->cfg.surrogate, m->cfg.smooth_k);
    lb_gemm(m, 0, 1, N, dout, din, 1.0f, m->lin_xg, m->lin_ub, 0.0f, m->lin_y0);
    for (size_t j = 0; j < dout; j++) {
      double acc = 0.0; float hj = m->lin_h_eff[j];
      for (size_t i = 0; i < N; i++) {
        float dyv = dY[i * dout + j];
        acc += (double)dyv * (double)m->lin_y0[i * dout + j];
        m->lin_dy0[i * dout + j] = dyv * hj;
      }
      if (use_row) dh[j] += (float)acc;
    }
    lb_gemm(m, 1, 0, dout, din, N, 1.0f, m->lin_dy0, m->lin_xg, 0.0f, m->lin_dub);
    for (size_t i = 0; i < dout * din; i++)
      dWm[i] += m->lin_dub[i] * bin_bwd(Wm[i], m->cfg.surrogate, m->cfg.smooth_k, active_cnt);
    lb_gemm(m, 0, 0, N, din, dout, 1.0f, m->lin_dy0, m->lin_ub, 0.0f, m->lin_dxg);
    for (size_t c = 0; c < din; c++) {
      double acc = 0.0; float gc = m->lin_g_eff[c];
      for (size_t i = 0; i < N; i++) {
        float dxgv = m->lin_dxg[i * din + c];
        acc += (double)dxgv * (double)X[i * din + c];
        dX[i * din + c] += dxgv * gc;
      }
      if (use_col) dg[c] += (float)acc;
    }
  } else if (ps->cfg.mode == LB_MODE_LITTLEBIT_1P) {
    lb_path_backward(m, N, din, dout, ps->cfg.r1, ps->cfg.scale_flags, ps->cfg.fp16_round,
                     X, dY, W + ps->off[0], W + ps->off[1], W + ps->off[2], W + ps->off[3], W + ps->off[4],
                     dX, G + ps->off[0], G + ps->off[1], G + ps->off[2], G + ps->off[3], G + ps->off[4], active_cnt);
  } else {
    lb_path_backward(m, N, din, dout, ps->cfg.r1, ps->cfg.scale_flags, ps->cfg.fp16_round,
                     X, dY, W + ps->off[0], W + ps->off[1], W + ps->off[2], W + ps->off[3], W + ps->off[4],
                     dX, G + ps->off[0], G + ps->off[1], G + ps->off[2], G + ps->off[3], G + ps->off[4], active_cnt);
    lb_path_backward(m, N, din, dout, ps->cfg.r2, ps->cfg.scale_flags, ps->cfg.fp16_round,
                     X, dY, W + ps->off[5], W + ps->off[6], W + ps->off[7], W + ps->off[8], W + ps->off[9],
                     dX, G + ps->off[5], G + ps->off[6], G + ps->off[7], G + ps->off[8], G + ps->off[9], active_cnt);
  }
}

/* ---------- Full Decoder Forward & Backward ---------- */
static int run_forward(lb_model *m, const uint32_t *tokens, int scratch_attn) {
  const lb_model_cfg *c = &m->cfg;
  size_t N = m->n_rows, d = c->width, nd = N * d, nf = N * c->ff;
  const float *emb = m->weights + m->off_embed;
  for (size_t i = 0; i < N; i++) {
    if (tokens[i] >= c->vocab) return -1;
    memcpy(m->xs + i * d, emb + (size_t)tokens[i] * d, d * sizeof(float));
  }
  int bad = 0;
  unsigned attn_ct = BCIR_TENSOR_ATTENTION_FUSED | (scratch_attn ? BCIR_TENSOR_ATTENTION_SCRATCH : BCIR_TENSOR_ATTENTION_LOWER | BCIR_TENSOR_ATTENTION_BANDS);
  for (uint32_t l = 0; l < c->layers; l++) {
    float *x = m->xs + (size_t)l * nd;
    float *y = m->xs + (size_t)(l + 1) * nd;
    layer_cache *a = &m->caches[l];
    bcir_tensor_rms_scan_pool(m->pool, N, d, (float)c->rms_eps, x, m->weights + m->off_norm1[l], a->n1, &bad);
    proj_forward(m, l, LB_PROJ_Q, a->n1, a->q);
    proj_forward(m, l, LB_PROJ_K, a->n1, a->k);
    proj_forward(m, l, LB_PROJ_V, a->n1, a->v);
    bcir_tensor_rope_lent_pool(m->pool, c->batch, c->time, c->heads, m->head_dim, m->rope_tbl, a->q, 0);
    bcir_tensor_rope_lent_pool(m->pool, c->batch, c->time, c->kvheads, m->head_dim, m->rope_tbl, a->k, 0);
    bcir_tensor_attention_contract_variant_scan_ws_pool(m->pool, 0, attn_ct,
        c->batch, c->time, c->heads, c->kvheads, m->head_dim,
        a->q, a->k, a->v, a->p, a->ctx, &bad, m->attn_ws, m->attn_ws_bytes);
    proj_forward(m, l, LB_PROJ_O, a->ctx, a->x2);
    bcir_tensor_add_pool(m->pool, nd, x, a->x2);
    bcir_tensor_rms_scan_pool(m->pool, N, d, (float)c->rms_eps, a->x2, m->weights + m->off_norm2[l], a->n2, &bad);
    proj_forward(m, l, LB_PROJ_GATE, a->n2, a->gate);
    proj_forward(m, l, LB_PROJ_UP,   a->n2, a->up);
    bcir_tensor_silu_mul_scan_pool(m->pool, nf, a->gate, a->up, a->hidden, &bad);
    proj_forward(m, l, LB_PROJ_DOWN, a->hidden, y);
    bcir_tensor_add_pool(m->pool, nd, a->x2, y);
  }
  bcir_tensor_rms_scan_pool(m->pool, N, d, (float)c->rms_eps, m->xs + (size_t)c->layers * nd,
                            m->weights + m->off_final_norm, m->final_n, &bad);
  lb_gemm(m, 0, 1, N, c->vocab, d, 1.0f, m->final_n, m->weights + m->off_lm_head, 0.0f, m->logits);
  return bad ? -2 : 0;
}

int lb_model_forward(lb_model *m, const uint32_t *tokens, float *out_logits, float *out_hiddens) {
  if (!m || !tokens) return -1;
  int rc = run_forward(m, tokens, 1);
  if (rc) return rc;
  size_t nd = m->n_rows * m->cfg.width, nv = m->n_rows * m->cfg.vocab;
  if (out_logits) memcpy(out_logits, m->logits, nv * sizeof(float));
  if (out_hiddens) memcpy(out_hiddens, m->xs + nd, (size_t)m->cfg.layers * nd * sizeof(float));
  return 0;
}

int lb_model_loss_backward(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                           const uint32_t *targets, int accumulate, lb_step_metrics *out) {
  if (!m || !tokens || !targets) return -1;
  const lb_model_cfg *c = &m->cfg;
  size_t N = m->n_rows, d = c->width, V = c->vocab, nd = N * d, nf = N * c->ff;
  int need_teacher = (c->objective != LB_OBJ_CE);
  if (need_teacher && !teacher) return -1;

  if (need_teacher) {
    if (lb_model_forward(teacher, tokens, m->teacher_logits, m->teacher_hiddens) != 0) return -2;
  }
  if (run_forward(m, tokens, 0) != 0) return -2;
  if (!accumulate) memset(m->grads, 0, m->total_params * sizeof(float));

  /* Count supervised tokens */
  size_t sup = 0;
  for (size_t i = 0; i < N; i++) if (targets[i] != LB_IGNORE_TARGET) sup++;
  if (sup == 0) return -1;

  double ce_sum = 0.0, kl_sum = 0.0;
  double inv_sup = 1.0 / (double)sup;
  double *ps = (double *)malloc(V * sizeof(double));
  double *pt = (double *)malloc(V * sizeof(double));
  memset(m->dlogits, 0, N * V * sizeof(float));

  for (size_t i = 0; i < N; i++) {
    if (targets[i] == LB_IGNORE_TARGET) continue;
    uint32_t tgt = targets[i];
    if (tgt >= V) { free(ps); free(pt); return -1; }
    const float *zs = m->logits + i * V;
    double ms = zs[0];
    for (size_t v = 1; v < V; v++) if (zs[v] > ms) ms = zs[v];
    double sums = 0.0;
    for (size_t v = 0; v < V; v++) { ps[v] = exp((double)zs[v] - ms); sums += ps[v]; }
    double invs = 1.0 / sums;
    for (size_t v = 0; v < V; v++) ps[v] *= invs;
    ce_sum += (log(sums) + ms - (double)zs[tgt]);

    float *dl = m->dlogits + i * V;
    if (c->objective == LB_OBJ_CE) {
      for (size_t v = 0; v < V; v++) dl[v] = (float)((ps[v] - (v == tgt ? 1.0 : 0.0)) * inv_sup);
    } else {
      const float *zt = m->teacher_logits + i * V;
      double mt = zt[0];
      for (size_t v = 1; v < V; v++) if (zt[v] > mt) mt = zt[v];
      double sumt = 0.0;
      for (size_t v = 0; v < V; v++) { pt[v] = exp((double)zt[v] - mt); sumt += pt[v]; }
      double invt = 1.0 / sumt;
      for (size_t v = 0; v < V; v++) pt[v] *= invt;
      for (size_t v = 0; v < V; v++) {
        if (pt[v] > 1e-30) kl_sum += pt[v] * (log(pt[v]) - log(ps[v] > 1e-30 ? ps[v] : 1e-30));
      }
      if (c->objective == LB_OBJ_CE_PLUS_KD) {
        for (size_t v = 0; v < V; v++)
          dl[v] = (float)((0.5 * (ps[v] - (v == tgt ? 1.0 : 0.0)) + 0.5 * (ps[v] - pt[v])) * inv_sup);
      } else {
        for (size_t v = 0; v < V; v++) dl[v] = (float)((ps[v] - pt[v]) * inv_sup);
      }
    }
  }
  free(ps); free(pt);

  /* Head backward */
  lb_gemm(m, 0, 0, N, d, V, 1.0f, m->dlogits, m->weights + m->off_lm_head, 0.0f, m->dx2);
  lb_gemm(m, 1, 0, V, d, N, 1.0f, m->dlogits, m->final_n, 1.0f, m->grads + m->off_lm_head);
  bcir_tensor_rms_backward_into_pool(m->pool, N, d, (float)c->rms_eps,
      m->xs + (size_t)c->layers * nd, m->weights + m->off_final_norm, m->dx2,
      m->dx, m->grads + m->off_final_norm, m->rms_inv, 0u);

  /* Layers backward */
  double inter_mse = 0.0;
  int use_inter = (c->objective == LB_OBJ_KD_FULL || c->objective == LB_OBJ_CE_PLUS_KD) && c->kd_lambda > 0.0;
  double mse_denom = (double)c->layers * (double)nd;
  float mse_grad_scale = use_inter ? (float)(2.0 * c->kd_lambda / mse_denom) : 0.0f;
  size_t active_cnt = 0;
  unsigned abwd_ct = BCIR_TENSOR_ATTENTION_FUSED | BCIR_TENSOR_ATTENTION_BANDS;

  for (int l = (int)c->layers - 1; l >= 0; l--) {
    float *x = m->xs + (size_t)l * nd;
    float *y = m->xs + (size_t)(l + 1) * nd;
    layer_cache *a = &m->caches[l];

    if (use_inter) {
      const float *ht = m->teacher_hiddens + (size_t)l * nd;
      for (size_t i = 0; i < nd; i++) {
        double diff = (double)y[i] - (double)ht[i];
        inter_mse += diff * diff;
        m->dx[i] += mse_grad_scale * (float)diff;
      }
    }

    /* Residual split: d_x2 = dx, down_proj bwd into dhidden */
    memcpy(m->dx2, m->dx, nd * sizeof(float));
    memset(m->dhidden, 0, nf * sizeof(float));
    proj_backward(m, (uint32_t)l, LB_PROJ_DOWN, a->hidden, m->dx, m->dhidden, &active_cnt);
    bcir_tensor_swiglu_backward_pool(m->pool, nf, a->gate, a->up, m->dhidden, m->dgate, m->dup);
    memset(m->dn2, 0, nd * sizeof(float));
    proj_backward(m, (uint32_t)l, LB_PROJ_GATE, a->n2, m->dgate, m->dn2, &active_cnt);
    proj_backward(m, (uint32_t)l, LB_PROJ_UP,   a->n2, m->dup,   m->dn2, &active_cnt);
    bcir_tensor_rms_backward_into_pool(m->pool, N, d, (float)c->rms_eps,
        a->x2, m->weights + m->off_norm2[l], m->dn2, m->dx2, m->grads + m->off_norm2[l],
        m->rms_inv, BCIR_TENSOR_RMSB_ADD);

    /* Attention block backward */
    memcpy(m->dx, m->dx2, nd * sizeof(float));
    memset(m->dctx, 0, nd * sizeof(float));
    proj_backward(m, (uint32_t)l, LB_PROJ_O, a->ctx, m->dx2, m->dctx, &active_cnt);
    bcir_tensor_attention_backward_contract_variant_ws_pool(m->pool, 0, abwd_ct,
        c->batch, c->time, c->heads, c->kvheads, m->head_dim,
        a->q, a->k, a->v, a->p, m->dctx, m->dq, m->dk, m->dv, m->attn_ws, m->attn_ws_bytes);
    bcir_tensor_rope_lent_pool(m->pool, c->batch, c->time, c->heads,   m->head_dim, m->rope_tbl, m->dq, 1);
    bcir_tensor_rope_lent_pool(m->pool, c->batch, c->time, c->kvheads, m->head_dim, m->rope_tbl, m->dk, 1);
    memset(m->dn1, 0, nd * sizeof(float));
    proj_backward(m, (uint32_t)l, LB_PROJ_Q, a->n1, m->dq, m->dn1, &active_cnt);
    proj_backward(m, (uint32_t)l, LB_PROJ_K, a->n1, m->dk, m->dn1, &active_cnt);
    proj_backward(m, (uint32_t)l, LB_PROJ_V, a->n1, m->dv, m->dn1, &active_cnt);
    bcir_tensor_rms_backward_into_pool(m->pool, N, d, (float)c->rms_eps,
        x, m->weights + m->off_norm1[l], m->dn1, m->dx, m->grads + m->off_norm1[l],
        m->rms_inv, BCIR_TENSOR_RMSB_ADD);
  }

  /* Embedding scatter */
  float *demb = m->grads + m->off_embed;
  for (size_t i = 0; i < N; i++) {
    float *row = demb + (size_t)tokens[i] * d;
    const float *dxr = m->dx + i * d;
    for (size_t k = 0; k < d; k++) row[k] += dxr[k];
  }

  if (out) {
    double ce = ce_sum * inv_sup, kl = kl_sum * inv_sup, imse = use_inter ? (inter_mse / mse_denom) : 0.0;
    double tot = (c->objective == LB_OBJ_CE) ? ce :
                 (c->objective == LB_OBJ_KD_KL) ? kl :
                 (c->objective == LB_OBJ_KD_FULL) ? (kl + c->kd_lambda * imse) :
                 (0.5 * ce + 0.5 * kl + c->kd_lambda * imse);
    out->total_loss = tot;
    out->ce_loss = ce;
    out->kl_loss = kl;
    out->inter_mse = imse;
    out->active_grad_frac = m->n_latent_params ? ((double)active_cnt / (double)m->n_latent_params) : 1.0;
  }
  return 0;
}

int lb_model_train_step(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                        const uint32_t *targets, const lb_adamw_cfg *opt, lb_step_metrics *out) {
  if (!m || !opt) return -1;
  double t0 = now_ms();
  lb_step_metrics met;
  memset(&met, 0, sizeof(met));
  int rc = lb_model_loss_backward(m, teacher, tokens, targets, 0, &met);
  if (rc) return rc;

  /* Optionally zero non-linear grads (freeze embed & RMSNorms during QAT) and/or dense projections (e.g. Q8 Attention) */
  if (opt->freeze_non_linear || opt->freeze_dense_projs) {
    for (size_t i = 0; i < m->total_params; i++)
      if ((opt->freeze_non_linear && m->param_is_nonlin[i]) ||
          (opt->freeze_dense_projs && m->param_is_dense_proj[i]))
        m->grads[i] = 0.0f;
  }

  /* Global L2 grad norm & clip */
  double g2 = 0.0;
  for (size_t i = 0; i < m->total_params; i++) {
    double g = (double)m->grads[i];
    if (!isfinite(g)) return -3;
    g2 += g * g;
  }
  double gnorm = sqrt(g2);
  double clip = (opt->grad_clip > 0.0 && gnorm > opt->grad_clip) ? (opt->grad_clip / gnorm) : 1.0;

  m->step++;
  m->b1_pow *= opt->beta1;
  m->b2_pow *= opt->beta2;
  double bc1 = 1.0 - m->b1_pow, bc2 = 1.0 - m->b2_pow;
  double lr_base = opt->lr;
  double lr_sc = opt->lr_scales > 0.0 ? opt->lr_scales : opt->lr;

  size_t flips = 0;
  for (size_t i = 0; i < m->total_params; i++) {
    if (opt->freeze_non_linear && m->param_is_nonlin[i]) continue;
    if (opt->freeze_dense_projs && m->param_is_dense_proj[i]) continue;
    double g = (double)m->grads[i] * clip;
    double m1 = opt->beta1 * (double)m->m1[i] + (1.0 - opt->beta1) * g;
    double m2 = opt->beta2 * (double)m->m2[i] + (1.0 - opt->beta2) * g * g;
    m->m1[i] = (float)m1;
    m->m2[i] = (float)m2;
    double lr = m->param_is_scale[i] ? lr_sc : lr_base;
    double wd = (m->param_is_scale[i] || m->param_is_nonlin[i]) ? 0.0 : opt->weight_decay;
    double w = (double)m->weights[i];
    w -= lr * (wd * w + (m1 / bc1) / (sqrt(m2 / bc2) + opt->epsilon));
    m->weights[i] = (float)w;
    if (m->param_is_latent[i]) {
      int8_t snew = m->weights[i] >= 0.0f ? 1 : -1;
      if (snew != m->prev_signs[i]) { flips++; m->prev_signs[i] = snew; }
    }
  }
  met.grad_norm = gnorm;
  met.sign_flip_rate = m->n_latent_params ? ((double)flips / (double)m->n_latent_params) : 0.0;
  met.step_ms = now_ms() - t0;
  met.step = m->step;
  if (out) *out = met;
  return 0;
}

int lb_model_train_loop(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                        const uint32_t *targets, size_t steps, const double *lrs,
                        const lb_adamw_cfg *opt, lb_step_metrics *events) {
  if (!m || !tokens || !targets || !opt) return -1;
  lb_adamw_cfg cur = *opt;
  double scale_ratio = (opt->lr_scales > 0.0 && opt->lr > 0.0) ? (opt->lr_scales / opt->lr) : 0.0;
  for (size_t s = 0; s < steps; s++) {
    if (lrs) {
      cur.lr = lrs[s];
      cur.lr_scales = scale_ratio > 0.0 ? lrs[s] * scale_ratio : 0.0;
    }
    int rc = lb_model_train_step(m, teacher, tokens + s * m->n_rows, targets + s * m->n_rows,
                                 &cur, events ? &events[s] : NULL);
    if (rc) return rc;
  }
  return 0;
}
