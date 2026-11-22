#!/usr/bin/env python3
"""Experiment 3: Whole-Model COMP-1 & Region-Policy Deployment Study.
Answers:
  1. Width & matrix aspect ratio vs total parameter count as first-order indicators of
     sub-1-bit storage floor (parameter-matched narrow_deep_6m vs wide_shallow_6m within 0.012%),
     AND why low storage floor alone does not guarantee accuracy (stable rank, spectral energy,
     attention sensitivity, training effectiveness).
  2. Attention and MLP as separate deployment quantization-policy regions (BCIRQ8 deterministic
     lossy Q8 Attention + Packed-1b LUT4 LittleBit MLP vs Uniform LittleBit vs Uniform BCIRQ8).
  3. Whole-model 50M (wide50m) deployment quality, memory, streaming decode/prefill latency,
     and Packed-1b LUT4 batch crossover (N=1..256) against Dense FP32/FP16/Q8/LowRank.
  4. Exact circuit & counterfactual preservation on wedge128_kv4_strong (kv_single, kv_4pair,
     value_swap, query_change, ordered length extrapolation) under PTQ & QAT.

Slices:
  --part a     : Parameter-matched 6.16M geometry study + spectral & storage-floor audit
  --part b     : Whole-model 50M COMP-1 deployment matrix + Packed-1b LUT4 N=1..256 crossover
  --part c     : wedge128_kv4_strong exact circuit & counterfactual qualification (PTQ + QAT)
  --part merge : Merge into results/exp3_littlebit_comp1_50m.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bcir_lab_harness as H


def matrix_spectral_audit(W: np.ndarray) -> Dict[str, Any]:
    s = np.linalg.svd(W.astype(np.float64), compute_uv=False)
    s2 = s * s
    tot = float(np.sum(s2)) + 1e-30
    cum = np.cumsum(s2) / tot
    r50 = int(np.searchsorted(cum, 0.50) + 1)
    r90 = int(np.searchsorted(cum, 0.90) + 1)
    r95 = int(np.searchsorted(cum, 0.95) + 1)
    stable_rank = float(tot / max(float(s2[0]), 1e-30))
    dout, din = W.shape
    min_d = min(dout, din)
    # Theoretical 1-path LittleBit storage floor at r=1 and at r=r90 (1-bit U,V + FP16 h,g,l)
    def bpw_at(r: int, paths: int = 1) -> float:
        bits = paths * (r * (dout + din) + 16 * (dout + din + r))
        return bits / float(dout * din)
    return {
        "shape": [int(dout), int(din)],
        "aspect_ratio": round(max(dout, din) / max(min_d, 1), 3),
        "min_dim": int(min_d),
        "stable_rank": round(stable_rank, 2),
        "stable_rank_frac": round(stable_rank / min_d, 4),
        "r50": r50,
        "r90": r90,
        "r90_frac": round(r90 / min_d, 4),
        "r95": r95,
        "bpw_floor_r1_1p": round(bpw_at(1, 1), 4),
        "bpw_at_r16_1p": round(bpw_at(min(16, min_d), 1), 4),
        "bpw_at_r10pct_res": round(bpw_at(max(1, int(round(0.05 * min_d))), 2), 4),
        "bpw_at_r90_1p": round(bpw_at(r90, 1), 4),
    }


def eval_deploy_configs(
    spec: H.BcirExtSpec,
    weights: np.ndarray,
    test_tk: np.ndarray,
    test_tg: np.ndarray,
    configs: List[Tuple[str, int, int, Optional[int], Optional[int], Optional[int], Optional[int]]],
    *,
    ctx_bench: int = 64,
    decode_iters: int = 21,
    prefill_iters: int = 3,
    eval_batches: int = 8,
    itq_iters: int = 6,
) -> Dict[str, Any]:
    """Evaluate a list of (name, attn_fmt, mlp_fmt, attn_r1, attn_r2, mlp_r1, mlp_r2)."""
    pol = H.make_policy()
    out: Dict[str, Any] = {}
    with H.ExtModelHandle(spec, pol) as em:
        em.load_weights(weights)
        tensors = em.tensors()
        # Cache factorizations keyed by (tensor_idx, r1, r2) so repeated configs reuse SVD/ITQ
        lb_cache: Dict[Tuple[int, int, int], Any] = {}

        ref_ce = None
        for cname, afmt, mfmt, ar1, ar2, mr1, mr2 in configs:
            with H.DeployModelHandle(spec, max_kv_len=max(128, ctx_bench + 64)) as dm:
                dm.load_from_ext(em, afmt if afmt != H.BCIR_DEPLOY_LB_PACKED1B else H.BCIR_DEPLOY_FP32,
                                 mfmt if mfmt != H.BCIR_DEPLOY_LB_PACKED1B else H.BCIR_DEPLOY_FP32)
                for t in tensors:
                    if t["region"] == 1 and afmt == H.BCIR_DEPLOY_LB_PACKED1B:
                        r1, r2 = int(ar1 or 16), int(ar2 or 0)
                        key = (t["idx"], r1, r2)
                        mat = weights[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                        if key not in lb_cache:
                            lb_cache[key] = H.factorize_littlebit_matrix(mat, r1, r2, iters=itq_iters)
                        p1, p2, _ = lb_cache[key]
                        dm.set_linear_lb(t["idx"], p1, p2, W_ref=mat)
                    elif t["region"] == 2 and mfmt == H.BCIR_DEPLOY_LB_PACKED1B:
                        r1, r2 = int(mr1 or 16), int(mr2 or 0)
                        key = (t["idx"], r1, r2)
                        mat = weights[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                        if key not in lb_cache:
                            lb_cache[key] = H.factorize_littlebit_matrix(mat, r1, r2, iters=itq_iters)
                        p1, p2, _ = lb_cache[key]
                        dm.set_linear_lb(t["idx"], p1, p2, W_ref=mat)

                st = dm.stats()
                ev = dm.evaluate(test_tk[:eval_batches], test_tg[:eval_batches], spec.batch, spec.time, spec.time)
                bn = dm.benchmark(ctx_len=ctx_bench, decode_iters=decode_iters, prefill_iters=prefill_iters)
                if ref_ce is None:
                    ref_ce = ev["ce"]
                dce = ev["ce"] - ref_ce
                out[cname] = {
                    "stats": st,
                    "eval": ev,
                    "delta_ce_vs_fp32": round(dce, 5),
                    "ppl_ratio_vs_fp32": round(math.exp(min(dce, 30.0)), 5),
                    "within_004_nats_gate": bool(dce <= 0.04),
                    "bench": bn,
                }
                print(
                    f"  {cname:28s}: body_bpw={st['realized_body_bpw']:5.2f} art={st['total_artifact_bytes']/1024**2:6.2f}MiB "
                    f"ce={ev['ce']:.4f} (dCE={dce:+.4f}, x{math.exp(min(dce,20)):.3f}) "
                    f"dec={bn['decode_median_us']:7.1f}us ({bn['decode_tok_per_s']:6.1f}tok/s) "
                    f"q8_viol={st['max_q8_bound_violation']:.1e} avx_diff={bn['max_portable_avx_diff']:.1e}",
                    flush=True,
                )
    return out


def run_part_a() -> Dict[str, Any]:
    print("\n=== EXP 3A: Parameter-Matched 6.16M Geometry Study (Width/Aspect vs Depth) & Spectral Audit ===", flush=True)
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, 12, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, 16, B, T)

    g_wide = dict(width=512, heads=8, kvheads=2, layers=2, ff=1536)
    g_narrow = dict(width=256, heads=4, kvheads=2, layers=8, ff=736)

    sp_wide = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=42, threads=2, **g_wide)
    sp_narrow = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=42, threads=2, **g_narrow)

    # Load wide512_fp32_s42.npz from Exp 2 (or train if absent)
    w_wide_path = H.CKPT_DIR / "wide512_fp32_s42.npz"
    if w_wide_path.exists():
        w_wide = np.load(w_wide_path)["weights"]
        wide_train_info = {"loaded_from": w_wide_path.name, "sha256": H.sha256_file(w_wide_path)}
    else:
        tr_tk, tr_tg = H.sample_lm_batches(wt_tr, 160, B, T, seed=20042)
        lrs = H.cosine_lr_schedule(160, 1.2e-3, 1.2e-4, 16)
        opt = H.BcirAdamwParams(lr=1.2e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)
        with H.ExtModelHandle(sp_wide, H.make_policy()) as em:
            r = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, 40)
            w_wide = r["best_weights"]
            w_wide_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(w_wide_path, weights=w_wide)
            wide_train_info = {"test_ce": r["test_ce"], "sha256": H.sha256_file(w_wide_path)}

    # Train parameter-matched narrow_deep_6m on EXACT same training batches!
    w_narrow_path = H.CKPT_DIR / "narrow_deep_6m_fp32_s42.npz"
    tr_tk, tr_tg = H.sample_lm_batches(wt_tr, 160, B, T, seed=20042)
    lrs = H.cosine_lr_schedule(160, 1.2e-3, 1.2e-4, 16)
    opt = H.BcirAdamwParams(lr=1.2e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)
    with H.ExtModelHandle(sp_narrow, H.make_policy()) as em:
        rn = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, 40)
        w_narrow = rn["best_weights"]
        w_narrow_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(w_narrow_path, weights=w_narrow)
        narrow_train_info = {
            "n_params": em.n_params,
            "best_val_ce": rn["best_val_ce"],
            "test_ce": rn["test_ce"],
            "test_ppl": rn["test_ppl"],
            "median_step_ms": rn["median_step_ms"],
            "sha256": H.sha256_file(w_narrow_path),
            "curve": rn["curve"],
        }
        print(
            f"  Trained narrow_deep_6m ({em.n_params:,d} params vs wide {w_wide.size:,d}): "
            f"test_ce={rn['test_ce']:.4f} (ppl={rn['test_ppl']:.2f}) ms/st={rn['median_step_ms']:.1f}",
            flush=True,
        )

    # Spectral audit on representative matrices of both matched 6.16M models
    spectra = {}
    for label, sp_m, w_m in [("wide_shallow_6m", sp_wide, w_wide), ("narrow_deep_6m", sp_narrow, w_narrow)]:
        with H.ExtModelHandle(sp_m, H.make_policy()) as em:
            em.load_weights(w_m)
            ts = em.tensors()
            sp_list = {}
            for t in ts:
                if t["layer"] in (0, sp_m.layers - 1) and t["region"] in (1, 2):
                    mat = w_m[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                    sp_list[t["name"]] = matrix_spectral_audit(mat)
            spectra[label] = sp_list

    # Deploy matched rank fractions on BOTH geometries (wide d=512 -> r=64 res; narrow d=256 -> r=32 res => identical 12.5% rank frac!)
    print("\n  --- Deploy wide_shallow_6m (d=512, ff=1536, L=2, 6.165M) ---", flush=True)
    dep_wide = eval_deploy_configs(
        sp_wide, w_wide, test_tk, test_tg,
        [
            ("FP32_Ref", H.BCIR_DEPLOY_FP32, H.BCIR_DEPLOY_FP32, None, None, None, None),
            ("FP16_Uniform", H.BCIR_DEPLOY_FP16, H.BCIR_DEPLOY_FP16, None, None, None, None),
            ("BCIRQ8_Uniform", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_BCIRQ8_G32, None, None, None, None),
            ("COMP1_Q8Attn_LBMLP_r64_1p", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 64, 0),
            ("COMP1_Q8Attn_LBMLP_r48_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 48, 48),
            ("LB_Uniform_r48_res", H.BCIR_DEPLOY_LB_PACKED1B, H.BCIR_DEPLOY_LB_PACKED1B, 32, 32, 48, 48),
        ],
        eval_batches=8, decode_iters=21, prefill_iters=3,
    )
    print("\n  --- Deploy narrow_deep_6m (d=256, ff=736, L=8, 6.166M) ---", flush=True)
    dep_narrow = eval_deploy_configs(
        sp_narrow, w_narrow, test_tk, test_tg,
        [
            ("FP32_Ref", H.BCIR_DEPLOY_FP32, H.BCIR_DEPLOY_FP32, None, None, None, None),
            ("FP16_Uniform", H.BCIR_DEPLOY_FP16, H.BCIR_DEPLOY_FP16, None, None, None, None),
            ("BCIRQ8_Uniform", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_BCIRQ8_G32, None, None, None, None),
            ("COMP1_Q8Attn_LBMLP_r32_1p", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 32, 0),
            ("COMP1_Q8Attn_LBMLP_r24_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 24, 24),
            ("LB_Uniform_r24_res", H.BCIR_DEPLOY_LB_PACKED1B, H.BCIR_DEPLOY_LB_PACKED1B, 16, 16, 24, 24),
        ],
        eval_batches=8, decode_iters=21, prefill_iters=3,
    )

    return {
        "provenance": H.get_host_provenance(),
        "param_match": {
            "wide_shallow_6m_params": int(w_wide.size),
            "narrow_deep_6m_params": int(w_narrow.size),
            "rel_param_diff_pct": round(100.0 * abs(w_wide.size - w_narrow.size) / w_wide.size, 4),
        },
        "wide_train_info": wide_train_info,
        "narrow_train_info": narrow_train_info,
        "spectra_audit": spectra,
        "deploy_wide_shallow_6m": dep_wide,
        "deploy_narrow_deep_6m": dep_narrow,
    }


def run_part_b() -> Dict[str, Any]:
    print("\n=== EXP 3B: Whole-Model 50M (wide50m) COMP-1 Deployment & Packed-1b LUT4 Batch Crossover ===", flush=True)
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, 8, B, T)
    g50 = dict(width=768, heads=12, kvheads=4, layers=8, ff=2048)
    sp50 = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=42, threads=2, **g50)

    ck50 = H.CKPT_DIR / "wide50m_fp32_120st.npz"
    if not ck50.exists():
        raise FileNotFoundError(f"Missing {ck50}; run exp2 --part b1 first")
    w50 = np.load(ck50)["weights"]
    ck50_sha = H.sha256_file(ck50)

    # Spectral audit on layer 0 & layer 7 of 50M teacher
    spectra_50m = {}
    with H.ExtModelHandle(sp50, H.make_policy()) as em:
        em.load_weights(w50)
        for t in em.tensors():
            if t["layer"] in (0, 7) and t["region"] in (1, 2):
                mat = w50[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                spectra_50m[t["name"]] = matrix_spectral_audit(mat)

    print("  --- Whole-Model 50M Deployment Matrix (evaluating untouched WikiText-2 test + streaming decode) ---", flush=True)
    dep_50m = eval_deploy_configs(
        sp50, w50, test_tk, test_tg,
        [
            ("FP32_Ref", H.BCIR_DEPLOY_FP32, H.BCIR_DEPLOY_FP32, None, None, None, None),
            ("FP16_Uniform", H.BCIR_DEPLOY_FP16, H.BCIR_DEPLOY_FP16, None, None, None, None),
            ("BCIRQ8_Uniform", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_BCIRQ8_G32, None, None, None, None),
            ("COMP1_Q8Attn_LBMLP_r64_1p", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 64, 0),
            ("COMP1_Q8Attn_LBMLP_r64_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 64, 64),
            ("LB_Uniform_r64_res", H.BCIR_DEPLOY_LB_PACKED1B, H.BCIR_DEPLOY_LB_PACKED1B, 48, 48, 64, 64),
        ],
        ctx_bench=64, decode_iters=11, prefill_iters=1, eval_batches=4, itq_iters=4,
    )

    # Isolated Packed-1b LUT4 vs Dense/LowRank batch crossover N=1..256 on 50M MLP shape (768 -> 2048, r=64 res)
    print("\n  --- Packed-1b LUT4 Batch Crossover on wide50m MLP (768x2048, r=64 res = 0.36 bpw) ---", flush=True)
    lib = H.load_lib()
    kernels = [
        ("Dense_FP32_BCIR", H.LB_KERN_DENSE_FP32_BCIR),
        ("Dense_FP16_Stream", H.LB_KERN_DENSE_FP16_STREAM),
        ("Dense_BCIRQ8", H.LB_KERN_DENSE_Q8_BCIR),
        ("LowRank_FP32", H.LB_KERN_LOWRANK_FP32),
        ("Packed1b_LUT4", H.LB_KERN_AVX512_LUT4_PACKED1B),
        ("Packed1b_LUT4_mb4", H.LB_KERN_AVX512_LUT4_PACKED1B_MB4),
    ]
    crossover: Dict[str, Any] = {}
    for N in (1, 4, 16, 32, 64, 128, 256):
        row = {}
        for kname, kid in kernels:
            res = H.LbBenchResult()
            iters = 31 if N <= 16 else 11
            rc = lib.lb_bench_linear_kernel(kid, N, 768, 2048, 64, 1, 2, iters, ctypes.byref(res))
            assert rc == 0
            row[kname] = {
                "median_us": round(float(res.median_us), 2),
                "gflops_equiv": round(float(res.gflops_equiv), 2),
                "weight_bytes": int(res.weight_bytes),
                "realized_bpw": round(float(res.realized_bpw), 4),
                "max_abs_err": float(res.max_abs_err),
            }
        crossover[f"N={N}"] = row
        print(
            f"    N={N:3d}: FP32={row['Dense_FP32_BCIR']['median_us']:7.1f}us "
            f"FP16={row['Dense_FP16_Stream']['median_us']:7.1f}us "
            f"Q8={row['Dense_BCIRQ8']['median_us']:7.1f}us "
            f"LR32={row['LowRank_FP32']['median_us']:7.1f}us "
            f"P1b_LUT4={row['Packed1b_LUT4']['median_us']:7.1f}us "
            f"P1b_mb4={row['Packed1b_LUT4_mb4']['median_us']:7.1f}us",
            flush=True,
        )

    return {
        "provenance": H.get_host_provenance(),
        "teacher_ckpt": {"name": ck50.name, "sha256": ck50_sha, "n_params": int(w50.size)},
        "spectra_50m": spectra_50m,
        "deploy_50m": dep_50m,
        "packed_lut4_crossover_768x2048_r64res": crossover,
    }


def make_wedge_kv_records(n_examples: int, batch: int, ctx: int, pairs: int, seed: int) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(seed)
    slots = np.arange(3, ctx - 5, 4)
    recs = []
    for _ in range(n_examples // batch):
        x = rng.integers(64, 96, (batch, ctx), dtype=np.uint32)
        x[:, 0] = 256
        y = np.full((batch, ctx), H.BCIR_EXT_IGNORE, dtype=np.uint32)
        vpos, qidx = [], []
        for b in range(batch):
            pos = rng.choice(slots, pairs, replace=False)
            keys = rng.choice(np.arange(32, 48), pairs, replace=False).astype(np.uint32)
            vals = rng.choice(np.arange(16), pairs, replace=False).astype(np.uint32)
            q = int(rng.integers(pairs))
            x[b, pos] = 258
            x[b, pos + 1] = keys
            x[b, pos + 2] = vals
            x[b, ctx - 2] = 257
            x[b, ctx - 1] = keys[q]
            y[b, ctx - 1] = vals[q]
            vpos.append((pos + 2).tolist())
            qidx.append(q)
        recs.append({"x": x, "y": y, "vpos": vpos, "qidx": qidx, "pairs": pairs})
    return recs


def swap_wedge_kv(rec: Dict[str, Any], mode: str) -> Dict[str, Any]:
    x = rec["x"].copy()
    y = rec["y"].copy()
    T = x.shape[1]
    for b in range(len(x)):
        q = rec["qidx"][b]
        other = (q + 1) % rec["pairs"]
        if mode == "value_swap":
            a = rec["vpos"][b][q]
            c = rec["vpos"][b][other]
            x[b, a], x[b, c] = x[b, c], x[b, a]
            y[b, T - 1] = x[b, a]
        else:
            p_new = rec["vpos"][b][other]
            x[b, T - 1] = x[b, p_new - 1]
            y[b, T - 1] = x[b, p_new]
    return {"x": x, "y": y, "vpos": rec["vpos"], "qidx": rec["qidx"], "pairs": rec["pairs"]}


def eval_deploy_on_kv_recs(dm: H.DeployModelHandle, recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    T = recs[0]["x"].shape[1]
    ce_sum, correct, in_cand, tot = 0.0, 0, 0, 0
    for rec in recs:
        for b in range(rec["x"].shape[0]):
            logits = dm.prefill_seq(rec["x"][b])[T - 1].astype(np.float64)
            logits -= np.max(logits)
            yy = int(rec["y"][b, T - 1])
            ce_sum += float(np.log(np.sum(np.exp(logits))) - logits[yy])
            pp = int(np.argmax(logits))
            if pp == yy:
                correct += 1
            cand = {int(rec["x"][b, p]) for p in rec["vpos"][b]}
            if pp in cand:
                in_cand += 1
            tot += 1
    return {"ce": round(ce_sum / tot, 5), "acc": round(correct / tot, 4), "in_cand": round(in_cand / tot, 4), "n": tot}


def make_ordered_records(n_examples: int, batch: int, ctx: int, seed: int, swap: bool = False) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(seed)
    recs = []
    for _ in range(n_examples // batch):
        x = rng.integers(0, 16, (batch, ctx), dtype=np.uint32)
        x[:, 0] = 256
        x[:, ctx - 1] = 257
        if swap:
            x[:, 1] = (x[:, 1] + 7) % 16
        y = np.full((batch, ctx), H.BCIR_EXT_IGNORE, dtype=np.uint32)
        y[:, ctx - 1] = x[:, 1]
        recs.append({"x": x, "y": y, "vpos": [[1] for _ in range(batch)], "qidx": [0] * batch, "pairs": 1})
    return recs


def run_part_c() -> Dict[str, Any]:
    print("\n=== EXP 3C: Exact Associative-Recall Circuit & Counterfactuals on wedge128_kv4_strong ===", flush=True)
    ck = H.CKPT_DIR / "wedge128_kv4_strong.npz"
    w = np.load(ck)["weights"]
    sp = H.make_spec(vocab=260, width=128, heads=4, kvheads=4, layers=3, ff=256,
                     batch=4, time_len=64, rope_base=10000.0, rms_eps=1e-5, threads=2)

    rec_1 = make_wedge_kv_records(64, 4, 64, 1, seed=9101)
    rec_4 = make_wedge_kv_records(64, 4, 64, 4, seed=9104)
    rec_vs = [swap_wedge_kv(r, "value_swap") for r in rec_4]
    rec_qc = [swap_wedge_kv(r, "query_change") for r in rec_4]
    rec_o64 = make_ordered_records(64, 4, 64, seed=9164, swap=False)
    rec_osw = make_ordered_records(64, 4, 64, seed=9164, swap=True)
    rec_o128 = make_ordered_records(64, 4, 128, seed=9128, swap=False)
    suites = [
        ("kv_single", rec_1),
        ("ordered_64", rec_o64),
        ("ordered_swap", rec_osw),
        ("ordered_128", rec_o128),
        ("kv_4pair", rec_4),
        ("value_swap", rec_vs),
        ("query_change", rec_qc),
    ]

    # Spectral audit on wedge128_kv4_strong
    spectra_w = {}
    with H.ExtModelHandle(sp, H.make_policy()) as em:
        em.load_weights(w)
        tensors = em.tensors()
        for t in tensors:
            if t["region"] in (1, 2):
                mat = w[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                spectra_w[t["name"]] = matrix_spectral_audit(mat)

        configs = [
            ("FP32_Ref", H.BCIR_DEPLOY_FP32, H.BCIR_DEPLOY_FP32, None, None, None, None),
            ("FP16_Uniform", H.BCIR_DEPLOY_FP16, H.BCIR_DEPLOY_FP16, None, None, None, None),
            ("BCIRQ8_Uniform", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_BCIRQ8_G32, None, None, None, None),
            ("COMP1_Q8Attn_LBMLP_r16_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 16, 16),
            ("COMP1_Q8Attn_LBMLP_r8_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, None, None, 8, 8),
            ("LB_AttnOnly_r16_res", H.BCIR_DEPLOY_LB_PACKED1B, H.BCIR_DEPLOY_FP32, 16, 16, None, None),
            ("LB_Uniform_r16_res", H.BCIR_DEPLOY_LB_PACKED1B, H.BCIR_DEPLOY_LB_PACKED1B, 16, 16, 16, 16),
        ]
        results = {}
        for cname, afmt, mfmt, ar1, ar2, mr1, mr2 in configs:
            with H.DeployModelHandle(sp, max_kv_len=160) as dm:
                dm.load_from_ext(em, afmt if afmt != H.BCIR_DEPLOY_LB_PACKED1B else H.BCIR_DEPLOY_FP32,
                                 mfmt if mfmt != H.BCIR_DEPLOY_LB_PACKED1B else H.BCIR_DEPLOY_FP32)
                for t in tensors:
                    mat = w[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                    if t["region"] == 1 and afmt == H.BCIR_DEPLOY_LB_PACKED1B:
                        p1, p2, _ = H.factorize_littlebit_matrix(mat, int(ar1), int(ar2), iters=8)
                        dm.set_linear_lb(t["idx"], p1, p2, W_ref=mat)
                    elif t["region"] == 2 and mfmt == H.BCIR_DEPLOY_LB_PACKED1B:
                        p1, p2, _ = H.factorize_littlebit_matrix(mat, int(mr1), int(mr2), iters=8)
                        dm.set_linear_lb(t["idx"], p1, p2, W_ref=mat)
                st = dm.stats()
                s_evals = {sn: eval_deploy_on_kv_recs(dm, sr) for sn, sr in suites}
                results[cname] = {"stats": st, "suites": s_evals}
                print(
                    f"  {cname:28s}: bpw={st['realized_body_bpw']:5.2f} | "
                    f"kv1={s_evals['kv_single']['acc']*100:5.1f}% o64={s_evals['ordered_64']['acc']*100:5.1f}% "
                    f"osw={s_evals['ordered_swap']['acc']*100:5.1f}% o128={s_evals['ordered_128']['acc']*100:5.1f}% "
                    f"kv4={s_evals['kv_4pair']['acc']*100:5.1f}%",
                    flush=True,
                )

    # Also run 160-step KD-QAT on wedge128_kv4_strong (mixed kv_single + ordered_64) comparing COMP-1 vs Uniform LB
    print("  --- KD-QAT Recovery on wedge128_kv4_strong (160 steps, KL+10*InterMSE) ---", flush=True)
    import lb_harness as LB

    dspec = LB.DecoderSpec(vocab_size=260, d_model=128, n_heads=4, n_kv_heads=4, n_layers=3, d_ff=256,
                           activation="silu_gate", tied_embeddings=True, rms_norm_eps=1e-5)
    tr_kv = make_wedge_kv_records(80 * 4, 4, 64, 1, seed=9200)
    tr_or = make_ordered_records(80 * 4, 4, 64, seed=9201)
    tr_recs = []
    for a, b in zip(tr_kv, tr_or):
        tr_recs.extend([a, b])
    tr_x = np.ascontiguousarray(np.stack([r["x"] for r in tr_recs]), dtype=np.uint32)
    tr_y = np.ascontiguousarray(np.stack([r["y"] for r in tr_recs]), dtype=np.uint32)
    lrs = H.cosine_lr_schedule(160, 1.5e-3, 1.5e-4, 16)
    opt_lb = LB.LBAdamWCfg(lr=1.5e-3, lr_scales=3.0e-3, beta1=0.9, beta2=0.95, epsilon=1e-8,
                           weight_decay=0.0, grad_clip=1.0, freeze_non_linear=1)

    teacher_lb = LB.LBModel(dspec, 4, 64, LB.make_proj_cfgs(dspec, LB.LB_MODE_DENSE_FP32),
                            objective=LB.LB_OBJ_CE, threads=2)
    teacher_lb.load_from_dense_bcir(w)

    def eval_lb_on_suites(mod: LB.LBModel) -> Dict[str, Any]:
        out_s = {}
        for sn, sr in suites:
            if sr[0]["x"].shape[1] != 64:
                continue
            ce_sum, cor, ic, tot = 0.0, 0, 0, 0
            for rec in sr:
                z = mod.forward(rec["x"])[:, 63, :].astype(np.float64)
                z -= z.max(axis=-1, keepdims=True)
                yy = rec["y"][:, 63].astype(np.int64)
                loss = np.log(np.exp(z).sum(axis=-1)) - z[np.arange(len(yy)), yy]
                pp = z.argmax(-1)
                ce_sum += float(loss.sum())
                cor += int((pp == yy).sum())
                tot += len(yy)
                for b in range(len(yy)):
                    if int(pp[b]) in {int(rec["x"][b, p]) for p in rec["vpos"][b]}:
                        ic += 1
            out_s[sn] = {"ce": round(ce_sum / tot, 5), "acc": round(cor / tot, 4), "in_cand": round(ic / tot, 4), "n": tot}
        return out_s

    qat_results: Dict[str, Any] = {}
    for qname, only_mlp, q8_dense, r_exp in [
        ("COMP1_Q8Attn_LBMLP_r16_res_QAT", True, True, 16),
        ("COMP1_Q8Attn_LBMLP_r8_res_QAT", True, True, 8),
        ("LB_Uniform_r16_res_QAT", False, False, 16),
    ]:
        pc = LB.make_proj_cfgs(dspec, LB.LB_MODE_LITTLEBIT_RES, explicit_r=r_exp, only_mlp=only_mlp)
        st_mod = LB.LBModel(dspec, 4, 64, pc, surrogate=LB.LB_SURR_SMOOTHSIGN_100, smooth_k=100.0,
                            objective=LB.LB_OBJ_KD_FULL, kd_lambda=10.0, threads=2)
        st_mod.load_from_dense_bcir(w, use_itq=True, itq_iters=15, q8_dense=q8_dense)
        evs = (LB.LBStepMetrics * 160)()
        rc = st_mod.lib.lb_model_train_loop(
            st_mod.ptr, teacher_lb.ptr,
            tr_x.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            tr_y.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            160, lrs.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.byref(opt_lb), evs,
        )
        assert rc == 0
        s_ev = eval_lb_on_suites(st_mod)
        qat_results[qname] = {
            "final_kl": round(float(evs[159].kl_loss), 5),
            "final_inter_mse": round(float(evs[159].inter_mse), 5),
            "median_step_ms": round(float(np.median([evs[i].step_ms for i in range(160)])), 2),
            "suites": s_ev,
        }
        print(
            f"  {qname:30s}: kl={evs[159].kl_loss:.4f} | "
            f"kv1={s_ev['kv_single']['acc']*100:5.1f}% o64={s_ev['ordered_64']['acc']*100:5.1f}% "
            f"osw={s_ev['ordered_swap']['acc']*100:5.1f}% kv4={s_ev['kv_4pair']['acc']*100:5.1f}%",
            flush=True,
        )
        st_mod.close()
    teacher_lb.close()

    return {
        "provenance": H.get_host_provenance(),
        "checkpoint": {"name": ck.name, "sha256": H.sha256_file(ck), "n_params": int(w.size)},
        "spectra_wedge128": spectra_w,
        "ptq_circuit_results": results,
        "qat_circuit_results": qat_results,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["a", "b", "c", "merge"], required=True)
    args = ap.parse_args()
    H.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.part == "a":
        H.write_json(H.RESULTS_DIR / "exp3_part_a.json", run_part_a())
    elif args.part == "b":
        H.write_json(H.RESULTS_DIR / "exp3_part_b.json", run_part_b())
    elif args.part == "c":
        H.write_json(H.RESULTS_DIR / "exp3_part_c.json", run_part_c())
    elif args.part == "merge":
        pa = json.loads((H.RESULTS_DIR / "exp3_part_a.json").read_text())
        pb = json.loads((H.RESULTS_DIR / "exp3_part_b.json").read_text())
        pc = json.loads((H.RESULTS_DIR / "exp3_part_c.json").read_text())
        H.write_json(
            H.RESULTS_DIR / "exp3_littlebit_comp1_50m.json",
            {"provenance": pa["provenance"], "part_a_geometry_6m": pa, "part_b_50m_comp1": pb, "part_c_wedge128_circuits": pc},
        )
        print("Merged -> results/exp3_littlebit_comp1_50m.json", flush=True)


if __name__ == "__main__":
    main()
