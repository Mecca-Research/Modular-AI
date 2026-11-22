"""Generate publication-grade 6-panel diagnostic figure from BCIR native-C LittleBit study JSONs."""
import json
import os
import re
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

STUDY_DIR = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
RESULTS_DIR = STUDY_DIR / 'results'
OUT_PNG = Path(os.environ.get('LITTLEBIT_OUT_PNG', str(STUDY_DIR / 'figures/bcir_littlebit_study_results.png')))


def parse_packed_lut4(path: Path):
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^(\S+)\s+\d+x\d+\s+r=\s*(\d+)\s*\|\s*packed1b_LUT4=\s*([\d.]+)us', line.strip())
        if m:
            out[m.group(1)] = {'r': int(m.group(2)), 'us': float(m.group(3))}
    return out


def make_plots():
    p1 = json.loads((RESULTS_DIR / 'pillar1_bpw_analysis.json').read_text())
    sa = json.loads((RESULTS_DIR / 'pillar4_suite_a.json').read_text())
    sb = json.loads((RESULTS_DIR / 'pillar4_suite_b_capabilities.json').read_text())
    sbo = json.loads((RESULTS_DIR / 'pillar4_suite_b_ordered.json').read_text())
    sc = json.loads((RESULTS_DIR / 'pillar4_suite_c_ablations.json').read_text())
    p5 = json.loads((RESULTS_DIR / 'pillar5_kernel_benchmark.json').read_text())
    p5b = json.loads((RESULTS_DIR / 'bench_lut4_packed_batched.json').read_text())
    packed = parse_packed_lut4(RESULTS_DIR / 'bench_lut4_packed.txt')

    if 'seaborn-v0_8-whitegrid' in plt.style.available:
        plt.style.use('seaborn-v0_8-whitegrid')
    fig, axes = plt.subplots(2, 3, figsize=(19, 10.8), dpi=160)

    # Panel A: Pillar 1 Scale-Overhead Floor vs Model Width d
    ax = axes[0, 0]
    ds = [r['d'] for r in p1]
    fl_res = [r['scale_floor_fp16_res'] for r in p1]
    fl_1p = [r['scale_floor_fp16_1p'] for r in p1]
    fl_kv = [r['kv_scale_floor_fp16_res'] for r in p1]
    ax.loglog(ds, fl_res, 'o-', label='Body Scale Floor (2-path Res, FP16)', lw=2.2, color='#d62728')
    ax.loglog(ds, fl_1p, 's--', label='Body Scale Floor (1-path, FP16)', lw=2.0, color='#1f77b4')
    ax.loglog(ds, fl_kv, '^:', label='GQA K/V Scale Floor (2-path Res)', lw=2.0, color='#ff7f0e')
    ax.axhline(0.10, color='#2ca02c', ls='-.', lw=1.6, label='0.10 BPW Target')
    ax.set_ylim(0.002, 0.90)
    ax.annotate('d=128\n0.425b', xy=(128, 0.425), xytext=(190, 0.33), fontsize=7.5,
                arrowprops=dict(arrowstyle='->', color='#d62728', lw=0.9))
    ax.annotate('d=512\n0.101b', xy=(512, 0.1005), xytext=(820, 0.16), fontsize=7.5,
                arrowprops=dict(arrowstyle='->', color='#d62728', lw=0.9))
    ax.annotate('7B (0.012b)', xy=(4096, 0.0123), xytext=(1500, 0.0045), fontsize=7.5,
                arrowprops=dict(arrowstyle='->', color='#d62728', lw=0.9))
    ax.set_xlabel('Model Width $d_{\\mathrm{model}}$ (log scale)')
    ax.set_ylabel('Scale-Only Floor (BPW, log scale)')
    ax.set_title('A. Multi-Scale Overhead Floor Law (Before r=1)', fontweight='bold')
    ax.legend(fontsize=7.5, loc='lower left')
    ax.grid(True, which='both', alpha=0.3)

    # Panel B: Suite A wide512 (5.50M GQA 4:1) BPW vs WikiText-2 Test PPL
    ax = axes[0, 1]
    wrows = sa['wide512_5.50M']
    fp32_ppl = next(r['qat_test']['ppl'] for r in wrows if 'FP32_teacher' in r['label'])
    q8_ppl = next(r['qat_test']['ppl'] for r in wrows if 'BCIR_Q8' in r['label'])
    ob_row = next(r for r in wrows if 'OneBit' in r['label'])
    a7_all = next(r for r in sc['A7_bcir_synthesis'] if r['label'] == 'A7_BCIR_All_ITQ_STE_KL_kv2')
    for prefix, label, style, col in [
        ('TinyRankFP16', 'Tiny-Rank FP16 + KD', 's--', '#7f7f7f'),
        ('LBv1_Res', 'LittleBit-v1 (Dual-SVID Res)', 'o-', '#1f77b4'),
        ('LBv2_ITQ_Res', 'LittleBit-2 (Joint-ITQ Res)', 'D-', '#2ca02c'),
    ]:
        pts = sorted([r for r in wrows if prefix in r['label']], key=lambda r: r['realized_body_bpw'])
        ax.plot([r['realized_body_bpw'] for r in pts], [r['qat_test']['ppl'] for r in pts],
                style, label=label, lw=2.2, markersize=6, color=col)
    ax.axhline(fp32_ppl, color='black', ls='-', lw=1.3, label=f'FP32 Teacher ({fp32_ppl:.2f})')
    ax.axhline(q8_ppl, color='#9467bd', ls=':', lw=1.6, label=f'BCIRQ8 8.50b ({q8_ppl:.2f})')
    ax.scatter([ob_row['realized_body_bpw']], [ob_row['qat_test']['ppl']], color='#d62728', s=75, zorder=5,
               label=f'OneBit Full 1b ({ob_row["qat_test"]["ppl"]:.2f})')
    ax.scatter([a7_all['realized_body_bpw']], [a7_all['qat_test']['ppl']], color='#ff7f0e', marker='*',
               s=180, edgecolor='black', linewidth=0.6, zorder=6,
               label=f'BCIR ITQ+STE+KL+kv2 ({a7_all["qat_test"]["ppl"]:.2f} @ 0.31b)')
    ax.set_xlabel('Realized Body BPW (incl. FP16 scales)')
    ax.set_ylabel('WikiText-2 Byte Test Perplexity (lower better)')
    ax.set_title('B. BCIR-wide512 (5.50M GQA) Sub-1-Bit Frontier', fontweight='bold')
    ax.legend(fontsize=7.2, loc='upper right')
    ax.grid(True, alpha=0.3)

    # Panel C: Suite B Exact Capabilities (KV-1pair PTQ/QAT + Ordered + Value-Swap Counterfactual)
    ax = axes[0, 2]
    kv1_map = {r['variant']: r for r in sb['kv_single_1pair']}
    ord_map = {r['variant']: r for r in sbo}
    keys = ['FP32_teacher', 'BCIR_Q8_PTQ', 'OneBit_Full_KD', 'TinyRankFP16_0.75',
            'LBv1_1P_0.75', 'LBv2_ITQ_1P_0.75', 'LBv2_ITQ_Res_0.75', 'LBv1_MLPonly_0.75', 'LBv1_AttnOnly_0.75']
    short = ['FP32', 'Q8\nPTQ', 'OneBit\n1.2b', 'TinyFP16\n0.75b', 'LBv1-1P\n0.75b',
             'LBv2-1P\n0.75b', 'LBv2-Res\n0.74b', 'LB-MLP\nonly', 'LB-Attn\nonly']
    x = np.arange(len(keys))
    w = 0.20
    ax.bar(x - 1.5 * w, [100 * kv1_map[k]['ptq']['acc'] for k in keys], w, label='KV-1p PTQ (0-shot)', color='#aec7e8')
    ax.bar(x - 0.5 * w, [100 * kv1_map[k]['qat']['acc'] for k in keys], w, label='KV-1p QAT+KD', color='#1f77b4')
    ax.bar(x + 0.5 * w, [100 * ord_map[k]['qat']['acc'] for k in keys], w, label='Ordered QAT+KD', color='#2ca02c')
    ax.bar(x + 1.5 * w, [100 * ord_map[k]['value_swap']['both_correct'] for k in keys], w, label='Ordered Value-Swap', color='#d62728')
    ax.set_xticks(x); ax.set_xticklabels(short, fontsize=7.2)
    ax.set_ylabel('Exact Accuracy / Both-Correct (%)')
    ax.set_ylim(0, 126)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_title('C. Exact Circuits & Counterfactuals (BCIR-wedge128)', fontweight='bold')
    ax.legend(fontsize=7.2, loc='upper center', ncol=2, frameon=True, framealpha=0.95)
    ax.grid(True, axis='y', alpha=0.3)

    # Panel D: Pillar 5 Exact 1-Bit Packed AVX-512 LUT4 Decode Speedups (N=1, 1T)
    ax = axes[1, 0]
    p5_lookup = {(r['label'], r['target_bpw']): r for r in p5['kernel_sweep'] if r['N'] == 1 and r['residual']}
    bench_spec = [
        ('wide50m\n0.30b', 'wide50m_0.30', ('bcir_wide50m_mlp', 0.3)),
        ('7B\n0.55b', 'llama2_7b_0.55', ('llama2_7b_mlp', 0.55)),
        ('7B\n0.30b', 'llama2_7b_0.30', ('llama2_7b_mlp', 0.3)),
        ('7B\n0.10b', 'llama2_7b_0.10', ('llama2_7b_mlp', 0.1)),
        ('70B\n0.55b', 'llama2_70b_0.55', ('llama2_70b_mlp', 0.55)),
        ('70B\n0.30b', 'llama2_70b_0.30', ('llama2_70b_mlp', 0.3)),
        ('70B\n0.10b', 'llama2_70b_0.10', ('llama2_70b_mlp', 0.1)),
    ]
    lbls = [s[0] for s in bench_spec]
    sp_fp16 = [p5_lookup[s[2]]['kernels']['dense_fp16_stream']['median_us'] / packed[s[1]]['us'] for s in bench_spec]
    sp_q8 = [p5_lookup[s[2]]['kernels']['dense_q8_bcir']['median_us'] / packed[s[1]]['us'] for s in bench_spec]
    sp_sx = [p5_lookup[s[2]]['kernels']['dense_fp16_stream']['median_us'] / p5_lookup[s[2]]['kernels']['lb_avx512_signxor']['median_us'] for s in bench_spec]
    xx = np.arange(len(bench_spec))
    ax.bar(xx - 0.27, sp_fp16, 0.27, label='Packed-1b LUT4 vs FP16', color='#2ca02c')
    ax.bar(xx,        sp_q8,   0.27, label='Packed-1b LUT4 vs Q8 g32', color='#1f77b4')
    ax.bar(xx + 0.27, sp_sx,   0.27, label='AVX-512 SignXOR vs FP16', color='#d62728')
    ax.set_yscale('log')
    ax.axhline(1.0, color='black', ls='--', lw=1)
    for i, v in enumerate(sp_fp16):
        ax.text(i - 0.27, v * 1.12, f'{v:.0f}×', ha='center', va='bottom', fontsize=6.8, fontweight='bold', color='#1a6b1a')
    ax.set_xticks(xx); ax.set_xticklabels(lbls, fontsize=7.5)
    ax.set_ylabel('Measured 1T CPU Speedup (×, log scale)')
    ax.set_ylim(0.8, 420)
    ax.set_title('D. Exact 1-Bit Packed AVX-512 LUT4 Decode (N=1)', fontweight='bold')
    ax.legend(fontsize=7.3, loc='upper left')
    ax.grid(True, which='both', axis='y', alpha=0.3)

    # Panel E: Measured Packed-1b LUT4 vs LowRank-FP32 Prefill Crossover Across Shapes (1T AVX-512)
    ax = axes[1, 1]
    w50_rows = sorted([r for r in p5b['results'] if r['label'] == 'wide50m_0.30'], key=lambda r: r['N'])
    l7b_rows = sorted([r for r in p5b['results'] if r['label'] == 'llama2_7b_0.30'], key=lambda r: r['N'])
    Ns_w = [r['N'] for r in w50_rows]
    Ns_7 = [r['N'] for r in l7b_rows]
    ax.plot(Ns_w, [r['kernels']['packed1b_lut4_mb4']['gflops_equiv'] for r in w50_rows], 'D-',
            label='wide50m 0.30b: Packed-1b LUT4 (mb4)', lw=2.2, color='#2ca02c')
    ax.plot(Ns_w, [r['kernels']['packed1b_lut4']['gflops_equiv'] for r in w50_rows], 's:',
            label='wide50m 0.30b: Packed-1b LUT4 (1-tok)', lw=1.7, color='#66bb6a')
    ax.plot(Ns_w, [r['kernels']['lowrank_fp32']['gflops_equiv'] for r in w50_rows], '^--',
            label='wide50m 0.30b: LowRank-FP32 (kblock)', lw=2.1, color='#ff7f0e')
    ax.plot(Ns_w, [r['kernels']['dense_fp32_bcir']['gflops_equiv'] for r in w50_rows], 'o-',
            label='wide50m 0.30b: BCIR FP32 kblock', lw=1.8, color='#1f77b4')
    ax.plot(Ns_7, [r['kernels']['packed1b_lut4_mb4']['gflops_equiv'] for r in l7b_rows], 'P-',
            label='Llama2-7B 0.30b: Packed-1b LUT4 (mb4)', lw=2.2, color='#9467bd')
    ax.plot(Ns_7, [r['kernels']['lowrank_fp32']['gflops_equiv'] for r in l7b_rows], 'v--',
            label='Llama2-7B 0.30b: LowRank-FP32 (kblock)', lw=1.9, color='#d62728')
    ax.axvline(64, color='#ff7f0e', ls=':', lw=1.1, alpha=0.8)
    ax.annotate('wide50m mb4 tie\n(N=64, 1.00×)', xy=(64, 472), xytext=(9.5, 515), fontsize=7.0,
                color='#b85600', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#b85600', lw=0.9))
    ax.annotate('7B mb4 still 1.96×\nfaster at N=128', xy=(128, 724), xytext=(22, 815), fontsize=7.0,
                color='#6a1b9a', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#6a1b9a', lw=0.9))
    ax.set_xscale('log', base=2)
    ax.set_xlabel('Batch × Tokens $N$ (Decode N=1 → Prefill N=256)')
    ax.set_ylabel('Effective Throughput (GFLOP/s equiv.)')
    ax.set_ylim(0, 900)
    ax.set_title('E. Packed-1b LUT4 Prefill Crossover (Shape-Dependent)', fontweight='bold')
    ax.legend(fontsize=6.6, loc='lower right', frameon=True, framealpha=0.93)
    ax.grid(True, alpha=0.3)

    # Panel F: Suite C Ablations Summary on wide512 @ 0.30 BPW (log-x for wide dynamic range)
    ax = axes[1, 2]
    def get_ppl(grp, lbl):
        return next(r['qat_test']['ppl'] for r in sc[grp] if r['label'] == lbl)

    ab_items = [
        ('COMP-1 Q8Attn+0.3bMLP', get_ppl('A7_bcir_synthesis', 'A7_COMP1_Q8Attn_ITQ_STE_KL_MLP030')),
        ('BCIR 0.31b (ITQ+STE+KL)', get_ppl('A7_bcir_synthesis', 'A7_BCIR_All_ITQ_STE_KL_kv2')),
        ('MLP-only 0.30b (Eq11)', get_ppl('A6_layer_sensitivity', 'A6_MLP_only_0.30')),
        ('GQA kv_factor=2.0',    get_ppl('A5_gqa_kv_factor', 'A5_kvfactor_2.0')),
        ('Attn-only 0.30b',      get_ppl('A6_layer_sensitivity', 'A6_Attn_only_0.30')),
        ('DualSVID + KL (λ=0)',  get_ppl('A4_init_and_objective', 'A4_DualSVID_KL_only')),
        ('ITQ + Clipped STE',    get_ppl('A3_surrogate', 'A3_ITQ_Clipped_STE')),
        ('DualSVID + CE only',   get_ppl('A4_init_and_objective', 'A4_DualSVID_CE_only')),
        ('Paper Eq.11 (λ=10)',   get_ppl('A3_surrogate', 'A3_SmoothSign_k100')),
        ('Naive SVD + KD',       get_ppl('A4_init_and_objective', 'A4_NaiveSVD_KD')),
        ('From-Scratch (CE)',    get_ppl('A4_init_and_objective', 'A4_FromScratch_Random_CE')),
        ('No scales (h,g,l)',    get_ppl('A2_multiscale', 'A2_scales_none')),
    ]
    y_pos = np.arange(len(ab_items))
    vals = [v for _, v in ab_items]
    cols = ['#2ca02c' if v < 8.5 else '#1f77b4' if v < 10.0 else '#ff7f0e' if v < 15 else '#d62728' for v in vals]
    ax.barh(y_pos, vals, color=cols)
    ax.set_xscale('log')
    ax.axvline(fp32_ppl, color='black', ls='--', lw=1.2, label=f'FP32 ({fp32_ppl:.2f})')
    ax.set_yticks(y_pos); ax.set_yticklabels([k for k, _ in ab_items], fontsize=7.4)
    ax.invert_yaxis()
    ax.set_xlim(5.5, max(vals) * 2.1)
    ax.set_xlabel('WikiText-2 Test Perplexity (log scale, lower better)')
    ax.set_title('F. Component Ablations (wide512 @ 0.30 BPW)', fontweight='bold')
    for i, v in enumerate(vals):
        ax.text(v * 1.05, i, f'{v:.2f}', va='center', fontsize=7.2)
    ax.legend(fontsize=7.3, loc='lower right')
    ax.grid(True, which='both', axis='x', alpha=0.3)

    fig.suptitle('BCIR Bare-Metal C Evaluation of LittleBit Sub-1-Bit Latent Factorization (arXiv:2506.13771v5 & LittleBit-2)',
                 fontsize=13.5, fontweight='bold', y=0.992)
    plt.tight_layout(rect=[0, 0, 1, 0.968])
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PNG, dpi=160)
    print('Saved figure:', OUT_PNG)


if __name__ == '__main__':
    make_plots()
