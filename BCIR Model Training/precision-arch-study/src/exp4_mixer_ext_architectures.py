#!/usr/bin/env python3
"""Experiment 4: MIXER-EXT Comparative Architecture Study & Weight-vs-KV Orthogonality Proof.
Architectures compared under identical BCIR Precision Policy (P1-P6) + mup_v1 + TRAIN-OPT-2:
  1. GQA_Full           : 4x Full GQA + RoPE (baseline)
  2. SWA_Only           : 4x SWA (window W) + RoPE (local-window control)
  3. Arch1_DeltaNet     : 4x Gated DeltaNet (chunkwise WY in FP64, O(1) recurrent state)
  4. Arch2_Hybrid_SWA   : 3:1 Hybrid SWA + Full GQA (3x SWA + 1x Full GQA at L3)
  5. Arch2_Hybrid_DN    : 3:1 Hybrid DeltaNet + Full GQA (3x DeltaNet + 1x Full GQA at L3)
  6. Arch4_MLA          : 4x Multi-Head Latent Attention with decoupled RoPE & absorbed decode

Slices:
  --part a     : Exact associative-recall curriculum, counterfactuals (value_swap, query_change),
                 long-distance filler (> SWA window W), & ordered length extrapolation (pilot4L ~1.6M)
  --part b     : WikiText-2 LM comparison at wide10m (d=512, L=4, ff=1152, ~9.84M, 2 seeds) +
                 2x2 Orthogonality Proof (COMP-1 Weight Compression x KV-Cache Architecture)
  --part merge : Merge into results/exp4_mixer_ext_complete.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bcir_lab_harness as H


def arch_defs_4L(swa_w: int, mla_dc: int, mla_dr: int) -> List[Tuple[str, List[int], Dict[str, int]]]:
    return [
        ("GQA_Full", [H.BCIR_MIXER_GQA] * 4, dict(swa_window=swa_w, mla_dc=mla_dc, mla_dr=mla_dr)),
        ("SWA_Only", [H.BCIR_MIXER_SWA] * 4, dict(swa_window=swa_w, mla_dc=mla_dc, mla_dr=mla_dr)),
        ("Arch1_DeltaNet", [H.BCIR_MIXER_DELTANET] * 4, dict(swa_window=swa_w, delta_chunk=16, mla_dc=mla_dc, mla_dr=mla_dr)),
        ("Arch2_Hybrid_SWA", [H.BCIR_MIXER_SWA, H.BCIR_MIXER_SWA, H.BCIR_MIXER_SWA, H.BCIR_MIXER_GQA], dict(swa_window=swa_w, mla_dc=mla_dc, mla_dr=mla_dr)),
        ("Arch2_Hybrid_DN_GQA", [H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_GQA], dict(swa_window=swa_w, delta_chunk=16, mla_dc=mla_dc, mla_dr=mla_dr)),
        ("Arch4_MLA", [H.BCIR_MIXER_MLA] * 4, dict(swa_window=swa_w, mla_dc=mla_dc, mla_dr=mla_dr)),
    ]


def run_part_a(arch_subset: List[str] | None = None) -> Dict[str, Any]:
    print("\n=== EXP 4A: MIXER-EXT Associative Recall, Counterfactuals, Long-Filler & Extrapolation ===", flush=True)
    # 4-layer pilot (d=192, h=4, kvh=2, ff=384, ~1.5-1.8M params), SWA W=16 so filler=20 exceeds window!
    B, T = 8, 64
    steps = 320
    eval_every = 40
    seeds = [42, 137]

    # Mixed 2..4 pair curriculum with occasional 0..16 filler tokens so models learn general binding
    def make_curriculum(n_steps: int, seed: int) -> Tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(seed)
        tks, tgs = [], []
        for s in range(n_steps):
            npairs = int(rng.choice([2, 3, 4]))
            filler = int(rng.choice([0, 8, 16]))
            tk, tg = H.build_kv_dataset(B, T, n_pairs=npairs, filler_len=filler, seed=int(rng.integers(0, 1 << 30)))
            tks.append(tk)
            tgs.append(tg)
        return np.stack(tks), np.stack(tgs)

    val_tk, val_tg = H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=8, seed=7100)
    val_tk, val_tg = val_tk.reshape(16, B, T), val_tg.reshape(16, B, T)

    T_ord8 = 96
    probe_sets = {
        "kv_single": H.build_kv_dataset(16 * B, T, n_pairs=1, filler_len=0, seed=8101),
        "kv_4pair": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=0, seed=8104),
        "value_swap": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=0, seed=8104, mode="value_swap"),
        "query_change": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=0, seed=8104, mode="query_change"),
        "long_filler24": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=24, seed=8124),
        "ordered_6pair": H.build_kv_dataset(16 * B, T, n_pairs=6, filler_len=0, seed=8106, ordered_query=True),
    }
    probe_sets_b = {k: (v[0].reshape(16, B, T), v[1].reshape(16, B, T)) for k, v in probe_sets.items()}
    ord8_tk, ord8_tg = H.build_kv_dataset(16 * B, T_ord8, n_pairs=8, filler_len=0, seed=8108, ordered_query=True)
    ord8_b = (ord8_tk.reshape(16, B, T_ord8), ord8_tg.reshape(16, B, T_ord8))
    all_probe_keys = list(probe_sets_b.keys()) + ["ordered_8pair"]

    pol = H.make_policy(fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL)
    lrs = H.cosine_lr_schedule(steps, 2.0e-3, 2.0e-4, warmup=25)
    opt = H.BcirAdamwParams(lr=2.0e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)

    out: Dict[str, Any] = {
        "provenance": H.get_host_provenance(),
        "steps": steps,
        "swa_window": 16,
        "ordered_8pair_seq_len": T_ord8,
        "architectures": {},
    }
    archs = arch_defs_4L(swa_w=16, mla_dc=64, mla_dr=16)
    if arch_subset is not None:
        archs = [a for a in archs if a[0] in arch_subset]
    for aname, mixers, extra in archs:
        runs = []
        for sd in seeds:
            tr_tk, tr_tg = make_curriculum(steps, seed=30000 + sd)
            sp = H.make_spec(
                width=192, heads=4, kvheads=2, layers=4, ff=384, batch=B, time_len=T,
                layer_mixers=mixers, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=sd, threads=2, **extra,
            )
            with H.ExtModelHandle(sp, pol) as em:
                res = H.train_with_val_selection(
                    em, tr_tk, tr_tg, val_tk, val_tg,
                    probe_sets_b["kv_4pair"][0], probe_sets_b["kv_4pair"][1],
                    opt, lrs, eval_every,
                )
                # Evaluate validation-selected checkpoint on ALL circuit & counterfactual probes
                em.load_weights(res["best_weights"])
                probes_out = {pk: em.evaluate(pv[0], pv[1]) for pk, pv in probe_sets_b.items()}
            sp96 = H.make_spec(
                width=192, heads=4, kvheads=2, layers=4, ff=384, batch=B, time_len=T_ord8,
                layer_mixers=mixers, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192,
                seed=sd, threads=2, skip_init=1, **extra,
            )
            with H.ExtModelHandle(sp96, pol) as em96:
                em96.load_weights(res["best_weights"])
                probes_out["ordered_8pair"] = em96.evaluate(ord8_b[0], ord8_b[1])
            res["probes"] = probes_out
            res.pop("best_weights", None)
            res.pop("final_weights", None)
            res["seed"] = sd
            runs.append(res)
            p = res["probes"]
            print(
                f"  [KV {aname:20s} s={sd:3d}] params={res['mem_stats']['n_params']:7,d} ms/st={res['median_step_ms']:5.1f} | "
                f"kv1={p['kv_single']['accuracy']*100:5.1f}% kv4={p['kv_4pair']['accuracy']*100:5.1f}% "
                f"swap={p['value_swap']['accuracy']*100:5.1f}% qchg={p['query_change']['accuracy']*100:5.1f}% "
                f"fill24={p['long_filler24']['accuracy']*100:5.1f}% ord6={p['ordered_6pair']['accuracy']*100:5.1f}% "
                f"ord8={p['ordered_8pair']['accuracy']*100:5.1f}%",
                flush=True,
            )
        summary_probes = {}
        for pk in all_probe_keys:
            summary_probes[pk] = {
                "acc_mean": round(float(np.mean([r["probes"][pk]["accuracy"] for r in runs])), 4),
                "ce_mean": round(float(np.mean([r["probes"][pk]["ce"] for r in runs])), 5),
            }
        out["architectures"][aname] = {
            "n_params": runs[0]["mem_stats"]["n_params"],
            "median_step_ms": round(float(np.mean([r["median_step_ms"] for r in runs])), 3),
            "probes_summary": summary_probes,
            "runs": runs,
        }
    return out


def run_part_a2() -> Dict[str, Any]:
    """Multi-query associative recall (4 queries/seq = 4x gradient density) designed to cross the
    induction-head phase transition inside 450 steps and starkly separate SWA_Only (W=16) from
    global/recurrent/hybrid/MLA mixers on long-distance filler (>W) and counterfactual swaps."""
    print("\n=== EXP 4A2: Multi-Query Induction-Head Phase Transition & Long-Filler Separation ===", flush=True)
    B, T = 8, 56
    steps = 450
    eval_every = 50
    pol = H.make_policy(fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL)
    lrs = H.cosine_lr_schedule(steps, 3.0e-3, 3.0e-4, warmup=30)
    opt = H.BcirAdamwParams(lr=3.0e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)

    def make_mq_train(seed: int) -> Tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(seed)
        tks, tgs = [], []
        for _ in range(steps):
            npairs = int(rng.choice([2, 3, 4]))
            filler = int(rng.choice([0, 4, 12]))
            tk, tg = H.build_kv_dataset(B, T, n_pairs=npairs, filler_len=filler, ordered_query=True, seed=int(rng.integers(0, 1 << 30)))
            tks.append(tk)
            tgs.append(tg)
        return np.stack(tks), np.stack(tgs)

    val_tk, val_tg = H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=8, ordered_query=True, seed=7200)
    val_tk, val_tg = val_tk.reshape(16, B, T), val_tg.reshape(16, B, T)
    probes = {
        "mq_4pair": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=0, ordered_query=True, seed=8204),
        "mq_value_swap": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=0, mode="value_swap", ordered_query=True, seed=8204),
        "mq_filler20_gtW": H.build_kv_dataset(16 * B, T, n_pairs=4, filler_len=20, ordered_query=True, seed=8220),
        "mq_extrap_6pair": H.build_kv_dataset(16 * B, T, n_pairs=6, filler_len=0, ordered_query=True, seed=8206),
    }
    probes_b = {k: (v[0].reshape(16, B, T), v[1].reshape(16, B, T)) for k, v in probes.items()}

    out: Dict[str, Any] = {"provenance": H.get_host_provenance(), "steps": steps, "swa_window": 16, "architectures": {}}
    tr_tk, tr_tg = make_mq_train(35042)
    for aname, mixers, extra in arch_defs_4L(swa_w=16, mla_dc=48, mla_dr=8):
        sp = H.make_spec(
            width=128, heads=4, kvheads=2, layers=4, ff=256, batch=B, time_len=T,
            layer_mixers=mixers, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=128, seed=42, threads=2, **extra,
        )
        with H.ExtModelHandle(sp, pol) as em:
            res = H.train_with_val_selection(
                em, tr_tk, tr_tg, val_tk, val_tg,
                probes_b["mq_4pair"][0], probes_b["mq_4pair"][1],
                opt, lrs, eval_every,
            )
            em.load_weights(res["best_weights"])
            pr = {pk: em.evaluate(pv[0], pv[1]) for pk, pv in probes_b.items()}
            res["probes"] = pr
        res.pop("best_weights", None)
        res.pop("final_weights", None)
        out["architectures"][aname] = res
        print(
            f"  [MQ {aname:20s}] ms/st={res['median_step_ms']:5.1f} | "
            f"mq4={pr['mq_4pair']['accuracy']*100:5.1f}% swap={pr['mq_value_swap']['accuracy']*100:5.1f}% "
            f"fill20(>W)={pr['mq_filler20_gtW']['accuracy']*100:5.1f}% ext6={pr['mq_extrap_6pair']['accuracy']*100:5.1f}%",
            flush=True,
        )
    return out


def run_part_b(arch_subset: List[str]) -> Dict[str, Any]:
    print(f"\n=== EXP 4B ({','.join(arch_subset)}): WikiText-2 wide10m (~9.84M) & 2x2 Orthogonality ===", flush=True)
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    steps = 160
    eval_every = 40
    seeds = [42, 137]

    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, 12, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, 24, B, T)
    pol = H.make_policy(fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL)
    lrs = H.cosine_lr_schedule(steps, 1.2e-3, 1.2e-4, warmup=16)
    opt = H.BcirAdamwParams(lr=1.2e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)

    out: Dict[str, Any] = {"provenance": H.get_host_provenance(), "steps": steps, "architectures": {}, "orthogonality_2x2": {}}
    all_archs = [
        ("GQA_Full", [H.BCIR_MIXER_GQA] * 4, dict(swa_window=32, mla_dc=128, mla_dr=32)),
        ("Arch1_DeltaNet", [H.BCIR_MIXER_DELTANET] * 4, dict(swa_window=32, delta_chunk=16, mla_dc=128, mla_dr=32)),
        ("Arch2_Hybrid_SWA", [H.BCIR_MIXER_SWA, H.BCIR_MIXER_SWA, H.BCIR_MIXER_SWA, H.BCIR_MIXER_GQA], dict(swa_window=32, mla_dc=128, mla_dr=32)),
        ("Arch2_Hybrid_DN_GQA", [H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_DELTANET, H.BCIR_MIXER_GQA], dict(swa_window=32, delta_chunk=16, mla_dc=128, mla_dr=32)),
        ("Arch4_MLA", [H.BCIR_MIXER_MLA] * 4, dict(swa_window=32, mla_dc=128, mla_dr=32)),
    ]
    archs = [a for a in all_archs if a[0] in arch_subset]

    for aname, mixers, extra in archs:
        runs = []
        s42_weights = None
        s42_spec = None
        for sd in seeds:
            tr_tk, tr_tg = H.sample_lm_batches(wt_tr, steps, B, T, seed=40000 + sd)
            sp = H.make_spec(
                width=512, heads=8, kvheads=2, layers=4, ff=1152, batch=B, time_len=T,
                layer_mixers=mixers, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=sd, threads=2, **extra,
            )
            with H.ExtModelHandle(sp, pol) as em:
                res = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, eval_every)
                if sd == 42:
                    s42_weights = res["best_weights"].copy()
                    s42_spec = sp
            res.pop("best_weights", None)
            res.pop("final_weights", None)
            res["seed"] = sd
            runs.append(res)
            print(
                f"  [LM {aname:20s} s={sd:3d}] params={res['mem_stats']['n_params']:9,d} "
                f"val={res['best_val_ce']:.4f} test={res['test_ce']:.4f} (ppl={res['test_ppl']:5.2f}) "
                f"ms/st={res['median_step_ms']:6.1f} kv/tok={res['mem_stats']['infer_kv_bytes_per_tok']}B "
                f"rec={res['mem_stats']['infer_rec_state_bytes']}B",
                flush=True,
            )
        out["architectures"][aname] = {
            "n_params": runs[0]["mem_stats"]["n_params"],
            "test_ce_mean": float(np.mean([r["test_ce"] for r in runs])),
            "test_ce_spread": float(np.max([r["test_ce"] for r in runs]) - np.min([r["test_ce"] for r in runs])),
            "test_ppl_mean": float(np.mean([r["test_ppl"] for r in runs])),
            "median_step_ms": float(np.mean([r["median_step_ms"] for r in runs])),
            "infer_kv_bytes_per_tok": runs[0]["mem_stats"]["infer_kv_bytes_per_tok"],
            "infer_rec_state_bytes": runs[0]["mem_stats"]["infer_rec_state_bytes"],
            "runs": runs,
        }

        assert s42_weights is not None and s42_spec is not None
        with H.ExtModelHandle(s42_spec, pol) as em:
            em.load_weights(s42_weights)
            tensors = em.tensors()
            ortho_row = {}
            for dname, afmt, mfmt, lb_r in [
                ("FP32", H.BCIR_DEPLOY_FP32, H.BCIR_DEPLOY_FP32, 0),
                ("BCIRQ8_Uniform", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_BCIRQ8_G32, 0),
                ("COMP1_Q8Attn_LBMLP_r48_res", H.BCIR_DEPLOY_BCIRQ8_G32, H.BCIR_DEPLOY_LB_PACKED1B, 48),
            ]:
                with H.DeployModelHandle(s42_spec, max_kv_len=128) as dm:
                    dm.load_from_ext(em, afmt, mfmt if mfmt != H.BCIR_DEPLOY_LB_PACKED1B else H.BCIR_DEPLOY_FP32)
                    if lb_r > 0:
                        for t in tensors:
                            if t["region"] == 2:
                                mat = s42_weights[t["offset"] : t["offset"] + t["rows"] * t["cols"]].reshape(t["rows"], t["cols"])
                                p1, p2, _ = H.factorize_littlebit_matrix(mat, lb_r, lb_r, iters=5)
                                dm.set_linear_lb(t["idx"], p1, p2, W_ref=mat)
                    st = dm.stats()
                    ev = dm.evaluate(test_tk[:6], test_tg[:6], B, T, T)
                    bn = dm.benchmark(ctx_len=64, decode_iters=15, prefill_iters=2)
                    ev_ok = bool(ev.get("ok", ev["rc"] == 0 and ev["nonfinite"] == 0 and math.isfinite(ev["ce"])))
                    tot_4k = st["total_artifact_bytes"] + st["kv_cache_bytes_at_4096"] + st["rec_state_bytes"]
                    proj_4k = (
                        st["projected_fp16_artifact_bytes"]
                        + st["projected_fp16_kv_bytes_at_4096"]
                        + st["projected_fp32_rec_state_bytes"]
                    )
                    ortho_row[dname] = {
                        "storage_precision": "FP32_nonlin_FP32_KV_FP64_DeltaNet",
                        "weight_mib": round(st["total_artifact_bytes"] / 1024**2, 3),
                        "kv_4096_mib": round(st["kv_cache_bytes_at_4096"] / 1024**2, 3),
                        "rec_state_mib": round(st["rec_state_bytes"] / 1024**2, 3),
                        "total_deploy_4096_mib": round(tot_4k / 1024**2, 3),
                        "body_bpw": round(st["realized_body_bpw"], 3),
                        "total_bpw": round(st["realized_total_bpw"], 3),
                        "projected_fp16_weight_mib": round(st["projected_fp16_artifact_bytes"] / 1024**2, 3),
                        "projected_fp16_kv_4096_mib": round(st["projected_fp16_kv_bytes_at_4096"] / 1024**2, 3),
                        "projected_fp32_rec_state_mib": round(st["projected_fp32_rec_state_bytes"] / 1024**2, 3),
                        "projected_fp16_deploy_4096_mib": round(proj_4k / 1024**2, 3),
                        "projected_fp16_total_bpw": round(st["projected_fp16_total_bpw"], 3),
                        "eval_ok": ev_ok,
                        "nonfinite": int(ev["nonfinite"]),
                        "test_ce": round(ev["ce"], 5) if ev_ok else float("nan"),
                        "test_ppl": round(ev["ppl"], 3) if ev_ok else float("nan"),
                        "decode_us": round(bn["decode_median_us"], 1),
                        "decode_tok_s": round(bn["decode_tok_per_s"], 1),
                    }
            out["orthogonality_2x2"][aname] = ortho_row
            print(
                f"    [Ortho {aname:19s}] FP32(W+KV4k)={ortho_row['FP32']['total_deploy_4096_mib']:5.2f}MiB "
                f"(KV={ortho_row['FP32']['kv_4096_mib']:4.2f}MiB) -> "
                f"COMP1(W+KV4k)={ortho_row['COMP1_Q8Attn_LBMLP_r48_res']['total_deploy_4096_mib']:5.2f}MiB "
                f"[projFP16={ortho_row['COMP1_Q8Attn_LBMLP_r48_res']['projected_fp16_deploy_4096_mib']:5.2f}MiB] "
                f"dCE={ortho_row['COMP1_Q8Attn_LBMLP_r48_res']['test_ce']-ortho_row['FP32']['test_ce']:+.4f}",
                flush=True,
            )

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["a", "a2", "b1", "b2", "merge"], required=True)
    ap.add_argument("--archs", type=str, default="", help="Optional comma-separated architecture subset for --part a")
    ap.add_argument("--merge-existing", action="store_true", help="Merge --archs subset into existing exp4_part_a.json")
    args = ap.parse_args()
    H.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.part == "a":
        subset = [s.strip() for s in args.archs.split(",") if s.strip()] if args.archs else None
        res_a = run_part_a(arch_subset=subset)
        out_p = H.RESULTS_DIR / "exp4_part_a.json"
        if args.merge_existing and out_p.exists():
            prev = json.loads(out_p.read_text())
            merged_archs = dict(prev.get("architectures", {}))
            merged_archs.update(res_a["architectures"])
            order = [a[0] for a in arch_defs_4L(swa_w=16, mla_dc=64, mla_dr=16)]
            res_a["architectures"] = {k: merged_archs[k] for k in order if k in merged_archs}
        H.write_json(out_p, res_a)
    elif args.part == "a2":
        H.write_json(H.RESULTS_DIR / "exp4_part_a2.json", run_part_a2())
    elif args.part == "b1":
        H.write_json(H.RESULTS_DIR / "exp4_part_b1.json", run_part_b(["GQA_Full", "Arch2_Hybrid_SWA", "Arch4_MLA"]))
    elif args.part == "b2":
        H.write_json(H.RESULTS_DIR / "exp4_part_b2.json", run_part_b(["Arch1_DeltaNet", "Arch2_Hybrid_DN_GQA"]))
    elif args.part == "merge":
        pa = json.loads((H.RESULTS_DIR / "exp4_part_a.json").read_text())
        pa2 = json.loads((H.RESULTS_DIR / "exp4_part_a2.json").read_text()) if (H.RESULTS_DIR / "exp4_part_a2.json").exists() else {}
        pb1 = json.loads((H.RESULTS_DIR / "exp4_part_b1.json").read_text())
        pb2 = json.loads((H.RESULTS_DIR / "exp4_part_b2.json").read_text())
        pb = {
            "provenance": pb1["provenance"],
            "steps": pb1["steps"],
            "architectures": {**pb1["architectures"], **pb2["architectures"]},
            "orthogonality_2x2": {**pb1["orthogonality_2x2"], **pb2["orthogonality_2x2"]},
        }
        H.write_json(
            H.RESULTS_DIR / "exp4_mixer_ext_complete.json",
            {"provenance": pa["provenance"], "part_a_kv_circuits": pa, "part_a2_multiquery_phase": pa2, "part_b_lm_and_orthogonality": pb},
        )
        print("Merged -> results/exp4_mixer_ext_complete.json", flush=True)


if __name__ == "__main__":
    main()
