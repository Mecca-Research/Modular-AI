#!/usr/bin/env python3
"""Generate the two 6-panel publication-grade study figures from persisted JSON results:
  1. figures/bcir_precision_frontier_50m.png
  2. figures/bcir_mixer_ext_architectures.png
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STUDY_DIR = Path(__file__).resolve().parents[1]
RES = STUDY_DIR / "results"
FIG = STUDY_DIR / "figures"
FIG.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.size": 8.5,
    "axes.titlesize": 9.5,
    "axes.titleweight": "bold",
    "axes.labelsize": 8.5,
    "legend.fontsize": 7.2,
    "xtick.labelsize": 7.8,
    "ytick.labelsize": 7.8,
    "axes.grid": True,
    "grid.alpha": 0.28,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def make_precision_figure() -> None:
    e1 = json.loads((RES / "exp1_train_opt_1_4.json").read_text())
    e2 = json.loads((RES / "exp2_precision_frontier_complete.json").read_text())
    e3 = json.loads((RES / "exp3_littlebit_comp1_50m.json").read_text())

    fig, axes = plt.subplots(2, 3, figsize=(15.5, 9.2), dpi=200)
    fig.suptitle(
        "BCIR Precision Policy (P1-P6) Frontier, Optimizer-State Compression, and COMP-1 at 50M",
        fontsize=12.5, fontweight="bold", y=0.98,
    )

    # Panel A: 50M Persistent Memory vs Test Delta-CE (Optimizer vs Compute vs Region)
    ax = axes[0, 0]
    p50 = e2["wide50m"]["policies"]
    order = [
        ("FP32_Ref", "FP32 Ref", "#1f77b4", "o"),
        ("Opt_Q8_BF16Grad", "Q8+BF16g Opt", "#2ca02c", "s"),
        ("Opt_Adafactor", "Adafactor Opt", "#17becf", "D"),
        ("Compute_BF16_NDT", "BF16 NDT", "#9467bd", "^"),
        ("Compute_BF16_VDP", "BF16 VDP", "#8c564b", "v"),
        ("Compute_FP8_E4M3_E5M2", "FP8 E4M3/E5M2", "#ff7f0e", "P"),
        ("Compute_MXFP4_Uniform", "MXFP4 Unif", "#d62728", "X"),
        ("Compute_Asym_MXFP4_FP8", "Asym FP4/FP8", "#e377c2", "*"),
        ("Region_FP32Attn_MXFP4MLP", "FP32-A/FP4-M", "#008080", "H"),
        ("Region_FP8Attn_MXFP4MLP_Q8Opt", "FP8-A/FP4-M+Q8", "#bcbd22", "p"),
    ]
    for key, lbl, col, mk in order:
        if key not in p50:
            continue
        d = p50[key]
        x = d["persistent_bytes"] / (1024**2)
        y = d["delta_ce_vs_fp32"]
        ax.scatter([x], [y], color=col, marker=mk, s=75, label=lbl, zorder=4, edgecolors="black", linewidths=0.5)
    ax.axhline(0.04, color="#d62728", linestyle="--", linewidth=1.1, label="P4 gate (+0.04 nats)")
    ax.axhline(0.0, color="#666666", linestyle=":", linewidth=0.9)
    ax.set_xlabel("Persistent Training State at 50.5M (MiB)")
    ax.set_ylabel("Held-Out Test Delta CE vs FP32 (nats)")
    ax.set_title("(A) 50.5M Precision Frontier: Storage vs Quality")
    ax.legend(ncol=2, loc="best", frameon=True, framealpha=0.88)

    # Panel B: 50M Learning Curves (Validation CE vs Step)
    ax = axes[0, 1]
    curve_keys = [
        ("FP32_Ref", "FP32 Ref", "#1f77b4", "-"),
        ("Opt_Q8_BF16Grad", "Q8+BF16g Opt (8.05 B/p)", "#2ca02c", "--"),
        ("Compute_BF16_VDP", "BF16 VDP", "#8c564b", "-."),
        ("Compute_FP8_E4M3_E5M2", "FP8 E4M3/E5M2", "#ff7f0e", "-"),
        ("Compute_MXFP4_Uniform", "MXFP4 Uniform", "#d62728", ":"),
        ("Region_FP32Attn_MXFP4MLP", "FP32-Attn / FP4-MLP", "#008080", "-"),
        ("Region_FP8Attn_MXFP4MLP_Q8Opt", "FP8-Attn/FP4-MLP+Q8", "#bcbd22", "--"),
    ]
    for key, lbl, col, ls in curve_keys:
        if key not in p50:
            continue
        cv = p50[key]["runs"][0]["curve"]
        xs = [c["step"] for c in cv if c["step"] > 0]
        ys = [c["val_ce"] for c in cv if c["step"] > 0]
        ax.plot(xs, ys, label=lbl, color=col, linestyle=ls, marker="o", markersize=3.5, linewidth=1.6)
    ax.set_xlabel("Optimizer Step (B=4, T=64, wide50m)")
    ax.set_ylabel("Validation Cross-Entropy (nats)")
    ax.set_title("(B) 50.5M Validation Learning Curves")
    ax.legend(loc="upper right", frameon=True, framealpha=0.88)

    # Panel C: Isolated 2T GEMM Microbenchmark on 50M MLP (256x2048x768)
    ax = axes[0, 2]
    gm = e2["gemm_microbench_2t"]["wide50m_mlp_gate_up"]
    gnames = ["fp32_kblock", "bf16_ndt", "bf16_vdp_avx512", "fp8_e4m3_blk32", "fp8_e5m2_blk32", "mxfp4_e2m1_blk32", "bcirq8_g32"]
    glabels = ["FP32\nkblock", "BF16\nNDT", "BF16\nVDP", "FP8\nE4M3", "FP8\nE5M2", "MXFP4\nE2M1", "BCIRQ8\ng32"]
    gvals = [gm[g]["gflops"] for g in gnames]
    cols = ["#1f77b4", "#9467bd", "#8c564b", "#ff7f0e", "#ffbb78", "#d62728", "#2ca02c"]
    bars = ax.bar(range(len(gnames)), gvals, color=cols, edgecolor="black", linewidth=0.5)
    for b, v in zip(bars, gvals):
        ax.text(b.get_x() + b.get_width() / 2, v + max(gvals) * 0.02, f"{v:.0f}", ha="center", va="bottom", fontsize=7.3)
    ax.set_xticks(range(len(gnames)))
    ax.set_xticklabels(glabels)
    ax.set_ylabel("Throughput (GFLOP/s, 2 threads)")
    ax.set_title("(C) 50M MLP GEMM Contracts (256x2048x768)")
    ax.set_ylim(0, max(gvals) * 1.18)

    # Panel D: TRAIN-OPT-1 Init Policy Across 3 Scales (Test PPL)
    ax = axes[1, 0]
    scales = [("pilot1m", "pilot1m\n0.86M"), ("wide512", "wide512\n6.16M"), ("wide50m", "wide50m\n50.54M")]
    vlist = [("legacy_002_clip1", "legacy 0.02 (c=1)", "#d62728"),
             ("residual_scaled_clip1", "resid_scaled (c=1)", "#ff7f0e"),
             ("mup_v1_clip1", "mup_v1 (c=1)", "#1f77b4"),
             ("mup_v1_clip3", "mup_v1 (c=3)", "#2ca02c")]
    x0 = np.arange(len(scales))
    w = 0.19
    for vi, (vk, vlbl, vcol) in enumerate(vlist):
        vals = [e1["train_opt_1"][sk]["variants"][vk]["test_ppl_mean"] for sk, _ in scales]
        ax.bar(x0 + (vi - 1.5) * w, vals, width=w, label=vlbl, color=vcol, edgecolor="black", linewidth=0.4)
    ax.set_xticks(x0)
    ax.set_xticklabels([lbl for _, lbl in scales])
    ax.set_ylabel("Held-Out Test Perplexity (lower is better)")
    ax.set_title("(D) TRAIN-OPT-1 Init & Clip Scaling")
    ax.legend(loc="upper left", frameon=True, framealpha=0.88)

    # Panel E: 50.5M COMP-1 Deployment: Attention vs MLP Region Policy
    ax = axes[1, 1]
    d50 = e3["part_b_50m_comp1"]["deploy_50m"]
    dep_items = [
        ("FP32_Ref", "FP32 (32b)", "#1f77b4", (-44, 10)),
        ("FP16_Uniform", "FP16 (16b)", "#17becf", (-18, 10)),
        ("BCIRQ8_Uniform", "BCIRQ8 (8.25b)", "#2ca02c", (-10, 10)),
        ("COMP1_Q8Attn_LBMLP_r64_res", "COMP-1 r64 res (2.28b)", "#008080", (10, -10)),
        ("COMP1_Q8Attn_LBMLP_r64_1p", "COMP-1 r64 1p (2.17b)", "#ff7f0e", (10, 6)),
        ("LB_Uniform_r64_res", "LB Uniform (0.32b)", "#d62728", (10, -3)),
    ]
    for k, lbl, col, ofs in dep_items:
        x = d50[k]["stats"]["realized_body_bpw"]
        y = d50[k]["delta_ce_vs_fp32"]
        ax.scatter([x], [y], color=col, s=85, edgecolors="black", linewidths=0.6, zorder=4)
        ax.annotate(lbl, (x, y), xytext=ofs, textcoords="offset points", fontsize=7.2)
    ax.axhline(0.04, color="#d62728", linestyle="--", linewidth=1.0, label="P4 gate (+0.04 nats)")
    ax.set_xlim(-1.5, 35.5)
    ax.set_ylim(-0.08, 1.18)
    ax.set_xlabel("Realized Body Bits / Weight (BPW)")
    ax.set_ylabel("Held-Out Test Delta CE vs FP32 (nats)")
    ax.set_title("(E) 50.5M COMP-1 Deployment: Attn/MLP Regions")
    ax.legend(loc="center right", frameon=True, framealpha=0.88)

    # Panel F: Packed-1b LUT4 Batch Crossover on 50M MLP (768x2048, r=64 res)
    ax = axes[1, 2]
    cr = e3["part_b_50m_comp1"]["packed_lut4_crossover_768x2048_r64res"]
    ns = [1, 4, 16, 32, 64, 128, 256]
    for kkey, lbl, col, mk in [
        ("Dense_FP32_BCIR", "Dense FP32", "#1f77b4", "o"),
        ("Dense_FP16_Stream", "Dense FP16", "#17becf", "s"),
        ("Dense_BCIRQ8", "Dense BCIRQ8", "#2ca02c", "^"),
        ("LowRank_FP32", "LowRank FP32", "#9467bd", "D"),
        ("Packed1b_LUT4", "Packed-1b LUT4", "#ff7f0e", "v"),
        ("Packed1b_LUT4_mb4", "Packed-1b mb4", "#d62728", "P"),
    ]:
        ys = [cr[f"N={n}"][kkey]["median_us"] for n in ns]
        ax.plot(ns, ys, label=lbl, color=col, marker=mk, markersize=4.5, linewidth=1.6)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xticks(ns)
    ax.set_xticklabels([str(n) for n in ns])
    ax.set_xlabel("Batch Size N (tokens, 768x2048, r=64 res)")
    ax.set_ylabel("Median Latency (us, log scale)")
    ax.set_title("(F) Packed-1b LUT4 Batch Crossover (50M MLP)")
    ax.legend(loc="upper left", frameon=True, framealpha=0.88)

    plt.tight_layout(rect=[0, 0.0, 1, 0.955])
    out_path = FIG / "bcir_precision_frontier_50m.png"
    plt.savefig(out_path, dpi=200)
    plt.close(fig)
    print(f"Saved {out_path}")


def make_arch_figure() -> None:
    e3 = json.loads((RES / "exp3_littlebit_comp1_50m.json").read_text())
    e4 = json.loads((RES / "exp4_mixer_ext_complete.json").read_text())

    fig, axes = plt.subplots(2, 3, figsize=(15.5, 9.2), dpi=200)
    fig.suptitle(
        "BCIR MIXER-EXT Architectures (DeltaNet, Hybrid-SWA, MLA), Circuit Preservation, and COMP-1 Orthogonality",
        fontsize=12.0, fontweight="bold", y=0.98,
    )

    # Panel A: KV Circuit, Counterfactual & Long-Filler (>W) Separation Across MIXER-EXT Architectures
    ax = axes[0, 0]
    kv_a = e4["part_a_kv_circuits"]["architectures"]
    mq_a2 = e4["part_a2_multiquery_phase"]["architectures"]
    plbls = ["kv1\n(4A)", "mq4\n(4A2)", "swap\n(4A2)", "fill20>W\n(4A2)", "ext6\n(4A2)"]
    arch_plot = [
        ("GQA_Full", "GQA Full", "#1f77b4"),
        ("SWA_Only", "SWA Only (W=16)", "#7f7f7f"),
        ("Arch1_DeltaNet", "Arch-1 DeltaNet", "#d62728"),
        ("Arch2_Hybrid_SWA", "Arch-2 Hybrid-SWA", "#2ca02c"),
        ("Arch2_Hybrid_DN_GQA", "Arch-2 DN+GQA", "#ff7f0e"),
        ("Arch4_MLA", "Arch-4 MLA", "#9467bd"),
    ]
    x = np.arange(len(plbls))
    w = 0.13
    for i, (ak, albl, col) in enumerate(arch_plot):
        pr2 = mq_a2[ak]["probes"]
        vals = [
            kv_a[ak]["probes_summary"]["kv_single"]["acc_mean"] * 100.0,
            pr2["mq_4pair"]["accuracy"] * 100.0,
            pr2["mq_value_swap"]["accuracy"] * 100.0,
            pr2["mq_filler20_gtW"]["accuracy"] * 100.0,
            pr2["mq_extrap_6pair"]["accuracy"] * 100.0,
        ]
        ax.bar(x + (i - 2.5) * w, vals, width=w, label=albl, color=col, edgecolor="black", linewidth=0.35)
    ax.set_xticks(x)
    ax.set_xticklabels(plbls)
    ax.set_ylim(0, 122)
    ax.set_ylabel("Exact Match Accuracy (%)")
    ax.set_title("(A) MIXER-EXT Recall, Counterfactuals & Filler>W")
    ax.legend(ncol=2, loc="upper right", frameon=True, framealpha=0.88)

    # Panel B: wide10m WikiText-2 Held-Out Test PPL vs Step Time
    ax = axes[0, 1]
    lm_b = e4["part_b_lm_and_orthogonality"]["architectures"]
    b_specs = [
        ("GQA_Full", "GQA Full", "#1f77b4", (8, 4)),
        ("Arch1_DeltaNet", "Arch-1 DeltaNet", "#d62728", (-56, 10)),
        ("Arch2_Hybrid_SWA", "Arch-2 Hybrid-SWA", "#2ca02c", (8, -12)),
        ("Arch2_Hybrid_DN_GQA", "Arch-2 DN+GQA", "#ff7f0e", (-54, 10)),
        ("Arch4_MLA", "Arch-4 MLA", "#9467bd", (8, -4)),
    ]
    for ak, albl, col, ofs in b_specs:
        d = lm_b[ak]
        ax.scatter([d["median_step_ms"]], [d["test_ppl_mean"]], color=col, s=110, edgecolors="black", linewidths=0.7, label=albl, zorder=4)
        ax.annotate(albl, (d["median_step_ms"], d["test_ppl_mean"]), xytext=ofs, textcoords="offset points", fontsize=7.6)
    ax.set_xlim(148, 250)
    ax.set_ylim(9.05, 11.28)
    ax.set_xlabel("Median Training Step Latency (ms/step, 2T, wide10m)")
    ax.set_ylabel("Held-Out WikiText-2 Test PPL")
    ax.set_title("(B) wide10m (~9.84M) LM Quality vs Training Speed")

    # Panel C: 2x2 Orthogonality Proof (Weights + KV@4096 MiB)
    ax = axes[0, 2]
    orth = e4["part_b_lm_and_orthogonality"]["orthogonality_2x2"]
    anames = ["GQA_Full", "Arch2_Hybrid_SWA", "Arch4_MLA", "Arch2_Hybrid_DN_GQA", "Arch1_DeltaNet"]
    albls = ["GQA\nFull", "Hybrid\nSWA", "Arch-4\nMLA", "Hybrid\nDN+GQA", "Arch-1\nDeltaNet"]
    x = np.arange(len(anames))
    w = 0.34
    fp32_w = [orth[a]["FP32"]["weight_mib"] for a in anames]
    fp32_kv = [orth[a]["FP32"]["kv_4096_mib"] + orth[a]["FP32"]["rec_state_mib"] for a in anames]
    c1_w = [orth[a]["COMP1_Q8Attn_LBMLP_r48_res"]["weight_mib"] for a in anames]
    c1_kv = [orth[a]["COMP1_Q8Attn_LBMLP_r48_res"]["kv_4096_mib"] + orth[a]["COMP1_Q8Attn_LBMLP_r48_res"]["rec_state_mib"] for a in anames]
    ax.bar(x - w / 2, fp32_w, width=w, color="#1f77b4", label="FP32 Weights", edgecolor="black", linewidth=0.4)
    ax.bar(x - w / 2, fp32_kv, width=w, bottom=fp32_w, color="#aec7e8", label="KV/State @4096 (FP32)", edgecolor="black", linewidth=0.4)
    ax.bar(x + w / 2, c1_w, width=w, color="#2ca02c", label="COMP-1 Weights", edgecolor="black", linewidth=0.4)
    ax.bar(x + w / 2, c1_kv, width=w, bottom=c1_w, color="#98df8a", label="KV/State @4096 (COMP-1)", edgecolor="black", linewidth=0.4)
    ax.set_xticks(x)
    ax.set_xticklabels(albls)
    max_tot = max(w_ + kv_ for w_, kv_ in zip(fp32_w, fp32_kv))
    ax.set_ylim(0, max(62.0, max_tot * 1.25))
    ax.set_ylabel("Total Deploy Footprint at T=4096 (MiB)")
    ax.set_title("(C) 2x2 Orthogonality: COMP-1 Weights x KV Arch")
    ax.legend(ncol=2, loc="upper center", frameon=True, framealpha=0.9)

    # Panel D: Parameter-Matched 6.16M Geometry (Width vs Depth) Storage Floor & Quality
    ax = axes[1, 0]
    ga = e3["part_a_geometry_6m"]
    dw = ga["deploy_wide_shallow_6m"]
    dn = ga["deploy_narrow_deep_6m"]
    w_pts = [("FP32_Ref", "FP32"), ("BCIRQ8_Uniform", "Q8"), ("COMP1_Q8Attn_LBMLP_r48_res", "C1-res"), ("COMP1_Q8Attn_LBMLP_r64_1p", "C1-1p"), ("LB_Uniform_r48_res", "LB-unif")]
    n_pts = [("FP32_Ref", "FP32"), ("BCIRQ8_Uniform", "Q8"), ("COMP1_Q8Attn_LBMLP_r24_res", "C1-res"), ("COMP1_Q8Attn_LBMLP_r32_1p", "C1-1p"), ("LB_Uniform_r24_res", "LB-unif")]
    ax.plot([dw[k]["stats"]["realized_body_bpw"] for k, _ in w_pts],
            [dw[k]["delta_ce_vs_fp32"] for k, _ in w_pts],
            marker="o", linewidth=1.8, color="#1f77b4", label="wide_shallow (d=512, L=2, 6.165M)")
    ax.plot([dn[k]["stats"]["realized_body_bpw"] for k, _ in n_pts],
            [dn[k]["delta_ce_vs_fp32"] for k, _ in n_pts],
            marker="s", linewidth=1.8, color="#d62728", linestyle="--", label="narrow_deep (d=256, L=8, 6.166M)")
    ax.axhline(0.04, color="#666666", linestyle=":", linewidth=1.0, label="P4 gate (+0.04 nats)")
    ax.set_xlabel("Realized Body BPW (Matched Rank Fraction)")
    ax.set_ylabel("Held-Out Test Delta CE vs FP32 (nats)")
    ax.set_title("(D) Matched-Param (6.16M) Width vs Depth")
    ax.legend(loc="upper right", frameon=True, framealpha=0.88)

    # Panel E: wedge128_kv4_strong Exact Circuit Preservation (PTQ vs KD-QAT, Attention vs MLP Region)
    ax = axes[1, 1]
    pc = e3["part_c_wedge128_circuits"]
    ptq = pc["ptq_circuit_results"]
    qat = pc["qat_circuit_results"]
    bars_e = [
        ("FP32\nRef", ptq["FP32_Ref"]["suites"]["kv_single"]["acc"] * 100.0, "#1f77b4"),
        ("BCIRQ8\n(8.25b)", ptq["BCIRQ8_Uniform"]["suites"]["kv_single"]["acc"] * 100.0, "#2ca02c"),
        ("C1-r16\nPTQ", ptq["COMP1_Q8Attn_LBMLP_r16_res"]["suites"]["kv_single"]["acc"] * 100.0, "#ff7f0e"),
        ("C1-r16\nQAT", qat["COMP1_Q8Attn_LBMLP_r16_res_QAT"]["suites"]["kv_single"]["acc"] * 100.0, "#008080"),
        ("C1-r8\nQAT", qat["COMP1_Q8Attn_LBMLP_r8_res_QAT"]["suites"]["kv_single"]["acc"] * 100.0, "#17becf"),
        ("LB-Unif\nPTQ", ptq["LB_Uniform_r16_res"]["suites"]["kv_single"]["acc"] * 100.0, "#d62728"),
        ("LB-Unif\nQAT", qat["LB_Uniform_r16_res_QAT"]["suites"]["kv_single"]["acc"] * 100.0, "#9467bd"),
    ]
    x = np.arange(len(bars_e))
    vals = [v for _, v, _ in bars_e]
    cols = [c for _, _, c in bars_e]
    b_obj = ax.bar(x, vals, color=cols, edgecolor="black", linewidth=0.5)
    for bo, v in zip(b_obj, vals):
        ax.text(bo.get_x() + bo.get_width() / 2, v + 1.5, f"{v:.1f}%", ha="center", va="bottom", fontsize=7.2)
    ax.set_xticks(x)
    ax.set_xticklabels([lbl for lbl, _, _ in bars_e], fontsize=7.4)
    ax.set_ylim(0, 114)
    ax.set_ylabel("kv_single Exact Recall (%)")
    ax.set_title("(E) wedge128 Circuit: COMP-1 vs Uniform LB")

    # Panel F: Spectral Audit — 90%-Energy Rank Fraction vs 1-Path Storage Floor
    ax = axes[1, 2]
    spec_groups = [
        ("wedge128 (0.53M)", pc["spectra_wedge128"], "#d62728", "o"),
        ("narrow_deep (6.16M)", ga["spectra_audit"]["narrow_deep_6m"], "#ff7f0e", "s"),
        ("wide_shallow (6.16M)", ga["spectra_audit"]["wide_shallow_6m"], "#2ca02c", "^"),
        ("wide50m (50.54M)", e3["part_b_50m_comp1"]["spectra_50m"], "#1f77b4", "D"),
    ]
    for glbl, sdict, col, mk in spec_groups:
        xs = [v["bpw_floor_r1_1p"] for v in sdict.values()]
        ys = [v["r90_frac"] * 100.0 for v in sdict.values()]
        ax.scatter(xs, ys, color=col, marker=mk, s=45, alpha=0.8, label=glbl, edgecolors="black", linewidths=0.4)
    ax.set_xscale("log")
    ax.set_xlabel("Sub-1-Bit Storage Floor at r=1 (BPW, log)")
    ax.set_ylabel("90%-Energy Spectral Rank (% of min dim)")
    ax.set_title("(F) Low Storage Floor != Low Effective Rank")
    ax.legend(loc="lower left", frameon=True, framealpha=0.88)

    plt.tight_layout(rect=[0, 0.0, 1, 0.955])
    out_path = FIG / "bcir_mixer_ext_architectures.png"
    plt.savefig(out_path, dpi=200)
    plt.close(fig)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    make_precision_figure()
    make_arch_figure()
