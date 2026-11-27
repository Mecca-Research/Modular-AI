/*===- bcir_comp1_deploy.h - Whole-Model COMP-1 & Region-Policy Deploy -----===*/
#ifndef BCIR_COMP1_DEPLOY_H
#define BCIR_COMP1_DEPLOY_H

#include "bcir_mixer_ext.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
  BCIR_DEPLOY_FP32        = 0,
  BCIR_DEPLOY_FP16        = 1,
  BCIR_DEPLOY_BCIRQ8_G32  = 2,
  BCIR_DEPLOY_LB_PACKED1B = 3
};

typedef struct bcir_deploy_stats {
  /* Actual evaluated resident storage in bcir_comp1_deploy.c (FP32 non-linear/KV, FP64 DeltaNet state) */
  size_t total_artifact_bytes, attn_bytes, mlp_bytes, nonlin_bytes;
  size_t body_params, total_params;
  double realized_body_bpw, realized_total_bpw;
  size_t kv_cache_bytes_at_64, kv_cache_bytes_at_1024, kv_cache_bytes_at_4096, rec_state_bytes;
  double max_weight_rel_frob_attn, max_weight_rel_frob_mlp, max_q8_bound_violation;
  /* Hypothetical compressed-state projection (FP16 non-linear/KV, FP32 DeltaNet state), distinct from measured runtime */
  size_t projected_fp16_nonlin_bytes, projected_fp16_artifact_bytes;
  double projected_fp16_total_bpw;
  size_t projected_fp16_kv_bytes_at_64, projected_fp16_kv_bytes_at_1024, projected_fp16_kv_bytes_at_4096;
  size_t projected_fp32_rec_state_bytes;
} bcir_deploy_stats;

typedef struct bcir_deploy_model bcir_deploy_model;

bcir_deploy_model *bcir_deploy_create(const bcir_ext_spec *spec, uint32_t max_kv_len);
void bcir_deploy_destroy(bcir_deploy_model *dm);
int bcir_deploy_load_from_ext(bcir_deploy_model *dm, const bcir_ext_model *src,
                              int default_attn_fmt, int default_mlp_fmt);
int bcir_deploy_set_linear_lb(bcir_deploy_model *dm, size_t tensor_idx,
                              uint32_t r1, const float *U1, const float *V1,
                              const float *h1, const float *g1, const float *l1,
                              uint32_t r2, const float *U2, const float *V2,
                              const float *h2, const float *g2, const float *l2,
                              const float *fp32_ref_or_null);
int bcir_deploy_set_linear_dense(bcir_deploy_model *dm, size_t tensor_idx,
                                 int fmt, const float *W_fp32);
void bcir_deploy_get_stats(const bcir_deploy_model *dm, bcir_deploy_stats *out);
void bcir_deploy_reset_cache(bcir_deploy_model *dm);
int bcir_deploy_decode_step(bcir_deploy_model *dm, uint32_t token_id, uint32_t pos,
                            int use_portable, float *out_logits);
int bcir_deploy_prefill_seq(bcir_deploy_model *dm, const uint32_t *tokens, uint32_t seq_len,
                            int use_portable, float *out_logits);
int bcir_deploy_evaluate(bcir_deploy_model *dm, const uint32_t *tokens, const uint32_t *targets,
                         size_t n_batches, uint32_t batch_size, uint32_t stride_T,
                         uint32_t active_T, int use_portable, bcir_ext_eval_result *out);

typedef struct bcir_deploy_bench {
  double decode_median_us, decode_tok_per_s, decode_attn_frac, decode_mlp_frac;
  double prefill_n16_ms, prefill_n64_ms, prefill_n128_ms, max_portable_avx_diff;
} bcir_deploy_bench;

int bcir_deploy_benchmark(bcir_deploy_model *dm, uint32_t ctx_len, uint32_t decode_iters,
                          uint32_t prefill_iters, bcir_deploy_bench *out);

#ifdef __cplusplus
}
#endif
#endif
