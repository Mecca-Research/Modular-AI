#!/usr/bin/env python3
"""Generate publication-quality figures for the BCIR ABI 6 MIXER-EXT & Optimizer Study."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STUDY_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = STUDY_DIR / "results"
FIG_DIR = STUDY_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)


def plot_exp_a() -> None:
    p = RESULTS_DIR / "exp_a_optimizer_frontier.json"
    if not p.exists():
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.0), dpi=150)

    # Panel 1: wide50m persistent RAM breakdown
    ax = axes[0]
    w50 = data["a3_wide50m_and_amort"]["wide50m_optimizers"]
    order = [
        ("Opt_FP32_AdamW", "FP32 AdamW\n(16.00 B/p)"),
        ("Opt_Adafactor", "Adafactor\n(12.01 B/p)"),
        ("Opt_Muon_FP32", "Muon FP32\n(12.03 B/p)"),
        ("Opt_Q8_BF16Grad", "Q8+BF16Grad\n(8.05 B/p)"),
        ("Opt_Q8_Muon_BF16Grad", "Q8-Muon+BF16\n(7.07 B/p)"),
    ]
    labels = [lbl for k, lbl in order if k in w50]
    w_mib = [w50[k]["weight_mib"] for k, _ in order if k in w50]
    o_mib = [w50[k]["opt_only_mib"] for k, _ in order if k in w50]
    x = np.arange(len(labels))
    ax.bar(x, w_mib, color="#4C78A8", label="FP32 Master Weights")
    ax.bar(x, o_mib, bottom=w_mib, color="#F58518", label="Grads + Optimizer State")
    for i, (w, o) in enumerate(zip(w_mib, o_mib)):
        tot = w + o
        ax.text(i, tot + 14, f"{tot:.0f} MiB", ha="center", va="bottom", fontsize=8.5, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Persistent Training State (MiB)")
    ax.set_title("A1. wide50m (50.74M) Persistent State")
    ax.set_ylim(0, max(w + o for w, o in zip(w_mib, o_mib)) * 1.15)
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", alpha=0.25)

    # Panel 2: wide10m Hybrid-DN+GQA Quality vs RAM Pareto
    ax = axes[1]
    a2 = data["a2_wide10m_hybrid_dn_gqa"]["runs"]
    colors = {
        "Opt_FP32_AdamW": "#4C78A8",
        "Opt_Adafactor": "#54A24B",
        "Opt_Q8_BF16Grad": "#E45756",
        "Opt_Q8_Muon_BF16Grad": "#B279A2",
    }
    short = {
        "Opt_FP32_AdamW": "FP32 AdamW",
        "Opt_Adafactor": "Adafactor",
        "Opt_Q8_BF16Grad": "Q8+BF16Grad",
        "Opt_Q8_Muon_BF16Grad": "Q8-Muon+BF16Grad",
    }
    offsets_a2 = {
        "Opt_FP32_AdamW": (-108, -22),
        "Opt_Adafactor": (-10, -28),
        "Opt_Q8_BF16Grad": (10, -18),
        "Opt_Q8_Muon_BF16Grad": (10, 6),
    }
    for k, rec in a2.items():
        ax.scatter(
            rec["persistent_mib"], rec["mean_test_ppl"],
            s=140, color=colors.get(k, "#333333"), zorder=4, edgecolors="black", linewidths=0.8,
        )
        ox, oy = offsets_a2.get(k, (6, 6))
        ax.annotate(
            f"{short.get(k, k)}\n({rec['bytes_per_param']:.2f} B/p, PPL {rec['mean_test_ppl']:.2f})",
            (rec["persistent_mib"], rec["mean_test_ppl"]),
            xytext=(ox, oy), textcoords="offset points",
            fontsize=8, fontweight="bold" if "Muon" in k else "normal",
        )
    ax.set_ylim(9.05, 10.22)
    ax.set_xlim(84, 216)
    ax.set_xlabel("Persistent Training State (MiB) — lower is better")
    ax.set_ylabel("Untouched WikiText-2 Test PPL (2 seeds) — lower is better")
    ax.set_title("A2. wide10m 3:1 Hybrid-DN+GQA Optimizer Pareto")
    ax.grid(True, alpha=0.25)

    # Panel 3: Muon Newton-Schulz batch amortization
    ax = axes[2]
    amort = data["a3_wide50m_and_amort"]["muon_batch_amortization"]
    toks = [r["tokens_per_update"] for r in amort]
    ratios = [r["q8_muon_vs_adafactor_x"] for r in amort]
    # Add analytical projection for 4096 & 8192 from measured fixed NS overhead (~940ms)
    ada_per_tok = amort[-1]["adafactor_ms"] / amort[-1]["tokens_per_update"]
    ns_fixed = amort[-1]["q8_muon_ms"] - amort[-1]["adafactor_ms"]
    proj_toks = [2048, 4096, 8192]
    proj_ratios = [(ada_per_tok * n + ns_fixed) / (ada_per_tok * n) for n in proj_toks]
    ax.plot(toks, ratios, "o-", color="#B279A2", linewidth=2.2, markersize=7, label="Measured (2-core Haswell)")
    ax.plot(proj_toks, proj_ratios, "s--", color="#B279A2", alpha=0.65, linewidth=1.6, markersize=5, label="Analytical Fixed-NS Projection")
    ax.axhline(1.0, color="#54A24B", linestyle=":", linewidth=1.4, label="Adafactor Parity (1.0x)")
    for t, r in zip(toks, ratios):
        ax.annotate(f"{r:.2f}x", (t, r), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8.5, fontweight="bold")
    ax.annotate(f"~{proj_ratios[-1]:.2f}x", (proj_toks[-1], proj_ratios[-1]), xytext=(-14, 8), textcoords="offset points", fontsize=8)
    ax.set_xscale("log", base=2)
    ax.set_xticks([256, 512, 1024, 2048, 4096, 8192])
    ax.set_xticklabels(["256", "512", "1024", "2048", "4096", "8192"])
    ax.set_xlabel("Tokens per Optimizer Update (N = B x T x accum)")
    ax.set_ylabel("Step Time Ratio (Q8-Muon / Adafactor)")
    ax.set_title("A3. Newton-Schulz Batch Amortization (wide512)")
    ax.set_ylim(0.5, max(ratios) * 1.15)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)

    fig.suptitle("BCIR ABI 6 Default Training Stack & Q8-Muon Frontier (2-Core Haswell AVX2)", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out = FIG_DIR / "abi6_optimizer_frontier.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_exp_b() -> None:
    p = RESULTS_DIR / "exp_b_arch_ladder_abi6.json"
    if not p.exists():
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    fig, axes = plt.subplots(2, 2, figsize=(15.5, 10.5), dpi=150)

    short_names = {
        "GQA_Full": "GQA Full",
        "GQA_Full_G1_QKNorm": "GQA+G1+QK",
        "GQA_Full_CanonAC": "GQA+CanonAC",
        "Arch1_DeltaNet": "Arch1 DN",
        "Arch2_Hybrid_DN_GQA": "Arch2 DN+GQA",
        "Arch4_MLA": "Arch4 MLA",
        "Arch7a_KDA_NoPEMLA": "7a KDA+NoPE",
        "Arch7b_GDN2_G1NoPEMLA": "7b GDN2+G1",
        "Arch7c_GDN2_G1NoPEMLA_CanonAC": "7c +CanonAC",
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2": "7d +mHC2 (#1)",
    }

    # Panel 1: wide10m PPL vs T=4096 State MiB
    ax = axes[0, 0]
    b2 = data.get("b2_wide10m_lm", {}).get("architectures", {})
    b2_offsets = {
        "GQA_Full": (-62, 7),
        "GQA_Full_CanonAC": (-82, -4),
        "GQA_Full_G1_QKNorm": (-76, -15),
        "Arch4_MLA": (-68, -4),
        "Arch1_DeltaNet": (6, 4),
        "Arch2_Hybrid_DN_GQA": (8, -6),
        "Arch7a_KDA_NoPEMLA": (7, 2),
        "Arch7b_GDN2_G1NoPEMLA": (7, 5),
        "Arch7c_GDN2_G1NoPEMLA_CanonAC": (8, 6),
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2": (8, -12),
    }
    for name, rec in b2.items():
        c = "#4C78A8" if "GQA_Full" in name else ("#54A24B" if "Arch7" in name else "#F58518")
        marker = "*" if "Arch7c" in name or "Arch7d" in name else "o"
        sz = 200 if marker == "*" else 100
        ax.scatter(rec["state_4k_mib"], rec["mean_test_ppl"], s=sz, c=c, marker=marker, edgecolors="black", linewidths=0.7, zorder=4)
        ox, oy = b2_offsets.get(name, (5, 4))
        ax.annotate(
            short_names.get(name, name),
            (rec["state_4k_mib"], rec["mean_test_ppl"]),
            xytext=(ox, oy), textcoords="offset points", fontsize=8,
            fontweight="bold" if "Arch7d" in name else "normal",
        )
    ax.set_xlim(0.2, 17.2)
    ax.set_ylim(9.42, 11.12)
    ax.set_xlabel("T=4096 Streaming State (KV + Recurrent MiB) — lower is better")
    ax.set_ylabel("Untouched WikiText-2 Test PPL (2 seeds) — lower is better")
    ax.set_title("B2. wide10m Quality vs T=4096 Inference State")
    ax.grid(True, alpha=0.25)

    # Panel 2: B1 Recall & Length Extrapolation
    ax = axes[0, 1]
    b1 = data.get("b1_recall_suite", {}).get("architectures", {})
    pick = [
        "GQA_Full",
        "GQA_Full_CanonAC",
        "GQA_Full_G1_QKNorm",
        "Arch1_DeltaNet",
        "Arch2_Hybrid_DN_GQA",
        "Arch7a_KDA_NoPEMLA",
        "Arch7b_GDN2_G1NoPEMLA",
        "Arch7c_GDN2_G1NoPEMLA_CanonAC",
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2",
    ]
    pick = [k for k in pick if k in b1]
    probes = [
        ("mq4", "MQ-4", "#4C78A8"),
        ("value_swap", "Value Swap", "#F58518"),
        ("filler20_gtW", "Filler>W", "#54A24B"),
        ("extrap_6pair", "Extrap 6p", "#E45756"),
        ("ordered_8pair_T96", "Ord 8p T96", "#B279A2"),
    ]
    x = np.arange(len(pick))
    width = 0.16
    for idx, (pk, plbl, col) in enumerate(probes):
        vals = [b1[k]["mean_probes_pct"][pk] for k in pick]
        ax.bar(x + (idx - 2) * width, vals, width=width, color=col, label=plbl)
    ax.set_xticks(x)
    ax.set_xticklabels([short_names.get(k, k) for k in pick], rotation=28, ha="right", fontsize=8)
    ax.set_ylabel("Exact Match Accuracy (%)")
    ax.set_title("B1. Associative Recall, Counterfactuals & T=96 Extrapolation")
    ax.legend(fontsize=7.5, ncol=3, loc="upper right")
    ax.set_ylim(0, 31)
    ax.grid(axis="y", alpha=0.25)

    # Panel 3: Attention Sink & Max Activation Outliers
    ax = axes[1, 0]
    names_b2 = [
        "GQA_Full",
        "GQA_Full_G1_QKNorm",
        "GQA_Full_CanonAC",
        "Arch4_MLA",
        "Arch1_DeltaNet",
        "Arch2_Hybrid_DN_GQA",
        "Arch7a_KDA_NoPEMLA",
        "Arch7b_GDN2_G1NoPEMLA",
        "Arch7c_GDN2_G1NoPEMLA_CanonAC",
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2",
    ]
    names_b2 = [k for k in names_b2 if k in b2]
    x2 = np.arange(len(names_b2))
    max_acts = [b2[k]["max_abs_act"] for k in names_b2]
    sinks = [b2[k]["first_tok_attn_mean"] for k in names_b2]
    ax.bar(x2 - 0.18, max_acts, width=0.36, color="#E45756", label="Max |Activation|")
    ax.set_ylabel("Max |Activation|", color="#E45756")
    ax.tick_params(axis="y", labelcolor="#E45756")
    ax_r = ax.twinx()
    ax_r.bar(x2 + 0.18, sinks, width=0.36, color="#4C78A8", alpha=0.8, label="First-Tok Attn Sink")
    ax_r.set_ylabel("First-Token Attention Mean (Sink)", color="#4C78A8")
    ax_r.tick_params(axis="y", labelcolor="#4C78A8")
    ax.set_xticks(x2)
    ax.set_xticklabels([short_names.get(k, k) for k in names_b2], rotation=32, ha="right", fontsize=8)
    ax.set_title("B2. Activation Outliers & Attention-Sink Audit (wide10m)")
    ax.grid(axis="y", alpha=0.2)

    # Panel 4: TRAIN-OPT-5 Online-Softmax O(T) vs O(T^2) Memory Scaling
    ax = axes[1, 1]
    b3 = data.get("b3_online_softmax_memory", {}).get("scaling", [])
    for arch_key, col, lbl in [
        ("GQA_Full", "#4C78A8", "GQA Full"),
        ("Arch7c_GDN2_G1NoPEMLA_CanonAC", "#54A24B", "Arch7c (3:1 GDN2+MLA)"),
    ]:
        rows = [r for r in b3 if r["arch"] == arch_key]
        if not rows:
            continue
        ts = [r["T"] for r in rows]
        mat = [r["materialized_prob_mib"] for r in rows]
        onl = [r["online_prob_mib"] for r in rows]
        ax.plot(ts, mat, "o--", color=col, linewidth=2.0, label=f"{lbl} Materialized O(T^2)")
        ax.plot(ts, onl, "s-", color=col, linewidth=2.4, label=f"{lbl} TRAIN-OPT-5 Online O(T)")
    ax.set_yscale("log")
    ax.set_xticks([64, 256, 512, 1024])
    ax.set_xlabel("Sequence Length T (wide10m, B=4)")
    ax.set_ylabel("Mixer Attention Prob / State Buffer (MiB, log)")
    ax.set_title("B3. TRAIN-OPT-5 Online-Softmax Memory Scaling (up to 512x)")
    ax.legend(fontsize=8)
    ax.grid(True, which="both", alpha=0.25)

    fig.suptitle("BCIR ABI 6 MIXER-EXT: 10-Architecture Ladder & Beyond-the-List Ablations", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = FIG_DIR / "abi6_architecture_frontier.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_exp_c() -> None:
    p = RESULTS_DIR / "exp_c_tier_ml_pilot.json"
    if not p.exists():
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    runs = data.get("runs", {})
    if not runs:
        return
    fig, axes = plt.subplots(1, 2, figsize=(14.0, 5.2), dpi=150)

    ordered_keys = [
        "GQA_Full_Adafactor",
        "Hybrid_DN_GQA_Adafactor",
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Adafactor",
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Q8Muon",
    ]
    ordered_keys = [k for k in ordered_keys if k in runs]
    palette = {
        "GQA_Full_Adafactor": ("#4C78A8", "GQA Full (Adafactor)"),
        "Hybrid_DN_GQA_Adafactor": ("#F58518", "Arch2 3:1 DN+GQA (Adafactor)"),
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Adafactor": ("#54A24B", "Arch7d +mHC2 (Adafactor)"),
        "Arch7d_GDN2_G1NoPEMLA_CanonAC_mHC2_Q8Muon": ("#B279A2", "Arch7d +mHC2 (Q8-Muon)"),
    }

    ax = axes[0]
    for key in ordered_keys:
        rec = runs[key]
        col, lbl = palette.get(key, ("#333333", key))
        pts = [c for c in rec["curve"] if c["tokens"] > 0]
        toks_m = [c["tokens"] / 1e6 for c in pts]
        ppls = [c["val_ppl"] for c in pts]
        ax.plot(toks_m, ppls, "o-", color=col, linewidth=2.3, markersize=6, label=f"{lbl} (test {rec['test_ppl']:.2f})")
    ax.set_xlabel("Training Tokens Executed (Millions, T=256, online_softmax=1)")
    ax.set_ylabel("Validation PPL — lower is better")
    ax.set_title("C1. 1.024M-Token Long-Horizon Pilot Learning Curves")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)

    ax = axes[1]
    x = np.arange(len(ordered_keys))
    test_ppls = [runs[k]["test_ppl"] for k in ordered_keys]
    cols = [palette.get(k, ("#333333", k))[0] for k in ordered_keys]
    bars = ax.bar(x, test_ppls, color=cols, edgecolor="black", linewidth=0.7)
    for bar, k in zip(bars, ordered_keys):
        r = runs[k]
        ax.text(
            bar.get_x() + bar.get_width() / 2.0, bar.get_height() + 0.1,
            f"PPL {r['test_ppl']:.2f}\n{r['persistent_mib']:.1f} MiB | act {r['max_abs_act']:.1f}\n{r['tok_per_s_2core_avx2']:.0f} tok/s",
            ha="center", va="bottom", fontsize=7.8, fontweight="bold",
        )
    ax.set_xticks(x)
    ax.set_xticklabels([palette.get(k, ("", k))[1].replace(" (", "\n(") for k in ordered_keys], fontsize=8)
    ax.set_ylabel("Untouched WikiText-2 Test PPL (T=256)")
    ax.set_title("C2. 1.024M-Token Pilot: Quality, RAM, Outliers & Speed")
    ax.set_ylim(0, max(test_ppls) * 1.30)
    ax.grid(axis="y", alpha=0.25)

    fig.suptitle("BCIR ABI 6 Experiment C: 1.024M-Token T=256 Online-Softmax Pilot", fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out = FIG_DIR / "abi6_tier_pilot_1m_curve.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def main() -> None:
    plot_exp_a()
    plot_exp_b()
    plot_exp_c()


if __name__ == "__main__":
    main()
