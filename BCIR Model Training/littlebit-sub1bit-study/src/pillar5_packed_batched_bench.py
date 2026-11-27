"""Batched prefill & decode benchmark for exact 1.0-bit/weight 2-nibbles-per-byte
AVX-512 LUT4 (`packed1b_LUT4` and `packed1b_LUT4_mb4`) vs unpacked LUT4, LowRank-FP32,
BCIR FP32 kblock, BCIR BF16, FP16 stream, and Q8 g32 on Sapphire Rapids (1 thread).

Directly establishes the shape-dependent crossover N*(din, dout, r, backend) between
packed LUT4-LANES and blocked GEMM fallback, and asserts Gate-3 numerical parity
against exact FP32 reference across every shape and batch size.
"""
import ctypes as ct
import json
import os
import platform
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lb_harness import (
    get_lib, LBBenchResult, solve_lb_rank,
    LB_KERN_DENSE_FP32_BCIR, LB_KERN_DENSE_BF16_BCIR, LB_KERN_DENSE_FP16_STREAM,
    LB_KERN_DENSE_Q8_BCIR, LB_KERN_LOWRANK_FP32,
    LB_KERN_AVX512_LUT4, LB_KERN_AVX512_LUT4_PACKED1B, LB_KERN_AVX512_LUT4_PACKED1B_MB4,
)


def main():
    lib = get_lib()

    # Gate-3 strict parity check on non-multiple and production shapes
    print("=== GATE 3: PACKED-1B LUT4 EXACT NUMERICAL PARITY CHECK ===")
    for (din, dout, r, N) in [(130, 258, 13, 1), (130, 258, 13, 7), (512, 1536, 41, 16), (768, 2048, 67, 64)]:
        for kname, kkind in [
            ("unpacked_lut4", LB_KERN_AVX512_LUT4),
            ("packed1b_lut4", LB_KERN_AVX512_LUT4_PACKED1B),
            ("packed1b_lut4_mb4", LB_KERN_AVX512_LUT4_PACKED1B_MB4),
        ]:
            res = LBBenchResult()
            rc = lib.lb_bench_linear_kernel(kkind, N, din, dout, r, 1, 1, 3, ct.byref(res))
            assert rc == 0 and res.max_abs_err < 1e-4, f"Parity failed: {kname} {(din,dout,r,N)} err={res.max_abs_err}"
            print(f"  PASS {kname:18s} {din:4d}x{dout:<4d} r={r:3d} N={N:2d} max_abs_err={res.max_abs_err:.2e}")

    kernels = [
        ("dense_fp32_bcir", LB_KERN_DENSE_FP32_BCIR),
        ("dense_bf16_bcir", LB_KERN_DENSE_BF16_BCIR),
        ("dense_fp16_stream", LB_KERN_DENSE_FP16_STREAM),
        ("dense_q8_bcir", LB_KERN_DENSE_Q8_BCIR),
        ("lowrank_fp32", LB_KERN_LOWRANK_FP32),
        ("unpacked_lut4", LB_KERN_AVX512_LUT4),
        ("packed1b_lut4", LB_KERN_AVX512_LUT4_PACKED1B),
        ("packed1b_lut4_mb4", LB_KERN_AVX512_LUT4_PACKED1B_MB4),
    ]

    # Comprehensive shape x batch sweep
    configs = [
        # (label, din, dout, N, target_bpw, iters)
        ("wide512_0.30",    512,  1536,   1, 0.30, 25),
        ("wide512_0.30",    512,  1536,   4, 0.30, 21),
        ("wide512_0.30",    512,  1536,   8, 0.30, 17),
        ("wide512_0.30",    512,  1536,  16, 0.30, 15),
        ("wide512_0.30",    512,  1536,  32, 0.30, 13),
        ("wide512_0.30",    512,  1536,  64, 0.30, 11),
        ("wide512_0.30",    512,  1536, 128, 0.30,  9),
        ("wide512_0.30",    512,  1536, 256, 0.30,  7),

        ("wide50m_0.30",    768,  2048,   1, 0.30, 25),
        ("wide50m_0.30",    768,  2048,   4, 0.30, 19),
        ("wide50m_0.30",    768,  2048,   8, 0.30, 17),
        ("wide50m_0.30",    768,  2048,  16, 0.30, 15),
        ("wide50m_0.30",    768,  2048,  32, 0.30, 13),
        ("wide50m_0.30",    768,  2048,  64, 0.30, 11),
        ("wide50m_0.30",    768,  2048, 128, 0.30,  9),
        ("wide50m_0.30",    768,  2048, 256, 0.30,  7),

        ("wide50m_0.55",    768,  2048,   1, 0.55, 25),
        ("wide50m_0.55",    768,  2048,  16, 0.55, 13),
        ("wide50m_0.55",    768,  2048,  64, 0.55,  9),
        ("wide50m_0.55",    768,  2048, 256, 0.55,  7),

        ("llama2_7b_0.30", 4096, 11008,   1, 0.30,  9),
        ("llama2_7b_0.30", 4096, 11008,   4, 0.30,  7),
        ("llama2_7b_0.30", 4096, 11008,   8, 0.30,  7),
        ("llama2_7b_0.30", 4096, 11008,  16, 0.30,  5),
        ("llama2_7b_0.30", 4096, 11008,  32, 0.30,  5),
        ("llama2_7b_0.30", 4096, 11008,  64, 0.30,  5),
        ("llama2_7b_0.30", 4096, 11008, 128, 0.30,  3),

        ("llama2_7b_0.55", 4096, 11008,   1, 0.55,  9),
        ("llama2_7b_0.55", 4096, 11008,  16, 0.55,  5),
        ("llama2_7b_0.55", 4096, 11008,  64, 0.55,  3),

        ("llama2_70b_0.55",8192, 28672,   1, 0.55,  5),
        ("llama2_70b_0.30",8192, 28672,   1, 0.30,  5),
        ("llama2_70b_0.30",8192, 28672,  16, 0.30,  3),
        ("llama2_70b_0.10",8192, 28672,   1, 0.10,  5),
    ]

    print("\n=== PACKED-1B LUT4 BATCHED PREFILL & DECODE BENCHMARK (1T Sapphire Rapids AVX-512) ===")
    rows = []
    for label, din, dout, N, tbpw, iters in configs:
        r = solve_lb_rank(dout, din, tbpw, residual=True, min_rank=8)
        rec = {"label": label, "din": din, "dout": dout, "N": N, "target_bpw": tbpw,
               "r_per_path": r, "kernels": {}}
        for kname, kkind in kernels:
            # Skip slow non-tiled streaming baselines on large N x huge layers (FP16/Q8 already O(N) linear)
            if kname in ("dense_fp16_stream", "dense_q8_bcir") and din >= 4096 and N >= 16:
                continue
            if kname == "dense_bf16_bcir" and din >= 8192:
                continue
            res = LBBenchResult()
            rc = lib.lb_bench_linear_kernel(kkind, N, din, dout, r, 1, 1, iters, ct.byref(res))
            assert rc == 0
            if "lut4" in kname:
                assert res.max_abs_err < 1e-3, f"Parity error on {label} N={N} {kname}: {res.max_abs_err}"
            rec["kernels"][kname] = {
                "median_us": round(res.median_us, 2),
                "min_us": round(res.min_us, 2),
                "gflops_equiv": round(res.gflops_equiv, 2),
                "max_abs_err": float(res.max_abs_err),
                "weight_bytes": int(res.weight_bytes),
                "realized_bpw": round(res.realized_bpw, 4),
                "ideal_deploy_weight_bytes": int(res.ideal_deploy_weight_bytes),
                "ideal_deploy_bpw": round(res.ideal_deploy_bpw, 4),
            }
        k = rec["kernels"]
        fp32 = k["dense_fp32_bcir"]["median_us"]
        lr32 = k["lowrank_fp32"]["median_us"]
        unp  = k["unpacked_lut4"]["median_us"]
        p1b  = k["packed1b_lut4"]["median_us"]
        mb4  = k["packed1b_lut4_mb4"]["median_us"]
        best_lut = min(p1b, mb4)
        winner = "packed1b_mb4" if (mb4 <= p1b and mb4 <= lr32 and mb4 <= fp32) else (
                 "packed1b" if (p1b <= lr32 and p1b <= fp32) else (
                 "lowrank_fp32" if lr32 <= fp32 else "dense_fp32"))
        rec["summary"] = {
            "winner": winner,
            "lr32_over_packed1b": round(lr32 / p1b, 2),
            "lr32_over_packed1b_mb4": round(lr32 / mb4, 2),
            "fp32_over_best_packed_lut4": round(fp32 / best_lut, 2),
            "unpacked_over_packed1b": round(unp / p1b, 2),
        }
        rows.append(rec)
        print(
            f"{label:15s} {din:4d}x{dout:<5d} N={N:3d} r={r:4d} | "
            f"FP32={fp32:9.1f} LR32={lr32:8.1f} Unp={unp:8.1f} P1b={p1b:8.1f} P1b_mb4={mb4:8.1f} | "
            f"LR/P1b={lr32/p1b:5.2f}x LR/mb4={lr32/mb4:5.2f}x -> {winner}",
            flush=True,
        )

    out = {
        "host": platform.platform(),
        "machine": platform.machine(),
        "threads": 1,
        "results": rows,
    }
    out_path = HERE.parent / "results/bench_lut4_packed_batched.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
