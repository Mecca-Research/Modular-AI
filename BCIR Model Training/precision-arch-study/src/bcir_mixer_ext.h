/*===- bcir_mixer_ext.h - Pluggable Sequence-Mixer ABI (MIXER-EXT) Engine ---===*/
#ifndef BCIR_MIXER_EXT_H
#define BCIR_MIXER_EXT_H

#include "bcir_precision.h"
#include "bcir_tensor.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BCIR_EXT_MAX_LAYERS 64u
#define BCIR_EXT_IGNORE     0xFFFFFFFFu

enum {
  BCIR_MIXER_GQA      = 0,
  BCIR_MIXER_SWA      = 1,
  BCIR_MIXER_DELTANET = 2,
  BCIR_MIXER_MLA      = 4
};

typedef struct bcir_ext_spec {
  uint32_t vocab, width, heads, kvheads, layers, ff, batch, time, tied;
  double rope_base, rms_eps;
  uint32_t layer_mixer[BCIR_EXT_MAX_LAYERS];
  uint32_t layer_use_rope[BCIR_EXT_MAX_LAYERS];
  uint32_t swa_window;
  uint32_t delta_chunk;
  uint32_t mla_dc;
  uint32_t mla_dr;
  int init_policy;
  uint32_t mup_d_base;
  uint64_t seed;
  uint32_t threads;
  int skip_init;
} bcir_ext_spec;

typedef struct bcir_ext_mem_stats {
  size_t n_params, attn_params, mlp_params, nonlin_params;
  size_t weight_bytes, opt_only_bytes, persistent_bytes;
  size_t activation_bytes, mixer_prob_bytes, scratch_bytes, total_bytes;
  size_t infer_kv_bytes_per_tok, infer_rec_state_bytes;
  double init_ms;
} bcir_ext_mem_stats;

typedef struct bcir_ext_step_metrics {
  double loss, grad_norm, step_ms;
  int clipped, status;
  uint64_t step, supervised_tokens, fp64_promotions, nonfinite_events;
} bcir_ext_step_metrics;

typedef struct bcir_ext_eval_result {
  double ce, accuracy;
  uint64_t tokens, nonfinite;
  double eval_ms;
} bcir_ext_eval_result;

typedef struct bcir_ext_model bcir_ext_model;

bcir_ext_model *bcir_ext_create(const bcir_ext_spec *spec, const bcir_prec_policy *pol);
void bcir_ext_destroy(bcir_ext_model *m);
void bcir_ext_get_mem_stats(const bcir_ext_model *m, bcir_ext_mem_stats *out);
void bcir_ext_get_telemetry(const bcir_ext_model *m, bcir_prec_telemetry *out);
void bcir_ext_reset_opt(bcir_ext_model *m);
float *bcir_ext_weights_ptr(bcir_ext_model *m);
float *bcir_ext_grads_fp32_ptr(bcir_ext_model *m);
size_t bcir_ext_param_count(const bcir_ext_model *m);

typedef struct bcir_ext_tensor_info {
  size_t offset, rows, cols;
  int layer, region;
  char name[48];
} bcir_ext_tensor_info;

size_t bcir_ext_tensor_count(const bcir_ext_model *m);
int bcir_ext_tensor_info_at(const bcir_ext_model *m, size_t idx, bcir_ext_tensor_info *out);

int bcir_ext_forward(bcir_ext_model *m, const uint32_t *tokens, uint32_t active_time, float *out_logits);
int bcir_ext_evaluate(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                      size_t n_batches, uint32_t active_time, bcir_ext_eval_result *out);
int bcir_ext_loss_backward(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                           uint32_t active_time, int accumulate,
                           double *out_loss, uint64_t *out_sup_tokens);
int bcir_ext_train_loop(bcir_ext_model *m, const uint32_t *tokens, const uint32_t *targets,
                        const uint32_t *active_times_or_null, const double *lrs,
                        size_t steps, const bcir_adamw_params *opt_cfg,
                        bcir_ext_step_metrics *events);

int bcir_deltanet_verify_wy_vs_recurrent(uint32_t B, uint32_t T, uint32_t H, uint32_t dh,
                                         uint32_t C, double *out_max_abs_diff);
int bcir_mla_verify_absorption(uint32_t T, uint32_t H, uint32_t dh, uint32_t dc, uint32_t dr,
                               double *out_max_abs_diff);

#ifdef __cplusplus
}
#endif
#endif
