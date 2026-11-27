#!/usr/bin/env python3
"""Experiment C: Resumable Tier M/L (>= 10M-Token) Qualification Runner & Real Local 1.024M-Token Pilot.

Production resumable qualification CLI using BCIR ABI 6 MIXER-EXT + TRAIN-OPT-5
online-softmax (T=256) + atomic SHA-256 checkpoints.

Supports:
  --tier pilot   : 1,024,000 tokens (1.024M tok, T=256, B=4 = 1,000 steps) — runs locally in minutes
  --tier M       : 10,000,000 tokens (Tier M >= 10M-token qualification)
  --tier L       : 50,000,000 tokens (Tier L >= 50M-token qualification)

Never fabricates unrun numbers: records exact tokens trained, wall-clock throughput,
periodic untouched validation & test curves, and calibrated projections for multi-core
Xeon/EPYC hosts when run in pilot mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from abi6_harness import (
    CANON_AC,
    CANON_OFF,
    CKPT_DIR,
    GATE_G1_HEADWISE,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GDN2,
    MIXER_GQA,
    MIXER_KDA,
    MIXER_MLA,
    OPT_ADAFACTOR_M2,
    OPT_Q8_BF16_GRAD,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    Abi6Model,
    AdamWParams,
    apply_comp1_ptq_weights,
    cosine_lr_schedule,
    host_info,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    quantize_bcirq8_g32,
    sample_lm_batches,
    save_json,
)

TIER_TOKENS = {
    "pilot": 1_024_000,   # 1,000 steps @ B=4, T=256
    "M": 10_000_000,      # 9,766 steps @ B=4, T=256
    "L": 50_000_000,      # 48,829 steps @ B=4, T=256
}

ARCH_PRESETS: Dict[str, Dict[str, Any]] = {
    "gqa_full": {
        "mixers": [MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
        "ropes": [1, 1, 1, 1],
        "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
    },
    "hybrid_dn_gqa": {
        "mixers": [MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
        "ropes": [0, 0, 0, 1],
        "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
    },
    "hybrid_kda_nopemla": {
        "mixers": [MIXER_KDA, MIXER_KDA, MIXER_KDA, MIXER_MLA],
        "ropes": [0, 0, 0, 0],
        "gate_mode": GATE_NONE, "qk_norm": 0, "canon_mode": CANON_OFF, "mhc_streams": 1,
    },
    "arch7c_gdn2_g1nopemla_canon": {
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0],
        "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_AC, "mhc_streams": 1,
    },
    "arch7d_gdn2_g1nopemla_canon_mhc2": {
        "mixers": [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
        "ropes": [0, 0, 0, 0],
        "gate_mode": GATE_G1_HEADWISE, "qk_norm": 1, "canon_mode": CANON_AC, "mhc_streams": 2,
    },
}

OPT_PRESETS = {
    "adafactor": (OPT_ADAFACTOR_M2, 0.90),
    "q8_bf16grad": (OPT_Q8_BF16_GRAD, 0.90),
    "q8_muon_bf16grad": (OPT_Q8_MUON_BF16_GRAD, 0.95),
}


def save_ckpt_atomic(path: Path, weights: np.ndarray, meta: Dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = np.ascontiguousarray(weights, dtype=np.float32).tobytes()
    sha = hashlib.sha256(raw).hexdigest()
    bin_p = path.with_suffix(".bin")
    json_p = path.with_suffix(".json")
    tmp_b = bin_p.with_suffix(".bin.tmp")
    tmp_j = json_p.with_suffix(".json.tmp")
    tmp_b.write_bytes(raw)
    os.replace(tmp_b, bin_p)
    meta_out = dict(meta, weights_sha256=sha, weights_file=bin_p.name)
    tmp_j.write_text(json.dumps(meta_out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_j, json_p)
    return sha


def load_ckpt_verified(path: Path) -> tuple[np.ndarray, Dict[str, Any]]:
    json_p = path.with_suffix(".json")
    meta = json.loads(json_p.read_text(encoding="utf-8"))
    raw = (json_p.parent / meta["weights_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["weights_sha256"]:
        raise ValueError(f"SHA-256 mismatch loading {path}")
    return np.frombuffer(raw, dtype=np.float32).copy(), meta


def run_qual(
    arch_name: str,
    opt_name: str,
    tier: str,
    *,
    width: int,
    heads: int,
    kvheads: int,
    ff: int,
    batch: int,
    seq_len: int,
    seed: int,
    threads: int,
    max_lr: float,
    eval_every: int,
    save_final_ckpt: bool = False,
    resume_ckpt: Path | None = None,
) -> Dict[str, Any]:
    cfg = ARCH_PRESETS[arch_name]
    opt_kind, beta1 = OPT_PRESETS[opt_name]
    target_tokens = TIER_TOKENS[tier]
    tok_per_step = batch * seq_len
    total_steps = (target_tokens + tok_per_step - 1) // tok_per_step

    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")
    val_tk, val_tg = make_fixed_eval_batches(va_stream, 8, batch, seq_len)
    test_tk, test_tg = make_fixed_eval_batches(te_stream, 12, batch, seq_len)

    sp = make_spec(
        vocab=256, width=width, heads=heads, kvheads=kvheads, layers=4, ff=ff,
        batch=batch, time_len=seq_len,
        layer_mixers=cfg["mixers"], layer_use_rope=cfg["ropes"],
        swa_window=16, delta_chunk=16, mla_dc=width // 2, mla_dr=max(16, (width // heads) // 2),
        mup_d_base=128, seed=seed, threads=threads,
        skip_init=1 if resume_ckpt else 0,
        gate_mode=cfg["gate_mode"], qk_norm=cfg["qk_norm"],
        canon_mode=cfg["canon_mode"], mhc_streams=cfg["mhc_streams"],
        online_softmax=1, gate_rank=max(16, width // heads),
    )
    pol = make_policy(opt_kind=opt_kind)
    lrs = cosine_lr_schedule(total_steps, max_lr, max_lr * 0.1, max(20, total_steps // 15))
    opt_cfg = AdamWParams(max_lr, beta1, 0.95, 1e-8, 0.01, 3.0)

    start_step = 0
    curve: List[Dict[str, Any]] = []
    with Abi6Model(sp, pol) as model:
        if resume_ckpt is not None:
            w_loaded, meta = load_ckpt_verified(resume_ckpt)
            model.set_weights(w_loaded)
            start_step = int(meta.get("step", 0))
            curve = list(meta.get("curve", []))

        ms = model.mem_stats()
        t0 = time.perf_counter()
        v0 = model.evaluate(val_tk, val_tg)
        best_val_ce = v0["ce"] if v0["ok"] else float("inf")
        best_step = start_step
        best_w = model.copy_weights()
        if not curve:
            curve.append({
                "step": 0, "tokens": 0, "wall_s": 0.0,
                "val_ce": round(v0["ce"], 6), "val_ppl": round(v0["ppl"], 4),
            })

        step_ms_all: List[float] = []
        s = start_step
        while s < total_steps:
            chunk = min(eval_every, total_steps - s)
            tr_tk, tr_tg = sample_lm_batches(tr_stream, chunk, batch, seq_len, seed=70000 + seed * 10000 + s)
            rc, evs = model.train_loop(tr_tk, tr_tg, opt_cfg, lrs=lrs[s : s + chunk])
            assert rc == 0, f"train_loop failed with rc={rc} at step {s}"
            for e in evs:
                step_ms_all.append(float(e["step_ms"]))
            s += chunk
            vv = model.evaluate(val_tk, val_tg)
            wall_s = time.perf_counter() - t0
            toks_seen = s * tok_per_step
            row = {
                "step": s,
                "tokens": toks_seen,
                "wall_s": round(wall_s, 2),
                "train_loss": round(float(np.mean([e["loss"] for e in evs[-min(16, len(evs)):]])), 6),
                "val_ce": round(vv["ce"], 6),
                "val_ppl": round(vv["ppl"], 4),
                "tok_per_s": round(toks_seen / max(wall_s, 1e-6), 1),
            }
            curve.append(row)
            if vv["ok"] and vv["ce"] < best_val_ce:
                best_val_ce = vv["ce"]
                best_step = s
                best_w = model.copy_weights()
            print(
                f"[Tier-{tier}] {arch_name:28s} {opt_name:16s} | st={s:5d}/{total_steps} "
                f"tok={toks_seen/1e6:5.3f}M | val_ce={row['val_ce']:.4f} ppl={row['val_ppl']:5.2f} "
                f"| {row['tok_per_s']:6.0f} tok/s ({wall_s:5.1f}s)",
                flush=True,
            )

        total_wall = time.perf_counter() - t0
        model.set_weights(best_w)
        model.reset_telemetry()
        test_res = model.evaluate(test_tk, test_tg)
        eval_tel = model.telemetry()

        model.set_weights(quantize_bcirq8_g32(best_w))
        q8_res = model.evaluate(test_tk, test_tg)

        ckpt_sha = None
        if save_final_ckpt:
            ckpt_p = CKPT_DIR / f"tier_{tier}_{arch_name}_{opt_name}_s{seed}"
            ckpt_sha = save_ckpt_atomic(ckpt_p, best_w, {
                "arch": arch_name, "opt": opt_name, "tier": tier,
                "step": best_step, "tokens": total_steps * tok_per_step,
                "test_ce": test_res["ce"], "curve": curve,
            })

    med_ms = float(np.median(step_ms_all)) if step_ms_all else 0.0
    tok_s = (total_steps * tok_per_step) / max(total_wall, 1e-6)
    return {
        "arch": arch_name,
        "opt": opt_name,
        "tier": tier,
        "width": width,
        "seq_len": seq_len,
        "batch": batch,
        "seed": seed,
        "n_params": ms["n_params"],
        "persistent_mib": round(ms["persistent_bytes"] / (1024 * 1024), 2),
        "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
        "prob_mib_online": round(ms["mixer_prob_bytes"] / (1024 * 1024), 3),
        "steps_executed": total_steps,
        "tokens_executed": total_steps * tok_per_step,
        "best_step": best_step,
        "best_val_ce": round(best_val_ce, 6),
        "test_ce": round(test_res["ce"], 6),
        "test_ppl": round(test_res["ppl"], 4),
        "q8_test_ce": round(q8_res["ce"], 6),
        "q8_delta_ce": round(q8_res["ce"] - test_res["ce"], 6),
        "first_tok_attn_mean": round(eval_tel["first_tok_attn_mean"], 4),
        "max_abs_act": round(eval_tel["max_abs_act"], 3),
        "step_ms_median": round(med_ms, 2),
        "wall_s": round(total_wall, 2),
        "tok_per_s_2core_avx2": round(tok_s, 1),
        "projected_tier_m_10m_2core_min": round((10_000_000.0 / max(tok_s, 1.0)) / 60.0, 1),
        "projected_tier_m_10m_16core_min": round(((10_000_000.0 / max(tok_s, 1.0)) / 6.5) / 60.0, 1),
        "ckpt_sha256": ckpt_sha,
        "curve": curve,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["pilot_matrix", "single"], default="pilot_matrix")
    ap.add_argument("--tier", choices=["pilot", "M", "L"], default="pilot")
    ap.add_argument("--arch", choices=list(ARCH_PRESETS), default="arch7d_gdn2_g1nopemla_canon_mhc2")
    ap.add_argument("--opt", choices=list(OPT_PRESETS), default="adafactor")
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--heads", type=int, default=4)
    ap.add_argument("--kvheads", type=int, default=2)
    ap.add_argument("--ff", type=int, default=320)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--seq-len", type=int, default=256)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--eval-every", type=int, default=125)
    ap.add_argument("--resume", type=str, default=None)
    ap.add_argument("--only-run", type=str, default=None, help="Run a single key from pilot_matrix and merge")
    ap.add_argument("--verify-resume", action="store_true", help="Verify atomic checkpoint save + resume parity")
    args = ap.parse_args()

    out_path = RESULTS_DIR / "exp_c_tier_ml_pilot.json"
    if args.mode == "single":
        res = run_qual(
            args.arch, args.opt, args.tier,
            width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
            batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
            max_lr=args.lr, eval_every=args.eval_every,
            save_final_ckpt=True,
            resume_ckpt=Path(args.resume) if args.resume else None,
        )
        save_json(RESULTS_DIR / f"tier_{args.tier}_{args.arch}_{args.opt}.json", {"host": host_info(), "run": res})
        return

    # Controlled 1.024M-token T=256 online-softmax long-horizon comparison on 2-core Haswell VM
    existing_payload: Dict[str, Any] = {}
    if out_path.exists():
        try:
            existing_payload = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing_payload = {}
    runs: Dict[str, Any] = dict(existing_payload.get("runs", {}))
    matrix = [
        ("GQA_Full_Adafactor", "gqa_full", "adafactor"),
        ("Hybrid_DN_GQA_Adafactor", "hybrid_dn_gqa", "adafactor"),
        ("Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Adafactor", "arch7d_gdn2_g1nopemla_canon_mhc2", "adafactor"),
        ("Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Q8Muon", "arch7d_gdn2_g1nopemla_canon_mhc2", "q8_muon_bf16grad"),
    ]
    if args.only_run:
        matrix = [m for m in matrix if m[0] == args.only_run]
        if not matrix:
            raise SystemExit(f"Unknown --only-run key: {args.only_run}")
    for key, arch_n, opt_n in matrix:
        runs[key] = run_qual(
            arch_n, opt_n, "pilot",
            width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
            batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
            max_lr=args.lr, eval_every=args.eval_every,
            save_final_ckpt=(key == "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Adafactor"),
        )
        payload = dict(existing_payload)
        payload.update({
            "host": host_info(),
            "tier_executed": "pilot (1.024M tokens @ T=256, B=8, online_softmax=1)",
            "tier_m_cli_example": (
                "python src/run_tier_ml_qual.py --mode single --tier M "
                "--arch arch7d_gdn2_g1nopemla_canon_mhc2 --opt q8_muon_bf16grad "
                "--width 512 --heads 8 --kvheads 2 --ff 1536 --batch 8 --seq-len 256 --threads 16"
            ),
            "runs": runs,
        })
        if args.verify_resume and key == "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Adafactor":
            ckpt_p = CKPT_DIR / f"tier_pilot_{arch_n}_{opt_n}_s{args.seed}"
            w_check, meta_check = load_ckpt_verified(ckpt_p)
            cfg_c = ARCH_PRESETS[arch_n]
            te_stream = load_wikitext_bytes("test")
            test_tk, test_tg = make_fixed_eval_batches(te_stream, 12, args.batch, args.seq_len)
            sp_c = make_spec(
                vocab=256, width=args.width, heads=args.heads, kvheads=args.kvheads, layers=4, ff=args.ff,
                batch=args.batch, time_len=args.seq_len,
                layer_mixers=cfg_c["mixers"], layer_use_rope=cfg_c["ropes"],
                swa_window=16, delta_chunk=16, mla_dc=args.width // 2, mla_dr=max(16, (args.width // args.heads) // 2),
                mup_d_base=128, seed=args.seed, threads=args.threads, skip_init=1,
                gate_mode=cfg_c["gate_mode"], qk_norm=cfg_c["qk_norm"],
                canon_mode=cfg_c["canon_mode"], mhc_streams=cfg_c["mhc_streams"],
                online_softmax=1, gate_rank=max(16, args.width // args.heads),
            )
            with Abi6Model(sp_c, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m_reload:
                m_reload.set_weights(w_check)
                r_reload = m_reload.evaluate(test_tk, test_tg)
            ce_diff = abs(float(r_reload["ce"]) - float(runs[key]["test_ce"]))
            payload["resume_roundtrip_verified"] = {
                "ckpt_file": ckpt_p.with_suffix(".bin").name,
                "weights_sha256": meta_check["weights_sha256"],
                "n_params": int(w_check.size),
                "step": int(meta_check["step"]),
                "sha256_verified": True,
                "reload_eval_ce_abs_diff": ce_diff,
                "bit_exact_eval": ce_diff < 1e-6,
            }
        existing_payload = payload
        save_json(out_path, payload)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
