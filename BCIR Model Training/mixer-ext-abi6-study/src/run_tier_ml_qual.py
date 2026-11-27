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
    sample_lm_steps,
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


def _ckpt_stem_and_json(path: Path) -> tuple[str, Path]:
    base_stem = path.stem if path.suffix in (".json", ".bin") else path.name
    return base_stem, path.parent / f"{base_stem}.json"


def save_ckpt_atomic(
    path: Path,
    weights: np.ndarray,
    meta: Dict[str, Any],
    *,
    opt_state: bytes | None = None,
    best_weights: np.ndarray | None = None,
    _abort_before_commit: bool = False,
) -> str:
    """Crash-atomic multi-file checkpoint save with generation-tagged payloads.

    Writes new generation-tagged `.bin`/`.opt.bin`/`.best.bin` payloads first,
    switches the `.json` pointer atomically via `os.replace`, and only then
    removes the previous generation's payload files so an interruption mid-save
    never corrupts or overwrites the last valid checkpoint generation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    base_stem, json_p = _ckpt_stem_and_json(path)
    prev_meta: Dict[str, Any] | None = None
    prev_gen = 0
    if json_p.exists():
        try:
            prev_meta = json.loads(json_p.read_text(encoding="utf-8"))
            prev_gen = int(prev_meta.get("generation", 0))
        except Exception:
            prev_meta = None
            prev_gen = 0
    gen = prev_gen + 1

    raw = np.ascontiguousarray(weights, dtype=np.float32).tobytes()
    sha = hashlib.sha256(raw).hexdigest()
    gen_prefix = f"{base_stem}.g{gen}_{sha[:8]}"
    bin_p = path.parent / f"{gen_prefix}.bin"
    tmp_b = path.parent / f".{gen_prefix}.bin.tmp"
    tmp_b.write_bytes(raw)
    os.replace(tmp_b, bin_p)

    meta_out = dict(meta, generation=gen, weights_sha256=sha, weights_file=bin_p.name)
    new_files = {bin_p.name}
    if _abort_before_commit:
        raise RuntimeError("simulated mid-save crash before metadata commit")

    if opt_state is not None:
        opt_p = path.parent / f"{gen_prefix}.opt.bin"
        opt_sha = hashlib.sha256(opt_state).hexdigest()
        tmp_o = path.parent / f".{gen_prefix}.opt.bin.tmp"
        tmp_o.write_bytes(opt_state)
        os.replace(tmp_o, opt_p)
        meta_out["opt_state_file"] = opt_p.name
        meta_out["opt_state_sha256"] = opt_sha
        meta_out["opt_state_bytes"] = len(opt_state)
        new_files.add(opt_p.name)
    if best_weights is not None:
        best_raw = np.ascontiguousarray(best_weights, dtype=np.float32).tobytes()
        best_p = path.parent / f"{gen_prefix}.best.bin"
        best_sha = hashlib.sha256(best_raw).hexdigest()
        tmp_bw = path.parent / f".{gen_prefix}.best.bin.tmp"
        tmp_bw.write_bytes(best_raw)
        os.replace(tmp_bw, best_p)
        meta_out["best_weights_file"] = best_p.name
        meta_out["best_weights_sha256"] = best_sha
        new_files.add(best_p.name)

    tmp_j = path.parent / f".{base_stem}.g{gen}.json.tmp"
    tmp_j.write_text(
        json.dumps(meta_out, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp_j, json_p)

    if prev_meta is not None:
        for key in ("weights_file", "opt_state_file", "best_weights_file"):
            old_name = prev_meta.get(key)
            if isinstance(old_name, str) and old_name not in new_files:
                (path.parent / old_name).unlink(missing_ok=True)
    return sha


def load_ckpt_verified(
    path: Path,
) -> tuple[np.ndarray, Dict[str, Any], bytes | None, np.ndarray | None]:
    _, json_p = _ckpt_stem_and_json(path)
    meta = json.loads(json_p.read_text(encoding="utf-8"))
    raw = (json_p.parent / meta["weights_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["weights_sha256"]:
        raise ValueError(f"SHA-256 mismatch loading weights from {path}")
    opt_state: bytes | None = None
    if "opt_state_file" in meta:
        opt_raw = (json_p.parent / meta["opt_state_file"]).read_bytes()
        if hashlib.sha256(opt_raw).hexdigest() != meta["opt_state_sha256"]:
            raise ValueError(f"SHA-256 mismatch loading optimizer state from {path}")
        opt_state = opt_raw
    best_weights: np.ndarray | None = None
    if "best_weights_file" in meta:
        best_raw = (json_p.parent / meta["best_weights_file"]).read_bytes()
        if hashlib.sha256(best_raw).hexdigest() != meta["best_weights_sha256"]:
            raise ValueError(f"SHA-256 mismatch loading best weights from {path}")
        best_weights = np.frombuffer(best_raw, dtype=np.float32).copy()
    return np.frombuffer(raw, dtype=np.float32).copy(), meta, opt_state, best_weights


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
    ckpt_every: int = 0,
    save_final_ckpt: bool = False,
    recovery_ckpt: Path | None = None,
    resume_ckpt: Path | None = None,
    target_tokens_override: int | None = None,
    stop_after_steps: int | None = None,
) -> Dict[str, Any]:
    if eval_every <= 0:
        raise ValueError(f"eval_every must be > 0, got {eval_every}")
    if ckpt_every < 0:
        raise ValueError(f"ckpt_every must be >= 0, got {ckpt_every}")
    cfg = ARCH_PRESETS[arch_name]
    opt_kind, beta1 = OPT_PRESETS[opt_name]
    target_tokens = target_tokens_override if target_tokens_override is not None else TIER_TOKENS[tier]
    tok_per_step = batch * seq_len
    total_steps = (target_tokens + tok_per_step - 1) // tok_per_step
    stop_step = min(total_steps, stop_after_steps) if stop_after_steps is not None else total_steps

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
    prev_elapsed_s = 0.0
    curve: List[Dict[str, Any]] = []
    recent_losses: List[float] = []
    recovery_path = (
        recovery_ckpt
        if recovery_ckpt is not None
        else (CKPT_DIR / f"tier_{tier}_{arch_name}_{opt_name}_s{seed}_latest" if ckpt_every > 0 else None)
    )
    with Abi6Model(sp, pol) as model:
        best_val_ce = float("inf")
        best_step = 0
        best_w: np.ndarray | None = None
        if resume_ckpt is not None:
            w_loaded, meta, opt_loaded, best_loaded = load_ckpt_verified(resume_ckpt)
            model.set_weights(w_loaded)
            start_step = int(meta.get("step", 0))
            if start_step < stop_step:
                if opt_loaded is None:
                    raise ValueError(
                        f"Cannot resume training from {resume_ckpt}: checkpoint does not contain "
                        "optimizer state (opt_state_file missing)"
                    )
                model.set_opt_state(opt_loaded)
            elif opt_loaded is not None:
                model.set_opt_state(opt_loaded)
            prev_elapsed_s = float(meta.get("elapsed_wall_s", 0.0))
            curve = list(meta.get("curve", []))
            recent_losses = [float(x) for x in meta.get("recent_losses", [])][-16:]
            if "best_val_ce" in meta:
                best_val_ce = float(meta["best_val_ce"])
                best_step = int(meta.get("best_step", start_step))
                best_w = best_loaded if best_loaded is not None else w_loaded.copy()

        ms = model.mem_stats()
        t0 = time.perf_counter()
        if best_w is None:
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
        last_recovery_sha: str | None = None
        while s < stop_step:
            next_eval = ((s // eval_every) + 1) * eval_every
            chunk = min(next_eval - s, stop_step - s)
            if ckpt_every > 0:
                next_ckpt = ((s // ckpt_every) + 1) * ckpt_every
                chunk = min(chunk, next_ckpt - s)
            tr_tk, tr_tg = sample_lm_steps(
                tr_stream, s, chunk, batch, seq_len, base_seed=70000 + seed * 10000
            )
            rc, evs = model.train_loop(tr_tk, tr_tg, opt_cfg, lrs=lrs[s : s + chunk])
            if rc != 0:
                raise RuntimeError(f"train_loop failed with rc={rc} at step {s}")
            for e in evs:
                step_ms_all.append(float(e["step_ms"]))
                recent_losses.append(float(e["loss"]))
            if len(recent_losses) > 16:
                recent_losses = recent_losses[-16:]
            s += chunk
            run_wall = time.perf_counter() - t0
            cum_wall = prev_elapsed_s + run_wall
            toks_seen = s * tok_per_step
            new_toks = (s - start_step) * tok_per_step
            run_tok_s = new_toks / max(run_wall, 1e-6)
            cum_tok_s = toks_seen / max(cum_wall, 1e-6)

            if s % eval_every == 0 or s == total_steps:
                vv = model.evaluate(val_tk, val_tg)
                row = {
                    "step": s,
                    "tokens": toks_seen,
                    "wall_s": round(cum_wall, 2),
                    "run_wall_s": round(run_wall, 2),
                    "train_loss": round(float(np.mean(recent_losses)), 6),
                    "val_ce": round(vv["ce"], 6),
                    "val_ppl": round(vv["ppl"], 4),
                    "tok_per_s": round(run_tok_s, 1),
                    "cumulative_tok_per_s": round(cum_tok_s, 1),
                }
                curve.append(row)
                if vv["ok"] and vv["ce"] < best_val_ce:
                    best_val_ce = vv["ce"]
                    best_step = s
                    best_w = model.copy_weights()
                print(
                    f"[Tier-{tier}] {arch_name:28s} {opt_name:16s} | st={s:5d}/{total_steps} "
                    f"tok={toks_seen/1e6:5.3f}M | val_ce={row['val_ce']:.4f} ppl={row['val_ppl']:5.2f} "
                    f"| {row['tok_per_s']:6.0f} tok/s ({cum_wall:5.1f}s)",
                    flush=True,
                )

            if recovery_path is not None and ckpt_every > 0 and (s % ckpt_every == 0 or s == stop_step):
                last_recovery_sha = save_ckpt_atomic(
                    recovery_path,
                    model.copy_weights(),
                    {
                        "kind": "recovery_latest",
                        "arch": arch_name,
                        "opt": opt_name,
                        "tier": tier,
                        "step": s,
                        "total_steps": total_steps,
                        "tokens": toks_seen,
                        "best_step": best_step,
                        "best_val_ce": best_val_ce,
                        "elapsed_wall_s": round(cum_wall, 4),
                        "curve": curve,
                        "recent_losses": recent_losses,
                    },
                    opt_state=model.copy_opt_state(),
                    best_weights=best_w,
                )

        run_wall = time.perf_counter() - t0
        cum_wall = prev_elapsed_s + run_wall
        latest_w = model.copy_weights()
        latest_opt = model.copy_opt_state()
        assert best_w is not None
        model.set_weights(best_w)
        model.reset_telemetry()
        test_res = model.evaluate(test_tk, test_tg)
        eval_tel = model.telemetry()

        model.set_weights(quantize_bcirq8_g32(best_w))
        q8_res = model.evaluate(test_tk, test_tg)

        ckpt_sha = None
        if save_final_ckpt:
            ckpt_p = CKPT_DIR / f"tier_{tier}_{arch_name}_{opt_name}_s{seed}"
            ckpt_sha = save_ckpt_atomic(
                ckpt_p,
                best_w,
                {
                    "kind": "best_val_selected",
                    "arch": arch_name,
                    "opt": opt_name,
                    "tier": tier,
                    "step": s,
                    "best_step": best_step,
                    "best_val_ce": best_val_ce,
                    "tokens": s * tok_per_step,
                    "elapsed_wall_s": round(cum_wall, 4),
                    "test_ce": test_res["ce"],
                    "curve": curve,
                },
                opt_state=latest_opt if s == best_step else None,
            )

    steps_this_run = max(0, s - start_step)
    tokens_this_run = steps_this_run * tok_per_step
    tokens_total = s * tok_per_step
    med_ms = float(np.median(step_ms_all)) if step_ms_all else 0.0
    run_tok_s = (tokens_this_run / max(run_wall, 1e-6)) if tokens_this_run > 0 else 0.0
    cum_tok_s = (tokens_total / max(cum_wall, 1e-6)) if (tokens_total > 0 and cum_wall > 0.0) else 0.0
    eff_tok_s = run_tok_s if tokens_this_run > 0 else cum_tok_s
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
        "start_step": start_step,
        "steps_executed_this_run": steps_this_run,
        "tokens_executed_this_run": tokens_this_run,
        "steps_executed": s,
        "tokens_executed": tokens_total,
        "best_step": best_step,
        "best_val_ce": round(best_val_ce, 6),
        "test_ce": round(test_res["ce"], 6),
        "test_ppl": round(test_res["ppl"], 4),
        "q8_test_ce": round(q8_res["ce"], 6),
        "q8_delta_ce": round(q8_res["ce"] - test_res["ce"], 6),
        "first_tok_attn_mean": round(eval_tel["first_tok_attn_mean"], 4),
        "max_abs_act": round(eval_tel["max_abs_act"], 3),
        "step_ms_median": round(med_ms, 2),
        "run_wall_s": round(run_wall, 2),
        "wall_s": round(cum_wall, 2),
        "tok_per_s_this_run": round(run_tok_s, 1),
        "tok_per_s_2core_avx2": round(eff_tok_s, 1),
        "projected_tier_m_10m_2core_min": (
            round((10_000_000.0 / eff_tok_s) / 60.0, 1) if eff_tok_s > 0.0 else None
        ),
        "projected_tier_m_10m_16core_min": (
            round(((10_000_000.0 / eff_tok_s) / 6.5) / 60.0, 1) if eff_tok_s > 0.0 else None
        ),
        "ckpt_sha256": ckpt_sha,
        "recovery_ckpt_sha256": last_recovery_sha,
        "latest_weights_sha256": hashlib.sha256(
            np.ascontiguousarray(latest_w, dtype=np.float32).tobytes()
        ).hexdigest(),
        "curve": curve,
    }


def verify_training_resume_parity(
    *,
    width: int = 160,
    heads: int = 4,
    kvheads: int = 2,
    ff: int = 320,
    batch: int = 8,
    seq_len: int = 256,
    seed: int = 42,
    threads: int = 2,
    max_lr: float = 3e-3,
) -> Dict[str, Any]:
    """Prove 0-bit-drift mid-run interruption & resume across Adafactor and Q8-Muon+BF16Grad,
    including misaligned eval_every=4 / ckpt_every=3 boundaries, crash-atomic generation commits
    preserving the previous valid generation on mid-save interruption, and exact resumed throughput."""
    import tempfile

    arch_name = "arch7d_gdn2_g1nopemla_canon_mhc2"
    tok_per_step = batch * seq_len
    total_steps = 8
    eval_every = 4
    ckpt_every = 3
    split_step = 3
    target_tokens = total_steps * tok_per_step
    report: Dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="bcir_resume_verify_") as td:
        tdir = Path(td)
        for opt_name in ("adafactor", "q8_muon_bf16grad"):
            rec_path = tdir / f"rec_{opt_name}"
            full = run_qual(
                arch_name, opt_name, "pilot",
                width=width, heads=heads, kvheads=kvheads, ff=ff,
                batch=batch, seq_len=seq_len, seed=seed, threads=threads,
                max_lr=max_lr, eval_every=eval_every, ckpt_every=0,
                save_final_ckpt=False, target_tokens_override=target_tokens,
            )
            part1 = run_qual(
                arch_name, opt_name, "pilot",
                width=width, heads=heads, kvheads=kvheads, ff=ff,
                batch=batch, seq_len=seq_len, seed=seed, threads=threads,
                max_lr=max_lr, eval_every=eval_every, ckpt_every=ckpt_every,
                recovery_ckpt=rec_path, save_final_ckpt=False,
                target_tokens_override=target_tokens, stop_after_steps=split_step,
            )
            assert part1["recovery_ckpt_sha256"] is not None
            assert part1["steps_executed_this_run"] == split_step
            # Simulate an interrupted overwrite of generation 2 BEFORE metadata commit:
            # the previous generation (step=3) must remain intact and pass SHA-256 verification.
            w_g1, meta_g1, opt_g1, best_g1 = load_ckpt_verified(rec_path)
            assert int(meta_g1["step"]) == split_step and int(meta_g1["generation"]) == 1
            try:
                save_ckpt_atomic(
                    rec_path,
                    np.zeros_like(w_g1),
                    dict(meta_g1, step=999),
                    opt_state=opt_g1,
                    best_weights=best_g1,
                    _abort_before_commit=True,
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("simulated mid-save crash did not abort")
            w_after_crash, meta_after_crash, opt_after_crash, _ = load_ckpt_verified(rec_path)
            assert int(meta_after_crash["generation"]) == 1
            assert int(meta_after_crash["step"]) == split_step
            assert np.array_equal(w_after_crash, w_g1) and opt_after_crash == opt_g1

            part2 = run_qual(
                arch_name, opt_name, "pilot",
                width=width, heads=heads, kvheads=kvheads, ff=ff,
                batch=batch, seq_len=seq_len, seed=seed, threads=threads,
                max_lr=max_lr, eval_every=eval_every, ckpt_every=ckpt_every,
                recovery_ckpt=rec_path, resume_ckpt=rec_path,
                save_final_ckpt=False, target_tokens_override=target_tokens,
            )
            # Zero-step reload on completed recovery checkpoint must report 0 newly executed tokens
            noop = run_qual(
                arch_name, opt_name, "pilot",
                width=width, heads=heads, kvheads=kvheads, ff=ff,
                batch=batch, seq_len=seq_len, seed=seed, threads=threads,
                max_lr=max_lr, eval_every=eval_every, ckpt_every=0,
                resume_ckpt=rec_path, save_final_ckpt=False,
                target_tokens_override=target_tokens,
            )
            eval_steps_full = [int(r["step"]) for r in full["curve"]]
            eval_steps_resumed = [int(r["step"]) for r in part2["curve"]]
            assert eval_steps_full == [0, 4, 8], f"unexpected full eval steps {eval_steps_full}"
            assert eval_steps_resumed == [0, 4, 8], (
                f"misaligned ckpt_every={ckpt_every} altered eval steps: {eval_steps_resumed}"
            )
            for rf, rr in zip(full["curve"], part2["curve"]):
                assert rf["val_ce"] == rr["val_ce"], f"val_ce curve mismatch at step {rf['step']}"
                assert rf.get("train_loss") == rr.get("train_loss"), (
                    f"train_loss curve mismatch at step {rf['step']}"
                )
            assert part2["steps_executed_this_run"] == total_steps - split_step
            assert part2["tokens_executed_this_run"] == (total_steps - split_step) * tok_per_step
            assert noop["steps_executed_this_run"] == 0
            assert noop["tokens_executed_this_run"] == 0
            assert noop["tok_per_s_this_run"] == 0.0
            assert part2["latest_weights_sha256"] == full["latest_weights_sha256"], (
                f"mid-run resumed weights drifted for {opt_name}"
            )
            assert part2["test_ce"] == full["test_ce"], f"test_ce mismatch for {opt_name}"
            assert part2["best_val_ce"] == full["best_val_ce"], f"best_val_ce mismatch for {opt_name}"
            report[opt_name] = {
                "steps_total": total_steps,
                "eval_every": eval_every,
                "ckpt_every": ckpt_every,
                "interrupted_at_step": split_step,
                "eval_steps_verified": eval_steps_resumed,
                "crash_overwrite_preserves_prev_gen": True,
                "weights_sha256_exact_match": True,
                "latest_weights_sha256": part2["latest_weights_sha256"],
                "test_ce_abs_diff": abs(part2["test_ce"] - full["test_ce"]),
                "part2_tokens_this_run": part2["tokens_executed_this_run"],
                "completed_reload_tokens_this_run": noop["tokens_executed_this_run"],
                "completed_reload_tok_per_s_this_run": noop["tok_per_s_this_run"],
                "bit_exact_training_resume": True,
            }
            print(
                f"[ResumeVerify] {opt_name:16s} | {split_step}+{total_steps - split_step} == {total_steps} "
                f"(eval={eval_steps_resumed}, ckpt_every={ckpt_every}) bits=0 crash_safe=1 "
                f"sha={part2['latest_weights_sha256'][:12]} "
                f"test_ce_diff={report[opt_name]['test_ce_abs_diff']:.1e} "
                f"part2_toks={part2['tokens_executed_this_run']} noop_toks={noop['tokens_executed_this_run']}",
                flush=True,
            )
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["pilot_matrix", "single", "verify_resume_only"], default="pilot_matrix")
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
    ap.add_argument(
        "--ckpt-every",
        type=int,
        default=None,
        help="Write periodic latest-state recovery checkpoint every N steps (default: eval-every in single mode, 0 in pilot_matrix)",
    )
    ap.add_argument("--resume", type=str, default=None)
    ap.add_argument("--only-run", type=str, default=None, help="Run a single key from pilot_matrix and merge")
    ap.add_argument("--verify-resume", action="store_true", help="Verify atomic checkpoint save + resume parity")
    args = ap.parse_args()

    out_path = RESULTS_DIR / "exp_c_tier_ml_pilot.json"
    if args.mode == "verify_resume_only":
        rep = verify_training_resume_parity(
            width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
            batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
            max_lr=args.lr,
        )
        if out_path.exists():
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            payload["training_resume_parity_verified"] = rep
            save_json(out_path, payload)
            print(f"Updated {out_path} with training_resume_parity_verified")
        return

    if args.mode == "single":
        ckpt_ev = args.eval_every if args.ckpt_every is None else args.ckpt_every
        res = run_qual(
            args.arch, args.opt, args.tier,
            width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
            batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
            max_lr=args.lr, eval_every=args.eval_every, ckpt_every=ckpt_ev,
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
    ckpt_ev = 0 if args.ckpt_every is None else args.ckpt_every
    for key, arch_n, opt_n in matrix:
        runs[key] = run_qual(
            arch_n, opt_n, "pilot",
            width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
            batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
            max_lr=args.lr, eval_every=args.eval_every, ckpt_every=ckpt_ev,
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
            payload["training_resume_parity_verified"] = verify_training_resume_parity(
                width=args.width, heads=args.heads, kvheads=args.kvheads, ff=args.ff,
                batch=args.batch, seq_len=args.seq_len, seed=args.seed, threads=args.threads,
                max_lr=args.lr,
            )
        existing_payload = payload
        save_json(out_path, payload)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
