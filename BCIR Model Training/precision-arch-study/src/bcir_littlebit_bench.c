#include "bcir_littlebit.h"
#if defined(__AVX512F__) && defined(__AVX512DQ__)
#include <immintrin.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec * 1e-3;
}

static int cmp_double(const void *a, const void *b) {
  double da = *(const double *)a, db = *(const double *)b;
  return (da > db) - (da < db);
}

static void *aligned64(size_t bytes) {
  if (bytes == 0) bytes = 64;
  size_t a = (bytes + 63u) & ~(size_t)63u;
  void *p = aligned_alloc(64, a);
  if (p) memset(p, 0, a);
  return p;
}

static uint32_t xorshift32(uint32_t *st) {
  uint32_t x = *st;
  x ^= x << 13; x ^= x >> 17; x ^= x << 5;
  *st = x;
  return x;
}

/* Pack binary matrix M [rows x cols] (values +1/-1) into:
 *  - bitplane [rows x ceil(cols/64)] uint64_t (bit k set => -1.0f, clear => +1.0f)
 *  - nibble4  [ceil(cols/4) x rows_padded16] uint8_t (unpacked 1B/nibble)
 *  - nib4_p1b [ceil(cols/4) x (rows_padded32/2)] uint8_t (exact 1.0-bit/weight:
 *      for each 32-row block b and lane 0..15, low nibble = row 32*b + lane,
 *      high nibble = row 32*b + 16 + lane)
 */
typedef struct packed_binmat {
  uint32_t rows, cols, rows16, rows32, bytes_per_g, words_per_row, groups4;
  uint64_t *bits;
  uint8_t *nib4;     /* [groups4 x rows16] (unpacked 1B/nibble) */
  uint8_t *nib4_p1b; /* [groups4 x bytes_per_g] (exact 1.0 bit/weight) */
  float *dense_pm1;  /* reference +1/-1 */
} packed_binmat;

static packed_binmat make_packed_binmat(uint32_t rows, uint32_t cols, uint32_t *rng) {
  packed_binmat pm;
  pm.rows = rows; pm.cols = cols;
  pm.rows16 = (rows + 15u) & ~15u;
  pm.rows32 = (rows + 31u) & ~31u;
  pm.bytes_per_g = pm.rows32 >> 1;
  pm.words_per_row = (cols + 63u) / 64u;
  pm.groups4 = (cols + 3u) / 4u;
  pm.bits = (uint64_t *)aligned64((size_t)rows * pm.words_per_row * sizeof(uint64_t));
  pm.nib4 = (uint8_t *)aligned64((size_t)pm.groups4 * pm.rows16);
  pm.nib4_p1b = (uint8_t *)aligned64((size_t)pm.groups4 * pm.bytes_per_g);
  pm.dense_pm1 = (float *)aligned64((size_t)rows * cols * sizeof(float));
  for (uint32_t r = 0; r < rows; r++) {
    uint32_t blk32 = r >> 5;
    uint32_t lane = r & 15u;
    uint32_t is_hi = (r >> 4) & 1u;
    for (uint32_t c = 0; c < cols; c++) {
      uint32_t neg = xorshift32(rng) & 1u;
      pm.dense_pm1[(size_t)r * cols + c] = neg ? -1.0f : 1.0f;
      if (neg) {
        uint8_t bit = (uint8_t)(1u << (c & 3u));
        pm.bits[(size_t)r * pm.words_per_row + (c >> 6)] |= (1ull << (c & 63u));
        pm.nib4[(size_t)(c >> 2) * pm.rows16 + r] |= bit;
        pm.nib4_p1b[(size_t)(c >> 2) * pm.bytes_per_g + (blk32 << 4) + lane] |=
            (uint8_t)(is_hi ? (bit << 4) : bit);
      }
    }
  }
  return pm;
}

static void free_packed_binmat(packed_binmat *pm) {
  free(pm->bits); free(pm->nib4); free(pm->nib4_p1b); free(pm->dense_pm1);
}

/* Portable C bit-packed binary matmul: Y[N x rows] = X[N x cols] @ B^T[cols x rows]
 * Uses zero FP multiplies on binary weights: pure sign-bit XOR + add! */
static void bin_mm_portable_rows(uint32_t N, const float *X, const packed_binmat *B, float *Y,
                                 uint32_t r_begin, uint32_t r_end) {
  uint32_t R = B->rows, C = B->cols, W = B->words_per_row;
  if (r_end > R) r_end = R;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * C;
    float *yn = Y + (size_t)n * R;
    for (uint32_t r = r_begin; r < r_end; r++) {
      const uint64_t *br = B->bits + (size_t)r * W;
      float acc = 0.0f;
      for (uint32_t w = 0; w < W; w++) {
        uint64_t bits = br[w];
        uint32_t c0 = w << 6;
        uint32_t lim = (c0 + 64u <= C) ? 64u : (C - c0);
        for (uint32_t k = 0; k < lim; k++) {
          float v = xn[c0 + k];
          acc += ((bits >> k) & 1ull) ? -v : v;
        }
      }
      yn[r] = acc;
    }
  }
}

/* AVX-512 Sign-XOR bit-packed binary matmul: 16-wide masked sign-flip + add, ZERO FP multiplies */
static void bin_mm_avx512_signxor_rows(uint32_t N, const float *X, const packed_binmat *B, float *Y,
                                       uint32_t r_begin, uint32_t r_end) {
  uint32_t R = B->rows, C = B->cols, W = B->words_per_row;
  const __m512 sign_bit = _mm512_set1_ps(-0.0f);
  if (r_end > R) r_end = R;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * C;
    float *yn = Y + (size_t)n * R;
    for (uint32_t r = r_begin; r < r_end; r++) {
      const uint64_t *br = B->bits + (size_t)r * W;
      __m512 acc0 = _mm512_setzero_ps();
      __m512 acc1 = _mm512_setzero_ps();
      uint32_t c = 0;
      for (; c + 64u <= C; c += 64u) {
        uint64_t bits = br[c >> 6];
        __m512 x0 = _mm512_loadu_ps(xn + c);
        __m512 x1 = _mm512_loadu_ps(xn + c + 16);
        __m512 x2 = _mm512_loadu_ps(xn + c + 32);
        __m512 x3 = _mm512_loadu_ps(xn + c + 48);
        x0 = _mm512_mask_xor_ps(x0, (__mmask16)(bits), x0, sign_bit);
        x1 = _mm512_mask_xor_ps(x1, (__mmask16)(bits >> 16), x1, sign_bit);
        x2 = _mm512_mask_xor_ps(x2, (__mmask16)(bits >> 32), x2, sign_bit);
        x3 = _mm512_mask_xor_ps(x3, (__mmask16)(bits >> 48), x3, sign_bit);
        acc0 = _mm512_add_ps(acc0, _mm512_add_ps(x0, x1));
        acc1 = _mm512_add_ps(acc1, _mm512_add_ps(x2, x3));
      }
      float sum = _mm512_reduce_add_ps(_mm512_add_ps(acc0, acc1));
      if (c < C) {
        uint64_t bits = br[c >> 6];
        for (uint32_t k = 0; c + k < C; k++) {
          float v = xn[c + k];
          sum += ((bits >> k) & 1ull) ? -v : v;
        }
      }
      yn[r] = sum;
    }
  }
}

/* AVX-512 4-Weight 16-Entry LUT (T-MAC / BitNet.cpp style):
 * For each 4-element group of X, build ONE 16-float ZMM LUT covering all 16 sign combos,
 * then accumulate 64 binary MACs (16 rows x 4 weights) in TWO instructions (vpermps + vaddps)! */
static void build_lut4_zmm(const float *x, uint32_t rem, __m512 *out_lut) {
  float x0 = rem > 0 ? x[0] : 0.0f;
  float x1 = rem > 1 ? x[1] : 0.0f;
  float x2 = rem > 2 ? x[2] : 0.0f;
  float x3 = rem > 3 ? x[3] : 0.0f;
  float s01_pp = x0 + x1, s01_mp = -x0 + x1, s01_pm = x0 - x1, s01_mm = -x0 - x1;
  float s23_pp = x2 + x3, s23_mp = -x2 + x3, s23_pm = x2 - x3, s23_mm = -x2 - x3;
  float t[16] __attribute__((aligned(64))) = {
    s01_pp + s23_pp, s01_mp + s23_pp, s01_pm + s23_pp, s01_mm + s23_pp,
    s01_pp + s23_mp, s01_mp + s23_mp, s01_pm + s23_mp, s01_mm + s23_mp,
    s01_pp + s23_pm, s01_mp + s23_pm, s01_pm + s23_pm, s01_mm + s23_pm,
    s01_pp + s23_mm, s01_mp + s23_mm, s01_pm + s23_mm, s01_mm + s23_mm
  };
  *out_lut = _mm512_load_ps(t);
}

static void bin_mm_avx512_lut4_rows(uint32_t N, const float *X, const packed_binmat *B, float *Y,
                                    __m512 *lut_buf, float *row_acc,
                                    uint32_t r_begin, uint32_t r_end) {
  uint32_t R = B->rows, R16 = B->rows16, C = B->cols, G = B->groups4;
  uint32_t b0 = r_begin & ~15u;
  uint32_t b1 = (r_end + 15u) & ~15u;
  if (b1 > R16) b1 = R16;
  uint32_t r_hi = r_end < R ? r_end : R;
  if (b0 >= r_hi) return;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * C;
    float *yn = Y + (size_t)n * R;
    for (uint32_t g = 0; g < G; g++) {
      uint32_t c0 = g << 2;
      build_lut4_zmm(xn + c0, C - c0, &lut_buf[g]);
    }
    for (uint32_t r0 = b0; r0 < b1; r0 += 64u) {
      uint32_t nb = (b1 - r0 >= 64u) ? 4u : ((b1 - r0) >> 4);
      __m512 a0 = _mm512_setzero_ps(), a1 = _mm512_setzero_ps();
      __m512 a2 = _mm512_setzero_ps(), a3 = _mm512_setzero_ps();
      for (uint32_t g = 0; g < G; g++) {
        __m512 lut = lut_buf[g];
        const uint8_t *p = B->nib4 + (size_t)g * R16 + r0;
        if (nb == 4u) {
          __m512i i0 = _mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p)));
          __m512i i1 = _mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p + 16)));
          __m512i i2 = _mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p + 32)));
          __m512i i3 = _mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p + 48)));
          a0 = _mm512_add_ps(a0, _mm512_permutexvar_ps(i0, lut));
          a1 = _mm512_add_ps(a1, _mm512_permutexvar_ps(i1, lut));
          a2 = _mm512_add_ps(a2, _mm512_permutexvar_ps(i2, lut));
          a3 = _mm512_add_ps(a3, _mm512_permutexvar_ps(i3, lut));
        } else {
          if (nb > 0u) a0 = _mm512_add_ps(a0, _mm512_permutexvar_ps(_mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p))), lut));
          if (nb > 1u) a1 = _mm512_add_ps(a1, _mm512_permutexvar_ps(_mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p + 16))), lut));
          if (nb > 2u) a2 = _mm512_add_ps(a2, _mm512_permutexvar_ps(_mm512_cvtepu8_epi32(_mm_loadu_si128((const __m128i *)(p + 32))), lut));
        }
      }
      if (nb > 0u) _mm512_store_ps(row_acc + (r0 - b0),      a0);
      if (nb > 1u) _mm512_store_ps(row_acc + (r0 - b0) + 16, a1);
      if (nb > 2u) _mm512_store_ps(row_acc + (r0 - b0) + 32, a2);
      if (nb > 3u) _mm512_store_ps(row_acc + (r0 - b0) + 48, a3);
    }
    memcpy(yn + r_begin, row_acc + (r_begin - b0), (size_t)(r_hi - r_begin) * sizeof(float));
  }
}

/* Exact 1.0-bit/weight 2-nibbles-per-byte AVX-512 LUT4 (token-major, decode-oriented):
 * Loads 16 bytes -> 32 output rows x 4 input cols = 128 binary weights in 16 bytes. */
static void bin_mm_avx512_lut4_packed1b_rows(uint32_t N, const float *X, const packed_binmat *B,
                                             float *Y, __m512 *lut_buf, float *row_acc,
                                             uint32_t r_begin, uint32_t r_end) {
  uint32_t R = B->rows, R32 = B->rows32, C = B->cols, G = B->groups4, bpg = B->bytes_per_g;
  const __m128i mask_lo = _mm_set1_epi8(0x0F);
  uint32_t b0 = r_begin & ~31u;
  uint32_t b1 = (r_end + 31u) & ~31u;
  if (b1 > R32) b1 = R32;
  uint32_t r_hi = r_end < R ? r_end : R;
  if (b0 >= r_hi) return;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * C;
    float *yn = Y + (size_t)n * R;
    for (uint32_t g = 0; g < G; g++) {
      uint32_t c0 = g << 2;
      build_lut4_zmm(xn + c0, C - c0, &lut_buf[g]);
    }
    for (uint32_t r0 = b0; r0 < b1; r0 += 64u) {
      uint32_t nb = (b1 - r0 >= 64u) ? 2u : 1u; /* each block = 32 rows = 16 bytes */
      __m512 a0 = _mm512_setzero_ps(), a1 = _mm512_setzero_ps();
      __m512 a2 = _mm512_setzero_ps(), a3 = _mm512_setzero_ps();
      for (uint32_t g = 0; g < G; g++) {
        __m512 lut = lut_buf[g];
        const uint8_t *p = B->nib4_p1b + (size_t)g * bpg + (r0 >> 1);
        __m128i m0 = _mm_loadu_si128((const __m128i *)p);
        __m512i lo0 = _mm512_cvtepu8_epi32(_mm_and_si128(m0, mask_lo));
        __m512i hi0 = _mm512_cvtepu8_epi32(_mm_and_si128(_mm_srli_epi16(m0, 4), mask_lo));
        a0 = _mm512_add_ps(a0, _mm512_permutexvar_ps(lo0, lut));
        a1 = _mm512_add_ps(a1, _mm512_permutexvar_ps(hi0, lut));
        if (nb == 2u) {
          __m128i m1 = _mm_loadu_si128((const __m128i *)(p + 16));
          __m512i lo1 = _mm512_cvtepu8_epi32(_mm_and_si128(m1, mask_lo));
          __m512i hi1 = _mm512_cvtepu8_epi32(_mm_and_si128(_mm_srli_epi16(m1, 4), mask_lo));
          a2 = _mm512_add_ps(a2, _mm512_permutexvar_ps(lo1, lut));
          a3 = _mm512_add_ps(a3, _mm512_permutexvar_ps(hi1, lut));
        }
      }
      _mm512_store_ps(row_acc + (r0 - b0),      a0);
      _mm512_store_ps(row_acc + (r0 - b0) + 16, a1);
      if (nb == 2u) {
        _mm512_store_ps(row_acc + (r0 - b0) + 32, a2);
        _mm512_store_ps(row_acc + (r0 - b0) + 48, a3);
      }
    }
    memcpy(yn + r_begin, row_acc + (r_begin - b0), (size_t)(r_hi - r_begin) * sizeof(float));
  }
}

/* Exact 1.0-bit/weight 2-nibbles-per-byte AVX-512 LUT4 (4-token weight-reusing micro-batch):
 * Loads and unpacks each 16-byte packed weight block ONCE and reuses it across 4 tokens,
 * amortizing both DRAM/cache weight loads and nibble-unpack instructions during prefill. */
static void bin_mm_avx512_lut4_packed1b_mb4_rows(uint32_t N, const float *X, const packed_binmat *B,
                                                 float *Y, __m512 *lut_buf, float *row_acc,
                                                 uint32_t r_begin, uint32_t r_end) {
  uint32_t R = B->rows, R32 = B->rows32, C = B->cols, G = B->groups4, bpg = B->bytes_per_g;
  const __m128i mask_lo = _mm_set1_epi8(0x0F);
  uint32_t b0 = r_begin & ~31u;
  uint32_t b1 = (r_end + 31u) & ~31u;
  if (b1 > R32) b1 = R32;
  uint32_t r_hi = r_end < R ? r_end : R;
  uint32_t span = b1 - b0;
  if (b0 >= r_hi) return;
  uint32_t n = 0;
  for (; n + 4u <= N; n += 4u) {
    for (uint32_t t = 0; t < 4u; t++) {
      const float *xt = X + (size_t)(n + t) * C;
      __m512 *lt = lut_buf + (size_t)t * G;
      for (uint32_t g = 0; g < G; g++) {
        uint32_t c0 = g << 2;
        build_lut4_zmm(xt + c0, C - c0, &lt[g]);
      }
    }
    for (uint32_t r0 = b0; r0 < b1; r0 += 32u) {
      __m512 lo_a0 = _mm512_setzero_ps(), hi_a0 = _mm512_setzero_ps();
      __m512 lo_a1 = _mm512_setzero_ps(), hi_a1 = _mm512_setzero_ps();
      __m512 lo_a2 = _mm512_setzero_ps(), hi_a2 = _mm512_setzero_ps();
      __m512 lo_a3 = _mm512_setzero_ps(), hi_a3 = _mm512_setzero_ps();
      const __m512 *l0 = lut_buf, *l1 = lut_buf + G, *l2 = lut_buf + 2u * G, *l3 = lut_buf + 3u * G;
      for (uint32_t g = 0; g < G; g++) {
        const uint8_t *p = B->nib4_p1b + (size_t)g * bpg + (r0 >> 1);
        __m128i m0 = _mm_loadu_si128((const __m128i *)p);
        __m512i lo = _mm512_cvtepu8_epi32(_mm_and_si128(m0, mask_lo));
        __m512i hi = _mm512_cvtepu8_epi32(_mm_and_si128(_mm_srli_epi16(m0, 4), mask_lo));
        __m512 v0 = l0[g], v1 = l1[g], v2 = l2[g], v3 = l3[g];
        lo_a0 = _mm512_add_ps(lo_a0, _mm512_permutexvar_ps(lo, v0));
        hi_a0 = _mm512_add_ps(hi_a0, _mm512_permutexvar_ps(hi, v0));
        lo_a1 = _mm512_add_ps(lo_a1, _mm512_permutexvar_ps(lo, v1));
        hi_a1 = _mm512_add_ps(hi_a1, _mm512_permutexvar_ps(hi, v1));
        lo_a2 = _mm512_add_ps(lo_a2, _mm512_permutexvar_ps(lo, v2));
        hi_a2 = _mm512_add_ps(hi_a2, _mm512_permutexvar_ps(hi, v2));
        lo_a3 = _mm512_add_ps(lo_a3, _mm512_permutexvar_ps(lo, v3));
        hi_a3 = _mm512_add_ps(hi_a3, _mm512_permutexvar_ps(hi, v3));
      }
      _mm512_store_ps(row_acc + 0u * span + (r0 - b0),      lo_a0);
      _mm512_store_ps(row_acc + 0u * span + (r0 - b0) + 16, hi_a0);
      _mm512_store_ps(row_acc + 1u * span + (r0 - b0),      lo_a1);
      _mm512_store_ps(row_acc + 1u * span + (r0 - b0) + 16, hi_a1);
      _mm512_store_ps(row_acc + 2u * span + (r0 - b0),      lo_a2);
      _mm512_store_ps(row_acc + 2u * span + (r0 - b0) + 16, hi_a2);
      _mm512_store_ps(row_acc + 3u * span + (r0 - b0),      lo_a3);
      _mm512_store_ps(row_acc + 3u * span + (r0 - b0) + 16, hi_a3);
    }
    for (uint32_t t = 0; t < 4u; t++) {
      memcpy(Y + (size_t)(n + t) * R + r_begin, row_acc + (size_t)t * span + (r_begin - b0),
             (size_t)(r_hi - r_begin) * sizeof(float));
    }
  }
  if (n < N) {
    bin_mm_avx512_lut4_packed1b_rows(N - n, X + (size_t)n * C, B, Y + (size_t)n * R,
                                     lut_buf, row_acc, r_begin, r_end);
  }
}

/* AVX-512 FP16-weight streaming matmul: Y[N x dout] = X[N x din] @ W_fp16^T */
static void dense_fp16_stream_avx512_rows(uint32_t N, uint32_t din, uint32_t dout,
                                          const float *X, const uint16_t *W16, float *Y,
                                          uint32_t j0, uint32_t j1) {
  if (j1 > dout) j1 = dout;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * din;
    float *yn = Y + (size_t)n * dout;
    for (uint32_t j = j0; j < j1; j++) {
      const uint16_t *wj = W16 + (size_t)j * din;
      __m512 acc0 = _mm512_setzero_ps(), acc1 = _mm512_setzero_ps();
      uint32_t c = 0;
      for (; c + 32u <= din; c += 32u) {
        __m512 w0 = _mm512_cvtph_ps(_mm256_loadu_si256((const __m256i *)(wj + c)));
        __m512 w1 = _mm512_cvtph_ps(_mm256_loadu_si256((const __m256i *)(wj + c + 16)));
        acc0 = _mm512_fmadd_ps(_mm512_loadu_ps(xn + c), w0, acc0);
        acc1 = _mm512_fmadd_ps(_mm512_loadu_ps(xn + c + 16), w1, acc1);
      }
      float sum = _mm512_reduce_add_ps(_mm512_add_ps(acc0, acc1));
      for (; c < din; c++) sum += xn[c] * (float)(*(const _Float16 *)(wj + c));
      yn[j] = sum;
    }
  }
}

/* AVX-512 BCIR Q8 block-32 symmetric dequant + FMA matmul */
static void dense_q8_avx512_rows(uint32_t N, uint32_t din, uint32_t dout,
                                 const float *X, const int8_t *codes, const float *scales,
                                 float *Y, uint32_t j0, uint32_t j1) {
  uint32_t ng = (din + 31u) / 32u;
  if (j1 > dout) j1 = dout;
  for (uint32_t n = 0; n < N; n++) {
    const float *xn = X + (size_t)n * din;
    float *yn = Y + (size_t)n * dout;
    for (uint32_t j = j0; j < j1; j++) {
      const int8_t *cj = codes + (size_t)j * din;
      const float *sj = scales + (size_t)j * ng;
      __m512 acc = _mm512_setzero_ps();
      uint32_t g = 0;
      for (; (g + 1u) * 32u <= din; g++) {
        uint32_t c = g * 32u;
        __m512 sc = _mm512_set1_ps(sj[g]);
        __m512 w0 = _mm512_cvtepi32_ps(_mm512_cvtepi8_epi32(_mm_loadu_si128((const __m128i *)(cj + c))));
        __m512 w1 = _mm512_cvtepi32_ps(_mm512_cvtepi8_epi32(_mm_loadu_si128((const __m128i *)(cj + c + 16))));
        __m512 p = _mm512_fmadd_ps(_mm512_loadu_ps(xn + c), w0, _mm512_mul_ps(_mm512_loadu_ps(xn + c + 16), w1));
        acc = _mm512_fmadd_ps(p, sc, acc);
      }
      float sum = _mm512_reduce_add_ps(acc);
      for (uint32_t c = g * 32u; c < din; c++) sum += xn[c] * (float)cj[c] * sj[c >> 5];
      yn[j] = sum;
    }
  }
}

/* Thread-pool task wrappers so 1T and MT run every kernel on the exact same CPU budget */
typedef struct lb_task_ctx {
  int kind;
  uint32_t N, din, dout, r, paths;
  const float *X;
  const packed_binmat *V, *U; /* [paths] */
  const float *g[2], *l[2], *h[2];
  float *Y, *xg, *z, *y0;
  /* For dense FP16 / Q8 */
  const uint16_t *W16;
  const int8_t *q8c;
  const float *q8s;
  uint32_t threads;
  /* Per-task scratch slices */
  __m512 *lut_bufs;
  float *row_accs;
  size_t lut_stride;
  size_t acc_stride;
  /* For row-parallel single/small-batch execution */
  uint32_t cur_path;
  int stage; /* 0 = V projection, 1 = U projection */
} lb_task_ctx;

static void dispatch_bin_mm(int kind, uint32_t N, const float *X, const packed_binmat *B,
                            float *Y, __m512 *lut_buf, float *row_acc,
                            uint32_t r0, uint32_t r1) {
  if (kind == LB_KERN_PORTABLE_BITPACK)
    bin_mm_portable_rows(N, X, B, Y, r0, r1);
  else if (kind == LB_KERN_AVX512_SIGNXOR)
    bin_mm_avx512_signxor_rows(N, X, B, Y, r0, r1);
  else if (kind == LB_KERN_AVX512_LUT4_PACKED1B)
    bin_mm_avx512_lut4_packed1b_rows(N, X, B, Y, lut_buf, row_acc, r0, r1);
  else if (kind == LB_KERN_AVX512_LUT4_PACKED1B_MB4)
    bin_mm_avx512_lut4_packed1b_mb4_rows(N, X, B, Y, lut_buf, row_acc, r0, r1);
  else
    bin_mm_avx512_lut4_rows(N, X, B, Y, lut_buf, row_acc, r0, r1);
}

static void run_lb_token_range(lb_task_ctx *ctx, uint32_t n0, uint32_t n1,
                               __m512 *lut_buf, float *row_acc) {
  uint32_t Nsub = n1 - n0, din = ctx->din, dout = ctx->dout, r = ctx->r;
  if (Nsub == 0u) return;
  const float *X = ctx->X + (size_t)n0 * din;
  float *Y = ctx->Y + (size_t)n0 * dout;
  if (ctx->kind == LB_KERN_DENSE_FP16_STREAM) {
    dense_fp16_stream_avx512_rows(Nsub, din, dout, X, ctx->W16, Y, 0u, dout);
    return;
  }
  if (ctx->kind == LB_KERN_DENSE_Q8_BCIR) {
    dense_q8_avx512_rows(Nsub, din, dout, X, ctx->q8c, ctx->q8s, Y, 0u, dout);
    return;
  }
  float *xg = ctx->xg + (size_t)n0 * din;
  float *z  = ctx->z  + (size_t)n0 * r;
  float *y0 = ctx->y0 + (size_t)n0 * dout;
  for (uint32_t p = 0; p < ctx->paths; p++) {
    for (uint32_t i = 0; i < Nsub; i++)
      for (uint32_t c = 0; c < din; c++) xg[(size_t)i * din + c] = X[(size_t)i * din + c] * ctx->g[p][c];
    dispatch_bin_mm(ctx->kind, Nsub, xg, &ctx->V[p], z, lut_buf, row_acc, 0u, r);
    for (uint32_t i = 0; i < Nsub; i++)
      for (uint32_t k = 0; k < r; k++) z[(size_t)i * r + k] *= ctx->l[p][k];
    dispatch_bin_mm(ctx->kind, Nsub, z, &ctx->U[p], y0, lut_buf, row_acc, 0u, dout);
    if (p == 0u) {
      for (uint32_t i = 0; i < Nsub; i++)
        for (uint32_t j = 0; j < dout; j++) Y[(size_t)i * dout + j] = y0[(size_t)i * dout + j] * ctx->h[p][j];
    } else {
      for (uint32_t i = 0; i < Nsub; i++)
        for (uint32_t j = 0; j < dout; j++) Y[(size_t)i * dout + j] += y0[(size_t)i * dout + j] * ctx->h[p][j];
    }
  }
}

static void lb_tok_pool_worker(void *vctx, size_t task) {
  lb_task_ctx *ctx = (lb_task_ctx *)vctx;
  uint32_t nt = ctx->threads ? ctx->threads : 1u;
  uint32_t n0 = (uint32_t)((size_t)ctx->N * task / nt);
  uint32_t n1 = (uint32_t)((size_t)ctx->N * (task + 1u) / nt);
  __m512 *lb = ctx->lut_bufs ? (ctx->lut_bufs + task * ctx->lut_stride) : NULL;
  float *ra = ctx->row_accs ? (ctx->row_accs + task * ctx->acc_stride) : NULL;
  run_lb_token_range(ctx, n0, n1, lb, ra);
}

static void lb_row_pool_worker(void *vctx, size_t task) {
  lb_task_ctx *ctx = (lb_task_ctx *)vctx;
  uint32_t nt = ctx->threads ? ctx->threads : 1u;
  if (ctx->kind == LB_KERN_DENSE_FP16_STREAM) {
    uint32_t j0 = (uint32_t)((size_t)ctx->dout * task / nt);
    uint32_t j1 = (uint32_t)((size_t)ctx->dout * (task + 1u) / nt);
    dense_fp16_stream_avx512_rows(ctx->N, ctx->din, ctx->dout, ctx->X, ctx->W16, ctx->Y, j0, j1);
    return;
  }
  if (ctx->kind == LB_KERN_DENSE_Q8_BCIR) {
    uint32_t j0 = (uint32_t)((size_t)ctx->dout * task / nt);
    uint32_t j1 = (uint32_t)((size_t)ctx->dout * (task + 1u) / nt);
    dense_q8_avx512_rows(ctx->N, ctx->din, ctx->dout, ctx->X, ctx->q8c, ctx->q8s, ctx->Y, j0, j1);
    return;
  }
  uint32_t p = ctx->cur_path;
  __m512 *lb = ctx->lut_bufs + task * ctx->lut_stride;
  float *ra = ctx->row_accs + task * ctx->acc_stride;
  uint32_t align = (ctx->kind == LB_KERN_AVX512_LUT4_PACKED1B ||
                    ctx->kind == LB_KERN_AVX512_LUT4_PACKED1B_MB4) ? 32u : 16u;
  uint32_t total = (ctx->stage == 0) ? ctx->r : ctx->dout;
  uint32_t blocks = (total + align - 1u) / align;
  uint32_t b0 = (uint32_t)((size_t)blocks * task / nt) * align;
  uint32_t b1 = (uint32_t)((size_t)blocks * (task + 1u) / nt) * align;
  if (b0 >= total) return;
  if (b1 > total) b1 = total;
  if (ctx->stage == 0) {
    dispatch_bin_mm(ctx->kind, ctx->N, ctx->xg, &ctx->V[p], ctx->z, lb, ra, b0, b1);
  } else {
    dispatch_bin_mm(ctx->kind, ctx->N, ctx->z, &ctx->U[p], ctx->y0, lb, ra, b0, b1);
    for (uint32_t i = 0; i < ctx->N; i++) {
      float *yn = ctx->Y + (size_t)i * ctx->dout;
      const float *y0n = ctx->y0 + (size_t)i * ctx->dout;
      if (p == 0u) {
        for (uint32_t j = b0; j < b1; j++) yn[j] = y0n[j] * ctx->h[p][j];
      } else {
        for (uint32_t j = b0; j < b1; j++) yn[j] += y0n[j] * ctx->h[p][j];
      }
    }
  }
}

static void run_lb_dispatch(bcir_tensor_pool *pool, lb_task_ctx *ctx) {
  if (!pool || ctx->threads <= 1u) {
    run_lb_token_range(ctx, 0u, ctx->N, ctx->lut_bufs, ctx->row_accs);
    return;
  }
  if (ctx->N >= ctx->threads) {
    bcir_tensor_pool_run(pool, lb_tok_pool_worker, ctx, ctx->threads);
    return;
  }
  if (ctx->kind == LB_KERN_DENSE_FP16_STREAM || ctx->kind == LB_KERN_DENSE_Q8_BCIR) {
    bcir_tensor_pool_run(pool, lb_row_pool_worker, ctx, ctx->threads);
    return;
  }
  for (uint32_t p = 0; p < ctx->paths; p++) {
    ctx->cur_path = p;
    for (uint32_t i = 0; i < ctx->N; i++)
      for (uint32_t c = 0; c < ctx->din; c++)
        ctx->xg[(size_t)i * ctx->din + c] = ctx->X[(size_t)i * ctx->din + c] * ctx->g[p][c];
    ctx->stage = 0;
    bcir_tensor_pool_run(pool, lb_row_pool_worker, ctx, ctx->threads);
    for (uint32_t i = 0; i < ctx->N; i++)
      for (uint32_t k = 0; k < ctx->r; k++) ctx->z[(size_t)i * ctx->r + k] *= ctx->l[p][k];
    ctx->stage = 1;
    bcir_tensor_pool_run(pool, lb_row_pool_worker, ctx, ctx->threads);
  }
}

int lb_bench_linear_kernel(int kernel_kind, uint32_t N, uint32_t din, uint32_t dout,
                           uint32_t r, int residual, uint32_t threads, uint32_t iters,
                           lb_bench_result *out) {
  if (!out || !N || !din || !dout || !iters) return -1;
  uint32_t rng = 0x12345678u ^ (din * 131u) ^ (dout * 977u) ^ (r * 31u);
  float *X = (float *)aligned64((size_t)N * din * sizeof(float));
  float *Y = (float *)aligned64((size_t)N * dout * sizeof(float));
  float *Yref = (float *)aligned64((size_t)N * dout * sizeof(float));
  for (size_t i = 0; i < (size_t)N * din; i++) X[i] = ((float)(xorshift32(&rng) & 1023u) / 512.0f - 1.0f) * 0.5f;

  void *pool_mem = NULL;
  bcir_tensor_pool *pool = NULL;
  if (threads > 1u) {
    size_t psz = bcir_tensor_pool_size(threads);
    if (psz) { pool_mem = aligned64(psz); pool = bcir_tensor_pool_init(pool_mem, psz, threads); }
  }
  uint32_t eff_threads = pool ? threads : 1u;

  double *times = (double *)malloc(iters * sizeof(double));
  size_t wbytes = 0u, ideal_bytes = 0u;
  double max_err = 0.0;

  if (kernel_kind == LB_KERN_DENSE_FP32_BCIR || kernel_kind == LB_KERN_DENSE_BF16_BCIR) {
    float *W = (float *)aligned64((size_t)dout * din * sizeof(float));
    for (size_t i = 0; i < (size_t)dout * din; i++) W[i] = ((float)(xorshift32(&rng) & 1023u) / 512.0f - 1.0f) * 0.05f;
    void *ws = NULL; size_t ws_bytes = 0;
    if (kernel_kind == LB_KERN_DENSE_BF16_BCIR) {
      ws_bytes = bcir_tensor_mm_bf16_workspace(N, dout, din);
      ws = aligned64(ws_bytes);
      wbytes = (size_t)dout * din * 4u; /* timed API reads FP32 W & converts to BF16 in ws */
      ideal_bytes = (size_t)dout * din * 2u;
      bcir_tensor_mm_bf16_pool(pool, 0, 1, N, dout, din, 1.0f, X, W, 0.0f, Y, ws, ws_bytes);
      for (uint32_t it = 0; it < iters; it++) {
        double t0 = now_us();
        bcir_tensor_mm_bf16_pool(pool, 0, 1, N, dout, din, 1.0f, X, W, 0.0f, Y, ws, ws_bytes);
        times[it] = now_us() - t0;
      }
      free(ws);
    } else {
      wbytes = (size_t)dout * din * 4u;
      ideal_bytes = wbytes;
      bcir_tensor_mm_kblock_pool(pool, 0, 1, N, dout, din, 1.0f, X, W, 0.0f, Y);
      for (uint32_t it = 0; it < iters; it++) {
        double t0 = now_us();
        bcir_tensor_mm_kblock_pool(pool, 0, 1, N, dout, din, 1.0f, X, W, 0.0f, Y);
        times[it] = now_us() - t0;
      }
    }
    free(W);
  } else if (kernel_kind == LB_KERN_LOWRANK_FP32) {
    uint32_t rtot = residual ? 2u * r : r;
    float *V = (float *)aligned64((size_t)din * rtot * sizeof(float));
    float *U = (float *)aligned64((size_t)dout * rtot * sizeof(float));
    float *Z = (float *)aligned64((size_t)N * rtot * sizeof(float));
    for (size_t i = 0; i < (size_t)din * rtot; i++) V[i] = ((float)(xorshift32(&rng) & 1023u) / 512.0f - 1.0f) * 0.05f;
    for (size_t i = 0; i < (size_t)dout * rtot; i++) U[i] = ((float)(xorshift32(&rng) & 1023u) / 512.0f - 1.0f) * 0.05f;
    wbytes = (size_t)rtot * (din + dout) * 4u;
    ideal_bytes = wbytes;
    bcir_tensor_mm_kblock_pool(pool, 0, 0, N, rtot, din, 1.0f, X, V, 0.0f, Z);
    bcir_tensor_mm_kblock_pool(pool, 0, 1, N, dout, rtot, 1.0f, Z, U, 0.0f, Y);
    for (uint32_t it = 0; it < iters; it++) {
      double t0 = now_us();
      bcir_tensor_mm_kblock_pool(pool, 0, 0, N, rtot, din, 1.0f, X, V, 0.0f, Z);
      bcir_tensor_mm_kblock_pool(pool, 0, 1, N, dout, rtot, 1.0f, Z, U, 0.0f, Y);
      times[it] = now_us() - t0;
    }
    free(V); free(U); free(Z);
  } else if (kernel_kind == LB_KERN_DENSE_FP16_STREAM) {
    uint16_t *W16 = (uint16_t *)aligned64((size_t)dout * din * 2u);
    float *Wf = (float *)aligned64((size_t)dout * din * 4u);
    for (size_t i = 0; i < (size_t)dout * din; i++) {
      _Float16 h = (_Float16)(((float)(xorshift32(&rng) & 1023u) / 512.0f - 1.0f) * 0.05f);
      memcpy(&W16[i], &h, 2); Wf[i] = (float)h;
    }
    wbytes = (size_t)dout * din * 2u;
    ideal_bytes = wbytes;
    lb_task_ctx ctx; memset(&ctx, 0, sizeof(ctx));
    ctx.kind = kernel_kind; ctx.N = N; ctx.din = din; ctx.dout = dout;
    ctx.X = X; ctx.Y = Y; ctx.W16 = W16; ctx.threads = eff_threads;
    run_lb_dispatch(pool, &ctx);
    bcir_tensor_mm_kblock_pool(NULL, 0, 1, N, dout, din, 1.0f, X, Wf, 0.0f, Yref);
    for (size_t i = 0; i < (size_t)N * dout; i++) {
      double e = fabs((double)Y[i] - (double)Yref[i]);
      if (e > max_err) max_err = e;
    }
    for (uint32_t it = 0; it < iters; it++) {
      double t0 = now_us();
      run_lb_dispatch(pool, &ctx);
      times[it] = now_us() - t0;
    }
    free(W16); free(Wf);
  } else if (kernel_kind == LB_KERN_DENSE_Q8_BCIR) {
    uint32_t ng = (din + 31u) / 32u;
    int8_t *codes = (int8_t *)aligned64((size_t)dout * din);
    float *scales = (float *)aligned64((size_t)dout * ng * 4u);
    float *Wf = (float *)aligned64((size_t)dout * din * 4u);
    for (uint32_t j = 0; j < dout; j++) {
      for (uint32_t g = 0; g < ng; g++) scales[(size_t)j * ng + g] = ldexpf(1.0f, -10);
      for (uint32_t c = 0; c < din; c++) {
        int8_t q = (int8_t)((int)(xorshift32(&rng) % 255u) - 127);
        codes[(size_t)j * din + c] = q;
        Wf[(size_t)j * din + c] = (float)q * scales[(size_t)j * ng + (c >> 5)];
      }
    }
    /* Actual timed kernel reads 1B int8 codes + 4B FP32 per-group scales; BCIRQ8 container stores 2B int16 exp */
    wbytes = (size_t)dout * din + (size_t)dout * ng * sizeof(float);
    ideal_bytes = (size_t)dout * din + (size_t)dout * ng * 2u;
    lb_task_ctx ctx; memset(&ctx, 0, sizeof(ctx));
    ctx.kind = kernel_kind; ctx.N = N; ctx.din = din; ctx.dout = dout;
    ctx.X = X; ctx.Y = Y; ctx.q8c = codes; ctx.q8s = scales; ctx.threads = eff_threads;
    run_lb_dispatch(pool, &ctx);
    bcir_tensor_mm_kblock_pool(NULL, 0, 1, N, dout, din, 1.0f, X, Wf, 0.0f, Yref);
    for (size_t i = 0; i < (size_t)N * dout; i++) {
      double e = fabs((double)Y[i] - (double)Yref[i]);
      if (e > max_err) max_err = e;
    }
    for (uint32_t it = 0; it < iters; it++) {
      double t0 = now_us();
      run_lb_dispatch(pool, &ctx);
      times[it] = now_us() - t0;
    }
    free(codes); free(scales); free(Wf);
  } else {
    /* LittleBit bit-packed binary kernels (1-path or 2-path Residual) */
    uint32_t paths = residual ? 2u : 1u;
    packed_binmat Vp[2], Up[2];
    float *gv[2], *lv[2], *hv[2];
    size_t actual_mat_bytes = 0u;
    for (uint32_t p = 0; p < paths; p++) {
      Vp[p] = make_packed_binmat(r, din, &rng);     /* [r x din] so X @ V^T = [N x r] */
      Up[p] = make_packed_binmat(dout, r, &rng);    /* [dout x r] so Z @ U^T = [N x dout] */
      gv[p] = (float *)aligned64(din * 4u);
      lv[p] = (float *)aligned64(r * 4u);
      hv[p] = (float *)aligned64(dout * 4u);
      for (uint32_t i = 0; i < din; i++) gv[p][i] = 0.25f + 0.05f * (float)(i & 7u);
      for (uint32_t i = 0; i < r; i++)   lv[p][i] = 0.05f / (float)(i + 1u);
      for (uint32_t i = 0; i < dout; i++) hv[p][i] = 0.25f + 0.05f * (float)(i & 7u);
      if (kernel_kind == LB_KERN_AVX512_LUT4_PACKED1B || kernel_kind == LB_KERN_AVX512_LUT4_PACKED1B_MB4) {
        actual_mat_bytes += (size_t)Vp[p].groups4 * Vp[p].bytes_per_g + (size_t)Up[p].groups4 * Up[p].bytes_per_g;
      } else if (kernel_kind == LB_KERN_AVX512_LUT4) {
        actual_mat_bytes += (size_t)Vp[p].groups4 * Vp[p].rows16 + (size_t)Up[p].groups4 * Up[p].rows16;
      } else {
        actual_mat_bytes += (size_t)Vp[p].rows * Vp[p].words_per_row * sizeof(uint64_t) +
                            (size_t)Up[p].rows * Up[p].words_per_row * sizeof(uint64_t);
      }
    }
    /* Actual representation read by timed kernel (padded binary tables + FP32 g/l/h scales) */
    wbytes = actual_mat_bytes + (size_t)paths * sizeof(float) * ((size_t)din + r + dout);
    /* Idealized unpadded deployment storage bound (1-bit U,V + FP16 h,g,l) */
    size_t bits_total = (size_t)paths * ((size_t)r * (din + dout) + 16u * (din + dout + r));
    ideal_bytes = (bits_total + 7u) / 8u;

    float *xg = (float *)aligned64((size_t)N * din * 4u);
    float *z  = (float *)aligned64((size_t)N * r * 4u);
    float *y0 = (float *)aligned64((size_t)N * dout * 4u);
    uint32_t max_g4 = ((din > r ? din : r) + 3u) / 4u;
    uint32_t max_r32 = ((dout > r ? dout : r) + 31u) & ~31u;
    size_t lut_stride = (size_t)4u * max_g4;
    size_t acc_stride = (size_t)4u * max_r32;
    __m512 *lut_bufs = (__m512 *)aligned64((size_t)eff_threads * lut_stride * sizeof(__m512));
    float *row_accs = (float *)aligned64((size_t)eff_threads * acc_stride * sizeof(float));

    /* Compute exact FP32 reference Yref */
    memset(Yref, 0, (size_t)N * dout * 4u);
    for (uint32_t p = 0; p < paths; p++) {
      for (uint32_t i = 0; i < N; i++)
        for (uint32_t c = 0; c < din; c++) xg[(size_t)i * din + c] = X[(size_t)i * din + c] * gv[p][c];
      bcir_tensor_mm_kblock_pool(NULL, 0, 1, N, r, din, 1.0f, xg, Vp[p].dense_pm1, 0.0f, z);
      for (uint32_t i = 0; i < N; i++)
        for (uint32_t k = 0; k < r; k++) z[(size_t)i * r + k] *= lv[p][k];
      bcir_tensor_mm_kblock_pool(NULL, 0, 1, N, dout, r, 1.0f, z, Up[p].dense_pm1, 0.0f, y0);
      for (uint32_t i = 0; i < N; i++)
        for (uint32_t j = 0; j < dout; j++) Yref[(size_t)i * dout + j] += y0[(size_t)i * dout + j] * hv[p][j];
    }

    lb_task_ctx ctx; memset(&ctx, 0, sizeof(ctx));
    ctx.kind = kernel_kind; ctx.N = N; ctx.din = din; ctx.dout = dout; ctx.r = r; ctx.paths = paths;
    ctx.X = X; ctx.Y = Y; ctx.xg = xg; ctx.z = z; ctx.y0 = y0; ctx.V = Vp; ctx.U = Up;
    ctx.threads = eff_threads; ctx.lut_bufs = lut_bufs; ctx.row_accs = row_accs;
    ctx.lut_stride = lut_stride; ctx.acc_stride = acc_stride;
    for (uint32_t p = 0; p < paths; p++) { ctx.g[p] = gv[p]; ctx.l[p] = lv[p]; ctx.h[p] = hv[p]; }

    /* Warmup + numerical correspondence check */
    run_lb_dispatch(pool, &ctx);
    for (size_t i = 0; i < (size_t)N * dout; i++) {
      double e = fabs((double)Y[i] - (double)Yref[i]);
      if (e > max_err) max_err = e;
    }
    for (uint32_t it = 0; it < iters; it++) {
      double t0 = now_us();
      run_lb_dispatch(pool, &ctx);
      times[it] = now_us() - t0;
    }
    free(xg); free(z); free(y0); free(lut_bufs); free(row_accs);
    for (uint32_t p = 0; p < paths; p++) {
      free_packed_binmat(&Vp[p]); free_packed_binmat(&Up[p]);
      free(gv[p]); free(lv[p]); free(hv[p]);
    }
  }

  qsort(times, iters, sizeof(double), cmp_double);
  out->median_us = times[iters / 2];
  out->min_us = times[0];
  out->gflops_equiv = (2.0 * (double)N * (double)din * (double)dout) / (out->median_us * 1e3);
  out->max_abs_err = max_err;
  out->weight_bytes = wbytes;
  out->realized_bpw = 8.0 * (double)wbytes / ((double)din * (double)dout);
  out->ideal_deploy_weight_bytes = ideal_bytes;
  out->ideal_deploy_bpw = 8.0 * (double)ideal_bytes / ((double)din * (double)dout);

  free(times); free(X); free(Y); free(Yref);
  if (pool) bcir_tensor_pool_stop(pool);
  free(pool_mem);
  return 0;
}
#else
int lb_bench_linear_kernel(int kernel_kind, uint32_t N, uint32_t din, uint32_t dout,
                           uint32_t r, int residual, uint32_t threads, uint32_t iters,
                           lb_bench_result *out) {
  (void)kernel_kind; (void)N; (void)din; (void)dout;
  (void)r; (void)residual; (void)threads; (void)iters;
  if (out) {
    out->median_us = 0.0; out->min_us = 0.0; out->gflops_equiv = 0.0;
    out->max_abs_err = 0.0; out->weight_bytes = 0u; out->realized_bpw = 0.0;
    out->ideal_deploy_weight_bytes = 0u; out->ideal_deploy_bpw = 0.0;
  }
  return -1;
}
#endif
