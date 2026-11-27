"""Pillar 5: Bare-metal C inference kernel benchmark (Portable bitpack, AVX-512 Sign-XOR,
AVX-512 4-bit LUT T-MAC, BCIR FP32 kblock, BCIR BF16, AVX-512 FP16 stream, BCIR Q8,
and Low-Rank FP32 at matched rank) across BCIR small/medium/50M AND Llama2-7B/70B shapes,
and decode (N=1) vs prefill (N=4, 16, 64, 256) batch sizes + QAT training overhead.
"""
import ctypes as ct
import json
import os
import sys
import time
from pathlib import Path
import numpy as np

SCRATCH = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lb_harness import (
    get_lib, LBBenchResult, solve_lb_rank,
    LB_KERN_PORTABLE_BITPACK, LB_KERN_AVX512_SIGNXOR, LB_KERN_AVX512_LUT4,
    LB_KERN_DENSE_FP32_BCIR, LB_KERN_DENSE_BF16_BCIR, LB_KERN_DENSE_FP16_STREAM,
    LB_KERN_DENSE_Q8_BCIR, LB_KERN_LOWRANK_FP32,
    LBModel, LBAdamWCfg, DecoderSpec, make_proj_cfgs,
    LB_MODE_DENSE_FP32, LB_MODE_LITTLEBIT_RES, LB_MODE_LITTLEBIT_1P, LB_MODE_LOWRANK_FP16,
    LB_OBJ_CE, LB_OBJ_KD_FULL,
)


def run_kernel_suite():
    lib = get_lib()
    kernels = [
        ('dense_fp32_bcir', LB_KERN_DENSE_FP32_BCIR),
        ('dense_bf16_bcir', LB_KERN_DENSE_BF16_BCIR),
        ('dense_fp16_stream', LB_KERN_DENSE_FP16_STREAM),
        ('dense_q8_bcir', LB_KERN_DENSE_Q8_BCIR),
        ('lowrank_fp32', LB_KERN_LOWRANK_FP32),
        ('lb_portable_bitpack', LB_KERN_PORTABLE_BITPACK),
        ('lb_avx512_signxor', LB_KERN_AVX512_SIGNXOR),
        ('lb_avx512_lut4', LB_KERN_AVX512_LUT4),
    ]

    # 1. Layer-size & BPW sweep at decode (N=1) and prefill (N=64)
    # Shapes cover BCIR 0.53M -> 50M AND Llama2-7B / Llama2-70B (paper Fig. 6!)
    configs = [
        # (label, din, dout, N, target_bpw, residual, iters)
        ('bcir_wedge128_mlp',  128,  256,  1, 0.55, 1, 80),
        ('bcir_pilot192_mlp',  192,  512,  1, 0.55, 1, 60),
        ('bcir_wide512_q',     512,  512,  1, 0.55, 1, 40),
        ('bcir_wide512_mlp',   512, 1536,  1, 0.55, 1, 30),
        ('bcir_wide512_mlp',   512, 1536,  1, 0.30, 1, 30),
        ('bcir_wide512_mlp',   512, 1536, 64, 0.55, 1, 15),
        ('bcir_wide512_mlp',   512, 1536, 64, 0.30, 1, 15),
        ('bcir_wide50m_mlp',   768, 2048,  1, 0.55, 1, 25),
        ('bcir_wide50m_mlp',   768, 2048,  1, 0.30, 1, 25),
        ('bcir_wide50m_mlp',   768, 2048,  1, 0.15, 1, 25),
        ('bcir_wide50m_mlp',   768, 2048, 16, 0.30, 1, 15),
        ('bcir_wide50m_mlp',   768, 2048, 64, 0.30, 1, 10),
        ('bcir_wide50m_mlp',   768, 2048,256, 0.30, 1,  7),
        # Paper-scale layers (Llama2-7B & Llama2-70B MLP from Paper Fig. 6!)
        ('llama2_7b_mlp',     4096,11008,  1, 1.00, 1,  9),
        ('llama2_7b_mlp',     4096,11008,  1, 0.55, 1,  9),
        ('llama2_7b_mlp',     4096,11008,  1, 0.30, 1,  9),
        ('llama2_7b_mlp',     4096,11008,  1, 0.10, 1,  9),
        ('llama2_7b_mlp',     4096,11008, 16, 0.30, 1,  5),
        ('llama2_7b_mlp',     4096,11008, 64, 0.30, 1,  5),
        ('llama2_70b_mlp',    8192,28672,  1, 0.55, 1,  5),
        ('llama2_70b_mlp',    8192,28672,  1, 0.30, 1,  5),
        ('llama2_70b_mlp',    8192,28672,  1, 0.10, 1,  5),
        # Compare 1-path vs 2-path Residual at matched 0.30 BPW on wide50m & 7B
        ('bcir_wide50m_1p',    768, 2048,  1, 0.30, 0, 25),
        ('llama2_7b_mlp_1p',  4096,11008,  1, 0.30, 0,  9),
    ]

    results = []
    print('=== PILLAR 5: BARE-METAL C KERNEL BENCHMARK (1T Sapphire Rapids AVX-512) ===')
    for label, din, dout, N, tbpw, res_flag, iters in configs:
        r = solve_lb_rank(dout, din, tbpw, residual=bool(res_flag), min_rank=8)
        row = {'label': label, 'din': din, 'dout': dout, 'N': N, 'target_bpw': tbpw,
               'residual': bool(res_flag), 'r_per_path': r, 'kernels': {}}
        for kname, kkind in kernels:
            # Skip ultra-slow portable kernel on giant 70B or large N>=64 7B so suite finishes fast
            if kname == 'lb_portable_bitpack' and (din >= 4096 or N >= 64):
                continue
            if kname == 'dense_bf16_bcir' and din >= 8192:
                continue
            res = LBBenchResult()
            rc = lib.lb_bench_linear_kernel(kkind, N, din, dout, r, res_flag, 1, iters, ct.byref(res))
            assert rc == 0
            row['kernels'][kname] = {
                'median_us': round(res.median_us, 2),
                'min_us': round(res.min_us, 2),
                'gflops_equiv': round(res.gflops_equiv, 2),
                'max_abs_err': float(res.max_abs_err),
                'weight_bytes': int(res.weight_bytes),
                'realized_bpw': round(res.realized_bpw, 4),
                'ideal_deploy_weight_bytes': int(res.ideal_deploy_weight_bytes),
                'ideal_deploy_bpw': round(res.ideal_deploy_bpw, 4),
            }
        fp32_us = row['kernels']['dense_fp32_bcir']['median_us']
        fp16_us = row['kernels']['dense_fp16_stream']['median_us']
        q8_us   = row['kernels']['dense_q8_bcir']['median_us']
        lr_us   = row['kernels']['lowrank_fp32']['median_us']
        lut_us  = row['kernels']['lb_avx512_lut4']['median_us']
        xor_us  = row['kernels']['lb_avx512_signxor']['median_us']
        lb_bpw  = row['kernels']['lb_avx512_lut4']['realized_bpw']
        row['speedups_lut4'] = {
            'vs_fp32': round(fp32_us / lut_us, 2),
            'vs_fp16': round(fp16_us / lut_us, 2),
            'vs_q8': round(q8_us / lut_us, 2),
            'vs_lowrank_fp32': round(lr_us / lut_us, 2),
            'vs_signxor': round(xor_us / lut_us, 2),
        }
        results.append(row)
        print(f"{label:18s} {din:4d}x{dout:<5d} N={N:3d} t={tbpw:.2f}(r={r:4d},bpw={lb_bpw:.3f}) | "
              f"FP32={fp32_us:8.1f}us FP16={fp16_us:8.1f}us Q8={q8_us:8.1f}us LR32={lr_us:7.1f}us "
              f"XOR={xor_us:8.1f}us LUT4={lut_us:7.1f}us | LUT4/FP16={fp16_us/lut_us:5.2f}x LUT4/FP32={fp32_us/lut_us:5.2f}x LUT4/LR32={lr_us/lut_us:4.2f}x",
              flush=True)

    # 2. QAT Training Step Overhead Benchmark (Dense FP32 vs LittleBit QAT-CE vs LittleBit QAT+KD)
    print('\n=== QAT TRAINING STEP OVERHEAD (2T Native C, B=4, T=64) ===')
    train_bench = []
    for name, kw in [
        ('pilot192_0.86M', dict(d_model=192, n_heads=4, n_kv_heads=2, n_layers=2, d_ff=512)),
        ('wide512_5.50M',  dict(d_model=512, n_heads=8, n_kv_heads=2, n_layers=2, d_ff=1536)),
    ]:
        spec = DecoderSpec(vocab_size=260, activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5, **kw)
        rng = np.random.default_rng(123)
        toks = rng.integers(0, 256, size=(12, 4 * 64), dtype=np.uint32)
        tgts = rng.integers(0, 256, size=(12, 4 * 64), dtype=np.uint32)
        lrs = np.full(12, 1e-4, dtype=np.float64)
        opt = LBAdamWCfg(1e-4, 0.0, 0.9, 0.95, 1e-8, 0.0, 1.0, 1, 0)
        teacher = LBModel(spec, 4, 64, make_proj_cfgs(spec, LB_MODE_DENSE_FP32), objective=LB_OBJ_CE, threads=2)
        teacher.weights_view()[:] = rng.normal(0, 0.02, teacher.param_count).astype(np.float32)

        for variant, mode, bpw, obj in [
            ('Dense_FP32_CE', LB_MODE_DENSE_FP32, 32.0, LB_OBJ_CE),
            ('LowRank_FP16_0.55_KD', LB_MODE_LOWRANK_FP16, 0.55, LB_OBJ_KD_FULL),
            ('LittleBit_1P_0.55_CE', LB_MODE_LITTLEBIT_1P, 0.55, LB_OBJ_CE),
            ('LittleBit_Res_0.55_CE', LB_MODE_LITTLEBIT_RES, 0.55, LB_OBJ_CE),
            ('LittleBit_Res_0.55_KD', LB_MODE_LITTLEBIT_RES, 0.55, LB_OBJ_KD_FULL),
            ('LittleBit_Res_0.30_KD', LB_MODE_LITTLEBIT_RES, 0.30, LB_OBJ_KD_FULL),
            ('LittleBit_Res_1.00_KD', LB_MODE_LITTLEBIT_RES, 1.00, LB_OBJ_KD_FULL),
        ]:
            m = LBModel(spec, 4, 64, make_proj_cfgs(spec, mode, target_bpw=bpw, min_rank=4), objective=obj, threads=2)
            m.weights_view()[:] = rng.normal(0, 0.02, m.param_count).astype(np.float32)
            evs = m.train_loop(teacher if obj != LB_OBJ_CE else None, toks, tgts, lrs, opt)
            med_ms = float(np.median([e['step_ms'] for e in evs[2:]]))
            bpw_info = m.realized_bpw_summary()
            rec = {
                'model': name, 'variant': variant, 'median_step_ms': round(med_ms, 2),
                'student_arena_mib': round(m.total_bytes / 2**20, 2),
                'with_teacher_mib': round((m.total_bytes + (teacher.total_bytes if obj != LB_OBJ_CE else 0)) / 2**20, 2),
                'deploy_total_mib': round(bpw_info['total_bytes_fp16_emb'] / 2**20, 3),
                'realized_body_bpw': round(bpw_info['body_bpw'], 4),
            }
            train_bench.append(rec)
            print(f"{name:15s} {variant:22s}: {med_ms:6.2f} ms/step | train_RAM={rec['with_teacher_mib']:6.2f} MiB | deploy={rec['deploy_total_mib']:.3f} MiB (body_bpw={rec['realized_body_bpw']:.3f})", flush=True)
            m.close()
        teacher.close()

    out = {'kernel_sweep': results, 'qat_training_overhead': train_bench}
    (SCRATCH / 'results/pillar5_kernel_benchmark.json').write_text(json.dumps(out, indent=2))
    print('Saved pillar5_kernel_benchmark.json')


if __name__ == '__main__':
    run_kernel_suite()
