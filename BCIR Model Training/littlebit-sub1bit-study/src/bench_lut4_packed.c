/* Standalone test of exact 1.0-bit/weight 2-nibbles-per-byte AVX-512 LUT4 kernel
 * for BCIR COMP-1 deployment spec: halves weight DRAM/L3 traffic vs 1-byte/nibble
 * while keeping vpermps 64-binary-MAC/2-instruction compute rate!
 */
#include <immintrin.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec * 1e-3;
}
static int cmp_d(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return (x > y) - (x < y);
}
static void *a64(size_t b) {
  size_t n = (b + 63u) & ~(size_t)63u;
  void *p = aligned_alloc(64, n ? n : 64);
  memset(p, 0, n ? n : 64);
  return p;
}
static uint32_t rng_u32(uint32_t *s) { uint32_t x = *s; x ^= x << 13; x ^= x >> 17; x ^= x << 5; *s = x; return x; }

static inline void build_lut4(const float *x, __m512 *lut) {
  float x0 = x[0], x1 = x[1], x2 = x[2], x3 = x[3];
  float a = x0 + x1, b = -x0 + x1, c = x0 - x1, d = -x0 - x1;
  float e = x2 + x3, f = -x2 + x3, g = x2 - x3, h = -x2 - x3;
  float t[16] __attribute__((aligned(64))) = {
    a+e, b+e, c+e, d+e, a+f, b+f, c+f, d+f,
    a+g, b+g, c+g, d+g, a+h, b+h, c+h, d+h
  };
  *lut = _mm512_load_ps(t);
}

/* Packed 1.0 bit/weight layout: nibbles for rows [r0..r0+15] in low nibbles,
 * rows [r0+16..r0+31] in high nibbles of the SAME 16 bytes! */
static void bin_mm_lut4_packed1b(uint32_t R, uint32_t C, const float *X,
                                 const uint8_t *packed, float *Y, __m512 *luts) {
  uint32_t G = C / 4u;
  uint32_t R32 = (R + 31u) & ~31u;
  uint32_t bytes_per_g = R32 / 2u; /* EXACTLY 1 bit per weight! */
  for (uint32_t g = 0; g < G; g++) build_lut4(X + g * 4u, &luts[g]);
  const __m128i mask_lo = _mm_set1_epi8(0x0F);
  for (uint32_t r0 = 0; r0 < R32; r0 += 64u) {
    uint32_t nb = (R32 - r0 >= 64u) ? 2u : 1u; /* each block = 32 rows = 16 bytes */
    __m512 a0 = _mm512_setzero_ps(), a1 = _mm512_setzero_ps();
    __m512 a2 = _mm512_setzero_ps(), a3 = _mm512_setzero_ps();
    for (uint32_t g = 0; g < G; g++) {
      __m512 lut = luts[g];
      const uint8_t *p = packed + (size_t)g * bytes_per_g + (r0 >> 1);
      __m128i b0 = _mm_loadu_si128((const __m128i *)p);
      __m512i lo0 = _mm512_cvtepu8_epi32(_mm_and_si128(b0, mask_lo));
      __m512i hi0 = _mm512_cvtepu8_epi32(_mm_and_si128(_mm_srli_epi16(b0, 4), mask_lo));
      a0 = _mm512_add_ps(a0, _mm512_permutexvar_ps(lo0, lut));
      a1 = _mm512_add_ps(a1, _mm512_permutexvar_ps(hi0, lut));
      if (nb == 2u) {
        __m128i b1 = _mm_loadu_si128((const __m128i *)(p + 16));
        __m512i lo1 = _mm512_cvtepu8_epi32(_mm_and_si128(b1, mask_lo));
        __m512i hi1 = _mm512_cvtepu8_epi32(_mm_and_si128(_mm_srli_epi16(b1, 4), mask_lo));
        a2 = _mm512_add_ps(a2, _mm512_permutexvar_ps(lo1, lut));
        a3 = _mm512_add_ps(a3, _mm512_permutexvar_ps(hi1, lut));
      }
    }
    _mm512_store_ps(Y + r0, a0);
    _mm512_store_ps(Y + r0 + 16, a1);
    if (nb == 2u) {
      _mm512_store_ps(Y + r0 + 32, a2);
      _mm512_store_ps(Y + r0 + 48, a3);
    }
  }
}

static void bench_one(const char *label, uint32_t din, uint32_t dout, uint32_t r, int iters) {
  uint32_t st = 0xCAFEBABEu ^ din ^ dout ^ r;
  uint32_t r32 = (r + 31u) & ~31u, do32 = (dout + 31u) & ~31u;
  float *X = (float *)a64(din * 4), *xg = (float *)a64(din * 4);
  float *Z = (float *)a64(r32 * 4), *Y = (float *)a64(do32 * 4), *Ytot = (float *)a64(do32 * 4);
  float *g[2], *l[2], *h[2];
  uint8_t *Vp[2], *Up[2];
  uint32_t r_pad4 = (r + 3u) & ~3u;
  for (int p = 0; p < 2; p++) {
    g[p] = (float *)a64(din * 4); l[p] = (float *)a64(r_pad4 * 4); h[p] = (float *)a64(dout * 4);
    for (uint32_t i = 0; i < din; i++) g[p][i] = 0.3f;
    for (uint32_t i = 0; i < r; i++)   l[p][i] = 0.05f;
    for (uint32_t i = 0; i < dout; i++) h[p][i] = 0.3f;
    Vp[p] = (uint8_t *)a64((size_t)(din / 4u) * (r32 / 2u));
    Up[p] = (uint8_t *)a64((size_t)(r_pad4 / 4u) * (do32 / 2u));
    for (size_t i = 0; i < (size_t)(din / 4u) * (r32 / 2u); i++) Vp[p][i] = (uint8_t)rng_u32(&st);
    for (size_t i = 0; i < (size_t)(r_pad4 / 4u) * (do32 / 2u); i++) Up[p][i] = (uint8_t)rng_u32(&st);
  }
  for (uint32_t i = 0; i < din; i++) X[i] = ((float)(rng_u32(&st) & 255u) / 128.0f - 1.0f);
  __m512 *luts = (__m512 *)a64((size_t)((din > r_pad4 ? din : r_pad4) / 4u) * sizeof(__m512));

  double *ts = (double *)malloc((size_t)iters * sizeof(double));
  for (int it = -2; it < iters; it++) {
    double t0 = now_us();
    for (int p = 0; p < 2; p++) {
      for (uint32_t i = 0; i < din; i++) xg[i] = X[i] * g[p][i];
      bin_mm_lut4_packed1b(r, din, xg, Vp[p], Z, luts);
      for (uint32_t i = 0; i < r; i++) Z[i] *= l[p][i];
      for (uint32_t i = r; i < r_pad4; i++) Z[i] = 0.0f;
      bin_mm_lut4_packed1b(dout, r_pad4, Z, Up[p], Y, luts);
      if (p == 0) for (uint32_t i = 0; i < dout; i++) Ytot[i] = Y[i] * h[p][i];
      else        for (uint32_t i = 0; i < dout; i++) Ytot[i] += Y[i] * h[p][i];
    }
    double dt = now_us() - t0;
    if (it >= 0) ts[it] = dt;
  }
  qsort(ts, (size_t)iters, sizeof(double), cmp_d);
  size_t exact_bytes = 2 * ((size_t)(din / 4u) * (r32 / 2u) + (size_t)(r_pad4 / 4u) * (do32 / 2u) + 2u * (din + dout + r));
  printf("%-20s %4ux%-5u r=%4u | packed1b_LUT4=%7.1fus (min=%7.1fus) | exact_bytes=%8zu (bpw=%.3f) chk=%.2f\n",
         label, din, dout, r, ts[iters / 2], ts[0], exact_bytes, 8.0 * exact_bytes / ((double)din * dout), Ytot[0]);
  free(ts); free(X); free(xg); free(Z); free(Y); free(Ytot); free(luts);
  for (int p = 0; p < 2; p++) { free(g[p]); free(l[p]); free(h[p]); free(Vp[p]); free(Up[p]); }
}

int main(void) {
  bench_one("wide50m_0.30",    768,  2048,   67, 25);
  bench_one("llama2_7b_0.55", 4096, 11008,  804, 15);
  bench_one("llama2_7b_0.30", 4096, 11008,  431, 15);
  bench_one("llama2_7b_0.10", 4096, 11008,  133, 15);
  bench_one("llama2_70b_0.55",8192, 28672, 1735,  9);
  bench_one("llama2_70b_0.30",8192, 28672,  939,  9);
  bench_one("llama2_70b_0.10",8192, 28672,  302,  9);
  return 0;
}
