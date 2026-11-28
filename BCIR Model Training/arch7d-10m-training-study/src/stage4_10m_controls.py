#!/usr/bin/env python3
"""Stage 4: 4-way parameter-matched 10M architecture & optimizer controls at identical
B=8, T=256, 96 steps (196,608 real tokens) on WikiText-2 raw bytes."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import numpy as np

from common_10m import (
    CANON_OFF,
    CKPT_DIR,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GQA,
    OPT_ADAFACTOR_M2,
    OPT_NAMES,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    Abi6Model,
    AdamWParams,
    apply_comp1_ptq_weights,
    comp1_weight_bytes,
    cosine_lr_schedule,
    host_info,
    load_ckpt_verified,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_policy,
    make_spec,
    process_rss_mib,
    q8_weight_bytes,
    quantize_bcirq8_per_tensor,
    sample_lm_steps,
    save_ckpt_atomic,
    save_json,
    write_steps_csv,
)


def load_stage0_winner_spec() -> Dict[str, Any]:
    s0 = json.loads((RESULTS_DIR / "stage0_sizing_and_ablation.json").read_text(encoding="utf-8"))
    wid = s0["micro_ablation"]["selected_id"]
    for c in s0["micro_ablation"]["candidates"]:
        if c["id"] == wid:
            return {"id": wid, "spec": c["spec"]}
    raise ValueError(f"Winner {wid!r} missing from Stage 0")


def main() -> None:
    import argparse
    import csv

    ap = argparse.ArgumentParser()
    ap.add_argument("--only-control", type=str, default=None, help="Run a single control by name and merge")
    ap.add_argument("--max-steps", type=int, default=None, help="Run at most N steps then save recovery ckpt")
    args = ap.parse_args()

    winner = load_stage0_winner_spec()
    sd = winner["spec"]

    B, T, steps, eval_every = 8, 256, 96, 24
    tok_per_step = B * T
    total_tokens = steps * tok_per_step  # 196,608 (matches Stage 1 token budget)

    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")
    val_tk, val_tg = make_fixed_eval_batches(va_stream, 8, B, T)
    test_tk_256, test_tg_256 = make_fixed_eval_batches(te_stream, 8, B, 256)
    test_tk_512, test_tg_512 = make_fixed_eval_batches(te_stream, 8, 4, 512)
    lrs = cosine_lr_schedule(steps, 3.0e-3, 3.0e-4, 16)

    controls = [
        (
            "GQA_Full_10M_Adafactor",
            make_spec(
                vocab=256, width=512, heads=8, kvheads=2, layers=4, ff=1152,
                batch=B, time_len=T, layer_mixers=[MIXER_GQA] * 4, layer_use_rope=[1] * 4,
                mla_dc=192, mla_dr=32, gate_mode=GATE_NONE, qk_norm=0, canon_mode=CANON_OFF,
                mhc_streams=1, online_softmax=1, gate_rank=64, seed=42, threads=2,
            ),
            OPT_ADAFACTOR_M2,
        ),
        (
            "Arch2_Hybrid_10M_Adafactor",
            make_spec(
                vocab=256, width=512, heads=8, kvheads=2, layers=4, ff=960,
                batch=B, time_len=T,
                layer_mixers=[MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
                layer_use_rope=[1] * 4, mla_dc=192, mla_dr=32, gate_mode=GATE_NONE,
                qk_norm=0, canon_mode=CANON_OFF, mhc_streams=1, online_softmax=1,
                gate_rank=64, seed=42, threads=2,
            ),
            OPT_ADAFACTOR_M2,
        ),
        (
            "Arch7d_10M_Adafactor",
            make_spec(
                vocab=sd["vocab"], width=sd["width"], heads=sd["heads"], kvheads=sd["kvheads"],
                layers=sd["layers"], ff=sd["ff"], batch=B, time_len=T,
                layer_mixers=sd["layer_mixer"], layer_use_rope=sd["layer_use_rope"],
                mla_dc=sd["mla_dc"], mla_dr=sd["mla_dr"], gate_mode=sd["gate_mode"],
                qk_norm=sd["qk_norm"], canon_mode=sd["canon_mode"],
                mhc_streams=sd["mhc_streams"], online_softmax=1,
                gate_rank=sd["gate_rank"], seed=42, threads=2,
            ),
            OPT_ADAFACTOR_M2,
        ),
        (
            "Arch7d_10M_Q8Muon_FlatT256",
            make_spec(
                vocab=sd["vocab"], width=sd["width"], heads=sd["heads"], kvheads=sd["kvheads"],
                layers=sd["layers"], ff=sd["ff"], batch=B, time_len=T,
                layer_mixers=sd["layer_mixer"], layer_use_rope=sd["layer_use_rope"],
                mla_dc=sd["mla_dc"], mla_dr=sd["mla_dr"], gate_mode=sd["gate_mode"],
                qk_norm=sd["qk_norm"], canon_mode=sd["canon_mode"],
                mhc_streams=sd["mhc_streams"], online_softmax=1,
                gate_rank=sd["gate_rank"], seed=42, threads=2,
            ),
            OPT_Q8_MUON_BF16_GRAD,
        ),
    ]

    if args.only_control:
        controls = [c for c in controls if c[0] == args.only_control]
        if not controls:
            raise SystemExit(f"Unknown --only-control: {args.only_control}")

    out_p = RESULTS_DIR / "stage4_10m_controls.json"
    csv_p = RESULTS_DIR / "stage4_controls_steps.csv"
    runs: Dict[str, Any] = {}
    if out_p.exists():
        runs = dict(json.loads(out_p.read_text(encoding="utf-8")).get("runs", {}))
    all_step_rows: List[Dict[str, Any]] = []
    if csv_p.exists():
        with csv_p.open("r", encoding="utf-8") as f:
            all_step_rows = [dict(r) for r in csv.DictReader(f)]
    target_names = {c[0] for c in controls}
    all_step_rows = [r for r in all_step_rows if r.get("control") not in target_names]

    for name, sp, opt_k in controls:
        beta1 = 0.95 if opt_k == OPT_Q8_MUON_BF16_GRAD else 0.90
        opt_cfg = AdamWParams(3.0e-3, beta1, 0.95, 1e-8, 0.01, 3.0)
        pol = make_policy(opt_kind=opt_k)
        rec_ckpt = CKPT_DIR / f"stage4_{name}_recovery"
        resume_mid = rec_ckpt.with_suffix(".json").exists()
        if resume_mid:
            sp.skip_init = 1
        curve: List[Dict[str, Any]] = []
        ctrl_step_rows: List[Dict[str, Any]] = []
        step_ms_list: List[float] = []
        grad_norms: List[float] = []
        clip_hits = 0
        start_s = 0
        prev_wall = 0.0

        with Abi6Model(sp, pol) as model:
            ms = model.mem_stats()
            if resume_mid:
                w_ld, meta, opt_ld, best_ld = load_ckpt_verified(rec_ckpt)
                assert opt_ld is not None
                model.set_weights(w_ld)
                model.set_opt_state(opt_ld)
                start_s = int(meta["step"])
                prev_wall = float(meta["wall_s"])
                curve = list(meta["curve"])
                ctrl_step_rows = list(meta["ctrl_step_rows"])
                step_ms_list = [float(r["step_ms"]) for r in ctrl_step_rows]
                grad_norms = [float(r["grad_norm"]) for r in ctrl_step_rows]
                clip_hits = sum(int(r["clipped"]) for r in ctrl_step_rows)
                best_val_ce = float(meta["best_val_ce"])
                best_step = int(meta["best_step"])
                best_w = best_ld if best_ld is not None else w_ld.copy()
                print(f"[Stage 4] Resumed {name} at step={start_s}/{steps}", flush=True)
            else:
                v0 = model.evaluate(val_tk, val_tg)
                best_val_ce = v0["ce"]
                best_step = 0
                best_w = model.copy_weights()
                curve.append({
                    "step": 0, "tokens": 0, "wall_s": 0.0,
                    "val_ce": round(v0["ce"], 6), "val_ppl": round(v0["ppl"], 4), "val_bpb": round(v0["bpb"], 5),
                })

            stop_s = min(steps, start_s + args.max_steps) if args.max_steps is not None else steps
            t0 = time.perf_counter()
            s = start_s
            while s < stop_s:
                next_eval = ((s // eval_every) + 1) * eval_every
                chunk = min(next_eval - s, stop_s - s)
                tr_tk, tr_tg = sample_lm_steps(tr_stream, s, chunk, B, T, base_seed=440000)
                rc, evs = model.train_loop(tr_tk, tr_tg, opt_cfg, lrs=lrs[s : s + chunk])
                if rc != 0:
                    raise RuntimeError(f"Stage 4 {name} failed rc={rc} at step {s}")
                for idx_e, ev in enumerate(evs):
                    st = s + idx_e + 1
                    st_ms = float(ev["step_ms"])
                    step_ms_list.append(st_ms)
                    grad_norms.append(float(ev["grad_norm"]))
                    clip_hits += int(ev["clipped"])
                    ctrl_step_rows.append({
                        "control": name,
                        "optimizer": OPT_NAMES[opt_k],
                        "step": st,
                        "tokens": st * tok_per_step,
                        "lr": round(float(lrs[st - 1]), 7),
                        "loss": round(float(ev["loss"]), 6),
                        "grad_norm": round(float(ev["grad_norm"]), 5),
                        "clipped": int(ev["clipped"]),
                        "step_ms": round(st_ms, 2),
                        "tok_per_s": round(tok_per_step / max(st_ms / 1000.0, 1e-6), 1),
                    })
                s += chunk
                wall = prev_wall + (time.perf_counter() - t0)
                if s % eval_every == 0 or s == steps:
                    vv = model.evaluate(val_tk, val_tg)
                    curve.append({
                        "step": s,
                        "tokens": s * tok_per_step,
                        "wall_s": round(wall, 2),
                        "val_ce": round(vv["ce"], 6),
                        "val_ppl": round(vv["ppl"], 4),
                        "val_bpb": round(vv["bpb"], 5),
                        "val_acc": round(vv["accuracy"], 5),
                        "tok_per_s": round((s * tok_per_step) / max(wall, 1e-6), 1),
                    })
                    if vv["ok"] and vv["ce"] < best_val_ce:
                        best_val_ce = vv["ce"]
                        best_step = s
                        best_w = model.copy_weights()
                    print(
                        f"[Stage 4] {name:28s} | st={s:3d}/{steps} tok={s*tok_per_step/1e3:5.1f}k | "
                        f"val_ce={vv['ce']:.4f} PPL={vv['ppl']:5.2f} | {curve[-1]['tok_per_s']:5.0f} tok/s ({wall:5.1f}s)",
                        flush=True,
                    )

            if s < steps:
                save_ckpt_atomic(
                    rec_ckpt,
                    model.copy_weights(),
                    {
                        "control": name,
                        "step": s,
                        "wall_s": round(prev_wall + (time.perf_counter() - t0), 4),
                        "best_val_ce": best_val_ce,
                        "best_step": best_step,
                        "curve": curve,
                        "ctrl_step_rows": ctrl_step_rows,
                    },
                    opt_state=model.copy_opt_state(),
                    best_weights=best_w,
                )
                print(f"[Stage 4 PAUSED] Saved recovery for {name} at step {s}/{steps}", flush=True)
                return

            all_step_rows.extend(ctrl_step_rows)
            wall_total = prev_wall + (time.perf_counter() - t0)
            if rec_ckpt.with_suffix(".json").exists():
                rm = json.loads(rec_ckpt.with_suffix(".json").read_text(encoding="utf-8"))
                for k in ("weights_file", "opt_state_file", "best_weights_file"):
                    fn = rm.get(k)
                    if isinstance(fn, str):
                        (CKPT_DIR / fn).unlink(missing_ok=True)
                rec_ckpt.with_suffix(".json").unlink(missing_ok=True)
            model.set_weights(best_w)
            model.reset_telemetry()
            test256 = model.evaluate(test_tk_256, test_tg_256)
            eval_tel = model.telemetry()

            q8_w, _ = quantize_bcirq8_per_tensor(model, best_w)
            model.set_weights(q8_w)
            q8_ev = model.evaluate(test_tk_256, test_tg_256)

            c1_w = apply_comp1_ptq_weights(model, best_w, rank=16, scale_bits=32)
            model.set_weights(c1_w)
            c1_ev = model.evaluate(test_tk_256, test_tg_256)

            q8_b = q8_weight_bytes(model)
            c1_info = comp1_weight_bytes(model, rank=16, scale_bits=32)

        # Zero-shot T=512 test evaluation on the trained T=256 weights
        sp512 = make_spec(
            vocab=int(sp.vocab), width=int(sp.width), heads=int(sp.heads), kvheads=int(sp.kvheads),
            layers=int(sp.layers), ff=int(sp.ff), batch=4, time_len=512,
            layer_mixers=[int(sp.layer_mixer[i]) for i in range(int(sp.layers))],
            layer_use_rope=[int(sp.layer_use_rope[i]) for i in range(int(sp.layers))],
            mla_dc=int(sp.mla_dc), mla_dr=int(sp.mla_dr), gate_mode=int(sp.gate_mode),
            qk_norm=int(sp.qk_norm), canon_mode=int(sp.canon_mode),
            mhc_streams=int(sp.mhc_streams), online_softmax=1,
            gate_rank=int(sp.gate_rank), seed=42, threads=2, skip_init=1,
        )
        with Abi6Model(sp512, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m512:
            m512.set_weights(best_w)
            test512 = m512.evaluate(test_tk_512, test_tg_512)

        kv4k = (ms["infer_kv_bytes_per_tok"] * 4096) / 1048576.0
        rec_m = ms["infer_rec_state_bytes"] / 1048576.0
        st4k = kv4k + rec_m
        runs[name] = {
            "optimizer": OPT_NAMES[opt_k],
            "n_params": ms["n_params"],
            "persistent_mib": round(ms["persistent_bytes"] / 1048576.0, 2),
            "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
            "arena_mib": round(ms["total_bytes"] / 1048576.0, 2),
            "state_4k_mib": round(st4k, 3),
            "total_4k_q8_mib": round(q8_b / 1048576.0 + st4k, 2),
            "total_4k_comp1_fp32scale_mib": round(c1_info["total_bytes"] / 1048576.0 + st4k, 2),
            "comp1_mlp_bpw": c1_info["mlp_comp1_bpw"],
            "steps": steps,
            "tokens": total_tokens,
            "best_step": best_step,
            "best_val_ce": round(best_val_ce, 6),
            "test_ce_t256": round(test256["ce"], 6),
            "test_ppl_t256": round(test256["ppl"], 4),
            "test_bpb_t256": round(test256["bpb"], 5),
            "test_acc_t256": round(test256["accuracy"], 5),
            "test_ce_t512_zeroshot": round(test512["ce"], 6),
            "test_ppl_t512_zeroshot": round(test512["ppl"], 4),
            "q8_test_ce": round(q8_ev["ce"], 6),
            "q8_delta_ce": round(q8_ev["ce"] - test256["ce"], 6),
            "comp1_ptq_ce": round(c1_ev["ce"], 6),
            "comp1_ptq_delta_ce": round(c1_ev["ce"] - test256["ce"], 6),
            "max_abs_act": round(eval_tel["max_abs_act"], 3),
            "first_tok_attn_mean": round(eval_tel["first_tok_attn_mean"], 4),
            "median_step_ms": round(float(np.median(step_ms_list)), 2),
            "wall_s": round(wall_total, 2),
            "throughput_tok_per_s": round(total_tokens / max(wall_total, 1e-6), 1),
            "grad_norm_mean": round(float(np.mean(grad_norms)), 4),
            "clip_rate_pct": round(100.0 * clip_hits / steps, 2),
            "curve": curve,
        }
        save_json(
            out_p,
            {
                "stage": "Stage 4 — Parameter-Matched 10M Architecture & Optimizer Controls",
                "arch_winner_id": winner["id"],
                "host": host_info(),
                "rss": process_rss_mib(),
                "batch": B,
                "seq_len": T,
                "steps": steps,
                "tokens": total_tokens,
                "runs": runs,
            },
        )
        write_steps_csv(RESULTS_DIR / "stage4_controls_steps.csv", all_step_rows)

    print(f"[Stage 4 DONE] Saved {out_p} and results/stage4_controls_steps.csv")


if __name__ == "__main__":
    main()
