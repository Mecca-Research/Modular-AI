#!/usr/bin/env python3
"""Stage 4: Generate 5 publication-quality figures from Stages 1-3 JSON/CSV outputs
and compute SHA-256 MANIFEST.json across all tracked study artifacts."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from common_wsp_sim import (
    CKPT_DIR,
    FIG_DIR,
    RESULTS_DIR,
    STUDY_DIR,
    sha256_file,
    write_json,
)

plt.rcParams.update({
    "font.size": 9.5,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.labelsize": 9.5,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": "white",
    "axes.facecolor": "#fbfcfd",
    "grid.alpha": 0.35,
})

TIER_COLORS = {
    "Baseline": "#64748b",
    "Control_50M_ExpD": "#dc2626",
    "Tier2_WSP": "#2563eb",
    "Tier2_WSP_Ablation": "#60a5fa",
    "Tier2_SSP_Weight": "#7c3aed",
    "Tier1_SSP_Live": "#059669",
    "Tier1_Harness": "#0d9488",
    "Hybrid_Tier1_Tier2": "#ea580c",
    "Control_WSP3": "#991b1b",
}

COND_COLORS = {
    "Cold_Start": "#94a3b8",
    "Harness_Transfer": "#2563eb",
    "Problem_Only": "#f59e0b",
    "Full_SIM": "#059669",
}


def plot_fig1_stage1_locus_audit() -> None:
    d1 = json.loads((RESULTS_DIR / "stage1_wsp_ssp_locus_audit.json").read_text(encoding="utf-8"))
    diag = d1["evolve_diagnosis"]["layers"]
    rows = d1["arms_table"]

    fig = plt.figure(figsize=(16.5, 10.2), dpi=180)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.0, 1.25], width_ratios=[1.05, 1.15], hspace=0.34, wspace=0.25)

    # Panel A: Per-layer residual contrast ||du|| + G1 gate loop vs clean + MLA repeat-copy heads
    ax_a = fig.add_subplot(gs[0, 0])
    layers = ["L0", "L1", "L2", "L3"]
    x = np.arange(len(layers))
    du = [diag[l]["resid_contrast_norm"] for l in layers]
    g1_loop = [float(np.mean(diag[l]["g1_loop_mean"])) for l in layers]
    g1_clean = [float(np.mean(diag[l]["g1_clean_mean"])) for l in layers]
    bar_cols = ["#7c3aed", "#7c3aed", "#7c3aed", "#ea580c"]
    bars = ax_a.bar(x - 0.16, du, width=0.32, color=bar_cols, edgecolor="black", linewidth=0.6, label="Resid Contrast ||Δu||₂")
    ax_a.set_xticks(x)
    ax_a.set_xticklabels(["L0\n(GDN-2)", "L1\n(GDN-2)", "L2\n(GDN-2)", "L3\n(NoPE-MLA)"])
    ax_a.set_ylabel("Residual Loop Contrast ||Δu||₂")
    ax_a.set_title("A. Gradient-Free Loop Activation Diagnosis (BCIR-Arch7d-10M)")
    ax_a.grid(axis="y", linestyle="--")
    for b, v in zip(bars, du):
        ax_a.text(b.get_x() + b.get_width() / 2, v + 0.03 * max(du), f"{v:.2f}", ha="center", va="bottom", fontsize=8.5, fontweight="bold")
    ax_a2 = ax_a.twinx()
    ax_a2.spines["top"].set_visible(False)
    ax_a2.plot(x + 0.16, g1_loop, "o-", color="#dc2626", linewidth=2, label="G1 Gate (Looping)")
    ax_a2.plot(x + 0.16, g1_clean, "s--", color="#059669", linewidth=1.8, label="G1 Gate (Clean)")
    ax_a2.set_ylabel("Mean G1 Sigmoid Gate Openness")
    ax_a2.set_ylim(0.0, 1.0)
    h1, l1 = ax_a.get_legend_handles_labels()
    h2, l2 = ax_a2.get_legend_handles_labels()
    ax_a.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, frameon=True)

    # Panel B: L3 NoPE-MLA Per-Head Repeat-Copy Attention & G1 Contrast (8 heads)
    ax_b = fig.add_subplot(gs[0, 1])
    h_idx = np.arange(8)
    rep_l = diag["L3"]["attn_rep_loop"]
    rep_c = diag["L3"]["attn_rep_clean"]
    g1_l3_d = diag["L3"]["g1_diff"]
    ax_b.bar(h_idx - 0.18, rep_l, width=0.35, color="#dc2626", alpha=0.88, label="Repeat-Token Attn Mass (Looping)")
    ax_b.bar(h_idx + 0.18, rep_c, width=0.35, color="#3b82f6", alpha=0.80, label="Repeat-Token Attn Mass (Clean)")
    top_h = diag["L3"]["top_heads"][0]
    ax_b.annotate(f"Top Copy Head H{top_h}\n(p25c / G1 target)",
                  xy=(top_h - 0.18, rep_l[top_h]), xytext=(min(top_h + 1.1, 5.2), max(rep_l) * 0.92),
                  arrowprops=dict(arrowstyle="->", color="#991b1b", lw=1.2),
                  fontsize=8.5, fontweight="bold", color="#991b1b")
    ax_b.set_xticks(h_idx)
    ax_b.set_xticklabels([f"H{i}\n(ΔG1 {g1_l3_d[i]:+.2f})" for i in range(8)], fontsize=8)
    ax_b.set_ylabel("L3 NoPE-MLA Attention Mass on Matching Prior Bytes")
    ax_b.set_title("B. Global NoPE-MLA (L3) Head-Level Induction / Loop-Copy Localization")
    ax_b.grid(axis="y", linestyle="--")
    ax_b.legend(loc="upper right", fontsize=8.5, frameon=True)

    # Panel C: All 18 Arms Held-Out Test Composite Score (Horizontal Bar Chart)
    ax_c = fig.add_subplot(gs[1, 0])
    labels = [r["arm_id"].replace("_", " ")[3:] for r in rows]
    scores = [r["test_score"] for r in rows]
    colors = [TIER_COLORS.get(r["tier"], "#64748b") for r in rows]
    y = np.arange(len(rows))
    ax_c.barh(y, scores, color=colors, edgecolor="black", linewidth=0.45, height=0.72)
    ax_c.set_yticks(y)
    ax_c.set_yticklabels(labels, fontsize=7.8)
    ax_c.invert_yaxis()
    ax_c.set_xlabel("Untouched Held-Out Test Composite Score (0–100; Higher = Better)")
    ax_c.set_title("C. 18-Arm Cross-Locus WSP / SSP / Control Held-Out Ranking")
    ax_c.grid(axis="x", linestyle="--")
    for i, r in enumerate(rows):
        ax_c.text(r["test_score"] + 0.8, i, f"{r['test_score']:.1f} (loop {r['test_loop_seq_rate']*100:.0f}%)",
                  va="center", fontsize=7.4)
    ax_c.set_xlim(0, max(scores) * 1.24)

    # Panel D: Pareto Frontier — Held-Out Composite Score vs KL(clean || edited)
    ax_d = fig.add_subplot(gs[1, 1])
    for r in rows:
        kl_plot = max(r["test_kl"], 1e-6)
        sc = r["test_score"]
        sz = 55 + 22 * math.log10(max(1.0, float(r["changed_params"]) + 1.0))
        col = TIER_COLORS.get(r["tier"], "#64748b")
        ax_d.scatter(kl_plot, sc, s=sz, color=col, edgecolor="black", linewidth=0.7, alpha=0.9, zorder=3)
    key_ids = {
        "01_Baseline_Clean": ("Clean Base (20.2)", (10, -2)),
        "02_Control_50M_ExpD_BlindSVD_L2": ("50M ExpD BlindSVD\n(32.8, 475k coords)", (-108, 12)),
        "06_WSP_L3_MLA_wdown_SparseTopK": ("L3.wdown Sparse\n(27.6, p50c)", (-22, 16)),
        "08_WSP_L3_MLA_G1_HeadGate": ("L3.bg G1 Gate\n(21.2, 3 scalars!)", (-62, -26)),
        "13_SSP_Live_GDN2_EchoDamp": ("SSP-Live GDN-2\n(30.5, 0 KL)", (10, 4)),
        "15_Harness_Decoding_NgramGuard": ("NgramGuard\n(78.9, 0 KL!)", (10, -4)),
        "16_Hybrid_Tier2WSP_plus_Tier1SSP": ("Hybrid Tier1+2\n(65.8)", (-78, -6)),
        "17_Control_Random_MatchedNorm": ("Random Matched-L2\n(20.2)", (-42, 14)),
        "18_Control_SGD_MicroFT_8steps": ("SGD 8-step FT\n(34.5, 10M coords)", (-28, 22)),
    }
    row_by_id = {r["arm_id"]: r for r in rows}
    for aid, (txt, ofs) in key_ids.items():
        if aid in row_by_id:
            r = row_by_id[aid]
            ax_d.annotate(txt, (max(r["test_kl"], 1e-6), r["test_score"]),
                          xytext=ofs, textcoords="offset points", fontsize=7.5, fontweight="bold",
                          arrowprops=dict(arrowstyle="->", color="#475569", lw=0.7))
    ax_d.set_xscale("log")
    ax_d.axvline(0.05, color="#dc2626", linestyle=":", linewidth=1.3, label="KL = 0.05 Ceiling")
    ax_d.set_xlabel("Untouched Test KL(clean || edited) [Log Scale; 10⁻⁶ = Exact Zero Drift]")
    ax_d.set_ylabel("Untouched Held-Out Composite Score (0–100; Higher = Better)")
    ax_d.set_title("D. Pareto Frontier: Behavioral Score vs Collateral KL & Edit Sparsity")
    ax_d.set_ylim(10, 88)
    ax_d.set_xlim(5e-7, 9e-2)
    ax_d.grid(True, which="both", linestyle="--", alpha=0.3)
    handles = [
        mpatches.Patch(color=TIER_COLORS["Tier1_SSP_Live"], label="Tier 1: SSP-Live / Harness (0 weight edits)"),
        mpatches.Patch(color=TIER_COLORS["Tier2_WSP"], label="Tier 2: Surgical WSP (L3.wdown / G1)"),
        mpatches.Patch(color=TIER_COLORS["Tier2_SSP_Weight"], label="Tier 2: GDN-2 State-Space Params"),
        mpatches.Patch(color=TIER_COLORS["Hybrid_Tier1_Tier2"], label="Hybrid Tier 1 + Tier 2"),
        mpatches.Patch(color=TIER_COLORS["Control_50M_ExpD"], label="Controls (50M BlindSVD / Random / SGD)"),
    ]
    ax_d.legend(handles=handles, loc="upper right", fontsize=7.4, frameon=True)

    fig.suptitle("BCIR-Arch7d-10M: Re-Examining Weight-Space & State-Space Programming Across All Hybrid Loci",
                 fontsize=13, fontweight="bold", y=0.98)
    fig.savefig(FIG_DIR / "fig1_arch7d_10m_locus_audit.png", bbox_inches="tight")
    plt.close(fig)


def plot_fig2_hybrid_train_editability() -> None:
    d2 = json.loads((RESULTS_DIR / "stage2_hybrid_train_editability.json").read_text(encoding="utf-8"))
    with (RESULTS_DIR / "stage2_train_steps.csv").open(encoding="utf-8") as f:
        srows = list(csv.DictReader(f))

    fig, axes = plt.subplots(1, 3, figsize=(16.8, 5.1), dpi=180)
    plt.subplots_adjust(wspace=0.28)
    arch_colors = {"Arch7d_2M": "#059669", "Arch2_Hybrid_2M": "#2563eb", "GQA_Full_2M": "#dc2626"}

    # Panel A: Training Loss & Val PPL Curves
    ax = axes[0]
    for aname, col in arch_colors.items():
        pts = [r for r in srows if r["arch"] == aname]
        toks = np.array([float(r["tokens"]) / 1e3 for r in pts])
        loss = np.array([float(r["loss"]) for r in pts])
        ker = np.ones(5) / 5.0
        sm = np.convolve(loss, ker, mode="valid")
        ax.plot(toks[2:-2], sm, color=col, linewidth=2.0, label=f"{aname} (Test PPL {d2['architectures'][aname]['final_test_baseline']['ppl']:.2f})")
    ax.set_xlabel("Training Tokens (k bytes, WikiText-2)")
    ax.set_ylabel("Smoothed Train Cross-Entropy (nats)")
    ax.set_title("A. Matched ~2.6M Training\n(Opt_Q8_Muon_BF16Grad)", fontsize=10.5)
    ax.grid(True, linestyle="--")
    ax.legend(fontsize=8.2, frameon=True)

    # Panel B: Peak Activation Trajectory
    ax = axes[1]
    for aname, col in arch_colors.items():
        traj = d2["architectures"][aname]["trajectory"]
        tk = [t["tokens"] / 1e3 for t in traj]
        act = [t["max_abs_act"] for t in traj]
        ax.plot(tk, act, "o-", color=col, linewidth=2.0, label=f"{aname} max|act|")
    ax.set_xlabel("Training Tokens (k bytes)")
    ax.set_ylabel("Peak Residual Activation max|act|")
    ax.set_title("B. Activation Stability\n(Hybrid vs Uniform GQA)", fontsize=10.5)
    ax.grid(True, linestyle="--")
    ax.legend(fontsize=8.2, frameon=True)

    # Panel C: Per-Layer (L0..L3) Sparse WSP Score Gain across Architectures
    ax = axes[2]
    layers = ["L0", "L1", "L2", "L3"]
    x = np.arange(len(layers))
    w_bar = 0.25
    for idx, (aname, col) in enumerate(arch_colors.items()):
        pl = d2["architectures"][aname]["per_layer_wsp"]
        gains = [pl[l]["score_gain"] for l in layers]
        bars = ax.bar(x + (idx - 1) * w_bar, gains, width=w_bar, color=col, edgecolor="black", linewidth=0.5, label=aname)
        for b, g in zip(bars, gains):
            ax.text(b.get_x() + b.get_width() / 2, g + (0.25 if g >= 0 else -0.35), f"{g:+.1f}",
                    ha="center", va="bottom" if g >= 0 else "top", fontsize=7.4, fontweight="bold")
    ax.axhline(0.0, color="black", linewidth=0.8)
    ax.set_ylim(-9.4, 7.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["L0\n(Rec/GQA)", "L1\n(Rec/GQA)", "L2\n(Rec/GQA)", "L3\n(Global Anchor)"])
    ax.set_ylabel("Untouched Held-Out Score Gain (ΔScore)")
    ax.set_title("C. Layer-Wise Sparse WSP\nEditability Localization (GQA 0/4)", fontsize=10.5)
    ax.grid(axis="y", linestyle="--")
    ax.legend(fontsize=8.0, frameon=True, loc="upper right")

    fig.suptitle("Stage 2: Matched 3-Architecture Training & Layer-Wise WSP Editability Localization",
                 fontsize=12.5, fontweight="bold", y=1.03)
    fig.savefig(FIG_DIR / "fig2_hybrid_train_and_editability.png", bbox_inches="tight")
    plt.close(fig)


def plot_fig3_sim_transitions() -> None:
    d3 = json.loads((RESULTS_DIR / "stage3_sim_crsi_experiments.json").read_text(encoding="utf-8"))
    tdet = d3["transitions_detail"]
    tids = [
        ("T1a_CrossDataset_10M_Prose_to_Structured", "T1a: Cross-Dataset (10M)\nProse → Structured"),
        ("T1b_CrossDataset_10M_Prose_to_LongCtx", "T1b: Cross-Dataset (10M)\nProse → LongCtx Echo"),
        ("T2a_CrossScale_2M_to_10M_Arch7d", "T2a: Cross-Scale Policy\nArch7d 2M → 10M"),
        ("T2b_CrossFamily_GQA2M_to_Arch7d10M", "T2b: Cross-Family Policy\nGQA-2M → Arch7d-10M"),
        ("T3_CrossPrecision_Q8_to_COMP1_10M", "T3: Cross-Precision (10M)\nBCIRQ8 v1 → COMP-1 1-Bit"),
    ]

    fig, axes = plt.subplots(2, 3, figsize=(16.5, 9.2), dpi=180)
    axes_flat = axes.ravel()

    for idx, (tid, title) in enumerate(tids):
        ax = axes_flat[idx]
        tr = tdet[tid]
        for cname in ("Cold_Start", "Harness_Transfer", "Problem_Only", "Full_SIM"):
            hist = tr["conditions"][cname]["history"]
            rds = [h["round"] for h in hist]
            scs = [h["heldout_score"] for h in hist]
            lw = 2.6 if cname == "Full_SIM" else 1.8
            mk = "o" if cname == "Full_SIM" else ("s" if cname == "Harness_Transfer" else ("^" if cname == "Problem_Only" else "x"))
            ax.plot(rds, scs, marker=mk, color=COND_COLORS[cname], linewidth=lw, label=cname.replace("_", " "))
        ax.set_title(title)
        ax.set_xlabel("CRSI Target Round (Matched Budget)")
        ax.set_ylabel("Untouched Held-Out Target Score")
        ax.set_xticks(rds)
        ax.grid(True, linestyle="--")
        if idx == 0:
            ax.legend(fontsize=8.2, frameon=True, loc="lower right")

    ax = axes_flat[5]
    x = np.arange(len(tids))
    w = 0.19
    for ci, cname in enumerate(("Cold_Start", "Harness_Transfer", "Problem_Only", "Full_SIM")):
        vals = [tdet[tid]["conditions"][cname]["final_heldout"]["score"] for tid, _ in tids]
        ax.bar(x + (ci - 1.5) * w, vals, width=w, color=COND_COLORS[cname], edgecolor="black", linewidth=0.45,
               label=cname.replace("_", " "))
    ax.set_xticks(x)
    ax.set_xticklabels(["T1a\nData", "T1b\nLongCtx", "T2a\n2M→10M", "T2b\nGQA→7d", "T3\nCOMP-1"], fontsize=8.5)
    ax.set_ylabel("Final Held-Out Target Score")
    ax.set_title("Final Held-Out Summary (All 5 Real BCIR Shifts)")
    ax.grid(axis="y", linestyle="--")
    ax.legend(fontsize=7.8, frameon=True, loc="lower right")

    fig.suptitle("Stage 3: Google Self-Improvement Memory (SIM, arXiv:2611.18856) Across 5 Real BCIR Continual Shifts",
                 fontsize=13, fontweight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(FIG_DIR / "fig3_sim_crsi_transitions.png", bbox_inches="tight")
    plt.close(fig)


def plot_fig4_sim_10seed_stats() -> None:
    d3 = json.loads((RESULTS_DIR / "stage3_sim_crsi_experiments.json").read_text(encoding="utf-8"))
    rows = d3["paired_10seed_rows"]
    st = d3["paired_10seed_stats"]
    st_tr = st["full_sim_vs_harness_transfer"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.4, 5.4), dpi=180)
    plt.subplots_adjust(wspace=0.22)

    idx = np.arange(len(rows))
    tr_v = [r["harness_transfer"] for r in rows]
    po_v = [r["problem_only"] for r in rows]
    sm_v = [r["full_sim"] for r in rows]
    cs_v = [r["cold_start"] for r in rows]

    for i in range(len(rows)):
        ax1.plot([i, i], [tr_v[i], sm_v[i]], color="#059669" if sm_v[i] >= tr_v[i] else "#dc2626", linewidth=1.8, alpha=0.75)
    ax1.scatter(idx, cs_v, color=COND_COLORS["Cold_Start"], marker="x", s=45, label=f"Cold Start (μ={st['mean_cold_start']:.2f})", zorder=3)
    ax1.scatter(idx, tr_v, color=COND_COLORS["Harness_Transfer"], marker="s", s=55, label=f"Harness Transfer (μ={st['mean_harness_transfer']:.2f})", zorder=4)
    ax1.scatter(idx, po_v, color=COND_COLORS["Problem_Only"], marker="^", s=50, label=f"Problem-Only (μ={st['mean_problem_only']:.2f})", zorder=4)
    ax1.scatter(idx, sm_v, color=COND_COLORS["Full_SIM"], marker="o", s=65, edgecolor="black", linewidth=0.6, label=f"Full SIM (μ={st['mean_full_sim']:.2f})", zorder=5)
    ax1.set_xticks(idx)
    ax1.set_xticklabels([f"P{i+1}" for i in range(len(rows))])
    ax1.set_xlabel("Independent Paired Source→Target Seed Run (Arch7d_2M D1 → D2)")
    ax1.set_ylabel("Untouched Held-Out Target Score")
    ax1.set_title(f"A. 10-Seed Paired CRSI Reproducibility (n=6 Evolve)\nWins {st_tr['wins']}/10 | Δ={st_tr['mean_diff']:+.2f} | p={st_tr['p_two_sided']:.4f} | dz={st_tr['cohens_dz']:.2f}", fontsize=10.2)
    ax1.grid(True, linestyle="--")
    ax1.legend(fontsize=8.2, frameon=True, loc="lower left")

    fams = ["harness_ngram", "ssp_live_gdn2", "ssp_live_mla", "wsp_l3_wdown", "wsp_l3_g1",
            "ssp_weight_gdn2", "wsp_gdn_wdown", "wsp_canon", "wsp_mhc"]
    short = ["Ngram\nGuard", "SSP-Live\nGDN-2", "SSP-Live\nMLA", "L3.wdown\nSparse", "L3.bg\nG1",
             "GDN-2\nGates", "L0-L2\nwdown", "Canon-AC\n(Trap)", "mHC\n(Trap)"]
    tr_cnt = [sum(r["action_counts_transfer"].get(f, 0) for r in rows) for f in fams]
    sm_cnt = [sum(r["action_counts_full_sim"].get(f, 0) for r in rows) for f in fams]
    tr_pct = [100.0 * v / max(1, sum(tr_cnt)) for v in tr_cnt]
    sm_pct = [100.0 * v / max(1, sum(sm_cnt)) for v in sm_cnt]
    x = np.arange(len(fams))
    ax2.bar(x - 0.19, tr_pct, width=0.38, color=COND_COLORS["Harness_Transfer"], alpha=0.85, label="Harness Transfer (No Memory)")
    ax2.bar(x + 0.19, sm_pct, width=0.38, color=COND_COLORS["Full_SIM"], alpha=0.92, label="Full SIM (Provenance + Failure Memory)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(short, fontsize=7.8)
    ax2.set_ylabel("Share of Candidate Evaluation Budget (%)")
    ax2.set_title(f"B. SIM Trap-Locus Suppression & Source Retention\nMean D1 Retention: Full SIM {st['mean_retention_full_sim']:.2f} vs Transfer {st['mean_retention_transfer']:.2f} (+{st['mean_retention_full_sim']-st['mean_retention_transfer']:.2f})", fontsize=10.2)
    ax2.grid(axis="y", linestyle="--")
    ax2.legend(fontsize=8.2, frameon=True)

    fig.suptitle("Stage 3 Statistical Validation: Paired n=10 SIM Advantage, Trap-Locus Suppression & Source Retention",
                 fontsize=12.5, fontweight="bold", y=1.03)
    fig.savefig(FIG_DIR / "fig4_sim_10seed_paired_stats.png", bbox_inches="tight")
    plt.close(fig)


def plot_fig5_unified_architecture_diagram() -> None:
    d1 = json.loads((RESULTS_DIR / "stage1_wsp_ssp_locus_audit.json").read_text(encoding="utf-8"))
    d3 = json.loads((RESULTS_DIR / "stage3_sim_crsi_experiments.json").read_text(encoding="utf-8"))
    r_by_id = {r["arm_id"]: r for r in d1["arms_table"]}
    base_r4 = r_by_id["01_Baseline_Clean"]["test_rep4_rate"] * 100.0
    l3_w = r_by_id["06_WSP_L3_MLA_wdown_SparseTopK"]
    l3_g = r_by_id["08_WSP_L3_MLA_G1_HeadGate"]
    sspl = r_by_id["13_SSP_Live_GDN2_EchoDamp"]
    ng = r_by_id["15_Harness_Decoding_NgramGuard"]
    s10 = d3["paired_10seed_stats"]["full_sim_vs_harness_transfer"]

    fig, ax = plt.subplots(figsize=(15.4, 8.2), dpi=180)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 104)
    ax.axis("off")

    def box(x, y, w, h, fc, ec, title, body, title_col="#0f172a"):
        rect = mpatches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.8,rounding_size=1.8",
                                       facecolor=fc, edgecolor=ec, linewidth=2.0)
        ax.add_patch(rect)
        ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="top", fontsize=10.2, fontweight="bold", color=title_col)
        ax.text(x + 1.8, y + h - 6.8, body, ha="left", va="top", fontsize=8.4, color="#1e293b", linespacing=1.35)

    # Tier 3: Outer-Loop Meta-Memory (SIM)
    box(3, 76, 94, 23, "#ecfdf5", "#059669",
        "TIER 3: OUTER-LOOP META-MEMORY — Self-Improvement Memory (SIM, arXiv:2611.18856, bcir.sim.v1)",
        "• Governs the OPTIMIZER (Proposer + Selector), NOT the policy context window. Problem-grouped JSON ledger storing:\n"
        "  {problem_id, root_cause_hypothesis, locus, rationale, predicted_outcome (pre-eval), observed_outcome, failed/regressed attempts, provenance}.\n"
        "• 5/5 real BCIR continual shifts #1 (or 99+ tie, 1.4–1.9× faster): T1a +4.20 pts | T2b GQA→Arch7d +6.24 pts (ret 78.3 vs 8.4) | T3 Q8→COMP-1 +27.35 pts.\n"
        f"• 10-seed paired (n=6 evolve): Full SIM leads all 4 conditions (81.7 vs 72.8, {s10['wins']}/10 wins, Δ={s10['mean_diff']:+.2f}, p={s10['p_two_sided']:.3f}, ret +9.7 pts); cuts trap loci 2.1–2.6×.",
        title_col="#065f46")

    # Tier 2: Persistent Surgical Weight-Space Programming (WSP-1..4)
    box(3, 40, 45, 25, "#eff6ff", "#2563eb",
        "TIER 2: PERSISTENT WEIGHT-SPACE (WSP-1..4)",
        "• Optimal Hybrid Locus: Global NoPE-MLA Anchor (L3 ≡ 3 mod 4)\n"
        "  (exact structural stratum of Qwen3.5-0.8B L15/L19 in Takehi et al.)\n"
        f"• L3.wdown Sparse Top-64: {l3_w['changed_pct']:.3f}% params | rep4 {base_r4:.1f}%→{l3_w['test_rep4_rate']*100:.1f}% (beats dense/rand)\n"
        f"• L3.bg G1 Gate (Arch-7d exclusive): {l3_g['changed_params']} scalars ({l3_g['changed_pct']:.5f}%) | KL={l3_g['test_kl']:.4f}\n"
        "• Avoided Traps: Blind SVD (475k coords), Canon-AC, mHC, L0–L1 FFN\n"
        "• Governed by WSP-1 WeightPatch SHA-256 + WSP-2 Sentinel + WSP-3 KL gate",
        title_col="#1e40af")

    # Tier 1: Ephemeral State-Space Programming (SSP-Live) + Decoding Harness
    box(52, 40, 45, 25, "#f5f3ff", "#7c3aed",
        "TIER 1: LIVE STATE-SPACE PROGRAMMING (SSP-Live)",
        "• Zero Weight Mutation (ΔCE = 0.0000, KL = 0.0000 on clean text!)\n"
        "• Recurrent Stratum (L0–L2 GDN-2): Adaptive online echo damping of\n"
        f"  FP64 state S_t + alpha/erase shift → score {sspl['test_score']:.1f}, rep4 {base_r4:.1f}%→{sspl['test_rep4_rate']*100:.1f}%\n"
        "• Global Stratum (L3 NoPE-MLA): Adaptive repeat-key attention logit bias\n"
        f"• Decoding Harness (NgramGuard): score {ng['test_score']:.1f}, loop 100%→{ng['test_loop_seq_rate']*100:.0f}%, dist4 {ng['test_distinct4']*100:.1f}%\n"
        "• Preferred over static weight edits for runtime decoding attractors",
        title_col="#5b21b6")

    # Foundation: BCIR-Arch7d-10M ISO C11 ABI 6 Substrate
    box(3, 4, 94, 25, "#fff7ed", "#ea580c",
        "BCIR-Arch7d-10M PRODUCTION ISO C11 SUBSTRATE (3:1 GDN-2 + NoPE-MLA + G1 + QK-Norm + Canon-AC + mHC)",
        "• Recurrent Fast Working Memory (L0, L1, L2): GDN-2 (channelwise Diag(α) + decoupled erase b_t & write w_t, FP64 WY, O(1) state = 0.75 MiB)\n"
        "• Global Semantic & Induction Hub (L3): NoPE-MLA (dc=128 compressed KV = 2.00 MiB @ T=4096) + Headwise G1 Sigmoid Gate + QK-Norm\n"
        "• Structural Backbone: Canon-AC (k=4 causal conv) + DeepSeek mHC (C=2 Birkhoff doubly-stochastic streams, max|act|=2.17, sink=0.005 @ 4K)\n"
        "• Training & Deploy: Opt_Q8_Muon_BF16Grad (7.13 B/p) → BCIRQ8 v1 (9.87 MiB, +0.0001 nats) / COMP-1 1-bit MLP (5.04 MiB, 7.79 MiB @ 4K)\n"
        "• Symbolic Exact Routing (WSP-4 / SLM-6): Deterministic arithmetic & exact KV offloaded outside neural weights",
        title_col="#9a3412")

    arrow_kw = dict(arrowstyle="-|>,head_width=0.5,head_length=0.8", lw=2.0)
    ax.annotate("", xy=(25.5, 66.2), xytext=(25.5, 74.8), arrowprops=dict(color="#059669", **arrow_kw))
    ax.annotate("", xy=(74.5, 66.2), xytext=(74.5, 74.8), arrowprops=dict(color="#059669", **arrow_kw))
    ax.text(27.0, 70.5, "Proposes & gates WSP WeightPatch", va="center", fontsize=8.3, color="#065f46", fontweight="bold")
    ax.text(76.0, 70.5, "Proposes & tunes SSP-Live / Harness", va="center", fontsize=8.3, color="#065f46", fontweight="bold")
    ax.annotate("", xy=(25.5, 30.2), xytext=(25.5, 38.8), arrowprops=dict(color="#2563eb", **arrow_kw))
    ax.annotate("", xy=(74.5, 30.2), xytext=(74.5, 38.8), arrowprops=dict(color="#7c3aed", **arrow_kw))
    ax.text(27.0, 34.5, "Persistent sparse patch (L3.wdown / L3.bg)", va="center", fontsize=8.3, color="#1e40af", fontweight="bold")
    ax.text(76.0, 34.5, "Per-token runtime control (GDN-2 S_t / MLA / Logits)", va="center", fontsize=8.3, color="#5b21b6", fontweight="bold")

    ax.set_title("Unified 3-Tier BCIR Hybrid Architecture: Where State-Space Programming, Weight-Space Programming & SIM Fit",
                 fontsize=12.5, fontweight="bold", pad=8)
    fig.savefig(FIG_DIR / "fig5_unified_3tier_hybrid_architecture.png", bbox_inches="tight")
    plt.close(fig)


def write_manifest() -> None:
    files = []
    for base in (STUDY_DIR / "src", RESULTS_DIR, FIG_DIR, CKPT_DIR):
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.name != "MANIFEST.json" and not p.name.endswith(".tmp"):
                files.append({
                    "path": str(p.relative_to(STUDY_DIR)),
                    "bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                })
    for top in ("Makefile", ".gitignore", "README.md"):
        tp = STUDY_DIR / top
        if tp.exists():
            files.append({"path": top, "bytes": tp.stat().st_size, "sha256": sha256_file(tp)})
    files.sort(key=lambda r: r["path"])
    write_json(RESULTS_DIR / "MANIFEST.json", {
        "schema": "bcir.study.manifest.v1",
        "study": "wsp-sim-hybrid-study",
        "file_count": len(files),
        "files": files,
    })


def main() -> None:
    plot_fig1_stage1_locus_audit()
    plot_fig2_hybrid_train_editability()
    plot_fig3_sim_transitions()
    plot_fig4_sim_10seed_stats()
    plot_fig5_unified_architecture_diagram()
    write_manifest()
    print("Generated all 5 figures + results/MANIFEST.json successfully.", flush=True)


if __name__ == "__main__":
    main()
