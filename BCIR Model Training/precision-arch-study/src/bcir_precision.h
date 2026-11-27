/*===- bcir_precision.h - Executable BCIR Precision Policy (P1-P6) Engine ---===*/
#ifndef BCIR_PRECISION_H
#define BCIR_PRECISION_H

#include "bcir_tensor.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
  BCIR_GEMM_FP32_KBLOCK    = 0,
  BCIR_GEMM_BF16_NDT       = 1,
  BCIR_GEMM_BF16_VDP       = 2,
  BCIR_GEMM_FP8_E4M3_BLK32 = 3,
  BCIR_GEMM_FP8_E5M2_BLK32 = 4,
  BCIR_GEMM_FP4_MX_BLK32   = 5,
  BCIR_GEMM_Q8_BLK32       = 6,
  BCIR_GEMM_KIND_COUNT     = 7
};

enum {
  BCIR_OPT_FP32                 = 0,
  BCIR_OPT_Q8_MOMENTS           = 1,
  BCIR_OPT_Q8_MOMENTS_BF16_GRAD = 2,
  BCIR_OPT_ADAFACTOR_M2         = 3,
  BCIR_OPT_ADAFACTOR_BF16_GRAD  = 4,
  BCIR_OPT_KIND_COUNT           = 5
};

enum {
  BCIR_INIT_LEGACY_002      = 0,
  BCIR_INIT_RESIDUAL_SCALED = 1,
  BCIR_INIT_MUP_V1          = 2
};

enum {
  BCIR_FP64_GUARD_OFF         = 0,
  BCIR_FP64_GUARD_CONDITIONAL = 1,
  BCIR_FP64_GUARD_ALWAYS      = 2
};

typedef struct bcir_num_contract_desc {
  int kind;
  const char *name;
  const char *operand_fmt;
  const char *exponent_policy;
  const char *rounding_mode;
  const char *accumulator_type;
  const char *reduction_order;
  const char *subnormal_handling;
  const char *overflow_behavior;
  double max_rel_err_bound;
  int block_size;
  double bits_per_elem;
} bcir_num_contract_desc;

typedef struct bcir_prec_policy {
  int attn_fwd_gemm;
  int attn_bwd_gemm;
  int mlp_fwd_gemm;
  int mlp_bwd_gemm;
  int opt_kind;
  int fp64_guard_mode;
  double rmsnorm_cond_thresh;
  double softmax_logit_thresh;
  double wy_cond_thresh;
  int use_vdp_portable;
} bcir_prec_policy;

typedef struct bcir_prec_telemetry {
  uint64_t fp64_norm_promotions;
  uint64_t fp64_softmax_promotions;
  uint64_t fp64_wy_solves;
  uint64_t nonfinite_events;
  uint64_t grad_clip_hits;
  uint64_t total_steps;
  uint64_t q8_m2_floor_clamps;
  uint64_t gemm_calls_attn;
  uint64_t gemm_calls_mlp;
  double max_grad_norm;
  double max_softmax_logit;
  double min_rms_observed;
  double max_wy_offdiag;
} bcir_prec_telemetry;

const bcir_num_contract_desc *bcir_prec_gemm_desc(int gemm_kind);
const char *bcir_prec_opt_name(int opt_kind);
void bcir_prec_policy_default(bcir_prec_policy *pol);
void bcir_prec_telemetry_reset(bcir_prec_telemetry *t);

int bcir_mm_bf16_vdp_variants(void);
float bcir_quant_fp8_e4m3_scalar(float x);
float bcir_quant_fp8_e5m2_scalar(float x);
float bcir_quant_fp4_e2m1_scalar(float x);
void bcir_quant_dequant_blk32_pool(bcir_tensor_pool *pool, int gemm_kind,
                                   const float *src, float *dst, size_t n);

void bcir_quant_q8_g32_encode(const float *src, int8_t *q_out, int8_t *exp_out, size_t n);
void bcir_quant_q8_g32_decode(const int8_t *q_in, const int8_t *exp_in, float *dst, size_t n);

int bcir_mm_bf16_vdp_variant(bcir_tensor_pool *pool, int variant,
                             int ta, int tb, size_t m, size_t n, size_t k,
                             float alpha, const float *a, const float *b,
                             float beta, float *c, void *scratch, size_t scratch_bytes);
size_t bcir_mm_bf16_vdp_scratch_bytes(size_t m, size_t n, size_t k);

int bcir_prec_mm_pool(bcir_tensor_pool *pool, int gemm_kind, int force_portable,
                      int ta, int tb, size_t m, size_t n, size_t k,
                      float alpha, const float *a, const float *b,
                      float beta, float *c, void *scratch, size_t scratch_bytes);
size_t bcir_prec_mm_scratch_bytes(int gemm_kind, size_t m, size_t n, size_t k);

#define BCIR_OPT_Q8_BLOCK 256u

typedef struct bcir_opt_tensor_shape {
  size_t offset;
  size_t rows;
  size_t cols;
  double lr_mult;
  int apply_wd;
} bcir_opt_tensor_shape;

typedef struct bcir_opt_state {
  int opt_kind;
  size_t n_params;
  size_t n_blocks;
  uint64_t step;
  double beta1_power;
  double beta2_power;
  float *grad_fp32;
  uint16_t *grad_bf16;
  float *m1_fp32;
  float *m2_fp32;
  int8_t *m1_q8;
  float *m1_scale;
  uint8_t *m2_q8;
  float *m2_log_min;
  float *m2_log_step;
  size_t n_shapes;
  bcir_opt_tensor_shape *shapes;
  float **af_row;
  float **af_col;
  size_t persistent_bytes;
  size_t opt_only_bytes;
} bcir_opt_state;

typedef struct bcir_adamw_params {
  double lr, beta1, beta2, epsilon, weight_decay, grad_clip;
} bcir_adamw_params;

bcir_opt_state *bcir_opt_create(int opt_kind, size_t n_params,
                                const bcir_opt_tensor_shape *shapes, size_t n_shapes);
void bcir_opt_destroy(bcir_opt_state *opt);
void bcir_opt_reset(bcir_opt_state *opt);
void bcir_opt_zero_grad(bcir_opt_state *opt);
void bcir_opt_commit_grad_fp32(bcir_opt_state *opt, const float *grad_work, int accumulate);
void bcir_opt_commit_grad_span_fp32(bcir_opt_state *opt, size_t offset, size_t count,
                                    const float *grad_buf, int accumulate);
int bcir_opt_step(bcir_tensor_pool *pool, bcir_opt_state *opt, float *weights,
                  const float *grad_work_or_null, const bcir_adamw_params *cfg,
                  double grad_scale, double *out_grad_norm, int *out_clipped,
                  bcir_prec_telemetry *tel);

typedef struct bcir_init_tensor_desc {
  size_t offset, rows, cols;
  int role;
  float const_val;
} bcir_init_tensor_desc;

void bcir_init_weights_c11(float *weights, size_t n_params,
                           const bcir_init_tensor_desc *descs, size_t n_descs,
                           uint32_t n_layers, uint32_t d_model, uint32_t d_base,
                           uint64_t seed, int init_policy,
                           bcir_opt_tensor_shape *out_opt_shapes);

#ifdef __cplusplus
}
#endif
#endif
