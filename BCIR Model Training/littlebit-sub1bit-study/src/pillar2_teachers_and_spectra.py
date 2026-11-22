"""Pillar 2: Train native-C BCIR FP32 teacher models (pilot192 0.86M, wide512 5.50M),
audit saved wedge-pr818 0.53M LM/KV teachers, and run full spectral & geometric
reconstruction analysis (LittleBit-v1 Dual-SVID vs LittleBit-2 Joint-ITQ vs Tiny-Rank FP16 vs OneBit vs Q8).
"""
import ctypes as ct
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
    BCIR_LIB, DecoderSpec, NativeDecoder, parameter_layout,
    dual_svid_path, joint_itq_rotate, onebit_svid, bcir_q8_quantize_matrix,
    solve_lb_rank, solve_lowrank_fp16_rank, PROJ_NAMES,
)
from bcir.hosted.models.native import NativeAdamW

TEACHERS_DIR = SCRATCH / 'checkpoints'
TEACHERS_DIR.mkdir(exist_ok=True)
WT_TRAIN = np.load(SCRATCH / 'build/data/wikitext2_train.npy')
WT_VAL = np.load(SCRATCH / 'build/data/wikitext2_validation.npy')
WT_TEST = np.load(SCRATCH / 'build/data/wikitext2_test.npy')


def make_lm_batches(data, seed, n_batches, batch=4, ctx=64):
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(data) - ctx - 1, size=(n_batches, batch))
    rows = data[starts[:, :, None] + np.arange(ctx + 1)]
    return np.ascontiguousarray(rows[:, :, :-1], dtype=np.uint32), np.ascontiguousarray(rows[:, :, 1:], dtype=np.uint32)


def eval_native_lm(m, xy):
    xs, ys = xy
    vbuf = np.ctypeslib.as_array(m.buffers['activations'])
    B, T, V = m.batch_size, m.context_length, m.spec.vocab_size
    n_log = B * T * V
    ce_sum = 0.0; correct = 0; tot = 0
    for x, y in zip(xs, ys):
        xc = np.ascontiguousarray(x, dtype=np.uint32)
        rc = m.lib.bcir_decoder_forward(ct.byref(m.state), xc.ctypes.data_as(ct.POINTER(ct.c_uint32)), xc.size)
        assert rc == 0
        z = vbuf[m.activation_count - n_log:].reshape(B, T, V).astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        loss = np.log(np.exp(z).sum(axis=-1)) - np.take_along_axis(z, y[..., None], axis=-1)[..., 0]
        ce_sum += float(loss.sum())
        correct += int((z.argmax(-1) == y).sum())
        tot += y.size
    return {'ce': ce_sum / tot, 'ppl': math.exp(ce_sum / tot), 'acc': correct / tot, 'tokens': tot}


def train_bcir_teacher(name, spec, steps, peak_lr, seed=20261122):
    out_npz = TEACHERS_DIR / f'{name}.npz'
    out_json = TEACHERS_DIR / f'{name}.json'
    val_xy = make_lm_batches(WT_VAL, 671122, 16, 4, 64)
    test_xy = make_lm_batches(WT_TEST, 681122, 64, 4, 64)
    if out_npz.exists() and out_json.exists():
        print(f'[teacher {name}] already trained, loading cached metadata')
        return np.load(out_npz)['weights'], json.loads(out_json.read_text())

    train_xy = make_lm_batches(WT_TRAIN, seed, steps, 4, 64)
    m = NativeDecoder(BCIR_LIB, spec, seed=seed, batch_size=4, context_length=64, threads=2, contract='kblock+reduce+attn')
    opt = NativeAdamW(lr=peak_lr, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.1, grad_clip=1.0)
    warmup = max(30, steps // 20)
    min_lr = peak_lr * 0.1
    lrs = [
        peak_lr * (i + 1) / warmup if i < warmup
        else min_lr + 0.5 * (peak_lr - min_lr) * (1.0 + math.cos(math.pi * (i - warmup) / max(steps - warmup, 1)))
        for i in range(steps)
    ]
    t0 = time.perf_counter()
    best_ce = 1e30
    best_w = None
    best_step = 0
    hist = []
    chunk = 100
    for s0 in range(0, steps, chunk):
        s1 = min(s0 + chunk, steps)
        ev = m.run(train_xy[0][s0:s1].ravel().tolist(), train_xy[1][s0:s1].ravel().tolist(),
                   steps=s1 - s0, optimizer=opt, learning_rates=lrs[s0:s1])
        vm = eval_native_lm(m, val_xy)
        hist.append({'step': s1, 'train_loss': float(ev[-1]['loss']), 'val_ce': vm['ce'], 'val_ppl': vm['ppl']})
        if vm['ce'] < best_ce:
            best_ce = vm['ce']; best_step = s1
            best_w = np.ctypeslib.as_array(m.buffers['weights']).copy()
        if s1 % 500 == 0 or s1 == steps:
            print(f'[{name} step {s1}/{steps}] train={ev[-1]["loss"]:.3f} val_ce={vm["ce"]:.3f} ppl={vm["ppl"]:.2f} elapsed={time.perf_counter()-t0:.1f}s', flush=True)
    elapsed = time.perf_counter() - t0
    np.ctypeslib.as_array(m.buffers['weights'])[:] = best_w
    tm = eval_native_lm(m, test_xy)
    meta = {
        'name': name, 'seed': seed, 'steps': steps, 'best_step': best_step,
        'train_seconds': elapsed, 'ms_per_step': 1000.0 * elapsed / steps,
        'val_best_ce': best_ce, 'test': tm, 'history': hist,
        'params': int(best_w.size),
    }
    np.savez(out_npz, weights=best_w)
    out_json.write_text(json.dumps(meta, indent=2))
    print(f'[teacher {name} DONE] best_step={best_step} test_ce={tm["ce"]:.4f} test_ppl={tm["ppl"]:.2f} acc={tm["acc"]:.4f} ({meta["ms_per_step"]:.1f} ms/step)')
    m.close()
    return best_w, meta


def row_distortion_lambda(M):
    """LittleBit-2 Lemma 4.2 local distortion coefficient: lambda(u) = 1 - ||u||_1^2 / (r ||u||_2^2)."""
    r = M.shape[1]
    l1 = np.sum(np.abs(M), axis=1)
    l2sq = np.sum(M * M, axis=1)
    lam = 1.0 - (l1 * l1) / np.maximum(r * l2sq, 1e-18)
    return float(np.mean(lam)), float(np.max(lam))


def spectral_audit_model(name, spec, dense_w):
    layout = parameter_layout(spec)
    by_name = {nm: dense_w[off:off + cnt].reshape(sh) for nm, sh, off, cnt in layout}
    proj_map = [
        'self_attn.q_proj.weight', 'self_attn.k_proj.weight', 'self_attn.v_proj.weight',
        'self_attn.o_proj.weight', 'mlp.gate_proj.weight', 'mlp.up_proj.weight', 'mlp.down_proj.weight',
    ]
    rows = []
    for l in range(spec.n_layers):
        for p_idx, pname in enumerate(proj_map):
            W = by_name[f'model.layers.{l}.{pname}'].astype(np.float64)
            U, S, Vt = np.linalg.svd(W, full_matrices=False)
            # Fit power-law decay sigma_k ~ k^(-gamma) on k = 1 .. floor(0.8 * min_dim)
            k_fit = max(8, int(0.8 * len(S)))
            ks = np.arange(1, k_fit + 1, dtype=np.float64)
            slope, _ = np.polyfit(np.log(ks), np.log(np.maximum(S[:k_fit], 1e-12)), 1)
            gamma = float(-slope)
            e = S * S
            p = e / max(float(np.sum(e)), 1e-18)
            erank = float(math.exp(-float(np.sum(p * np.log(np.maximum(p, 1e-18))))))
            cum = np.cumsum(p)
            r90 = int(np.searchsorted(cum, 0.90) + 1)

            # Check latent geometry misalignment at 0.55 BPW (or rank=min(32, min_dim//2))
            dout, din = W.shape
            r_res = solve_lb_rank(dout, din, 0.55, residual=True, min_rank=4)
            r_1p = solve_lb_rank(dout, din, 0.55, residual=False, min_rank=4)
            r_fp16 = solve_lowrank_fp16_rank(dout, din, 0.55, min_rank=1)

            # Raw SVD U' vs Joint-ITQ rotated U' distortion lambda & kurtosis
            k_g = min(r_1p, len(S))
            rtS = np.sqrt(np.maximum(S[:k_g], 0.0))
            Up_raw = U[:, :k_g] * rtS[None, :]
            Vp_raw = Vt[:k_g, :].T * rtS[None, :]
            lam_mean_raw, lam_max_raw = row_distortion_lambda(Up_raw)
            u_flat = Up_raw.reshape(-1)
            kurt_raw = float(np.mean((u_flat - u_flat.mean()) ** 4) / max(np.var(u_flat) ** 2, 1e-18))
            Up_itq, Vp_itq = joint_itq_rotate(Up_raw, Vp_raw, iters=50, seed=42 + l * 7 + p_idx)
            lam_mean_itq, lam_max_itq = row_distortion_lambda(Up_itq)
            ui_flat = Up_itq.reshape(-1)
            kurt_itq = float(np.mean((ui_flat - ui_flat.mean()) ** 4) / max(np.var(ui_flat) ** 2, 1e-18))

            # Zero-shot reconstruction errors at 0.55 BPW budget
            wnorm = float(np.linalg.norm(W))
            # Tiny-rank FP16
            W_tr = (U[:, :r_fp16] * S[None, :r_fp16]) @ Vt[:r_fp16, :]
            err_fp16_055 = float(np.linalg.norm(W - W_tr) / wnorm)
            # LB v1 1-path
            _, _, _, _, _, W_lb1 = dual_svid_path(W, r_1p, use_itq=False)
            err_lb1_055 = float(np.linalg.norm(W - W_lb1) / wnorm)
            # LB v1 2-path res
            _, _, _, _, _, W_p1 = dual_svid_path(W, r_res, use_itq=False)
            _, _, _, _, _, W_p2 = dual_svid_path(W - W_p1, r_res, use_itq=False)
            err_lbres_055 = float(np.linalg.norm(W - (W_p1 + W_p2)) / wnorm)
            # LB v2 Joint-ITQ 1-path
            _, _, _, _, _, W_lb2_1p = dual_svid_path(W, r_1p, use_itq=True, seed=42 + l * 7 + p_idx)
            err_lb2_1p_055 = float(np.linalg.norm(W - W_lb2_1p) / wnorm)
            # LB v2 Joint-ITQ 2-path res
            _, _, _, _, _, W_q1 = dual_svid_path(W, r_res, use_itq=True, seed=42 + l * 7 + p_idx)
            _, _, _, _, _, W_q2 = dual_svid_path(W - W_q1, r_res, use_itq=True, seed=142 + l * 7 + p_idx)
            err_lb2_res_055 = float(np.linalg.norm(W - (W_q1 + W_q2)) / wnorm)
            # OneBit full-rank (~1.05 BPW) & BCIR Q8 (8.25 BPW)
            _, _, _, W_ob = onebit_svid(W)
            err_onebit = float(np.linalg.norm(W - W_ob) / wnorm)
            W_q8 = bcir_q8_quantize_matrix(W)
            err_q8 = float(np.linalg.norm(W - W_q8) / wnorm)

            rows.append({
                'model': name, 'layer': l, 'proj': PROJ_NAMES[p_idx], 'shape': [dout, din],
                'gamma': round(gamma, 4), 'erank': round(erank, 2), 'erank_frac': round(erank / min(dout, din), 4),
                'r90_frac': round(r90 / min(dout, din), 4),
                'r_fp16_055': r_fp16, 'r_1p_055': r_1p, 'r_res_055': r_res,
                'lam_mean_raw': round(lam_mean_raw, 4), 'lam_max_raw': round(lam_max_raw, 4), 'kurt_raw': round(kurt_raw, 2),
                'lam_mean_itq': round(lam_mean_itq, 4), 'lam_max_itq': round(lam_max_itq, 4), 'kurt_itq': round(kurt_itq, 2),
                'err_fp16_055': round(err_fp16_055, 4), 'err_lb1_055': round(err_lb1_055, 4),
                'err_lbres_055': round(err_lbres_055, 4), 'err_lb2_1p_055': round(err_lb2_1p_055, 4),
                'err_lb2_res_055': round(err_lb2_res_055, 4), 'err_onebit': round(err_onebit, 4),
                'err_q8': round(err_q8, 6),
            })
    return rows


def main():
    # 1. Check all 3 seeds of wedge-pr818 kv_curriculum to pick the 100%-accurate teacher
    spec128 = DecoderSpec(vocab_size=260, d_model=128, n_heads=4, n_kv_heads=4, n_layers=3, d_ff=256,
                          activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    from pillar1_setup_and_bpw import make_kv_batches, eval_native_kv
    kb4 = make_kv_batches(128, 4, 128, pairs=4, seed=20262528)
    for sd in (20261018, 20261019, 20261020):
        ck = MODULAR / f'wedge-pr818/checkpoints/kv_curriculum-attention-{sd}.npz'
        z = np.load(ck, allow_pickle=False)
        m = NativeDecoder(BCIR_LIB, spec128, seed=1, batch_size=4, context_length=128, threads=2, contract='kblock+reduce+attn')
        np.ctypeslib.as_array(m.buffers['weights'])[:] = z['weights']
        res, _ = eval_native_kv(m, kb4)
        print(f'[kv_curriculum seed {sd}] T=128 4-pair acc={res["acc"]:.4f} ce={res["ce"]:.4f} in_cand={res["in_cand_rate"]:.4f}')
        m.close()

    # 2. Train fresh native-C BCIR teachers: pilot192 (0.86M, 2000 steps) & wide512 (5.50M, 1500 steps)
    spec192 = DecoderSpec(vocab_size=260, d_model=192, n_heads=4, n_kv_heads=2, n_layers=2, d_ff=512,
                          activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    spec512 = DecoderSpec(vocab_size=260, d_model=512, n_heads=8, n_kv_heads=2, n_layers=2, d_ff=1536,
                          activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    w192, _ = train_bcir_teacher('pilot192_0.86M', spec192, steps=2000, peak_lr=1.5e-3, seed=20261122)
    w512, _ = train_bcir_teacher('wide512_5.50M', spec512, steps=1500, peak_lr=8e-4, seed=20261122)

    # 3. Spectral audit across all teachers (wedge128 LM, wedge128 KV1, pilot192, wide512)
    w128_lm = np.load(MODULAR / 'wedge-pr818/checkpoints/lm-attention-20261018.npz')['weights']
    w128_kv = np.load(MODULAR / 'wedge-pr818/checkpoints/kv_single-attention-20261018.npz')['weights']
    audit = []
    for nm, sp, ww in [
        ('wedge128_lm', spec128, w128_lm),
        ('wedge128_kv1', spec128, w128_kv),
        ('pilot192_lm', spec192, w192),
        ('wide512_lm', spec512, w512),
    ]:
        rows = spectral_audit_model(nm, sp, ww)
        audit.extend(rows)
        g_avg = np.mean([r['gamma'] for r in rows])
        er_avg = np.mean([r['erank_frac'] for r in rows])
        l_raw = np.mean([r['lam_mean_raw'] for r in rows])
        l_itq = np.mean([r['lam_mean_itq'] for r in rows])
        e_fp16 = np.mean([r['err_fp16_055'] for r in rows])
        e_lb1 = np.mean([r['err_lb1_055'] for r in rows])
        e_lbr = np.mean([r['err_lbres_055'] for r in rows])
        e_lb2r = np.mean([r['err_lb2_res_055'] for r in rows])
        e_ob = np.mean([r['err_onebit'] for r in rows])
        print(f'[SPECTRA {nm:13s}] gamma={g_avg:.3f} erank/d={er_avg:.3f} | lam: {l_raw:.3f}->{l_itq:.3f} | '
              f'0.55BPW FrobErr: FP16={e_fp16:.3f} LBv1-1p={e_lb1:.3f} LBv1-res={e_lbr:.3f} LBv2-res={e_lb2r:.3f} | OneBit={e_ob:.3f}')

    (SCRATCH / 'results/pillar2_spectra_audit.json').write_text(json.dumps(audit, indent=2))
    print('Saved pillar2_spectra_audit.json')


if __name__ == '__main__':
    main()
