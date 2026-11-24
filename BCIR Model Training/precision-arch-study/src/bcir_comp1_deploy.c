/*===- bcir_comp1_deploy.c - Whole-Model COMP-1 & Region-Policy Deploy -----===
 * Whole-model inference & streaming-decode benchmark supporting mixed region
 * formats (FP32, IEEE FP16, BCIRQ8 g32, and Packed-1b LUT4 LittleBit) across
 * GQA, SWA (ring buffer), Arch-1 DeltaNet (O(1) recurrent state), and
 * Arch-4 MLA (absorbed latent KV cache with decoupled RoPE).
 *===------------------------------------------------------------------------===*/
#include "bcir_comp1_deploy.h"
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if defined(__AVX512F__)
#include <immintrin.h>
#endif

static double dep_now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec * 1e-3;
}

static void *dep_calloc(size_t n, size_t sz) {
  size_t b = n * sz; if (b == 0u) b = 64u;
  size_t ab = (b + 63u) & ~(size_t)63u;
  void *p = NULL;
  if (posix_memalign(&p, 64u, ab) != 0) return NULL;
  memset(p, 0, ab);
  return p;
}

static inline uint16_t f32_to_f16_bits(float x) {
  uint32_t u; memcpy(&u, &x, 4u);
  uint32_t s = (u >> 16) & 0x8000u;
  int32_t e = (int32_t)((u >> 23) & 0xFFu) - 127 + 15;
  uint32_t m = u & 0x007FFFFFu;
  if (e <= 0) {
    if (e < -10) return (uint16_t)s;
    m |= 0x00800000u;
    uint32_t sh = (uint32_t)(14 - e);
    uint32_t half = 1u << (sh - 1u);
    return (uint16_t)(s | ((m + half) >> sh));
  }
  if (e >= 31) return (uint16_t)(s | 0x7C00u);
  uint32_t r = m + 0x00000FFFu + ((m >> 13) & 1u);
  if (r & 0x00800000u) { r = 0u; e++; if (e >= 31) return (uint16_t)(s | 0x7C00u); }
  return (uint16_t)(s | ((uint32_t)e << 10) | (r >> 13));
}

static inline float f16_bits_to_f32(uint16_t h) {
  uint32_t s = (uint32_t)(h & 0x8000u) << 16;
  uint32_t e = (h >> 10) & 0x1Fu, m = h & 0x03FFu, u;
  if (e == 0u) {
    if (m == 0u) u = s;
    else {
      int32_t ex = -14;
      while ((m & 0x0400u) == 0u) { m <<= 1; ex--; }
      m &= 0x03FFu;
      u = s | ((uint32_t)(ex + 127) << 23) | (m << 13);
    }
  } else if (e == 31u) u = s | 0x7F800000u | (m << 13);
  else u = s | ((e + 112u) << 23) | (m << 13);
  float f; memcpy(&f, &u, 4u); return f;
}

typedef struct dep_lb_path {
  uint32_t din, dout, r;
  uint32_t din_g4, r_g4;
  uint8_t *v_nib_p; /* [ceil(din/4) * ceil(r/2)] packed 2 nibbles/byte */
  uint8_t *u_nib_p; /* [ceil(r/4) * ceil(dout/2)] packed 2 nibbles/byte */
  float *g, *l, *h;
  size_t bytes;
} dep_lb_path;

typedef struct dep_linear {
  int fmt;
  int region;
  uint32_t rows, cols; /* dout, din */
  float *w_fp32;
  uint16_t *w_fp16;
  int8_t *w_q8;
  int8_t *w_q8_exp;
  dep_lb_path p1, p2;
  int has_p2;
  size_t artifact_bytes;
  double rel_frob_err;
  double max_q8_bound_viol;
} dep_linear;

static void dep_lb_path_free(dep_lb_path *p) {
  free(p->v_nib_p); free(p->u_nib_p); free(p->g); free(p->l); free(p->h);
  memset(p, 0, sizeof(*p));
}

static void dep_linear_free(dep_linear *lin) {
  free(lin->w_fp32); free(lin->w_fp16); free(lin->w_q8); free(lin->w_q8_exp);
  dep_lb_path_free(&lin->p1); dep_lb_path_free(&lin->p2);
  memset(lin, 0, sizeof(*lin));
}

static void dep_lb_path_pack(dep_lb_path *p, uint32_t din, uint32_t dout, uint32_t r,
                             const float *U, const float *V,
                             const float *h, const float *g, const float *l) {
  dep_lb_path_free(p);
  if (r == 0u) return;
  p->din = din; p->dout = dout; p->r = r;
  p->din_g4 = (din + 3u) / 4u;
  p->r_g4 = (r + 3u) / 4u;
  uint32_t r_p = (r + 1u) / 2u, dout_p = (dout + 1u) / 2u;
  p->v_nib_p = (uint8_t *)dep_calloc((size_t)p->din_g4 * r_p, 1u);
  p->u_nib_p = (uint8_t *)dep_calloc((size_t)p->r_g4 * dout_p, 1u);
  for (uint32_t grp = 0; grp < p->din_g4; ++grp) {
    for (uint32_t m = 0; m < r; ++m) {
      uint8_t nib = 0u;
      for (uint32_t b = 0; b < 4u; ++b) {
        uint32_t k = grp * 4u + b;
        if (k < din && V[(size_t)k * r + m] >= 0.0f) nib |= (uint8_t)(1u << b);
      }
      uint8_t *dst = &p->v_nib_p[(size_t)grp * r_p + (m >> 1)];
      if ((m & 1u) == 0u) *dst |= nib; else *dst |= (uint8_t)(nib << 4);
    }
  }
  for (uint32_t grp = 0; grp < p->r_g4; ++grp) {
    for (uint32_t o = 0; o < dout; ++o) {
      uint8_t nib = 0u;
      for (uint32_t b = 0; b < 4u; ++b) {
        uint32_t m = grp * 4u + b;
        if (m < r && U[(size_t)o * r + m] >= 0.0f) nib |= (uint8_t)(1u << b);
      }
      uint8_t *dst = &p->u_nib_p[(size_t)grp * dout_p + (o >> 1)];
      if ((o & 1u) == 0u) *dst |= nib; else *dst |= (uint8_t)(nib << 4);
    }
  }
  p->g = (float *)dep_calloc(din, 4u); p->l = (float *)dep_calloc(r, 4u); p->h = (float *)dep_calloc(dout, 4u);
  for (uint32_t i = 0; i < din; ++i) p->g[i] = f16_bits_to_f32(f32_to_f16_bits(g[i]));
  for (uint32_t i = 0; i < r; ++i) p->l[i] = f16_bits_to_f32(f32_to_f16_bits(l[i]));
  for (uint32_t i = 0; i < dout; ++i) p->h[i] = f16_bits_to_f32(f32_to_f16_bits(h[i]));
  /* Exact deployment bytes: 1-bit U & V + FP16 scales h, g, l */
  size_t bit_bytes = (((size_t)dout * r + 7u) / 8u) + (((size_t)din * r + 7u) / 8u);
  size_t scale_bytes = ((size_t)din + r + dout) * 2u;
  p->bytes = bit_bytes + scale_bytes;
}

static void lut4_packed_gemv_portable(const uint8_t *nib_p, uint32_t n_in, uint32_t n_out,
                                      const float *x, float *y, int add_to_y) {
  uint32_t g4 = (n_in + 3u) / 4u, op = (n_out + 1u) / 2u;
  if (!add_to_y) memset(y, 0, (size_t)n_out * 4u);
  float lut[16];
  for (uint32_t g = 0; g < g4; ++g) {
    float x0 = (g * 4u + 0u < n_in) ? x[g * 4u + 0u] : 0.0f;
    float x1 = (g * 4u + 1u < n_in) ? x[g * 4u + 1u] : 0.0f;
    float x2 = (g * 4u + 2u < n_in) ? x[g * 4u + 2u] : 0.0f;
    float x3 = (g * 4u + 3u < n_in) ? x[g * 4u + 3u] : 0.0f;
    for (uint32_t mask = 0; mask < 16u; ++mask) {
      float s0 = (mask & 1u) ? x0 : -x0;
      float s1 = (mask & 2u) ? x1 : -x1;
      float s2 = (mask & 4u) ? x2 : -x2;
      float s3 = (mask & 8u) ? x3 : -x3;
      lut[mask] = (s0 + s1) + (s2 + s3);
    }
    const uint8_t *row = nib_p + (size_t)g * op;
    for (uint32_t o = 0; o < n_out; ++o) {
      uint8_t b = row[o >> 1];
      uint8_t nib = ((o & 1u) == 0u) ? (b & 0xFu) : (b >> 4);
      y[o] += lut[nib];
    }
  }
}

#if defined(__AVX512F__)
static void lut4_packed_gemv_avx512(const uint8_t *nib_p, uint32_t n_in, uint32_t n_out,
                                    const float *x, float *y, int add_to_y) {
  uint32_t g4 = (n_in + 3u) / 4u, op = (n_out + 1u) / 2u;
  if (!add_to_y) memset(y, 0, (size_t)n_out * 4u);
  float lut[16];
  __m512i mask_lo = _mm512_set1_epi32(0x0F);
  for (uint32_t g = 0; g < g4; ++g) {
    float x0 = (g * 4u + 0u < n_in) ? x[g * 4u + 0u] : 0.0f;
    float x1 = (g * 4u + 1u < n_in) ? x[g * 4u + 1u] : 0.0f;
    float x2 = (g * 4u + 2u < n_in) ? x[g * 4u + 2u] : 0.0f;
    float x3 = (g * 4u + 3u < n_in) ? x[g * 4u + 3u] : 0.0f;
    for (uint32_t mask = 0; mask < 16u; ++mask) {
      float s0 = (mask & 1u) ? x0 : -x0;
      float s1 = (mask & 2u) ? x1 : -x1;
      float s2 = (mask & 4u) ? x2 : -x2;
      float s3 = (mask & 8u) ? x3 : -x3;
      lut[mask] = (s0 + s1) + (s2 + s3);
    }
    __m512 vlut = _mm512_loadu_ps(lut);
    const uint8_t *row = nib_p + (size_t)g * op;
    uint32_t o = 0u;
    for (; o + 32u <= n_out; o += 32u) {
      __m128i raw = _mm_loadu_si128((const __m128i *)(row + (o >> 1)));
      __m512i bytes = _mm512_cvtepu8_epi32(raw);
      __m512i lo = _mm512_and_si512(bytes, mask_lo);
      __m512i hi = _mm512_srli_epi32(bytes, 4);
      __m512 vlo = _mm512_permutexvar_ps(lo, vlut);
      __m512 vhi = _mm512_permutexvar_ps(hi, vlut);
      /* Interleave (lo0, hi0, lo1, hi1, ...) via unpacklo/hi + permute */
      __m512i idx_ev = _mm512_set_epi32(23,7,22,6,21,5,20,4,19,3,18,2,17,1,16,0);
      __m512i idx_od = _mm512_set_epi32(31,15,30,14,29,13,28,12,27,11,26,10,25,9,24,8);
      __m512 p0 = _mm512_permutex2var_ps(vlo, idx_ev, vhi);
      __m512 p1 = _mm512_permutex2var_ps(vlo, idx_od, vhi);
      _mm512_storeu_ps(y + o, _mm512_add_ps(_mm512_loadu_ps(y + o), p0));
      _mm512_storeu_ps(y + o + 16u, _mm512_add_ps(_mm512_loadu_ps(y + o + 16u), p1));
    }
    for (; o < n_out; ++o) {
      uint8_t b = row[o >> 1];
      uint8_t nib = ((o & 1u) == 0u) ? (b & 0xFu) : (b >> 4);
      y[o] += lut[nib];
    }
  }
}
#endif

static void dep_lb_path_eval(const dep_lb_path *p, const float *x, float *xg, float *z,
                             float *y_tmp, float *y, int use_portable, int add_to_y) {
  (void)use_portable;
  if (p->r == 0u) return;
  for (uint32_t i = 0; i < p->din; ++i) xg[i] = x[i] * p->g[i];
#if defined(__AVX512F__)
  if (!use_portable) {
    lut4_packed_gemv_avx512(p->v_nib_p, p->din, p->r, xg, z, 0);
    for (uint32_t m = 0; m < p->r; ++m) z[m] *= p->l[m];
    lut4_packed_gemv_avx512(p->u_nib_p, p->r, p->dout, z, y_tmp, 0);
  } else
#endif
  {
    lut4_packed_gemv_portable(p->v_nib_p, p->din, p->r, xg, z, 0);
    for (uint32_t m = 0; m < p->r; ++m) z[m] *= p->l[m];
    lut4_packed_gemv_portable(p->u_nib_p, p->r, p->dout, z, y_tmp, 0);
  }
  if (!add_to_y) {
    for (uint32_t o = 0; o < p->dout; ++o) y[o] = y_tmp[o] * p->h[o];
  } else {
    for (uint32_t o = 0; o < p->dout; ++o) y[o] += y_tmp[o] * p->h[o];
  }
}

static void dep_linear_gemv(const dep_linear *lin, const float *x, float *y,
                            float *s_xg, float *s_z, float *s_yt, int use_portable) {
  uint32_t R = lin->rows, C = lin->cols;
  if (lin->fmt == BCIR_DEPLOY_FP32) {
    for (uint32_t r = 0; r < R; ++r) {
      const float *wr = lin->w_fp32 + (size_t)r * C;
      float acc = 0.0f;
      for (uint32_t c = 0; c < C; ++c) acc += wr[c] * x[c];
      y[r] = acc;
    }
  } else if (lin->fmt == BCIR_DEPLOY_FP16) {
    for (uint32_t r = 0; r < R; ++r) {
      const uint16_t *wr = lin->w_fp16 + (size_t)r * C;
      float acc = 0.0f;
      for (uint32_t c = 0; c < C; ++c) acc += f16_bits_to_f32(wr[c]) * x[c];
      y[r] = acc;
    }
  } else if (lin->fmt == BCIR_DEPLOY_BCIRQ8_G32) {
    for (uint32_t r = 0; r < R; ++r) {
      size_t base = (size_t)r * C;
      float acc = 0.0f;
      for (uint32_t c = 0; c < C; c += 32u) {
        size_t idx = base + c;
        size_t g = idx >> 5;
        uint32_t end = (c + 32u <= C) ? (c + 32u) : C;
        /* Handle unaligned row start safely */
        if ((idx & 31u) == 0u && end == c + 32u) {
          float sc = ldexpf(1.0f, (int)lin->w_q8_exp[g]);
          float gacc = 0.0f;
          for (uint32_t j = 0; j < 32u; ++j) gacc += (float)lin->w_q8[idx + j] * x[c + j];
          acc += gacc * sc;
        } else {
          for (uint32_t j = c; j < end; ++j) {
            size_t k = base + j;
            acc += ldexpf((float)lin->w_q8[k], (int)lin->w_q8_exp[k >> 5]) * x[j];
          }
        }
      }
      y[r] = acc;
    }
  } else if (lin->fmt == BCIR_DEPLOY_LB_PACKED1B) {
    dep_lb_path_eval(&lin->p1, x, s_xg, s_z, s_yt, y, use_portable, 0);
    if (lin->has_p2) dep_lb_path_eval(&lin->p2, x, s_xg, s_z, s_yt, y, use_portable, 1);
  }
}

/* ==================== Whole-Model Streaming Structure ==================== */
typedef struct dep_layer {
  uint32_t mixer, use_rope;
  float *norm1, *norm2, *norm_kv, *balpha, *bbeta;
  /* Indices into dm->linears[] */
  int idx_q, idx_k, idx_v, idx_o, idx_a, idx_b;
  int idx_qc, idx_qr, idx_dkv, idx_uk, idx_uv, idx_kr;
  int idx_gate, idx_up, idx_down;
  /* Streaming caches */
  float *kv_k;     /* GQA: [max_kv * kvd], SWA: [W * kvd], MLA: [max_kv * dc] (c_hat) */
  float *kv_v;     /* GQA: [max_kv * kvd], SWA: [W * kvd], MLA: [max_kv * dr] (k_r) */
  double *dn_S;    /* DeltaNet: [H * dh * dh] */
} dep_layer;

struct bcir_deploy_model {
  bcir_ext_spec sp;
  uint32_t max_kv;
  float *embed, *final_norm, *lm_head;
  size_t n_tensors;
  int *tensor_to_lin; /* -1 for non-linear, else index in linears[] */
  size_t n_linears;
  dep_linear *linears;
  dep_layer layers[BCIR_EXT_MAX_LAYERS];

  float *rope_hd, *rope_dr;
  /* Scratch vectors for single-token decode */
  float *x, *n1, *q, *k, *v, *ctx, *x2, *n2, *gate, *up, *hid;
  float *qc, *qr, *craw, *chat, *kr, *qabs, *cctx, *scores;
  float *s_xg, *s_z, *s_yt;
};

bcir_deploy_model *bcir_deploy_create(const bcir_ext_spec *spec, uint32_t max_kv_len) {
  if (!spec || max_kv_len == 0u) return NULL;
  bcir_deploy_model *dm = (bcir_deploy_model *)calloc(1u, sizeof(bcir_deploy_model));
  dm->sp = *spec; dm->max_kv = max_kv_len;
  if (dm->sp.swa_window == 0u) dm->sp.swa_window = 32u;
  if (dm->sp.mla_dc == 0u) dm->sp.mla_dc = dm->sp.width / 2u;
  if (dm->sp.mla_dr == 0u) dm->sp.mla_dr = (dm->sp.width / dm->sp.heads) / 2u;
  if (dm->sp.mla_dr < 2u) dm->sp.mla_dr = 2u;
  if (dm->sp.mla_dr & 1u) dm->sp.mla_dr++;

  size_t d = dm->sp.width, H = dm->sp.heads, KH = dm->sp.kvheads, dh = d / H;
  size_t kvd = KH * dh, ff = dm->sp.ff, V = dm->sp.vocab, dc = dm->sp.mla_dc, dr = dm->sp.mla_dr;

  dm->embed = (float *)dep_calloc(V * d, 4u);
  dm->final_norm = (float *)dep_calloc(d, 4u);
  dm->lm_head = dm->sp.tied ? dm->embed : (float *)dep_calloc(V * d, 4u);

  dm->rope_hd = (float *)dep_calloc((size_t)max_kv_len * dh, 4u);
  bcir_tensor_rope_table_pool(NULL, max_kv_len, dh, dm->sp.rope_base, dm->rope_hd);
  dm->rope_dr = (float *)dep_calloc((size_t)max_kv_len * dr, 4u);
  bcir_tensor_rope_table_pool(NULL, max_kv_len, dr, dm->sp.rope_base, dm->rope_dr);

  for (uint32_t l = 0; l < dm->sp.layers; ++l) {
    dep_layer *dl = &dm->layers[l];
    dl->mixer = dm->sp.layer_mixer[l]; dl->use_rope = dm->sp.layer_use_rope[l];
    dl->norm1 = (float *)dep_calloc(d, 4u); dl->norm2 = (float *)dep_calloc(d, 4u);
    if (dl->mixer == BCIR_MIXER_GQA) {
      dl->kv_k = (float *)dep_calloc((size_t)max_kv_len * kvd, 4u);
      dl->kv_v = (float *)dep_calloc((size_t)max_kv_len * kvd, 4u);
    } else if (dl->mixer == BCIR_MIXER_SWA) {
      dl->kv_k = (float *)dep_calloc((size_t)dm->sp.swa_window * kvd, 4u);
      dl->kv_v = (float *)dep_calloc((size_t)dm->sp.swa_window * kvd, 4u);
    } else if (dl->mixer == BCIR_MIXER_DELTANET) {
      dl->balpha = (float *)dep_calloc(H, 4u); dl->bbeta = (float *)dep_calloc(H, 4u);
      dl->dn_S = (double *)dep_calloc(H * dh * dh, sizeof(double));
    } else if (dl->mixer == BCIR_MIXER_MLA) {
      dl->norm_kv = (float *)dep_calloc(dc, 4u);
      dl->kv_k = (float *)dep_calloc((size_t)max_kv_len * dc, 4u);
      dl->kv_v = (float *)dep_calloc((size_t)max_kv_len * dr, 4u);
    }
  }
  size_t md = d > ff ? d : ff; if (dc > md) md = dc;
  dm->x = (float *)dep_calloc(d, 4u); dm->n1 = (float *)dep_calloc(d, 4u);
  dm->q = (float *)dep_calloc(d, 4u); dm->k = (float *)dep_calloc(d, 4u); dm->v = (float *)dep_calloc(d, 4u);
  dm->ctx = (float *)dep_calloc(d, 4u); dm->x2 = (float *)dep_calloc(d, 4u); dm->n2 = (float *)dep_calloc(d, 4u);
  dm->gate = (float *)dep_calloc(ff, 4u); dm->up = (float *)dep_calloc(ff, 4u); dm->hid = (float *)dep_calloc(ff, 4u);
  dm->qc = (float *)dep_calloc(d, 4u); dm->qr = (float *)dep_calloc(H * dr, 4u);
  dm->craw = (float *)dep_calloc(dc, 4u); dm->chat = (float *)dep_calloc(dc, 4u);
  dm->kr = (float *)dep_calloc(dr, 4u); dm->qabs = (float *)dep_calloc(dc, 4u);
  dm->cctx = (float *)dep_calloc(dc, 4u); dm->scores = (float *)dep_calloc(max_kv_len, 4u);
  dm->s_xg = (float *)dep_calloc(md, 4u); dm->s_z = (float *)dep_calloc(md, 4u); dm->s_yt = (float *)dep_calloc(md, 4u);
  return dm;
}

void bcir_deploy_destroy(bcir_deploy_model *dm) {
  if (!dm) return;
  if (!dm->sp.tied) free(dm->lm_head);
  free(dm->embed); free(dm->final_norm); free(dm->rope_hd); free(dm->rope_dr);
  for (size_t i = 0; i < dm->n_linears; ++i) dep_linear_free(&dm->linears[i]);
  free(dm->linears); free(dm->tensor_to_lin);
  for (uint32_t l = 0; l < dm->sp.layers; ++l) {
    dep_layer *dl = &dm->layers[l];
    free(dl->norm1); free(dl->norm2); free(dl->norm_kv); free(dl->balpha); free(dl->bbeta);
    free(dl->kv_k); free(dl->kv_v); free(dl->dn_S);
  }
  free(dm->x); free(dm->n1); free(dm->q); free(dm->k); free(dm->v);
  free(dm->ctx); free(dm->x2); free(dm->n2); free(dm->gate); free(dm->up); free(dm->hid);
  free(dm->qc); free(dm->qr); free(dm->craw); free(dm->chat); free(dm->kr);
  free(dm->qabs); free(dm->cctx); free(dm->scores);
  free(dm->s_xg); free(dm->s_z); free(dm->s_yt);
  free(dm);
}

static void encode_dense_linear(dep_linear *lin, int fmt, int region, uint32_t rows, uint32_t cols, const float *W) {
  dep_linear_free(lin);
  lin->fmt = fmt; lin->region = region; lin->rows = rows; lin->cols = cols;
  size_t n = (size_t)rows * cols;
  double wn2 = 0.0, en2 = 0.0, max_viol = 0.0;
  for (size_t i = 0; i < n; ++i) wn2 += (double)W[i] * (double)W[i];
  if (fmt == BCIR_DEPLOY_FP32) {
    lin->w_fp32 = (float *)dep_calloc(n, 4u);
    memcpy(lin->w_fp32, W, n * 4u);
    lin->artifact_bytes = n * 4u;
  } else if (fmt == BCIR_DEPLOY_FP16) {
    lin->w_fp16 = (uint16_t *)dep_calloc(n, 2u);
    for (size_t i = 0; i < n; ++i) {
      lin->w_fp16[i] = f32_to_f16_bits(W[i]);
      double e = (double)f16_bits_to_f32(lin->w_fp16[i]) - (double)W[i];
      en2 += e * e;
    }
    lin->artifact_bytes = n * 2u;
  } else if (fmt == BCIR_DEPLOY_BCIRQ8_G32) {
    size_t ng = (n + 31u) / 32u;
    lin->w_q8 = (int8_t *)dep_calloc(n, 1u);
    lin->w_q8_exp = (int8_t *)dep_calloc(ng, 1u);
    bcir_quant_q8_g32_encode(W, lin->w_q8, lin->w_q8_exp, n);
    for (size_t i = 0; i < n; ++i) {
      int expv = (int)lin->w_q8_exp[i >> 5];
      double w_hat = ldexp((double)lin->w_q8[i], expv);
      double err = fabs((double)W[i] - w_hat);
      en2 += err * err;
      double bound = ldexp(1.0, expv - 1);
      if (err > bound + 1e-12) { double v = err - bound; if (v > max_viol) max_viol = v; }
    }
    lin->artifact_bytes = n + ng;
  }
  lin->rel_frob_err = (wn2 > 0.0) ? sqrt(en2 / wn2) : 0.0;
  lin->max_q8_bound_viol = max_viol;
}

int bcir_deploy_load_from_ext(bcir_deploy_model *dm, const bcir_ext_model *src,
                              int default_attn_fmt, int default_mlp_fmt) {
  if (!dm || !src) return 1;
  size_t nt = bcir_ext_tensor_count(src);
  const float *W = bcir_ext_weights_ptr((bcir_ext_model *)src);
  for (size_t i = 0; i < dm->n_linears; ++i) dep_linear_free(&dm->linears[i]);
  free(dm->linears); free(dm->tensor_to_lin);
  dm->n_tensors = nt;
  dm->tensor_to_lin = (int *)malloc(nt * sizeof(int));
  size_t nl = 0u;
  for (size_t i = 0; i < nt; ++i) {
    bcir_ext_tensor_info ti; bcir_ext_tensor_info_at(src, i, &ti);
    if (ti.region == 1 || ti.region == 2) nl++;
  }
  dm->n_linears = nl;
  dm->linears = (dep_linear *)calloc(nl > 0u ? nl : 1u, sizeof(dep_linear));
  size_t li = 0u;
  for (size_t i = 0; i < nt; ++i) {
    bcir_ext_tensor_info ti; bcir_ext_tensor_info_at(src, i, &ti);
    const float *p = W + ti.offset;
    size_t len = ti.rows * ti.cols;
    dm->tensor_to_lin[i] = -1;
    if (strcmp(ti.name, "embed") == 0) { memcpy(dm->embed, p, len * 4u); continue; }
    if (strcmp(ti.name, "final_norm") == 0) { memcpy(dm->final_norm, p, len * 4u); continue; }
    if (strcmp(ti.name, "lm_head") == 0 && !dm->sp.tied) { memcpy(dm->lm_head, p, len * 4u); continue; }
    if (ti.layer < 0 || (uint32_t)ti.layer >= dm->sp.layers) continue;
    dep_layer *dl = &dm->layers[ti.layer];
    const char *dot = strchr(ti.name, '.');
    const char *sub = dot ? (dot + 1) : ti.name;
    if (strcmp(sub, "norm1") == 0) { memcpy(dl->norm1, p, len * 4u); continue; }
    if (strcmp(sub, "norm2") == 0) { memcpy(dl->norm2, p, len * 4u); continue; }
    if (strcmp(sub, "norm_kv") == 0) { memcpy(dl->norm_kv, p, len * 4u); continue; }
    if (strcmp(sub, "balpha") == 0) { memcpy(dl->balpha, p, len * 4u); continue; }
    if (strcmp(sub, "bbeta") == 0) { memcpy(dl->bbeta, p, len * 4u); continue; }
    if (ti.region == 1 || ti.region == 2) {
      int fmt = (ti.region == 1) ? default_attn_fmt : default_mlp_fmt;
      encode_dense_linear(&dm->linears[li], fmt, ti.region, (uint32_t)ti.rows, (uint32_t)ti.cols, p);
      dm->tensor_to_lin[i] = (int)li;
      if (strcmp(sub, "wq") == 0) dl->idx_q = (int)li;
      else if (strcmp(sub, "wk") == 0) dl->idx_k = (int)li;
      else if (strcmp(sub, "wv") == 0) dl->idx_v = (int)li;
      else if (strcmp(sub, "wo") == 0) dl->idx_o = (int)li;
      else if (strcmp(sub, "walpha") == 0) dl->idx_a = (int)li;
      else if (strcmp(sub, "wbeta") == 0) dl->idx_b = (int)li;
      else if (strcmp(sub, "wq_c") == 0) dl->idx_qc = (int)li;
      else if (strcmp(sub, "wq_r") == 0) dl->idx_qr = (int)li;
      else if (strcmp(sub, "w_dkv") == 0) dl->idx_dkv = (int)li;
      else if (strcmp(sub, "w_uk") == 0) dl->idx_uk = (int)li;
      else if (strcmp(sub, "w_uv") == 0) dl->idx_uv = (int)li;
      else if (strcmp(sub, "w_kr") == 0) dl->idx_kr = (int)li;
      else if (strcmp(sub, "wgate") == 0) dl->idx_gate = (int)li;
      else if (strcmp(sub, "wup") == 0) dl->idx_up = (int)li;
      else if (strcmp(sub, "wdown") == 0) dl->idx_down = (int)li;
      li++;
    }
  }
  return 0;
}

int bcir_deploy_set_linear_dense(bcir_deploy_model *dm, size_t tensor_idx, int fmt, const float *W_fp32) {
  if (!dm || !W_fp32 || tensor_idx >= dm->n_tensors) return 1;
  int li = dm->tensor_to_lin[tensor_idx];
  if (li < 0) return 1;
  dep_linear *lin = &dm->linears[li];
  encode_dense_linear(lin, fmt, lin->region, lin->rows, lin->cols, W_fp32);
  return 0;
}

int bcir_deploy_set_linear_lb(bcir_deploy_model *dm, size_t tensor_idx,
                              uint32_t r1, const float *U1, const float *V1,
                              const float *h1, const float *g1, const float *l1,
                              uint32_t r2, const float *U2, const float *V2,
                              const float *h2, const float *g2, const float *l2,
                              const float *fp32_ref_or_null) {
  if (!dm || tensor_idx >= dm->n_tensors) return 1;
  int li = dm->tensor_to_lin[tensor_idx];
  if (li < 0) return 1;
  dep_linear *lin = &dm->linears[li];
  uint32_t R = lin->rows, C = lin->cols; int reg = lin->region;
  dep_linear_free(lin);
  lin->fmt = BCIR_DEPLOY_LB_PACKED1B; lin->region = reg; lin->rows = R; lin->cols = C;
  dep_lb_path_pack(&lin->p1, C, R, r1, U1, V1, h1, g1, l1);
  lin->artifact_bytes = lin->p1.bytes;
  if (r2 > 0u && U2 && V2 && h2 && g2 && l2) {
    dep_lb_path_pack(&lin->p2, C, R, r2, U2, V2, h2, g2, l2);
    lin->has_p2 = 1;
    lin->artifact_bytes += lin->p2.bytes;
  }
  if (fp32_ref_or_null) {
    double wn2 = 0.0, en2 = 0.0;
    for (uint32_t o = 0; o < R; ++o) {
      for (uint32_t c = 0; c < C; ++c) {
        double w = (double)fp32_ref_or_null[(size_t)o * C + c];
        double s1 = 0.0, s2 = 0.0;
        for (uint32_t m = 0; m < r1; ++m) {
          double us = (U1[(size_t)o * r1 + m] >= 0.0f) ? 1.0 : -1.0;
          double vs = (V1[(size_t)c * r1 + m] >= 0.0f) ? 1.0 : -1.0;
          s1 += us * (double)lin->p1.l[m] * vs;
        }
        double wh = (double)lin->p1.h[o] * s1 * (double)lin->p1.g[c];
        if (lin->has_p2) {
          for (uint32_t m = 0; m < r2; ++m) {
            double us = (U2[(size_t)o * r2 + m] >= 0.0f) ? 1.0 : -1.0;
            double vs = (V2[(size_t)c * r2 + m] >= 0.0f) ? 1.0 : -1.0;
            s2 += us * (double)lin->p2.l[m] * vs;
          }
          wh += (double)lin->p2.h[o] * s2 * (double)lin->p2.g[c];
        }
        wn2 += w * w; en2 += (w - wh) * (w - wh);
      }
    }
    lin->rel_frob_err = (wn2 > 0.0) ? sqrt(en2 / wn2) : 0.0;
  }
  return 0;
}

void bcir_deploy_get_stats(const bcir_deploy_model *dm, bcir_deploy_stats *out) {
  if (!dm || !out) return;
  memset(out, 0, sizeof(*out));
  size_t d = dm->sp.width, H = dm->sp.heads, KH = dm->sp.kvheads, dh = d / H;
  size_t kvd = KH * dh, V = dm->sp.vocab, dc = dm->sp.mla_dc, dr = dm->sp.mla_dr;
  size_t nonlin_p = V * d + d + (dm->sp.tied ? 0u : V * d);
  for (uint32_t l = 0; l < dm->sp.layers; ++l) {
    nonlin_p += 2u * d;
    uint32_t mx = dm->layers[l].mixer;
    if (mx == BCIR_MIXER_DELTANET) { nonlin_p += 2u * H; out->rec_state_bytes += H * dh * dh * 4u; }
    else if (mx == BCIR_MIXER_MLA) { nonlin_p += dc; }
  }
  out->nonlin_bytes = nonlin_p * 2u; /* FP16 non-linear deployment storage */
  for (size_t i = 0; i < dm->n_linears; ++i) {
    const dep_linear *lin = &dm->linears[i];
    size_t np = (size_t)lin->rows * lin->cols;
    out->body_params += np;
    if (lin->region == 1) {
      out->attn_bytes += lin->artifact_bytes;
      if (lin->rel_frob_err > out->max_weight_rel_frob_attn) out->max_weight_rel_frob_attn = lin->rel_frob_err;
    } else {
      out->mlp_bytes += lin->artifact_bytes;
      if (lin->rel_frob_err > out->max_weight_rel_frob_mlp) out->max_weight_rel_frob_mlp = lin->rel_frob_err;
    }
    if (lin->max_q8_bound_viol > out->max_q8_bound_violation) out->max_q8_bound_violation = lin->max_q8_bound_viol;
  }
  out->total_params = out->body_params + nonlin_p;
  out->total_artifact_bytes = out->attn_bytes + out->mlp_bytes + out->nonlin_bytes;
  out->realized_body_bpw = (out->body_params > 0u) ? (8.0 * (double)(out->attn_bytes + out->mlp_bytes) / (double)out->body_params) : 0.0;
  out->realized_total_bpw = (out->total_params > 0u) ? (8.0 * (double)out->total_artifact_bytes / (double)out->total_params) : 0.0;

  /* FP16 KV cache footprint at T=64, 1024, 4096 */
  size_t lens[3] = { 64u, 1024u, 4096u }, kvs[3] = { 0u, 0u, 0u };
  for (int k = 0; k < 3; ++k) {
    for (uint32_t l = 0; l < dm->sp.layers; ++l) {
      uint32_t mx = dm->layers[l].mixer;
      if (mx == BCIR_MIXER_GQA) kvs[k] += lens[k] * 2u * kvd * 2u;
      else if (mx == BCIR_MIXER_SWA) {
        size_t eff = (lens[k] < dm->sp.swa_window) ? lens[k] : dm->sp.swa_window;
        kvs[k] += eff * 2u * kvd * 2u;
      } else if (mx == BCIR_MIXER_MLA) {
        kvs[k] += lens[k] * (dc + dr) * 2u;
      }
    }
  }
  out->kv_cache_bytes_at_64 = kvs[0];
  out->kv_cache_bytes_at_1024 = kvs[1];
  out->kv_cache_bytes_at_4096 = kvs[2];
}

void bcir_deploy_reset_cache(bcir_deploy_model *dm) {
  if (!dm) return;
  size_t d = dm->sp.width, H = dm->sp.heads, dh = d / H;
  for (uint32_t l = 0; l < dm->sp.layers; ++l) {
    if (dm->layers[l].dn_S) memset(dm->layers[l].dn_S, 0, H * dh * dh * sizeof(double));
  }
}

static void dep_rms_1(size_t width, double eps, const float *x, const float *g, float *y) {
  double ss = 0.0;
  for (size_t i = 0; i < width; ++i) ss += (double)x[i] * (double)x[i];
  float inv = (float)(1.0 / sqrt(ss / (double)width + eps));
  for (size_t i = 0; i < width; ++i) y[i] = (x[i] * inv) * g[i];
}

static void dep_rope_1(size_t heads, size_t dim, const float *table_row, float *x) {
  size_t half = dim / 2u;
  for (size_t h = 0; h < heads; ++h) {
    float *xh = x + h * dim;
    for (size_t j = 0; j < half; ++j) {
      float c = table_row[j], s = table_row[half + j];
      float a = xh[j], b = xh[half + j];
      xh[j] = a * c - b * s;
      xh[half + j] = a * s + b * c;
    }
  }
}

static int dep_decode_step_timed(bcir_deploy_model *dm, uint32_t token_id, uint32_t pos,
                                 int use_portable, float *out_logits,
                                 double *attn_us_acc, double *mlp_us_acc) {
  if (!dm || token_id >= dm->sp.vocab || pos >= dm->max_kv) return 1;
  size_t d = dm->sp.width, H = dm->sp.heads, KH = dm->sp.kvheads, dh = d / H;
  size_t kvd = KH * dh, ff = dm->sp.ff, V = dm->sp.vocab, dc = dm->sp.mla_dc, dr = dm->sp.mla_dr;
  memcpy(dm->x, dm->embed + (size_t)token_id * d, d * 4u);

  for (uint32_t l = 0; l < dm->sp.layers; ++l) {
    dep_layer *dl = &dm->layers[l];
    double ta0 = attn_us_acc ? dep_now_us() : 0.0;
    dep_rms_1(d, dm->sp.rms_eps, dm->x, dl->norm1, dm->n1);
    if (dl->mixer == BCIR_MIXER_GQA || dl->mixer == BCIR_MIXER_SWA) {
      dep_linear_gemv(&dm->linears[dl->idx_q], dm->n1, dm->q, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_k], dm->n1, dm->k, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_v], dm->n1, dm->v, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      if (dl->use_rope) {
        dep_rope_1(H, dh, dm->rope_hd + (size_t)pos * dh, dm->q);
        dep_rope_1(KH, dh, dm->rope_hd + (size_t)pos * dh, dm->k);
      }
      size_t W = (dl->mixer == BCIR_MIXER_SWA) ? dm->sp.swa_window : dm->max_kv;
      size_t slot = (dl->mixer == BCIR_MIXER_SWA) ? (pos % W) : pos;
      memcpy(dl->kv_k + slot * kvd, dm->k, kvd * 4u);
      memcpy(dl->kv_v + slot * kvd, dm->v, kvd * 4u);
      size_t j0 = (dl->mixer == BCIR_MIXER_SWA && pos + 1u > W) ? (pos + 1u - W) : 0u;
      size_t len = pos + 1u - j0;
      double scale = 1.0 / sqrt((double)dh);
      for (size_t h = 0; h < H; ++h) {
        size_t kvh = h / (H / KH);
        const float *qh = dm->q + h * dh;
        double mx = -1e30;
        for (size_t u = 0; u < len; ++u) {
          size_t j = j0 + u, s_j = (dl->mixer == BCIR_MIXER_SWA) ? (j % W) : j;
          const float *kj = dl->kv_k + s_j * kvd + kvh * dh;
          double s = 0.0; for (size_t a = 0; a < dh; ++a) s += (double)qh[a] * (double)kj[a];
          s *= scale; dm->scores[u] = (float)s; if (s > mx) mx = s;
        }
        double sum = 0.0;
        for (size_t u = 0; u < len; ++u) { double e = exp((double)dm->scores[u] - mx); dm->scores[u] = (float)e; sum += e; }
        double inv = 1.0 / sum;
        float *ch = dm->ctx + h * dh;
        for (size_t a = 0; a < dh; ++a) {
          double acc = 0.0;
          for (size_t u = 0; u < len; ++u) {
            size_t j = j0 + u, s_j = (dl->mixer == BCIR_MIXER_SWA) ? (j % W) : j;
            acc += ((double)dm->scores[u] * inv) * (double)dl->kv_v[s_j * kvd + kvh * dh + a];
          }
          ch[a] = (float)acc;
        }
      }
      dep_linear_gemv(&dm->linears[dl->idx_o], dm->ctx, dm->x2, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    } else if (dl->mixer == BCIR_MIXER_DELTANET) {
      dep_linear_gemv(&dm->linears[dl->idx_q], dm->n1, dm->q, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_k], dm->n1, dm->k, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_v], dm->n1, dm->v, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      float ap[64], bp[64];
      dep_linear_gemv(&dm->linears[dl->idx_a], dm->n1, ap, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_b], dm->n1, bp, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      for (size_t h = 0; h < H; ++h) {
        float *qh = dm->q + h * dh, *kh = dm->k + h * dh, *vh = dm->v + h * dh, *yh = dm->ctx + h * dh;
        double sq = 1e-6, sk = 1e-6;
        for (size_t a = 0; a < dh; ++a) { sq += (double)qh[a] * qh[a]; sk += (double)kh[a] * kh[a]; }
        double iq = 1.0 / sqrt(sq), ik = 1.0 / sqrt(sk);
        for (size_t a = 0; a < dh; ++a) { qh[a] = (float)(qh[a] * iq); kh[a] = (float)(kh[a] * ik); }
        float al_in = ap[h] + dl->balpha[h], be_in = bp[h] + dl->bbeta[h];
        double al = (al_in >= 0.0f) ? (1.0 / (1.0 + exp(-(double)al_in))) : (exp((double)al_in) / (1.0 + exp((double)al_in)));
        double be = (be_in >= 0.0f) ? (1.0 / (1.0 + exp(-(double)be_in))) : (exp((double)be_in) / (1.0 + exp((double)be_in)));
        double *S = dl->dn_S + h * dh * dh;
        double kS[256];
        for (size_t c = 0; c < dh; ++c) {
          double acc = 0.0; for (size_t a = 0; a < dh; ++a) acc += (double)kh[a] * S[a * dh + c];
          kS[c] = acc;
        }
        for (size_t a = 0; a < dh; ++a)
          for (size_t c = 0; c < dh; ++c)
            S[a * dh + c] = al * (S[a * dh + c] - be * (double)kh[a] * kS[c]) + be * (double)kh[a] * (double)vh[c];
        for (size_t c = 0; c < dh; ++c) {
          double acc = 0.0; for (size_t a = 0; a < dh; ++a) acc += (double)qh[a] * S[a * dh + c];
          yh[c] = (float)acc;
        }
      }
      dep_linear_gemv(&dm->linears[dl->idx_o], dm->ctx, dm->x2, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    } else if (dl->mixer == BCIR_MIXER_MLA) {
      dep_linear_gemv(&dm->linears[dl->idx_qc], dm->n1, dm->qc, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_qr], dm->n1, dm->qr, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_linear_gemv(&dm->linears[dl->idx_dkv], dm->n1, dm->craw, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      dep_rms_1(dc, dm->sp.rms_eps, dm->craw, dl->norm_kv, dm->chat);
      dep_linear_gemv(&dm->linears[dl->idx_kr], dm->n1, dm->kr, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
      if (dl->use_rope) {
        dep_rope_1(H, dr, dm->rope_dr + (size_t)pos * dr, dm->qr);
        dep_rope_1(1u, dr, dm->rope_dr + (size_t)pos * dr, dm->kr);
      }
      memcpy(dl->kv_k + (size_t)pos * dc, dm->chat, dc * 4u);
      memcpy(dl->kv_v + (size_t)pos * dr, dm->kr, dr * 4u);
      /* Absorbed decode: q_abs = W_uk_h^T q_c_h against cached c_hat, then y_h = W_uv_h c_ctx */
      const dep_linear *luk = &dm->linears[dl->idx_uk], *luv = &dm->linears[dl->idx_uv];
      double scale = 1.0 / sqrt((double)(dh + dr));
      for (size_t h = 0; h < H; ++h) {
        const float *qch = dm->qc + h * dh, *qrh = dm->qr + h * dr;
        for (size_t c = 0; c < dc; ++c) {
          double acc = 0.0;
          for (size_t a = 0; a < dh; ++a) {
            size_t row = h * dh + a;
            float w = 0.0f;
            if (luk->fmt == BCIR_DEPLOY_FP32) w = luk->w_fp32[row * dc + c];
            else if (luk->fmt == BCIR_DEPLOY_FP16) w = f16_bits_to_f32(luk->w_fp16[row * dc + c]);
            else if (luk->fmt == BCIR_DEPLOY_BCIRQ8_G32) {
              size_t k = row * dc + c; w = ldexpf((float)luk->w_q8[k], (int)luk->w_q8_exp[k >> 5]);
            }
            acc += (double)w * (double)qch[a];
          }
          dm->qabs[c] = (float)acc;
        }
        double mx = -1e30;
        for (size_t j = 0; j <= pos; ++j) {
          const float *chj = dl->kv_k + j * dc, *krj = dl->kv_v + j * dr;
          double s = 0.0;
          for (size_t c = 0; c < dc; ++c) s += (double)dm->qabs[c] * (double)chj[c];
          for (size_t a = 0; a < dr; ++a) s += (double)qrh[a] * (double)krj[a];
          s *= scale; dm->scores[j] = (float)s; if (s > mx) mx = s;
        }
        double sum = 0.0;
        for (size_t j = 0; j <= pos; ++j) { double e = exp((double)dm->scores[j] - mx); dm->scores[j] = (float)e; sum += e; }
        double inv = 1.0 / sum;
        memset(dm->cctx, 0, dc * 4u);
        for (size_t j = 0; j <= pos; ++j) {
          float p = (float)((double)dm->scores[j] * inv);
          const float *chj = dl->kv_k + j * dc;
          for (size_t c = 0; c < dc; ++c) dm->cctx[c] += p * chj[c];
        }
        float *yh = dm->ctx + h * dh;
        for (size_t a = 0; a < dh; ++a) {
          size_t row = h * dh + a;
          double acc = 0.0;
          for (size_t c = 0; c < dc; ++c) {
            float w = 0.0f;
            if (luv->fmt == BCIR_DEPLOY_FP32) w = luv->w_fp32[row * dc + c];
            else if (luv->fmt == BCIR_DEPLOY_FP16) w = f16_bits_to_f32(luv->w_fp16[row * dc + c]);
            else if (luv->fmt == BCIR_DEPLOY_BCIRQ8_G32) {
              size_t k = row * dc + c; w = ldexpf((float)luv->w_q8[k], (int)luv->w_q8_exp[k >> 5]);
            }
            acc += (double)w * (double)dm->cctx[c];
          }
          yh[a] = (float)acc;
        }
      }
      dep_linear_gemv(&dm->linears[dl->idx_o], dm->ctx, dm->x2, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    }
    for (size_t i = 0; i < d; ++i) dm->x2[i] += dm->x[i];
    if (attn_us_acc) *attn_us_acc += dep_now_us() - ta0;

    double tm0 = mlp_us_acc ? dep_now_us() : 0.0;
    dep_rms_1(d, dm->sp.rms_eps, dm->x2, dl->norm2, dm->n2);
    dep_linear_gemv(&dm->linears[dl->idx_gate], dm->n2, dm->gate, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    dep_linear_gemv(&dm->linears[dl->idx_up], dm->n2, dm->up, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    for (size_t i = 0; i < ff; ++i) {
      float g = dm->gate[i];
      float sig = (g >= 0.0f) ? (1.0f / (1.0f + expf(-g))) : (expf(g) / (1.0f + expf(g)));
      dm->hid[i] = (g * sig) * dm->up[i];
    }
    dep_linear_gemv(&dm->linears[dl->idx_down], dm->hid, dm->x, dm->s_xg, dm->s_z, dm->s_yt, use_portable);
    for (size_t i = 0; i < d; ++i) dm->x[i] += dm->x2[i];
    if (mlp_us_acc) *mlp_us_acc += dep_now_us() - tm0;
  }

  dep_rms_1(d, dm->sp.rms_eps, dm->x, dm->final_norm, dm->n1);
  if (out_logits) {
    for (size_t v = 0; v < V; ++v) {
      const float *wr = dm->lm_head + v * d;
      float acc = 0.0f;
      for (size_t c = 0; c < d; ++c) acc += wr[c] * dm->n1[c];
      out_logits[v] = acc;
    }
  }
  return 0;
}

int bcir_deploy_decode_step(bcir_deploy_model *dm, uint32_t token_id, uint32_t pos,
                            int use_portable, float *out_logits) {
  return dep_decode_step_timed(dm, token_id, pos, use_portable, out_logits, NULL, NULL);
}

int bcir_deploy_prefill_seq(bcir_deploy_model *dm, const uint32_t *tokens, uint32_t seq_len,
                            int use_portable, float *out_logits) {
  if (!dm || !tokens || seq_len == 0u || seq_len > dm->max_kv) return 1;
  bcir_deploy_reset_cache(dm);
  size_t V = dm->sp.vocab;
  for (uint32_t t = 0; t < seq_len; ++t) {
    float *dst = out_logits ? (out_logits + (size_t)t * V) : NULL;
    int rc = dep_decode_step_timed(dm, tokens[t], t, use_portable, dst, NULL, NULL);
    if (rc != 0) return rc;
  }
  return 0;
}

int bcir_deploy_evaluate(bcir_deploy_model *dm, const uint32_t *tokens, const uint32_t *targets,
                         size_t n_batches, uint32_t batch_size, uint32_t stride_T,
                         uint32_t active_T, int use_portable, bcir_ext_eval_result *out) {
  if (!dm || !tokens || !targets || !out || active_T == 0u || active_T > dm->max_kv) return 1;
  double t0 = dep_now_us() * 1e-3, loss_sum = 0.0;
  uint64_t tok_cnt = 0u, hits = 0u, nf = 0u;
  size_t V = dm->sp.vocab;
  float *logits = (float *)dep_calloc(V, 4u);
  for (size_t bi = 0; bi < n_batches; ++bi) {
    for (uint32_t b = 0; b < batch_size; ++b) {
      const uint32_t *seq_tk = tokens + (bi * batch_size + b) * stride_T;
      const uint32_t *seq_tg = targets + (bi * batch_size + b) * stride_T;
      bcir_deploy_reset_cache(dm);
      for (uint32_t t = 0; t < active_T; ++t) {
        if (dep_decode_step_timed(dm, seq_tk[t], t, use_portable, logits, NULL, NULL) != 0) nf++;
        uint32_t y = seq_tg[t];
        if (y == BCIR_EXT_IGNORE) continue;
        double mx = -1e30; uint32_t best = 0u;
        for (size_t v = 0; v < V; ++v) {
          double lv = (double)logits[v];
          if (!isfinite(lv)) { nf++; break; }
          if (lv > mx) { mx = lv; best = (uint32_t)v; }
        }
        double se = 0.0;
        for (size_t v = 0; v < V; ++v) se += exp((double)logits[v] - mx);
        double ce = (mx + log(se)) - (double)logits[y];
        if (!isfinite(ce)) nf++; else loss_sum += ce;
        if (best == y) hits++;
        tok_cnt++;
      }
    }
  }
  free(logits);
  out->tokens = tok_cnt; out->nonfinite = nf;
  out->ce = (tok_cnt > 0u) ? (loss_sum / (double)tok_cnt) : 0.0;
  out->accuracy = (tok_cnt > 0u) ? ((double)hits / (double)tok_cnt) : 0.0;
  out->eval_ms = dep_now_us() * 1e-3 - t0;
  return (nf > 0u) ? 3 : 0;
}

static int cmp_dbl(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return (x < y) ? -1 : (x > y ? 1 : 0);
}

int bcir_deploy_benchmark(bcir_deploy_model *dm, uint32_t ctx_len, uint32_t decode_iters,
                          uint32_t prefill_iters, bcir_deploy_bench *out) {
  if (!dm || !out || ctx_len == 0u || ctx_len >= dm->max_kv) return 1;
  memset(out, 0, sizeof(*out));
  size_t V = dm->sp.vocab;
  float *l_avx = (float *)dep_calloc(V, 4u), *l_por = (float *)dep_calloc(V, 4u);
  uint32_t *seq = (uint32_t *)malloc((ctx_len + 128u) * sizeof(uint32_t));
  for (uint32_t i = 0; i < ctx_len + 128u; ++i) seq[i] = (i * 17u + 5u) % dm->sp.vocab;

  /* Parity check: portable vs AVX-512 on one prefilled context */
  bcir_deploy_reset_cache(dm);
  for (uint32_t t = 0; t < ctx_len; ++t) dep_decode_step_timed(dm, seq[t], t, 0, NULL, NULL, NULL);
  dep_decode_step_timed(dm, seq[ctx_len], ctx_len, 0, l_avx, NULL, NULL);
  bcir_deploy_reset_cache(dm);
  for (uint32_t t = 0; t < ctx_len; ++t) dep_decode_step_timed(dm, seq[t], t, 1, NULL, NULL, NULL);
  dep_decode_step_timed(dm, seq[ctx_len], ctx_len, 1, l_por, NULL, NULL);
  double md = 0.0;
  for (size_t v = 0; v < V; ++v) { double d = fabs((double)l_avx[v] - (double)l_por[v]); if (d > md) md = d; }
  out->max_portable_avx_diff = md;

  /* Decode latency at ctx_len (median of decode_iters) */
  if (decode_iters < 3u) decode_iters = 3u;
  double *ts = (double *)malloc(decode_iters * sizeof(double));
  double a_sum = 0.0, m_sum = 0.0, tot_sum = 0.0;
  for (uint32_t it = 0; it < decode_iters; ++it) {
    double au = 0.0, mu = 0.0, t0 = dep_now_us();
    dep_decode_step_timed(dm, seq[ctx_len + (it & 31u)], ctx_len, 0, l_avx, &au, &mu);
    double dt = dep_now_us() - t0;
    ts[it] = dt; a_sum += au; m_sum += mu; tot_sum += dt;
  }
  qsort(ts, decode_iters, sizeof(double), cmp_dbl);
  out->decode_median_us = ts[decode_iters / 2u];
  out->decode_tok_per_s = (out->decode_median_us > 0.0) ? (1e6 / out->decode_median_us) : 0.0;
  out->decode_attn_frac = (tot_sum > 0.0) ? (a_sum / tot_sum) : 0.0;
  out->decode_mlp_frac = (tot_sum > 0.0) ? (m_sum / tot_sum) : 0.0;
  free(ts);

  /* Prefill N=16, 64, 128 (if <= max_kv) */
  uint32_t ns[3] = { 16u, 64u, 128u };
  double *pf_out[3] = { &out->prefill_n16_ms, &out->prefill_n64_ms, &out->prefill_n128_ms };
  if (prefill_iters < 1u) prefill_iters = 1u;
  double *pt = (double *)malloc(prefill_iters * sizeof(double));
  for (int k = 0; k < 3; ++k) {
    if (ns[k] > dm->max_kv) { *pf_out[k] = 0.0; continue; }
    for (uint32_t it = 0; it < prefill_iters; ++it) {
      double t0 = dep_now_us();
      bcir_deploy_reset_cache(dm);
      for (uint32_t t = 0; t < ns[k]; ++t)
        dep_decode_step_timed(dm, seq[t], t, 0, (t + 1u == ns[k]) ? l_avx : NULL, NULL, NULL);
      pt[it] = (dep_now_us() - t0) * 1e-3;
    }
    qsort(pt, prefill_iters, sizeof(double), cmp_dbl);
    *pf_out[k] = pt[prefill_iters / 2u];
  }
  free(pt); free(seq); free(l_avx); free(l_por);
  return 0;
}
