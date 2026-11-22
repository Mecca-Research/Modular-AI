"""Pillar 4: Comprehensive bare-metal C empirical evaluation of LittleBit (arXiv:2506.13771v5)
and LittleBit-2 (arXiv:2603.00042v2) on BCIR-generated models:
  - Suite A: Main sub-1-bit frontier on BCIR-wide512 (5.50M GQA 4:1) & BCIR-pilot192 (0.86M GQA 2:1)
  - Suite B: Capability stress test on saved Modular-AI BCIR-wedge128 (0.53M): LM vs kv_single vs kv_curriculum + counterfactuals
  - Suite C: Full controlled ablation matrix (Residual vs 1-path, h/g/l scales, SmoothSign vs STE,
             from-scratch vs PTQ vs CE-QAT vs KD-QAT, GQA kv_factor, MLP-only vs Attn-only sensitivity).
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
import numpy as np

SCRATCH = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
MODULAR = Path(os.environ.get('MODULAR_TRAINING_DIR', str(Path(__file__).resolve().parents[2])))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lb_harness import (
    DecoderSpec, LBModel, LBAdamWCfg, make_proj_cfgs,
    LB_IGNORE_TARGET, LB_MODE_DENSE_FP32, LB_MODE_LOWRANK_FP16,
    LB_MODE_ONEBIT_FULL, LB_MODE_LITTLEBIT_1P, LB_MODE_LITTLEBIT_RES,
    LB_SURR_SMOOTHSIGN_100, LB_SURR_SMOOTHSIGN_10, LB_SURR_CLIPPED_STE, LB_SURR_UNCLIPPED_STE,
    LB_SCALE_ALL, LB_SCALE_ROW, LB_SCALE_COL,
    LB_OBJ_CE, LB_OBJ_KD_KL, LB_OBJ_KD_FULL, LB_OBJ_CE_PLUS_KD,
)

WT_TRAIN = np.load(SCRATCH / 'build/data/wikitext2_train.npy')
WT_VAL = np.load(SCRATCH / 'build/data/wikitext2_validation.npy')
WT_TEST = np.load(SCRATCH / 'build/data/wikitext2_test.npy')


def make_lm_batches(data, seed, n_batches, batch=4, ctx=64):
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(data) - ctx - 1, size=(n_batches, batch))
    rows = data[starts[:, :, None] + np.arange(ctx + 1)]
    return np.ascontiguousarray(rows[:, :, :-1], dtype=np.uint32), np.ascontiguousarray(rows[:, :, 1:], dtype=np.uint32)


def eval_lb_lm(model: LBModel, xy):
    xs, ys = xy
    ce_sum = 0.0; correct = 0; tot = 0
    for x, y in zip(xs, ys):
        z = model.forward(x).astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        loss = np.log(np.exp(z).sum(axis=-1)) - np.take_along_axis(z, y[..., None], axis=-1)[..., 0]
        ce_sum += float(loss.sum())
        correct += int((z.argmax(-1) == y).sum())
        tot += y.size
    return {'ce': round(ce_sum / tot, 5), 'ppl': round(math.exp(ce_sum / tot), 4), 'acc': round(correct / tot, 5), 'tokens': tot}


def cosine_lrs(steps, peak_lr, min_ratio=0.1, warmup_frac=0.05):
    wu = max(5, int(steps * warmup_frac))
    lo = peak_lr * min_ratio
    return np.array([
        peak_lr * (i + 1) / wu if i < wu
        else lo + 0.5 * (peak_lr - lo) * (1.0 + math.cos(math.pi * (i - wu) / max(steps - wu, 1)))
        for i in range(steps)
    ], dtype=np.float64)


def run_lm_case(label, spec, teacher_model, teacher_w, mode, target_bpw,
                train_xy, val_xy, test_xy, qat_steps=300, peak_lr=5e-4,
                use_itq=False, naive_svd=False, init_mode='dual_svid', q8_dense=False,
                surrogate=LB_SURR_SMOOTHSIGN_100, smooth_k=100.0,
                objective=LB_OBJ_KD_FULL, kd_lambda=10.0,
                scale_flags=LB_SCALE_ALL, kv_factor=1.0, min_rank=1,
                only_mlp=False, only_attn=False, freeze_non_linear=1, seed=20261122):
    pc = make_proj_cfgs(spec, mode, target_bpw=target_bpw, kv_factor=kv_factor,
                        scale_flags=scale_flags, fp16_round=1, min_rank=min_rank,
                        only_mlp=only_mlp, only_attn=only_attn)
    m = LBModel(spec, 4, 64, pc, surrogate=surrogate, smooth_k=smooth_k,
                objective=objective, kd_lambda=kd_lambda, threads=2, use_kblock=1)
    t_init0 = time.perf_counter()
    recon = m.load_from_dense_bcir(teacher_w, init_mode=init_mode, use_itq=use_itq,
                                   itq_iters=50, seed=seed, naive_svd=naive_svd, q8_dense=q8_dense)
    init_s = time.perf_counter() - t_init0
    bpw_info = m.realized_bpw_summary(scale_bits=16)
    ptq_val = eval_lb_lm(m, val_xy)
    ptq_test = eval_lb_lm(m, test_xy)

    qat_val = ptq_val
    qat_test = ptq_test
    best_val_ce = ptq_val['ce']
    best_w = m.weights_view().copy()
    best_step = 0
    evs_summary = {}

    if qat_steps > 0:
        lrs = cosine_lrs(qat_steps, peak_lr)
        opt = LBAdamWCfg(peak_lr, 0.0, 0.9, 0.999, 1e-8, 0.0, 1.0, freeze_non_linear)
        chunk = 50
        all_evs = []
        for s0 in range(0, qat_steps, chunk):
            s1 = min(s0 + chunk, qat_steps)
            evs = m.train_loop(teacher_model if objective != LB_OBJ_CE else None,
                               train_xy[0][s0:s1], train_xy[1][s0:s1], lrs[s0:s1], opt)
            all_evs.extend(evs)
            vm = eval_lb_lm(m, val_xy)
            if vm['ce'] < best_val_ce:
                best_val_ce = vm['ce']; best_step = s1; best_w = m.weights_view().copy()
        m.weights_view()[:] = best_w
        qat_val = eval_lb_lm(m, val_xy)
        qat_test = eval_lb_lm(m, test_xy)
        evs_summary = {
            'best_step': best_step,
            'median_step_ms': round(float(np.median([e['step_ms'] for e in all_evs])), 2),
            'mean_sign_flip_rate': round(float(np.mean([e['sign_flip_rate'] for e in all_evs])), 6),
            'early_sign_flip_rate': round(float(np.mean([e['sign_flip_rate'] for e in all_evs[:20]])), 6),
            'late_sign_flip_rate': round(float(np.mean([e['sign_flip_rate'] for e in all_evs[-20:]])), 6),
            'mean_active_grad_frac': round(float(np.mean([e['active_grad_frac'] for e in all_evs])), 5),
            'final_kl': round(all_evs[-1]['kl_loss'], 5),
            'final_inter_mse': round(all_evs[-1]['inter_mse'], 5),
            'mean_grad_norm': round(float(np.mean([e['grad_norm'] for e in all_evs])), 4),
        }

    mean_frob = float(np.mean([r['rel_frob_err'] for r in recon]))
    out = {
        'label': label, 'mode': mode, 'target_bpw': target_bpw,
        'realized_body_bpw': round(bpw_info['body_bpw'], 4),
        'realized_total_bpw': round(bpw_info['total_bpw_fp16_emb'], 4),
        'deploy_kib': round(bpw_info['total_bytes_fp16_emb'] / 1024.0, 2),
        'ranks_layer0': [(int(p.r1), int(p.r2)) for p in pc[:7]],
        'init_seconds': round(init_s, 2), 'init_mean_frob_err': round(mean_frob, 4),
        'ptq_val': ptq_val, 'ptq_test': ptq_test,
        'qat_val': qat_val, 'qat_test': qat_test,
        'qat_telemetry': evs_summary,
    }
    print(f"[{label:34s}] bpw={out['realized_body_bpw']:.3f}(tot={out['realized_total_bpw']:.3f}) "
          f"frob={mean_frob:.3f} | PTQ ppl={ptq_test['ppl']:7.2f} -> QAT ppl={qat_test['ppl']:7.2f} (ce={qat_test['ce']:.4f}) "
          f"| flip={evs_summary.get('mean_sign_flip_rate',0):.5f} actG={evs_summary.get('mean_active_grad_frac',0):.3f} {evs_summary.get('median_step_ms',0):.1f}ms",
          flush=True)
    m.close()
    return out


# ---------- KV Recall & Counterfactual Helpers (Suite B) ----------
def make_kv_records(n_examples, batch, ctx, pairs, seed):
    rng = np.random.default_rng(seed)
    slots = np.arange(3, ctx - 5, 4)
    recs = []
    for _ in range(n_examples // batch):
        x = rng.integers(64, 96, (batch, ctx), dtype=np.uint32)
        x[:, 0] = 256
        y = np.full((batch, ctx), LB_IGNORE_TARGET, dtype=np.uint32)
        vpos = []; qidx = []
        for b in range(batch):
            pos = rng.choice(slots, pairs, replace=False)
            keys = rng.choice(np.arange(32, 48), pairs, replace=False).astype(np.uint32)
            vals = rng.choice(np.arange(16), pairs, replace=False).astype(np.uint32)
            q = int(rng.integers(pairs))
            x[b, pos] = 258; x[b, pos + 1] = keys; x[b, pos + 2] = vals
            x[b, ctx - 2] = 257; x[b, ctx - 1] = keys[q]
            y[b, ctx - 1] = vals[q]
            vpos.append((pos + 2).tolist()); qidx.append(q)
        recs.append({'x': x, 'y': y, 'vpos': vpos, 'qidx': qidx, 'pairs': pairs})
    return recs


def swap_kv_record(rec, mode='value_swap'):
    x = rec['x'].copy(); y = rec['y'].copy(); T = x.shape[1]
    for b in range(len(x)):
        q = rec['qidx'][b]; other = (q + 1) % rec['pairs']
        if mode == 'value_swap':
            a = rec['vpos'][b][q]; c = rec['vpos'][b][other]
            x[b, a], x[b, c] = x[b, c], x[b, a]
            y[b, T - 1] = x[b, a]
        else:  # query_change
            p_new = rec['vpos'][b][other]
            x[b, T - 1] = x[b, p_new - 1]
            y[b, T - 1] = x[b, p_new]
    return {'x': x, 'y': y, 'vpos': rec['vpos'], 'qidx': rec['qidx'], 'pairs': rec['pairs']}


def eval_lb_kv(model: LBModel, recs):
    ce_sum = 0.0; correct = 0; in_cand = 0; tot = 0; preds = []; labels = []
    T = model.time_len
    for rec in recs:
        z = model.forward(rec['x'])[:, T - 1, :].astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        yy = rec['y'][:, T - 1].astype(np.int64)
        loss = np.log(np.exp(z).sum(axis=-1)) - z[np.arange(len(yy)), yy]
        pp = z.argmax(-1)
        ce_sum += float(loss.sum()); correct += int((pp == yy).sum()); tot += len(yy)
        for b in range(len(yy)):
            cand = set(int(rec['x'][b, p]) for p in rec['vpos'][b])
            in_cand += int(int(pp[b]) in cand)
        preds.extend(pp.tolist()); labels.extend(yy.tolist())
    return {'ce': round(ce_sum / tot, 5), 'acc': round(correct / tot, 4), 'in_cand': round(in_cand / tot, 4), 'n': tot}, np.array(preds), np.array(labels)


def run_suite_a(which='both'):
    print(f'\n================ SUITE A ({which}): MAIN SUB-1-BIT FRONTIER ================')
    val_xy = make_lm_batches(WT_VAL, 671122, 16, 4, 64)
    test_xy = make_lm_batches(WT_TEST, 681122, 64, 4, 64)
    qat_train_xy = make_lm_batches(WT_TRAIN, 771122, 300, 4, 64)
    out_path = SCRATCH / 'results/pillar4_suite_a.json'
    all_res = json.loads(out_path.read_text()) if out_path.exists() else {}

    cfgs = []
    if which in ('pilot', 'both'):
        cfgs.append(('pilot192_0.86M', DecoderSpec(vocab_size=260, d_model=192, n_heads=4, n_kv_heads=2, n_layers=2, d_ff=512,
                                                   activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5), 300, 8e-4))
    if which in ('wide', 'both'):
        cfgs.append(('wide512_5.50M',  DecoderSpec(vocab_size=260, d_model=512, n_heads=8, n_kv_heads=2, n_layers=2, d_ff=1536,
                                                   activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5), 120, 8e-4))

    for mname, spec, steps_qat, lr_qat in cfgs:
        print(f'\n--- Model: {mname} (steps={steps_qat}) ---')
        tw = np.load(SCRATCH / f'checkpoints/{mname}.npz')['weights']
        teacher = LBModel(spec, 4, 64, make_proj_cfgs(spec, LB_MODE_DENSE_FP32), objective=LB_OBJ_CE, threads=2)
        teacher.load_from_dense_bcir(tw)
        rows = list(all_res.get(mname, []))
        done_labels = {r['label'] for r in rows}

        def maybe_run(lbl, *a, **kw):
            if lbl in done_labels:
                print(f'[{lbl:34s}] (cached)', flush=True)
                return
            rows.append(run_lm_case(lbl, *a, **kw))
            done_labels.add(lbl)
            all_res[mname] = rows
            out_path.write_text(json.dumps(all_res, indent=2))

        maybe_run(f'{mname}_FP32_teacher', spec, teacher, tw, LB_MODE_DENSE_FP32, 32.0, qat_train_xy, val_xy, test_xy, qat_steps=0)
        maybe_run(f'{mname}_BCIR_Q8_PTQ', spec, teacher, tw, LB_MODE_DENSE_FP32, 8.25, qat_train_xy, val_xy, test_xy, qat_steps=0, q8_dense=True)
        maybe_run(f'{mname}_OneBit_1.0_KD', spec, teacher, tw, LB_MODE_ONEBIT_FULL, 1.0, qat_train_xy, val_xy, test_xy, qat_steps=steps_qat, peak_lr=lr_qat)

        bpws = (1.00, 0.70, 0.55, 0.30, 0.20) if 'wide512' in mname else (1.00, 0.70, 0.55, 0.35)
        for bpw in bpws:
            maybe_run(f'{mname}_TinyRankFP16_{bpw:.2f}', spec, teacher, tw, LB_MODE_LOWRANK_FP16, bpw,
                      qat_train_xy, val_xy, test_xy, qat_steps=steps_qat, peak_lr=lr_qat, min_rank=1)
            maybe_run(f'{mname}_LBv1_Res_{bpw:.2f}', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, bpw,
                      qat_train_xy, val_xy, test_xy, qat_steps=steps_qat, peak_lr=lr_qat, use_itq=False, min_rank=2)
            maybe_run(f'{mname}_LBv2_ITQ_Res_{bpw:.2f}', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, bpw,
                      qat_train_xy, val_xy, test_xy, qat_steps=steps_qat, peak_lr=lr_qat, use_itq=True, min_rank=2)
        teacher.close()
        print(f'Saved {mname} into pillar4_suite_a.json', flush=True)


def run_suite_c(part='all'):
    print(f'\n================ SUITE C ({part}): CONTROLLED ABLATION MATRIX (wide512 5.50M GQA 4:1) ================')
    spec = DecoderSpec(vocab_size=260, d_model=512, n_heads=8, n_kv_heads=2, n_layers=2, d_ff=1536,
                       activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    tw = np.load(SCRATCH / 'checkpoints/wide512_5.50M.npz')['weights']
    val_xy = make_lm_batches(WT_VAL, 671122, 16, 4, 64)
    test_xy = make_lm_batches(WT_TEST, 681122, 64, 4, 64)
    tr_xy = make_lm_batches(WT_TRAIN, 771122, 120, 4, 64)
    teacher = LBModel(spec, 4, 64, make_proj_cfgs(spec, LB_MODE_DENSE_FP32), objective=LB_OBJ_CE, threads=2)
    teacher.load_from_dense_bcir(tw)

    out_path = SCRATCH / 'results/pillar4_suite_c_ablations.json'
    abl = json.loads(out_path.read_text()) if out_path.exists() else {}

    def maybe_abl(group, lbl, *a, **kw):
        rows = abl.setdefault(group, [])
        if any(r['label'] == lbl for r in rows):
            print(f'[{lbl:34s}] (cached)', flush=True)
            return
        rows.append(run_lm_case(lbl, *a, **kw))
        out_path.write_text(json.dumps(abl, indent=2))

    if part in ('c1', 'all'):
        print('\n--- A1: Residual Compensation vs 1-Path (matched bit budget) ---', flush=True)
        for bpw in (0.80, 0.55, 0.30, 0.20):
            maybe_abl('A1_residual_vs_1path', f'A1_1Path_{bpw:.2f}', spec, teacher, tw, LB_MODE_LITTLEBIT_1P, bpw, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, min_rank=2)
            maybe_abl('A1_residual_vs_1path', f'A1_Res2Path_{bpw:.2f}', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, bpw, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, min_rank=2)

        print('\n--- A2: Multi-Scale Compensation (h,g,l vs h,g vs none) at 0.30 BPW ---', flush=True)
        maybe_abl('A2_multiscale', 'A2_scales_hgl', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, scale_flags=LB_SCALE_ALL, min_rank=2)
        maybe_abl('A2_multiscale', 'A2_scales_hg_only', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, scale_flags=LB_SCALE_ROW | LB_SCALE_COL, min_rank=2)
        maybe_abl('A2_multiscale', 'A2_scales_none', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, scale_flags=0, min_rank=2)

        print('\n--- A3: SmoothSign vs STE Surrogate Gradients at 0.30 BPW ---', flush=True)
        maybe_abl('A3_surrogate', 'A3_SmoothSign_k100', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, surrogate=LB_SURR_SMOOTHSIGN_100, smooth_k=100.0, min_rank=2)
        maybe_abl('A3_surrogate', 'A3_SmoothSign_k10',  spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, surrogate=LB_SURR_SMOOTHSIGN_10, smooth_k=10.0, min_rank=2)
        maybe_abl('A3_surrogate', 'A3_Clipped_STE',     spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, surrogate=LB_SURR_CLIPPED_STE, smooth_k=1.0, min_rank=2)
        maybe_abl('A3_surrogate', 'A3_Unclipped_STE',   spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, surrogate=LB_SURR_UNCLIPPED_STE, smooth_k=1.0, min_rank=2)
        maybe_abl('A3_surrogate', 'A3_ITQ_SmoothSign_k100', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, use_itq=True, surrogate=LB_SURR_SMOOTHSIGN_100, smooth_k=100.0, min_rank=2)
        maybe_abl('A3_surrogate', 'A3_ITQ_Clipped_STE',     spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, use_itq=True, surrogate=LB_SURR_CLIPPED_STE, smooth_k=1.0, min_rank=2)

    if part in ('c2', 'all'):
        print('\n--- A4: Initialization & Objective Ablation at 0.30 BPW ---', flush=True)
        tr_xy300 = make_lm_batches(WT_TRAIN, 771122, 300, 4, 64)
        maybe_abl('A4_init_and_objective', 'A4_FromScratch_Random_CE', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=1e-3, init_mode='random', objective=LB_OBJ_CE, freeze_non_linear=0, surrogate=LB_SURR_CLIPPED_STE, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_NaiveSVD_KD', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, naive_svd=True, objective=LB_OBJ_KD_FULL, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_DualSVID_CE_only', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, objective=LB_OBJ_CE, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_DualSVID_KL_only', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, objective=LB_OBJ_KD_KL, kd_lambda=0.0, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_DualSVID_KL_InterMSE', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, objective=LB_OBJ_KD_FULL, kd_lambda=10.0, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_DualSVID_CE_KL_MSE', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, objective=LB_OBJ_CE_PLUS_KD, kd_lambda=10.0, min_rank=2)
        maybe_abl('A4_init_and_objective', 'A4_DualSVID_300step_check', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy300, val_xy, test_xy, qat_steps=300, peak_lr=8e-4, objective=LB_OBJ_KD_FULL, kd_lambda=10.0, min_rank=2)

        print('\n--- A5: GQA kv_factor Ablation (4:1 GQA) at 0.30 BPW ---', flush=True)
        maybe_abl('A5_gqa_kv_factor', 'A5_kvfactor_1.0', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, kv_factor=1.0, min_rank=2)
        maybe_abl('A5_gqa_kv_factor', 'A5_kvfactor_2.0', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, kv_factor=2.0, min_rank=2)
        maybe_abl('A5_gqa_kv_factor', 'A5_kvfactor_4.0', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, kv_factor=4.0, min_rank=2)

        print('\n--- A6: Layer Sensitivity (MLP-only vs Attn-only vs All) at 0.30 BPW ---', flush=True)
        maybe_abl('A6_layer_sensitivity', 'A6_MLP_only_0.30', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, only_mlp=True, min_rank=2)
        maybe_abl('A6_layer_sensitivity', 'A6_Attn_only_0.30', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, only_attn=True, min_rank=2)
        maybe_abl('A6_layer_sensitivity', 'A6_All_layers_0.30', spec, teacher, tw, LB_MODE_LITTLEBIT_RES, 0.30, tr_xy, val_xy, test_xy, qat_steps=120, peak_lr=8e-4, min_rank=2)

    teacher.close()
    print(f'Saved Suite C ({part}) into pillar4_suite_c_ablations.json', flush=True)


if __name__ == '__main__':
    pa = argparse.ArgumentParser()
    pa.add_argument('--suite', choices=['a_pilot', 'a_wide', 'a', 'b', 'c1', 'c2', 'c', 'all'], default='all')
    args = pa.parse_args()
    if args.suite == 'a_pilot': run_suite_a('pilot')
    elif args.suite == 'a_wide': run_suite_a('wide')
    elif args.suite in ('a', 'all'): run_suite_a('both')
    if args.suite in ('b', 'all'): run_suite_b()
    if args.suite == 'c1': run_suite_c('c1')
    elif args.suite == 'c2': run_suite_c('c2')
    elif args.suite in ('c', 'all'): run_suite_c('all')
