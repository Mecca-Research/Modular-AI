#!/usr/bin/env python3
"""Stages 1–3: Progressive Context Curriculum Training (T=128 -> T=256 -> T=512,
1,114,112 real WikiText-2 bytes) for the Stage-0-selected BCIR-Arch7d-10M model
using Opt_Q8_Muon_BF16Grad (7.22 B/p) with 0-bit-drift cross-stage optimizer handoff,
crash-atomic generation checkpoints, per-step CSV telemetry, and per-stage spectral forensics."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from common_10m import (
    CKPT_DIR,
    OPT_ADAFACTOR_M2,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    Abi6Model,
    AdamWParams,
    analyze_weights_forensics,
    cosine_lr_schedule,
    host_info,
    load_bcirq8_ckpt,
    load_ckpt_verified,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    process_rss_mib,
    quantize_bcirq8_per_tensor,
    sample_lm_steps,
    save_bcirq8_ckpt,
    save_ckpt_atomic,
    save_json,
    spec_to_dict,
    write_steps_csv,
)

# 3-Stage Curriculum Schedule (640 global steps = 1,114,112 real tokens)
STAGE_PLAN = [
    {
        "stage_num": 1,
        "name": "stage1_warmup_t128",
        "title": "Stage 1 — Warmup & Feature Acquisition (T=128, B=8, N=1024)",
        "global_start": 0,
        "steps": 192,
        "batch": 8,
        "seq_len": 128,
        "eval_every": 32,
        "ckpt_every": 64,
        "base_seed": 110000,
    },
    {
        "stage_num": 2,
        "name": "stage2_core_t256",
        "title": "Stage 2 — Core Medium-Context Training (T=256, B=8, N=2048)",
        "global_start": 192,
        "steps": 320,
        "batch": 8,
        "seq_len": 256,
        "eval_every": 32,
        "ckpt_every": 64,
        "base_seed": 220000,
    },
    {
        "stage_num": 3,
        "name": "stage3_longctx_t512",
        "title": "Stage 3 — Long-Context Extension & Cooldown (T=512, B=4, N=2048)",
        "global_start": 512,
        "steps": 128,
        "batch": 4,
        "seq_len": 512,
        "eval_every": 32,
        "ckpt_every": 64,
        "base_seed": 330000,
    },
]
TOTAL_GLOBAL_STEPS = sum(s["steps"] for s in STAGE_PLAN)  # 640
MAX_LR = 3.0e-3
MIN_LR = 2.0e-4
WARMUP_STEPS = 40


def load_selected_arch_cfg(config_id_override: Optional[str] = None) -> Dict[str, Any]:
    s0 = json.loads((RESULTS_DIR / "stage0_sizing_and_ablation.json").read_text(encoding="utf-8"))
    target_id = config_id_override or s0["micro_ablation"]["selected_id"]
    for c in s0["micro_ablation"]["candidates"]:
        if c["id"] == target_id:
            return {"id": target_id, "spec_dict": c["spec"]}
    raise ValueError(f"Config id {target_id!r} not found in Stage 0 results")


def build_spec_from_dict(
    sd: Dict[str, Any], *, batch: int, seq_len: int, skip_init: int, threads: int = 2
):
    return make_spec(
        vocab=sd["vocab"],
        width=sd["width"],
        heads=sd["heads"],
        kvheads=sd["kvheads"],
        layers=sd["layers"],
        ff=sd["ff"],
        batch=batch,
        time_len=seq_len,
        tied=sd["tied"],
        layer_mixers=sd["layer_mixer"],
        layer_use_rope=sd["layer_use_rope"],
        swa_window=sd["swa_window"],
        delta_chunk=sd["delta_chunk"],
        mla_dc=sd["mla_dc"],
        mla_dr=sd["mla_dr"],
        init_policy=sd["init_policy"],
        mup_d_base=sd["mup_d_base"],
        seed=sd["seed"],
        threads=threads,
        skip_init=skip_init,
        gate_mode=sd["gate_mode"],
        qk_norm=sd["qk_norm"],
        canon_mode=sd["canon_mode"],
        mhc_streams=sd["mhc_streams"],
        online_softmax=1,
        gate_rank=sd["gate_rank"],
    )


def eval_multi_horizon(
    sd: Dict[str, Any], weights: np.ndarray, stream: np.ndarray, horizons: List[int]
) -> Dict[str, Any]:
    """Evaluate weights across multiple sequence lengths T on fixed non-overlapping slices."""
    out: Dict[str, Any] = {}
    for T in horizons:
        B = 8 if T <= 256 else (4 if T == 512 else 1)
        nb = 8 if T <= 256 else (8 if T == 512 else 4)
        tk, tg = make_fixed_eval_batches(stream, nb, B, T)
        sp = build_spec_from_dict(sd, batch=B, seq_len=T, skip_init=1, threads=2)
        with Abi6Model(sp, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m:
            m.set_weights(weights)
            m.reset_telemetry()
            ev = m.evaluate(tk, tg)
            tel = m.telemetry()
        out[f"T{T}"] = {
            "ce": round(ev["ce"], 6),
            "ppl": round(ev["ppl"], 4),
            "bpb": round(ev["bpb"], 5),
            "accuracy": round(ev["accuracy"], 5),
            "tokens": ev["tokens"],
            "eval_ms": round(ev["eval_ms"], 1),
            "max_abs_act": round(tel["max_abs_act"], 3),
            "first_tok_attn_mean": round(tel["first_tok_attn_mean"], 4),
        }
    return out


def run_stage(
    stage_cfg: Dict[str, Any],
    arch_cfg: Dict[str, Any],
    global_lrs: np.ndarray,
    *,
    max_steps_this_call: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    snum = stage_cfg["stage_num"]
    sname = stage_cfg["name"]
    sd = arch_cfg["spec_dict"]
    B = stage_cfg["batch"]
    T = stage_cfg["seq_len"]
    steps = stage_cfg["steps"]
    g_start = stage_cfg["global_start"]
    eval_every = stage_cfg["eval_every"]
    ckpt_every = stage_cfg["ckpt_every"]
    tok_per_step = B * T

    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")
    val_tk, val_tg = make_fixed_eval_batches(va_stream, 8, B, T)

    prev_stage_ckpt = CKPT_DIR / f"stage{snum - 1}_end" if snum > 1 else None
    stage_rec_ckpt = CKPT_DIR / f"stage{snum}_recovery"
    stage_end_ckpt = CKPT_DIR / f"stage{snum}_end"

    # Determine starting token/wall accumulation from prior stage if snum > 1
    prev_tokens = 0
    prev_wall_s = 0.0
    for prior in STAGE_PLAN:
        if prior["stage_num"] < snum:
            prev_tokens += prior["steps"] * prior["batch"] * prior["seq_len"]
            p_json = RESULTS_DIR / f"{prior['name']}.json"
            if p_json.exists():
                prev_wall_s += float(json.loads(p_json.read_text(encoding="utf-8"))["stage_wall_s"])

    # Check if we are resuming mid-stage from stage_rec_ckpt or starting fresh from prev_stage_ckpt
    resume_mid = stage_rec_ckpt.with_suffix(".json").exists()
    sp = build_spec_from_dict(
        sd, batch=B, seq_len=T, skip_init=1 if (resume_mid or prev_stage_ckpt is not None) else 0
    )
    pol = make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)
    opt_cfg = AdamWParams(MAX_LR, 0.95, 0.95, 1e-8, 0.01, 3.0)

    local_start = 0
    stage_prev_wall = 0.0
    curve: List[Dict[str, Any]] = []
    step_rows: List[Dict[str, Any]] = []
    recent_losses: List[float] = []
    init_forensics: Optional[Dict[str, Any]] = None
    best_val_ce = float("inf")
    best_global_step = g_start
    best_w: Optional[np.ndarray] = None
    handoff_sha_in: Optional[str] = None

    with Abi6Model(sp, pol) as model:
        if resume_mid:
            w_ld, meta, opt_ld, best_ld = load_ckpt_verified(stage_rec_ckpt)
            assert opt_ld is not None
            model.set_weights(w_ld)
            model.set_opt_state(opt_ld)
            local_start = int(meta["local_step"])
            stage_prev_wall = float(meta["stage_wall_s"])
            curve = list(meta["curve"])
            step_rows = list(meta.get("step_rows", []))
            recent_losses = list(meta.get("recent_losses", []))
            init_forensics = meta.get("init_forensics")
            best_val_ce = float(meta["best_val_ce"])
            best_global_step = int(meta["best_global_step"])
            best_w = best_ld if best_ld is not None else w_ld.copy()
            handoff_sha_in = meta.get("handoff_sha_in")
            print(f"[Stage {snum}] Resumed mid-stage at local_step={local_start}/{steps}", flush=True)
        elif prev_stage_ckpt is not None:
            w_ld, meta, opt_ld, _ = load_ckpt_verified(prev_stage_ckpt)
            assert opt_ld is not None, f"Missing optimizer state in {prev_stage_ckpt}"
            model.set_weights(w_ld)
            model.set_opt_state(opt_ld)
            handoff_sha_in = meta["weights_sha256"]
            print(
                f"[Stage {snum}] Loaded Stage {snum - 1} handoff weights (sha={handoff_sha_in[:12]}) "
                f"+ full Opt_Q8_Muon_BF16Grad state ({len(opt_ld)/1048576:.2f} MiB, opt_step={meta.get('opt_step')})",
                flush=True,
            )

        ms = model.mem_stats()
        if local_start == 0:
            if snum == 1:
                init_forensics = analyze_weights_forensics(model, model.copy_weights())
            v0 = model.evaluate(val_tk, val_tg)
            best_val_ce = v0["ce"]
            best_global_step = g_start
            best_w = model.copy_weights()
            curve.append({
                "stage": snum,
                "local_step": 0,
                "global_step": g_start,
                "tokens_cum": prev_tokens,
                "stage_wall_s": 0.0,
                "cum_wall_s": round(prev_wall_s, 2),
                "lr": round(float(global_lrs[g_start]), 6),
                "val_ce": round(v0["ce"], 6),
                "val_ppl": round(v0["ppl"], 4),
                "val_bpb": round(v0["bpb"], 5),
                "val_acc": round(v0["accuracy"], 5),
            })
            print(
                f"[Stage {snum}] Start gstep={g_start} tok={prev_tokens/1e6:.3f}M | "
                f"val_ce(T={T})={v0['ce']:.4f} PPL={v0['ppl']:.2f} BPB={v0['bpb']:.3f} | "
                f"arena={ms['total_bytes']/1048576:.1f} MiB",
                flush=True,
            )

        stop_local = (
            min(steps, local_start + max_steps_this_call)
            if max_steps_this_call is not None
            else steps
        )
        t0 = time.perf_counter()
        s = local_start
        while s < stop_local:
            next_eval = ((s // eval_every) + 1) * eval_every
            chunk = min(next_eval - s, stop_local - s)
            if ckpt_every > 0:
                next_ckpt = ((s // ckpt_every) + 1) * ckpt_every
                chunk = min(chunk, next_ckpt - s)

            tr_tk, tr_tg = sample_lm_steps(
                tr_stream, s, chunk, B, T, base_seed=stage_cfg["base_seed"]
            )
            chunk_lrs = global_lrs[g_start + s : g_start + s + chunk]
            rc, evs = model.train_loop(tr_tk, tr_tg, opt_cfg, lrs=chunk_lrs)
            if rc != 0:
                raise RuntimeError(f"Stage {snum} train_loop failed rc={rc} at local_step={s}")

            rss_now = process_rss_mib()["rss_mib"]
            for idx_e, ev in enumerate(evs):
                ls = s + idx_e + 1
                gs = g_start + ls
                toks_c = prev_tokens + ls * tok_per_step
                st_ms = float(ev["step_ms"])
                step_rows.append({
                    "stage": snum,
                    "seq_len": T,
                    "batch": B,
                    "local_step": ls,
                    "global_step": gs,
                    "opt_step": int(ev["step"]),
                    "tokens_cum": toks_c,
                    "lr": round(float(chunk_lrs[idx_e]), 7),
                    "loss": round(float(ev["loss"]), 6),
                    "bpb_train": round(float(ev["loss"]) / 0.6931471805599453, 5),
                    "grad_norm": round(float(ev["grad_norm"]), 5),
                    "clipped": int(ev["clipped"]),
                    "step_ms": round(st_ms, 2),
                    "tok_per_s": round(tok_per_step / max(st_ms / 1000.0, 1e-6), 1),
                    "fp64_promotions": int(ev["fp64_promotions"]),
                    "nonfinite_events": int(ev["nonfinite_events"]),
                    "rss_mib": rss_now,
                })
                recent_losses.append(float(ev["loss"]))
            if len(recent_losses) > 16:
                recent_losses = recent_losses[-16:]

            s += chunk
            st_wall = stage_prev_wall + (time.perf_counter() - t0)
            cum_wall = prev_wall_s + st_wall
            toks_c = prev_tokens + s * tok_per_step
            st_tok_s = (s * tok_per_step) / max(st_wall, 1e-6)

            if s % eval_every == 0 or s == steps:
                vv = model.evaluate(val_tk, val_tg)
                tel = model.telemetry()
                row = {
                    "stage": snum,
                    "local_step": s,
                    "global_step": g_start + s,
                    "tokens_cum": toks_c,
                    "stage_wall_s": round(st_wall, 2),
                    "cum_wall_s": round(cum_wall, 2),
                    "lr": round(float(global_lrs[g_start + s - 1]), 6),
                    "train_loss_16": round(float(np.mean(recent_losses)), 6),
                    "val_ce": round(vv["ce"], 6),
                    "val_ppl": round(vv["ppl"], 4),
                    "val_bpb": round(vv["bpb"], 5),
                    "val_acc": round(vv["accuracy"], 5),
                    "tok_per_s": round(st_tok_s, 1),
                    "max_abs_act": round(tel["max_abs_act"], 3),
                    "first_tok_attn_mean": round(tel["first_tok_attn_mean"], 4),
                    "clip_rate_pct": round(100.0 * sum(r["clipped"] for r in step_rows) / max(len(step_rows), 1), 2),
                }
                curve.append(row)
                if vv["ok"] and vv["ce"] < best_val_ce:
                    best_val_ce = vv["ce"]
                    best_global_step = g_start + s
                    best_w = model.copy_weights()
                print(
                    f"[Stage {snum} T={T}] st={s:3d}/{steps} (g={g_start+s:3d}) tok={toks_c/1e6:5.3f}M | "
                    f"tr={row['train_loss_16']:.4f} val_ce={row['val_ce']:.4f} PPL={row['val_ppl']:5.2f} "
                    f"BPB={row['val_bpb']:.3f} acc={row['val_acc']*100:4.1f}% | "
                    f"act={row['max_abs_act']:5.2f} sink={row['first_tok_attn_mean']:.3f} | "
                    f"{row['tok_per_s']:5.0f} tok/s ({st_wall:5.1f}s)",
                    flush=True,
                )

            if ckpt_every > 0 and (s % ckpt_every == 0 or s == stop_local):
                save_ckpt_atomic(
                    stage_rec_ckpt,
                    model.copy_weights(),
                    {
                        "kind": f"stage{snum}_recovery",
                        "arch_id": arch_cfg["id"],
                        "stage": snum,
                        "local_step": s,
                        "global_step": g_start + s,
                        "opt_step": int(evs[-1]["step"]),
                        "tokens_cum": toks_c,
                        "stage_wall_s": round(st_wall, 4),
                        "best_val_ce": best_val_ce,
                        "best_global_step": best_global_step,
                        "handoff_sha_in": handoff_sha_in,
                        "init_forensics": init_forensics,
                        "curve": curve,
                        "recent_losses": recent_losses,
                        "step_rows": step_rows,
                    },
                    opt_state=model.copy_opt_state(),
                    best_weights=best_w,
                )

        if s < steps:
            print(
                f"[Stage {snum} PAUSED] Saved crash-atomic recovery at local_step={s}/{steps} "
                f"(cum_tok={toks_c/1e6:.3f}M, stage_wall={st_wall:.1f}s)",
                flush=True,
            )
            return None

        stage_wall_s = stage_prev_wall + (time.perf_counter() - t0)
        end_w = model.copy_weights()
        end_opt = model.copy_opt_state()
        end_tel = model.telemetry()
        assert best_w is not None
        end_forensics = analyze_weights_forensics(model, end_w)

        end_sha = save_ckpt_atomic(
            stage_end_ckpt,
            end_w,
            {
                "kind": f"stage{snum}_end",
                "arch_id": arch_cfg["id"],
                "stage": snum,
                "local_step": steps,
                "global_step": g_start + steps,
                "opt_step": int(step_rows[-1]["opt_step"]),
                "tokens_cum": prev_tokens + steps * tok_per_step,
                "stage_wall_s": round(stage_wall_s, 4),
                "best_val_ce": best_val_ce,
                "best_global_step": best_global_step,
                "spec": sd,
            },
            opt_state=end_opt,
            best_weights=best_w,
        )

        # Clean up recovery pointer once stage_end is safely committed
        rec_meta_p = stage_rec_ckpt.with_suffix(".json")
        if rec_meta_p.exists():
            rm = json.loads(rec_meta_p.read_text(encoding="utf-8"))
            for k in ("weights_file", "opt_state_file", "best_weights_file"):
                fn = rm.get(k)
                if isinstance(fn, str):
                    (CKPT_DIR / fn).unlink(missing_ok=True)
            rec_meta_p.unlink(missing_ok=True)

        # Save Stage 3 permanent BCIRQ8 v1 checkpoint (tracked in git!) + verify reload parity
        bcirq8_info = None
        if snum == 3:
            bcirq8_path = CKPT_DIR / "arch7d_10m_stage3_best_bcirq8"
            q8_sha, q8_dec = save_bcirq8_ckpt(
                bcirq8_path,
                model,
                best_w,
                {
                    "arch_id": arch_cfg["id"],
                    "stage": 3,
                    "global_step": g_start + steps,
                    "best_global_step": best_global_step,
                    "tokens_trained": prev_tokens + steps * tok_per_step,
                    "best_val_ce_t512": round(best_val_ce, 6),
                    "spec": sd,
                    "optimizer": "Opt_Q8_Muon_BF16Grad",
                },
            )
            reloaded_w, reloaded_meta = load_bcirq8_ckpt(bcirq8_path, model)
            assert np.array_equal(reloaded_w, q8_dec), "BCIRQ8 reload bit mismatch"
            model.set_weights(reloaded_w)
            q8_val = model.evaluate(val_tk, val_tg)
            bcirq8_info = {
                "path": "checkpoints/arch7d_10m_stage3_best_bcirq8.bin",
                "sha256": q8_sha,
                "bytes": reloaded_meta["weights_bytes"],
                "mib": round(reloaded_meta["weights_bytes"] / 1048576.0, 2),
                "fp32_source_sha256": reloaded_meta["fp32_source_sha256"],
                "reload_exact_bit_match": True,
                "reload_val_ce_t512": round(q8_val["ce"], 6),
                "reload_val_ppl_t512": round(q8_val["ppl"], 4),
            }
            print(
                f"[Stage 3] Exported verified BCIRQ8 v1 checkpoint ({bcirq8_info['mib']} MiB, "
                f"sha={q8_sha[:12]}, val_ce_t512={bcirq8_info['reload_val_ce_t512']:.4f})",
                flush=True,
            )

    # Multi-horizon untouched test evaluations on BOTH end weights and best-val weights
    test_eval_end = eval_multi_horizon(sd, end_w, te_stream, [128, 256, 512])
    test_eval_best = eval_multi_horizon(sd, best_w, te_stream, [128, 256, 512])
    val_eval_t256_end = eval_multi_horizon(sd, end_w, va_stream, [256])["T256"]

    step_ms_list = [r["step_ms"] for r in step_rows]
    grad_norms = [r["grad_norm"] for r in step_rows]
    clip_cnt = sum(r["clipped"] for r in step_rows)
    csv_p = RESULTS_DIR / f"stage{snum}_steps.csv"
    write_steps_csv(csv_p, step_rows)

    summary = {
        "stage_num": snum,
        "name": sname,
        "title": stage_cfg["title"],
        "arch_id": arch_cfg["id"],
        "spec": sd,
        "optimizer": "Opt_Q8_Muon_BF16Grad",
        "host": host_info(),
        "rss": process_rss_mib(),
        "mem_stats": ms,
        "batch": B,
        "seq_len": T,
        "tokens_per_step": tok_per_step,
        "steps": steps,
        "global_step_start": g_start,
        "global_step_end": g_start + steps,
        "tokens_this_stage": steps * tok_per_step,
        "tokens_cumulative": prev_tokens + steps * tok_per_step,
        "lr_start": round(float(global_lrs[g_start]), 6),
        "lr_end": round(float(global_lrs[g_start + steps - 1]), 6),
        "stage_wall_s": round(stage_wall_s, 2),
        "cumulative_wall_s": round(prev_wall_s + stage_wall_s, 2),
        "median_step_ms": round(float(np.median(step_ms_list)), 2),
        "p95_step_ms": round(float(np.percentile(step_ms_list, 95)), 2),
        "throughput_tok_per_s": round((steps * tok_per_step) / max(stage_wall_s, 1e-6), 1),
        "grad_norm_mean": round(float(np.mean(grad_norms)), 4),
        "grad_norm_p95": round(float(np.percentile(grad_norms, 95)), 4),
        "grad_norm_max": round(float(np.max(grad_norms)), 4),
        "clip_hits": clip_cnt,
        "clip_rate_pct": round(100.0 * clip_cnt / max(len(step_rows), 1), 2),
        "telemetry": end_tel,
        "best_val_ce": round(best_val_ce, 6),
        "best_global_step": best_global_step,
        "val_t256_end": val_eval_t256_end,
        "test_multi_horizon_end": test_eval_end,
        "test_multi_horizon_best": test_eval_best,
        "handoff_weights_sha256_in": handoff_sha_in,
        "end_weights_sha256": end_sha,
        "best_weights_sha256": hashlib.sha256(np.ascontiguousarray(best_w, dtype=np.float32).tobytes()).hexdigest(),
        "bcirq8_export": bcirq8_info,
        "init_forensics": init_forensics,
        "end_forensics": end_forensics,
        "curve": curve,
        "steps_csv": f"results/stage{snum}_steps.csv",
    }
    out_p = RESULTS_DIR / f"{sname}.json"
    save_json(out_p, summary)
    print(
        f"[Stage {snum} DONE] cum_tok={summary['tokens_cumulative']/1e6:.3f}M | "
        f"test(T256)={test_eval_best['T256']['ce']:.4f} (PPL {test_eval_best['T256']['ppl']:.2f}) | "
        f"test(T512)={test_eval_best['T512']['ce']:.4f} (PPL {test_eval_best['T512']['ppl']:.2f}) | "
        f"clip={summary['clip_rate_pct']:.1f}% | wall={summary['stage_wall_s']:.1f}s -> {out_p}",
        flush=True,
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["1", "2", "3", "all"], default="all")
    ap.add_argument("--config-id", type=str, default=None)
    ap.add_argument("--max-steps", type=int, default=None, help="Run at most N steps then save recovery checkpoint")
    args = ap.parse_args()

    arch_cfg = load_selected_arch_cfg(args.config_id)
    global_lrs = cosine_lr_schedule(TOTAL_GLOBAL_STEPS, MAX_LR, MIN_LR, WARMUP_STEPS)
    stages = STAGE_PLAN if args.stage == "all" else [STAGE_PLAN[int(args.stage) - 1]]
    for st in stages:
        res = run_stage(st, arch_cfg, global_lrs, max_steps_this_call=args.max_steps)
        if res is None:
            break


if __name__ == "__main__":
    main()
