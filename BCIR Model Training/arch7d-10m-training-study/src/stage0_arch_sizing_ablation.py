#!/usr/bin/env python3
"""Stage 0: Gate 0 C verification, 10M architecture sizing/memory/COMP-1 audit,
all-7-optimizer memory audit, and 3-way 10M configuration micro-ablation."""
from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List

import numpy as np

from common_10m import (
    CANON_AC,
    CANON_OFF,
    GATE_G1_HEADWISE,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GDN2,
    MIXER_GQA,
    MIXER_MLA,
    OPT_ADAFACTOR_BF16_GRAD,
    OPT_ADAFACTOR_M2,
    OPT_FP32,
    OPT_MUON_FP32,
    OPT_NAMES,
    OPT_Q8_BF16_GRAD,
    OPT_Q8_MOMENTS,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    STUDY_DIR,
    Abi6Model,
    AdamWParams,
    comp1_weight_bytes,
    cosine_lr_schedule,
    host_info,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    process_rss_mib,
    q8_weight_bytes,
    save_json,
    sample_lm_steps,
    spec_to_dict,
)

SIZING_CANDIDATES: List[Dict[str, Any]] = [
    {
        "id": "Arch7d_10M_A_ff896_gr32_dc192",
        "desc": "3:1 GDN-2 + G1-NoPE-MLA + Canon-AC + mHC2 (d=512, L=4, ff=896, dc=192, gr=32)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 896,
        "mla_dc": 192, "mla_dr": 32, "gate_rank": 32,
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0], "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1,
        "canon_mode": CANON_AC, "mhc_streams": 2,
    },
    {
        "id": "Arch7d_10M_B_ff928_gr32_dc128",
        "desc": "3:1 GDN-2 + G1-NoPE-MLA + Canon-AC + mHC2 (d=512, L=4, ff=928, dc=128, gr=32)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 928,
        "mla_dc": 128, "mla_dr": 32, "gate_rank": 32,
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0], "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1,
        "canon_mode": CANON_AC, "mhc_streams": 2,
    },
    {
        "id": "Arch7d_10M_C_ff832_gr64_dc192",
        "desc": "3:1 GDN-2 + G1-NoPE-MLA + Canon-AC + mHC2 (d=512, L=4, ff=832, dc=192, gr=64)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 832,
        "mla_dc": 192, "mla_dr": 32, "gate_rank": 64,
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0], "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1,
        "canon_mode": CANON_AC, "mhc_streams": 2,
    },
    {
        "id": "Arch7d_10M_D_ff896_gr64_dc192",
        "desc": "3:1 GDN-2 + G1-NoPE-MLA + Canon-AC + mHC2 (d=512, L=4, ff=896, dc=192, gr=64)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 896,
        "mla_dc": 192, "mla_dr": 32, "gate_rank": 64,
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0], "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1,
        "canon_mode": CANON_AC, "mhc_streams": 2,
    },
    {
        "id": "Arch7d_NarrowDeep_d384_L8_ff768",
        "desc": "Narrow-deep counterexample violating COMP-1 P4 width gate (d=384 < 512, L=8)",
        "width": 384, "heads": 6, "kvheads": 2, "layers": 8, "ff": 768,
        "mla_dc": 128, "mla_dr": 32, "gate_rank": 48,
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA] * 2,
        "ropes": [0] * 8, "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1,
        "canon_mode": CANON_AC, "mhc_streams": 2,
    },
    {
        "id": "Arch2_Hybrid_10M_ff960",
        "desc": "Parameter-matched Arch-2 3:1 DeltaNet+GQA control (d=512, L=4, ff=960)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 960,
        "mla_dc": 192, "mla_dr": 32, "gate_rank": 64,
        "mixers": [MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
        "ropes": [1, 1, 1, 1], "gate_mode": GATE_NONE, "qk_norm": 0,
        "canon_mode": CANON_OFF, "mhc_streams": 1,
    },
    {
        "id": "GQA_Full_10M_ff1152",
        "desc": "Parameter-matched GQA_Full RoPE control (d=512, L=4, ff=1152)",
        "width": 512, "heads": 8, "kvheads": 2, "layers": 4, "ff": 1152,
        "mla_dc": 192, "mla_dr": 32, "gate_rank": 64,
        "mixers": [MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
        "ropes": [1, 1, 1, 1], "gate_mode": GATE_NONE, "qk_norm": 0,
        "canon_mode": CANON_OFF, "mhc_streams": 1,
    },
]


def run_gate0() -> Dict[str, Any]:
    subprocess.run(["make", "-C", str(STUDY_DIR), "all"], check=True)
    res = subprocess.run(
        [str(STUDY_DIR / "build" / "test_mixer_ext")],
        capture_output=True,
        text=True,
        check=True,
    )
    out_p = RESULTS_DIR / "gate0_abi6_verify_output.txt"
    out_p.write_text(res.stdout, encoding="utf-8", newline="\n")
    print("[Stage 0] Gate 0 C verification PASSED:")
    for line in res.stdout.strip().splitlines()[-4:]:
        print("  ", line)
    return {
        "returncode": res.returncode,
        "lines": len(res.stdout.splitlines()),
        "summary_tail": res.stdout.strip().splitlines()[-3:],
    }


def run_sizing_audit() -> List[Dict[str, Any]]:
    rows = []
    for cfg in SIZING_CANDIDATES:
        sp256 = make_spec(
            vocab=256, width=cfg["width"], heads=cfg["heads"], kvheads=cfg["kvheads"],
            layers=cfg["layers"], ff=cfg["ff"], batch=8, time_len=256,
            layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
            mla_dc=cfg["mla_dc"], mla_dr=cfg["mla_dr"], gate_rank=cfg["gate_rank"],
            gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
            canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
            online_softmax=1, skip_init=1, threads=2,
        )
        sp512 = make_spec(
            vocab=256, width=cfg["width"], heads=cfg["heads"], kvheads=cfg["kvheads"],
            layers=cfg["layers"], ff=cfg["ff"], batch=4, time_len=512,
            layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
            mla_dc=cfg["mla_dc"], mla_dr=cfg["mla_dr"], gate_rank=cfg["gate_rank"],
            gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
            canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
            online_softmax=1, skip_init=1, threads=2,
        )
        with Abi6Model(sp256, make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)) as m:
            ms256 = m.mem_stats()
            q8_b = q8_weight_bytes(m)
            c1_32 = comp1_weight_bytes(m, rank=16, scale_bits=32)
            c1_16 = comp1_weight_bytes(m, rank=16, scale_bits=16)
        with Abi6Model(sp512, make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)) as m512:
            ms512 = m512.mem_stats()

        kv4k = (ms256["infer_kv_bytes_per_tok"] * 4096) / 1048576.0
        rec = ms256["infer_rec_state_bytes"] / 1048576.0
        st4k = kv4k + rec
        row = {
            "id": cfg["id"],
            "desc": cfg["desc"],
            "n_params": ms256["n_params"],
            "attn_params": ms256["attn_params"],
            "mlp_params": ms256["mlp_params"],
            "nonlin_params": ms256["nonlin_params"],
            "weight_fp32_mib": round(ms256["weight_bytes"] / 1048576.0, 2),
            "persistent_q8muon_mib": round(ms256["persistent_bytes"] / 1048576.0, 2),
            "q8muon_bytes_per_param": round(ms256["persistent_bytes"] / ms256["n_params"], 3),
            "arena_b8_t256_mib": round(ms256["total_bytes"] / 1048576.0, 2),
            "arena_b4_t512_mib": round(ms512["total_bytes"] / 1048576.0, 2),
            "kv_4k_mib": round(kv4k, 3),
            "rec_state_mib": round(rec, 3),
            "state_4k_mib": round(st4k, 3),
            "p4_width_gate_pass": c1_32["eligible_tensors"] > 0,
            "comp1_eligible_tensors": c1_32["eligible_tensors"],
            "comp1_mlp_bpw_fp32scale": c1_32["mlp_comp1_bpw"],
            "comp1_mlp_bpw_fp16scale": c1_16["mlp_comp1_bpw"],
            "q8_weights_mib": round(q8_b / 1048576.0, 2),
            "comp1_weights_fp32scale_mib": round(c1_32["total_bytes"] / 1048576.0, 2),
            "comp1_weights_fp16scale_mib": round(c1_16["total_bytes"] / 1048576.0, 2),
            "total_4k_fp32_mib": round(ms256["weight_bytes"] / 1048576.0 + st4k, 2),
            "total_4k_q8_mib": round(q8_b / 1048576.0 + st4k, 2),
            "total_4k_comp1_fp32scale_mib": round(c1_32["total_bytes"] / 1048576.0 + st4k, 2),
            "total_4k_comp1_fp16scale_mib": round(c1_16["total_bytes"] / 1048576.0 + st4k, 2),
        }
        rows.append(row)
        print(
            f"[Stage 0 Sizing] {row['id']:32s} | P={row['n_params']/1e6:6.3f}M | "
            f"pers={row['persistent_q8muon_mib']:5.1f}M | st4k={row['state_4k_mib']:5.2f}M | "
            f"P4={int(row['p4_width_gate_pass'])} ({row['comp1_eligible_tensors']:2d}) | "
            f"c1_4k={row['total_4k_comp1_fp32scale_mib']:5.2f}M (fp16s {row['total_4k_comp1_fp16scale_mib']:5.2f}M)",
            flush=True,
        )
    return rows


def run_optimizer_memory_audit(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    fp32_pers = None
    for opt_k in (
        OPT_FP32,
        OPT_ADAFACTOR_M2,
        OPT_MUON_FP32,
        OPT_Q8_MOMENTS,
        OPT_ADAFACTOR_BF16_GRAD,
        OPT_Q8_BF16_GRAD,
        OPT_Q8_MUON_BF16_GRAD,
    ):
        sp = make_spec(
            vocab=256, width=cfg["width"], heads=cfg["heads"], kvheads=cfg["kvheads"],
            layers=cfg["layers"], ff=cfg["ff"], batch=8, time_len=256,
            layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
            mla_dc=cfg["mla_dc"], mla_dr=cfg["mla_dr"], gate_rank=cfg["gate_rank"],
            gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
            canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
            online_softmax=1, skip_init=1, threads=2,
        )
        with Abi6Model(sp, make_policy(opt_kind=opt_k)) as m:
            ms = m.mem_stats()
            opt_ser_bytes = int(m.lib.bcir_ext_opt_state_bytes(m.ptr))
        pers_mib = ms["persistent_bytes"] / 1048576.0
        if fp32_pers is None:
            fp32_pers = pers_mib
        rows.append({
            "optimizer": OPT_NAMES[opt_k],
            "opt_kind": opt_k,
            "n_params": ms["n_params"],
            "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
            "weights_mib": round(ms["weight_bytes"] / 1048576.0, 2),
            "opt_plus_grad_mib": round(ms["opt_only_bytes"] / 1048576.0, 2),
            "persistent_mib": round(pers_mib, 2),
            "saved_vs_fp32_mib": round(fp32_pers - pers_mib, 2),
            "saved_pct": round(100.0 * (fp32_pers - pers_mib) / fp32_pers, 2),
            "opt_checkpoint_mib": round(opt_ser_bytes / 1048576.0, 2),
            "arena_b8_t256_mib": round(ms["total_bytes"] / 1048576.0, 2),
        })
    return rows


def run_micro_ablation() -> Dict[str, Any]:
    """3-way 32-step micro-ablation at B=8, T=128 (32,768 tok) under Opt_Q8_Muon_BF16Grad."""
    tr = load_wikitext_bytes("train")
    va = load_wikitext_bytes("validation")
    steps = 32
    B, T = 8, 128
    tk, tg = sample_lm_steps(tr, 0, steps, B, T, base_seed=91000)
    val_tk_128, val_tg_128 = make_fixed_eval_batches(va, 8, B, 128)
    val_tk_256, val_tg_256 = make_fixed_eval_batches(va, 4, 8, 256)
    lrs = cosine_lr_schedule(steps, 3.0e-3, 1.0e-3, 8)
    opt_cfg = AdamWParams(3.0e-3, 0.95, 0.95, 1e-8, 0.01, 3.0)

    out = []
    for cfg in SIZING_CANDIDATES[:3]:
        t0 = time.perf_counter()
        sp = make_spec(
            vocab=256, width=cfg["width"], heads=cfg["heads"], kvheads=cfg["kvheads"],
            layers=cfg["layers"], ff=cfg["ff"], batch=B, time_len=T,
            layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
            mla_dc=cfg["mla_dc"], mla_dr=cfg["mla_dr"], gate_rank=cfg["gate_rank"],
            gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
            canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
            online_softmax=1, skip_init=0, seed=42, threads=2,
        )
        with Abi6Model(sp, make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)) as m:
            ms = m.mem_stats()
            rc, evs = m.train_loop(tk, tg, opt_cfg, lrs=lrs)
            assert rc == 0
            v128 = m.evaluate(val_tk_128, val_tg_128)
            tel = m.telemetry()
            w_end = m.copy_weights()
        # Cross-evaluate on T=256 zero-shot
        sp256 = make_spec(
            vocab=256, width=cfg["width"], heads=cfg["heads"], kvheads=cfg["kvheads"],
            layers=cfg["layers"], ff=cfg["ff"], batch=8, time_len=256,
            layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
            mla_dc=cfg["mla_dc"], mla_dr=cfg["mla_dr"], gate_rank=cfg["gate_rank"],
            gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
            canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
            online_softmax=1, skip_init=1, seed=42, threads=2,
        )
        with Abi6Model(sp256, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m256:
            m256.set_weights(w_end)
            v256 = m256.evaluate(val_tk_256, val_tg_256)
        wall = time.perf_counter() - t0
        med_ms = float(np.median([e["step_ms"] for e in evs]))
        rec = {
            "id": cfg["id"],
            "spec": spec_to_dict(sp),
            "n_params": ms["n_params"],
            "steps": steps,
            "tokens": steps * B * T,
            "final_train_loss": round(float(evs[-1]["loss"]), 6),
            "val_ce_t128": round(v128["ce"], 6),
            "val_ppl_t128": round(v128["ppl"], 4),
            "val_ce_t256_zeroshot": round(v256["ce"], 6),
            "val_ppl_t256_zeroshot": round(v256["ppl"], 4),
            "median_step_ms": round(med_ms, 2),
            "tok_per_s": round((B * T) / (med_ms / 1000.0), 1),
            "wall_s": round(wall, 2),
            "max_abs_act": round(tel["max_abs_act"], 3),
            "first_tok_attn_mean": round(tel["first_tok_attn_mean"], 4),
            "nonfinite": int(tel["nonfinite_events"]),
            "grad_clip_hits": int(tel["grad_clip_hits"]),
        }
        out.append(rec)
        print(
            f"[Stage 0 Ablation] {rec['id']:32s} | P={rec['n_params']/1e6:6.3f}M | "
            f"val128={rec['val_ce_t128']:.4f} (PPL {rec['val_ppl_t128']:5.2f}) | "
            f"val256_zs={rec['val_ce_t256_zeroshot']:.4f} (PPL {rec['val_ppl_t256_zeroshot']:5.2f}) | "
            f"{rec['median_step_ms']:.0f}ms ({rec['tok_per_s']:.0f} tok/s)",
            flush=True,
        )

    winner = min(out, key=lambda r: r["val_ce_t256_zeroshot"])
    return {
        "steps": steps,
        "batch": B,
        "seq_len": T,
        "tokens": steps * B * T,
        "optimizer": "Opt_Q8_Muon_BF16Grad",
        "candidates": out,
        "selected_id": winner["id"],
        "selection_criterion": "lowest zero-shot T=256 validation CE at matched 32,768 tokens with P4 width gate satisfied",
    }


def main() -> None:
    g0 = run_gate0()
    sizing = run_sizing_audit()
    opt_audit = run_optimizer_memory_audit(SIZING_CANDIDATES[0])
    ablation = run_micro_ablation()
    payload = {
        "stage": "Stage 0 — Gate 0 Verification, 10M Architecture Sizing & Micro-Ablation",
        "host": host_info(),
        "rss": process_rss_mib(),
        "gate0": g0,
        "sizing_candidates": sizing,
        "optimizer_memory_audit_10m": opt_audit,
        "micro_ablation": ablation,
    }
    out_p = RESULTS_DIR / "stage0_sizing_and_ablation.json"
    save_json(out_p, payload)
    print(f"[Stage 0] Selected 10M winner: {ablation['selected_id']} -> saved {out_p}")


if __name__ == "__main__":
    main()
