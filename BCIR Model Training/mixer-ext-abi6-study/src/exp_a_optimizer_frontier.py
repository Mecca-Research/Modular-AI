#!/usr/bin/env python3
"""Experiment A: Next-Gen Default Training Stack (P1-P6) Optimizer Frontier.

Compares TRAIN-OPT-1 (mup_v1, grad_clip=3.0) + TRAIN-OPT-2 paired with:
  1. Opt_FP32_AdamW        (16.00 B/p baseline)
  2. Opt_Adafactor         (12.01 B/p prior compute Pareto)
  3. Opt_Q8_BF16Grad       ( 8.05 B/p prior RAM Pareto)
  4. Opt_Muon_FP32         (~12.00 B/p, 5-step NS + Moonlight RMS scaling + AdamW-32)
  5. Opt_Q8_Muon_BF16Grad  (~7.05 B/p, Muon-8L/AdamW-32 + BF16Grad; arXiv:2509.23106v3)

Reports equal-token AND equal-wall-time curves on canonical WikiText-2 bytes
for wide512 (6.16M GQA) and wide10m (11.04M 3:1 Hybrid-DN+GQA), plus exact 50.54M
memory/step-time audit and Muon NS batch-amortization crossover measurement.
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from abi6_harness import (
    MIXER_DELTANET,
    MIXER_GQA,
    OPT_ADAFACTOR_M2,
    OPT_FP32,
    OPT_MUON_FP32,
    OPT_Q8_BF16_GRAD,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    Abi6Model,
    AdamWParams,
    cosine_lr_schedule,
    host_info,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    sample_lm_batches,
    save_json,
    train_with_val_selection,
)

BASE_OPTS = [
    ("Opt_FP32_AdamW", OPT_FP32, 3e-3, 0.90),
    ("Opt_Adafactor", OPT_ADAFACTOR_M2, 3e-3, 0.90),
    ("Opt_Q8_BF16Grad", OPT_Q8_BF16_GRAD, 3e-3, 0.90),
]

MUON_OPTS = [
    ("Opt_Muon_FP32", OPT_MUON_FP32, 3e-3, 0.95),
    ("Opt_Q8_Muon_BF16Grad", OPT_Q8_MUON_BF16_GRAD, 3e-3, 0.95),
]


def interp_val_ce_at_wall(curve: List[Dict[str, Any]], target_s: float) -> float | None:
    pts = [(c["wall_s"], c["val_ce"]) for c in curve if c.get("val_ce") is not None]
    if not pts:
        return None
    if target_s <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        t0, y0 = pts[i - 1]
        t1, y1 = pts[i]
        if target_s <= t1:
            frac = (target_s - t0) / max(t1 - t0, 1e-9)
            return round(y0 + frac * (y1 - y0), 6)
    return pts[-1][1]


def run_suite_subset(
    suite_dict: Dict[str, Any],
    name: str,
    grid: Sequence[Tuple[str, int, float, float]],
    *,
    width: int,
    heads: int,
    kvheads: int,
    layers: int,
    ff: int,
    mixers: List[int],
    ropes: List[int],
    steps: int,
    seeds: List[int],
    eval_every: int,
) -> Dict[str, Any]:
    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")
    val_tk, val_tg = make_fixed_eval_batches(va_stream, 12, 4, 64)
    test_tk, test_tg = make_fixed_eval_batches(te_stream, 16, 4, 64)

    runs = suite_dict.setdefault("runs", {})
    suite_dict["suite"] = name
    suite_dict["steps"] = steps
    suite_dict["tokens_per_run"] = steps * 4 * 64

    for label, opt_kind, max_lr, beta1 in grid:
        existing_by_seed = {
            r["seed"]: r for r in runs.get(label, {}).get("seeds", [])
        }
        for sd in seeds:
            tr_tk, tr_tg = sample_lm_batches(tr_stream, steps, 4, 64, seed=1000 + sd)
            sp = make_spec(
                vocab=256, width=width, heads=heads, kvheads=kvheads,
                layers=layers, ff=ff, batch=4, time_len=64,
                layer_mixers=mixers, layer_use_rope=ropes,
                mup_d_base=128, seed=sd, threads=2,
            )
            pol = make_policy(opt_kind=opt_kind)
            lrs = cosine_lr_schedule(steps, max_lr, max_lr * 0.1, max(10, steps // 10))
            opt_cfg = AdamWParams(max_lr, beta1, 0.95, 1e-8, 0.01, 3.0)
            with Abi6Model(sp, pol) as model:
                res = train_with_val_selection(
                    model, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg,
                    opt_cfg, lrs, eval_every=eval_every,
                )
            ms = res["mem_stats"]
            row = {
                "seed": sd,
                "n_params": ms["n_params"],
                "persistent_mib": round(ms["persistent_bytes"] / (1024 * 1024), 3),
                "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
                "scratch_mib": round(ms["scratch_bytes"] / (1024 * 1024), 3),
                "test_ce": round(res["test_ce"], 6),
                "test_ppl": round(res["test_ppl"], 4),
                "best_val_ce": round(res["best_val_ce"], 6),
                "best_step": res["best_step"],
                "step_ms_median": res["step_ms_median"],
                "wall_s": res["wall_s"],
                "curve": res["curve"],
            }
            existing_by_seed[sd] = row
            print(
                f"[{name}] {label:24s} s={sd:4d} | {row['bytes_per_param']:5.2f} B/p "
                f"({row['persistent_mib']:6.2f} MiB) | test_ce={row['test_ce']:.4f} "
                f"ppl={row['test_ppl']:.2f} | {row['step_ms_median']:6.1f} ms/st ({row['wall_s']:.1f}s)",
                flush=True,
            )
        seed_rows = [existing_by_seed[k] for k in sorted(existing_by_seed)]
        runs[label] = {
            "opt_kind": opt_kind,
            "max_lr": max_lr,
            "beta1": beta1,
            "n_params": seed_rows[0]["n_params"],
            "persistent_mib": seed_rows[0]["persistent_mib"],
            "bytes_per_param": seed_rows[0]["bytes_per_param"],
            "mean_test_ce": round(float(np.mean([r["test_ce"] for r in seed_rows])), 6),
            "std_test_ce": round(float(np.std([r["test_ce"] for r in seed_rows])), 6),
            "mean_test_ppl": round(float(math.exp(np.mean([r["test_ce"] for r in seed_rows]))), 4),
            "mean_step_ms": round(float(np.mean([r["step_ms_median"] for r in seed_rows])), 3),
            "mean_wall_s": round(float(np.mean([r["wall_s"] for r in seed_rows])), 3),
            "seeds": seed_rows,
        }

    if "Opt_FP32_AdamW" in runs:
        min_wall = min(r["mean_wall_s"] for r in runs.values())
        fp32_ce = runs["Opt_FP32_AdamW"]["mean_test_ce"]
        for rec in runs.values():
            rec["delta_ce_vs_fp32"] = round(rec["mean_test_ce"] - fp32_ce, 6)
            ew = [interp_val_ce_at_wall(r["curve"], min_wall) for r in rec["seeds"]]
            rec["equal_wall_budget_s"] = round(min_wall, 3)
            rec["equal_wall_val_ce"] = round(float(np.mean(ew)), 6)
    return suite_dict


def run_50m_and_amortization() -> Dict[str, Any]:
    """Exact 50.54M (wide50m) memory & step-time audit + Muon NS batch-amortization."""
    tr_stream = load_wikitext_bytes("train")
    tr_tk, tr_tg = sample_lm_batches(tr_stream, 2, 4, 64, seed=5050)
    rows: Dict[str, Any] = {}
    fp32_mib = None
    for label, opt_kind, max_lr, beta1 in BASE_OPTS + MUON_OPTS:
        sp = make_spec(
            vocab=256, width=768, heads=12, kvheads=4, layers=8, ff=2048,
            batch=4, time_len=64, layer_mixers=[MIXER_GQA] * 8, layer_use_rope=[1] * 8,
            mup_d_base=128, seed=42, threads=2, online_softmax=1,
        )
        pol = make_policy(opt_kind=opt_kind)
        opt_cfg = AdamWParams(max_lr, beta1, 0.95, 1e-8, 0.01, 3.0)
        with Abi6Model(sp, pol) as model:
            ms = model.mem_stats()
            rc, evs = model.train_loop(tr_tk, tr_tg, opt_cfg)
            assert rc == 0
            pmib = ms["persistent_bytes"] / (1024 * 1024)
            if label == "Opt_FP32_AdamW":
                fp32_mib = pmib
            rows[label] = {
                "n_params": ms["n_params"],
                "weight_mib": round(ms["weight_bytes"] / (1024 * 1024), 2),
                "opt_only_mib": round(ms["opt_only_bytes"] / (1024 * 1024), 2),
                "persistent_mib": round(pmib, 2),
                "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
                "scratch_mib": round(ms["scratch_bytes"] / (1024 * 1024), 2),
                "step_ms_2nd": round(evs[-1]["step_ms"], 1),
            }
            print(
                f"[wide50m] {label:24s} | {rows[label]['bytes_per_param']:5.2f} B/p "
                f"({rows[label]['persistent_mib']:6.2f} MiB, scratch={rows[label]['scratch_mib']:5.2f} MiB) "
                f"| step2={rows[label]['step_ms_2nd']:.1f} ms",
                flush=True,
            )
    for rec in rows.values():
        rec["saved_vs_fp32_mib"] = round(fp32_mib - rec["persistent_mib"], 2)

    # Measure Muon NS overhead amortization on wide512 as effective tokens/update grows (B*T = 256..4096)
    amort = []
    for batch_sz in (4, 16, 32):
        tk_b, tg_b = sample_lm_batches(tr_stream, 2, batch_sz, 64, seed=6000 + batch_sz)
        times = {}
        for tag, ok in (("adafactor", OPT_ADAFACTOR_M2), ("q8_muon", OPT_Q8_MUON_BF16_GRAD)):
            sp = make_spec(
                vocab=256, width=512, heads=8, kvheads=2, layers=2, ff=1536,
                batch=batch_sz, time_len=64, layer_mixers=[MIXER_GQA, MIXER_GQA], layer_use_rope=[1, 1],
                mup_d_base=128, seed=42, threads=2, online_softmax=1,
            )
            with Abi6Model(sp, make_policy(opt_kind=ok)) as model:
                rc, evs = model.train_loop(tk_b, tg_b, AdamWParams(3e-3, 0.95, 0.95, 1e-8, 0.01, 3.0))
                assert rc == 0
                times[tag] = round(evs[-1]["step_ms"], 1)
        ratio = round(times["q8_muon"] / max(times["adafactor"], 1e-6), 2)
        row = {
            "tokens_per_update": batch_sz * 64,
            "adafactor_ms": times["adafactor"],
            "q8_muon_ms": times["q8_muon"],
            "q8_muon_vs_adafactor_x": ratio,
        }
        amort.append(row)
        print(
            f"[Muon amort wide512] N={batch_sz*64:4d} tok/upd | "
            f"Adafactor={times['adafactor']:6.1f} ms | Q8-Muon={times['q8_muon']:6.1f} ms ({ratio:.2f}x)",
            flush=True,
        )
    return {"wide50m_optimizers": rows, "muon_batch_amortization": amort}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["a1_base", "a1_muon", "a2_base", "a2_muon", "a3"], required=True)
    ap.add_argument("--only-opt", type=str, default=None)
    ap.add_argument("--only-seed", type=int, default=None)
    args = ap.parse_args()
    out_path = RESULTS_DIR / "exp_a_optimizer_frontier.json"
    data: Dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"host": host_info()}

    muon_grid = [x for x in MUON_OPTS if args.only_opt is None or x[0] == args.only_opt]
    seed_list = [args.only_seed] if args.only_seed is not None else [42, 1337]

    if args.part == "a1_base":
        data["a1_wide512_gqa"] = run_suite_subset(
            data.get("a1_wide512_gqa", {}), "wide512_gqa", BASE_OPTS,
            width=512, heads=8, kvheads=2, layers=2, ff=1536,
            mixers=[MIXER_GQA, MIXER_GQA], ropes=[1, 1], steps=160, seeds=seed_list, eval_every=40,
        )
    elif args.part == "a1_muon":
        data["a1_wide512_gqa"] = run_suite_subset(
            data.get("a1_wide512_gqa", {}), "wide512_gqa", muon_grid,
            width=512, heads=8, kvheads=2, layers=2, ff=1536,
            mixers=[MIXER_GQA, MIXER_GQA], ropes=[1, 1], steps=160, seeds=seed_list, eval_every=40,
        )
    elif args.part == "a2_base":
        data["a2_wide10m_hybrid_dn_gqa"] = run_suite_subset(
            data.get("a2_wide10m_hybrid_dn_gqa", {}), "wide10m_hybrid_dn_gqa", BASE_OPTS,
            width=512, heads=8, kvheads=2, layers=4, ff=1536,
            mixers=[MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
            ropes=[0, 0, 0, 1], steps=120, seeds=seed_list, eval_every=40,
        )
    elif args.part == "a2_muon":
        data["a2_wide10m_hybrid_dn_gqa"] = run_suite_subset(
            data.get("a2_wide10m_hybrid_dn_gqa", {}), "wide10m_hybrid_dn_gqa", muon_grid,
            width=512, heads=8, kvheads=2, layers=4, ff=1536,
            mixers=[MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
            ropes=[0, 0, 0, 1], steps=120, seeds=seed_list, eval_every=40,
        )
    elif args.part == "a3":
        data["a3_wide50m_and_amort"] = run_50m_and_amortization()

    save_json(out_path, data)
    print(f"Saved {out_path} ({args.part})")


if __name__ == "__main__":
    main()
