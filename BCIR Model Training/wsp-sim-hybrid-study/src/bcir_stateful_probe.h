/*===- bcir_stateful_probe.h - Incremental Decode, Activation Probe & SSP-Live ---===
 * Study-local ISO C11 companion to BCIR's unmodified production ABI 6 runtime
 * (bcir_mixer_ext.h). Reads weights and tensor metadata directly from any live
 * bcir_ext_model instance via public ABI 6 getters and provides:
 *   1. Exact single-token stateful decoding (GDN-2, DeltaNet, NoPE-MLA, GQA,
 *      G1 headwise gates, QK-Norm, Canon-AC, DeepSeek mHC C=2) verified to
 *      < 1e-5 max abs logit diff against bcir_ext_forward.
 *   2. 100% gradient-free layer-wise internal activation capture (residual stream,
 *      SwiGLU hidden activations, G1 head gates, GDN-2 decay/erase/write gates,
 *      recurrent state norm/echo cosine, and global attention repeat-copy mass).
 *   3. Live State-Space Programming (SSP-Live) runtime hooks (adaptive GDN-2
 *      decay/erase shifts, state-echo damping, MLA repeat-position logit bias,
 *      G1 runtime bias, and decoding repetition/presence penalty) with zero
 *      weight mutation.
 *===---------------------------------------------------------------------------===*/
#ifndef BCIR_STATEFUL_PROBE_H
#define BCIR_STATEFUL_PROBE_H

#include "bcir_mixer_ext.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BCIR_PROBE_MAX_HEADS 32u

typedef struct bcir_ssp_config {
  /* Tier 1A: Live GDN-2 / DeltaNet recurrent state-space interventions (zero weight edit) */
  float gdn_alpha_shift;        /* additive logit shift on alpha gate (negative = faster decay) */
  float gdn_erase_shift;        /* additive logit shift on erase gate (positive = stronger erase) */
  float gdn_write_shift;        /* additive logit shift on write gate */
  float state_echo_damp;        /* fraction [0, 0.9] to damp S_t when n-gram echo triggers */
  uint32_t adaptive_echo_only;  /* 0 = apply always during gen; 1 = apply only when echo detected */
  uint32_t echo_ngram;          /* n-gram size for online echo detection (default 4) */
  uint32_t echo_window;         /* lookback window for online echo detection (default 48) */
  /* Tier 1B: Live Global Attention (NoPE-MLA / GQA) & G1 runtime interventions */
  float mla_repeat_logit_bias;  /* additive logit bias on attention keys whose token matched recent echo */
  float g1_head_bias[BCIR_PROBE_MAX_HEADS]; /* runtime additive bias to L_{last} G1 head logits */
  uint32_t enable_g1_bias;      /* 0 or 1 */
  /* Tier 1C: Decoding-harness controls */
  float rep_penalty;            /* >= 1.0 multiplicative penalty on tokens in rep_window (1.0 = off) */
  float presence_penalty;       /* >= 0.0 additive logit penalty for tokens in rep_window */
  float ngram_block_penalty;    /* >= 0.0 additive penalty on token completing a repeated n-gram */
  uint32_t rep_window;          /* lookback tokens for rep/presence penalty */
  uint32_t max_pos_cap;         /* 0 = default max_seq */
} bcir_ssp_config;

typedef struct bcir_probe_step_out {
  /* Caller-allocated optional buffers (NULL = skip capture):
   *   resid_post_mlp : [layers * d]
   *   mlp_hidden     : [layers * ff]
   *   g1_gates       : [layers * H]
   *   gdn_alpha      : [layers * d]
   *   gdn_erase      : [layers * d]
   *   gdn_write      : [layers * d]
   *   state_norm     : [layers * H]
   *   state_cos_echo : [layers * H]
   *   attn_repeat    : [layers * H]  (attention mass on earlier positions matching prev token)
   *   attn_entropy   : [layers * H]
   */
  float *resid_post_mlp;
  float *mlp_hidden;
  float *g1_gates;
  float *gdn_alpha;
  float *gdn_erase;
  float *gdn_write;
  float *state_norm;
  float *state_cos_echo;
  float *attn_repeat;
  float *attn_entropy;
} bcir_probe_step_out;

typedef struct bcir_stateful_ctx bcir_stateful_ctx;

BCIR_DT_API bcir_stateful_ctx *bcir_stateful_create(const bcir_ext_model *m,
                                                    const bcir_ext_spec *spec,
                                                    uint32_t max_seq);
BCIR_DT_API void bcir_stateful_destroy(bcir_stateful_ctx *ctx);
BCIR_DT_API void bcir_stateful_reset(bcir_stateful_ctx *ctx);
BCIR_DT_API void bcir_stateful_sync_weights(bcir_stateful_ctx *ctx, const bcir_ext_model *m);

/* Advance one token, writing [vocab] logits and optional layer activation probes. */
BCIR_DT_API int bcir_stateful_step(bcir_stateful_ctx *ctx,
                                   uint32_t token_id,
                                   int in_generation,
                                   const bcir_ssp_config *ssp,
                                   float *out_logits,
                                   bcir_probe_step_out *probe);

/* Fast C greedy/SSP batch rollout:
 *   prompts     : [n_prompts * prompt_len]
 *   out_tokens  : [n_prompts * (prompt_len + gen_len)]
 *   out_entropy : [n_prompts * gen_len] (optional, NULL ok)
 *   out_echo    : [n_prompts * gen_len] (optional uint8: 1 if generated token triggers echo)
 */
BCIR_DT_API int bcir_stateful_generate_batch(bcir_stateful_ctx *ctx,
                                             const uint32_t *prompts,
                                             size_t n_prompts,
                                             uint32_t prompt_len,
                                             uint32_t gen_len,
                                             const bcir_ssp_config *ssp,
                                             uint32_t *out_tokens,
                                             float *out_entropy,
                                             uint8_t *out_echo);

/* Exact activation-contrast probe over a batch of labeled sequences:
 * For each sequence i and position t in [prompt_len, seq_len-1], accumulates:
 *   - loop_count / clean_count based on mask_loop[i * seq_len + t] (1=loop, 0=clean, 255=ignore)
 *   - mean residual stream [L * d] for loop vs clean
 *   - mean SwiGLU hidden  [L * ff] for loop vs clean
 *   - mean G1 gates       [L * H]  for loop vs clean
 *   - mean GDN2 alpha/erase/write [L * d] for loop vs clean
 *   - mean state_cos_echo & attn_repeat [L * H] for loop vs clean
 *   - per-column SwiGLU hidden * residual projection accumulator (computed in Python from exact means,
 *     plus exact per-step covariance sum_t (a_{l,j}(t) * x_l(t)) not needed if we also return
 *     loop_mlp_act_abs [L * ff] and clean_mlp_act_abs [L * ff] and exact column scores via 2nd pass!).
 */
typedef struct bcir_contrast_accum {
  uint64_t loop_tokens;
  uint64_t clean_tokens;
  /* All buffers allocated by caller as double arrays:
   *   resid_loop, resid_clean             : [L * d]
   *   mlp_hid_loop, mlp_hid_clean         : [L * ff]
   *   mlp_hid_abs_loop, mlp_hid_abs_clean : [L * ff]
   *   g1_loop, g1_clean                   : [L * H]
   *   alpha_loop, alpha_clean             : [L * d]
   *   erase_loop, erase_clean             : [L * d]
   *   write_loop, write_clean             : [L * d]
   *   st_echo_loop, st_echo_clean         : [L * H]
   *   attn_rep_loop, attn_rep_clean       : [L * H]
   */
  double *resid_loop, *resid_clean;
  double *mlp_hid_loop, *mlp_hid_clean;
  double *mlp_hid_abs_loop, *mlp_hid_abs_clean;
  double *g1_loop, *g1_clean;
  double *alpha_loop, *alpha_clean;
  double *erase_loop, *erase_clean;
  double *write_loop, *write_clean;
  double *st_echo_loop, *st_echo_clean;
  double *attn_rep_loop, *attn_rep_clean;
} bcir_contrast_accum;

BCIR_DT_API int bcir_stateful_probe_contrast(bcir_stateful_ctx *ctx,
                                             const uint32_t *seqs,
                                             const uint8_t *mask_loop,
                                             size_t n_seqs,
                                             uint32_t seq_len,
                                             bcir_contrast_accum *acc);

#ifdef __cplusplus
}
#endif
#endif
