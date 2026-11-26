/*===- test_gate0_all.c - Gate 0 Numerical & AD Verifier for BCIR Lab -------===*/
#include "bcir_comp1_deploy.h"
#include "bcir_decoder_train.h"
#include "bcir_littlebit.h"
#include "bcir_mixer_ext.h"
#include "bcir_precision.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int run_fd_suite(const char *label, const bcir_ext_spec *spec_in,
                        const uint32_t *tokens, const uint32_t *targets, uint32_t active_T) {
  bcir_prec_policy pol; bcir_prec_policy_default(&pol);
  pol.fp64_guard_mode = BCIR_FP64_GUARD_ALWAYS;
  bcir_ext_model *m = bcir_ext_create(spec_in, &pol);
  if (!m) { printf("FAIL %s: create\n", label); return 1; }
  double loss0 = 0.0; uint64_t sup = 0u;
  if (bcir_ext_loss_backward(m, tokens, targets, active_T, 0, &loss0, &sup) != 0) {
    printf("FAIL %s: bwd\n", label); bcir_ext_destroy(m); return 1;
  }
  size_t np = bcir_ext_param_count(m);
  float *g_ana = (float *)malloc(np * 4u);
  memcpy(g_ana, bcir_ext_grads_fp32_ptr(m), np * 4u);
  float *w = bcir_ext_weights_ptr(m);
  size_t nt = bcir_ext_tensor_count(m), checked = 0u;
  double max_abs = 0.0, max_rel = 0.0, worst_ga = 0.0, worst_gn = 0.0;
  char worst_name[48] = "";
  for (size_t ti = 0; ti < nt; ++ti) {
    bcir_ext_tensor_info info; bcir_ext_tensor_info_at(m, ti, &info);
    size_t len = info.rows * info.cols;
    double eps = (strcmp(info.name, "embed") == 0) ? 1e-4 : 1e-3;
    size_t idxs[3] = { 0u, len / 2u, (len > 1u) ? (len - 1u) : 0u };
    int ncheck = (len >= 3u) ? 3 : (int)len;
    for (int ci = 0; ci < ncheck; ++ci) {
      size_t idx = info.offset + idxs[ci];
      float orig = w[idx]; double lp = 0.0, lm = 0.0; uint64_t s1 = 0u, s2 = 0u;
      w[idx] = (float)((double)orig + eps); bcir_ext_loss_backward(m, tokens, targets, active_T, 0, &lp, &s1);
      w[idx] = (float)((double)orig - eps); bcir_ext_loss_backward(m, tokens, targets, active_T, 0, &lm, &s2);
      w[idx] = orig;
      double gn = (lp - lm) / (2.0 * eps), ga = (double)g_ana[idx];
      double ae = fabs(ga - gn), re = ae / fmax(fmax(fabs(ga), fabs(gn)), 1e-3);
      if (ae > max_abs) max_abs = ae;
      if (re > max_rel) {
        max_rel = re; snprintf(worst_name, sizeof(worst_name), "%s[%zu]", info.name, idxs[ci]);
        worst_ga = ga; worst_gn = gn;
      }
      checked++;
    }
  }
  free(g_ana); bcir_ext_destroy(m);
  int pass = (max_rel < 2e-2 && max_abs < 2e-3);
  printf("  [FD] %-22s: checked=%3zu tensors=%2zu loss=%.4f max_abs=%.3e max_rel=%.3e (%s) -> %s\n",
         label, checked, nt, loss0, max_abs, max_rel, worst_name, pass ? "PASS" : "FAIL");
  (void)worst_ga; (void)worst_gn;
  return pass ? 0 : 1;
}

int main(void) {
  int fails = 0;
  printf("=== GATE 0: BCIR PRECISION & MIXER-EXT VERIFICATION ===\n");

  /* 0. VDPBF16PS portable vs AVX512_BF16 bit-exact parity */
  {
    size_t M = 64, N = 128, K = 96;
    float *A = (float *)malloc(M * K * 4u), *B = (float *)malloc(K * N * 4u);
    float *Cp = (float *)calloc(M * N, 4u), *Ca = (float *)calloc(M * N, 4u);
    uint64_t r = 42;
    for (size_t i = 0; i < M * K; ++i) { r = r * 6364136223846793005ULL + 1; A[i] = ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
    for (size_t i = 0; i < K * N; ++i) { r = r * 6364136223846793005ULL + 1; B[i] = ((float)(int32_t)(r >> 40) / (float)(1 << 23) - 1.0f); }
    bcir_mm_bf16_vdp_variant(NULL, 1, 0, 1, M, N, K, 1.0f, A, B, 0.0f, Cp, NULL, 0);
    bcir_mm_bf16_vdp_variant(NULL, 0, 0, 1, M, N, K, 1.0f, A, B, 0.0f, Ca, NULL, 0);
    size_t mm = 0u;
    for (size_t i = 0; i < M * N; ++i) { uint32_t u0, u1; memcpy(&u0, &Cp[i], 4); memcpy(&u1, &Ca[i], 4); if (u0 != u1) mm++; }
    printf("  [VDP] VDPBF16PS portable vs AVX512 (64x128x96):   bit_mismatches=%zu -> %s\n", mm, mm == 0 ? "PASS" : "FAIL");
    if (mm != 0) fails++;
    free(A); free(B); free(Cp); free(Ca);
  }

  double wy_diff = 0.0, mla_diff = 0.0;
  int rc_wy = bcir_deltanet_verify_wy_vs_recurrent(2, 73, 4, 16, 16, &wy_diff);
  printf("  [WY] DeltaNet WY vs exact recurrence (T=73 C=16): max_abs=%.3e -> %s\n", wy_diff, rc_wy == 0 ? "PASS" : "FAIL");
  if (rc_wy != 0) fails++;

  int rc_mla = bcir_mla_verify_absorption(32, 4, 16, 24, 8, &mla_diff);
  printf("  [MLA] Absorbed decode vs unabsorbed attention:    max_abs=%.3e -> %s\n", mla_diff, rc_mla == 0 ? "PASS" : "FAIL");
  if (rc_mla != 0) fails++;

  bcir_ext_spec sp; memset(&sp, 0, sizeof(sp));
  sp.vocab = 32; sp.width = 32; sp.heads = 4; sp.kvheads = 2; sp.layers = 2;
  sp.ff = 64; sp.batch = 2; sp.time = 16; sp.tied = 1; sp.rope_base = 10000.0; sp.rms_eps = 1e-6;
  sp.swa_window = 6; sp.delta_chunk = 8; sp.mla_dc = 16; sp.mla_dr = 4;
  sp.init_policy = BCIR_INIT_RESIDUAL_SCALED; sp.seed = 777; sp.threads = 2;
  for (uint32_t l = 0; l < 2; ++l) { sp.layer_mixer[l] = BCIR_MIXER_GQA; sp.layer_use_rope[l] = 1; }
  uint32_t tok[32], tgt[32];
  for (uint32_t i = 0; i < 32; ++i) { tok[i] = (i * 7 + 3) % 32; tgt[i] = (i * 11 + 5) % 32; }

  {
    bcir_prec_policy pol; bcir_prec_policy_default(&pol); pol.fp64_guard_mode = BCIR_FP64_GUARD_OFF;
    bcir_ext_model *em = bcir_ext_create(&sp, &pol);
    bcir_decoder_spec bsp = {32, 32, 4, 2, 2, 64, 2, 16, 1, 10000.0, 1e-6};
    bcir_decoder_plan bpl; bcir_decoder_make_plan(&bsp, &bpl);
    bcir_decoder_state bs; memset(&bs, 0, sizeof(bs)); bs.plan = bpl;
    bs.weights = (float *)calloc(bpl.parameters, 4); bs.gradients = (float *)calloc(bpl.parameters, 4);
    bs.moment1 = (float *)calloc(bpl.parameters, 4); bs.moment2 = (float *)calloc(bpl.parameters, 4);
    bs.activations = (float *)calloc(bpl.activations, 4); bs.scratch = (float *)calloc(bpl.scratch, 4);
    bs.weight_capacity = bs.gradient_capacity = bs.moment1_capacity = bs.moment2_capacity = bpl.parameters;
    bs.activation_capacity = bpl.activations; bs.scratch_capacity = bpl.scratch;
    bs.contract = BCIR_DT_CONTRACT_KBLOCK | BCIR_DT_CONTRACT_ATTN | BCIR_DT_CONTRACT_REDUCE;
    memcpy(bs.weights, bcir_ext_weights_ptr(em), bpl.parameters * 4);
    double le = 0.0, lb = 0.0; uint64_t sup = 0u;
    bcir_ext_loss_backward(em, tok, tgt, 16, 0, &le, &sup);
    bcir_decoder_loss_backward(&bs, tok, tgt, 32, 0, &lb);
    double gd = 0.0; float *ge = bcir_ext_grads_fp32_ptr(em);
    for (size_t i = 0; i < bpl.parameters; ++i) { double d = fabs((double)ge[i] - (double)bs.gradients[i]); if (d > gd) gd = d; }
    int ok = (fabs(le - lb) < 1e-6 && gd < 1e-5);
    printf("  [BCIR] GQA vs production bcir_decoder_train.c:    loss_diff=%.3e grad_diff=%.3e -> %s\n", fabs(le - lb), gd, ok ? "PASS" : "FAIL");
    if (!ok) fails++;
    free(bs.weights); free(bs.gradients); free(bs.moment1); free(bs.moment2); free(bs.activations); free(bs.scratch);
    bcir_ext_destroy(em);
  }

  fails += run_fd_suite("GQA (RoPE)", &sp, tok, tgt, 16);
  sp.layer_mixer[0] = sp.layer_mixer[1] = BCIR_MIXER_SWA;
  fails += run_fd_suite("SWA (W=6, RoPE)", &sp, tok, tgt, 16);
  sp.layer_mixer[0] = sp.layer_mixer[1] = BCIR_MIXER_DELTANET; sp.layer_use_rope[0] = sp.layer_use_rope[1] = 0;
  fails += run_fd_suite("Arch-1 DeltaNet (C=8)", &sp, tok, tgt, 16);
  sp.layers = 3;
  sp.layer_mixer[0] = BCIR_MIXER_SWA; sp.layer_use_rope[0] = 1;
  sp.layer_mixer[1] = BCIR_MIXER_DELTANET; sp.layer_use_rope[1] = 0;
  sp.layer_mixer[2] = BCIR_MIXER_SWA; sp.layer_use_rope[2] = 1;
  fails += run_fd_suite("Arch-2 Hybrid-SWA", &sp, tok, tgt, 16);
  sp.layers = 2; sp.layer_mixer[0] = sp.layer_mixer[1] = BCIR_MIXER_MLA; sp.layer_use_rope[0] = sp.layer_use_rope[1] = 1;
  fails += run_fd_suite("Arch-4 MLA (dc=16,dr=4)", &sp, tok, tgt, 16);

  /* TRAIN-OPT-2 & Thread parity & Optimizer-State Compression */
  sp.layers = 3;
  sp.layer_mixer[0] = BCIR_MIXER_SWA; sp.layer_use_rope[0] = 1;
  sp.layer_mixer[1] = BCIR_MIXER_DELTANET; sp.layer_use_rope[1] = 0;
  sp.layer_mixer[2] = BCIR_MIXER_MLA; sp.layer_use_rope[2] = 1;
  {
    bcir_prec_policy pol; bcir_prec_policy_default(&pol);
    bcir_ext_model *em = bcir_ext_create(&sp, &pol);
    uint32_t all_ign[32]; for (int i = 0; i < 32; ++i) all_ign[i] = BCIR_EXT_IGNORE;
    double l_bad = 0.0; uint64_t s_bad = 0;
    int rc_ign = bcir_ext_loss_backward(em, tok, all_ign, 16, 0, &l_bad, &s_bad);
    uint32_t tgt_pad[32], tok_trim[20], tgt_trim[20];
    for (int b = 0; b < 2; ++b) for (int t = 0; t < 16; ++t) {
      tgt_pad[b * 16 + t] = (t < 10) ? tgt[b * 16 + t] : BCIR_EXT_IGNORE;
      if (t < 10) { tok_trim[b * 10 + t] = tok[b * 16 + t]; tgt_trim[b * 10 + t] = tgt[b * 16 + t]; }
    }
    double lp = 0.0, lt = 0.0; uint64_t sp1 = 0, sp2 = 0;
    bcir_ext_loss_backward(em, tok, tgt_pad, 16, 0, &lp, &sp1);
    size_t np = bcir_ext_param_count(em); float *gp = (float *)malloc(np * 4);
    memcpy(gp, bcir_ext_grads_fp32_ptr(em), np * 4);
    bcir_ext_loss_backward(em, tok_trim, tgt_trim, 10, 0, &lt, &sp2);
    double gd = 0.0; float *gt = bcir_ext_grads_fp32_ptr(em);
    for (size_t i = 0; i < np; ++i) { double d = fabs((double)gp[i] - (double)gt[i]); if (d > gd) gd = d; }
    int ok = (rc_ign == 1 && sp1 == 20u && sp2 == 20u && fabs(lp - lt) < 1e-6 && gd < 1e-5);
    printf("  [OPT2] All-masked refuse=%d SuffixTrim(10/16) loss_diff=%.3e grad_diff=%.3e -> %s\n", rc_ign, fabs(lp - lt), gd, ok ? "PASS" : "FAIL");
    if (!ok) fails++;
    free(gp); bcir_ext_destroy(em);
  }
  {
    bcir_prec_policy pol; bcir_prec_policy_default(&pol);
    sp.threads = 1; bcir_ext_model *m1 = bcir_ext_create(&sp, &pol);
    sp.threads = 2; bcir_ext_model *m2 = bcir_ext_create(&sp, &pol);
    bcir_adamw_params opt = {1e-3, 0.9, 0.95, 1e-8, 0.01, 1.0};
    bcir_ext_step_metrics e1, e2;
    bcir_ext_train_loop(m1, tok, tgt, NULL, NULL, 1, &opt, &e1);
    bcir_ext_train_loop(m2, tok, tgt, NULL, NULL, 1, &opt, &e2);
    size_t bd = 0u, np = bcir_ext_param_count(m1);
    float *w1 = bcir_ext_weights_ptr(m1), *w2 = bcir_ext_weights_ptr(m2);
    for (size_t i = 0; i < np; ++i) { uint32_t u1, u2; memcpy(&u1, &w1[i], 4); memcpy(&u2, &w2[i], 4); if (u1 != u2) bd++; }
    printf("  [THR] 1T vs 2T Hybrid (SWA+DeltaNet+MLA):         bit_diffs=%zu -> %s\n", bd, bd == 0 ? "PASS" : "FAIL");
    if (bd != 0) fails++;
    bcir_ext_destroy(m1); bcir_ext_destroy(m2);
    int okinds[5] = {BCIR_OPT_FP32, BCIR_OPT_Q8_MOMENTS, BCIR_OPT_Q8_MOMENTS_BF16_GRAD,
                     BCIR_OPT_ADAFACTOR_M2, BCIR_OPT_ADAFACTOR_BF16_GRAD};
    uint32_t tok5[5 * 32], tgt5[5 * 32];
    for (int s = 0; s < 5; ++s) { memcpy(tok5 + s * 32, tok, 128); memcpy(tgt5 + s * 32, tgt, 128); }
    for (int oi = 0; oi < 5; ++oi) {
      bcir_prec_policy op; bcir_prec_policy_default(&op); op.opt_kind = okinds[oi];
      bcir_ext_model *om = bcir_ext_create(&sp, &op);
      bcir_ext_mem_stats ms; bcir_ext_get_mem_stats(om, &ms);
      bcir_ext_step_metrics evs[5];
      int rc = bcir_ext_train_loop(om, tok5, tgt5, NULL, NULL, 5, &opt, evs);
      int ok = (rc == 0 && evs[4].loss < evs[0].loss && evs[4].nonfinite_events == 0);
      printf("  [OPT7] %-28s: %5.2f B/param  loss %.4f -> %.4f -> %s\n",
             bcir_prec_opt_name(okinds[oi]), (double)ms.persistent_bytes / (double)ms.n_params,
             evs[0].loss, evs[4].loss, ok ? "PASS" : "FAIL");
      if (!ok) fails++;
      bcir_ext_destroy(om);
    }

    /* [MEM] Streaming BF16-grad workspace accounting: no hidden full-model FP32 grad buffer */
    {
      bcir_prec_policy op; bcir_prec_policy_default(&op); op.opt_kind = BCIR_OPT_Q8_MOMENTS_BF16_GRAD;
      bcir_ext_model *om = bcir_ext_create(&sp, &op);
      bcir_ext_mem_stats ms; bcir_ext_get_mem_stats(om, &ms);
      size_t sum_b = ms.persistent_bytes + ms.activation_bytes + ms.mixer_prob_bytes + ms.scratch_bytes;
      int no_full_fp32 = (bcir_ext_grads_fp32_ptr(om) == NULL);
      int ok = (no_full_fp32 && ms.persistent_bytes == ms.weight_bytes + ms.opt_only_bytes &&
                ms.total_bytes == sum_b && ms.scratch_bytes > 0u);
      printf("  [MEM] BF16-grad streaming (no full FP32 buf=%d):  pers=%zuB scr=%zuB tot=%zuB -> %s\n",
             no_full_fp32, ms.persistent_bytes, ms.scratch_bytes, ms.total_bytes, ok ? "PASS" : "FAIL");
      if (!ok) fails++;
      bcir_ext_destroy(om);
    }

    /* [STAT] & [NF] Deploy stats match resident FP32/FP64 storage & fail closed on nonfinite weights */
    {
      bcir_prec_policy pol; bcir_prec_policy_default(&pol);
      bcir_ext_model *em = bcir_ext_create(&sp, &pol);
      bcir_deploy_model *dm = bcir_deploy_create(&sp, 32);
      bcir_deploy_load_from_ext(dm, em, BCIR_DEPLOY_BCIRQ8_G32, BCIR_DEPLOY_BCIRQ8_G32);
      bcir_deploy_stats st; bcir_deploy_get_stats(dm, &st);
      int stat_ok = (st.nonlin_bytes == st.projected_fp16_nonlin_bytes * 2u &&
                     st.kv_cache_bytes_at_4096 == st.projected_fp16_kv_bytes_at_4096 * 2u &&
                     st.rec_state_bytes == st.projected_fp32_rec_state_bytes * 2u &&
                     st.total_artifact_bytes > st.projected_fp16_artifact_bytes);
      printf("  [STAT] Deploy resident FP32/FP64 vs proj FP16:    nonlin=%zuB kv4k=%zuB rec=%zuB -> %s\n",
             st.nonlin_bytes, st.kv_cache_bytes_at_4096, st.rec_state_bytes, stat_ok ? "PASS" : "FAIL");
      if (!stat_ok) fails++;

      /* Inject NaN into first linear tensor and confirm fail-closed evaluation */
      size_t nt = bcir_ext_tensor_count(em), first_lin = nt;
      bcir_ext_tensor_info ti;
      for (size_t i = 0; i < nt; ++i) {
        bcir_ext_tensor_info_at(em, i, &ti);
        if (ti.region == 1) { first_lin = i; break; }
      }
      float *bad_w = (float *)malloc(ti.rows * ti.cols * 4u);
      for (size_t i = 0; i < ti.rows * ti.cols; ++i) bad_w[i] = NAN;
      bcir_deploy_set_linear_dense(dm, first_lin, BCIR_DEPLOY_FP32, bad_w);
      bcir_ext_eval_result ev_nf;
      int rc_nf = bcir_deploy_evaluate(dm, tok, tgt, 1, sp.batch, sp.time, sp.time, 0, &ev_nf);
      int nf_ok = (rc_nf == 3 && ev_nf.nonfinite > 0u && isnan(ev_nf.ce));
      printf("  [NF] Deploy fail-closed on NaN weights:           rc=%d nf=%llu isnan=%d -> %s\n",
             rc_nf, (unsigned long long)ev_nf.nonfinite, isnan(ev_nf.ce), nf_ok ? "PASS" : "FAIL");
      if (!nf_ok) fails++;
      free(bad_w); bcir_deploy_destroy(dm); bcir_ext_destroy(em);
    }

    /* [FRZ] LittleBit QAT freeze_dense_projs preserves dense Attention bit-exact while updating MLP */
    {
      lb_model_cfg lc; memset(&lc, 0, sizeof(lc));
      lc.vocab = 32; lc.width = 32; lc.heads = 4; lc.kvheads = 2; lc.layers = 1; lc.ff = 64;
      lc.batch = 2; lc.time = 8; lc.tied = 1; lc.rope_base = 10000.0; lc.rms_eps = 1e-6;
      lc.surrogate = LB_SURR_CLIPPED_STE; lc.smooth_k = 1.0; lc.objective = LB_OBJ_CE;
      lc.threads = 2; lc.use_kblock = 1;
      uint32_t d = 32, dkv = 16, ff = 64;
      uint32_t shapes[7][2] = {{d, d}, {dkv, d}, {dkv, d}, {d, d}, {ff, d}, {ff, d}, {d, ff}};
      lb_proj_cfg pcs[7];
      for (int p = 0; p < 7; ++p) {
        memset(&pcs[p], 0, sizeof(pcs[p]));
        pcs[p].mode = (p < 4) ? LB_MODE_DENSE_FP32 : LB_MODE_LITTLEBIT_RES;
        pcs[p].din = shapes[p][1]; pcs[p].dout = shapes[p][0];
        pcs[p].r1 = (p < 4) ? 0u : 4u; pcs[p].r2 = (p < 4) ? 0u : 4u;
        pcs[p].scale_flags = LB_SCALE_ALL; pcs[p].fp16_round = 1;
      }
      lb_model *lm = lb_model_create(&lc, pcs);
      size_t np_lb = lb_model_param_count(lm);
      float *wl = lb_model_weights_ptr(lm);
      uint64_t rng = 1234567ULL;
      for (size_t i = 0; i < np_lb; ++i) {
        rng = rng * 6364136223846793005ULL + 1ULL;
        wl[i] = ((float)(int32_t)(rng >> 40) / (float)(1 << 23) - 1.0f) * 0.1f;
      }
      float *snap = (float *)malloc(np_lb * 4u);
      memcpy(snap, wl, np_lb * 4u);
      double lrs2[2] = {1e-2, 1e-2};
      lb_adamw_cfg aopt = {1e-2, 1e-2, 0.9, 0.95, 1e-8, 0.0, 1.0, 1, 1};
      lb_step_metrics ev2[2];
      uint32_t tok2[32], tgt2[32];
      for (int i = 0; i < 32; ++i) { tok2[i] = tok[i]; tgt2[i] = tgt[i]; }
      int rc_lb = lb_model_train_loop(lm, NULL, tok2, tgt2, 2, lrs2, &aopt, ev2);
      size_t attn_diffs = 0u, mlp_diffs = 0u;
      for (int p = 0; p < 4; ++p) {
        size_t off = 0, cnt = 0; lb_model_tensor_span(lm, 20, 0, p, 0, &off, &cnt);
        for (size_t k = 0; k < cnt; ++k) if (memcmp(&wl[off + k], &snap[off + k], 4) != 0) attn_diffs++;
      }
      for (int p = 4; p < 7; ++p) {
        size_t off = 0, cnt = 0; lb_model_tensor_span(lm, 20, 0, p, 0, &off, &cnt);
        for (size_t k = 0; k < cnt; ++k) if (memcmp(&wl[off + k], &snap[off + k], 4) != 0) mlp_diffs++;
      }
      int frz_ok = (rc_lb == 0 && attn_diffs == 0u && mlp_diffs > 0u);
      printf("  [FRZ] LB QAT freeze_dense_projs (Q8 Attn intact): attn_diffs=%zu mlp_diffs=%zu -> %s\n",
             attn_diffs, mlp_diffs, frz_ok ? "PASS" : "FAIL");
      if (!frz_ok) fails++;
      free(snap); lb_model_destroy(lm);
    }

    /* [BENCH] Matched-thread kernel bench (N=1 row-split & N=16 token-split) + actual vs ideal bytes */
    {
      lb_bench_result br1, br16;
      int rc1 = lb_bench_linear_kernel(LB_KERN_AVX512_LUT4_PACKED1B, 1, 128, 256, 32, 2, 2, 3, &br1);
      int rc16 = lb_bench_linear_kernel(LB_KERN_AVX512_LUT4_PACKED1B, 16, 128, 256, 32, 2, 2, 3, &br16);
#if defined(__AVX512F__) && defined(__AVX512DQ__)
      int b_ok = (rc1 == 0 && rc16 == 0 && br1.max_abs_err < 1e-4 && br16.max_abs_err < 1e-4 &&
                  br1.weight_bytes > br1.ideal_deploy_weight_bytes);
      printf("  [BENCH] 2T Packed1b N=1/N=16 actual vs ideal B:   act=%zuB ideal=%zuB err=%.2e -> %s\n",
             br1.weight_bytes, br1.ideal_deploy_weight_bytes, fmax(br1.max_abs_err, br16.max_abs_err),
             b_ok ? "PASS" : "FAIL");
      if (!b_ok) fails++;
#else
      int b_ok = (rc1 == -1 && rc16 == -1);
      printf("  [BENCH] AVX-512 kernel bench (no AVX-512 host):   rc1=%d rc16=%d -> %s (SKIP-EXEC)\n",
             rc1, rc16, b_ok ? "PASS" : "FAIL");
      if (!b_ok) fails++;
#endif
    }
  }
  printf("\nGATE 0 VERDICT: %s (fails=%d)\n", fails == 0 ? "ALL PASSED" : "FAILED", fails);
  return fails;
}
