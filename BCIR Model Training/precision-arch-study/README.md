# BCIR Precision Policy (P1-P6), TRAIN-OPT-1/2/4/6/7, MIXER-EXT, and Whole-Model COMP-1 Study

Self-contained bare-metal C11 + Python ctypes reproducibility bundle for the BCIR floating-point precision frontier (up to `50.54M`), optimizer-state compression, separate Attention/MLP quantization-policy regions, `TRAIN-OPT-1/2/4/6/7`, `MIXER-EXT` sequence mixers (`GQA`, `SWA`, `Arch-1 DeltaNet`, `Arch-2 Hybrid-SWA`, `Arch-2 Hybrid-DN+GQA`, `Arch-4 MLA` with decoupled RoPE), and whole-model `COMP-1` deployment.

## Host & Provenance

- CPU: `Intel(R) Xeon(R) 6985P-C` (2 cores, `avx512f`, `avx512_bf16`, `avx512_fp16`, `avx512_vnni`, no AMX exposed)
- Compiler: `clang 23.1.6` (`-O3 -std=c11 -march=native -ffp-contract=off -Wall -Wextra -Werror`)
- BCIR runtime base commit: `15c7287e93a968795cf27c31caa7ee5e79cd48f9` (`runtime/c/bcir_tensor.c`, `runtime/c/bcir_decoder_train.c`)
- Dataset: strictly separated WikiText-2 raw-byte splits from `../gp-global/data/` (`train`, `validation`, untouched `test`), plus synthetic associative-recall / counterfactual / length-extrapolation circuits (`kv_single`, `kv_4pair`, `value_swap`, `query_change`, `long_filler24`, `ordered_6pair`, `ordered_8pair`) and `checkpoints/wedge128_kv4_strong.npz`.

## Build & Gate 0 Verification

```bash
make
make verify | tee results/gate0_verify_output.txt
```

Gate 0 (`src/test_gate0_all.c`) verifies 20 numerical, AD, memory-accounting, and policy invariants before any training run:

1. `VDPBF16PS` portable C11 vs `AVX512_BF16` hardware bit-exact parity (`bit_mismatches=0`)
2. `Arch-1 DeltaNet` chunkwise WY (`FP64`) vs exact token-by-token recurrence (`max_abs=0.000e+00`)
3. `Arch-4 MLA` absorbed decode vs unabsorbed attention (`max_abs=2.067e-08`)
4. `GQA` forward/backward bit-exact parity vs production `bcir_decoder_train.c` (`loss_diff=0.000e+00`, `grad_diff=0.000e+00`)
5. Central finite-difference AD checks across all 5 mixer families (`GQA`, `SWA`, `DeltaNet`, `Hybrid-SWA`, `MLA`)
6. `TRAIN-OPT-2` all-masked refusal and physical suffix-trimming equivalence (`loss_diff=0.000e+00`, `grad_diff=0.000e+00`)
7. 1-thread vs 2-thread bit-exact hybrid training parity (`bit_diffs=0`)
8. All 5 `TRAIN-OPT-7` optimizer-state compression contracts (`16.00` down to `8.05 B/param`)
9. Streaming `BF16`-grad workspace accounting (`[MEM]`): no hidden full-model FP32 gradient buffer; single-tensor `grad_tile` (`6.0 MiB` at 50M) explicitly included in `scratch_bytes` and `total_bytes`
10. Deployment resident-precision accounting (`[STAT]`): `nonlin_bytes`, `kv_cache_bytes_at_*`, and `rec_state_bytes` reflect actual evaluated `FP32` non-linear/KV and `FP64` DeltaNet buffers, with hypothetical `FP16`/`FP32` compressed projections reported in separate `projected_fp16_*` / `projected_fp32_*` fields
11. Fail-closed nonfinite deployment evaluation (`[NF]`): nonfinite steps/logits/CE return `rc=3`, `ce=NaN`, and cannot pass precision gates
12. Frozen dense Q8 attention during LittleBit KD-QAT (`[FRZ]`): `freeze_dense_projs=1` keeps Q8-quantized Attention projections bit-identical throughout `COMP-1` QAT (`exp3 --part c` and `exp5 --part wide512`)
13. Matched-thread kernel benchmark dispatch (`[BENCH]`): all `bcir_littlebit_bench.c` kernels run at the exact requested thread count via `bcir_tensor_pool_run` and report actual runtime `weight_bytes`/`realized_bpw` separately from `ideal_deploy_weight_bytes`/`ideal_deploy_bpw`

## Reproducing All 5 Experiment Suites

Use a Python environment with `numpy`, `pyarrow`, and `matplotlib`:

```bash
# Experiment 1: TRAIN-OPT-4 init, TRAIN-OPT-2 suffix trimming, TRAIN-OPT-1 across 0.86M / 6.16M / 50.54M
python src/exp1_train_opt_1_4.py --part small
python src/exp1_train_opt_1_4.py --part 50m
python src/exp1_train_opt_1_4.py --part merge

# Experiment 2: P1-P6 precision frontier & optimizer-state vs compute precision (0.86M / 6.16M / 50.54M)
python src/exp2_precision_frontier.py --part micro
python src/exp2_precision_frontier.py --part a_pilot
python src/exp2_precision_frontier.py --part a_wide1
python src/exp2_precision_frontier.py --part a_wide2
python src/exp2_precision_frontier.py --part b1
python src/exp2_precision_frontier.py --part b2a
python src/exp2_precision_frontier.py --part b2b
python src/exp2_precision_frontier.py --part b3a
python src/exp2_precision_frontier.py --part b3b
python src/exp2_precision_frontier.py --part merge

# Experiment 3: Matched-param 6.16M geometry, 50.54M COMP-1 deployment, Packed-1b LUT4 crossover, wedge128 circuits
python src/exp3_littlebit_comp1_50m.py --part a
python src/exp3_littlebit_comp1_50m.py --part b
python src/exp3_littlebit_comp1_50m.py --part c
python src/exp3_littlebit_comp1_50m.py --part merge

# Experiment 4: MIXER-EXT KV circuits (ordered_8pair at T=96), multi-query phase transition, wide10m LM, and 2x2 COMP-1 x KV orthogonality
python src/exp4_mixer_ext_architectures.py --part a
python src/exp4_mixer_ext_architectures.py --part a2
python src/exp4_mixer_ext_architectures.py --part b1
python src/exp4_mixer_ext_architectures.py --part b2
python src/exp4_mixer_ext_architectures.py --part merge

# Regenerate both 6-panel figures
python src/make_study_plots.py

# Experiment 5: Production BCIR Deployment Policy (COMP-1 / P1-P6) & Permanent Baseline Checkpoint
# Links directly against BCIR runtime/c/{bcir_q8_model,bcir_decode,bcir_ai_kernels,bcir_llama,bcir_tensor}.c
python src/exp5_deploy_policy_baseline.py --part kernels
python src/exp5_deploy_policy_baseline.py --part wide512 --wide512-steps 160 --wide512-qat-steps 80
python src/exp5_deploy_policy_baseline.py --part wide50m --wide50m-steps 60
python src/exp5_deploy_policy_baseline.py --part wedge128
python src/exp5_deploy_policy_baseline.py --part merge
```

Note: `checkpoints/wide50m_fp32_120st.npz` (`193 MiB`) exceeds GitHub's `100 MiB` single-file limit and is omitted from git tracking; its exact SHA-256 digest is recorded in `results/MANIFEST.json` and it reproduces deterministically via `python src/exp2_precision_frontier.py --part b1`. Experiment 5's `results/deploy_policy_baseline_checkpoint.json` (`bcir.deploy_baseline.v1` schema), along with refreshed `exp3 --part c`, `exp4 --part a` (`ordered_8pair` at `T=96`), and `exp4 --part a2` (all 4 queries on `mq_filler20_gtW`), were executed on a 2-core `Intel(R) Xeon(R) CPU @ 2.30GHz` (`avx2_fma`, no `avx512f`) host with `clang 23.1.6`; historical Xeon 6985P-C AVX-512 crossover latencies in `exp3_part_b.json` carry an explicit `crossover_thread_count_note` documenting the pre-fix 1T-vs-2T mismatch while preserving the measured timings without fabrication.

## Directory Layout

- `src/`: Bare-metal C11 precision, `MIXER-EXT`, `COMP-1` deploy, LittleBit QAT/kernel engines, `bcir_native_deploy_bench.c` (linked directly to BCIR production `runtime/c/`), and Python drivers (`exp1`–`exp5`)
- `results/`: Merged JSON artifacts, slice JSONs, `deploy_policy_baseline_checkpoint.json` (`bcir.deploy_baseline.v1`), `gate0_verify_output.txt`, and SHA-256 `MANIFEST.json`
- `checkpoints/`: Saved FP32 checkpoints (`wedge128_kv4_strong.npz`, `wide512_fp32_s42.npz`, `narrow_deep_6m_fp32_s42.npz`)
- `figures/`: Publication figures (`bcir_precision_frontier_50m.png`, `bcir_mixer_ext_architectures.png`)
