"""Prepare WikiText-2 byte splits, verify Modular-AI wedge-pr818 attention checkpoints
in current BCIR native C runtime, and compute Pillar 1 analytical BPW / KV-cache bounds.
"""
import ctypes as ct
import json
import math
import os
import sys
import time
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq

BCIR_REPO = Path(os.environ.get('BCIR_REPO', str(Path(__file__).resolve().parents[4] / 'Binary-Correspondence-Intermediate-Representation')))
MODULAR = Path(os.environ.get('MODULAR_TRAINING_DIR', str(Path(__file__).resolve().parents[2])))
SCRATCH = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BCIR_REPO))

from bcir.frontends.models.decode import DecoderSpec
from bcir.hosted.models.native import NativeDecoder, NativeAdamW, parameter_layout

LIB = SCRATCH / 'build/bcir_decoder_train.so'

# 1. Extract WikiText-2 byte arrays once
data_dir = SCRATCH / 'build/data'
data_dir.mkdir(exist_ok=True)
wt_src = MODULAR / 'gp-global/data'
wt_stats = {}
for split in ('train', 'validation', 'test'):
    out_p = data_dir / f'wikitext2_{split}.npy'
    if not out_p.exists():
        tbl = pq.read_table(wt_src / f'wikitext2-{split}.parquet', columns=['text'])
        txt = '\n'.join(tbl['text'].to_pylist())
        arr = np.frombuffer(txt.encode('utf-8'), dtype=np.uint8).astype(np.uint32)
        np.save(out_p, arr)
    arr = np.load(out_p)
    wt_stats[split] = {'bytes': int(arr.size), 'min': int(arr.min()), 'max': int(arr.max())}
print('WikiText-2 byte splits:', json.dumps(wt_stats))

# 2. Verify saved Modular-AI wedge-pr818 checkpoints in BCIR NativeDecoder
def eval_native_lm(m, data, seed=20262164, n_batches=16, batch=4, ctx=64):
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(data) - ctx - 1, size=(n_batches, batch))
    rows = data[starts[:, :, None] + np.arange(ctx + 1)]
    xs, ys = rows[:, :, :-1].astype(np.uint32), rows[:, :, 1:].astype(np.uint32)
    ce_sum = 0.0
    correct = 0
    total = 0
    vbuf = np.ctypeslib.as_array(m.buffers['activations'])
    n_log = batch * ctx * m.spec.vocab_size
    for x, y in zip(xs, ys):
        xc = np.ascontiguousarray(x, dtype=np.uint32)
        rc = m.lib.bcir_decoder_forward(ct.byref(m.state), xc.ctypes.data_as(ct.POINTER(ct.c_uint32)), xc.size)
        assert rc == 0, rc
        z = vbuf[m.activation_count - n_log:].reshape(batch, ctx, m.spec.vocab_size).astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        loss = np.log(np.exp(z).sum(axis=-1)) - np.take_along_axis(z, y[..., None], axis=-1)[..., 0]
        ce_sum += float(loss.sum())
        correct += int((z.argmax(-1) == y).sum())
        total += y.size
    return {'ce': ce_sum / total, 'ppl': math.exp(ce_sum / total), 'acc': correct / total, 'tokens': total}

def make_kv_batches(n_examples, batch, ctx, pairs, seed):
    assert n_examples % batch == 0
    rng = np.random.default_rng(seed)
    out = []
    slots = np.arange(3, ctx - 5, 4)
    for _ in range(n_examples // batch):
        x = rng.integers(64, 96, (batch, ctx), dtype=np.uint32)
        x[:, 0] = 256
        y = np.full((batch,), -1, dtype=np.int64)
        vpos = []
        qidx = []
        for b in range(batch):
            pos = rng.choice(slots, pairs, replace=False)
            keys = rng.choice(np.arange(32, 48), pairs, replace=False).astype(np.uint32)
            vals = rng.choice(np.arange(16), pairs, replace=False).astype(np.uint32)
            q = int(rng.integers(pairs))
            x[b, pos] = 258
            x[b, pos + 1] = keys
            x[b, pos + 2] = vals
            x[b, ctx - 2] = 257
            x[b, ctx - 1] = keys[q]
            y[b] = int(vals[q])
            vpos.append((pos + 2).tolist())
            qidx.append(q)
        out.append((x, y, vpos, qidx))
    return out

def eval_native_kv(m, batches_kv):
    vbuf = np.ctypeslib.as_array(m.buffers['activations'])
    B, T, V = m.batch_size, m.context_length, m.spec.vocab_size
    n_log = B * T * V
    ce_sum = 0.0
    correct = 0
    in_set = 0
    total = 0
    preds = []
    for x, y, vpos, _ in batches_kv:
        xc = np.ascontiguousarray(x, dtype=np.uint32)
        rc = m.lib.bcir_decoder_forward(ct.byref(m.state), xc.ctypes.data_as(ct.POINTER(ct.c_uint32)), xc.size)
        assert rc == 0, rc
        z = vbuf[m.activation_count - n_log:].reshape(B, T, V)[:, T - 1, :].astype(np.float64)
        z -= z.max(axis=-1, keepdims=True)
        loss = np.log(np.exp(z).sum(axis=-1)) - z[np.arange(B), y]
        pp = z.argmax(-1)
        ce_sum += float(loss.sum())
        correct += int((pp == y).sum())
        for b in range(B):
            cand = set(int(x[b, p]) for p in vpos[b])
            in_set += int(int(pp[b]) in cand)
        preds.extend(pp.tolist())
        total += B
    return {'ce': ce_sum / total, 'acc': correct / total, 'in_cand_rate': in_set / total, 'n': total}, np.array(preds)

spec128 = DecoderSpec(vocab_size=260, d_model=128, n_heads=4, n_kv_heads=4, n_layers=3, d_ff=256,
                      activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
wt_test = np.load(data_dir / 'wikitext2_test.npy')

ck_dir = MODULAR / 'wedge-pr818/checkpoints'
for ck_name, task in [
    ('lm-attention-20261018.npz', 'lm'),
    ('kv_single-attention-20261018.npz', 'kv1'),
    ('kv_curriculum-attention-20261018.npz', 'kv4'),
]:
    z = np.load(ck_dir / ck_name, allow_pickle=False)
    ctx = 64 if task == 'lm' else 128
    m = NativeDecoder(LIB, spec128, seed=1, batch_size=4, context_length=ctx, threads=2, contract='kblock+reduce+attn')
    wbuf = np.ctypeslib.as_array(m.buffers['weights'])
    assert wbuf.shape == z['weights'].shape
    wbuf[:] = z['weights']
    if task == 'lm':
        res = eval_native_lm(m, wt_test, seed=20262164, n_batches=32, batch=4, ctx=64)
        print(f'[verify {ck_name}] T=64 WT2-test:', res)
    elif task == 'kv1':
        kb = make_kv_batches(128, 4, 128, pairs=1, seed=20262228)
        res, _ = eval_native_kv(m, kb)
        print(f'[verify {ck_name}] T=128 KV-1pair:', res)
    elif task == 'kv4':
        kb = make_kv_batches(128, 4, 128, pairs=4, seed=20262528)
        res, _ = eval_native_kv(m, kb)
        print(f'[verify {ck_name}] T=128 KV-4pair:', res)
    m.close()

# 3. Pillar 1: Exact LittleBit BPW Scale-Floor & KV-Cache Compression Analysis
def lb_bpw(dout, din, r, residual=True, scale_bits=16):
    paths = 2 if residual else 1
    binary_bits = paths * r * (din + dout)
    scale_tot = paths * scale_bits * (din + dout + r)
    return (binary_bits + scale_tot) / (din * dout)

def solve_rank(dout, din, target_bpw, residual=True, scale_bits=16, min_rank=1):
    paths = 2 if residual else 1
    num = target_bpw * din * dout - paths * scale_bits * (din + dout)
    den = paths * (din + dout + scale_bits)
    r = int(math.floor(num / den))
    return max(min_rank, r), num <= 0

models_cfg = [
    ('BCIR-wedge128 (0.53M, MHA)',     128, 128,  256, 3, 260, True),
    ('BCIR-pilot192 (0.86M, GQA 2:1)', 192,  96,  512, 2, 260, True),
    ('BCIR-mid384 (3.25M, GQA 3:1)',   384, 128, 1024, 2, 260, True),
    ('BCIR-wide512 (5.50M, GQA 4:1)',  512, 128, 1536, 2, 260, True),
    ('BCIR-wide50m (50.5M, GQA 3:1)',  768, 256, 2048, 8, 260, True),
    ('OPT-1.3B (MHA)',                2048,2048, 8192,24,50272, True),
    ('Llama2-7B (MHA)',               4096,4096,11008,32,32000, False),
    ('Llama3-8B (GQA 4:1)',           4096,1024,14336,32,128256,False),
    ('Llama2-70B (GQA 8:1)',          8192,1024,28672,80,32000, False),
]

p1 = []
print('\n=== PILLAR 1: EXACT SCALE-FLOOR & REALIZED BPW LAW ===')
for name, d, dkv, ff, L, V, tied in models_cfg:
    shapes = [('q', d, d), ('k', dkv, d), ('v', dkv, d), ('o', d, d),
              ('gate', ff, d), ('up', ff, d), ('down', d, ff)]
    body_params = L * sum(r * c for _, r, c in shapes)
    emb_params = (1 if tied else 2) * V * d + (2 * L + 1) * d
    tot_params = body_params + emb_params
    floor_fp16_res = sum(32 * (r + c) for _, r, c in shapes) / sum(r * c for _, r, c in shapes)
    floor_fp16_1p  = sum(16 * (r + c) for _, r, c in shapes) / sum(r * c for _, r, c in shapes)
    floor_fp32_res = sum(64 * (r + c) for _, r, c in shapes) / sum(r * c for _, r, c in shapes)
    kv_floor_res   = 32 * (dkv + d) / (dkv * d)
    # Check realized BPW at target 0.10, 0.30, 0.55 with min_split_dim=8 (repo default) and min_rank=1
    targets = {}
    for tb in (0.10, 0.30, 0.55, 1.00):
        for min_r in (1, 8):
            bits_body = 0
            clamped = 0
            kv_rank = 0
            for nm, ro, co in shapes:
                r, neg = solve_rank(ro, co, tb, residual=True, scale_bits=16, min_rank=min_r)
                if neg or r == min_r:
                    clamped += 1
                bits_body += L * (2 * r * (ro + co) + 32 * (ro + co) + 32 * r)
                if nm == 'k':
                    kv_rank = r
            body_bpw = bits_body / body_params
            tot_bpw_fp16emb = (bits_body + 16 * emb_params) / tot_params
            kv_ratio_2path = dkv / (2 * kv_rank)
            kv_ratio_2path_kv4 = dkv / (2 * kv_rank * 4)
            targets[f't{tb:.2f}_minr{min_r}'] = {
                'realized_body_bpw': round(body_bpw, 4),
                'realized_total_bpw_fp16emb': round(tot_bpw_fp16emb, 4),
                'clamped_layers_of_7': clamped,
                'k_rank_per_path': kv_rank,
                'kv_cache_ratio_2path_kv1': round(kv_ratio_2path, 3),
                'kv_cache_ratio_2path_kv4': round(kv_ratio_2path_kv4, 3),
            }
    row = {
        'model': name, 'd': d, 'dkv': dkv, 'ff': ff, 'L': L, 'V': V,
        'body_params': body_params, 'emb_pct': round(100 * emb_params / tot_params, 2),
        'scale_floor_fp16_res': round(floor_fp16_res, 4),
        'scale_floor_fp16_1p': round(floor_fp16_1p, 4),
        'scale_floor_fp32_res': round(floor_fp32_res, 4),
        'kv_scale_floor_fp16_res': round(kv_floor_res, 4),
        'targets': targets,
    }
    p1.append(row)
    t01_r8 = targets['t0.10_minr8']
    t03_r8 = targets['t0.30_minr8']
    t055_r8 = targets['t0.55_minr8']
    print(f"{name:32s} | floor_res(fp16)={floor_fp16_res:.4f} kv_floor={kv_floor_res:.4f} | "
          f"t0.10(min8)->body={t01_r8['realized_body_bpw']:.3f} tot={t01_r8['realized_total_bpw_fp16emb']:.3f} | "
          f"t0.55->body={t055_r8['realized_body_bpw']:.3f} kv_ratio(2p,kv1)={t055_r8['kv_cache_ratio_2path_kv1']:.2f}x kv4={t055_r8['kv_cache_ratio_2path_kv4']:.2f}x")

(SCRATCH / 'results/pillar1_bpw_analysis.json').write_text(json.dumps(p1, indent=2))
