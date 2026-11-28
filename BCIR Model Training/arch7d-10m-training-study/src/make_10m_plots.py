#!/usr/bin/env python3
"""Generate 5 publication-quality multi-panel figures for the BCIR-Arch7d-10M study."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STUDY_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = STUDY_DIR / "results"
FIG_DIR = STUDY_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)


def _read_csv(path: Path) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    with path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: float(v) if k not in ("control", "optimizer") else v for k, v in r.items()})
    return rows


def plot_fig1_curriculum() -> None:
    s1 = json.loads((RESULTS_DIR / "stage1_warmup_t128.json").read_text(encoding="utf-8"))
    s2 = json.loads((RESULTS_DIR / "stage2_core_t256.json").read_text(encoding="utf-8"))
    s3 = json.loads((RESULTS_DIR / "stage3_longctx_t512.json").read_text(encoding="utf-8"))
    steps = (
        _read_csv(RESULTS_DIR / "stage1_steps.csv")
        + _read_csv(RESULTS_DIR / "stage2_steps.csv")
        + _read_csv(RESULTS_DIR / "stage3_steps.csv")
    )
    toks_m = np.array([r["tokens_cum"] / 1e6 for r in steps])
    losses = np.array([r["loss"] for r in steps])
    gsteps = np.array([r["global_step"] for r in steps])
    lrs = np.array([r["lr"] for r in steps])
    gnorms = np.array([r["grad_norm"] for r in steps])
    tok_s = np.array([r["tok_per_s"] for r in steps])

    b1 = s1["tokens_cumulative"] / 1e6
    b2 = s2["tokens_cumulative"] / 1e6

    fig, axes = plt.subplots(2, 2, figsize=(15.5, 10.2), dpi=150)

    # Panel A: Training loss + Stage validation CE
    ax = axes[0, 0]
    ax.plot(toks_m, losses, color="#4C78A8", alpha=0.30, linewidth=0.9, label="Per-step Train Loss")
    win = 11
    sm = np.convolve(losses, np.ones(win) / win, mode="valid")
    ax.plot(toks_m[win - 1 :], sm, color="#4C78A8", linewidth=2.0, label="Train Loss (11-step MA)")
    for st_obj, col, lbl in [(s1, "#54A24B", "Stage 1 Val (T=128)"), (s2, "#F58518", "Stage 2 Val (T=256)"), (s3, "#E45756", "Stage 3 Val (T=512)")]:
        pts = [c for c in st_obj["curve"] if c["local_step"] > 0]
        ax.plot([c["tokens_cum"] / 1e6 for c in pts], [c["val_ce"] for c in pts], "o-", color=col, linewidth=2.2, markersize=5.5, label=lbl)
    ax.axvline(b1, color="#555555", linestyle="--", linewidth=1.1, alpha=0.7)
    ax.axvline(b2, color="#555555", linestyle="--", linewidth=1.1, alpha=0.7)
    ax.text(b1 * 0.5, 3.55, "Stage 1\nT=128", ha="center", fontsize=8.5, fontweight="bold")
    ax.text((b1 + b2) * 0.5, 3.55, "Stage 2\nT=256", ha="center", fontsize=8.5, fontweight="bold")
    ax.text((b2 + toks_m[-1]) * 0.5, 3.05, "Stage 3\nT=512", ha="center", fontsize=8.5, fontweight="bold")
    ax.set_xlabel("Cumulative Real Training Tokens (Millions)")
    ax.set_ylabel("Cross-Entropy Loss (nats/byte)")
    ax.set_title("A. 10M Progressive Curriculum Loss & Validation Curve")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.25)

    # Panel B: Global LR & Grad Norm
    ax = axes[0, 1]
    ax.plot(gsteps, gnorms, color="#B279A2", alpha=0.75, linewidth=1.2, label="Global Grad L2 Norm")
    ax.axhline(3.0, color="#E45756", linestyle="--", linewidth=1.4, label="Grad Clip Threshold (3.0)")
    ax.set_xlabel("Global Optimizer Step (0-Bit-Drift Cross-Stage Opt_Q8_Muon)")
    ax.set_ylabel("Gradient Norm", color="#B279A2")
    ax.tick_params(axis="y", labelcolor="#B279A2")
    ax_r = ax.twinx()
    ax_r.plot(gsteps, lrs * 1e3, color="#4C78A8", linewidth=2.0, label="Learning Rate (x1e-3)")
    ax_r.set_ylabel("Learning Rate (x 1e-3)", color="#4C78A8")
    ax_r.tick_params(axis="y", labelcolor="#4C78A8")
    ax.axvline(192, color="#555555", linestyle=":", linewidth=1.1)
    ax.axvline(512, color="#555555", linestyle=":", linewidth=1.1)
    ax.set_title("B. Cosine LR Schedule, Grad Norm & Clipping Telemetry")
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax_r.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.25)

    # Panel C: Activation Outliers, Attention Sink & Throughput
    ax = axes[1, 0]
    c_all = [c for st in (s1, s2, s3) for c in st["curve"] if "max_abs_act" in c]
    ctoks = [c["tokens_cum"] / 1e6 for c in c_all]
    acts = [c["max_abs_act"] for c in c_all]
    sinks = [c["first_tok_attn_mean"] for c in c_all]
    ax.plot(ctoks, acts, "o-", color="#E45756", linewidth=2.0, label="Max |Activation|")
    ax.set_xlabel("Cumulative Training Tokens (Millions)")
    ax.set_ylabel("Max |Activation|", color="#E45756")
    ax.tick_params(axis="y", labelcolor="#E45756")
    ax_r2 = ax.twinx()
    ax_r2.plot(ctoks, sinks, "s--", color="#4C78A8", linewidth=1.8, label="First-Tok Attn Sink")
    ax_r2.set_ylabel("First-Token Attention Mean (Sink)", color="#4C78A8")
    ax_r2.tick_params(axis="y", labelcolor="#4C78A8")
    ax.axvline(b1, color="#555555", linestyle=":", linewidth=1.1)
    ax.axvline(b2, color="#555555", linestyle=":", linewidth=1.1)
    ax.set_title("C. Activation Outlier & Attention-Sink Stability Across Stages")
    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax_r2.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.25)

    # Panel D: Untouched Test PPL across stages and evaluation horizons
    ax = axes[1, 1]
    stage_lbls = ["Stage 1\n(197k tok, T=128)", "Stage 2\n(852k tok, T=256)", "Stage 3\n(1.114M tok, T=512)"]
    x = np.arange(3)
    w = 0.25
    for idx_h, (hkey, col, hlbl) in enumerate([("T128", "#4C78A8", "Test T=128"), ("T256", "#54A24B", "Test T=256"), ("T512", "#F58518", "Test T=512")]):
        vals = [st["test_multi_horizon_best"][hkey]["ppl"] for st in (s1, s2, s3)]
        bars = ax.bar(x + (idx_h - 1) * w, vals, width=w, color=col, edgecolor="black", linewidth=0.6, label=hlbl)
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height() + 0.12, f"{val:.2f}", ha="center", va="bottom", fontsize=8, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(stage_lbls, fontsize=8.5)
    ax.set_ylabel("Untouched WikiText-2 Test PPL (lower is better)")
    ax.set_title("D. Multi-Horizon Untouched Test PPL Across Curriculum Stages")
    ax.set_ylim(0, max(s1["test_multi_horizon_best"]["T128"]["ppl"], s1["test_multi_horizon_best"]["T512"]["ppl"]) * 1.18)
    ax.legend(fontsize=8.5)
    ax.grid(axis="y", alpha=0.25)

    fig.suptitle(f"BCIR-Arch7d-10M ({s3['arch_id']}, {s3['mem_stats']['n_params']/1e6:.2f}M) — 3-Stage Curriculum Training (1.114M Tokens, Opt_Q8_Muon_BF16Grad)", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = FIG_DIR / "fig1_10m_curriculum_dynamics.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_fig2_controls_pareto() -> None:
    s0 = json.loads((RESULTS_DIR / "stage0_sizing_and_ablation.json").read_text(encoding="utf-8"))
    s4 = json.loads((RESULTS_DIR / "stage4_10m_controls.json").read_text(encoding="utf-8"))
    runs = s4["runs"]

    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.2), dpi=150)
    styles = {
        "GQA_Full_10M_Adafactor": ("#4C78A8", "o-", "GQA_Full 10M (Adafactor)"),
        "Arch2_Hybrid_10M_Adafactor": ("#F58518", "s-", "Arch-2 Hybrid 10M (Adafactor)"),
        "Arch7d_10M_Adafactor": ("#54A24B", "^-", "Arch-7d 10M (Adafactor)"),
        "Arch7d_10M_Q8Muon_FlatT256": ("#B279A2", "D-", "Arch-7d 10M (Q8-Muon)"),
    }

    # Panel A: Equal-token curves
    ax = axes[0]
    for k, (col, mk, lbl) in styles.items():
        if k not in runs:
            continue
        pts = [c for c in runs[k]["curve"] if c["tokens"] > 0]
        ax.plot([c["tokens"] / 1e3 for c in pts], [c["val_ppl"] for c in pts], mk, color=col, linewidth=2.2, markersize=6, label=f"{lbl}\n(test {runs[k]['test_ppl_t256']:.2f})")
    ax.set_xlabel("Training Tokens Executed (Thousands, T=256)")
    ax.set_ylabel("Validation PPL (lower is better)")
    ax.set_title("A. Equal-Token 10M Comparison (196.6k tok)")
    ax.legend(fontsize=7.8)
    ax.grid(True, alpha=0.25)

    # Panel B: Equal-wall-time curves
    ax = axes[1]
    for k, (col, mk, lbl) in styles.items():
        if k not in runs:
            continue
        pts = [c for c in runs[k]["curve"] if c["wall_s"] > 0]
        ax.plot([c["wall_s"] for c in pts], [c["val_ppl"] for c in pts], mk, color=col, linewidth=2.2, markersize=6, label=lbl)
    ax.set_xlabel("Wall-Clock Time on 2-Core Haswell AVX2 (seconds)")
    ax.set_ylabel("Validation PPL (lower is better)")
    ax.set_title("B. Equal-Wall-Time 10M Pareto Trajectory")
    ax.legend(fontsize=7.8)
    ax.grid(True, alpha=0.25)

    # Panel C: All-7 Optimizer RAM audit on 10M model
    ax = axes[2]
    opt_rows = s0["optimizer_memory_audit_10m"]
    lbls = [r["optimizer"].replace("Opt_", "").replace("_", "\n") for r in opt_rows]
    w_m = [r["weights_mib"] for r in opt_rows]
    o_m = [r["opt_plus_grad_mib"] for r in opt_rows]
    x = np.arange(len(opt_rows))
    ax.bar(x, w_m, color="#4C78A8", label="FP32 Master Weights")
    ax.bar(x, o_m, bottom=w_m, color="#F58518", label="Grads + Opt Moments")
    for i, r in enumerate(opt_rows):
        ax.text(i, r["persistent_mib"] + 2.5, f"{r['persistent_mib']:.0f}M\n({r['bytes_per_param']:.2f}B/p)", ha="center", va="bottom", fontsize=7.2, fontweight="bold" if "Muon_BF16" in r["optimizer"] else "normal")
    ax.set_xticks(x)
    ax.set_xticklabels(lbls, fontsize=7.0)
    ax.set_ylabel("Persistent Training RAM (MiB)")
    ax.set_title("C. 10M Persistent RAM Across All 7 Optimizers")
    ax.set_ylim(0, max(r["persistent_mib"] for r in opt_rows) * 1.22)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25)

    fig.suptitle("Stage 4: Parameter-Matched 10M Architecture & Optimizer Controls (B=8, T=256, online_softmax=1)", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = FIG_DIR / "fig2_10m_matched_controls_pareto.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_fig3_extrapolation() -> None:
    s5 = json.loads((RESULTS_DIR / "stage5_deep_eval_and_deploy.json").read_text(encoding="utf-8"))
    ext = s5["context_extrapolation_t64_to_t2048"]
    pos512 = s5["per_position_ce_t512"]
    pos1024 = s5["per_position_ce_t1024"]

    fig, axes = plt.subplots(1, 2, figsize=(15.0, 5.2), dpi=150)

    # Panel A: T=64..2048 zero-shot extrapolation
    ax = axes[0]
    ts = [r["seq_len"] for r in ext]
    ppls = [r["test_ppl"] for r in ext]
    states = [r["infer_state_mib_at_T"] for r in ext]
    ax.plot(ts, ppls, "o-", color="#54A24B", linewidth=2.4, markersize=7, label="Untouched Test PPL")
    for t, p in zip(ts, ppls):
        ax.annotate(f"{p:.2f}", (t, p), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8.5, fontweight="bold")
    ax.axvline(512, color="#E45756", linestyle="--", linewidth=1.5, label="Max Train Context (T=512)")
    ax.axvspan(512, 2200, color="#F58518", alpha=0.08, label="Zero-Shot Extrapolation (2x–4x)")
    ax.set_xscale("log", base=2)
    ax.set_xticks(ts)
    ax.set_xticklabels([str(t) for t in ts])
    ax.set_xlabel("Evaluation Sequence Length T (bytes)")
    ax.set_ylabel("Untouched WikiText-2 Test PPL (lower is better)", color="#54A24B")
    ax.tick_params(axis="y", labelcolor="#54A24B")
    ax_r = ax.twinx()
    ax_r.plot(ts, states, "s--", color="#4C78A8", linewidth=1.8, label="Inference State (MiB)")
    ax_r.set_ylabel("KV + Recurrent State (MiB)", color="#4C78A8")
    ax_r.tick_params(axis="y", labelcolor="#4C78A8")
    ax.set_title("A. Zero-Shot Context Scaling (T=64 to T=2048 = 4x Train T)")
    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax_r.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, fontsize=7.8, loc="upper right")
    ax.grid(True, alpha=0.25)

    # Panel B: Per-position byte CE curve
    ax = axes[1]
    ce512 = pos512["per_pos_ce_stride8"]
    x512 = np.arange(len(ce512)) * 8 + 4
    ax.plot(x512, ce512, "-", color="#4C78A8", linewidth=2.1, label=f"T=512 Eval (overall {pos512['overall_ce']:.3f} nats)")
    if pos1024 and pos1024.get("per_pos_ce_stride8"):
        ce1024 = pos1024["per_pos_ce_stride8"]
        x1024 = np.arange(len(ce1024)) * 8 + 4
        ax.plot(x1024, ce1024, "-", color="#F58518", linewidth=1.8, alpha=0.85, label=f"T=1024 Zero-Shot Eval (overall {pos1024['overall_ce']:.3f} nats)")
    ax.axvline(512, color="#E45756", linestyle="--", linewidth=1.3, label="Train Horizon (pos=512)")
    ax.set_xlabel("Byte Position in Sequence (8-byte bins)")
    ax.set_ylabel("Per-Position Cross-Entropy (nats/byte)")
    ax.set_title("B. In-Context Adaptation: Per-Position Byte CE Profile")
    ax.legend(fontsize=8.2)
    ax.grid(True, alpha=0.25)

    fig.suptitle("Stage 5: BCIR-Arch7d-10M Context Extrapolation & In-Context Position Profile (GDN-2 + G1-NoPE-MLA)", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = FIG_DIR / "fig3_10m_context_extrapolation_and_position_ce.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_fig4_forensics() -> None:
    s0 = json.loads((RESULTS_DIR / "stage0_sizing_and_ablation.json").read_text(encoding="utf-8"))
    s5 = json.loads((RESULTS_DIR / "stage5_deep_eval_and_deploy.json").read_text(encoding="utf-8"))
    evo = s5["spectral_and_structural_evolution"]

    fig, axes = plt.subplots(2, 2, figsize=(15.5, 10.0), dpi=150)

    # Panel A: Spectral r90 & stable rank evolution
    ax = axes[0, 0]
    lbls = [e["checkpoint"].replace(" (", "\n(") for e in evo]
    x = np.arange(len(evo))
    mlp_r90 = [e["mlp_mean_r90_frac"] * 100.0 for e in evo]
    attn_r90 = [e["attn_mean_r90_frac"] * 100.0 for e in evo]
    sr = [e["mlp_mean_stable_rank"] for e in evo]
    ax.plot(x, mlp_r90, "o-", color="#4C78A8", linewidth=2.2, markersize=7, label="MLP Mean r90 (% of min dim)")
    ax.plot(x, attn_r90, "s-", color="#54A24B", linewidth=2.2, markersize=7, label="Mixer/Attn Mean r90 (% of min dim)")
    ax.set_xticks(x)
    ax.set_xticklabels(lbls, fontsize=8)
    ax.set_ylabel("90%-Energy Spectral Rank (% of min dim)")
    ax_r = ax.twinx()
    ax_r.plot(x, sr, "D--", color="#E45756", linewidth=2.0, markersize=6, label="MLP Mean Stable Rank")
    ax_r.set_ylabel("MLP Stable Rank (||W||_F^2 / s1^2)", color="#E45756")
    ax_r.tick_params(axis="y", labelcolor="#E45756")
    ax.set_title("A. Spectral Rank Evolution (Init -> 1.114M Tokens)")
    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax_r.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, fontsize=8, loc="lower left")
    ax.grid(True, alpha=0.25)

    # Panel B: Canon-AC learned 4-tap conv RMS at Stage 3
    ax = axes[0, 1]
    ck = evo[-1]["canon_kernels"]
    c_names = sorted(ck.keys())
    xc = np.arange(len(c_names))
    w = 0.2
    colors = ["#4C78A8", "#54A24B", "#F58518", "#E45756"]
    for tap in range(4):
        vals = [ck[n]["tap_rms"][tap] for n in c_names]
        ax.bar(xc + (tap - 1.5) * w, vals, width=w, color=colors[tap], label=f"Tap -{tap}")
    ax.set_xticks(xc)
    ax.set_xticklabels(c_names, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("Learned Conv1d Tap RMS Weight")
    ax.set_title("B. Learned Canon-AC 4-Tap Causal Kernels (Stage 3)")
    ax.legend(fontsize=8, ncol=2)
    ax.grid(axis="y", alpha=0.25)

    # Panel C: mHC Birkhoff off-diagonal mixing mass across stages
    ax = axes[1, 0]
    mhc_keys = sorted(k for k, v in evo[-1]["mhc_summary"].items() if isinstance(v, dict) and "offdiag_mass" in v)
    xm = np.arange(len(mhc_keys))
    w = 0.22
    for idx_e, (e, col) in enumerate(zip(evo, ["#999999", "#4C78A8", "#F58518", "#54A24B"])):
        vals = [e["mhc_summary"][k]["offdiag_mass"] for k in mhc_keys]
        ax.bar(xm + (idx_e - 1.5) * w, vals, width=w, color=col, label=e["checkpoint"])
    ax.set_xticks(xm)
    ax.set_xticklabels(mhc_keys, rotation=28, ha="right", fontsize=8)
    ax.set_ylim(0, 0.062)
    ax.set_ylabel("Birkhoff Off-Diagonal Stream Mixing Mass")
    ax.set_title("C. DeepSeek mHC C=2 Doubly-Stochastic Mixing Evolution")
    ax.legend(fontsize=7.5, ncol=2, loc="upper right")
    ax.grid(axis="y", alpha=0.25)

    # Panel D: Stage 0 Sizing Audit — P4 Width Gate Proof
    ax = axes[1, 1]
    sz = s0["sizing_candidates"]
    names = [r["id"].replace("Arch7d_", "7d_").replace("_matched", "") for r in sz]
    q8_4k = [r["total_4k_q8_mib"] for r in sz]
    c1_4k = [r["total_4k_comp1_fp32scale_mib"] for r in sz]
    xs = np.arange(len(sz))
    ax.bar(xs - 0.18, q8_4k, width=0.35, color="#4C78A8", label="BCIRQ8 v1 + T=4K State")
    c_cols = ["#54A24B" if r["p4_width_gate_pass"] else "#E45756" for r in sz]
    bars = ax.bar(xs + 0.18, c1_4k, width=0.35, color=c_cols, label="COMP-1 + T=4K State (red = P4 fail)")
    for bar, r in zip(bars, sz):
        tag = "P4 FAIL" if not r["p4_width_gate_pass"] else f"{r['total_4k_comp1_fp32scale_mib']:.1f}M"
        ax.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height() + 0.3, tag, ha="center", va="bottom", fontsize=7.5, fontweight="bold")
    ax.set_xticks(xs)
    ax.set_xticklabels(names, rotation=28, ha="right", fontsize=7.8)
    ax.set_ylabel("Total T=4096 Deployment Footprint (MiB)")
    ax.set_title("D. Stage 0 Sizing & P4 Width-Gate Proof (d=512 vs d=384)")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25)

    fig.suptitle("BCIR-Arch7d-10M Structural Forensics: Spectral Rank, Canon-AC, mHC Birkhoff & P4 Sizing", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = FIG_DIR / "fig4_10m_spectral_mhc_canon_forensics.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_fig5_deployment() -> None:
    s5 = json.loads((RESULTS_DIR / "stage5_deep_eval_and_deploy.json").read_text(encoding="utf-8"))
    dep = s5["deployment_policy_qualification"]
    qat = s5["comp1_ste_qat_recovery"]

    fig, axes = plt.subplots(1, 2, figsize=(15.0, 5.2), dpi=150)

    # Panel A: Total T=4096 deployment footprint vs Untouched Test PPL
    ax = axes[0]
    pts = list(dep) + [{
        "policy": f"COMP1_FactorQAT_r{qat['rank']}_fp{qat['scale_bits']}scale",
        "total_4k_mib": qat["total_4k_mib"],
        "weights_mib": qat["weights_mib"],
        "test_ppl": qat["qat_test_ppl"],
    }]
    label_offsets = {
        "FP32_Ref": (-82, 10),
        "BCIRQ8_v1_g32": (10, 6),
        "COMP1_PTQ_r16_fp16scale": (12, 8),
        "COMP1_PTQ_r32_fp16scale": (12, -14),
        "COMP1_FactorQAT_r16_fp16scale": (12, -4),
    }
    for p in pts:
        name = p["policy"]
        col = "#4C78A8" if "FP32" in name else ("#54A24B" if "BCIRQ8" in name else ("#B279A2" if "FactorQAT" in name else "#F58518"))
        mk = "*" if "FactorQAT" in name else "o"
        sz = 230 if mk == "*" else 105
        ax.scatter(p["total_4k_mib"], p["test_ppl"], s=sz, c=col, marker=mk, edgecolors="black", linewidths=0.8, zorder=4)
        if name in label_offsets:
            ox, oy = label_offsets[name]
            short = (
                name.replace("COMP1_PTQ_r16_fp16scale", "COMP-1 PTQ r16 (fp16/fp32s)")
                .replace("COMP1_PTQ_r32_fp16scale", "COMP-1 PTQ r32 (fp16/fp32s)")
                .replace("COMP1_FactorQAT_r16_fp16scale", "COMP-1 Factor-QAT r16 fp16s")
            )
            ax.annotate(
                f"{short}\n({p['total_4k_mib']:.2f} MiB, PPL {p['test_ppl']:.2f})",
                (p["total_4k_mib"], p["test_ppl"]),
                xytext=(ox, oy),
                textcoords="offset points",
                fontsize=7.8,
                fontweight="bold" if ("FactorQAT" in name or "BCIRQ8" in name) else "normal",
            )
    ax.set_xlim(5.5, 43.5)
    ax.set_ylim(3.9, 9.4)
    ax.set_xlabel("Total T=4096 Deployment Footprint: Weights + State (MiB)")
    ax.set_ylabel("Untouched WikiText-2 Test PPL @ T=256 (lower is better)")
    ax.set_title("A. BCIR Deployment Policy (P1–P6) Accuracy vs Footprint")
    ax.grid(True, alpha=0.25)

    # Panel B: Factor-QAT recovery trajectory
    ax = axes[1]
    c = qat["curve"]
    xs = [r["step"] for r in c]
    ys = [r["val_ppl"] for r in c]
    ax.plot(xs, ys, "o-", color="#B279A2", linewidth=2.4, markersize=7, label="COMP-1 Factor-QAT (r=16, FP16 scales)")
    ax.axhline(dep[1]["test_ppl"], color="#54A24B", linestyle="--", linewidth=1.6, label=f"BCIRQ8 v1 Reference (test PPL {dep[1]['test_ppl']:.2f})")
    ax.axhline(ys[0], color="#F58518", linestyle=":", linewidth=1.6, label=f"Zero-QAT COMP-1 PTQ (val PPL {ys[0]:.2f})")
    for x0, y0 in zip(xs, ys):
        ax.annotate(f"{y0:.2f}", (x0, y0), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8.2)
    ax.set_ylim(3.9, 8.6)
    ax.set_xlabel("Exact Scale-Factor QAT Steps (Frozen BCIRQ8 Attn & Binary Signs)")
    ax.set_ylabel("Validation PPL on Discrete COMP-1 Weights")
    ax.set_title(f"B. Exact Differentiable COMP-1 Factor-QAT ({qat['qat_recovery_nats']:+.4f} nats)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)

    fig.suptitle("Stage 5: BCIR-Arch7d-10M Deployment Policy Qualification (FP32 -> BCIRQ8 v1 -> COMP-1 PTQ & Factor-QAT)", fontsize=12.2, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = FIG_DIR / "fig5_10m_comp1_deployment_frontier.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def main() -> None:
    plot_fig1_curriculum()
    plot_fig2_controls_pareto()
    plot_fig3_extrapolation()
    plot_fig4_forensics()
    plot_fig5_deployment()


if __name__ == "__main__":
    main()
