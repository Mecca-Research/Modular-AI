"""Test authentic saved Modular-AI ordered-attention-20261018.npz (100% teacher + 100% value_swap)
under BCIR Q8, OneBit, TinyRank FP16, LittleBit-v1, LittleBit-2 (Joint-ITQ), MLP-only, and Attn-only.
"""
import json
import os
import sys
from pathlib import Path
import numpy as np

SCRATCH = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
MODULAR = Path(os.environ.get('MODULAR_TRAINING_DIR', str(Path(__file__).resolve().parents[2])))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lb_harness import (
    DecoderSpec, LBModel, LBAdamWCfg, make_proj_cfgs,
    LB_IGNORE_TARGET, LB_MODE_DENSE_FP32, LB_MODE_LOWRANK_FP16,
    LB_MODE_ONEBIT_FULL, LB_MODE_LITTLEBIT_1P, LB_MODE_LITTLEBIT_RES,
    LB_SURR_SMOOTHSIGN_100, LB_OBJ_CE, LB_OBJ_KD_FULL,
)
from pillar4_experiments import cosine_lrs


def make_ordered_records(n_examples, batch, ctx, seed):
    rng = np.random.default_rng(seed)
    recs = []
    for _ in range(n_examples // batch):
        x = rng.integers(0, 16, (batch, ctx), dtype=np.uint32)
        x[:, 0] = 256
        x[:, ctx - 1] = 257
        y = np.full((batch, ctx), LB_IGNORE_TARGET, dtype=np.uint32)
        y[:, ctx - 1] = x[:, 1]
        recs.append({'x': x, 'y': y})
    return recs


def swap_ordered_record(rec):
    x = rec['x'].copy(); y = rec['y'].copy(); T = x.shape[1]
    for b in range(len(x)):
        choices = np.flatnonzero((x[b, 2:T - 1] != x[b, 1]) & (x[b, 2:T - 1] < 16)) + 2
        c = int(choices[0])
        x[b, 1], x[b, c] = x[b, c], x[b, 1]
        y[b, T - 1] = x[b, 1]
    return {'x': x, 'y': y}


def eval_ordered(model: LBModel, recs):
    ce_sum = 0.0; correct = 0; tot = 0; preds = []; labels = []
    T = model.time_len
    for rec in recs:
        z = model.forward(rec['x'])[:, T - 1, :].astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        yy = rec['y'][:, T - 1].astype(np.int64)
        loss = np.log(np.exp(z).sum(axis=-1)) - z[np.arange(len(yy)), yy]
        pp = z.argmax(-1)
        ce_sum += float(loss.sum()); correct += int((pp == yy).sum()); tot += len(yy)
        preds.extend(pp.tolist()); labels.extend(yy.tolist())
    return {'ce': round(ce_sum / tot, 5), 'acc': round(correct / tot, 4), 'n': tot}, np.array(preds), np.array(labels)


def main():
    spec128 = DecoderSpec(vocab_size=260, d_model=128, n_heads=4, n_kv_heads=4, n_layers=3, d_ff=256,
                          activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    tw = np.load(MODULAR / 'wedge-pr818/checkpoints/ordered-attention-20261018.npz')['weights']
    ctx = 128
    teacher = LBModel(spec128, 4, ctx, make_proj_cfgs(spec128, LB_MODE_DENSE_FP32), objective=LB_OBJ_CE, threads=2)
    teacher.load_from_dense_bcir(tw)
    tr_recs = make_ordered_records(250 * 4, 4, ctx, seed=881199)
    val_recs = make_ordered_records(64, 4, ctx, seed=30528)
    test_recs = make_ordered_records(128, 4, ctx, seed=20262228)
    swap_recs = [swap_ordered_record(r) for r in test_recs]
    tr_x = np.ascontiguousarray(np.stack([r['x'] for r in tr_recs]), dtype=np.uint32)
    tr_y = np.ascontiguousarray(np.stack([r['y'] for r in tr_recs]), dtype=np.uint32)

    print('=== SUITE B ADDENDUM: SAVED ordered-attention-20261018.npz (T=128 + value_swap) ===')
    rows = []
    variants = [
        ('FP32_teacher', LB_MODE_DENSE_FP32, 32.0, 0, False, False, False, False),
        ('BCIR_Q8_PTQ', LB_MODE_DENSE_FP32, 8.50, 0, False, True, False, False),
        ('OneBit_Full_KD', LB_MODE_ONEBIT_FULL, 1.0, 250, False, False, False, False),
        ('TinyRankFP16_0.75', LB_MODE_LOWRANK_FP16, 0.75, 250, False, False, False, False),
        ('LBv1_1P_1.00', LB_MODE_LITTLEBIT_1P, 1.00, 250, False, False, False, False),
        ('LBv1_1P_0.75', LB_MODE_LITTLEBIT_1P, 0.75, 250, False, False, False, False),
        ('LBv1_Res_0.75', LB_MODE_LITTLEBIT_RES, 0.75, 250, False, False, False, False),
        ('LBv2_ITQ_1P_0.75', LB_MODE_LITTLEBIT_1P, 0.75, 250, True, False, False, False),
        ('LBv2_ITQ_Res_0.75', LB_MODE_LITTLEBIT_RES, 0.75, 250, True, False, False, False),
        ('LBv1_MLPonly_0.75', LB_MODE_LITTLEBIT_1P, 0.75, 250, False, False, True, False),
        ('LBv1_AttnOnly_0.75', LB_MODE_LITTLEBIT_1P, 0.75, 250, False, False, False, True),
    ]
    for vname, mode, bpw, qsteps, uitq, q8f, omlp, oattn in variants:
        pc = make_proj_cfgs(spec128, mode, target_bpw=bpw, min_rank=2, only_mlp=omlp, only_attn=oattn)
        m = LBModel(spec128, 4, ctx, pc, surrogate=LB_SURR_SMOOTHSIGN_100, smooth_k=100.0,
                    objective=LB_OBJ_KD_FULL, kd_lambda=10.0, threads=2)
        m.load_from_dense_bcir(tw, use_itq=uitq, q8_dense=q8f)
        bpw_info = m.realized_bpw_summary()
        ptq_m, _, _ = eval_ordered(m, test_recs)
        if qsteps > 0:
            lrs = cosine_lrs(qsteps, 8e-4)
            opt = LBAdamWCfg(8e-4, 0.0, 0.9, 0.999, 1e-8, 0.0, 1.0, 1, 1 if q8f else 0)
            best_ce = eval_ordered(m, val_recs)[0]['ce']
            best_w = m.weights_view().copy()
            for s0 in range(0, qsteps, 50):
                s1 = min(s0 + 50, qsteps)
                m.train_loop(teacher, tr_x[s0:s1], tr_y[s0:s1], lrs[s0:s1], opt)
                vc = eval_ordered(m, val_recs)[0]['ce']
                if vc < best_ce:
                    best_ce = vc; best_w = m.weights_view().copy()
            m.weights_view()[:] = best_w
        qat_m, pp, yy = eval_ordered(m, test_recs)
        sm, sp, sy = eval_ordered(m, swap_recs)
        both_ok = round(float(np.mean((pp == yy) & (sp == sy))), 4)
        chg = round(float(np.mean(pp != sp)), 4)
        rec = {
            'variant': vname, 'realized_body_bpw': round(bpw_info['body_bpw'], 4),
            'ptq': ptq_m, 'qat': qat_m,
            'value_swap': {'changed_acc': sm['acc'], 'both_correct': both_ok, 'pred_change_rate': chg},
        }
        rows.append(rec)
        print(f"  [{vname:20s}] bpw={rec['realized_body_bpw']:.3f} | PTQ acc={ptq_m['acc']:.4f} -> QAT acc={qat_m['acc']:.4f} (ce={qat_m['ce']:.4f}) | vswap_both={both_ok:.4f} chg={chg:.4f}", flush=True)
        m.close()
    teacher.close()
    (SCRATCH / 'results/pillar4_suite_b_ordered.json').write_text(json.dumps(rows, indent=2))
    print('Saved pillar4_suite_b_ordered.json')


if __name__ == '__main__':
    main()
