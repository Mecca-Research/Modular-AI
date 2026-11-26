#!/usr/bin/env python3
"""Experiment 2: BCIR Precision Policy (P1-P6) Frontier — Optimizer-State Compression vs
Compute Precision (FP32 / BF16-NDT / BF16-VDP / FP8 E4M3-E5M2 / MXFP4 E2M1) & Separate
Attention/MLP Quantization-Policy Regions across pilot1m (0.86M), wide512 (6.16M), and
wide50m (50.54M).

Slices (each 2-5 min, zero contention):
  --part micro   : Isolated 2T GEMM microbenchmarks on wide50m shapes + 50M memory breakdown
  --part a_pilot : Full 12-policy matrix on pilot1m (2 seeds, 300 steps)
  --part a_wide1 : First 4 policies on wide512 (2 seeds, 160 steps, saves wide512_fp32_s42.npz)
  --part a_wide2 : Remaining 4 policies on wide512 (2 seeds, 160 steps)
  --part b1      : wide50m (50.54M) FP32_Ref (180st teacher + 120st readout), Opt_Q8_BF16Grad, Opt_Adafactor
  --part b2      : wide50m (50.54M) BF16_NDT, BF16_VDP, FP8_E4M3_E5M2
  --part b3      : wide50m (50.54M) MXFP4_Uniform, Asym_MXFP4_FP8, Region_FP32Attn_MXFP4MLP, Region_FP8Attn_MXFP4MLP_Q8Opt
  --part merge   : Combine all slices into results/exp2_precision_frontier_complete.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bcir_lab_harness as H

POLICIES: Dict[str, Dict[str, Any]] = {
    "FP32_Ref": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK, mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Opt_Q8_Moments": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK, mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_Q8_MOMENTS, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Opt_Q8_BF16Grad": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK, mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_Q8_MOMENTS_BF16_GRAD, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Opt_Adafactor": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK, mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_ADAFACTOR_M2, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Opt_Adafactor_BF16Grad": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK, mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_ADAFACTOR_BF16_GRAD, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Compute_BF16_NDT": dict(
        attn_fwd=H.BCIR_GEMM_BF16_NDT, attn_bwd=H.BCIR_GEMM_BF16_NDT,
        mlp_fwd=H.BCIR_GEMM_BF16_NDT, mlp_bwd=H.BCIR_GEMM_BF16_NDT,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Compute_BF16_VDP": dict(
        attn_fwd=H.BCIR_GEMM_BF16_VDP, attn_bwd=H.BCIR_GEMM_BF16_VDP,
        mlp_fwd=H.BCIR_GEMM_BF16_VDP, mlp_bwd=H.BCIR_GEMM_BF16_VDP,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Compute_FP8_E4M3_E5M2": dict(
        attn_fwd=H.BCIR_GEMM_FP8_E4M3_BLK32, attn_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        mlp_fwd=H.BCIR_GEMM_FP8_E4M3_BLK32, mlp_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Compute_MXFP4_Uniform": dict(
        attn_fwd=H.BCIR_GEMM_FP4_MX_BLK32, attn_bwd=H.BCIR_GEMM_FP4_MX_BLK32,
        mlp_fwd=H.BCIR_GEMM_FP4_MX_BLK32, mlp_bwd=H.BCIR_GEMM_FP4_MX_BLK32,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Compute_Asym_MXFP4_FP8": dict(
        attn_fwd=H.BCIR_GEMM_FP4_MX_BLK32, attn_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        mlp_fwd=H.BCIR_GEMM_FP4_MX_BLK32, mlp_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Region_FP32Attn_MXFP4MLP": dict(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK, attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP4_MX_BLK32, mlp_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        opt_kind=H.BCIR_OPT_FP32, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
    "Region_FP8Attn_MXFP4MLP_Q8Opt": dict(
        attn_fwd=H.BCIR_GEMM_FP8_E4M3_BLK32, attn_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        mlp_fwd=H.BCIR_GEMM_FP4_MX_BLK32, mlp_bwd=H.BCIR_GEMM_FP8_E5M2_BLK32,
        opt_kind=H.BCIR_OPT_Q8_MOMENTS_BF16_GRAD, fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    ),
}


def run_micro_and_memory() -> Dict[str, Any]:
    print("\n=== EXP 2 MICRO: Isolated 2T GEMM Contracts & 50M Persistent Memory Table ===", flush=True)
    lib = H.load_lib()
    psz = lib.bcir_tensor_pool_size(2)
    pmem = np.zeros(psz, dtype=np.uint8)
    pool = lib.bcir_tensor_pool_init(pmem.ctypes.data, psz, 2)

    shapes = [
        ("wide50m_mlp_gate_up", 256, 2048, 768),
        ("wide50m_attn_qo", 256, 768, 768),
        ("wide50m_mlp_down", 256, 768, 2048),
    ]
    gemm_modes = [
        ("fp32_kblock", H.BCIR_GEMM_FP32_KBLOCK, 0),
        ("bf16_ndt", H.BCIR_GEMM_BF16_NDT, 0),
        ("bf16_vdp_avx512", H.BCIR_GEMM_BF16_VDP, 0),
        ("bf16_vdp_portable", H.BCIR_GEMM_BF16_VDP, 1),
        ("fp8_e4m3_blk32", H.BCIR_GEMM_FP8_E4M3_BLK32, 0),
        ("fp8_e5m2_blk32", H.BCIR_GEMM_FP8_E5M2_BLK32, 0),
        ("mxfp4_e2m1_blk32", H.BCIR_GEMM_FP4_MX_BLK32, 0),
        ("bcirq8_g32", H.BCIR_GEMM_Q8_BLK32, 0),
    ]
    rng = np.random.default_rng(7)
    bench_out: Dict[str, Any] = {}
    for sname, M, N, K in shapes:
        A = rng.normal(0.0, 0.25, size=(M, K)).astype(np.float32)
        B = rng.normal(0.0, 0.25, size=(N, K)).astype(np.float32)  # tb=1 -> y = A @ B^T
        C_ref = np.zeros((M, N), dtype=np.float32)
        lib.bcir_prec_mm_pool(pool, H.BCIR_GEMM_FP32_KBLOCK, 0, 0, 1, M, N, K, 1.0, A.ctypes.data, B.ctypes.data, 0.0, C_ref.ctypes.data, None, 0)
        ref_norm = float(np.linalg.norm(C_ref))
        sres = {}
        for gname, gk, port in gemm_modes:
            sb = int(lib.bcir_prec_mm_scratch_bytes(gk, M, N, K))
            scr = np.zeros(max(64, sb), dtype=np.uint8)
            C = np.zeros((M, N), dtype=np.float32)
            # Warmup
            lib.bcir_prec_mm_pool(pool, gk, port, 0, 1, M, N, K, 1.0, A.ctypes.data, B.ctypes.data, 0.0, C.ctypes.data, scr.ctypes.data, sb)
            iters = 5 if port else 15
            ts = []
            for _ in range(iters):
                t0 = time.perf_counter()
                lib.bcir_prec_mm_pool(pool, gk, port, 0, 1, M, N, K, 1.0, A.ctypes.data, B.ctypes.data, 0.0, C.ctypes.data, scr.ctypes.data, sb)
                ts.append((time.perf_counter() - t0) * 1e3)
            med_ms = float(np.median(ts))
            gflops = (2.0 * M * N * K) / (med_ms * 1e6)
            rel_err = float(np.linalg.norm(C - C_ref) / max(ref_norm, 1e-12))
            sres[gname] = {
                "median_ms": round(med_ms, 3),
                "gflops": round(gflops, 2),
                "rel_frob_err_vs_fp32": round(rel_err, 6),
                "max_abs_err_vs_fp32": round(float(np.max(np.abs(C - C_ref))), 6),
            }
            print(f"  [{sname:20s}] {gname:18s}: {med_ms:6.2f} ms ({gflops:6.1f} GF/s) rel_err={rel_err:.5f}", flush=True)
        bench_out[sname] = sres

    lib.bcir_tensor_pool_stop(pool)

    # Exact 50M persistent storage comparison across optimizer-state policies vs inference weight formats
    sp50 = H.make_spec(width=768, heads=12, kvheads=4, layers=8, ff=2048, batch=4, time_len=64, skip_init=1, threads=1)
    opt_mem = {}
    for opt_name, ok in [
        ("adamw_fp32", H.BCIR_OPT_FP32),
        ("adamw_q8_moments", H.BCIR_OPT_Q8_MOMENTS),
        ("adamw_q8_moments_bf16_grad", H.BCIR_OPT_Q8_MOMENTS_BF16_GRAD),
        ("adamw_adafactor_m2", H.BCIR_OPT_ADAFACTOR_M2),
        ("adamw_adafactor_bf16_grad", H.BCIR_OPT_ADAFACTOR_BF16_GRAD),
    ]:
        with H.ExtModelHandle(sp50, H.make_policy(opt_kind=ok)) as em:
            ms = em.mem_stats()
            opt_mem[opt_name] = {
                "n_params": ms["n_params"],
                "weight_mib": round(ms["weight_bytes"] / (1024**2), 2),
                "opt_only_mib": round(ms["opt_only_bytes"] / (1024**2), 2),
                "persistent_mib": round(ms["persistent_bytes"] / (1024**2), 2),
                "scratch_mib": round(ms["scratch_bytes"] / (1024**2), 2),
                "total_mib": round(ms["total_bytes"] / (1024**2), 2),
                "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
                "saved_vs_fp32_mib": round((ms["n_params"] * 16 - ms["persistent_bytes"]) / (1024**2), 2),
            }
            print(
                f"  [50M Mem] {opt_name:28s}: persistent={opt_mem[opt_name]['persistent_mib']:6.2f} MiB "
                f"({opt_mem[opt_name]['bytes_per_param']:.2f} B/p, saves {opt_mem[opt_name]['saved_vs_fp32_mib']:6.2f} MiB)",
                flush=True,
            )

    return {
        "provenance": H.get_host_provenance(),
        "contracts": H.get_contract_table(),
        "gemm_microbench_2t": bench_out,
        "mem_50m_optimizer_vs_infer": opt_mem,
    }


def run_policy_list(
    scale_name: str,
    geom: Dict[str, int],
    policy_names: List[str],
    steps: int,
    eval_every: int,
    seeds: List[int],
    max_lr: float,
    val_batches: int = 12,
    test_batches: int = 32,
    save_ckpt_map: Dict[Tuple[str, int], Path] | None = None,
    extra_steps_map: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, val_batches, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, test_batches, B, T)

    out: Dict[str, Any] = {"scale": scale_name, "geom": geom, "steps": steps, "max_lr": max_lr, "policies": {}}
    for pname in policy_names:
        pkw = POLICIES[pname]
        pol = H.make_policy(**pkw)
        run_steps = (extra_steps_map or {}).get(pname, steps)
        lrs = H.cosine_lr_schedule(run_steps, max_lr, max_lr * 0.1, warmup=max(5, run_steps // 10))
        runs = []
        for sd in seeds:
            tr_tk, tr_tg = H.sample_lm_batches(wt_tr, run_steps, B, T, seed=20000 + sd)
            sp = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=sd, threads=2, **geom)
            opt = H.BcirAdamwParams(lr=max_lr, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)
            with H.ExtModelHandle(sp, pol) as em:
                res = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, eval_every)
                if save_ckpt_map and (pname, sd) in save_ckpt_map:
                    ckp = save_ckpt_map[(pname, sd)]
                    ckp.parent.mkdir(parents=True, exist_ok=True)
                    np.savez(ckp, weights=res["best_weights"])
                    res["saved_ckpt"] = str(ckp.name)
                    res["saved_ckpt_sha256"] = H.sha256_file(ckp)
                # If run_steps > steps (e.g. 180st teacher while frontier compares at 120st), also record matched-step readout!
                if run_steps > steps:
                    c_at = [c for c in res["curve"] if c["step"] <= steps]
                    best_c = min(c_at, key=lambda x: x["val_ce"])
                    res["matched_step_val_ce"] = best_c["val_ce"]
                    res["matched_step_best_step"] = best_c["step"]
            res.pop("best_weights", None)
            res.pop("final_weights", None)
            res["seed"] = sd
            runs.append(res)
            ms = res["mem_stats"]
            bpp = ms["persistent_bytes"] / ms["n_params"]
            print(
                f"  [{scale_name:7s}] {pname:30s} s={sd:3d}: val={res['best_val_ce']:.4f} "
                f"test={res['test_ce']:.4f} (ppl={res['test_ppl']:5.2f}) "
                f"ms/st={res['median_step_ms']:6.1f} {bpp:5.2f}B/p "
                f"fp64={res['telemetry']['fp64_norm_promotions']+res['telemetry']['fp64_softmax_promotions']} "
                f"nf={res['telemetry']['nonfinite_events']}",
                flush=True,
            )
        tces = [r["test_ce"] for r in runs]
        out["policies"][pname] = {
            "policy_cfg": pkw,
            "test_ce_mean": float(np.mean(tces)),
            "test_ce_spread": float(np.max(tces) - np.min(tces)),
            "test_ppl_mean": float(np.mean([r["test_ppl"] for r in runs])),
            "median_step_ms": float(np.mean([r["median_step_ms"] for r in runs])),
            "persistent_bytes": runs[0]["mem_stats"]["persistent_bytes"],
            "bytes_per_param": round(runs[0]["mem_stats"]["persistent_bytes"] / runs[0]["mem_stats"]["n_params"], 3),
            "runs": runs,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["micro", "a_pilot", "a_wide1", "a_wide2", "b1", "b2a", "b2b", "b3a", "b3b", "merge"], required=True)
    args = ap.parse_args()
    H.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    g_pilot = dict(width=192, heads=4, kvheads=2, layers=2, ff=512)
    g_wide = dict(width=512, heads=8, kvheads=2, layers=2, ff=1536)
    g_50m = dict(width=768, heads=12, kvheads=4, layers=8, ff=2048)

    if args.part == "micro":
        H.write_json(H.RESULTS_DIR / "exp2_part_micro.json", run_micro_and_memory())
    elif args.part == "a_pilot":
        res = run_policy_list(
            "pilot1m", g_pilot, list(POLICIES.keys()),
            steps=300, eval_every=50, seeds=[42, 137], max_lr=1.5e-3,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_a_pilot.json", res)
    elif args.part == "a_wide1":
        res = run_policy_list(
            "wide512", g_wide,
            ["FP32_Ref", "Opt_Q8_BF16Grad", "Opt_Adafactor", "Compute_BF16_VDP"],
            steps=160, eval_every=40, seeds=[42, 137], max_lr=1.2e-3,
            save_ckpt_map={("FP32_Ref", 42): H.CKPT_DIR / "wide512_fp32_s42.npz"},
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_a_wide1.json", res)
    elif args.part == "a_wide2":
        res = run_policy_list(
            "wide512", g_wide,
            ["Compute_FP8_E4M3_E5M2", "Compute_Asym_MXFP4_FP8", "Region_FP32Attn_MXFP4MLP", "Region_FP8Attn_MXFP4MLP_Q8Opt"],
            steps=160, eval_every=40, seeds=[42, 137], max_lr=1.2e-3,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_a_wide2.json", res)
    elif args.part == "b1":
        res = run_policy_list(
            "wide50m", g_50m,
            ["FP32_Ref", "Opt_Q8_BF16Grad", "Opt_Adafactor"],
            steps=120, eval_every=30, seeds=[42], max_lr=1.0e-3, val_batches=8, test_batches=16,
            save_ckpt_map={("FP32_Ref", 42): H.CKPT_DIR / "wide50m_fp32_120st.npz"},
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_b1.json", res)
    elif args.part == "b2a":
        res = run_policy_list(
            "wide50m", g_50m,
            ["Compute_BF16_NDT", "Compute_BF16_VDP"],
            steps=120, eval_every=30, seeds=[42], max_lr=1.0e-3, val_batches=8, test_batches=16,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_b2a.json", res)
    elif args.part == "b2b":
        res = run_policy_list(
            "wide50m", g_50m,
            ["Compute_FP8_E4M3_E5M2"],
            steps=120, eval_every=30, seeds=[42], max_lr=1.0e-3, val_batches=8, test_batches=16,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_b2b.json", res)
    elif args.part == "b3a":
        res = run_policy_list(
            "wide50m", g_50m,
            ["Compute_MXFP4_Uniform", "Compute_Asym_MXFP4_FP8"],
            steps=120, eval_every=30, seeds=[42], max_lr=1.0e-3, val_batches=8, test_batches=16,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_b3a.json", res)
    elif args.part == "b3b":
        res = run_policy_list(
            "wide50m", g_50m,
            ["Region_FP32Attn_MXFP4MLP", "Region_FP8Attn_MXFP4MLP_Q8Opt"],
            steps=120, eval_every=30, seeds=[42], max_lr=1.0e-3, val_batches=8, test_batches=16,
        )
        H.write_json(H.RESULTS_DIR / "exp2_part_b3b.json", res)
    elif args.part == "merge":
        micro = json.loads((H.RESULTS_DIR / "exp2_part_micro.json").read_text())
        pilot = json.loads((H.RESULTS_DIR / "exp2_part_a_pilot.json").read_text())
        w1 = json.loads((H.RESULTS_DIR / "exp2_part_a_wide1.json").read_text())
        w2 = json.loads((H.RESULTS_DIR / "exp2_part_a_wide2.json").read_text())
        wide = dict(w1)
        wide["policies"] = {**w1["policies"], **w2["policies"]}
        b1 = json.loads((H.RESULTS_DIR / "exp2_part_b1.json").read_text())
        b2a = json.loads((H.RESULTS_DIR / "exp2_part_b2a.json").read_text())
        b2b = json.loads((H.RESULTS_DIR / "exp2_part_b2b.json").read_text())
        b3a = json.loads((H.RESULTS_DIR / "exp2_part_b3a.json").read_text())
        b3b = json.loads((H.RESULTS_DIR / "exp2_part_b3b.json").read_text())
        w50 = dict(b1)
        w50["policies"] = {**b1["policies"], **b2a["policies"], **b2b["policies"], **b3a["policies"], **b3b["policies"]}
        # Compute Delta CE & exp(Delta CE) vs FP32_Ref for every scale!
        for blk in (pilot, wide, w50):
            ref_ce = blk["policies"]["FP32_Ref"]["test_ce_mean"]
            ref_ms = blk["policies"]["FP32_Ref"]["median_step_ms"]
            for pname, p in blk["policies"].items():
                dce = p["test_ce_mean"] - ref_ce
                p["delta_ce_vs_fp32"] = round(dce, 5)
                p["ppl_ratio_vs_fp32"] = round(math.exp(dce), 5)
                p["within_004_nats_gate"] = bool(dce <= 0.04)
                p["step_ms_ratio_vs_fp32"] = round(p["median_step_ms"] / max(ref_ms, 1e-6), 3)
        merged = {
            "provenance": micro["provenance"],
            "contracts": micro["contracts"],
            "gemm_microbench_2t": micro["gemm_microbench_2t"],
            "mem_50m_optimizer_vs_infer": micro["mem_50m_optimizer_vs_infer"],
            "pilot1m": pilot,
            "wide512": wide,
            "wide50m": w50,
            "caveat_token_exposure": (
                "50M runs train for 120 steps x 256 tok = 30,720 tokens (and pilot/wide for 76.8k/40.96k tokens); "
                "these are initial research gates quantifying numerical stability, storage-vs-compute tradeoffs, "
                "and region sensitivity, not converged general-purpose LLM pretraining."
            ),
        }
        H.write_json(H.RESULTS_DIR / "exp2_precision_frontier_complete.json", merged)
        print("Merged -> results/exp2_precision_frontier_complete.json", flush=True)


if __name__ == "__main__":
    main()
