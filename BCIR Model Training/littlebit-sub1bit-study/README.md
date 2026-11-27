# BCIR Bare-Metal C LittleBit / LittleBit-2 Sub-1-Bit Study Bundle

Complete reproducible native-C implementation, Python driver harnesses, FP32 teacher checkpoints, raw JSON/text measurements, and diagnostic figures for the BCIR evaluation of **LittleBit** (Lee et al., NeurIPS 2025, `arXiv:2506.13771v5`) and **LittleBit-2** (`arXiv:2603.00042v2`, Joint-ITQ).

Companion BCIR study and roadmaps (pinned base BCIR commit `3fdb708d73371a6e0e1abf7dde70b7239fc2fc06`):
- `docs/research/BCIR_LITTLEBIT_SUB1BIT_STUDY_2026-11-22.md`
- `docs/research/BCIR_MODEL_ARCHITECTURE_ROADMAP_2026-11-21.md` (`COMP-1`, Structural Law 6)
- `docs/research/BCIR_TRAINING_STRATEGY_AND_OPTIMIZATION_ROADMAP_2026-11-21.md` (Part D, Priority 7)

## Directory Layout

| Path | Contents |
|---|---|
| `Makefile` | Portable C11 build (`-O3 -std=c11 -D_POSIX_C_SOURCE=200809L -march=native -ffp-contract=off`) linking directly against `$BCIR_REPO/runtime/c/bcir_tensor.c` and `bcir_decoder_train.c` |
| `src/bcir_littlebit.h` | Public C11 API for all 5 linear modes (`DENSE_FP32`, `LOWRANK_FP16`, `ONEBIT_FULL`, `LITTLEBIT_1P`, `LITTLEBIT_RES`), surrogates (`SmoothSign`, `Clipped-STE`, `Unclipped-STE`), objectives (`CE`, `KD_KL`, `KD_FULL`), and kernel microbenchmarks |
| `src/bcir_littlebit_core.c` | Bare-metal C forward, backward, KD/InterMSE, sign-flip / active-gradient telemetry, and AdamW trainer over `bcir_tensor.c` |
| `src/bcir_littlebit_bench.c` | Single-thread AVX-512 kernel benchmark suite (`PORTABLE_BITPACK`, `AVX512_SIGNXOR`, `AVX512_LUT4`, `AVX512_LUT4_PACKED1B`, `AVX512_LUT4_PACKED1B_MB4`, `DENSE_FP16_STREAM`, `DENSE_Q8_BCIR`, `LOWRANK_FP32`, `DENSE_FP32_BCIR`, `DENSE_BF16_BCIR`) with per-call FP32 reference parity verification |
| `src/bench_lut4_packed.c` | Standalone exact 1.0-bit/weight 2-nibbles-per-byte AVX-512 LUT4 decode microbenchmark (`N = 1`) |
| `src/lb_harness.py` | Python `ctypes` bridge, Dual-SVID / Joint-ITQ / OneBit / Q8 / fresh random-from-scratch initializers, exact `BCIRQ8` (`8.50 bpw`) deployment-size accounting, and the 4-gate verification suite (`verify_all`) |
| `src/pillar1_setup_and_bpw.py` | WikiText-2 byte split extraction (from `../gp-global/data/`), `wedge-pr818` teacher audit, and Pillar 1 scale/embedding/KV analytical bounds |
| `src/pillar2_teachers_and_spectra.py` | Native-C FP32 teacher training (`pilot192_0.86M`, `wide512_5.50M`) and 70-matrix spectral + Joint-ITQ conditioning audit |
| `src/pillar4_experiments.py` | Pillar 4 Suite A (WikiText-2 LM frontier), Suite B (`wedge128` LM / `kv_single` / `kv_curriculum`), and Suite C (controlled ablations A1–A7, including `A7_bcir_synthesis` and fresh random-init `A4_FromScratch_Random_CE`) |
| `src/suite_b_ordered_addendum.py` | Pillar 4 Suite B `ordered` and `ordered value_swap` counterfactual evaluation on `../wedge-pr818/checkpoints/ordered-attention-20261018.npz` |
| `src/pillar5_kernel_benchmark.py` | Pillar 5 single-thread kernel sweep (`N = 1..256`, reporting backend-specific actual `weight_bytes`/`realized_bpw` alongside `ideal_deploy_weight_bytes`/`ideal_deploy_bpw`) and 2-thread native-C QAT step-time / RAM overhead benchmark |
| `src/pillar5_packed_batched_bench.py` | Batched prefill & decode crossover benchmark comparing exact 1.0-bit/weight `packed1b_LUT4` and 4-token micro-batched `packed1b_LUT4_mb4` against `LowRank-FP32`, `BCIR FP32 kblock`, `BCIR BF16`, `Unpacked LUT4`, `FP16`, and `Q8 g32` across `wide512`, `wide50m`, `Llama2-7B`, and `Llama2-70B` |
| `src/make_littlebit_plots.py` | Regenerates `figures/bcir_littlebit_study_results.png` directly from `results/*.json` |
| `checkpoints/` | Trained FP32 BCIR teacher weights (`pilot192_0.86M.npz`: `861,888` params; `wide512_5.50M.npz`: legacy filename, exact `6,164,992` total params = `1,310,720` attn + `4,718,592` SwiGLU MLP + `133,120` tied embed + `2,560` norms; `wedge128_kv4_strong.npz`: `525,696` params) and JSON configs |
| `results/` | Raw JSON/text outputs from every pillar, `verify_gates_output.txt`, and `MANIFEST.json` with SHA-256 checksums for all code, checkpoints, `../wedge-pr818` teachers, and `../gp-global/data/` WikiText-2 splits |
| `figures/bcir_littlebit_study_results.png` | 6-panel summary figure (Panels A–F) |

## Reproducing the Verification Gates and Benchmarks

Prerequisites:
- A local checkout of `Mecca-Research/Binary-Correspondence-Intermediate-Representation` (tested at commit `3fdb708d73371a6e0e1abf7dde70b7239fc2fc06`)
- `clang` (tested with `clang 23.1.6`) or `gcc` on an AVX-512 x86-64 Linux host (non-AVX-512 x86-64 hosts build and run Gates 1, 2, and 4 cleanly while documenting a Gate 3 AVX-512 skip)
- Python 3.11–3.15 with `numpy` (`pyarrow` only if rebuilding `build/data/wikitext2_*.npy` from `../gp-global/data/*.parquet`; `matplotlib` for `make_littlebit_plots.py`)

```bash
cd "BCIR Model Training/littlebit-sub1bit-study"
export BCIR_REPO=/path/to/Binary-Correspondence-Intermediate-Representation

# 1. Build libbcir_littlebit.so, bcir_decoder_train.so, bench_lut4_packed and run Gates 1-4:
make verify

# 2. Run the batched Packed-1b LUT4 (token-major & mb4) prefill/decode crossover benchmark:
make bench-packed

# 3. Run standalone N=1 exact 1.0-bit/weight packed LUT4 decode benchmark:
./build/bench_lut4_packed

# 4. Prepare WikiText-2 byte splits from ../gp-global/data and run Pillar 1 analytical bounds:
python3 src/pillar1_setup_and_bpw.py

# 5. Re-run Pillar 2 spectral audit, Pillar 4 Suites A/B/C (including A7_bcir_synthesis), Pillar 5 kernels, and figure:
python3 src/pillar2_teachers_and_spectra.py
python3 src/pillar4_experiments.py --suite a_pilot
python3 src/pillar4_experiments.py --suite a_wide
python3 src/pillar4_experiments.py --suite b
python3 src/suite_b_ordered_addendum.py
python3 src/pillar4_experiments.py --suite c
python3 src/pillar5_kernel_benchmark.py
python3 src/make_littlebit_plots.py

# 6. Clean compiled artifacts:
make clean
```

## Provenance & Checksums

Full SHA-256 digests and exact byte sizes for all source files, `checkpoints/*.npz`, `results/*.json`, the saved `../wedge-pr818/checkpoints/*.npz` teachers, and `../gp-global/data/wikitext2-*.parquet` splits are recorded in [`results/MANIFEST.json`](results/MANIFEST.json). Measured AVX-512 kernel latencies in `results/pillar5_kernel_benchmark.json`, `results/bench_lut4_packed_batched.json`, and `results/verify_gates_output.txt` are preserved from Sapphire Rapids (`avx512f`/`avx512dq`), while `weight_bytes`/`realized_bpw` reflect each backend's exact runtime representation alongside `ideal_deploy_weight_bytes`/`ideal_deploy_bpw`.
