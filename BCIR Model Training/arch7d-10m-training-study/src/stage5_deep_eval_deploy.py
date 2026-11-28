#!/usr/bin/env python3
"""Stage 5: Deep Evaluation, T=64..2048 Zero-Shot Context Extrapolation, Per-Position Byte CE,
BCIRQ8 v1 & COMP-1 (PTQ + Native STE-QAT) Deployment Qualification, Qualitative Byte Completions,
and Spectral/mHC/Canon Evolution Summary for BCIR-Arch7d-10M."""
from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from common_10m import (
    CKPT_DIR,
    OPT_ADAFACTOR_M2,
    OPT_FP32,
    RESULTS_DIR,
    Abi6Model,
    apply_comp1_ptq_weights,
    comp1_weight_bytes,
    cosine_lr_schedule,
    dual_svid_1bit_factorize,
    dual_svid_1bit_reconstruct,
    eval_per_position_ce,
    generate_bytes,
    host_info,
    load_bcirq8_ckpt,
    load_ckpt_verified,
    load_wikitext_bytes,
    make_fixed_eval_batches,
    make_fixed_span_partition_batches,
    make_fixed_target_context_batches,
    make_policy,
    make_spec,
    process_rss_mib,
    q8_weight_bytes,
    quantize_bcirq8_per_tensor,
    sample_lm_steps,
    save_json,
)


def build_spec(sd: Dict[str, Any], *, batch: int, seq_len: int, threads: int = 2):
    return make_spec(
        vocab=sd["vocab"], width=sd["width"], heads=sd["heads"], kvheads=sd["kvheads"],
        layers=sd["layers"], ff=sd["ff"], batch=batch, time_len=seq_len, tied=sd["tied"],
        layer_mixers=sd["layer_mixer"], layer_use_rope=sd["layer_use_rope"],
        swa_window=sd["swa_window"], delta_chunk=sd["delta_chunk"],
        mla_dc=sd["mla_dc"], mla_dr=sd["mla_dr"], init_policy=sd["init_policy"],
        mup_d_base=sd["mup_d_base"], seed=sd["seed"], threads=threads, skip_init=1,
        gate_mode=sd["gate_mode"], qk_norm=sd["qk_norm"], canon_mode=sd["canon_mode"],
        mhc_streams=sd["mhc_streams"], online_softmax=1, gate_rank=sd["gate_rank"],
    )


def run_comp1_factor_qat(
    sd: Dict[str, Any],
    base_w: np.ndarray,
    tr_stream: np.ndarray,
    val_tk: np.ndarray,
    val_tg: np.ndarray,
    test_tk: np.ndarray,
    test_tg: np.ndarray,
    *,
    rank: int = 16,
    scale_bits: int = 16,
    steps: int = 32,
    eval_every: int = 8,
) -> Dict[str, Any]:
    """Exact differentiable COMP-1 Scale-Factor QAT on the 10M model:
    freezes all non-MLP tensors at deterministic BCIRQ8 v1 and freezes the Dual-SVID + Joint-ITQ
    binary sign factors (B, Bu, Bv), optimizing continuous scale factors (alpha_r, alpha_c, h_u,
    g_v, lam) directly via exact chain-rule gradients from BCIR's C backward pass with FP16-rounded
    forward reconstruction at every step."""
    B, T = 4, 256
    sp = build_spec(sd, batch=B, seq_len=T, threads=2)
    pol = make_policy(opt_kind=OPT_FP32)
    lrs = cosine_lr_schedule(steps, 4.0e-3, 4.0e-4, 4)

    t0 = time.perf_counter()
    with Abi6Model(sp, pol) as model:
        tinfos = model.tensor_infos()
        mlp_spans = [
            (ti["offset"], ti["rows"], ti["cols"])
            for ti in tinfos
            if ti["region"] == 2 and min(ti["rows"], ti["cols"]) >= 512
        ]
        q8_frozen, _ = quantize_bcirq8_per_tensor(model, base_w)
        factors: List[Dict[str, Any]] = []
        adam_state: List[Dict[str, Tuple[np.ndarray, np.ndarray]]] = []
        for off, r, c in mlp_spans:
            mat = base_w[off : off + r * c].reshape(r, c)
            fac = dual_svid_1bit_factorize(mat, rank=rank, itq_iters=25, scale_bits=scale_bits)
            factors.append(fac)
            st = {}
            for key in ("alpha_r", "alpha_c", "h_u", "g_v", "lam"):
                st[key] = (np.zeros_like(fac[key]), np.zeros_like(fac[key]))
            adam_state.append(st)

        def materialize_deploy() -> np.ndarray:
            dep = q8_frozen.copy()
            for (off, r, c), fac in zip(mlp_spans, factors):
                dep[off : off + r * c] = dual_svid_1bit_reconstruct(fac, round_scales=True).reshape(-1)
            return dep

        deploy_w = materialize_deploy()
        model.set_weights(deploy_w)
        v_init = model.evaluate(val_tk, val_tg)
        t_init = model.evaluate(test_tk, test_tg)

        best_val = v_init["ce"]
        best_deploy = deploy_w.copy()
        curve = [{"step": 0, "val_ce": round(v_init["ce"], 6), "val_ppl": round(v_init["ppl"], 4)}]

        tr_tk, tr_tg = sample_lm_steps(tr_stream, 0, steps, B, T, base_seed=555000)
        beta1, beta2, eps = 0.9, 0.95, 1e-8
        for s in range(steps):
            model.set_weights(deploy_w)
            loss, _sup = model.loss_backward(tr_tk[s], tr_tg[s], active_time=T, accumulate=0)
            g_all = model.copy_grads_fp32()
            lr = float(lrs[s])
            t_idx = s + 1
            for (off, r, c), fac, st in zip(mlp_spans, factors, adam_state):
                G = g_all[off : off + r * c].reshape(r, c).astype(np.float64)
                B_mat = fac["B"]
                ar, ac = fac["alpha_r"], fac["alpha_c"]
                g_ar = np.sum(G * B_mat * ac[None, :], axis=1)
                g_ac = np.sum(G * B_mat * ar[:, None], axis=0)

                bu, bv = fac["bu"], fac["bv"]
                hu, gv, lm = fac["h_u"], fac["g_v"], fac["lam"]
                u_s = hu[:, None] * bu             # [m, r]
                v_s = gv[:, None] * bv             # [n, r]
                gv_proj = G @ v_s                  # [m, r]
                gu_proj = G.T @ u_s                # [n, r]
                g_lam = np.sum(gv_proj * u_s, axis=0)
                g_hu = np.sum(gv_proj * bu * lm[None, :], axis=1)
                g_gv = np.sum(gu_proj * bv * lm[None, :], axis=1)

                grads = {
                    "alpha_r": g_ar,
                    "alpha_c": g_ac,
                    "h_u": g_hu,
                    "g_v": g_gv,
                    "lam": g_lam,
                }
                for key, gk in grads.items():
                    gn = float(np.linalg.norm(gk))
                    if gn > 3.0:
                        gk = gk * (3.0 / gn)
                    m1, m2 = st[key]
                    m1[:] = beta1 * m1 + (1.0 - beta1) * gk
                    m2[:] = beta2 * m2 + (1.0 - beta2) * (gk * gk)
                    m1h = m1 / (1.0 - beta1 ** t_idx)
                    m2h = m2 / (1.0 - beta2 ** t_idx)
                    fac[key] -= lr * (m1h / (np.sqrt(m2h) + eps))

            deploy_w = materialize_deploy()
            if (s + 1) % eval_every == 0 or s == steps - 1:
                model.set_weights(deploy_w)
                vv = model.evaluate(val_tk, val_tg)
                curve.append({
                    "step": s + 1,
                    "train_loss": round(loss, 5),
                    "val_ce": round(vv["ce"], 6),
                    "val_ppl": round(vv["ppl"], 4),
                })
                if vv["ce"] < best_val:
                    best_val = vv["ce"]
                    best_deploy = deploy_w.copy()
                print(
                    f"[Stage 5 COMP-1 Factor-QAT] st={s+1:2d}/{steps} | tr={loss:.4f} | "
                    f"val_ce={vv['ce']:.4f} (PPL {vv['ppl']:5.2f}, delta vs PTQ {vv['ce']-v_init['ce']:+.4f})",
                    flush=True,
                )

        model.set_weights(best_deploy)
        t_final = model.evaluate(test_tk, test_tg)
        c1_info = comp1_weight_bytes(model, rank=rank, scale_bits=scale_bits)

    wall = time.perf_counter() - t0
    return {
        "method": "exact_differentiable_scale_factor_qat_frozen_bcirq8_attn",
        "rank": rank,
        "scale_bits": scale_bits,
        "mlp_comp1_bpw": c1_info["mlp_comp1_bpw"],
        "weights_mib": round(c1_info["total_bytes"] / 1048576.0, 2),
        "steps": steps,
        "tokens": steps * B * T,
        "wall_s": round(wall, 2),
        "ptq_test_ce": round(t_init["ce"], 6),
        "ptq_test_ppl": round(t_init["ppl"], 4),
        "qat_test_ce": round(t_final["ce"], 6),
        "qat_test_ppl": round(t_final["ppl"], 4),
        "qat_test_bpb": round(t_final["bpb"], 5),
        "qat_recovery_nats": round(t_final["ce"] - t_init["ce"], 6),
        "curve": curve,
    }


def run_fixed_target_extrapolation(
    sd: Dict[str, Any],
    weights: np.ndarray,
    te_stream: np.ndarray,
    *,
    weights_source: str,
    horizons: Tuple[int, ...] = (64, 128, 256, 512, 1024, 2048),
) -> Dict[str, Any]:
    """Evaluate zero-shot context extrapolation across `horizons` while holding target bytes
    STRICTLY IDENTICAL across every horizon (verified by SHA-256).

    Protocol A (Fixed Target Window): 32 evenly spaced test anchors, scoring strictly the
      identical trailing W=64 bytes (2,048 total target bytes) while varying preceding context
      T in {64, 128, 256, 512, 1024, 2048}.
    Protocol B (Fixed Contiguous Span Partition): 8 disjoint 2048-byte test spans (16,384
      identical target bytes) partitioned into non-overlapping T-byte blocks for every T.
    """
    win_batches, win_sha, win_anchors = make_fixed_target_context_batches(
        te_stream, horizons=horizons, target_window=64, n_windows=32, max_context=max(horizons)
    )
    span_batches, span_sha, span_starts = make_fixed_span_partition_batches(
        te_stream, horizons=horizons, span_len=2048, n_spans=8
    )

    fixed_window_rows: List[Dict[str, Any]] = []
    fixed_span_rows: List[Dict[str, Any]] = []
    pos_ce_512: Dict[str, Any] = {}
    pos_ce_1024: Dict[str, Any] = {}
    pos_ce_2048: Dict[str, Any] = {}

    for T in horizons:
        B = 8 if T <= 256 else (4 if T == 512 else 1)
        sp = build_spec(sd, batch=B, seq_len=T, threads=2)
        tk_w, tg_w = win_batches[T]
        tk_s, tg_s = span_batches[T]
        assert tk_w.shape[0] % B == 0 and tk_s.shape[0] % B == 0
        tk_w_b = tk_w.reshape(-1, B, T)
        tg_w_b = tg_w.reshape(-1, B, T)
        tk_s_b = tk_s.reshape(-1, B, T)
        tg_s_b = tg_s.reshape(-1, B, T)

        with Abi6Model(sp, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m:
            m.set_weights(weights)
            ms = m.mem_stats()
            kv_t_mib = (ms["infer_kv_bytes_per_tok"] * T) / 1048576.0
            rec_mib = ms["infer_rec_state_bytes"] / 1048576.0

            # Protocol A: Fixed-target-window (identical 2,048 trailing target bytes)
            m.reset_telemetry()
            ev_w = m.evaluate(tk_w_b, tg_w_b)
            tel_w = m.telemetry()
            assert ev_w["tokens"] == 32 * 64, f"Expected 2048 fixed target bytes, got {ev_w['tokens']}"

            # Protocol B: Fixed contiguous span partition (identical 16,384 target bytes)
            m.reset_telemetry()
            ev_s = m.evaluate(tk_s_b, tg_s_b)
            tel_s = m.telemetry()
            assert ev_s["tokens"] == 8 * 2048, f"Expected 16384 fixed span bytes, got {ev_s['tokens']}"

            if T == 512:
                pos_ce_512 = eval_per_position_ce(m, tk_s_b, tg_s_b)
            elif T == 1024:
                pos_ce_1024 = eval_per_position_ce(m, tk_s_b, tg_s_b)
            elif T == 2048:
                pos_ce_2048 = eval_per_position_ce(m, tk_s_b, tg_s_b)

        regime = "in_dist" if T <= 512 else f"zeroshot_{T // 512}x_extrap"
        row_w = {
            "seq_len": T,
            "preceding_context_bytes": T - 64,
            "regime": regime,
            "protocol": "fixed_target_window_w64_n32",
            "target_bytes_sha256": win_sha,
            "tokens_evaluated": ev_w["tokens"],
            "test_ce": round(ev_w["ce"], 6),
            "test_ppl": round(ev_w["ppl"], 4),
            "test_bpb": round(ev_w["bpb"], 5),
            "test_acc": round(ev_w["accuracy"], 5),
            "eval_ms": round(ev_w["eval_ms"], 1),
            "max_abs_act": round(tel_w["max_abs_act"], 3),
            "first_tok_attn_mean": round(tel_w["first_tok_attn_mean"], 4),
            "infer_state_mib_at_T": round(kv_t_mib + rec_mib, 3),
            "eval_arena_mib": round(ms["total_bytes"] / 1048576.0, 2),
        }
        row_s = {
            "seq_len": T,
            "regime": regime,
            "protocol": "fixed_span_partition_2048x8",
            "target_bytes_sha256": span_sha,
            "tokens_evaluated": ev_s["tokens"],
            "test_ce": round(ev_s["ce"], 6),
            "test_ppl": round(ev_s["ppl"], 4),
            "test_bpb": round(ev_s["bpb"], 5),
            "test_acc": round(ev_s["accuracy"], 5),
            "eval_ms": round(ev_s["eval_ms"], 1),
            "eval_tok_per_s": round(ev_s["tokens"] / max(ev_s["eval_ms"] / 1000.0, 1e-6), 1),
            "max_abs_act": round(tel_s["max_abs_act"], 3),
            "first_tok_attn_mean": round(tel_s["first_tok_attn_mean"], 4),
            "infer_state_mib_at_T": round(kv_t_mib + rec_mib, 3),
            "eval_arena_mib": round(ms["total_bytes"] / 1048576.0, 2),
        }
        fixed_window_rows.append(row_w)
        fixed_span_rows.append(row_s)
        print(
            f"[Stage 5 Fixed-Target] T={T:4d} ({regime:18s}) | "
            f"Win64(2kB): CE={row_w['test_ce']:.4f} PPL={row_w['test_ppl']:5.2f} acc={row_w['test_acc']*100:4.1f}% | "
            f"Span16kB: CE={row_s['test_ce']:.4f} PPL={row_s['test_ppl']:5.2f} acc={row_s['test_acc']*100:4.1f}% | "
            f"act={row_s['max_abs_act']:4.2f} sink={row_s['first_tok_attn_mean']:.3f}",
            flush=True,
        )

    return {
        "weights_source": weights_source,
        "fixed_target_window_meta": {
            "n_windows": 32,
            "target_window_bytes": 64,
            "scored_target_bytes": 2048,
            "target_bytes_sha256": win_sha,
            "anchor_ends_first_last": [win_anchors[0], win_anchors[-1]],
        },
        "fixed_span_partition_meta": {
            "n_spans": 8,
            "span_len_bytes": 2048,
            "scored_target_bytes": 16384,
            "target_bytes_sha256": span_sha,
            "span_starts_first_last": [span_starts[0], span_starts[-1]],
        },
        "fixed_target_window_rows": fixed_window_rows,
        "fixed_span_partition_rows": fixed_span_rows,
        "per_position_ce_t512": pos_ce_512,
        "per_position_ce_t1024": pos_ce_1024,
        "per_position_ce_t2048": pos_ce_2048,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qat-only", action="store_true", help="Run only exact differentiable COMP-1 Factor-QAT and update stage5 JSON")
    ap.add_argument(
        "--fixed-target-extrap-only",
        action="store_true",
        help="Run only fixed-target-span context extrapolation (T=64..2048) and update stage5 JSON",
    )
    args = ap.parse_args()

    s1 = json.loads((RESULTS_DIR / "stage1_warmup_t128.json").read_text(encoding="utf-8"))
    s2 = json.loads((RESULTS_DIR / "stage2_core_t256.json").read_text(encoding="utf-8"))
    s3 = json.loads((RESULTS_DIR / "stage3_longctx_t512.json").read_text(encoding="utf-8"))
    sd = s3["spec"]

    tr_stream = load_wikitext_bytes("train")
    va_stream = load_wikitext_bytes("validation")
    te_stream = load_wikitext_bytes("test")

    # Prefer Stage 3 FP32 best weights when present; fall back to tracked deterministic BCIRQ8 v1 (+0.0001 nats)
    stage3_fp32_json = CKPT_DIR / "stage3_end.json"
    if stage3_fp32_json.exists():
        _w_end, _meta3, _opt3, w_best = load_ckpt_verified(CKPT_DIR / "stage3_end")
        assert w_best is not None
        weights_source = "stage3_end_fp32_best"
    else:
        sp_ld = build_spec(sd, batch=1, seq_len=64, threads=2)
        with Abi6Model(sp_ld, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m_ld:
            w_best, q8_meta = load_bcirq8_ckpt(CKPT_DIR / "arch7d_10m_stage3_best_bcirq8", m_ld)
        weights_source = f"arch7d_10m_stage3_best_bcirq8 (sha256={q8_meta['weights_sha256'][:12]})"

    if args.fixed_target_extrap_only:
        out_p = RESULTS_DIR / "stage5_deep_eval_and_deploy.json"
        payload = json.loads(out_p.read_text(encoding="utf-8"))
        ext = run_fixed_target_extrapolation(sd, w_best, te_stream, weights_source=weights_source)
        payload["context_extrapolation_protocol"] = {
            "weights_source": ext["weights_source"],
            "fixed_target_window": ext["fixed_target_window_meta"],
            "fixed_span_partition": ext["fixed_span_partition_meta"],
        }
        payload["context_extrapolation_t64_to_t2048"] = ext["fixed_target_window_rows"]
        payload["context_extrapolation_fixed_span_16kb"] = ext["fixed_span_partition_rows"]
        payload["per_position_ce_t512"] = ext["per_position_ce_t512"]
        payload["per_position_ce_t1024"] = ext["per_position_ce_t1024"]
        payload["per_position_ce_t2048"] = ext["per_position_ce_t2048"]
        save_json(out_p, payload)
        print(f"[Stage 5 Fixed-Target Extrap DONE] Updated {out_p}", flush=True)
        return

    if args.qat_only:
        out_p = RESULTS_DIR / "stage5_deep_eval_and_deploy.json"
        payload = json.loads(out_p.read_text(encoding="utf-8"))
        test_tk_256, test_tg_256 = make_fixed_eval_batches(te_stream, 8, 4, 256)
        val_tk_256, val_tg_256 = make_fixed_eval_batches(va_stream, 6, 4, 256)
        sp256 = build_spec(sd, batch=4, seq_len=256, threads=2)
        with Abi6Model(sp256, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m:
            ms = m.mem_stats()
        st4k = (ms["infer_kv_bytes_per_tok"] * 4096 + ms["infer_rec_state_bytes"]) / 1048576.0
        fqat = run_comp1_factor_qat(
            sd, w_best, tr_stream, val_tk_256, val_tg_256, test_tk_256, test_tg_256,
            rank=16, scale_bits=16, steps=32, eval_every=8,
        )
        fp32_ce = payload["deployment_policy_qualification"][0]["test_ce"]
        fqat["delta_ce_vs_fp32"] = round(fqat["qat_test_ce"] - fp32_ce, 6)
        fqat["total_4k_mib"] = round(fqat["weights_mib"] + st4k, 2)
        payload["comp1_ste_qat_recovery"] = fqat
        save_json(out_p, payload)
        print(
            f"[Stage 5 Factor-QAT DONE] PTQ {fqat['ptq_test_ce']:.4f} -> QAT {fqat['qat_test_ce']:.4f} "
            f"(recovered {fqat['qat_recovery_nats']:+.4f} nats; delta vs FP32 {fqat['delta_ce_vs_fp32']:+.4f}) -> {out_p}",
            flush=True,
        )
        return

    # 1. Fixed-target context-length scaling & zero-shot extrapolation across T=64..2048
    ext = run_fixed_target_extrapolation(sd, w_best, te_stream, weights_source=weights_source)
    extrap_rows = ext["fixed_target_window_rows"]
    extrap_span_rows = ext["fixed_span_partition_rows"]
    pos_ce_512 = ext["per_position_ce_t512"]
    pos_ce_1024 = ext["per_position_ce_t1024"]
    pos_ce_2048 = ext["per_position_ce_t2048"]

    # 2. Deployment Policy (P1-P6) sweep: FP32 vs BCIRQ8 v1 vs COMP-1 (r=16/32 x scale=16/32)
    test_tk_256, test_tg_256 = make_fixed_eval_batches(te_stream, 8, 4, 256)
    val_tk_256, val_tg_256 = make_fixed_eval_batches(va_stream, 6, 4, 256)
    sp256 = build_spec(sd, batch=4, seq_len=256, threads=2)
    deploy_rows: List[Dict[str, Any]] = []
    with Abi6Model(sp256, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m:
        ms = m.mem_stats()
        kv4k = (ms["infer_kv_bytes_per_tok"] * 4096) / 1048576.0
        rec_m = ms["infer_rec_state_bytes"] / 1048576.0
        st4k = kv4k + rec_m

        m.set_weights(w_best)
        ev_fp32 = m.evaluate(test_tk_256, test_tg_256)
        deploy_rows.append({
            "policy": "FP32_Ref",
            "weights_mib": round(ms["weight_bytes"] / 1048576.0, 2),
            "mlp_bpw": 32.0,
            "compression_vs_fp32_x": 1.0,
            "total_4k_mib": round(ms["weight_bytes"] / 1048576.0 + st4k, 2),
            "test_ce": round(ev_fp32["ce"], 6),
            "delta_ce": 0.0,
            "test_ppl": round(ev_fp32["ppl"], 4),
            "test_bpb": round(ev_fp32["bpb"], 5),
            "test_acc": round(ev_fp32["accuracy"], 5),
        })

        # Verify tracked BCIRQ8 v1 checkpoint reload directly
        q8_reloaded, q8_meta = load_bcirq8_ckpt(CKPT_DIR / "arch7d_10m_stage3_best_bcirq8", m)
        m.set_weights(q8_reloaded)
        ev_q8 = m.evaluate(test_tk_256, test_tg_256)
        q8_mib = q8_weight_bytes(m) / 1048576.0
        assert q8_meta["weights_bytes"] == q8_weight_bytes(m)
        deploy_rows.append({
            "policy": "BCIRQ8_v1_g32",
            "weights_mib": round(q8_mib, 2),
            "mlp_bpw": 8.25,
            "compression_vs_fp32_x": round((ms["weight_bytes"] / 1048576.0) / q8_mib, 2),
            "total_4k_mib": round(q8_mib + st4k, 2),
            "test_ce": round(ev_q8["ce"], 6),
            "delta_ce": round(ev_q8["ce"] - ev_fp32["ce"], 6),
            "test_ppl": round(ev_q8["ppl"], 4),
            "test_bpb": round(ev_q8["bpb"], 5),
            "test_acc": round(ev_q8["accuracy"], 5),
            "checkpoint_sha256": q8_meta["weights_sha256"],
        })

        for rank, sbits in [(16, 16), (16, 32), (32, 16), (32, 32)]:
            c1_w = apply_comp1_ptq_weights(m, w_best, rank=rank, scale_bits=sbits)
            m.set_weights(c1_w)
            ev_c1 = m.evaluate(test_tk_256, test_tg_256)
            c1_info = comp1_weight_bytes(m, rank=rank, scale_bits=sbits)
            w_mib = c1_info["total_bytes"] / 1048576.0
            row_c1 = {
                "policy": f"COMP1_PTQ_r{rank}_fp{sbits}scale",
                "rank": rank,
                "scale_bits": sbits,
                "weights_mib": round(w_mib, 2),
                "mlp_bpw": c1_info["mlp_comp1_bpw"],
                "compression_vs_fp32_x": round((ms["weight_bytes"] / 1048576.0) / w_mib, 2),
                "compression_vs_q8_x": round(q8_mib / w_mib, 2),
                "total_4k_mib": round(w_mib + st4k, 2),
                "test_ce": round(ev_c1["ce"], 6),
                "delta_ce": round(ev_c1["ce"] - ev_fp32["ce"], 6),
                "test_ppl": round(ev_c1["ppl"], 4),
                "test_bpb": round(ev_c1["bpb"], 5),
                "test_acc": round(ev_c1["accuracy"], 5),
            }
            deploy_rows.append(row_c1)
            print(
                f"[Stage 5 Deploy] {row_c1['policy']:24s} | {row_c1['weights_mib']:5.2f}M "
                f"({row_c1['compression_vs_fp32_x']:.2f}x vs FP32, MLP {row_c1['mlp_bpw']:.4f} bpw) | "
                f"4k={row_c1['total_4k_mib']:5.2f}M | CE={row_c1['test_ce']:.4f} ({row_c1['delta_ce']:+.4f})",
                flush=True,
            )

    # 3. Exact Differentiable Scale-Factor COMP-1 QAT recovery (32 steps, rank=16, fp16 scales, frozen BCIRQ8 attention)
    ste_qat = run_comp1_factor_qat(
        sd, w_best, tr_stream, val_tk_256, val_tg_256, test_tk_256, test_tg_256,
        rank=16, scale_bits=16, steps=32, eval_every=8,
    )
    ste_qat["delta_ce_vs_fp32"] = round(ste_qat["qat_test_ce"] - deploy_rows[0]["test_ce"], 6)
    ste_qat["total_4k_mib"] = round(ste_qat["weights_mib"] + st4k, 2)
    print(
        f"[Stage 5 Factor-QAT DONE] PTQ {ste_qat['ptq_test_ce']:.4f} -> QAT {ste_qat['qat_test_ce']:.4f} "
        f"(recovered {ste_qat['qat_recovery_nats']:+.4f} nats; delta vs FP32 {ste_qat['delta_ce_vs_fp32']:+.4f})",
        flush=True,
    )

    # 4. Qualitative raw-byte autoregressive completions from real WikiText-2 test prefixes
    sp_gen = build_spec(sd, batch=1, seq_len=512, threads=2)
    prompts = [
        b" = Robert Boulter = \n \n Robert Boulter is an English film , television and theatre actor . He ",
        b" In the early 20th century , the city experienced rapid growth due to ",
        b" The Battle of Gettysburg was fought in July 1863 during the American Civil War , and ",
    ]
    generations = []
    with Abi6Model(sp_gen, make_policy(opt_kind=OPT_ADAFACTOR_M2)) as m_gen:
        m_gen.set_weights(w_best)
        for idx_p, pr in enumerate(prompts):
            g_greedy = generate_bytes(m_gen, pr, max_new=96, temperature=0.0, seed=100 + idx_p)
            g_samp = generate_bytes(m_gen, pr, max_new=96, temperature=0.65, seed=200 + idx_p)
            generations.append({
                "prompt": pr.decode("utf-8"),
                "greedy_continuation": g_greedy[len(pr.decode("utf-8")) :],
                "temp065_continuation": g_samp[len(pr.decode("utf-8")) :],
            })

    # 5. Spectral, Canon-AC & mHC evolution trajectory (Init -> Stage 1 -> Stage 2 -> Stage 3)
    spectral_evolution = []
    for label, fdict, toks in [
        ("Init (0 tok)", s1["init_forensics"], 0),
        ("Stage 1 End (197k tok)", s1["end_forensics"], s1["tokens_cumulative"]),
        ("Stage 2 End (852k tok)", s2["end_forensics"], s2["tokens_cumulative"]),
        ("Stage 3 End (1.114M tok)", s3["end_forensics"], s3["tokens_cumulative"]),
    ]:
        if fdict is None:
            continue
        spectral_evolution.append({
            "checkpoint": label,
            "tokens": toks,
            "total_frob_norm": fdict["total_frob_norm"],
            "mlp_mean_r90_frac": fdict["mlp_mean_r90_frac"],
            "mlp_mean_stable_rank": fdict["mlp_mean_stable_rank"],
            "attn_mean_r90_frac": fdict["attn_mean_r90_frac"],
            "canon_kernels": fdict["canon_kernels"],
            "mhc_summary": fdict["mhc_summary"],
        })

    # 6. Calibrated multi-core Tier M (10M tok) & Chinchilla (200M tok) projections
    tok_s_2core_t256 = s2["throughput_tok_per_s"]
    tok_s_2core_t512 = s3["throughput_tok_per_s"]
    projections = {
        "measured_2core_avx2_t256_tok_per_s": tok_s_2core_t256,
        "measured_2core_avx2_t512_tok_per_s": tok_s_2core_t512,
        "tier_m_10m_tokens_2core_avx2_hrs": round((10_000_000.0 / tok_s_2core_t256) / 3600.0, 2),
        "tier_m_10m_tokens_16core_avx2_hrs_est": round((10_000_000.0 / (tok_s_2core_t256 * 6.5)) / 3600.0, 2),
        "tier_m_10m_tokens_32core_avx512_hrs_est": round((10_000_000.0 / (tok_s_2core_t256 * 18.0)) / 3600.0, 2),
        "chinchilla_200m_tokens_32core_avx512_hrs_est": round((200_000_000.0 / (tok_s_2core_t256 * 18.0)) / 3600.0, 1),
        "note": "16-core AVX2 (6.5x) and 32-core AVX-512 (18x) estimates are analytical projections from measured 2-core AVX2 throughput; only 1.114M tokens were executed locally.",
    }

    payload = {
        "stage": "Stage 5 — Deep Evaluation, T=64..2048 Extrapolation, Deployment Qualification & Forensics",
        "arch_id": s3["arch_id"],
        "tokens_trained": s3["tokens_cumulative"],
        "host": host_info(),
        "rss": process_rss_mib(),
        "context_extrapolation_protocol": {
            "weights_source": ext["weights_source"],
            "fixed_target_window": ext["fixed_target_window_meta"],
            "fixed_span_partition": ext["fixed_span_partition_meta"],
        },
        "context_extrapolation_t64_to_t2048": extrap_rows,
        "context_extrapolation_fixed_span_16kb": extrap_span_rows,
        "per_position_ce_t512": pos_ce_512,
        "per_position_ce_t1024": pos_ce_1024,
        "per_position_ce_t2048": pos_ce_2048,
        "deployment_policy_qualification": deploy_rows,
        "comp1_ste_qat_recovery": ste_qat,
        "spectral_and_structural_evolution": spectral_evolution,
        "qualitative_byte_generations": generations,
        "tier_m_projections": projections,
    }
    out_p = RESULTS_DIR / "stage5_deep_eval_and_deploy.json"
    save_json(out_p, payload)
    print(f"[Stage 5 DONE] Saved {out_p}")


if __name__ == "__main__":
    main()
