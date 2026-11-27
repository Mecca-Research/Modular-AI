/*===- bcir_littlebit.h - Bare-metal C LittleBit sub-1-bit QAT & inference ---===
 *
 * Implements LittleBit (Lee et al., NeurIPS 2025, arXiv:2506.13771v5) and
 * LittleBit-2 (ICML 2026, arXiv:2603.00042v2) directly on BCIR's native C
 * tensor primitives (bcir_tensor.h) with zero PyTorch / framework dependency:
 *
 *   1. Five linear layer modes inside the identical BCIR Llama/SwiGLU decoder:
 *        - LB_MODE_DENSE_FP32     : standard dense FP32 (bit-exact vs BCIR)
 *        - LB_MODE_LOWRANK_FP16   : unbinarized low-rank U*V^T (FP16-rounded fwd)
 *        - LB_MODE_ONEBIT_FULL    : full-rank 1-bit diag(h)*sign(W)*diag(g)
 *        - LB_MODE_LITTLEBIT_1P   : single-path LittleBit (rank r1)
 *        - LB_MODE_LITTLEBIT_RES  : dual-path Primary + Residual Compensation
 *   2. Exact SmoothSign(k=100), SmoothSign(k=10), Clipped-STE, Unclipped-STE
 *   3. Exact multi-scale ablation flags (row h, col g, latent l)
 *   4. Exact QAT objectives: pure masked CE, Output KL KD, KL + lambda*Inter-MSE
 *   5. True bit-packed 1-bit uint64_t storage + Portable & AVX-512 inference
 *      kernels (masked sign-XOR and 4-bit LUT T-MAC style)
 *
 * All heavy tensor ops dispatch directly to BCIR's bcir_tensor.c kernels.
 *===------------------------------------------------------------------------===*/
#ifndef BCIR_LITTLEBIT_H
#define BCIR_LITTLEBIT_H

#include "bcir_tensor.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LB_IGNORE_TARGET 0xFFFFFFFFu

enum {
  LB_MODE_DENSE_FP32   = 0,
  LB_MODE_LOWRANK_FP16 = 1,
  LB_MODE_ONEBIT_FULL  = 2,
  LB_MODE_LITTLEBIT_1P = 3,
  LB_MODE_LITTLEBIT_RES = 4
};

enum {
  LB_SURR_SMOOTHSIGN_100 = 0, /* fwd: sign(x), bwd: 100 * sech^2(100*x) */
  LB_SURR_SMOOTHSIGN_10  = 1, /* fwd: sign(x), bwd: 10  * sech^2(10*x)  */
  LB_SURR_CLIPPED_STE    = 2, /* fwd: sign(x), bwd: 1(|x| <= 1)         */
  LB_SURR_UNCLIPPED_STE  = 3, /* fwd: sign(x), bwd: 1                   */
  LB_SURR_SMOOTH_FWD_TEST = 4 /* ONLY for finite-diff check: fwd tanh(k*x) */
};

enum {
  LB_SCALE_ROW    = 1u, /* learnable h */
  LB_SCALE_COL    = 2u, /* learnable g */
  LB_SCALE_LATENT = 4u, /* learnable l */
  LB_SCALE_ALL    = 7u
};

enum {
  LB_OBJ_CE       = 0, /* pure supervised cross-entropy */
  LB_OBJ_KD_KL    = 1, /* output KL(teacher || student) */
  LB_OBJ_KD_FULL  = 2, /* output KL + kd_lambda * mean_layer_MSE */
  LB_OBJ_CE_PLUS_KD = 3 /* 0.5*CE + 0.5*KL + kd_lambda * mean_layer_MSE */
};

/* Per-projection configuration (7 projections per layer: q, k, v, o, gate, up, down) */
enum {
  LB_PROJ_Q = 0, LB_PROJ_K = 1, LB_PROJ_V = 2, LB_PROJ_O = 3,
  LB_PROJ_GATE = 4, LB_PROJ_UP = 5, LB_PROJ_DOWN = 6,
  LB_PROJ_COUNT = 7
};

typedef struct lb_proj_cfg {
  uint32_t mode;        /* LB_MODE_* */
  uint32_t din;
  uint32_t dout;
  uint32_t r1;          /* primary rank (for LOWRANK / 1P / RES) */
  uint32_t r2;          /* residual rank (for RES only, 0 otherwise) */
  uint32_t scale_flags; /* LB_SCALE_* */
  uint32_t fp16_round;  /* 1: round scales (or low-rank factors) to IEEE FP16 in fwd */
} lb_proj_cfg;

typedef struct lb_model_cfg {
  uint32_t vocab, width, heads, kvheads, layers, ff, batch, time, tied;
  double rope_base, rms_eps;
  uint32_t surrogate;       /* LB_SURR_* */
  double smooth_k;          /* default 100.0 for SMOOTHSIGN_100, 10.0 for SMOOTHSIGN_10 */
  uint32_t objective;       /* LB_OBJ_* */
  double kd_lambda;         /* default 10.0 (LittleBit Eq. 11) */
  uint32_t threads;         /* bcir_tensor_pool threads */
  uint32_t use_kblock;      /* 1: bcir_tensor_mm_kblock_pool, 0: v1 */
} lb_model_cfg;

typedef struct lb_adamw_cfg {
  double lr;
  double lr_scales;         /* if > 0, separate LR for h, g, l; else uses lr */
  double beta1, beta2, epsilon, weight_decay, grad_clip;
  int freeze_non_linear;    /* 1: freeze embed & RMSNorm during QAT (paper style) */
  int freeze_dense_projs;   /* 1: freeze LB_MODE_DENSE_FP32 projections (e.g. Q8 Attention in COMP-1 QAT) */
} lb_adamw_cfg;

typedef struct lb_step_metrics {
  double total_loss;
  double ce_loss;
  double kl_loss;
  double inter_mse;
  double grad_norm;
  double sign_flip_rate;    /* fraction of binary signs flipped on this step */
  double active_grad_frac;  /* fraction of latent U,V entries with |k*x| < 3.0 */
  double step_ms;
  uint64_t step;
} lb_step_metrics;

typedef struct lb_packed_path {
  uint32_t din, dout, r;
  uint32_t din_words;       /* ceil(din / 64) */
  uint32_t dout_words;      /* ceil(dout / 64) */
  uint32_t r_words;         /* ceil(r / 64) */
  /* Bit-packed V_sign^T [r x din_words] (each row m holds din bits of col m of V_sign) */
  uint64_t *v_bits;
  /* Bit-packed U_sign [dout x r_words] (each row j holds r bits of row j of U_sign) */
  uint64_t *u_bits;
  /* Also packed in 4-bit nibble transpose layout for AVX-512 LUT4 kernel:
   * v_nibbles: [ceil(din/4) x r] uint8_t (values 0..15)
   * u_nibbles: [ceil(r/4) x dout] uint8_t (values 0..15) */
  uint8_t *v_nibbles;
  uint8_t *u_nibbles;
  float *g;                 /* [din] */
  float *l;                 /* [r] */
  float *h;                 /* [dout] */
  size_t packed_bytes_fp16; /* exact deployment bytes (1-bit U,V + FP16 h,g,l) */
} lb_packed_path;

typedef struct lb_model lb_model;

/* Lifecycle */
lb_model *lb_model_create(const lb_model_cfg *cfg, const lb_proj_cfg *projs /* [layers*7] */);
void lb_model_destroy(lb_model *m);

/* Parameter buffer access for initialization from Python (Dual-SVID / Joint-ITQ / dense copy) */
size_t lb_model_param_count(const lb_model *m);
size_t lb_model_total_bytes(const lb_model *m); /* persistent + activations + scratch */
float *lb_model_weights_ptr(lb_model *m);
float *lb_model_grads_ptr(lb_model *m);
/* Query exact offset & length of any named tensor inside weights[]:
 * kind: 0=embed, 1=final_norm, 2=lm_head (if untied),
 *       10=norm1(layer), 11=norm2(layer),
 *       20=proj_param(layer, proj_idx, sub_id)
 * sub_id for proj_param:
 *   DENSE: 0=W[dout*din]
 *   LOWRANK: 0=U[dout*r1], 1=V[din*r1]
 *   ONEBIT: 0=W[dout*din], 1=h[dout], 2=g[din]
 *   LITTLEBIT_1P: 0=U1[dout*r1], 1=V1[din*r1], 2=h1[dout], 3=g1[din], 4=l1[r1]
 *   LITTLEBIT_RES: 0..4=path1 (U1,V1,h1,g1,l1), 5..9=path2 (U2,V2,h2,g2,l2)
 */
int lb_model_tensor_span(const lb_model *m, int kind, uint32_t layer, uint32_t proj,
                         uint32_t sub_id, size_t *offset, size_t *count);

/* Forward pass: writes logits [batch*time*vocab] and optionally layer hidden outputs [layers*batch*time*width] */
int lb_model_forward(lb_model *m, const uint32_t *tokens, float *out_logits, float *out_hiddens);

/* Single backward pass (computes loss & gradients; does not step AdamW) */
int lb_model_loss_backward(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                           const uint32_t *targets, int accumulate, lb_step_metrics *out);

/* Single training step: forward (+ teacher forward if KD), backward, clip, AdamW update */
int lb_model_train_step(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                        const uint32_t *targets, const lb_adamw_cfg *opt, lb_step_metrics *out);

/* Multi-step native C loop (eliminates Python per-step overhead!) */
int lb_model_train_loop(lb_model *m, lb_model *teacher, const uint32_t *tokens,
                        const uint32_t *targets, size_t steps, const double *lrs,
                        const lb_adamw_cfg *opt, lb_step_metrics *events);

/* Zero optimizer moments & reset step counter */
void lb_model_reset_opt(lb_model *m);

/* ---------- Bare-metal Bit-Packed Inference Kernel Microbenchmark API ---------- */
enum {
  LB_KERN_PORTABLE_BITPACK = 0,
  LB_KERN_AVX512_SIGNXOR   = 1,
  LB_KERN_AVX512_LUT4      = 2,
  LB_KERN_DENSE_FP32_BCIR  = 3,
  LB_KERN_DENSE_BF16_BCIR  = 4,
  LB_KERN_DENSE_FP16_STREAM = 5,
  LB_KERN_DENSE_Q8_BCIR    = 6,
  LB_KERN_LOWRANK_FP32     = 7,
  LB_KERN_AVX512_LUT4_PACKED1B     = 8,
  LB_KERN_AVX512_LUT4_PACKED1B_MB4 = 9
};

typedef struct lb_bench_result {
  double median_us;
  double min_us;
  double gflops_equiv;    /* 2*N*din*dout / time */
  double max_abs_err;     /* vs exact FP32 reference */
  size_t weight_bytes;    /* actual backend representation bytes read by timed kernel (incl. padding & runtime scales) */
  double realized_bpw;    /* 8 * weight_bytes / (din * dout) */
  size_t ideal_deploy_weight_bytes; /* idealized unpadded deployment bound (1-bit U,V + FP16 scales / BCIRQ8 int16 exp) */
  double ideal_deploy_bpw;
} lb_bench_result;

int lb_bench_linear_kernel(int kernel_kind, uint32_t N, uint32_t din, uint32_t dout,
                           uint32_t r, int residual, uint32_t threads, uint32_t iters,
                           lb_bench_result *out);

#ifdef __cplusplus
}
#endif

#endif /* BCIR_LITTLEBIT_H */
