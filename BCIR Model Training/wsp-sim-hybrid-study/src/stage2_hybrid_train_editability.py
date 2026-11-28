#!/usr/bin/env python3
"""Stage 2: Parameter-Matched 3-Architecture Training (Arch7d vs Arch2 vs GQA_Full)
+ Cross-Architecture WSP/SSP Editability Forensics + 10M COMP-1 Deployment Diagnosis.

Produces:
  - Per-step training telemetry CSV (results/stage2_train_steps.csv)
  - Checkpoint-by-checkpoint organic loop & WSP-2 health trajectory
  - Layer-by-layer (L0..L3) sparse WSP & SSP-Live editability audit across all 3 architectures
  - Saved BCIRQ8 checkpoints for Arch7d_2M & GQA_Full_2M (used in Stage 3 Cross-Model SIM CRSI)
  - COMP-1 (1.146 bpw MLP) 10M behavioral diagnosis (used in Stage 3 Cross-Precision SIM CRSI)
"""
from __future__ import annotations

import ctypes as ct
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from common_wsp_sim import (
    AdamWParams,
    Abi6Model,
    CANON_AC,
    CANON_OFF,
    CKPT_DIR,
    GATE_G1_HEADWISE,
    GATE_NONE,
    MIXER_DELTANET,
    MIXER_GDN2,
    MIXER_GQA,
    MIXER_MLA,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    StatefulProbe,
    audit_weight_health,
    build_comp1_weights,
    composite_behavior_score,
    compute_kl_and_ce,
    compute_rollout_metrics,
    extract_domain_prompts,
    get_host_provenance,
    label_loop_tokens,
    load_arch7d_10m_tracked,
    load_lib,
    load_wikitext_bytes,
    make_eval_batches,
    make_policy,
    make_spec,
    make_ssp_config,
    write_csv,
    write_json,
)
from stage1_wsp_ssp_locus_audit import (
    apply_g1_bias_edit,
    apply_head_wo_zero,
    apply_wdown_sparse_deflation,
    compute_layer_diagnostics,
    eval_arm,
)


def encode_bcirq8_v1_per_tensor(path: Path, model: Abi6Model, w: np.ndarray) -> str:
    import hashlib
    lib = load_lib()
    chunks = []
    for ti in model.tensor_infos():
        off, n = ti["offset"], ti["rows"] * ti["cols"]
        nb = (n + 31) // 32
        src = np.ascontiguousarray(w[off:off + n], dtype=np.float32)
        q = np.empty(n, dtype=np.int8)
        ex = np.empty(nb, dtype=np.int8)
        lib.bcir_quant_q8_g32_encode(
            src.ctypes.data_as(ct.POINTER(ct.c_float)),
            q.ctypes.data_as(ct.POINTER(ct.c_int8)),
            ex.ctypes.data_as(ct.POINTER(ct.c_int8)),
            n,
        )
        chunks.append(q.tobytes())
        chunks.append(ex.tobytes())
    raw = b"".join(chunks)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def build_2m_specs() -> Dict[str, Any]:
    """Parameter-matched ~2.5M 4-layer models (d=256, h=4, kvh=2, B=8, T=128)."""
    return {
        "Arch7d_2M": make_spec(
            vocab=256, width=256, heads=4, kvheads=2, layers=4, ff=480,
            batch=8, time_len=128, tied=1,
            layer_mixers=[MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA],
            layer_use_rope=[0, 0, 0, 0], delta_chunk=16, mla_dc=64, mla_dr=32,
            gate_mode=GATE_G1_HEADWISE, qk_norm=1, canon_mode=CANON_AC,
            mhc_streams=2, online_softmax=1, gate_rank=32, seed=42, threads=2,
        ),
        "Arch2_Hybrid_2M": make_spec(
            vocab=256, width=256, heads=4, kvheads=2, layers=4, ff=512,
            batch=8, time_len=128, tied=1,
            layer_mixers=[MIXER_DELTANET, MIXER_DELTANET, MIXER_DELTANET, MIXER_GQA],
            layer_use_rope=[0, 0, 0, 1], delta_chunk=16, mla_dc=64, mla_dr=32,
            gate_mode=GATE_NONE, qk_norm=0, canon_mode=CANON_OFF,
            mhc_streams=1, online_softmax=1, gate_rank=32, seed=42, threads=2,
        ),
        "GQA_Full_2M": make_spec(
            vocab=256, width=256, heads=4, kvheads=2, layers=4, ff=608,
            batch=8, time_len=128, tied=1,
            layer_mixers=[MIXER_GQA, MIXER_GQA, MIXER_GQA, MIXER_GQA],
            layer_use_rope=[1, 1, 1, 1], delta_chunk=16, mla_dc=64, mla_dr=32,
            gate_mode=GATE_NONE, qk_norm=0, canon_mode=CANON_OFF,
            mhc_streams=1, online_softmax=1, gate_rank=32, seed=42, threads=2,
        ),
    }


def cosine_lr_schedule(steps: int, warmup: int, lr_max: float, lr_min: float) -> np.ndarray:
    lrs = np.empty(steps, dtype=np.float64)
    for s in range(steps):
        if s < warmup:
            lrs[s] = lr_max * float(s + 1) / float(max(1, warmup))
        else:
            frac = float(s - warmup) / float(max(1, steps - warmup - 1))
            lrs[s] = lr_min + 0.5 * (lr_max - lr_min) * (1.0 + math.cos(math.pi * frac))
    return lrs


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=str, default="all",
                    choices=["all", "Arch7d_2M", "Arch2_Hybrid_2M", "GQA_Full_2M", "comp1"])
    args = ap.parse_args()

    t0_all = time.monotonic()
    print(f"=== Stage 2 ({args.only}): 3-Architecture Matched Training & Cross-Locus Editability Study ===", flush=True)

    train_stream = load_wikitext_bytes("train")
    val_stream = load_wikitext_bytes("val")
    test_stream = load_wikitext_bytes("test")

    total_steps = 160
    chunk_steps = 40
    B, T = 8, 128
    tr_xs, tr_ys = make_eval_batches(train_stream, batch=B, seq_len=T, n_batches=total_steps, seed=20261127)
    val_xs, val_ys = make_eval_batches(val_stream, batch=B, seq_len=T, n_batches=6, seed=3101)
    test_xs, test_ys = make_eval_batches(test_stream, batch=B, seq_len=T, n_batches=10, seed=4101)

    prompt_len, gen_len = 48, 80
    ev_prompts = extract_domain_prompts(val_stream, "prose_unloop", n_prompts=16, prompt_len=prompt_len, seed=101)
    te_prompts = extract_domain_prompts(test_stream, "prose_unloop", n_prompts=32, prompt_len=prompt_len, seed=202)
    lrs = cosine_lr_schedule(total_steps, warmup=24, lr_max=4.0e-3, lr_min=4.0e-4)
    opt_cfg = AdamWParams(lr=4.0e-3, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.05, grad_clip=3.0)

    partial_p = RESULTS_DIR / "stage2_partial.json"
    cache = json.loads(partial_p.read_text(encoding="utf-8")) if partial_p.exists() else {
        "steps": {}, "architectures": {}, "layer_edit": {}, "comp1": None
    }

    specs = build_2m_specs()
    for arch_name, sp in specs.items():
        if args.only not in ("all", arch_name):
            continue
        print(f"\n--- Training {arch_name} ({total_steps} steps = {total_steps*B*T:,} tokens) ---", flush=True)
        pol = make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)
        model = Abi6Model(sp, pol)
        ms = model.mem_stats()
        probe = StatefulProbe(model, max_seq=256)
        print(f"  Params={ms['n_params']:,} | Pers={ms['persistent_bytes']/1048576:.2f} MiB | "
              f"T=4096 state={(ms['infer_kv_bytes_per_tok']*4096 + ms['infer_rec_state_bytes'])/1048576:.2f} MiB",
              flush=True)

        traj = []
        arch_steps = []
        t_arch = time.monotonic()
        train_only_sec = 0.0
        for c_start in range(0, total_steps, chunk_steps):
            c_end = min(total_steps, c_start + chunk_steps)
            t_tr = time.monotonic()
            rc, evs = model.train_loop(tr_xs[c_start:c_end], tr_ys[c_start:c_end], opt_cfg, lrs=lrs[c_start:c_end])
            train_only_sec += time.monotonic() - t_tr
            assert rc == 0, f"{arch_name} train_loop failed rc={rc}"
            for i, e in enumerate(evs):
                gs = c_start + i + 1
                arch_steps.append({
                    "arch": arch_name, "step": gs, "tokens": gs * B * T,
                    "lr": round(float(lrs[gs - 1]), 7), "loss": round(float(e["loss"]), 5),
                    "grad_norm": round(float(e["grad_norm"]), 4), "clipped": int(e["clipped"]),
                    "step_ms": round(float(e["step_ms"]), 2),
                })
            val_m = model.evaluate(val_xs, val_ys)
            tel = model.telemetry()
            probe.sync()
            out_t, out_e, _ = probe.generate_batch(ev_prompts, gen_len)
            roll = compute_rollout_metrics(out_t, prompt_len, out_e)
            h_stat = audit_weight_health(model)
            snap = {
                "step": c_end, "tokens": c_end * B * T,
                "val_ce": round(val_m["ce"], 5), "val_ppl": round(val_m["ppl"], 3),
                "evolve_loop_seq_rate": round(roll["loop_seq_rate"], 4),
                "evolve_rep4_rate": round(roll["rep4_rate"], 4),
                "evolve_distinct4": round(roll["distinct_4"], 4),
                "max_abs_act": round(tel["max_abs_act"], 4),
                "attn_sink": round(tel["first_tok_attn_mean"], 4),
                "wsp2_pass": h_stat["pass"],
                "wsp2_max_over_med": round(max(v["max_over_median"] for v in h_stat["wdown_stats"].values()), 3),
            }
            traj.append(snap)
            print(f"  step={c_end:3d}: val_ppl={snap['val_ppl']:.2f} | loop={snap['evolve_loop_seq_rate']*100:.1f}% "
                  f"dist4={snap['evolve_distinct4']*100:.1f}% | max|act|={snap['max_abs_act']:.2f} "
                  f"sink={snap['attn_sink']:.3f}", flush=True)

        train_plus_diag_sec = time.monotonic() - t_arch
        train_sec = train_only_sec
        w_final = model.copy_weights()

        ckpt_sha = None
        if arch_name in ("Arch7d_2M", "GQA_Full_2M"):
            bin_p = CKPT_DIR / f"{arch_name.lower()}_bcirq8.bin"
            ckpt_sha = encode_bcirq8_v1_per_tensor(bin_p, model, w_final)
            write_json(CKPT_DIR / f"{arch_name.lower()}_bcirq8.json", {
                "arch_name": arch_name, "n_params": int(model.n_params),
                "tokens_trained": total_steps * B * T, "weights_file": bin_p.name,
                "weights_sha256": ckpt_sha,
                "spec": {
                    "vocab": int(sp.vocab), "width": int(sp.width), "heads": int(sp.heads),
                    "kvheads": int(sp.kvheads), "layers": int(sp.layers), "ff": int(sp.ff),
                    "batch": int(sp.batch), "time": int(sp.time), "tied": int(sp.tied),
                    "layer_mixer": [int(sp.layer_mixer[i]) for i in range(int(sp.layers))],
                    "layer_use_rope": [int(sp.layer_use_rope[i]) for i in range(int(sp.layers))],
                    "delta_chunk": int(sp.delta_chunk), "mla_dc": int(sp.mla_dc), "mla_dr": int(sp.mla_dr),
                    "gate_mode": int(sp.gate_mode), "qk_norm": int(sp.qk_norm),
                    "canon_mode": int(sp.canon_mode), "mhc_streams": int(sp.mhc_streams),
                    "online_softmax": int(sp.online_softmax), "gate_rank": int(sp.gate_rank),
                    "seed": int(sp.seed), "threads": int(sp.threads),
                },
            })

        ev_ref_ce, ev_ref_logits = compute_kl_and_ce(model, val_xs, val_ys)
        te_ref_ce, te_ref_logits = compute_kl_and_ce(model, test_xs, test_ys)
        base_te = eval_arm(model, probe, w_final, w_final, None, te_prompts, gen_len,
                           test_xs, test_ys, te_ref_logits, te_ref_ce["ce"])

        ev_toks, _, _ = probe.generate_batch(ev_prompts, gen_len)
        mask_loop = label_loop_tokens(ev_toks, prompt_len)
        contrast = probe.probe_contrast(ev_toks, mask_loop)
        diag = compute_layer_diagnostics(model, contrast, w_final)

        per_layer_results = {}
        arch_layer_rows = []
        for l in range(int(sp.layers)):
            ld = diag["layers"][f"L{l}"]
            best_ev = None
            best_w = w_final
            best_cfg = {}
            for k, a in ((16, 1.0), (32, 0.8), (32, 1.4), (64, 1.0), (64, 1.5)):
                wc = apply_wdown_sparse_deflation(model, w_final, l, ld["u_vec"], ld["top_cols_all"][:k], a)
                em = eval_arm(model, probe, w_final, wc, None, ev_prompts, gen_len,
                              val_xs, val_ys, ev_ref_logits, ev_ref_ce["ce"])
                key = (1 if em["kl_from_clean"] <= 0.06 else 0, em["score"])
                if best_ev is None or key > best_ev[0]:
                    best_ev = (key, em)
                    best_w = wc
                    best_cfg = {"k": k, "alpha": a}
            tm = eval_arm(model, probe, w_final, best_w, None, te_prompts, gen_len,
                          test_xs, test_ys, te_ref_logits, te_ref_ce["ce"])
            loop_drop = base_te["loop_seq_rate"] - tm["loop_seq_rate"]
            score_gain = tm["score"] - base_te["score"]
            eff = score_gain / max(tm["kl_from_clean"], 1e-4)
            per_layer_results[f"L{l}"] = {
                "mixer": int(sp.layer_mixer[l]), "resid_contrast_norm": round(ld["resid_contrast_norm"], 4),
                "cfg": best_cfg, "test_score": round(tm["score"], 3), "score_gain": round(score_gain, 3),
                "test_loop_seq_rate": round(tm["loop_seq_rate"], 4), "loop_drop": round(loop_drop, 4),
                "test_distinct4": round(tm["distinct_4"], 4), "test_delta_ce": round(tm["delta_ce"], 5),
                "test_kl": round(tm["kl_from_clean"], 6), "gain_per_kl": round(eff, 1),
            }
            arch_layer_rows.append({
                "arch": arch_name, "layer": f"L{l}", "mixer_id": int(sp.layer_mixer[l]),
                "resid_contrast_norm": round(ld["resid_contrast_norm"], 4),
                "base_score": round(base_te["score"], 3), "edit_score": round(tm["score"], 3),
                "score_gain": round(score_gain, 3), "base_loop_rate": round(base_te["loop_seq_rate"], 4),
                "edit_loop_rate": round(tm["loop_seq_rate"], 4), "test_delta_ce": round(tm["delta_ce"], 5),
                "test_kl": round(tm["kl_from_clean"], 6), "gain_per_kl": round(eff, 1),
            })
            print(f"  [Edit L{l}] score={tm['score']:.2f} (d={score_gain:+.2f}) | "
                  f"loop={tm['loop_seq_rate']*100:.1f}% (base {base_te['loop_seq_rate']*100:.1f}%) | "
                  f"dCE={tm['delta_ce']:+.4f} KL={tm['kl_from_clean']:.5f}", flush=True)

        ld3 = diag["layers"]["L3"]
        if sp.gate_mode == GATE_G1_HEADWISE:
            w_g1 = apply_g1_bias_edit(model, w_final, 3, ld3["top_heads"][:1], -1.0)
            g1_te = eval_arm(model, probe, w_final, w_g1, None, te_prompts, gen_len,
                             test_xs, test_ys, te_ref_logits, te_ref_ce["ce"])
        else:
            w_wo = apply_head_wo_zero(model, w_final, 3, ld3["top_heads"][:1], 0.5)
            g1_te = eval_arm(model, probe, w_final, w_wo, None, te_prompts, gen_len,
                             test_xs, test_ys, te_ref_logits, te_ref_ce["ce"])

        ssp_cfg = make_ssp_config(state_echo_damp=0.35, gdn_alpha_shift=-0.8, gdn_erase_shift=1.2,
                                  mla_repeat_logit_bias=-1.5, adaptive_echo_only=1, echo_ngram=4, echo_window=48)
        ssp_te = eval_arm(model, probe, w_final, w_final, ssp_cfg, te_prompts, gen_len,
                          test_xs, test_ys, te_ref_logits, te_ref_ce["ce"])
        print(f"  [SSP-Live] score={ssp_te['score']:.2f} (d={ssp_te['score']-base_te['score']:+.2f}) | "
              f"loop={ssp_te['loop_seq_rate']*100:.1f}% | dCE=0.0000 KL=0.0000", flush=True)

        cache["steps"][arch_name] = arch_steps
        cache["layer_edit"][arch_name] = arch_layer_rows
        cache["architectures"][arch_name] = {
            "n_params": int(ms["n_params"]),
            "persistent_mib": round(ms["persistent_bytes"] / 1048576.0, 2),
            "t4096_state_mib": round((ms["infer_kv_bytes_per_tok"] * 4096 + ms["infer_rec_state_bytes"]) / 1048576.0, 3),
            "train_seconds": round(train_sec, 2),
            "train_plus_diag_seconds": round(train_plus_diag_sec, 2),
            "tok_per_s": round(total_steps * B * T / max(train_sec, 1e-6), 1),
            "bcirq8_sha256": ckpt_sha, "trajectory": traj,
            "final_test_baseline": base_te, "per_layer_wsp": per_layer_results,
            "l3_head_or_g1_test": g1_te, "ssp_live_test": ssp_te,
        }
        write_json(partial_p, cache)
        probe.close()
        model.close()

    if args.only in ("all", "comp1"):
        print("\n--- Part 2B: Diagnosing COMP-1 (1.146 bpw MLP) Behavioral Shift on BCIR-Arch7d-10M ---", flush=True)
        m10, w10_q8, _ = load_arch7d_10m_tracked(batch=4, time_len=256, threads=2)
        t_c1 = time.monotonic()
        w10_comp1 = build_comp1_weights(m10, w10_q8, rank=16, fp16_scales=True)
        c1_fac_sec = time.monotonic() - t_c1
        te10_xs, te10_ys = make_eval_batches(test_stream, batch=4, seq_len=256, n_batches=8, seed=401)
        te10_prompts = extract_domain_prompts(test_stream, "prose_unloop", n_prompts=32, prompt_len=48, seed=202)
        p10 = StatefulProbe(m10, max_seq=256)
        m10.set_weights(w10_q8)
        q8_ce, q8_ref_logits = compute_kl_and_ce(m10, te10_xs, te10_ys)
        q8_te = eval_arm(m10, p10, w10_q8, w10_q8, None, te10_prompts, 80, te10_xs, te10_ys, q8_ref_logits, q8_ce["ce"])
        c1_te = eval_arm(m10, p10, w10_q8, w10_comp1, None, te10_prompts, 80, te10_xs, te10_ys, q8_ref_logits, q8_ce["ce"])
        print(f"  [10M Q8 v1]   CE={q8_te['ce']:.4f} PPL={q8_te['ppl']:.2f} | loop={q8_te['loop_seq_rate']*100:.1f}% dist4={q8_te['distinct_4']*100:.1f}%", flush=True)
        print(f"  [10M COMP-1]  CE={c1_te['ce']:.4f} PPL={c1_te['ppl']:.2f} (dCE={c1_te['delta_ce']:+.4f}) | "
              f"loop={c1_te['loop_seq_rate']*100:.1f}% dist4={c1_te['distinct_4']*100:.1f}% (fac={c1_fac_sec:.2f}s)", flush=True)
        cache["comp1"] = {"factorize_seconds": round(c1_fac_sec, 2), "q8_v1_baseline": q8_te, "comp1_r16_fp16s": c1_te}
        p10.close()
        m10.close()
        s2_final = RESULTS_DIR / "stage2_hybrid_train_editability.json"
        if args.only == "comp1" and s2_final.exists() and len(cache["architectures"]) < 3:
            existing = json.loads(s2_final.read_text(encoding="utf-8"))
            existing["comp1_10m_shift_diagnosis"] = cache["comp1"]
            write_json(s2_final, existing)
            print("=== Stage 2 (comp1) updated comp1_10m_shift_diagnosis -> results/stage2_hybrid_train_editability.json ===", flush=True)
        else:
            write_json(partial_p, cache)

    if len(cache["architectures"]) == 3 and cache["comp1"] is not None:
        all_steps = [r for k in ("Arch7d_2M", "Arch2_Hybrid_2M", "GQA_Full_2M") for r in cache["steps"][k]]
        all_le = [r for k in ("Arch7d_2M", "Arch2_Hybrid_2M", "GQA_Full_2M") for r in cache["layer_edit"][k]]
        write_json(RESULTS_DIR / "stage2_hybrid_train_editability.json", {
            "schema": "bcir.wsp_ssp.stage2_hybrid_train_editability.v1",
            "provenance": get_host_provenance(),
            "train_config": {"steps": total_steps, "batch": B, "seq_len": T, "tokens": total_steps * B * T,
                             "optimizer": "Opt_Q8_Muon_BF16Grad", "lr_max": 4e-3, "lr_min": 4e-4},
            "architectures": cache["architectures"],
            "layer_editability_table": all_le,
            "comp1_10m_shift_diagnosis": cache["comp1"],
        })
        write_csv(RESULTS_DIR / "stage2_train_steps.csv", all_steps)
        write_csv(RESULTS_DIR / "stage2_layer_editability.csv", all_le)
        if partial_p.exists():
            partial_p.unlink()
        print("=== Stage 2 ALL PARTS FINALIZED -> results/stage2_hybrid_train_editability.json ===", flush=True)
    print(f"=== Stage 2 ({args.only}) finished in {time.monotonic() - t0_all:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
