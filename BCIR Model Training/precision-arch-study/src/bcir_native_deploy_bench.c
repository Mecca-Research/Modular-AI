/*===- bcir_native_deploy_bench.c - production BCIRQ8 v1/v2 evaluator & bench ===
 *
 * Linked directly against BCIR's production runtime/c sources:
 *   bcir_q8_model.c, bcir_decode.c, bcir_ai_kernels.c, bcir_llama.c, bcir_tensor.c
 *
 * Provides three JSON-emitting subcommands for P1-P6 deployment qualification:
 *   --eval-lm <model.bcirq8> <tokens.u32> <targets.u32> <n_seqs> <seq_len>
 *             [--threads N] [--preds-out <preds.i32>]
 *   --bench-decode <model.bcirq8> <ctx_len> <decode_steps> [--threads N]
 *   --bench-kernels
 *===------------------------------------------------------------------------===*/
#define _POSIX_C_SOURCE 200809L

#include "bcir_ai_kernels.h"
#include "bcir_llama.h"
#include "bcir_llama_ops.h"
#include "bcir_q8_model.h"
#include "bcir_tensor.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define BENCH_MAX_MODEL_BYTES (512ull * 1024ull * 1024ull)
#define BENCH_IGNORE_TARGET 0xFFFFFFFFu

static double now_sec(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static int cmp_double(const void *a, const void *b) {
  double da = *(const double *)a, db = *(const double *)b;
  return (da > db) - (da < db);
}

static double median_of(double *arr, size_t n) {
  if (!n) return 0.0;
  qsort(arr, n, sizeof *arr, cmp_double);
  return (n & 1u) ? arr[n / 2u] : 0.5 * (arr[n / 2u - 1u] + arr[n / 2u]);
}

static void pool_run_adapter(void *self, bcir_llama_task_fn task, void *ctx, size_t tasks) {
  bcir_tensor_pool_run((bcir_tensor_pool *)self, task, ctx, tasks);
}

static int make_runner(size_t threads, void **pool_mem_out, bcir_tensor_pool **pool_out,
                       bcir_llama_runner *runner_out) {
  memset(runner_out, 0, sizeof *runner_out);
  *pool_mem_out = NULL;
  *pool_out = NULL;
  if (threads <= 1u) return 0;
  size_t pool_bytes = bcir_tensor_pool_size(threads);
  void *mem = malloc(pool_bytes);
  if (!mem) return -1;
  bcir_tensor_pool *pool = bcir_tensor_pool_init(mem, pool_bytes, threads);
  if (!pool) {
    free(mem);
    return -1;
  }
  *pool_mem_out = mem;
  *pool_out = pool;
  runner_out->run = pool_run_adapter;
  runner_out->self = pool;
  runner_out->workers = threads;
  runner_out->split_min = BCIR_LLAMA_SPLIT_MIN;
  runner_out->head_split_min = BCIR_LLAMA_HEAD_SPLIT_MIN;
  return 0;
}

static void destroy_runner(void *pool_mem, bcir_tensor_pool *pool) {
  if (pool) bcir_tensor_pool_stop(pool);
  free(pool_mem);
}

/* Exact production token step via bcir_llama_ops.h (matches bcir_llama.c bit-for-bit). */
static int step_one_token(const bcir_q8_model *m, bcir_llama_ws *w, int32_t token,
                          size_t pos, double *final_row) {
  uint32_t d = m->d_model, dk = d / m->n_heads, kvd = m->n_kv_heads * dk, layer;
  if (bcir_llama_op_embed(m, w, token)) return -1;
  for (layer = 0; layer < m->n_layers; ++layer) {
    int16_t li = (int16_t)layer;
    bcir_llama_op_rmsnorm(m, w, w->x, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_G_ATTN, li), w->h);
    if (bcir_llama_op_matvec(m, w, w->h, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_Q, li), d, d, w->q) ||
        bcir_llama_op_matvec(m, w, w->h, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_K, li), d, kvd, w->k) ||
        bcir_llama_op_matvec(m, w, w->h, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_V, li), d, kvd, w->v))
      return -1;
    bcir_llama_op_rope(m, w, pos);
    bcir_llama_op_kv_append(m, w, layer, pos);
    if (bcir_llama_op_attention(m, w, layer, pos)) return -1;
    if (bcir_llama_op_matvec(m, w, w->context, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_O, li),
                             d, d, w->attn))
      return -1;
    bcir_llama_op_residual(m, w);
    bcir_llama_op_rmsnorm(m, w, w->x, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_G_FF, li), w->h2);
    if (bcir_llama_op_matvec(m, w, w->h2, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_GATE, li),
                             d, m->d_ff, w->gate) ||
        bcir_llama_op_matvec(m, w, w->h2, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_UP, li),
                             d, m->d_ff, w->up))
      return -1;
    bcir_llama_op_swiglu(m, w);
    if (bcir_llama_op_matvec(m, w, w->ff, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_W_DOWN, li),
                             m->d_ff, d, w->attn))
      return -1;
    bcir_llama_op_residual(m, w);
  }
  bcir_llama_op_rmsnorm(m, w, w->x, bcir_q8_model_tensor(m, BCIR_Q8_TENSOR_G_FINAL, -1), final_row);
  return 0;
}

static void count_encodings(const bcir_q8_model *m, uint32_t *q8_out, uint32_t *lut4_out) {
  uint32_t q8 = 0u, lut4 = 0u, i;
  for (i = 0u; i < m->tensor_count; ++i) {
    if (m->tensors[i].encoding == BCIR_Q8_TENSOR_ENC_LUT4_1B) lut4++;
    else q8++;
  }
  *q8_out = q8;
  *lut4_out = lut4;
}

static int cmd_eval_lm(const char *model_path, const char *tok_path, const char *tgt_path,
                       size_t n_seqs, size_t seq_len, size_t threads, const char *preds_path) {
  bcir_q8_model model;
  bcir_llama_ws ws;
  bcir_llama_runner runner;
  void *pool_mem = NULL;
  bcir_tensor_pool *pool = NULL;
  char error[256];
  size_t total = n_seqs * seq_len;
  uint32_t *tokens = NULL, *targets = NULL;
  int32_t *preds = NULL;
  size_t preds_count = 0u;
  FILE *ft = NULL, *fy = NULL;
  double t0, elapsed, ce_sum = 0.0;
  uint64_t supervised = 0u, correct = 0u;
  uint32_t q8_cnt = 0u, lut4_cnt = 0u;

  bcir_q8_model_init(&model);
  if (bcir_q8_model_load_limited(model_path, &model, error, sizeof error, BENCH_MAX_MODEL_BYTES) ||
      !bcir_llama_model_ready(&model)) {
    fprintf(stderr, "model load failed: %s\n", error);
    bcir_q8_model_free(&model);
    return 1;
  }
  count_encodings(&model, &q8_cnt, &lut4_cnt);
  tokens = (uint32_t *)malloc(total * sizeof *tokens);
  targets = (uint32_t *)malloc(total * sizeof *targets);
  if (preds_path) preds = (int32_t *)malloc(total * sizeof *preds);
  if (!tokens || !targets || (preds_path && !preds)) {
    fprintf(stderr, "oom reading eval buffers\n");
    free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
    return 1;
  }
  ft = fopen(tok_path, "rb");
  fy = fopen(tgt_path, "rb");
  if (!ft || !fy || fread(tokens, sizeof(uint32_t), total, ft) != total ||
      fread(targets, sizeof(uint32_t), total, fy) != total) {
    fprintf(stderr, "failed reading tokens/targets files\n");
    if (ft) fclose(ft);
    if (fy) fclose(fy);
    free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
    return 1;
  }
  fclose(ft);
  fclose(fy);

  if (bcir_llama_ws_init(&ws, &model, seq_len, NULL) ||
      make_runner(threads, &pool_mem, &pool, &runner) ||
      bcir_llama_ws_set_runner(&ws, threads > 1u ? &runner : NULL)) {
    fprintf(stderr, "workspace/runner init failed\n");
    bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool);
    free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
    return 1;
  }

  t0 = now_sec();
  for (size_t s = 0u; s < n_seqs; ++s) {
    ws.rope_pos = SIZE_MAX;
    for (size_t p = 0u; p < seq_len; ++p) {
      int32_t tok = (int32_t)tokens[s * seq_len + p];
      uint32_t tgt = targets[s * seq_len + p];
      if (step_one_token(&model, &ws, tok, p, ws.h)) {
        fprintf(stderr, "step_one_token failed at seq=%zu pos=%zu\n", s, p);
        bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool);
        free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
        return 1;
      }
      if (tgt == BENCH_IGNORE_TARGET) continue;
      if (tgt >= model.vocab_size) {
        fprintf(stderr, "target out of vocab range\n");
        bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool);
        free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
        return 1;
      }
      if (bcir_llama_op_head(&model, &ws, ws.h, ws.logits)) {
        fprintf(stderr, "head failed\n");
        bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool);
        free(tokens); free(targets); free(preds); bcir_q8_model_free(&model);
        return 1;
      }
      int32_t pred = bcir_llama_op_argmax(&model, ws.logits);
      double mval = ws.logits[0], sum_exp = 0.0;
      for (uint32_t v = 1u; v < model.vocab_size; ++v)
        if (ws.logits[v] > mval) mval = ws.logits[v];
      for (uint32_t v = 0u; v < model.vocab_size; ++v)
        sum_exp += exp(ws.logits[v] - mval);
      double nll = (mval + log(sum_exp)) - ws.logits[tgt];
      ce_sum += nll;
      if ((uint32_t)pred == tgt) correct++;
      if (preds) preds[preds_count++] = pred;
      supervised++;
    }
  }
  elapsed = now_sec() - t0;

  if (preds_path && preds) {
    FILE *fp = fopen(preds_path, "wb");
    if (fp) {
      (void)fwrite(preds, sizeof(int32_t), preds_count, fp);
      fclose(fp);
    }
  }

  double mean_ce = supervised ? (ce_sum / (double)supervised) : 0.0;
  double ppl = exp(mean_ce < 50.0 ? mean_ce : 50.0);
  double acc = supervised ? ((double)correct / (double)supervised) : 0.0;
  printf(
      "{\"mode\":\"eval-lm\",\"version\":%u,\"file_bytes\":%zu,"
      "\"q8_tensors\":%u,\"lut4_tensors\":%u,\"threads\":%zu,"
      "\"n_seqs\":%zu,\"seq_len\":%zu,\"supervised_tokens\":%llu,"
      "\"ce\":%.6f,\"ppl\":%.6f,\"token_acc\":%.6f,\"elapsed_s\":%.4f}\n",
      (unsigned)model.version, model.storage_size, q8_cnt, lut4_cnt, threads,
      n_seqs, seq_len, (unsigned long long)supervised, mean_ce, ppl, acc, elapsed);

  bcir_llama_ws_free(&ws);
  destroy_runner(pool_mem, pool);
  free(tokens);
  free(targets);
  free(preds);
  bcir_q8_model_free(&model);
  return 0;
}

static int run_decode_pass(const bcir_q8_model *model, size_t ctx_len, size_t decode_steps,
                           size_t threads, double *prefill_ms_out, double *dec_med_us_out,
                           double *final_logits_out) {
  bcir_llama_ws ws;
  bcir_llama_runner runner;
  void *pool_mem = NULL;
  bcir_tensor_pool *pool = NULL;
  double *step_us = (double *)malloc(decode_steps * sizeof *step_us);
  if (!step_us) return -1;
  if (bcir_llama_ws_init(&ws, model, ctx_len + decode_steps + 1u, NULL) ||
      make_runner(threads, &pool_mem, &pool, &runner) ||
      bcir_llama_ws_set_runner(&ws, threads > 1u ? &runner : NULL)) {
    free(step_us);
    bcir_llama_ws_free(&ws);
    destroy_runner(pool_mem, pool);
    return -1;
  }

  double t0 = now_sec();
  for (size_t p = 0u; p < ctx_len; ++p) {
    int32_t tok = (int32_t)((p * 17u + 5u) % model->vocab_size);
    if (step_one_token(model, &ws, tok, p, ws.h)) {
      free(step_us); bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool); return -1;
    }
  }
  *prefill_ms_out = (now_sec() - t0) * 1e3;

  int32_t next_tok = 0;
  for (size_t s = 0u; s < decode_steps; ++s) {
    double ts0 = now_sec();
    if (bcir_llama_op_head(model, &ws, ws.h, ws.logits)) {
      free(step_us); bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool); return -1;
    }
    next_tok = bcir_llama_op_argmax(model, ws.logits);
    if (s + 1u < decode_steps) {
      if (step_one_token(model, &ws, next_tok, ctx_len + s, ws.h)) {
        free(step_us); bcir_llama_ws_free(&ws); destroy_runner(pool_mem, pool); return -1;
      }
    }
    step_us[s] = (now_sec() - ts0) * 1e6;
  }
  memcpy(final_logits_out, ws.logits, (size_t)model->vocab_size * sizeof(double));
  *dec_med_us_out = median_of(step_us, decode_steps);

  free(step_us);
  bcir_llama_ws_free(&ws);
  destroy_runner(pool_mem, pool);
  return 0;
}

static int cmd_bench_decode(const char *model_path, size_t ctx_len, size_t decode_steps,
                            size_t threads) {
  bcir_q8_model model;
  char error[256];
  uint32_t q8_cnt = 0u, lut4_cnt = 0u;
  bcir_q8_model_init(&model);
  if (bcir_q8_model_load_limited(model_path, &model, error, sizeof error, BENCH_MAX_MODEL_BYTES) ||
      !bcir_llama_model_ready(&model)) {
    fprintf(stderr, "model load failed: %s\n", error);
    bcir_q8_model_free(&model);
    return 1;
  }
  count_encodings(&model, &q8_cnt, &lut4_cnt);
  double *logits_1t = (double *)malloc((size_t)model.vocab_size * sizeof(double));
  double *logits_nt = (double *)malloc((size_t)model.vocab_size * sizeof(double));
  double prefill_1t = 0.0, dec_1t = 0.0, prefill_nt = 0.0, dec_nt = 0.0;
  if (!logits_1t || !logits_nt ||
      run_decode_pass(&model, ctx_len, decode_steps, 1u, &prefill_1t, &dec_1t, logits_1t) ||
      run_decode_pass(&model, ctx_len, decode_steps, threads, &prefill_nt, &dec_nt, logits_nt)) {
    fprintf(stderr, "decode benchmark failed\n");
    free(logits_1t); free(logits_nt); bcir_q8_model_free(&model);
    return 1;
  }
  int drift_bits = (memcmp(logits_1t, logits_nt, (size_t)model.vocab_size * sizeof(double)) == 0) ? 0 : 1;
  printf(
      "{\"mode\":\"bench-decode\",\"version\":%u,\"file_bytes\":%zu,"
      "\"q8_tensors\":%u,\"lut4_tensors\":%u,\"ctx_len\":%zu,\"decode_steps\":%zu,"
      "\"threads\":%zu,\"prefill_1t_ms\":%.3f,\"decode_1t_median_us\":%.2f,"
      "\"decode_1t_tok_s\":%.2f,\"prefill_nt_ms\":%.3f,\"decode_nt_median_us\":%.2f,"
      "\"decode_nt_tok_s\":%.2f,\"threads_1t_vs_nt_bit_drift\":%d,"
      "\"q8_instances\":%d,\"lut4_instances\":%d}\n",
      (unsigned)model.version, model.storage_size, q8_cnt, lut4_cnt, ctx_len, decode_steps,
      threads, prefill_1t, dec_1t, dec_1t > 0.0 ? 1e6 / dec_1t : 0.0,
      prefill_nt, dec_nt, dec_nt > 0.0 ? 1e6 / dec_nt : 0.0,
      drift_bits, bcir_ai_q8_instances(), bcir_ai_lut4_instances());
  free(logits_1t);
  free(logits_nt);
  bcir_q8_model_free(&model);
  return 0;
}

static uint16_t f32_to_f16_bits(float v) {
  uint32_t x;
  memcpy(&x, &v, sizeof x);
  uint32_t sign = (x >> 16) & 0x8000u;
  int exp = (int)((x >> 23) & 0xFFu) - 127 + 15;
  uint32_t mant = x & 0x007FFFFFu;
  if (exp <= 0) return (uint16_t)sign;
  if (exp >= 31) return (uint16_t)(sign | 0x7BFFu);
  return (uint16_t)(sign | ((uint32_t)exp << 10) | (mant >> 13));
}

static void fill_fp16_buf(uint8_t *dst, size_t n, uint32_t seed) {
  for (size_t i = 0u; i < n; ++i) {
    float v = 0.08f * sinf((float)(i + 1u) * 0.27f + (float)seed);
    uint16_t h = f32_to_f16_bits(v);
    dst[2u * i] = (uint8_t)(h & 0xFFu);
    dst[2u * i + 1u] = (uint8_t)(h >> 8);
  }
}

static int cmd_bench_kernels(void) {
  typedef struct shape_spec {
    const char *name;
    size_t in_dim, out_dim, r1, r2;
  } shape_spec;
  static const shape_spec shapes[] = {
      {"wide512_mlp_up", 512u, 1536u, 262u, 87u},
      {"wide512_mlp_down", 1536u, 512u, 262u, 87u},
      {"wide50m_mlp_up", 768u, 2048u, 392u, 131u},
      {"wide50m_mlp_down", 2048u, 768u, 392u, 131u},
  };
  static const size_t batch_ns[] = {1u, 4u, 16u, 64u};
  int q8_mask = bcir_ai_q8_instances();
  int lut4_mask = bcir_ai_lut4_instances();
  printf("{\"mode\":\"bench-kernels\",\"q8_instances\":%d,\"lut4_instances\":%d,\"sweeps\":[",
         q8_mask, lut4_mask);
  int first = 1;
  for (size_t si = 0u; si < sizeof shapes / sizeof shapes[0]; ++si) {
    size_t din = shapes[si].in_dim, dout = shapes[si].out_dim;
    size_t ranks[2] = {shapes[si].r1, shapes[si].r2};
    /* Prepare 2-path LUT4 weights */
    bcir_ai_lut4_path paths[2];
    uint8_t *vp[2] = {NULL, NULL}, *up[2] = {NULL, NULL};
    uint8_t *gp[2] = {NULL, NULL}, *lp[2] = {NULL, NULL}, *hp[2] = {NULL, NULL};
    size_t lut4_total_bytes = 0u;
    for (size_t p = 0u; p < 2u; ++p) {
      size_t vb = 0u, ub = 0u;
      (void)bcir_ai_lut4_packed_bytes(ranks[p], din, &vb);
      (void)bcir_ai_lut4_packed_bytes(dout, ranks[p], &ub);
      vp[p] = (uint8_t *)malloc(vb);
      up[p] = (uint8_t *)malloc(ub);
      gp[p] = (uint8_t *)malloc(din * 2u);
      lp[p] = (uint8_t *)malloc(ranks[p] * 2u);
      hp[p] = (uint8_t *)malloc(dout * 2u);
      int8_t *sv = (int8_t *)malloc(ranks[p] * din);
      int8_t *su = (int8_t *)malloc(dout * ranks[p]);
      for (size_t k = 0u; k < ranks[p] * din; ++k) sv[k] = ((k * 7u + p) % 3u) ? 1 : -1;
      for (size_t k = 0u; k < dout * ranks[p]; ++k) su[k] = ((k * 5u + p) % 2u) ? -1 : 1;
      (void)bcir_ai_lut4_pack_signs(sv, ranks[p], din, vp[p], vb);
      (void)bcir_ai_lut4_pack_signs(su, dout, ranks[p], up[p], ub);
      free(sv); free(su);
      fill_fp16_buf(gp[p], din, (uint32_t)(10u + p));
      fill_fp16_buf(lp[p], ranks[p], (uint32_t)(20u + p));
      fill_fp16_buf(hp[p], dout, (uint32_t)(30u + p));
      paths[p].rank = ranks[p];
      paths[p].v_packed = vp[p]; paths[p].v_packed_bytes = vb;
      paths[p].u_packed = up[p]; paths[p].u_packed_bytes = ub;
      paths[p].g_fp16_le = gp[p]; paths[p].l_fp16_le = lp[p]; paths[p].h_fp16_le = hp[p];
      lut4_total_bytes += vb + ub + 2u * (din + ranks[p] + dout);
    }
    /* Prepare Q8 g=32 weights of identical logical shape [din, dout] */
    size_t elems = din * dout, groups = (elems + 31u) / 32u;
    uint8_t *q8_codes = (uint8_t *)malloc(elems);
    uint8_t *q8_exps = (uint8_t *)malloc(groups * 2u);
    for (size_t k = 0u; k < elems; ++k) q8_codes[k] = (uint8_t)((int8_t)(((int)(k % 251u)) - 125));
    for (size_t g = 0u; g < groups; ++g) {
      int16_t e = (int16_t)(-9 + (int)(g % 3u));
      q8_exps[2u * g] = (uint8_t)(e & 0xFF);
      q8_exps[2u * g + 1u] = (uint8_t)((e >> 8) & 0xFF);
    }
    size_t q8_total_bytes = elems + groups * 2u;

    /* Inputs/outputs for max N=64 */
    double *x64 = (double *)malloc(64u * din * sizeof(double));
    double *y64_a = (double *)malloc(64u * dout * sizeof(double));
    double *y64_b = (double *)malloc(64u * dout * sizeof(double));
    float *x32 = (float *)malloc(64u * din * sizeof(float));
    float *y32_a = (float *)malloc(64u * dout * sizeof(float));
    float *y32_b = (float *)malloc(64u * dout * sizeof(float));
    for (size_t k = 0u; k < 64u * din; ++k) {
      x64[k] = 0.25 * sin((double)(k + 1u) * 0.13);
      x32[k] = (float)x64[k];
    }

    /* Determinism check: portable(1) vs widest(0) on N=1 & N=4 */
    (void)bcir_ai_lut4_matvec_f64_instance(1, x64, din, paths, 2u, dout, y64_a);
    (void)bcir_ai_lut4_matvec_f64_instance(0, x64, din, paths, 2u, dout, y64_b);
    int drift_f64 = (memcmp(y64_a, y64_b, dout * sizeof(double)) == 0) ? 0 : 1;
    (void)bcir_ai_lut4_matvec_f32_instance(1, x32, din, paths, 2u, dout, y32_a);
    (void)bcir_ai_lut4_matvec_f32_instance(0, x32, din, paths, 2u, dout, y32_b);
    int drift_f32 = (memcmp(y32_a, y32_b, dout * sizeof(float)) == 0) ? 0 : 1;
    (void)bcir_ai_q8_matvec_f64_instance(1, x64, din, q8_codes, elems, q8_exps, groups, dout, 32u, y64_a);
    (void)bcir_ai_q8_matvec_f64_instance(0, x64, din, q8_codes, elems, q8_exps, groups, dout, 32u, y64_b);
    int drift_q8 = (memcmp(y64_a, y64_b, dout * sizeof(double)) == 0) ? 0 : 1;

    for (size_t ni = 0u; ni < sizeof batch_ns / sizeof batch_ns[0]; ++ni) {
      size_t ntok = batch_ns[ni];
      size_t reps = ntok <= 4u ? 15u : (ntok <= 16u ? 7u : 5u);
      double ts_q8[15], ts_l64[15], ts_l32[15];
      /* Warmup */
      for (size_t t = 0u; t < ntok; ++t)
        (void)bcir_ai_q8_matvec_f64_instance(0, x64 + t * din, din, q8_codes, elems, q8_exps, groups, dout, 32u, y64_a + t * dout);
      (void)bcir_ai_lut4_matmul_mb4_f64_instance(0, x64, ntok, din, paths, 2u, dout, y64_b);
      (void)bcir_ai_lut4_matmul_mb4_f32_instance(0, x32, ntok, din, paths, 2u, dout, y32_b);

      for (size_t r = 0u; r < reps; ++r) {
        double t0 = now_sec();
        for (size_t t = 0u; t < ntok; ++t)
          (void)bcir_ai_q8_matvec_f64_instance(0, x64 + t * din, din, q8_codes, elems, q8_exps, groups, dout, 32u, y64_a + t * dout);
        ts_q8[r] = (now_sec() - t0) * 1e6;

        t0 = now_sec();
        (void)bcir_ai_lut4_matmul_mb4_f64_instance(0, x64, ntok, din, paths, 2u, dout, y64_b);
        ts_l64[r] = (now_sec() - t0) * 1e6;

        t0 = now_sec();
        (void)bcir_ai_lut4_matmul_mb4_f32_instance(0, x32, ntok, din, paths, 2u, dout, y32_b);
        ts_l32[r] = (now_sec() - t0) * 1e6;
      }
      double med_q8 = median_of(ts_q8, reps);
      double med_l64 = median_of(ts_l64, reps);
      double med_l32 = median_of(ts_l32, reps);
      if (!first) printf(",");
      first = 0;
      printf(
          "{\"shape\":\"%s\",\"in_dim\":%zu,\"out_dim\":%zu,\"r1\":%zu,\"r2\":%zu,\"n_tokens\":%zu,"
          "\"q8_bytes\":%zu,\"lut4_bytes\":%zu,\"lut4_bpw\":%.4f,\"compression_ratio\":%.3f,"
          "\"q8_f64_us\":%.2f,\"lut4_mb4_f64_us\":%.2f,\"lut4_mb4_f32_us\":%.2f,"
          "\"speedup_lut4_f64_vs_q8\":%.3f,\"speedup_lut4_f32_vs_q8\":%.3f,"
          "\"max_bit_drift\":%d}",
          shapes[si].name, din, dout, ranks[0], ranks[1], ntok,
          q8_total_bytes, lut4_total_bytes, 8.0 * (double)lut4_total_bytes / (double)elems,
          (double)q8_total_bytes / (double)lut4_total_bytes,
          med_q8, med_l64, med_l32,
          med_l64 > 0.0 ? med_q8 / med_l64 : 0.0,
          med_l32 > 0.0 ? med_q8 / med_l32 : 0.0,
          drift_f64 | drift_f32 | drift_q8);
    }
    for (size_t p = 0u; p < 2u; ++p) {
      free(vp[p]); free(up[p]); free(gp[p]); free(lp[p]); free(hp[p]);
    }
    free(q8_codes); free(q8_exps);
    free(x64); free(y64_a); free(y64_b); free(x32); free(y32_a); free(y32_b);
  }
  printf("]}\n");
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: %s --eval-lm ... | --bench-decode ... | --bench-kernels\n", argv[0]);
    return 2;
  }
  if (!strcmp(argv[1], "--bench-kernels")) return cmd_bench_kernels();
  if (!strcmp(argv[1], "--eval-lm") && argc >= 7) {
    const char *model_path = argv[2], *tok_path = argv[3], *tgt_path = argv[4];
    size_t n_seqs = (size_t)strtoull(argv[5], NULL, 10);
    size_t seq_len = (size_t)strtoull(argv[6], NULL, 10);
    size_t threads = 2u;
    const char *preds_path = NULL;
    for (int i = 7; i < argc; ++i) {
      if (!strcmp(argv[i], "--threads") && i + 1 < argc)
        threads = (size_t)strtoull(argv[++i], NULL, 10);
      else if (!strcmp(argv[i], "--preds-out") && i + 1 < argc)
        preds_path = argv[++i];
    }
    return cmd_eval_lm(model_path, tok_path, tgt_path, n_seqs, seq_len, threads, preds_path);
  }
  if (!strcmp(argv[1], "--bench-decode") && argc >= 5) {
    const char *model_path = argv[2];
    size_t ctx_len = (size_t)strtoull(argv[3], NULL, 10);
    size_t decode_steps = (size_t)strtoull(argv[4], NULL, 10);
    size_t threads = 2u;
    for (int i = 5; i < argc; ++i) {
      if (!strcmp(argv[i], "--threads") && i + 1 < argc)
        threads = (size_t)strtoull(argv[++i], NULL, 10);
    }
    return cmd_bench_decode(model_path, ctx_len, decode_steps, threads);
  }
  fprintf(stderr, "unknown or malformed arguments\n");
  return 2;
}
