"""Python ctypes bridge to libbcir_littlebit.so + exact Dual-SVID / Joint-ITQ / Q8 / SVD initializers
and strict numerical verification gate against BCIR bcir_decoder_train.so.
"""
import ctypes as ct
import json
import math
import os
import sys
import time
from pathlib import Path
import numpy as np

BCIR_REPO = Path(os.environ.get('BCIR_REPO', str(Path(__file__).resolve().parents[4] / 'Binary-Correspondence-Intermediate-Representation')))
SCRATCH = Path(os.environ.get('LITTLEBIT_STUDY_DIR', str(Path(__file__).resolve().parents[1])))
LIB_PATH = SCRATCH / 'build/libbcir_lab.so' if (SCRATCH / 'build/libbcir_lab.so').exists() else SCRATCH / 'build/libbcir_littlebit.so'
BCIR_LIB = SCRATCH / 'build/libbcir_lab.so'
sys.path.insert(0, str(BCIR_REPO))

from bcir.frontends.models.decode import DecoderSpec
from bcir.hosted.models.native import NativeDecoder, parameter_layout

LB_IGNORE_TARGET = 0xFFFFFFFF
LB_MODE_DENSE_FP32 = 0
LB_MODE_LOWRANK_FP16 = 1
LB_MODE_ONEBIT_FULL = 2
LB_MODE_LITTLEBIT_1P = 3
LB_MODE_LITTLEBIT_RES = 4

LB_SURR_SMOOTHSIGN_100 = 0
LB_SURR_SMOOTHSIGN_10 = 1
LB_SURR_CLIPPED_STE = 2
LB_SURR_UNCLIPPED_STE = 3
LB_SURR_SMOOTH_FWD_TEST = 4

LB_SCALE_ROW = 1
LB_SCALE_COL = 2
LB_SCALE_LATENT = 4
LB_SCALE_ALL = 7

LB_OBJ_CE = 0
LB_OBJ_KD_KL = 1
LB_OBJ_KD_FULL = 2
LB_OBJ_CE_PLUS_KD = 3

PROJ_NAMES = ('q', 'k', 'v', 'o', 'gate', 'up', 'down')

LB_KERN_PORTABLE_BITPACK = 0
LB_KERN_AVX512_SIGNXOR = 1
LB_KERN_AVX512_LUT4 = 2
LB_KERN_DENSE_FP32_BCIR = 3
LB_KERN_DENSE_BF16_BCIR = 4
LB_KERN_DENSE_FP16_STREAM = 5
LB_KERN_DENSE_Q8_BCIR = 6
LB_KERN_LOWRANK_FP32 = 7
LB_KERN_AVX512_LUT4_PACKED1B = 8
LB_KERN_AVX512_LUT4_PACKED1B_MB4 = 9


class LBProjCfg(ct.Structure):
    _fields_ = [
        ('mode', ct.c_uint32), ('din', ct.c_uint32), ('dout', ct.c_uint32),
        ('r1', ct.c_uint32), ('r2', ct.c_uint32),
        ('scale_flags', ct.c_uint32), ('fp16_round', ct.c_uint32),
    ]


class LBModelCfg(ct.Structure):
    _fields_ = [
        ('vocab', ct.c_uint32), ('width', ct.c_uint32), ('heads', ct.c_uint32),
        ('kvheads', ct.c_uint32), ('layers', ct.c_uint32), ('ff', ct.c_uint32),
        ('batch', ct.c_uint32), ('time', ct.c_uint32), ('tied', ct.c_uint32),
        ('rope_base', ct.c_double), ('rms_eps', ct.c_double),
        ('surrogate', ct.c_uint32), ('smooth_k', ct.c_double),
        ('objective', ct.c_uint32), ('kd_lambda', ct.c_double),
        ('threads', ct.c_uint32), ('use_kblock', ct.c_uint32),
    ]


class LBAdamWCfg(ct.Structure):
    _fields_ = [
        ('lr', ct.c_double), ('lr_scales', ct.c_double),
        ('beta1', ct.c_double), ('beta2', ct.c_double),
        ('epsilon', ct.c_double), ('weight_decay', ct.c_double),
        ('grad_clip', ct.c_double), ('freeze_non_linear', ct.c_int),
    ]


class LBStepMetrics(ct.Structure):
    _fields_ = [
        ('total_loss', ct.c_double), ('ce_loss', ct.c_double),
        ('kl_loss', ct.c_double), ('inter_mse', ct.c_double),
        ('grad_norm', ct.c_double), ('sign_flip_rate', ct.c_double),
        ('active_grad_frac', ct.c_double), ('step_ms', ct.c_double),
        ('step', ct.c_uint64),
    ]


class LBBenchResult(ct.Structure):
    _fields_ = [
        ('median_us', ct.c_double), ('min_us', ct.c_double),
        ('gflops_equiv', ct.c_double), ('max_abs_err', ct.c_double),
        ('weight_bytes', ct.c_size_t), ('realized_bpw', ct.c_double),
    ]


_LIB = None


def get_lib():
    global _LIB
    if _LIB is None:
        lib = ct.CDLL(str(LIB_PATH))
        lib.lb_model_create.argtypes = [ct.POINTER(LBModelCfg), ct.POINTER(LBProjCfg)]
        lib.lb_model_create.restype = ct.c_void_p
        lib.lb_model_destroy.argtypes = [ct.c_void_p]
        lib.lb_model_param_count.argtypes = [ct.c_void_p]
        lib.lb_model_param_count.restype = ct.c_size_t
        lib.lb_model_total_bytes.argtypes = [ct.c_void_p]
        lib.lb_model_total_bytes.restype = ct.c_size_t
        lib.lb_model_weights_ptr.argtypes = [ct.c_void_p]
        lib.lb_model_weights_ptr.restype = ct.POINTER(ct.c_float)
        lib.lb_model_grads_ptr.argtypes = [ct.c_void_p]
        lib.lb_model_grads_ptr.restype = ct.POINTER(ct.c_float)
        lib.lb_model_tensor_span.argtypes = [
            ct.c_void_p, ct.c_int, ct.c_uint32, ct.c_uint32, ct.c_uint32,
            ct.POINTER(ct.c_size_t), ct.POINTER(ct.c_size_t),
        ]
        lib.lb_model_tensor_span.restype = ct.c_int
        lib.lb_model_forward.argtypes = [
            ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_float), ct.POINTER(ct.c_float),
        ]
        lib.lb_model_forward.restype = ct.c_int
        lib.lb_model_loss_backward.argtypes = [
            ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
            ct.c_int, ct.POINTER(LBStepMetrics),
        ]
        lib.lb_model_loss_backward.restype = ct.c_int
        lib.lb_model_train_step.argtypes = [
            ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
            ct.POINTER(LBAdamWCfg), ct.POINTER(LBStepMetrics),
        ]
        lib.lb_model_train_step.restype = ct.c_int
        lib.lb_model_train_loop.argtypes = [
            ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
            ct.c_size_t, ct.POINTER(ct.c_double), ct.POINTER(LBAdamWCfg), ct.POINTER(LBStepMetrics),
        ]
        lib.lb_model_train_loop.restype = ct.c_int
        lib.lb_model_reset_opt.argtypes = [ct.c_void_p]
        lib.lb_bench_linear_kernel.argtypes = [
            ct.c_int, ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.c_uint32,
            ct.c_int, ct.c_uint32, ct.c_uint32, ct.POINTER(LBBenchResult),
        ]
        lib.lb_bench_linear_kernel.restype = ct.c_int
        _LIB = lib
    return _LIB


def proj_shapes_for_spec(spec: DecoderSpec):
    d = spec.d_model
    dkv = spec.n_kv_heads * (d // spec.n_heads)
    ff = spec.d_ff
    return [(d, d), (dkv, d), (dkv, d), (d, d), (ff, d), (ff, d), (d, ff)]


def solve_lb_rank(dout, din, target_bpw, residual=True, scale_bits=16, min_rank=1, max_rank=None):
    paths = 2 if residual else 1
    num = target_bpw * din * dout - paths * scale_bits * (din + dout)
    den = paths * (din + dout + scale_bits)
    r = int(math.floor(num / den))
    cap = min(din, dout) if max_rank is None else min(min(din, dout), max_rank)
    return max(min_rank, min(cap, r))


def solve_lowrank_fp16_rank(dout, din, target_bpw, min_rank=1):
    # 16 bits per entry of U[dout, r] and V[din, r]
    r = int(math.floor((target_bpw * din * dout) / (16.0 * (din + dout))))
    return max(min_rank, min(min(din, dout), r))


def realized_proj_bits(pcfg: LBProjCfg, scale_bits=16):
    din, dout = int(pcfg.din), int(pcfg.dout)
    if pcfg.mode == LB_MODE_DENSE_FP32:
        return 32 * din * dout
    if pcfg.mode == LB_MODE_LOWRANK_FP16:
        return 16 * int(pcfg.r1) * (din + dout)
    if pcfg.mode == LB_MODE_ONEBIT_FULL:
        return din * dout + scale_bits * (din + dout)
    if pcfg.mode == LB_MODE_LITTLEBIT_1P:
        r1 = int(pcfg.r1)
        sc = (scale_bits * dout if (pcfg.scale_flags & LB_SCALE_ROW) else 0) + \
             (scale_bits * din if (pcfg.scale_flags & LB_SCALE_COL) else 0) + \
             (scale_bits * r1 if (pcfg.scale_flags & LB_SCALE_LATENT) else 0)
        return r1 * (din + dout) + sc
    r1, r2 = int(pcfg.r1), int(pcfg.r2)
    sc1 = (scale_bits * dout if (pcfg.scale_flags & LB_SCALE_ROW) else 0) + \
          (scale_bits * din if (pcfg.scale_flags & LB_SCALE_COL) else 0) + \
          (scale_bits * r1 if (pcfg.scale_flags & LB_SCALE_LATENT) else 0)
    sc2 = (scale_bits * dout if (pcfg.scale_flags & LB_SCALE_ROW) else 0) + \
          (scale_bits * din if (pcfg.scale_flags & LB_SCALE_COL) else 0) + \
          (scale_bits * r2 if (pcfg.scale_flags & LB_SCALE_LATENT) else 0)
    return (r1 + r2) * (din + dout) + sc1 + sc2


# ---------- Exact Dual-SVID (LittleBit v1) & Joint-ITQ (LittleBit-2) ----------
def rank1_mag_svd(A_abs):
    """Exact best rank-1 non-negative approximation |A| ≈ s_row @ s_col^T via SVD."""
    u, s, vt = np.linalg.svd(A_abs.astype(np.float64), full_matrices=False)
    u0 = np.abs(u[:, 0])
    v0 = np.abs(vt[0, :])
    rt = math.sqrt(float(s[0])) if s[0] > 0 else 0.0
    return (u0 * rt).astype(np.float32), (v0 * rt).astype(np.float32)


def joint_itq_rotate(U_prime, V_prime, iters=50, seed=42):
    """LittleBit-2 (ICML 2026 Algorithm 1): Joint Orthogonal Procrustes alignment on Z = [U'; V'].
    Preserves U' @ V'^T = (U' R)(V' R)^T identically while aligning coordinates with {±1}^r."""
    r = U_prime.shape[1]
    Z = np.vstack([U_prime, V_prime]).astype(np.float64)
    rng = np.random.default_rng(seed)
    Q, _ = np.linalg.qr(rng.standard_normal((r, r)))
    R = Q
    for _ in range(iters):
        B = np.where(Z @ R >= 0.0, 1.0, -1.0)
        Phi, _, PsiT = np.linalg.svd(B.T @ Z, full_matrices=False)
        R = PsiT.T @ Phi.T
    return (U_prime @ R).astype(np.float32), (V_prime @ R).astype(np.float32)


def dual_svid_path(W, r, use_itq=False, itq_iters=50, scale_flags=LB_SCALE_ALL, seed=42):
    """Exact Dual-SVID (LittleBit v1 Eqs. 6-8) with optional Joint-ITQ preconditioning (LittleBit-2)."""
    W64 = W.astype(np.float64)
    U_svd, S_svd, Vt_svd = np.linalg.svd(W64, full_matrices=False)
    k = min(r, len(S_svd))
    rtS = np.sqrt(np.maximum(S_svd[:k], 0.0))
    U_p = np.zeros((W.shape[0], r), dtype=np.float64)
    V_p = np.zeros((W.shape[1], r), dtype=np.float64)
    U_p[:, :k] = U_svd[:, :k] * rtS[None, :]
    V_p[:, :k] = Vt_svd[:k, :].T * rtS[None, :]
    if use_itq and k > 1:
        U_p[:, :k], V_p[:, :k] = joint_itq_rotate(U_p[:, :k], V_p[:, :k], iters=itq_iters, seed=seed)

    h0, lu0 = rank1_mag_svd(np.abs(U_p))
    g0, lv0 = rank1_mag_svd(np.abs(V_p))
    l0 = (lu0 * lv0).astype(np.float32)

    if not (scale_flags & LB_SCALE_LATENT):
        # Absorb mean latent scale into h0 so initial magnitude stays calibrated when l=1 is ablated
        scale_fix = float(np.mean(np.abs(l0))) if l0.size else 1.0
        h0 = (h0 * math.sqrt(max(scale_fix, 1e-12))).astype(np.float32)
        g0 = (g0 * math.sqrt(max(scale_fix, 1e-12))).astype(np.float32)
        l0 = np.ones_like(l0)
    if not (scale_flags & LB_SCALE_ROW):
        h0 = np.ones_like(h0)
    if not (scale_flags & LB_SCALE_COL):
        g0 = np.ones_like(g0)

    Usign = np.where(U_p >= 0.0, 1.0, -1.0)
    Vsign = np.where(V_p >= 0.0, 1.0, -1.0)
    W_hat = (h0[:, None] * Usign) @ ((l0[:, None] * Vsign.T) * g0[None, :])
    return U_p.astype(np.float32), V_p.astype(np.float32), h0, g0, l0, W_hat.astype(np.float32)


def onebit_svid(W):
    """OneBit-style full-rank 1-bit SVID: W ≈ diag(h) sign(W) diag(g)."""
    h0, g0 = rank1_mag_svd(np.abs(W))
    W_hat = (h0[:, None] * np.where(W >= 0.0, 1.0, -1.0)) * g0[None, :]
    return W.astype(np.float32), h0, g0, W_hat.astype(np.float32)


def bcir_q8_quantize_matrix(W, group_size=32):
    """Exact BCIR Q8 block-32 power-of-2 symmetric quantization (bcir_q8_model format, 8.25 BPW)."""
    flat = W.reshape(-1).astype(np.float64)
    out = np.empty_like(flat)
    for start in range(0, flat.size, group_size):
        grp = flat[start:start + group_size]
        amax = float(np.max(np.abs(grp)))
        if amax == 0.0:
            out[start:start + group_size] = 0.0
            continue
        # BCIR Q8: power-of-two exponent such that |code| <= 127
        exp = int(math.ceil(math.log2(amax / 127.0)))
        exp = max(-300, min(300, exp))
        scale = math.ldexp(1.0, exp)
        q = np.clip(np.round(grp / scale), -127, 127)
        out[start:start + group_size] = q * scale
    return out.reshape(W.shape).astype(np.float32)


class LBModel:
    def __init__(self, spec: DecoderSpec, batch: int, time_len: int, proj_cfgs,
                 surrogate=LB_SURR_SMOOTHSIGN_100, smooth_k=100.0,
                 objective=LB_OBJ_CE, kd_lambda=10.0, threads=2, use_kblock=1):
        self.lib = get_lib()
        self.spec = spec
        self.batch = batch
        self.time_len = time_len
        self.proj_cfgs = list(proj_cfgs)
        assert len(self.proj_cfgs) == spec.n_layers * 7
        self.cfg = LBModelCfg(
            vocab=spec.vocab_size, width=spec.d_model, heads=spec.n_heads,
            kvheads=spec.n_kv_heads, layers=spec.n_layers, ff=spec.d_ff,
            batch=batch, time=time_len, tied=1 if spec.tied_embeddings else 0,
            rope_base=spec.rope_base, rms_eps=spec.rms_norm_eps,
            surrogate=surrogate, smooth_k=smooth_k,
            objective=objective, kd_lambda=kd_lambda,
            threads=threads, use_kblock=use_kblock,
        )
        arr = (LBProjCfg * len(self.proj_cfgs))(*self.proj_cfgs)
        self.ptr = self.lib.lb_model_create(ct.byref(self.cfg), arr)
        assert self.ptr, 'lb_model_create failed'
        self.param_count = int(self.lib.lb_model_param_count(self.ptr))
        self.total_bytes = int(self.lib.lb_model_total_bytes(self.ptr))

    def close(self):
        if self.ptr:
            self.lib.lb_model_destroy(self.ptr)
            self.ptr = None

    def weights_view(self):
        p = self.lib.lb_model_weights_ptr(self.ptr)
        return np.ctypeslib.as_array(p, shape=(self.param_count,))

    def grads_view(self):
        p = self.lib.lb_model_grads_ptr(self.ptr)
        return np.ctypeslib.as_array(p, shape=(self.param_count,))

    def span(self, kind, layer=0, proj=0, sub_id=0):
        off, cnt = ct.c_size_t(), ct.c_size_t()
        rc = self.lib.lb_model_tensor_span(self.ptr, kind, layer, proj, sub_id, ct.byref(off), ct.byref(cnt))
        assert rc == 0, (kind, layer, proj, sub_id)
        return int(off.value), int(cnt.value)

    def set_tensor(self, arr, kind, layer=0, proj=0, sub_id=0):
        off, cnt = self.span(kind, layer, proj, sub_id)
        flat = np.ascontiguousarray(arr, dtype=np.float32).reshape(-1)
        assert flat.size == cnt, (flat.size, cnt)
        self.weights_view()[off:off + cnt] = flat

    def load_from_dense_bcir(self, dense_weights, init_mode='dual_svid', use_itq=False,
                             itq_iters=50, seed=42, naive_svd=False, q8_dense=False):
        """Load non-linear weights verbatim and initialize all linear projections from a BCIR FP32 model."""
        layout = parameter_layout(self.spec)
        by_name = {}
        for name, shape, off, cnt in layout:
            by_name[name] = dense_weights[off:off + cnt].reshape(shape).copy()

        self.set_tensor(by_name['model.embed_tokens.weight'], 0)
        self.set_tensor(by_name['model.norm.weight'], 1)
        if not self.spec.tied_embeddings:
            self.set_tensor(by_name['lm_head.weight'], 2)

        rng = np.random.default_rng(seed)
        proj_map = [
            'self_attn.q_proj.weight', 'self_attn.k_proj.weight', 'self_attn.v_proj.weight',
            'self_attn.o_proj.weight', 'mlp.gate_proj.weight', 'mlp.up_proj.weight', 'mlp.down_proj.weight',
        ]
        recon_stats = []
        for l in range(self.spec.n_layers):
            self.set_tensor(by_name[f'model.layers.{l}.input_layernorm.weight'], 10, l)
            self.set_tensor(by_name[f'model.layers.{l}.post_attention_layernorm.weight'], 11, l)
            for p_idx, pname in enumerate(proj_map):
                W = by_name[f'model.layers.{l}.{pname}']
                pcfg = self.proj_cfgs[l * 7 + p_idx]
                mode = int(pcfg.mode)
                if mode == LB_MODE_DENSE_FP32:
                    Wuse = bcir_q8_quantize_matrix(W) if q8_dense else W
                    self.set_tensor(Wuse, 20, l, p_idx, 0)
                    err = float(np.linalg.norm(W - Wuse) / max(np.linalg.norm(W), 1e-12))
                elif mode == LB_MODE_LOWRANK_FP16:
                    U_svd, S_svd, Vt_svd = np.linalg.svd(W.astype(np.float64), full_matrices=False)
                    r = int(pcfg.r1)
                    k = min(r, len(S_svd))
                    rtS = np.sqrt(np.maximum(S_svd[:k], 0.0))
                    U = np.zeros((W.shape[0], r), dtype=np.float32)
                    V = np.zeros((W.shape[1], r), dtype=np.float32)
                    U[:, :k] = (U_svd[:, :k] * rtS[None, :]).astype(np.float32)
                    V[:, :k] = (Vt_svd[:k, :].T * rtS[None, :]).astype(np.float32)
                    self.set_tensor(U, 20, l, p_idx, 0)
                    self.set_tensor(V, 20, l, p_idx, 1)
                    W_hat = (U.astype(np.float16).astype(np.float32)) @ (V.astype(np.float16).astype(np.float32)).T
                    err = float(np.linalg.norm(W - W_hat) / max(np.linalg.norm(W), 1e-12))
                elif mode == LB_MODE_ONEBIT_FULL:
                    Wm, h0, g0, W_hat = onebit_svid(W)
                    self.set_tensor(Wm, 20, l, p_idx, 0)
                    self.set_tensor(h0, 20, l, p_idx, 1)
                    self.set_tensor(g0, 20, l, p_idx, 2)
                    err = float(np.linalg.norm(W - W_hat) / max(np.linalg.norm(W), 1e-12))
                elif mode in (LB_MODE_LITTLEBIT_1P, LB_MODE_LITTLEBIT_RES):
                    if init_mode == 'random':
                        r1 = int(pcfg.r1)
                        std = 0.02 / math.sqrt(max(r1, 1))
                        U1 = rng.normal(0.0, 0.02, (W.shape[0], r1)).astype(np.float32)
                        V1 = rng.normal(0.0, 0.02, (W.shape[1], r1)).astype(np.float32)
                        h1 = np.full(W.shape[0], math.sqrt(std), dtype=np.float32)
                        g1 = np.full(W.shape[1], math.sqrt(std), dtype=np.float32)
                        l1 = np.ones(r1, dtype=np.float32)
                        W_hat1 = (h1[:, None] * np.sign(U1)) @ ((l1[:, None] * np.sign(V1).T) * g1[None, :])
                    else:
                        U1, V1, h1, g1, l1, W_hat1 = dual_svid_path(
                            W, int(pcfg.r1), use_itq=use_itq, itq_iters=itq_iters,
                            scale_flags=int(pcfg.scale_flags), seed=seed + l * 31 + p_idx)
                        if naive_svd:
                            # Ablation: keep SVD signs but replace Dual-SVID scales with uniform std
                            s = float(np.std(W)) / math.sqrt(max(int(pcfg.r1), 1))
                            h1 = np.full_like(h1, math.sqrt(max(s, 1e-6)))
                            g1 = np.full_like(g1, math.sqrt(max(s, 1e-6)))
                            l1 = np.ones_like(l1)
                            W_hat1 = (h1[:, None] * np.where(U1 >= 0, 1.0, -1.0)) @ ((l1[:, None] * np.where(V1 >= 0, 1.0, -1.0).T) * g1[None, :])
                    self.set_tensor(U1, 20, l, p_idx, 0)
                    self.set_tensor(V1, 20, l, p_idx, 1)
                    self.set_tensor(h1, 20, l, p_idx, 2)
                    self.set_tensor(g1, 20, l, p_idx, 3)
                    self.set_tensor(l1, 20, l, p_idx, 4)
                    W_hat = W_hat1
                    if mode == LB_MODE_LITTLEBIT_RES:
                        r2 = int(pcfg.r2)
                        if init_mode == 'random':
                            std = 0.01 / math.sqrt(max(r2, 1))
                            U2 = rng.normal(0.0, 0.02, (W.shape[0], r2)).astype(np.float32)
                            V2 = rng.normal(0.0, 0.02, (W.shape[1], r2)).astype(np.float32)
                            h2 = np.full(W.shape[0], math.sqrt(std), dtype=np.float32)
                            g2 = np.full(W.shape[1], math.sqrt(std), dtype=np.float32)
                            l2 = np.ones(r2, dtype=np.float32)
                            W_hat2 = (h2[:, None] * np.sign(U2)) @ ((l2[:, None] * np.sign(V2).T) * g2[None, :])
                        else:
                            W_res = W - W_hat1
                            U2, V2, h2, g2, l2, W_hat2 = dual_svid_path(
                                W_res, r2, use_itq=use_itq, itq_iters=itq_iters,
                                scale_flags=int(pcfg.scale_flags), seed=seed + 1000 + l * 31 + p_idx)
                            if naive_svd:
                                s = float(np.std(W_res)) / math.sqrt(max(r2, 1))
                                h2 = np.full_like(h2, math.sqrt(max(s, 1e-6)))
                                g2 = np.full_like(g2, math.sqrt(max(s, 1e-6)))
                                l2 = np.ones_like(l2)
                                W_hat2 = (h2[:, None] * np.where(U2 >= 0, 1.0, -1.0)) @ ((l2[:, None] * np.where(V2 >= 0, 1.0, -1.0).T) * g2[None, :])
                        self.set_tensor(U2, 20, l, p_idx, 5)
                        self.set_tensor(V2, 20, l, p_idx, 6)
                        self.set_tensor(h2, 20, l, p_idx, 7)
                        self.set_tensor(g2, 20, l, p_idx, 8)
                        self.set_tensor(l2, 20, l, p_idx, 9)
                        W_hat = W_hat1 + W_hat2
                    err = float(np.linalg.norm(W - W_hat) / max(np.linalg.norm(W), 1e-12))
                recon_stats.append({'layer': l, 'proj': PROJ_NAMES[p_idx], 'rel_frob_err': err})
        self.lib.lb_model_reset_opt(self.ptr)
        return recon_stats

    def forward(self, tokens):
        t = np.ascontiguousarray(tokens, dtype=np.uint32)
        logits = np.empty((self.batch, self.time_len, self.spec.vocab_size), dtype=np.float32)
        rc = self.lib.lb_model_forward(
            self.ptr, t.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            logits.ctypes.data_as(ct.POINTER(ct.c_float)), None,
        )
        assert rc == 0, rc
        return logits

    def train_loop(self, teacher, tokens_seq, targets_seq, lrs, opt_cfg: LBAdamWCfg):
        steps = len(lrs)
        tok = np.ascontiguousarray(tokens_seq, dtype=np.uint32)
        tgt = np.ascontiguousarray(targets_seq, dtype=np.uint32)
        lr_arr = np.ascontiguousarray(lrs, dtype=np.float64)
        evs = (LBStepMetrics * steps)()
        rc = self.lib.lb_model_train_loop(
            self.ptr, teacher.ptr if teacher else None,
            tok.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tgt.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            steps, lr_arr.ctypes.data_as(ct.POINTER(ct.c_double)),
            ct.byref(opt_cfg), evs,
        )
        assert rc == 0, f'train_loop rc={rc}'
        return [
            {
                'step': int(e.step), 'total_loss': float(e.total_loss), 'ce_loss': float(e.ce_loss),
                'kl_loss': float(e.kl_loss), 'inter_mse': float(e.inter_mse),
                'grad_norm': float(e.grad_norm), 'sign_flip_rate': float(e.sign_flip_rate),
                'active_grad_frac': float(e.active_grad_frac), 'step_ms': float(e.step_ms),
            }
            for e in evs
        ]

    def realized_bpw_summary(self, scale_bits=16):
        body_bits = sum(realized_proj_bits(p, scale_bits) for p in self.proj_cfgs)
        body_params = sum(int(p.din) * int(p.dout) for p in self.proj_cfgs)
        emb_params = (1 if self.spec.tied_embeddings else 2) * self.spec.vocab_size * self.spec.d_model
        norm_params = (2 * self.spec.n_layers + 1) * self.spec.d_model
        nonlin_params = emb_params + norm_params
        tot_params = body_params + nonlin_params
        return {
            'body_bpw': body_bits / body_params,
            'total_bpw_fp16_emb': (body_bits + 16 * nonlin_params) / tot_params,
            'body_bytes': body_bits / 8.0,
            'total_bytes_fp16_emb': (body_bits + 16 * nonlin_params) / 8.0,
            'training_arena_bytes': self.total_bytes,
            'training_params_fp32': self.param_count,
        }


def make_proj_cfgs(spec: DecoderSpec, mode, target_bpw=0.55, kv_factor=1.0,
                   scale_flags=LB_SCALE_ALL, fp16_round=1, min_rank=1,
                   only_mlp=False, only_attn=False, explicit_r=None):
    shapes = proj_shapes_for_spec(spec)
    cfgs = []
    for _ in range(spec.n_layers):
        for p_idx, (dout, din) in enumerate(shapes):
            is_mlp = p_idx >= 4
            is_kv = p_idx in (1, 2)
            eff_mode = mode
            if only_mlp and not is_mlp:
                eff_mode = LB_MODE_DENSE_FP32
            if only_attn and is_mlp:
                eff_mode = LB_MODE_DENSE_FP32
            if eff_mode == LB_MODE_DENSE_FP32 or eff_mode == LB_MODE_ONEBIT_FULL:
                cfgs.append(LBProjCfg(eff_mode, din, dout, 0, 0, scale_flags, fp16_round))
            elif eff_mode == LB_MODE_LOWRANK_FP16:
                r1 = explicit_r if explicit_r else solve_lowrank_fp16_rank(dout, din, target_bpw, min_rank=min_rank)
                cfgs.append(LBProjCfg(eff_mode, din, dout, r1, 0, scale_flags, fp16_round))
            elif eff_mode == LB_MODE_LITTLEBIT_1P:
                bpw = target_bpw * (kv_factor if is_kv else 1.0)
                r1 = explicit_r if explicit_r else solve_lb_rank(dout, din, bpw, residual=False, min_rank=min_rank)
                cfgs.append(LBProjCfg(eff_mode, din, dout, r1, 0, scale_flags, fp16_round))
            else:  # LB_MODE_LITTLEBIT_RES
                bpw = target_bpw * (kv_factor if is_kv else 1.0)
                r = explicit_r if explicit_r else solve_lb_rank(dout, din, bpw, residual=True, min_rank=min_rank)
                cfgs.append(LBProjCfg(eff_mode, din, dout, r, r, scale_flags, fp16_round))
    return cfgs


def verify_all():
    """Gate 1: Dense correspondence vs BCIR bcir_decoder_train.so.
    Gate 2: Finite-difference gradient check across ALL 5 linear modes & KD objectives.
    Gate 3: AVX-512 bit-packed kernel exact correspondence check.
    """
    spec = DecoderSpec(vocab_size=32, d_model=32, n_heads=4, n_kv_heads=2, n_layers=2, d_ff=64,
                       activation='silu_gate', tied_embeddings=True, rms_norm_eps=1e-5)
    B, T = 2, 8
    rng = np.random.default_rng(777)
    x = rng.integers(0, 32, size=(B, T), dtype=np.uint32)
    y = rng.integers(0, 32, size=(B, T), dtype=np.uint32)

    # Gate 1: Compare LB_MODE_DENSE_FP32 vs unmodified BCIR NativeDecoder
    m_bcir = NativeDecoder(BCIR_LIB, spec, seed=123, batch_size=B, context_length=T, threads=2, contract='kblock+reduce+attn')
    w_dense = np.ctypeslib.as_array(m_bcir.buffers['weights']).copy()
    xc = np.ascontiguousarray(x, dtype=np.uint32)
    yc = np.ascontiguousarray(y, dtype=np.uint32)
    loss_bcir = ct.c_double()
    rc = m_bcir.lib.bcir_decoder_loss_backward(
        ct.byref(m_bcir.state), xc.ctypes.data_as(ct.POINTER(ct.c_uint32)),
        yc.ctypes.data_as(ct.POINTER(ct.c_uint32)), xc.size, 0, ct.byref(loss_bcir))
    assert rc == 0
    g_bcir = np.ctypeslib.as_array(m_bcir.buffers['gradients']).copy()
    m_bcir.close()

    m_lb = LBModel(spec, B, T, make_proj_cfgs(spec, LB_MODE_DENSE_FP32), objective=LB_OBJ_CE, threads=2)
    m_lb.load_from_dense_bcir(w_dense)
    met = LBStepMetrics()
    rc = m_lb.lib.lb_model_loss_backward(
        m_lb.ptr, None, xc.ctypes.data_as(ct.POINTER(ct.c_uint32)),
        yc.ctypes.data_as(ct.POINTER(ct.c_uint32)), 0, ct.byref(met))
    assert rc == 0
    g_lb = m_lb.grads_view().copy()
    loss_diff = abs(float(loss_bcir.value) - float(met.ce_loss))
    grad_diff = float(np.max(np.abs(g_bcir - g_lb)))
    print(f'[GATE 1 Dense vs BCIR] loss_diff={loss_diff:.2e} max_grad_diff={grad_diff:.2e}')
    assert loss_diff < 1e-6 and grad_diff < 2e-5, (loss_diff, grad_diff)

    # Gate 2: Central finite-difference check on smooth forward (tanh(k*x), k=3.0) for EVERY mode + KD
    teacher = m_lb
    for mode_name, mode, obj in [
        ('LOWRANK_FP16+CE', LB_MODE_LOWRANK_FP16, LB_OBJ_CE),
        ('ONEBIT_FULL+CE', LB_MODE_ONEBIT_FULL, LB_OBJ_CE),
        ('LITTLEBIT_1P+CE', LB_MODE_LITTLEBIT_1P, LB_OBJ_CE),
        ('LITTLEBIT_RES+CE', LB_MODE_LITTLEBIT_RES, LB_OBJ_CE),
        ('LITTLEBIT_RES+KD_FULL', LB_MODE_LITTLEBIT_RES, LB_OBJ_KD_FULL),
    ]:
        pc = make_proj_cfgs(spec, mode, explicit_r=6, fp16_round=0)
        st = LBModel(spec, B, T, pc, surrogate=LB_SURR_SMOOTH_FWD_TEST, smooth_k=3.0,
                     objective=obj, kd_lambda=10.0, threads=2)
        st.load_from_dense_bcir(w_dense, use_itq=False)
        rc = st.lib.lb_model_loss_backward(
            st.ptr, teacher.ptr if obj != LB_OBJ_CE else None,
            xc.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            yc.ctypes.data_as(ct.POINTER(ct.c_uint32)), 0, ct.byref(met))
        assert rc == 0
        g_ana = st.grads_view().copy()
        wv = st.weights_view()
        # Sample 24 stratified coordinates across all parameter spans plus top-12 gradient entries
        strat = np.linspace(0, st.param_count - 1, 24, dtype=int)
        top12 = np.argsort(np.abs(g_ana))[-12:]
        idxs = np.unique(np.concatenate([strat, top12]))
        eps = 2e-3
        max_rel = 0.0
        max_abs = 0.0
        for idx in idxs:
            orig = float(wv[idx])
            wv[idx] = orig + eps
            mp = LBStepMetrics()
            st.lib.lb_model_loss_backward(st.ptr, teacher.ptr if obj != LB_OBJ_CE else None,
                                          xc.ctypes.data_as(ct.POINTER(ct.c_uint32)),
                                          yc.ctypes.data_as(ct.POINTER(ct.c_uint32)), 0, ct.byref(mp))
            wv[idx] = orig - eps
            mm = LBStepMetrics()
            st.lib.lb_model_loss_backward(st.ptr, teacher.ptr if obj != LB_OBJ_CE else None,
                                          xc.ctypes.data_as(ct.POINTER(ct.c_uint32)),
                                          yc.ctypes.data_as(ct.POINTER(ct.c_uint32)), 0, ct.byref(mm))
            wv[idx] = orig
            g_num = (mp.total_loss - mm.total_loss) / (2.0 * eps)
            diff = abs(g_ana[idx] - g_num)
            rel = diff / max(abs(g_ana[idx]), abs(g_num), 1e-3)
            max_abs = max(max_abs, diff)
            max_rel = max(max_rel, rel)
        print(f'[GATE 2 FiniteDiff {mode_name:22s}] max_abs_err={max_abs:.3e} max_rel_err={max_rel:.3e}')
        assert max_rel < 5e-3, (mode_name, max_rel)
        st.close()
    teacher.close()

    # Gate 3: Bit-packed AVX-512 kernel numerical accuracy check
    lib = get_lib()
    for kname, kkind in [
        ('PORTABLE_BITPACK', LB_KERN_PORTABLE_BITPACK),
        ('AVX512_SIGNXOR', LB_KERN_AVX512_SIGNXOR),
        ('AVX512_LUT4', LB_KERN_AVX512_LUT4),
        ('AVX512_LUT4_PACKED1B', LB_KERN_AVX512_LUT4_PACKED1B),
        ('AVX512_LUT4_PACKED1B_MB4', LB_KERN_AVX512_LUT4_PACKED1B_MB4),
        ('DENSE_FP16_STREAM', LB_KERN_DENSE_FP16_STREAM),
        ('DENSE_Q8_BCIR', LB_KERN_DENSE_Q8_BCIR),
    ]:
        res = LBBenchResult()
        rc = lib.lb_bench_linear_kernel(kkind, 4, 256, 384, 32, 1, 1, 5, ct.byref(res))
        assert rc == 0
        print(f'[GATE 3 Kernel {kname:18s}] max_abs_err={res.max_abs_err:.2e} median_us={res.median_us:.1f} bpw={res.realized_bpw:.3f}')
        assert res.max_abs_err < 1e-4, (kname, res.max_abs_err)
    print('ALL 3 VERIFICATION GATES PASSED!')


if __name__ == '__main__':
    verify_all()
