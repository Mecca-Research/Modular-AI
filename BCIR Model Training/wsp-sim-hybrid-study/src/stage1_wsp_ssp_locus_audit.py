#!/usr/bin/env python3
"""Stage 1: Gate 0 Stateful Parity + Real 10M Organic Loop Diagnosis +
18-Arm Cross-Locus WSP / SSP Architectural Audit on BCIR-Arch7d-10M.

Supports crash-atomic per-arm resumption (results/stage1_partial_arms.json) and
strictly enforces WSP-1..3 discipline:
  - All behavioral diagnosis, contrast probing, and hyperparameter selection run
    EXCLUSIVELY on WikiText-2 validation (`evolve`) splits.
  - Final evaluation runs ONCE on strictly disjoint WikiText-2 test (`heldout`) splits.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from common_wsp_sim import (
    AdamWParams,
    Abi6Model,
    CANON_OFF,
    CKPT_DIR,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GQA,
    RESULTS_DIR,
    SspConfig,
    StatefulProbe,
    audit_weight_health,
    composite_behavior_score,
    compute_kl_and_ce,
    compute_rollout_metrics,
    extract_domain_prompts,
    get_host_provenance,
    label_loop_tokens,
    load_arch7d_10m_tracked,
    load_wikitext_bytes,
    make_eval_batches,
    make_policy,
    make_spec,
    make_ssp_config,
    make_wsp1_weight_patch,
    write_csv,
    write_json,
)


def gate0_verify_stateful_parity(model10m: Abi6Model) -> Dict[str, Any]:
    """Verify exact incremental stateful decoder matches unmodified BCIR bcir_ext_forward."""
    results = {}
    rng = np.random.default_rng(20261127)
    T_check = 48
    toks = rng.integers(0, 256, size=(int(model10m.spec.batch), T_check), dtype=np.uint32)
    ref_logits = model10m.forward_logits(toks, active_time=T_check)[0]
    with StatefulProbe(model10m, max_seq=128) as sp:
        sp.reset()
        got = np.stack([sp.step(int(toks[0, t])) for t in range(T_check)], axis=0)
    diff_10m = float(np.max(np.abs(ref_logits - got)))
    results["arch7d_10m_max_abs_diff"] = diff_10m
    assert diff_10m < 1e-4, f"Arch7d-10M stateful parity failed: {diff_10m}"

    sp_gqa = make_spec(
        width=128, heads=4, kvheads=2, layers=2, ff=256, batch=1, time_len=32,
        layer_mixers=[MIXER_GQA, MIXER_GQA], layer_use_rope=[1, 1],
        gate_mode=GATE_NONE, qk_norm=0, canon_mode=CANON_OFF, mhc_streams=1,
        online_softmax=0, seed=7, threads=1,
    )
    with Abi6Model(sp_gqa, make_policy()) as mgqa:
        tk = rng.integers(0, 256, size=(1, 24), dtype=np.uint32)
        ref = mgqa.forward_logits(tk, active_time=24)[0]
        with StatefulProbe(mgqa, max_seq=64) as sp:
            got_gqa = np.stack([sp.step(int(tk[0, t])) for t in range(24)], axis=0)
        diff_gqa = float(np.max(np.abs(ref - got_gqa)))
        results["gqa_rope_max_abs_diff"] = diff_gqa
        assert diff_gqa < 1e-4, f"GQA stateful parity failed: {diff_gqa}"

    sp_a2 = make_spec(
        width=128, heads=4, kvheads=2, layers=4, ff=256, batch=1, time_len=32,
        layer_mixers=[MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
        layer_use_rope=[0, 0, 0, 1],
        gate_mode=GATE_NONE, qk_norm=0, canon_mode=CANON_OFF, mhc_streams=1,
        online_softmax=0, seed=11, threads=1,
    )
    with Abi6Model(sp_a2, make_policy()) as ma2:
        tk = rng.integers(0, 256, size=(1, 24), dtype=np.uint32)
        ref = ma2.forward_logits(tk, active_time=24)[0]
        with StatefulProbe(ma2, max_seq=64) as sp:
            got_a2 = np.stack([sp.step(int(tk[0, t])) for t in range(24)], axis=0)
        diff_a2 = float(np.max(np.abs(ref - got_a2)))
        results["arch2_deltanet_gqa_max_abs_diff"] = diff_a2
        assert diff_a2 < 1e-4, f"Arch-2 stateful parity failed: {diff_a2}"

    results["all_pass"] = True
    return results


def eval_arm(
    model: Abi6Model,
    probe: StatefulProbe,
    w_base: np.ndarray,
    w_cand: np.ndarray,
    ssp: Optional[SspConfig],
    prompts: np.ndarray,
    gen_len: int,
    eval_xs: np.ndarray,
    eval_ys: np.ndarray,
    ref_logits: List[np.ndarray],
    base_ce: float,
) -> Dict[str, Any]:
    t0 = time.monotonic()
    model.set_weights(w_cand)
    probe.sync()
    out_toks, out_ent, _ = probe.generate_batch(prompts, gen_len, ssp=ssp)
    roll = compute_rollout_metrics(out_toks, prompts.shape[1], out_ent)
    changed = int(np.count_nonzero(w_cand - w_base))
    if changed > 0:
        ce_res, _ = compute_kl_and_ce(model, eval_xs, eval_ys, ref_logits=ref_logits)
    else:
        ce_res = {
            "ce": base_ce,
            "ppl": float(math.exp(min(base_ce, 50.0))),
            "bpb": float(base_ce / math.log(2.0)),
            "accuracy": None,
            "tokens": int(eval_ys.size),
            "kl_from_clean": 0.0,
            "top1_agreement": 1.0,
            "max_abs_logit_delta": 0.0,
        }
    delta_ce = float(ce_res["ce"] - base_ce)
    kl = float(ce_res["kl_from_clean"])
    score = composite_behavior_score(roll, delta_ce, kl)
    l2 = float(np.linalg.norm((w_cand - w_base).astype(np.float64)))
    sample_texts = [
        bytes(int(b) for b in out_toks[i, prompts.shape[1]:]).decode("utf-8", errors="replace")[:88]
        for i in range(min(3, len(out_toks)))
    ]
    return {
        "score": score,
        "loop_seq_rate": roll["loop_seq_rate"],
        "loop_seq_count": roll["loop_seq_count"],
        "n_sequences": roll["n_sequences"],
        "rep4_rate": roll["rep4_rate"],
        "rep8_rate": roll["rep8_rate"],
        "distinct_4": roll["distinct_4"],
        "distinct_8": roll["distinct_8"],
        "max_8gram_repeats_mean": roll["max_8gram_repeats_mean"],
        "mean_entropy": roll["mean_entropy"],
        "ce": ce_res["ce"],
        "ppl": ce_res["ppl"],
        "delta_ce": delta_ce,
        "kl_from_clean": kl,
        "top1_agreement": ce_res["top1_agreement"],
        "max_abs_logit_delta": ce_res["max_abs_logit_delta"],
        "changed_params": changed,
        "changed_pct": 100.0 * changed / float(w_base.size),
        "edit_l2": l2,
        "eval_seconds": time.monotonic() - t0,
        "sample_generations": sample_texts,
    }


def compute_layer_diagnostics(
    model: Abi6Model, contrast: Dict[str, Any], w_base: np.ndarray
) -> Dict[str, Any]:
    tmap = model.tensor_map(w_base)
    L = int(model.spec.layers)
    H = int(model.spec.heads)
    diag: Dict[str, Any] = {
        "loop_tokens": contrast["loop_tokens"],
        "clean_tokens": contrast["clean_tokens"],
        "layers": {},
    }
    for l in range(L):
        diff_resid = contrast["resid_loop"][l] - contrast["resid_clean"][l]
        rn = float(np.linalg.norm(diff_resid))
        u = diff_resid / max(rn, 1e-12)

        wdown = tmap[f"L{l}.wdown"].astype(np.float64)
        proj = u @ wdown
        da_signed = contrast["mlp_hid_loop"][l] - contrast["mlp_hid_clean"][l]
        da_abs = contrast["mlp_hid_abs_loop"][l] - contrast["mlp_hid_abs_clean"][l]
        col_write_score = da_signed * proj
        col_rank_score = np.abs(col_write_score) + 0.25 * np.abs(proj) * np.maximum(da_abs, 0.0)
        top_cols = np.argsort(-col_rank_score).tolist()

        g1_diff = (contrast["g1_loop"][l] - contrast["g1_clean"][l]).tolist()
        attn_rep_diff = (contrast["attn_rep_loop"][l] - contrast["attn_rep_clean"][l]).tolist()
        st_echo_diff = (contrast["st_echo_loop"][l] - contrast["st_echo_clean"][l]).tolist()
        if int(model.spec.layer_mixer[l]) in (MIXER_GQA, 4):
            head_scores = [
                float(contrast["attn_rep_loop"][l, h] * (1.0 + max(0.0, g1_diff[h])) + 0.5 * attn_rep_diff[h])
                for h in range(H)
            ]
        else:
            head_scores = [
                float(contrast["st_echo_loop"][l, h] * max(0.05, contrast["g1_loop"][l, h]) + 0.5 * st_echo_diff[h])
                for h in range(H)
            ]
        top_heads = sorted(range(H), key=lambda h: -head_scores[h])

        alpha_diff = contrast["alpha_loop"][l] - contrast["alpha_clean"][l]
        erase_diff = contrast["erase_loop"][l] - contrast["erase_clean"][l]
        chan_echo_score = contrast["alpha_loop"][l] * (1.0 - contrast["erase_loop"][l]) + 0.5 * (alpha_diff - erase_diff)
        top_chans = np.argsort(-chan_echo_score).tolist()

        diag["layers"][f"L{l}"] = {
            "mixer": int(model.spec.layer_mixer[l]),
            "resid_contrast_norm": rn,
            "u_vec": u,
            "col_proj": proj,
            "col_rank_score": col_rank_score,
            "top_cols_16": top_cols[:16],
            "top_cols_all": top_cols,
            "g1_loop_mean": contrast["g1_loop"][l].tolist(),
            "g1_clean_mean": contrast["g1_clean"][l].tolist(),
            "g1_diff": g1_diff,
            "attn_rep_loop": contrast["attn_rep_loop"][l].tolist(),
            "attn_rep_clean": contrast["attn_rep_clean"][l].tolist(),
            "st_echo_loop": contrast["st_echo_loop"][l].tolist(),
            "st_echo_clean": contrast["st_echo_clean"][l].tolist(),
            "head_loop_scores": head_scores,
            "top_heads": top_heads,
            "alpha_loop_mean": float(np.mean(contrast["alpha_loop"][l])),
            "alpha_clean_mean": float(np.mean(contrast["alpha_clean"][l])),
            "erase_loop_mean": float(np.mean(contrast["erase_loop"][l])),
            "erase_clean_mean": float(np.mean(contrast["erase_clean"][l])),
            "top_gdn_chans": top_chans,
        }
    return diag


def apply_wdown_sparse_deflation(
    model: Abi6Model, w_base: np.ndarray, layer: int, u_vec: np.ndarray, cols: Sequence[int], alpha: float
) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    W = tmap[f"L{layer}.wdown"]
    u = u_vec.astype(np.float64)
    u = u / max(float(np.linalg.norm(u)), 1e-12)
    idx = np.asarray(cols, dtype=np.int64)
    sub = W[:, idx].astype(np.float64)
    proj = u @ sub
    W[:, idx] = (sub - alpha * np.outer(u, proj)).astype(np.float32)
    return w


def apply_blind_svd_deflation(model: Abi6Model, w_base: np.ndarray, layer: int, alpha: float) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    W = tmap[f"L{layer}.wdown"]
    mat = W.astype(np.float64)
    rng = np.random.default_rng(20261104 + layer)
    u = rng.standard_normal(mat.shape[0])
    u /= np.linalg.norm(u)
    for _ in range(20):
        u = mat @ (mat.T @ u)
        u /= np.linalg.norm(u)
    W[:] = (mat - alpha * np.outer(u, u @ mat)).astype(np.float32)
    return w


def apply_g1_bias_edit(model: Abi6Model, w_base: np.ndarray, layer: int, heads: Sequence[int], delta_bg: float) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    bg = tmap[f"L{layer}.bg"]
    for h in heads:
        bg[0, int(h)] = np.float32(float(bg[0, int(h)]) + delta_bg)
    return w


def apply_head_wo_zero(model: Abi6Model, w_base: np.ndarray, layer: int, heads: Sequence[int], scale: float = 0.0) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    wo = tmap[f"L{layer}.wo"]
    dh = int(model.spec.width // model.spec.heads)
    for h in heads:
        wo[:, int(h) * dh:(int(h) + 1) * dh] *= np.float32(scale)
    return w


def apply_gdn2_state_gate_edit(
    model: Abi6Model, w_base: np.ndarray, layers: Sequence[int], diag: Dict[str, Any],
    top_k_chans: int, delta_alpha: float, delta_erase: float,
) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    for l in layers:
        chans = diag["layers"][f"L{l}"]["top_gdn_chans"][:top_k_chans]
        ba = tmap[f"L{l}.ba_chan"]
        be = tmap[f"L{l}.be_chan"]
        for c in chans:
            ba[0, int(c)] = np.float32(float(ba[0, int(c)]) + delta_alpha)
            be[0, int(c)] = np.float32(float(be[0, int(c)]) + delta_erase)
    return w


def apply_canon_scale_edit(model: Abi6Model, w_base: np.ndarray, layers: Sequence[int], tap_scale: float) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    for l in layers:
        for suf in ("canon_a", "canon_c"):
            nm = f"L{l}.{suf}"
            if nm in tmap:
                tmap[nm][1:, :] *= np.float32(tap_scale)
    return w


def apply_mhc_mix_edit(model: Abi6Model, w_base: np.ndarray, delta_offdiag: float) -> np.ndarray:
    w = w_base.copy()
    tmap = model.tensor_map(w)
    for l in range(int(model.spec.layers)):
        for nm in (f"L{l}.mhc_hres1", f"L{l}.mhc_hres2"):
            if nm in tmap:
                m = tmap[nm]
                m[0, 1] += np.float32(delta_offdiag)
                m[1, 0] += np.float32(delta_offdiag)
    return w


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-arm", type=int, default=1)
    ap.add_argument("--end-arm", type=int, default=18)
    args = ap.parse_args()

    t_start = time.monotonic()
    print(f"=== Stage 1 (Arms {args.start_arm}..{args.end_arm}): 18-Arm WSP/SSP Locus Audit on BCIR-Arch7d-10M ===", flush=True)

    model, w_base, ckpt_meta = load_arch7d_10m_tracked(batch=4, time_len=256, threads=2)
    base_sha = ckpt_meta["weights_sha256"]

    g0 = gate0_verify_stateful_parity(model)
    print(f"[Gate 0] PASS: Arch7d-10M={g0['arch7d_10m_max_abs_diff']:.2e}, "
          f"GQA={g0['gqa_rope_max_abs_diff']:.2e}, Arch2={g0['arch2_deltanet_gqa_max_abs_diff']:.2e}", flush=True)

    health = audit_weight_health(model, w_base)
    val_stream = load_wikitext_bytes("val")
    test_stream = load_wikitext_bytes("test")
    train_stream = load_wikitext_bytes("train")

    prompt_len = 48
    gen_len = 80
    evolve_prompts = extract_domain_prompts(val_stream, "prose_unloop", n_prompts=12, prompt_len=prompt_len, seed=101)
    heldout_prompts = extract_domain_prompts(test_stream, "prose_unloop", n_prompts=32, prompt_len=prompt_len, seed=202)
    evolve_xs, evolve_ys = make_eval_batches(val_stream, batch=4, seq_len=256, n_batches=3, seed=301)   # 3,072 bytes
    heldout_xs, heldout_ys = make_eval_batches(test_stream, batch=4, seq_len=256, n_batches=8, seed=401)  # 8,192 bytes

    probe = StatefulProbe(model, max_seq=256)
    model.set_weights(w_base)
    probe.sync()
    ev_ref_ce, ev_ref_logits = compute_kl_and_ce(model, evolve_xs, evolve_ys)
    te_ref_ce, te_ref_logits = compute_kl_and_ce(model, heldout_xs, heldout_ys)

    # Evolve-only behavioral rollout + 100% gradient-free activation contrast probe
    ev_toks, ev_ent, _ = probe.generate_batch(evolve_prompts, gen_len, ssp=None)
    ev_roll = compute_rollout_metrics(ev_toks, prompt_len, ev_ent)
    mask_loop = label_loop_tokens(ev_toks, prompt_len)
    t_probe = time.monotonic()
    contrast = probe.probe_contrast(ev_toks, mask_loop)
    diag = compute_layer_diagnostics(model, contrast, w_base)
    probe_sec = time.monotonic() - t_probe
    print(f"[Diagnosis on Evolve] loop={ev_roll['loop_seq_rate']*100:.1f}% rep4={ev_roll['rep4_rate']*100:.1f}% "
          f"dist4={ev_roll['distinct_4']*100:.1f}% | probe={probe_sec:.2f}s (loop_tok={diag['loop_tokens']}, clean_tok={diag['clean_tokens']})", flush=True)
    for l in range(int(model.spec.layers)):
        ld = diag["layers"][f"L{l}"]
        print(f"  L{l} (mixer={ld['mixer']}): ||du||={ld['resid_contrast_norm']:.3f} | "
              f"top_head={ld['top_heads'][0]} | g1_loop={np.mean(ld['g1_loop_mean']):.3f} vs clean={np.mean(ld['g1_clean_mean']):.3f} | "
              f"attn_rep_max={max(ld['attn_rep_loop']):.3f} | st_echo={np.mean(ld['st_echo_loop']):.3f}", flush=True)

    partial_path = RESULTS_DIR / "stage1_partial_arms.json"
    cache = json.loads(partial_path.read_text(encoding="utf-8")) if partial_path.exists() else {"rows": {}, "details": {}}

    def run_selection_arm(
        arm_num: int,
        arm_id: str,
        tier: str,
        locus: str,
        candidates: List[Tuple[Dict[str, Any], np.ndarray, Optional[SspConfig]]],
        max_evolve_kl: float = 0.08,
    ) -> Optional[Tuple[Dict[str, Any], np.ndarray, Optional[SspConfig]]]:
        if arm_num < args.start_arm or arm_num > args.end_arm:
            return None
        best_ev = None
        best_pack = None
        ev_trace = []
        for cfg, w_c, ssp_c in candidates:
            ev_m = eval_arm(
                model, probe, w_base, w_c, ssp_c, evolve_prompts, gen_len,
                evolve_xs, evolve_ys, ev_ref_logits, ev_ref_ce["ce"],
            )
            ev_trace.append({"cfg": cfg, "evolve_score": round(ev_m["score"], 3),
                             "evolve_loop_rate": round(ev_m["loop_seq_rate"], 4),
                             "evolve_delta_ce": round(ev_m["delta_ce"], 5),
                             "evolve_kl": round(ev_m["kl_from_clean"], 6)})
            valid = ev_m["kl_from_clean"] <= max_evolve_kl
            key = (1 if valid else 0, ev_m["score"])
            if best_ev is None or key > best_ev[0]:
                best_ev = (key, ev_m)
                best_pack = (cfg, w_c, ssp_c)
        assert best_pack is not None and best_ev is not None
        best_cfg, best_w, best_ssp = best_pack
        te_m = eval_arm(
            model, probe, w_base, best_w, best_ssp, heldout_prompts, gen_len,
            heldout_xs, heldout_ys, te_ref_logits, te_ref_ce["ce"],
        )
        row = {
            "arm_num": arm_num,
            "arm_id": arm_id,
            "tier": tier,
            "locus": locus,
            "selected_cfg": json.dumps(best_cfg, sort_keys=True),
            "evolve_score": round(best_ev[1]["score"], 3),
            "test_score": round(te_m["score"], 3),
            "test_loop_seq_rate": round(te_m["loop_seq_rate"], 4),
            "test_rep4_rate": round(te_m["rep4_rate"], 4),
            "test_rep8_rate": round(te_m["rep8_rate"], 4),
            "test_distinct4": round(te_m["distinct_4"], 4),
            "test_ce": round(te_m["ce"], 5),
            "test_ppl": round(te_m["ppl"], 4),
            "test_delta_ce": round(te_m["delta_ce"], 5),
            "test_kl": round(te_m["kl_from_clean"], 6),
            "test_top1_agree": round(te_m["top1_agreement"], 5),
            "changed_params": te_m["changed_params"],
            "changed_pct": round(te_m["changed_pct"], 6),
            "edit_l2": round(te_m["edit_l2"], 5),
            "test_eval_s": round(te_m["eval_seconds"], 2),
        }
        cache["rows"][arm_id] = row
        cache["details"][arm_id] = {
            "tier": tier, "locus": locus, "selected_cfg": best_cfg,
            "evolve_candidates": ev_trace, "evolve_selected": best_ev[1], "heldout_test": te_m,
        }
        write_json(partial_path, cache)
        print(f"[{arm_id}] test_score={te_m['score']:.2f} | loop={te_m['loop_seq_rate']*100:.1f}% "
              f"rep4={te_m['rep4_rate']*100:.1f}% dist4={te_m['distinct_4']*100:.1f}% | "
              f"dCE={te_m['delta_ce']:+.4f} KL={te_m['kl_from_clean']:.5f} | "
              f"changed={te_m['changed_params']} ({te_m['changed_pct']:.4f}%) | cfg={best_cfg}", flush=True)
        return best_cfg, best_w, best_ssp

    # Arm 01: Clean Baseline
    run_selection_arm(1, "01_Baseline_Clean", "Baseline", "None", [({"mode": "clean"}, w_base, None)])

    # Arm 02: Exact 50M Exp D Baseline (Blind Weight-Only SVD, dense 100% of L2.wdown)
    run_selection_arm(
        2, "02_Control_50M_ExpD_BlindSVD_L2", "Control_50M_ExpD", "L2.wdown (100% dense, blind SVD)",
        [({"layer": 2, "alpha": a}, apply_blind_svd_deflation(model, w_base, 2, a), None) for a in (0.1, 0.3, 1.0)],
        max_evolve_kl=10.0,
    )

    # Arms 03-06: Per-Layer Activation-Contrast Sparse Top-k wdown (L0, L1, L2 GDN-2 vs L3 NoPE-MLA!)
    for l in range(int(model.spec.layers)):
        ld = diag["layers"][f"L{l}"]
        mname = "MLA" if l == 3 else "GDN2"
        cands = [
            ({"layer": l, "k": k, "alpha": a},
             apply_wdown_sparse_deflation(model, w_base, l, ld["u_vec"], ld["top_cols_all"][:k], a), None)
            for k, a in ((16, 1.0), (32, 0.8), (32, 1.4), (64, 0.8), (64, 1.4), (128, 1.2))
        ]
        run_selection_arm(3 + l, f"{3+l:02d}_WSP_L{l}_{mname}_wdown_SparseTopK", "Tier2_WSP", f"L{l}.wdown (sparse top-k)", cands)

    # Arm 07: Ablation — L3.wdown Dense Rank-1 (100% cols) with SAME activation-contrast u_3
    ld3 = diag["layers"]["L3"]
    all_cols = list(range(int(model.spec.ff)))
    run_selection_arm(
        7, "07_WSP_L3_MLA_wdown_DenseRank1", "Tier2_WSP_Ablation", "L3.wdown (100% dense, act-contrast)",
        [({"layer": 3, "k": len(all_cols), "alpha": a},
          apply_wdown_sparse_deflation(model, w_base, 3, ld3["u_vec"], all_cols, a), None) for a in (0.4, 0.8, 1.4)],
    )

    # Arm 08: Arch-7d Exclusive Ultra-Sparse Locus — L3 G1 Headwise Sigmoid Output Gate (1-3 scalars!)
    cands_g1 = [
        ({"layer": 3, "heads": ld3["top_heads"][:nh], "delta_bg": dbg},
         apply_g1_bias_edit(model, w_base, 3, ld3["top_heads"][:nh], dbg), None)
        for nh, dbg in ((1, -0.8), (1, -1.8), (2, -0.8), (2, -1.8), (3, -1.2))
    ]
    run_selection_arm(8, "08_WSP_L3_MLA_G1_HeadGate", "Tier2_WSP", "L3.bg (1-3 scalars)", cands_g1)

    # Arm 09: Exact Takehi p25c Head-Zeroing / Scaling on L3.wo
    cands_wo = [
        ({"layer": 3, "heads": ld3["top_heads"][:nh], "scale": sc},
         apply_head_wo_zero(model, w_base, 3, ld3["top_heads"][:nh], sc), None)
        for nh, sc in ((1, 0.0), (1, 0.4), (2, 0.4))
    ]
    run_selection_arm(9, "09_WSP_L3_MLA_HeadOut_Scale", "Tier2_WSP", "L3.wo (head columns)", cands_wo)

    # Arm 10: State-Space Parameter Programming (SSP-Weight) on GDN-2 L0-L2 ba_chan / be_chan
    cands_sspw = [
        ({"layers": [0, 1, 2], "top_k_chans": kch, "delta_alpha": da, "delta_erase": de},
         apply_gdn2_state_gate_edit(model, w_base, [0, 1, 2], diag, kch, da, de), None)
        for kch, da, de in ((32, -0.4, 0.4), (64, -0.4, 0.6), (64, -0.8, 1.0), (128, -0.6, 0.8))
    ]
    run_selection_arm(10, "10_SSP_Weight_GDN2_StateGates", "Tier2_SSP_Weight", "L0-L2 ba_chan/be_chan", cands_sspw)

    # Arm 11: Canon-AC 4-tap Conv Locus
    cands_canon = [
        ({"layers": ls, "tap_scale": sc}, apply_canon_scale_edit(model, w_base, ls, sc), None)
        for ls, sc in (([3], 0.6), ([0, 1, 2, 3], 0.6), ([0, 1, 2, 3], 0.85))
    ]
    run_selection_arm(11, "11_WSP_CanonAC_Taps", "Tier2_WSP", "Canon-A/C delayed taps", cands_canon)

    # Arm 12: DeepSeek mHC Birkhoff Stream Mixer Locus (32 params = 0.0003%)
    cands_mhc = [
        ({"delta_offdiag": d_off}, apply_mhc_mix_edit(model, w_base, d_off), None)
        for d_off in (-0.4, 0.4, 0.9)
    ]
    run_selection_arm(12, "12_WSP_mHC_Streams", "Tier2_WSP", "L0-L3 mhc_hres (32 params)", cands_mhc)

    # Arm 13: Tier 1 Live State-Space Programming (SSP-Live: Adaptive GDN-2 Echo Damp + Erase Boost, 0 weight edits!)
    cands_sspl = [
        (cfg, w_base, make_ssp_config(**cfg))
        for cfg in (
            {"state_echo_damp": 0.25, "gdn_alpha_shift": -0.6, "gdn_erase_shift": 0.9, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"state_echo_damp": 0.40, "gdn_alpha_shift": -1.0, "gdn_erase_shift": 1.5, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"state_echo_damp": 0.55, "gdn_alpha_shift": -1.4, "gdn_erase_shift": 2.0, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"state_echo_damp": 0.35, "gdn_alpha_shift": -0.8, "gdn_erase_shift": 1.2, "adaptive_echo_only": 0, "echo_ngram": 4, "echo_window": 48},
        )
    ]
    run_selection_arm(13, "13_SSP_Live_GDN2_EchoDamp", "Tier1_SSP_Live", "Live GDN-2 S_t + gates (0 weight edits)", cands_sspl)

    # Arm 14: Tier 1 Live Global-Attention Repeat-Position Logit Bias (0 weight edits!)
    cands_mlal = [
        (cfg, w_base, make_ssp_config(**cfg))
        for cfg in (
            {"mla_repeat_logit_bias": -1.0, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"mla_repeat_logit_bias": -2.5, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"mla_repeat_logit_bias": -4.0, "adaptive_echo_only": 1, "echo_ngram": 4, "echo_window": 48},
            {"mla_repeat_logit_bias": -2.0, "adaptive_echo_only": 0, "echo_ngram": 4, "echo_window": 48},
        )
    ]
    run_selection_arm(14, "14_SSP_Live_MLA_RepeatBias", "Tier1_SSP_Live", "Live L3 MLA repeat-key bias (0 weight edits)", cands_mlal)

    # Arm 15: Tier 1 Inference Decoding Harness (Adaptive n-gram block + presence penalty, 0 weight edits!)
    cands_dec = [
        (cfg, w_base, make_ssp_config(**cfg))
        for cfg in (
            {"ngram_block_penalty": 1.5, "presence_penalty": 0.0, "rep_window": 32, "echo_ngram": 4, "echo_window": 48, "adaptive_echo_only": 1},
            {"ngram_block_penalty": 3.5, "presence_penalty": 0.0, "rep_window": 32, "echo_ngram": 4, "echo_window": 48, "adaptive_echo_only": 1},
            {"ngram_block_penalty": 5.0, "presence_penalty": 0.15, "rep_window": 32, "echo_ngram": 4, "echo_window": 48, "adaptive_echo_only": 1},
            {"ngram_block_penalty": 5.0, "presence_penalty": 0.25, "rep_window": 48, "echo_ngram": 3, "echo_window": 48, "adaptive_echo_only": 1},
        )
    ]
    run_selection_arm(15, "15_Harness_Decoding_NgramGuard", "Tier1_Harness", "Logit n-gram/presence guard (0 weight edits)", cands_dec)

    # Reconstruct winning L3 WSP & SSP-Live configs from partial cache for Arms 16-17
    if args.start_arm <= 16 <= args.end_arm:
        c06 = cache["details"]["06_WSP_L3_MLA_wdown_SparseTopK"]["selected_cfg"]
        c08 = cache["details"]["08_WSP_L3_MLA_G1_HeadGate"]["selected_cfg"]
        c13 = cache["details"]["13_SSP_Live_GDN2_EchoDamp"]["selected_cfg"]
        ev06 = cache["details"]["06_WSP_L3_MLA_wdown_SparseTopK"]["evolve_selected"]["score"]
        ev08 = cache["details"]["08_WSP_L3_MLA_G1_HeadGate"]["evolve_selected"]["score"]
        if ev06 >= ev08:
            w_pers = apply_wdown_sparse_deflation(model, w_base, 3, ld3["u_vec"], ld3["top_cols_all"][:c06["k"]], c06["alpha"])
            pers_src = f"L3.wdown_k{c06['k']}"
        else:
            w_pers = apply_g1_bias_edit(model, w_base, 3, c08["heads"], c08["delta_bg"])
            pers_src = f"L3.bg_h{len(c08['heads'])}"
        cands_hyb = []
        for ngb, mla_b in ((0.0, -2.0), (2.5, -1.5), (5.0, -1.5)):
            cfg = dict(c13)
            cfg["mla_repeat_logit_bias"] = mla_b
            cfg["ngram_block_penalty"] = ngb
            cfg["wsp_base"] = pers_src
            cands_hyb.append((cfg, w_pers, make_ssp_config(**cfg)))
        run_selection_arm(16, "16_Hybrid_Tier2WSP_plus_Tier1SSP", "Hybrid_Tier1_Tier2", f"{pers_src} + Live SSP + NgramGuard", cands_hyb)

    if args.start_arm <= 17 <= args.end_arm:
        c06 = cache["details"]["06_WSP_L3_MLA_wdown_SparseTopK"]["selected_cfg"]
        w06 = apply_wdown_sparse_deflation(model, w_base, 3, ld3["u_vec"], ld3["top_cols_all"][:c06["k"]], c06["alpha"])
        l3_k = int(c06["k"])
        l3_delta_l2 = float(np.linalg.norm((w06 - w_base).astype(np.float64)))
        rng_ctrl = np.random.default_rng(7771127)
        w_rand = w_base.copy()
        W_r = model.tensor_map(w_rand)["L3.wdown"]
        rand_cols = rng_ctrl.choice(W_r.shape[1], size=l3_k, replace=False)
        rand_u = rng_ctrl.standard_normal(W_r.shape[0])
        rand_u /= np.linalg.norm(rand_u)
        sub_r = W_r[:, rand_cols].astype(np.float64)
        delta_r = np.outer(rand_u, rand_u @ sub_r)
        dn = float(np.linalg.norm(delta_r))
        if dn > 1e-12:
            delta_r *= l3_delta_l2 / dn
        W_r[:, rand_cols] = (sub_r - delta_r).astype(np.float32)
        run_selection_arm(
            17, "17_Control_Random_MatchedNorm", "Control_WSP3", f"L3.wdown random k={l3_k}, matched L2",
            [({"layer": 3, "k": l3_k, "matched_l2": round(l3_delta_l2, 5)}, w_rand, None)],
            max_evolve_kl=10.0,
        )

    if args.start_arm <= 18 <= args.end_arm:
        model.set_weights(w_base)
        model.reset_opt()
        tr_xs, tr_ys = make_eval_batches(train_stream, batch=4, seq_len=256, n_batches=8, seed=999)
        opt_cfg = AdamWParams(lr=2.5e-4, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.05, grad_clip=3.0)
        t_sgd = time.monotonic()
        rc_sgd, _ = model.train_loop(tr_xs, tr_ys, opt_cfg)
        sgd_sec = time.monotonic() - t_sgd
        assert rc_sgd == 0
        w_sgd = model.copy_weights()
        run_selection_arm(
            18, "18_Control_SGD_MicroFT_8steps", "Control_WSP3", "All tensors (8-step Q8-Muon FT)",
            [({"steps": 8, "tokens": 8192, "lr": 2.5e-4, "train_s": round(sgd_sec, 2)}, w_sgd, None)],
            max_evolve_kl=10.0,
        )

    # Finalize full outputs once all 18 arms are present
    if len(cache["rows"]) == 18:
        c06 = cache["details"]["06_WSP_L3_MLA_wdown_SparseTopK"]["selected_cfg"]
        w06 = apply_wdown_sparse_deflation(model, w_base, 3, ld3["u_vec"], ld3["top_cols_all"][:c06["k"]], c06["alpha"])
        c08 = cache["details"]["08_WSP_L3_MLA_G1_HeadGate"]["selected_cfg"]
        w08 = apply_g1_bias_edit(model, w_base, 3, c08["heads"], c08["delta_bg"])
        write_json(CKPT_DIR / "wsp1_patch_L3_mla_wdown.json", make_wsp1_weight_patch(
            patch_id="wsp1_arch7d10m_L3_mla_wdown_sparse", base_sha256=base_sha,
            edited_weights=w06, base_weights=w_base, model=model, patch_kind="low_rank_edit",
            locus="L3.wdown (NoPE-MLA global layer sparse top-k columns)",
            rationale="Activation-contrast loop direction deflated strictly on top-k loop-writing columns of global NoPE-MLA FFN.",
            opt_state_policy="inference_only", eval_summary=cache["details"]["06_WSP_L3_MLA_wdown_SparseTopK"]["heldout_test"],
        ))
        write_json(CKPT_DIR / "wsp1_patch_L3_mla_g1.json", make_wsp1_weight_patch(
            patch_id="wsp1_arch7d10m_L3_mla_g1_gate", base_sha256=base_sha,
            edited_weights=w08, base_weights=w_base, model=model, patch_kind="low_rank_edit",
            locus="L3.bg (NoPE-MLA G1 headwise sigmoid output gate bias)",
            rationale="Ultra-sparse damping of repeat-copying attention heads via Arch-7d G1 gate bias (1-3 scalars).",
            opt_state_policy="inference_only", eval_summary=cache["details"]["08_WSP_L3_MLA_G1_HeadGate"]["heldout_test"],
        ))
        arm_rows = sorted(cache["rows"].values(), key=lambda r: r["arm_num"])
        diag_clean = {
            "loop_tokens": diag["loop_tokens"], "clean_tokens": diag["clean_tokens"],
            "probe_seconds": round(probe_sec, 3),
            "layers": {k: {fk: fv for fk, fv in v.items() if not isinstance(fv, np.ndarray) and fk != "top_cols_all" and fk != "top_gdn_chans"}
                       for k, v in diag["layers"].items()},
        }
        write_json(RESULTS_DIR / "stage1_wsp_ssp_locus_audit.json", {
            "schema": "bcir.wsp_ssp.stage1_locus_audit.v1",
            "provenance": get_host_provenance(),
            "base_checkpoint": ckpt_meta,
            "gate0_stateful_parity": g0,
            "wsp2_health_sentinel": health,
            "splits": {
                "evolve_prompts": {"source": "wikitext2_val", "n": int(evolve_prompts.shape[0]), "prompt_len": prompt_len, "gen_len": gen_len},
                "heldout_prompts": {"source": "wikitext2_test", "n": int(heldout_prompts.shape[0]), "prompt_len": prompt_len, "gen_len": gen_len},
                "evolve_ce_tokens": int(evolve_ys.size), "heldout_ce_tokens": int(heldout_ys.size),
            },
            "evolve_diagnosis": diag_clean,
            "arms_table": arm_rows,
            "arms_detail": cache["details"],
        })
        write_csv(RESULTS_DIR / "stage1_wsp_ssp_locus_audit.csv", arm_rows)
        if partial_path.exists():
            partial_path.unlink()
        print("=== Stage 1 ALL 18 ARMS FINALIZED -> results/stage1_wsp_ssp_locus_audit.json ===", flush=True)

    probe.close()
    model.close()
    print(f"=== Stage 1 chunk ({args.start_arm}..{args.end_arm}) finished in {time.monotonic() - t_start:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
