#!/usr/bin/env python3
"""Experiment 1: Operational Training Foundations (TRAIN-OPT-1, TRAIN-OPT-2, TRAIN-OPT-4).
Slices:
  --part small : TRAIN-OPT-4 init benchmark (all 3 scales) + TRAIN-OPT-2 suffix-trimming
                 benchmark + TRAIN-OPT-1 init/clip sweep on pilot1m (0.86M) & wide512 (6.16M, L=2)
  --part 50m   : TRAIN-OPT-1 init/clip sweep on wide50m (50.54M, L=8)
  --part merge : Merge slices into results/exp1_train_opt_1_4.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bcir_lab_harness as H


def bench_train_opt_4() -> Dict[str, Any]:
    print("\n=== TRAIN-OPT-4: Native C11 Ziggurat Init vs Python NumPy Init ===", flush=True)
    geoms = [
        ("pilot1m_0.86M", dict(width=192, heads=4, kvheads=2, layers=2, ff=512)),
        ("wide512_6.16M", dict(width=512, heads=8, kvheads=2, layers=2, ff=1536)),
        ("wide50m_50.54M", dict(width=768, heads=12, kvheads=4, layers=8, ff=2048)),
    ]
    out: Dict[str, Any] = {}
    pol = H.make_policy()
    for name, g in geoms:
        sp_c = H.make_spec(batch=1, time_len=16, init_policy=H.BCIR_INIT_MUP_V1, seed=42, threads=2, skip_init=0, **g)
        t0 = time.perf_counter()
        with H.ExtModelHandle(sp_c, pol) as em:
            c11_ms = (time.perf_counter() - t0) * 1e3
            n_params = em.n_params
            c11_internal_ms = em.mem_stats()["init_ms"]

        sp_sk = H.make_spec(batch=1, time_len=16, init_policy=H.BCIR_INIT_MUP_V1, seed=42, threads=2, skip_init=1, **g)
        t0 = time.perf_counter()
        with H.ExtModelHandle(sp_sk, pol) as em:
            skip_ms = (time.perf_counter() - t0) * 1e3

        # Pure Python + NumPy standard normal + copy into model buffer
        with H.ExtModelHandle(sp_sk, pol) as em:
            rng = np.random.default_rng(42)
            t0 = time.perf_counter()
            w = rng.normal(0.0, 0.02, size=n_params).astype(np.float32)
            em.load_weights(w)
            py_np_ms = (time.perf_counter() - t0) * 1e3

        # Standard Python stdlib random.gauss estimate on pilot1m or 200k slice scaled linearly
        import random
        r_py = random.Random(42)
        sample_n = min(n_params, 200_000)
        t0 = time.perf_counter()
        _ = [r_py.gauss(0.0, 0.02) for _ in range(sample_n)]
        py_stdlib_est_ms = (time.perf_counter() - t0) * 1e3 * (n_params / sample_n)

        rec = {
            "n_params": n_params,
            "c11_ziggurat_total_ms": round(c11_ms, 2),
            "c11_ziggurat_init_ms": round(c11_internal_ms, 2),
            "skip_init_ms": round(skip_ms, 2),
            "python_numpy_normal_ms": round(py_np_ms, 2),
            "python_stdlib_gauss_est_ms": round(py_stdlib_est_ms, 1),
            "speedup_c11_vs_numpy": round(py_np_ms / max(c11_ms, 1e-6), 2),
            "speedup_c11_vs_stdlib": round(py_stdlib_est_ms / max(c11_ms, 1e-6), 1),
            "speedup_skip_vs_stdlib": round(py_stdlib_est_ms / max(skip_ms, 1e-6), 1),
        }
        out[name] = rec
        print(
            f"  {name:15s}: params={n_params:10,d} | C11={c11_ms:6.1f}ms | skip={skip_ms:5.1f}ms "
            f"| NumPy={py_np_ms:6.1f}ms ({rec['speedup_c11_vs_numpy']}x) "
            f"| PyStdlib~={py_stdlib_est_ms:8.1f}ms ({rec['speedup_c11_vs_stdlib']}x)",
            flush=True,
        )
    return out


def bench_train_opt_2() -> Dict[str, Any]:
    print("\n=== TRAIN-OPT-2: Active-Length Suffix Trimming & All-Masked Refusal ===", flush=True)
    # 4-pair KV sequences fit in ~22 tokens inside T=64 buffer -> active_T=24 vs full T=64
    sp = H.make_spec(width=192, heads=4, kvheads=2, layers=2, ff=512, batch=8, time_len=64,
                     init_policy=H.BCIR_INIT_MUP_V1, seed=42, threads=2)
    pol = H.make_policy()
    tk, tg = H.build_kv_dataset(25 * 8, 64, n_pairs=4, seed=2026)
    tk = tk.reshape(25, 8, 64)
    tg = tg.reshape(25, 8, 64)
    # Compute active suffix per step (last non-IGNORE index + 1)
    active_ts = np.array([int(np.max(np.where(tg[s] != H.BCIR_EXT_IGNORE)[1]) + 1) for s in range(25)], dtype=np.uint32)
    opt = H.BcirAdamwParams(lr=1e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)

    # Full T=64 padded run
    with H.ExtModelHandle(sp, pol) as m_full:
        rc1, ev_full = m_full.train_loop(tk, tg, opt)
        w_full = m_full.copy_weights()

    # Suffix-trimmed run
    with H.ExtModelHandle(sp, pol) as m_trim:
        # Prepare tightly-packed trimmed batches for exact comparison
        rc2, ev_trim = m_trim.train_loop(tk, tg, opt, active_times=active_ts)
        # Wait: train_loop passes tk + s*B*T with stride B*T, and ext_forward reads B*act_T contiguous!
        # Let's properly pack each step's (B, act_T) into the leading slice of (B, T) as TRAIN-OPT-2 specifies!
    tk_pack = np.zeros_like(tk)
    tg_pack = np.full_like(tg, H.BCIR_EXT_IGNORE)
    for s in range(25):
        at = int(active_ts[s])
        flat_tk = tk[s, :, :at].ravel()
        flat_tg = tg[s, :, :at].ravel()
        tk_pack[s].ravel()[: flat_tk.size] = flat_tk
        tg_pack[s].ravel()[: flat_tg.size] = flat_tg

    with H.ExtModelHandle(sp, pol) as m_trim:
        rc2, ev_trim = m_trim.train_loop(tk_pack, tg_pack, opt, active_times=active_ts)
        w_trim = m_trim.copy_weights()

    max_w_diff = float(np.max(np.abs(w_full - w_trim)))
    max_loss_diff = float(max(abs(a["loss"] - b["loss"]) for a, b in zip(ev_full, ev_trim)))
    ms_full = float(np.median([e["step_ms"] for e in ev_full[3:]]))
    ms_trim = float(np.median([e["step_ms"] for e in ev_trim[3:]]))
    speedup = ms_full / max(ms_trim, 1e-6)
    res = {
        "batch": 8,
        "full_T": 64,
        "active_T_mean": float(np.mean(active_ts)),
        "active_T_max": int(np.max(active_ts)),
        "rc_full": rc1,
        "rc_trim": rc2,
        "max_loss_diff_25steps": max_loss_diff,
        "max_weight_diff_25steps": max_w_diff,
        "full_median_step_ms": round(ms_full, 3),
        "trimmed_median_step_ms": round(ms_trim, 3),
        "wall_speedup": round(speedup, 3),
    }
    print(
        f"  Active T={res['active_T_mean']:.1f}/64 | full={ms_full:.2f}ms/st vs trim={ms_trim:.2f}ms/st "
        f"({speedup:.2f}x) | max_loss_diff={max_loss_diff:.2e} max_w_diff={max_w_diff:.2e}",
        flush=True,
    )
    return res


def run_train_opt_1_scale(
    scale_name: str,
    geom: Dict[str, int],
    steps: int,
    eval_every: int,
    seeds: List[int],
    max_lr: float,
    val_batches: int = 12,
    test_batches: int = 32,
) -> Dict[str, Any]:
    print(f"\n=== TRAIN-OPT-1 Scale: {scale_name} (steps={steps}, seeds={seeds}, lr={max_lr}) ===", flush=True)
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, val_batches, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, test_batches, B, T)

    variants = [
        ("legacy_002_clip1", H.BCIR_INIT_LEGACY_002, 1.0),
        ("residual_scaled_clip1", H.BCIR_INIT_RESIDUAL_SCALED, 1.0),
        ("mup_v1_clip1", H.BCIR_INIT_MUP_V1, 1.0),
        ("mup_v1_clip3", H.BCIR_INIT_MUP_V1, 3.0),
    ]
    pol = H.make_policy(fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL)
    lrs = H.cosine_lr_schedule(steps, max_lr, max_lr * 0.1, warmup=max(5, steps // 10))
    scale_out: Dict[str, Any] = {"geom": geom, "steps": steps, "tokens_per_run": steps * B * T, "variants": {}}

    for vname, init_pol, clip in variants:
        runs = []
        for sd in seeds:
            tr_tk, tr_tg = H.sample_lm_batches(wt_tr, steps, B, T, seed=10000 + sd)
            sp = H.make_spec(batch=B, time_len=T, init_policy=init_pol, mup_d_base=192, seed=sd, threads=2, **geom)
            opt = H.BcirAdamwParams(lr=max_lr, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=clip)
            with H.ExtModelHandle(sp, pol) as em:
                res = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, eval_every)
            res.pop("best_weights", None)
            res.pop("final_weights", None)
            res["seed"] = sd
            runs.append(res)
            print(
                f"  [{scale_name:8s}] {vname:22s} s={sd:3d}: val_ce={res['best_val_ce']:.4f} "
                f"test_ce={res['test_ce']:.4f} (ppl={res['test_ppl']:.2f}) "
                f"gnorm_med={res['grad_norm_median']:.2f} clip_post={res['clip_rate_post_warmup']*100:5.1f}% "
                f"ms/st={res['median_step_ms']:.1f} nf={res['telemetry']['nonfinite_events']}",
                flush=True,
            )
        tces = [r["test_ce"] for r in runs]
        tppls = [r["test_ppl"] for r in runs]
        scale_out["variants"][vname] = {
            "init_policy": init_pol,
            "grad_clip": clip,
            "test_ce_mean": float(np.mean(tces)),
            "test_ce_spread": float(np.max(tces) - np.min(tces)),
            "test_ppl_mean": float(np.mean(tppls)),
            "clip_rate_post_warmup_mean": float(np.mean([r["clip_rate_post_warmup"] for r in runs])),
            "grad_norm_median_mean": float(np.mean([r["grad_norm_median"] for r in runs])),
            "median_step_ms": float(np.mean([r["median_step_ms"] for r in runs])),
            "runs": runs,
        }
    return scale_out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["small", "50m", "merge"], required=True)
    args = ap.parse_args()
    H.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.part == "small":
        data = {
            "provenance": H.get_host_provenance(),
            "train_opt_4": bench_train_opt_4(),
            "train_opt_2": bench_train_opt_2(),
            "train_opt_1_pilot1m": run_train_opt_1_scale(
                "pilot1m", dict(width=192, heads=4, kvheads=2, layers=2, ff=512),
                steps=300, eval_every=50, seeds=[42, 137], max_lr=1.5e-3,
            ),
            "train_opt_1_wide512": run_train_opt_1_scale(
                "wide512", dict(width=512, heads=8, kvheads=2, layers=2, ff=1536),
                steps=150, eval_every=30, seeds=[42, 137], max_lr=1.2e-3,
            ),
        }
        H.write_json(H.RESULTS_DIR / "exp1_part_small.json", data)
        print("\nSaved results/exp1_part_small.json", flush=True)
    elif args.part == "50m":
        data = {
            "provenance": H.get_host_provenance(),
            "train_opt_1_wide50m": run_train_opt_1_scale(
                "wide50m", dict(width=768, heads=12, kvheads=4, layers=8, ff=2048),
                steps=60, eval_every=20, seeds=[42, 137], max_lr=1.0e-3, val_batches=8, test_batches=16,
            ),
        }
        H.write_json(H.RESULTS_DIR / "exp1_part_50m.json", data)
        print("\nSaved results/exp1_part_50m.json", flush=True)
    elif args.part == "merge":
        p1 = json.loads((H.RESULTS_DIR / "exp1_part_small.json").read_text())
        p2 = json.loads((H.RESULTS_DIR / "exp1_part_50m.json").read_text())
        merged = {
            "provenance": p1["provenance"],
            "train_opt_4": p1["train_opt_4"],
            "train_opt_2": p1["train_opt_2"],
            "train_opt_1": {
                "pilot1m": p1["train_opt_1_pilot1m"],
                "wide512": p1["train_opt_1_wide512"],
                "wide50m": p2["train_opt_1_wide50m"],
            },
        }
        H.write_json(H.RESULTS_DIR / "exp1_train_opt_1_4.json", merged)
        print("Merged -> results/exp1_train_opt_1_4.json", flush=True)


if __name__ == "__main__":
    main()
