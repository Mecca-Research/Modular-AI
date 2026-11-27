#!/usr/bin/env python3
"""Experiment B: Next-Gen MIXER-EXT ABI 6 Architecture Ladder & Beyond-the-List Ablations.

Evaluates 10 controlled architectures under BCIR Default Training Stack (mup_v1,
grad_clip=3.0, TRAIN-OPT-2, Opt_Adafactor 12.01 B/p) across:
  - Part B1: Synthetic Associative Recall, Counterfactuals, Filler > W=16, and
             Length Extrapolation up to ordered_8pair @ T=96 (2 seeds, 250 steps).
  - Part B2: Canonical Raw-Byte WikiText-2 wide10m LM (d=512, L=4, 2 seeds, 160 steps),
             plus attention-sink (first_tok_attn_mean), max activation outlier audit,
             T=4096 state footprint, BCIRQ8 (g=32), and COMP-1 PTQ transfer.
  - Part B3: TRAIN-OPT-5 online-softmax O(T) vs O(T^2) memory scaling at T=64..1024.
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Any, Dict, List

import numpy as np

from abi6_harness import (
    CANON_AC,
    CANON_OFF,
    GATE_G1_HEADWISE,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GDN2,
    MIXER_GQA,
    MIXER_KDA,
    MIXER_MLA,
    OPT_ADAFACTOR_M2,
    RESULTS_DIR,
    Abi6Model,
    AdamWParams,
    apply_comp1_ptq_weights,
    build_kv_dataset,
    cosine_lr_schedule,
    host_info,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    quantize_bcirq8_g32,
    sample_lm_batches,
    save_json,
    train_with_val_selection,
)


def arch_catalog_4l() -> List[Dict[str, Any]]:
    return [
        {
            "name": "GQA_Full",
            "mixers": [MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
            "ropes": [1, 1, 1, 1],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "GQA_Full_G1_QKNorm",
            "mixers": [MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
            "ropes": [1, 1, 1, 1],
            "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "GQA_Full_CanonAC",
            "mixers": [MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
            "ropes": [1, 1, 1, 1],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_AC, "mhc_streams": 1,
        },
        {
            "name": "Arch1_DeltaNet",
            "mixers": [MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET],
            "ropes": [0, 0, 0, 0],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "Arch2_Hybrid_DN_GQA",
            "mixers": [MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
            "ropes": [0, 0, 0, 1],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "Arch4_MLA",
            "mixers": [MIXER_MLA, MIXER_MLA, MIXER_MLA, MIXER_MLA],
            "ropes": [1, 1, 1, 1],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "Arch7a_KDA_NoPEMLA",
            "mixers": [MIXER_KDA, MIXER_KDA, MIXER_KDA, MIXER_MLA],
            "ropes": [0, 0, 0, 0],
            "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "Arch7b_GDN2_G1NoPEMLA",
            "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
            "ropes": [0, 0, 0, 0],
            "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_OFF, "mhc_streams": 1,
        },
        {
            "name": "Arch7c_GDN2_G1NoPEMLA_CanonAC",
            "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
            "ropes": [0, 0, 0, 0],
            "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_AC, "mhc_streams": 1,
        },
        {
            "name": "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2",
            "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
            "ropes": [0, 0, 0, 0],
            "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_AC, "mhc_streams": 2,
        },
    ]


def run_part_b1_recall() -> Dict[str, Any]:
    """Multi-query associative recall (4 shuffled queries/seq = 4x gradient density),
    counterfactuals, filler > W=16, and ordered_8pair @ T=96."""
    seeds = [42, 1337]
    steps = 300
    B, T_max = 8, 96
    # Fixed evaluation suites (identical across all architectures; 4..8 supervised queries/seq).
    # mq4, value_swap, and query_change share seed=5001 so they are paired counterfactuals on the
    # exact same 64 KV contexts.
    eval_probes = {
        "mq4": (build_kv_dataset(64, T_max, 4, 5001, mode="standard", shuffled_query=True), 40),
        "value_swap": (build_kv_dataset(64, T_max, 4, 5001, mode="value_swap", shuffled_query=True), 40),
        "query_change": (build_kv_dataset(64, T_max, 4, 5001, mode="query_change", shuffled_query=True), 40),
        "filler20_gtW": (build_kv_dataset(64, T_max, 4, 5004, filler_len=20, shuffled_query=True), 60),
        "extrap_6pair": (build_kv_dataset(64, T_max, 6, 5005, mode="standard", shuffled_query=True), 56),
        "ordered_6pair": (build_kv_dataset(64, T_max, 6, 5006, ordered_query=True), 56),
        "ordered_8pair_T96": (build_kv_dataset(64, T_max, 8, 5007, ordered_query=True), 80),
    }
    val_tk, val_tg = build_kv_dataset(64, T_max, 4, 4999, mode="standard", shuffled_query=True)
    val_tk = val_tk.reshape(8, B, T_max)
    val_tg = val_tg.reshape(8, B, T_max)

    results: Dict[str, Any] = {}
    for cfg in arch_catalog_4l():
        name = cfg["name"]
        seed_rows = []
        for sd in seeds:
            rng = np.random.default_rng(2000 + sd)
            tks, tgs, act_t = [], [], []
            for st in range(steps):
                fl = int(rng.choice([0, 0, 8]))
                tk_b, tg_b = build_kv_dataset(
                    B, T_max, 4, int(rng.integers(0, 1 << 30)),
                    mode="standard", filler_len=fl, shuffled_query=True,
                )
                tks.append(tk_b)
                tgs.append(tg_b)
                act_t.append(48 if fl > 0 else 40)
            tr_tk = np.stack(tks)
            tr_tg = np.stack(tgs)
            active_t = np.asarray(act_t, dtype=np.uint32)
            sp = make_spec(
                vocab=128, width=128, heads=4, kvheads=2, layers=4, ff=384,
                batch=B, time_len=T_max, layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
                swa_window=16, delta_chunk=16, mla_dc=64, mla_dr=16,
                mup_d_base=128, seed=sd, threads=2,
                gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
                canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
                online_softmax=1, gate_rank=32,
            )
            pol = make_policy(opt_kind=OPT_ADAFACTOR_M2)
            lrs = cosine_lr_schedule(steps, 3e-3, 3e-4, 24)
            opt_cfg = AdamWParams(3e-3, 0.9, 0.95, 1e-8, 0.01, 3.0)
            with Abi6Model(sp, pol) as model:
                res = train_with_val_selection(
                    model, tr_tk, tr_tg, val_tk, val_tg, val_tk, val_tg,
                    opt_cfg, lrs, eval_every=50,
                    active_times=active_t, val_active_time=40,
                )
                if res["rc"] != 0 or res["steps_completed"] != steps:
                    raise RuntimeError(
                        f"[B1] {name} seed={sd} incomplete: rc={res['rc']} "
                        f"steps={res['steps_completed']}/{steps}"
                    )
                probe_acc = {}
                for pname, ((ptk, ptg), pact_t) in eval_probes.items():
                    er = model.evaluate(ptk.reshape(8, B, T_max), ptg.reshape(8, B, T_max), active_time=pact_t)
                    probe_acc[pname] = round(er["accuracy"] * 100.0, 2)
                tel = model.telemetry()
            seed_rows.append({
                "seed": sd,
                "n_params": res["mem_stats"]["n_params"],
                "step_ms": res["step_ms_median"],
                "probes": probe_acc,
                "first_tok_attn_mean": round(tel["first_tok_attn_mean"], 4),
                "max_abs_act": round(tel["max_abs_act"], 3),
            })
        mean_probes = {
            k: round(float(np.mean([r["probes"][k] for r in seed_rows])), 2)
            for k in eval_probes
        }
        results[name] = {
            "n_params": seed_rows[0]["n_params"],
            "step_ms_median": round(float(np.mean([r["step_ms"] for r in seed_rows])), 2),
            "mean_probes_pct": mean_probes,
            "first_tok_attn_mean": round(float(np.mean([r["first_tok_attn_mean"] for r in seed_rows])), 4),
            "max_abs_act": round(float(np.mean([r["max_abs_act"] for r in seed_rows])), 3),
            "seeds": seed_rows,
        }
        print(
            f"[B1 Recall] {name:35s} | mq4={mean_probes['mq4']:5.1f}% "
            f"swap={mean_probes['value_swap']:5.1f}% fill20={mean_probes['filler20_gtW']:5.1f}% "
            f"ex6={mean_probes['extrap_6pair']:5.1f}% ord8={mean_probes['ordered_8pair_T96']:5.1f}% "
            f"| sink={results[name]['first_tok_attn_mean']:.3f} max_act={results[name]['max_abs_act']:.2f}",
            flush=True,
        )
    return {"steps": steps, "T_train_active": [40, 48], "T_max": T_max, "queries_per_seq": 4, "architectures": results}


def run_part_b2_wide10m(
    existing: Dict[str, Any] | None = None, arch_slice: tuple[int, int] | None = None
) -> Dict[str, Any]:
    """Raw-byte WikiText-2 wide10m (d=512, L=4, 2 seeds, 160 steps) + BCIRQ8 & COMP-1 PTQ."""
    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")
    val_tk, val_tg = make_fixed_eval_batches(va_stream, 12, 4, 64)
    test_tk, test_tg = make_fixed_eval_batches(te_stream, 16, 4, 64)
    seeds = [42, 1337]
    steps = 160

    results: Dict[str, Any] = dict((existing or {}).get("architectures", {}))
    catalog = arch_catalog_4l()
    if arch_slice is not None:
        catalog = catalog[arch_slice[0] : arch_slice[1]]
    for cfg in catalog:
        name = cfg["name"]
        seed_rows = []
        for sd in seeds:
            tr_tk, tr_tg = sample_lm_batches(tr_stream, steps, 4, 64, seed=3000 + sd)
            sp = make_spec(
                vocab=256, width=512, heads=8, kvheads=2, layers=4, ff=1536,
                batch=4, time_len=64, layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
                swa_window=16, delta_chunk=16, mla_dc=192, mla_dr=32,
                mup_d_base=128, seed=sd, threads=2,
                gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
                canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
                online_softmax=0, gate_rank=64,
            )
            pol = make_policy(opt_kind=OPT_ADAFACTOR_M2)
            lrs = cosine_lr_schedule(steps, 3e-3, 3e-4, 16)
            opt_cfg = AdamWParams(3e-3, 0.9, 0.95, 1e-8, 0.01, 3.0)
            with Abi6Model(sp, pol) as model:
                res = train_with_val_selection(
                    model, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg,
                    opt_cfg, lrs, eval_every=40,
                )
                if res["rc"] != 0 or res["steps_completed"] != steps:
                    raise RuntimeError(
                        f"[B2] {name} seed={sd} incomplete: rc={res['rc']} "
                        f"steps={res['steps_completed']}/{steps}"
                    )
                base_w = res["best_weights"]
                # Zero-QAT BCIRQ8 (g=32) evaluation
                model.set_weights(quantize_bcirq8_g32(base_w))
                q8_res = model.evaluate(test_tk, test_tg)
                # Zero-QAT COMP-1 PTQ (Q8 Attention + Dual-SVID 1-bit MLP) evaluation
                comp1_w = apply_comp1_ptq_weights(model, base_w, rank=16)
                model.set_weights(comp1_w)
                c1_res = model.evaluate(test_tk, test_tg)
                ms = res["mem_stats"]
                tel = res["eval_telemetry"]

            kv4k_mib = (ms["infer_kv_bytes_per_tok"] * 4096) / (1024 * 1024)
            rec_mib = ms["infer_rec_state_bytes"] / (1024 * 1024)
            w_fp32_mib = ms["weight_bytes"] / (1024 * 1024)
            w_q8_mib = (ms["n_params"] * 8.25 / 8.0) / (1024 * 1024)
            w_c1_mib = ((ms["n_params"] - ms["mlp_params"]) * 8.25 / 8.0 + ms["mlp_params"] * 1.04 / 8.0) / (1024 * 1024)
            seed_rows.append({
                "seed": sd,
                "n_params": ms["n_params"],
                "test_ce": round(res["test_ce"], 6),
                "test_ppl": round(res["test_ppl"], 4),
                "q8_test_ce": round(q8_res["ce"], 6),
                "q8_delta_ce": round(q8_res["ce"] - res["test_ce"], 6),
                "comp1_ptq_ce": round(c1_res["ce"], 6),
                "comp1_ptq_delta_ce": round(c1_res["ce"] - res["test_ce"], 6),
                "step_ms_median": res["step_ms_median"],
                "wall_s": res["wall_s"],
                "first_tok_attn_mean": round(tel["first_tok_attn_mean"], 4),
                "max_abs_act": round(tel["max_abs_act"], 3),
                "state_4k_mib": round(kv4k_mib + rec_mib, 3),
                "kv_4k_mib": round(kv4k_mib, 3),
                "rec_mib": round(rec_mib, 3),
                "total_4k_fp32_mib": round(w_fp32_mib + kv4k_mib + rec_mib, 2),
                "total_4k_q8_mib": round(w_q8_mib + kv4k_mib + rec_mib, 2),
                "total_4k_comp1_mib": round(w_c1_mib + kv4k_mib + rec_mib, 2),
                "curve": res["curve"],
            })
        mean_ce = float(np.mean([r["test_ce"] for r in seed_rows]))
        results[name] = {
            "n_params": seed_rows[0]["n_params"],
            "mean_test_ce": round(mean_ce, 6),
            "std_test_ce": round(float(np.std([r["test_ce"] for r in seed_rows])), 6),
            "mean_test_ppl": round(float(math.exp(mean_ce)), 4),
            "mean_q8_delta_ce": round(float(np.mean([r["q8_delta_ce"] for r in seed_rows])), 6),
            "mean_comp1_ptq_delta_ce": round(float(np.mean([r["comp1_ptq_delta_ce"] for r in seed_rows])), 6),
            "mean_step_ms": round(float(np.mean([r["step_ms_median"] for r in seed_rows])), 2),
            "mean_wall_s": round(float(np.mean([r["wall_s"] for r in seed_rows])), 2),
            "first_tok_attn_mean": round(float(np.mean([r["first_tok_attn_mean"] for r in seed_rows])), 4),
            "max_abs_act": round(float(np.mean([r["max_abs_act"] for r in seed_rows])), 3),
            "kv_4k_mib": seed_rows[0]["kv_4k_mib"],
            "rec_mib": seed_rows[0]["rec_mib"],
            "state_4k_mib": seed_rows[0]["state_4k_mib"],
            "total_4k_q8_mib": seed_rows[0]["total_4k_q8_mib"],
            "total_4k_comp1_mib": seed_rows[0]["total_4k_comp1_mib"],
            "seeds": seed_rows,
        }
        print(
            f"[B2 wide10m] {name:35s} | P={results[name]['n_params']/1e6:5.2f}M "
            f"| CE={mean_ce:.4f} PPL={results[name]['mean_test_ppl']:5.2f} "
            f"| Q8d={results[name]['mean_q8_delta_ce']:+.4f} C1d={results[name]['mean_comp1_ptq_delta_ce']:+.4f} "
            f"| sink={results[name]['first_tok_attn_mean']:.3f} maxAct={results[name]['max_abs_act']:5.2f} "
            f"| st4k={results[name]['state_4k_mib']:4.2f}M | {results[name]['mean_step_ms']:5.1f}ms",
            flush=True,
        )

    if "GQA_Full" in results:
        gqa_ce = results["GQA_Full"]["mean_test_ce"]
        for rec in results.values():
            rec["delta_ce_vs_gqa"] = round(rec["mean_test_ce"] - gqa_ce, 6)
    return {"steps": steps, "tokens_per_run": steps * 4 * 64, "architectures": results}


def run_part_b3_online_memory() -> Dict[str, Any]:
    """TRAIN-OPT-5 O(T) vs O(T^2) attention memory scaling across T=64..1024."""
    rows = []
    for arch_name, mixers, ropes in [
        ("GQA_Full", [MIXER_GQA] * 4, [1] * 4),
        ("Arch7c_GDN2_G1NoPEMLA_CanonAC", [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA], [0, 0, 0, 0]),
    ]:
        for T in (64, 256, 512, 1024):
            entry = {"arch": arch_name, "T": T}
            for onl in (0, 1):
                sp = make_spec(
                    vocab=256, width=512, heads=8, kvheads=2, layers=4, ff=1536,
                    batch=4, time_len=T, layer_mixers=mixers, layer_use_rope=ropes,
                    mla_dc=192, mla_dr=32, skip_init=1, threads=1,
                    online_softmax=onl,
                    gate_mode=GATE_G1_HEADWISE if "G1" in arch_name else GATE_NONE,
                    canon_mode=CANON_AC if "Canon" in arch_name else CANON_OFF,
                )
                pol = make_policy(opt_kind=OPT_ADAFACTOR_M2)
                with Abi6Model(sp, pol) as model:
                    ms = model.mem_stats()
                key = "online" if onl else "materialized"
                entry[f"{key}_prob_mib"] = round(ms["mixer_prob_bytes"] / (1024 * 1024), 3)
                entry[f"{key}_total_mib"] = round(ms["total_bytes"] / (1024 * 1024), 2)
            entry["prob_reduction_x"] = round(
                entry["materialized_prob_mib"] / max(entry["online_prob_mib"], 1e-6), 2
            )
            rows.append(entry)
            print(
                f"[B3 TRAIN-OPT-5] {arch_name:30s} T={T:4d} | prob: "
                f"{entry['materialized_prob_mib']:7.2f} -> {entry['online_prob_mib']:6.2f} MiB "
                f"({entry['prob_reduction_x']:5.1f}x) | total: "
                f"{entry['materialized_total_mib']:6.1f} -> {entry['online_total_mib']:6.1f} MiB",
                flush=True,
            )
    return {"scaling": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["b1", "b2", "b3", "all"], default="all")
    ap.add_argument("--arch-slice", type=str, default=None, help="start:end slice for b2")
    args = ap.parse_args()
    out_path = RESULTS_DIR / "exp_b_arch_ladder_abi6.json"
    data: Dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"host": host_info()}
    sl = None
    if args.arch_slice:
        a, b = args.arch_slice.split(":")
        sl = (int(a), int(b))
    if args.part in ("b1", "all"):
        data["b1_recall_suite"] = run_part_b1_recall()
        save_json(out_path, data)
    if args.part in ("b2", "all"):
        data["b2_wide10m_lm"] = run_part_b2_wide10m(data.get("b2_wide10m_lm"), sl)
        save_json(out_path, data)
    if args.part in ("b3", "all"):
        data["b3_online_softmax_memory"] = run_part_b3_online_memory()
        save_json(out_path, data)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
