#!/usr/bin/env python3
"""Python ctypes harness for BCIR ABI 6 MIXER-EXT empirical study.

Links directly against BCIR's production runtime/c/{bcir_tensor,bcir_precision,bcir_mixer_ext}.c
(zero duplicated C code) and uses the exact canonical WikiText-2 byte splits from
`../gp-global/data/` for apples-to-apples comparisons with precision-arch-study.
"""
from __future__ import annotations

import ctypes as ct
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

STUDY_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = STUDY_DIR / "src"
BUILD_DIR = STUDY_DIR / "build"
RESULTS_DIR = STUDY_DIR / "results"
CKPT_DIR = STUDY_DIR / "checkpoints"
FIG_DIR = STUDY_DIR / "figures"
LIB_PATH = BUILD_DIR / "libbcir_mixer_ext.so"
WIKITEXT_DIR = STUDY_DIR.parent / "gp-global" / "data"
BCIR_REPO = STUDY_DIR.parents[2] / "Binary-Correspondence-Intermediate-Representation"

BCIR_EXT_MAX_LAYERS = 64
BCIR_EXT_IGNORE = 0xFFFFFFFF

MIXER_GQA = 0
MIXER_SWA = 1
MIXER_DELTANET = 2
MIXER_KDA = 3
MIXER_MLA = 4
MIXER_GDN2 = 5

GATE_NONE = 0
GATE_G1_HEADWISE = 1
GATE_G1_LOWRANK = 2

CANON_OFF = 0
CANON_A = 1
CANON_AC = 2

OPT_FP32 = 0
OPT_Q8_MOMENTS = 1
OPT_Q8_BF16_GRAD = 2
OPT_ADAFACTOR_M2 = 3
OPT_ADAFACTOR_BF16_GRAD = 4
OPT_MUON_FP32 = 5
OPT_Q8_MUON_BF16_GRAD = 6

INIT_LEGACY_002 = 0
INIT_RESIDUAL_SCALED = 1
INIT_MUP_V1 = 2

FP64_GUARD_OFF = 0
FP64_GUARD_CONDITIONAL = 1
FP64_GUARD_ALWAYS = 2


class PrecPolicy(ct.Structure):
    _fields_ = [
        ("attn_fwd_gemm", ct.c_int),
        ("attn_bwd_gemm", ct.c_int),
        ("mlp_fwd_gemm", ct.c_int),
        ("mlp_bwd_gemm", ct.c_int),
        ("opt_kind", ct.c_int),
        ("fp64_guard_mode", ct.c_int),
        ("rmsnorm_cond_thresh", ct.c_double),
        ("softmax_logit_thresh", ct.c_double),
        ("wy_cond_thresh", ct.c_double),
        ("use_vdp_portable", ct.c_int),
    ]


class PrecTelemetry(ct.Structure):
    _fields_ = [
        ("fp64_norm_promotions", ct.c_uint64),
        ("fp64_softmax_promotions", ct.c_uint64),
        ("fp64_wy_solves", ct.c_uint64),
        ("nonfinite_events", ct.c_uint64),
        ("grad_clip_hits", ct.c_uint64),
        ("total_steps", ct.c_uint64),
        ("q8_m2_floor_clamps", ct.c_uint64),
        ("gemm_calls_attn", ct.c_uint64),
        ("gemm_calls_mlp", ct.c_uint64),
        ("muon_ns_calls", ct.c_uint64),
        ("max_grad_norm", ct.c_double),
        ("max_softmax_logit", ct.c_double),
        ("min_rms_observed", ct.c_double),
        ("max_wy_offdiag", ct.c_double),
        ("max_abs_act", ct.c_double),
        ("first_tok_attn_mean", ct.c_double),
    ]


class ExtSpec(ct.Structure):
    _fields_ = [
        ("vocab", ct.c_uint32),
        ("width", ct.c_uint32),
        ("heads", ct.c_uint32),
        ("kvheads", ct.c_uint32),
        ("layers", ct.c_uint32),
        ("ff", ct.c_uint32),
        ("batch", ct.c_uint32),
        ("time", ct.c_uint32),
        ("tied", ct.c_uint32),
        ("rope_base", ct.c_double),
        ("rms_eps", ct.c_double),
        ("layer_mixer", ct.c_uint32 * BCIR_EXT_MAX_LAYERS),
        ("layer_use_rope", ct.c_uint32 * BCIR_EXT_MAX_LAYERS),
        ("swa_window", ct.c_uint32),
        ("delta_chunk", ct.c_uint32),
        ("mla_dc", ct.c_uint32),
        ("mla_dr", ct.c_uint32),
        ("init_policy", ct.c_int),
        ("mup_d_base", ct.c_uint32),
        ("seed", ct.c_uint64),
        ("threads", ct.c_uint32),
        ("skip_init", ct.c_int),
        ("gate_mode", ct.c_uint32),
        ("qk_norm", ct.c_uint32),
        ("canon_mode", ct.c_uint32),
        ("mhc_streams", ct.c_uint32),
        ("online_softmax", ct.c_uint32),
        ("gate_rank", ct.c_uint32),
    ]


class MemStats(ct.Structure):
    _fields_ = [
        ("n_params", ct.c_size_t),
        ("attn_params", ct.c_size_t),
        ("mlp_params", ct.c_size_t),
        ("nonlin_params", ct.c_size_t),
        ("weight_bytes", ct.c_size_t),
        ("opt_only_bytes", ct.c_size_t),
        ("persistent_bytes", ct.c_size_t),
        ("activation_bytes", ct.c_size_t),
        ("mixer_prob_bytes", ct.c_size_t),
        ("scratch_bytes", ct.c_size_t),
        ("total_bytes", ct.c_size_t),
        ("infer_kv_bytes_per_tok", ct.c_size_t),
        ("infer_rec_state_bytes", ct.c_size_t),
        ("init_ms", ct.c_double),
    ]


class StepMetrics(ct.Structure):
    _fields_ = [
        ("loss", ct.c_double),
        ("grad_norm", ct.c_double),
        ("step_ms", ct.c_double),
        ("clipped", ct.c_int),
        ("status", ct.c_int),
        ("step", ct.c_uint64),
        ("supervised_tokens", ct.c_uint64),
        ("fp64_promotions", ct.c_uint64),
        ("nonfinite_events", ct.c_uint64),
    ]


class EvalResult(ct.Structure):
    _fields_ = [
        ("ce", ct.c_double),
        ("accuracy", ct.c_double),
        ("tokens", ct.c_uint64),
        ("nonfinite", ct.c_uint64),
        ("eval_ms", ct.c_double),
    ]


class AdamWParams(ct.Structure):
    _fields_ = [
        ("lr", ct.c_double),
        ("beta1", ct.c_double),
        ("beta2", ct.c_double),
        ("epsilon", ct.c_double),
        ("weight_decay", ct.c_double),
        ("grad_clip", ct.c_double),
    ]


class TensorInfo(ct.Structure):
    _fields_ = [
        ("offset", ct.c_size_t),
        ("rows", ct.c_size_t),
        ("cols", ct.c_size_t),
        ("layer", ct.c_int),
        ("region", ct.c_int),
        ("name", ct.c_char * 48),
    ]


_LIB: Optional[ct.CDLL] = None


def load_lib() -> ct.CDLL:
    global _LIB
    if _LIB is not None:
        return _LIB
    if not LIB_PATH.exists():
        subprocess.run(["make"], cwd=str(STUDY_DIR), check=True)
    lib = ct.CDLL(str(LIB_PATH))
    lib.bcir_mixer_ext_abi_version.argtypes = []
    lib.bcir_mixer_ext_abi_version.restype = ct.c_uint32
    assert lib.bcir_mixer_ext_abi_version() == 6
    lib.bcir_prec_opt_name.argtypes = [ct.c_int]
    lib.bcir_prec_opt_name.restype = ct.c_char_p
    lib.bcir_ext_create.argtypes = [ct.POINTER(ExtSpec), ct.POINTER(PrecPolicy)]
    lib.bcir_ext_create.restype = ct.c_void_p
    lib.bcir_ext_destroy.argtypes = [ct.c_void_p]
    lib.bcir_ext_destroy.restype = None
    lib.bcir_ext_get_mem_stats.argtypes = [ct.c_void_p, ct.POINTER(MemStats)]
    lib.bcir_ext_get_mem_stats.restype = None
    lib.bcir_ext_get_telemetry.argtypes = [ct.c_void_p, ct.POINTER(PrecTelemetry)]
    lib.bcir_ext_get_telemetry.restype = None
    lib.bcir_ext_reset_telemetry.argtypes = [ct.c_void_p]
    lib.bcir_ext_reset_telemetry.restype = None
    lib.bcir_ext_reset_opt.argtypes = [ct.c_void_p]
    lib.bcir_ext_reset_opt.restype = None
    lib.bcir_ext_weights_ptr.argtypes = [ct.c_void_p]
    lib.bcir_ext_weights_ptr.restype = ct.POINTER(ct.c_float)
    lib.bcir_ext_param_count.argtypes = [ct.c_void_p]
    lib.bcir_ext_param_count.restype = ct.c_size_t
    lib.bcir_ext_tensor_count.argtypes = [ct.c_void_p]
    lib.bcir_ext_tensor_count.restype = ct.c_size_t
    lib.bcir_ext_tensor_info_at.argtypes = [ct.c_void_p, ct.c_size_t, ct.POINTER(TensorInfo)]
    lib.bcir_ext_tensor_info_at.restype = ct.c_int
    lib.bcir_ext_evaluate.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
        ct.c_size_t, ct.c_uint32, ct.POINTER(EvalResult),
    ]
    lib.bcir_ext_evaluate.restype = ct.c_int
    lib.bcir_ext_train_loop.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_double),
        ct.c_size_t, ct.POINTER(AdamWParams), ct.POINTER(StepMetrics),
    ]
    lib.bcir_ext_train_loop.restype = ct.c_int
    lib.bcir_quant_q8_g32_encode.argtypes = [
        ct.POINTER(ct.c_float), ct.POINTER(ct.c_int8), ct.POINTER(ct.c_int8), ct.c_size_t,
    ]
    lib.bcir_quant_q8_g32_encode.restype = None
    lib.bcir_quant_q8_g32_decode.argtypes = [
        ct.POINTER(ct.c_int8), ct.POINTER(ct.c_int8), ct.POINTER(ct.c_float), ct.c_size_t,
    ]
    lib.bcir_quant_q8_g32_decode.restype = None
    _LIB = lib
    return lib


def make_policy(opt_kind: int = OPT_FP32, fp64_guard_mode: int = FP64_GUARD_CONDITIONAL) -> PrecPolicy:
    return PrecPolicy(0, 0, 0, 0, opt_kind, fp64_guard_mode, 1e-4, 25.0, 0.0, 0)


def make_spec(
    *,
    vocab: int = 256,
    width: int = 512,
    heads: int = 8,
    kvheads: int = 2,
    layers: int = 4,
    ff: int = 1536,
    batch: int = 4,
    time_len: int = 64,
    tied: int = 0,
    layer_mixers: Optional[Sequence[int]] = None,
    layer_use_rope: Optional[Sequence[int]] = None,
    swa_window: int = 16,
    delta_chunk: int = 16,
    mla_dc: int = 0,
    mla_dr: int = 0,
    init_policy: int = INIT_MUP_V1,
    mup_d_base: int = 128,
    seed: int = 42,
    threads: int = 2,
    skip_init: int = 0,
    gate_mode: int = GATE_NONE,
    qk_norm: int = 0,
    canon_mode: int = CANON_OFF,
    mhc_streams: int = 1,
    online_softmax: int = 0,
    gate_rank: int = 0,
) -> ExtSpec:
    sp = ExtSpec()
    sp.vocab = vocab
    sp.width = width
    sp.heads = heads
    sp.kvheads = kvheads
    sp.layers = layers
    sp.ff = ff
    sp.batch = batch
    sp.time = time_len
    sp.tied = tied
    sp.rope_base = 10000.0
    sp.rms_eps = 1e-5
    mixers = list(layer_mixers) if layer_mixers is not None else [MIXER_GQA] * layers
    ropes = list(layer_use_rope) if layer_use_rope is not None else [1] * layers
    for i in range(layers):
        sp.layer_mixer[i] = mixers[i]
        sp.layer_use_rope[i] = ropes[i]
    sp.swa_window = swa_window
    sp.delta_chunk = delta_chunk
    sp.mla_dc = mla_dc
    sp.mla_dr = mla_dr
    sp.init_policy = init_policy
    sp.mup_d_base = mup_d_base
    sp.seed = seed
    sp.threads = threads
    sp.skip_init = skip_init
    sp.gate_mode = gate_mode
    sp.qk_norm = qk_norm
    sp.canon_mode = canon_mode
    sp.mhc_streams = mhc_streams
    sp.online_softmax = online_softmax
    sp.gate_rank = gate_rank
    return sp


class Abi6Model:
    def __init__(self, spec: ExtSpec, policy: PrecPolicy):
        self.lib = load_lib()
        self.spec = spec
        self.policy = policy
        self.ptr = self.lib.bcir_ext_create(ct.byref(spec), ct.byref(policy))
        if not self.ptr:
            raise RuntimeError("bcir_ext_create failed")
        self.n_params = int(self.lib.bcir_ext_param_count(self.ptr))

    def close(self) -> None:
        if getattr(self, "ptr", None):
            self.lib.bcir_ext_destroy(self.ptr)
            self.ptr = None

    def __enter__(self) -> "Abi6Model":
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def weights_view(self) -> np.ndarray:
        ptr = self.lib.bcir_ext_weights_ptr(self.ptr)
        return np.ctypeslib.as_array(ptr, shape=(self.n_params,))

    def copy_weights(self) -> np.ndarray:
        return self.weights_view().copy()

    def set_weights(self, arr: np.ndarray) -> None:
        v = self.weights_view()
        v[:] = np.asarray(arr, dtype=np.float32)

    def mem_stats(self) -> Dict[str, Any]:
        ms = MemStats()
        self.lib.bcir_ext_get_mem_stats(self.ptr, ct.byref(ms))
        return {k: getattr(ms, k) for k, _ in MemStats._fields_}

    def telemetry(self) -> Dict[str, Any]:
        t = PrecTelemetry()
        self.lib.bcir_ext_get_telemetry(self.ptr, ct.byref(t))
        return {k: getattr(t, k) for k, _ in PrecTelemetry._fields_}

    def reset_telemetry(self) -> None:
        self.lib.bcir_ext_reset_telemetry(self.ptr)

    def tensor_infos(self) -> List[Dict[str, Any]]:
        cnt = int(self.lib.bcir_ext_tensor_count(self.ptr))
        out = []
        ti = TensorInfo()
        for i in range(cnt):
            assert self.lib.bcir_ext_tensor_info_at(self.ptr, i, ct.byref(ti)) == 0
            out.append({
                "name": ti.name.decode("utf-8"),
                "offset": int(ti.offset),
                "rows": int(ti.rows),
                "cols": int(ti.cols),
                "layer": int(ti.layer),
                "region": int(ti.region),
            })
        return out

    def train_loop(
        self,
        tokens: np.ndarray,
        targets: np.ndarray,
        opt_cfg: AdamWParams,
        *,
        lrs: Optional[np.ndarray] = None,
        active_times: Optional[np.ndarray] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        steps = int(tokens.shape[0])
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        at_ptr = None
        if active_times is not None:
            at = np.ascontiguousarray(active_times, dtype=np.uint32)
            at_ptr = at.ctypes.data_as(ct.POINTER(ct.c_uint32))
        lr_ptr = None
        if lrs is not None:
            lr_a = np.ascontiguousarray(lrs, dtype=np.float64)
            lr_ptr = lr_a.ctypes.data_as(ct.POINTER(ct.c_double))
        evs = (StepMetrics * steps)()
        rc = int(self.lib.bcir_ext_train_loop(
            self.ptr,
            tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tg.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            at_ptr,
            lr_ptr,
            steps,
            ct.byref(opt_cfg),
            evs,
        ))
        out = [{k: getattr(evs[i], k) for k, _ in StepMetrics._fields_} for i in range(steps)]
        return rc, out

    def evaluate(
        self, tokens: np.ndarray, targets: np.ndarray, *, active_time: Optional[int] = None
    ) -> Dict[str, Any]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        nb = int(tk.shape[0])
        act_t = int(self.spec.time if active_time is None else active_time)
        res = EvalResult()
        rc = int(self.lib.bcir_ext_evaluate(
            self.ptr,
            tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tg.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            nb,
            act_t,
            ct.byref(res),
        ))
        ce = float(res.ce)
        ok = rc == 0 and math.isfinite(ce)
        return {
            "ok": ok,
            "rc": rc,
            "ce": ce if ok else None,
            "ppl": float(math.exp(min(ce, 50.0))) if ok else None,
            "accuracy": float(res.accuracy),
            "tokens": int(res.tokens),
            "nonfinite": int(res.nonfinite),
            "eval_ms": float(res.eval_ms),
        }


def quantize_bcirq8_g32(w: np.ndarray) -> np.ndarray:
    """Lossy deterministic BCIRQ8 g=32 pow2 quantize-dequantize via BCIR's C kernel."""
    lib = load_lib()
    src = np.ascontiguousarray(w, dtype=np.float32)
    n = src.size
    nb = (n + 31) // 32
    q = np.empty(n, dtype=np.int8)
    e = np.empty(nb, dtype=np.int8)
    dst = np.empty(n, dtype=np.float32)
    lib.bcir_quant_q8_g32_encode(
        src.ctypes.data_as(ct.POINTER(ct.c_float)),
        q.ctypes.data_as(ct.POINTER(ct.c_int8)),
        e.ctypes.data_as(ct.POINTER(ct.c_int8)),
        n,
    )
    lib.bcir_quant_q8_g32_decode(
        q.ctypes.data_as(ct.POINTER(ct.c_int8)),
        e.ctypes.data_as(ct.POINTER(ct.c_int8)),
        dst.ctypes.data_as(ct.POINTER(ct.c_float)),
        n,
    )
    return dst


def dual_svid_1bit_ptq(mat: np.ndarray, rank: int = 16, itq_iters: int = 10) -> np.ndarray:
    """BCIR COMP-1 Dual-SVID + Joint-ITQ packed-1-bit PTQ reconstruction (0.95-1.07 bpw)."""
    W = np.asarray(mat, dtype=np.float64)
    m, n = W.shape
    r = min(rank, m, n)
    B = np.where(W >= 0.0, 1.0, -1.0)
    # Row/col positive scaling via rank-1 SVD of |W|
    absW = np.abs(W)
    u1, s1, vt1 = np.linalg.svd(absW, full_matrices=False)
    alpha_r = np.abs(u1[:, 0]) * math.sqrt(max(s1[0], 1e-12))
    alpha_c = np.abs(vt1[0, :]) * math.sqrt(max(s1[0], 1e-12))
    # Iterative binary + scale refinement
    for _ in range(3):
        denom_r = np.sum((B * alpha_c[None, :]) ** 2, axis=1) + 1e-12
        alpha_r = np.maximum(np.sum(W * B * alpha_c[None, :], axis=1) / denom_r, 1e-8)
        denom_c = np.sum((B * alpha_r[:, None]) ** 2, axis=0) + 1e-12
        alpha_c = np.maximum(np.sum(W * B * alpha_r[:, None], axis=0) / denom_c, 1e-8)
    base = (alpha_r[:, None] * B) * alpha_c[None, :]
    res = W - base
    if r > 0:
        U, S, Vt = np.linalg.svd(res, full_matrices=False)
        U_r = U[:, :r] * np.sqrt(np.maximum(S[:r], 0.0))[None, :]
        V_r = (np.sqrt(np.maximum(S[:r], 0.0))[:, None] * Vt[:r, :])
        # Joint-ITQ alternating sign + continuous scale update
        Bu = np.where(U_r >= 0.0, 1.0, -1.0)
        Bv = np.where(V_r >= 0.0, 1.0, -1.0)
        su = np.mean(np.abs(U_r), axis=0) + 1e-8
        sv = np.mean(np.abs(V_r), axis=1) + 1e-8
        for _ in range(max(1, itq_iters // 2)):
            Bu = np.where(U_r >= 0.0, 1.0, -1.0)
            su = np.sum(U_r * Bu, axis=0) / (m + 1e-12)
            Bv = np.where(V_r >= 0.0, 1.0, -1.0)
            sv = np.sum(V_r * Bv, axis=1) / (n + 1e-12)
        recon = base + (Bu * su[None, :]) @ (Bv * sv[:, None])
    else:
        recon = base
    return recon.astype(np.float32)


def apply_comp1_ptq_weights(model: Abi6Model, base_w: np.ndarray, rank: int = 16) -> np.ndarray:
    """Apply BCIR Deployment Policy COMP-1: Q8 g=32 on Attention/Norms/Embed/Head,
    1-bit Dual-SVID on MLP (region==2) tensors with min(rows, cols) >= 512."""
    out = quantize_bcirq8_g32(base_w)
    for ti in model.tensor_infos():
        if ti["region"] == 2 and min(ti["rows"], ti["cols"]) >= 512:
            off = ti["offset"]
            sz = ti["rows"] * ti["cols"]
            mat = base_w[off : off + sz].reshape(ti["rows"], ti["cols"])
            out[off : off + sz] = dual_svid_1bit_ptq(mat, rank=rank).reshape(-1)
    return out


# ==================== Canonical WikiText-2 Byte Loader ====================
_WIKI_CACHE: Dict[str, np.ndarray] = {}


def load_wikitext_bytes(split: str) -> np.ndarray:
    if split in _WIKI_CACHE:
        return _WIKI_CACHE[split]
    import pyarrow.parquet as pq

    candidates = [
        WIKITEXT_DIR / f"{split}-00000-of-00001.parquet",
        WIKITEXT_DIR / f"wikitext-2-raw-v1-{split}.parquet",
        WIKITEXT_DIR / f"{split}.parquet",
    ]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        matches = sorted(WIKITEXT_DIR.glob(f"*{split}*.parquet"))
        path = matches[0] if matches else None
    if path is None:
        raise FileNotFoundError(f"WikiText-2 {split} parquet not found in {WIKITEXT_DIR}")
    tbl = pq.read_table(path)
    raw = "\n".join(s for s in tbl.column(0).to_pylist() if s).encode("utf-8")
    arr = np.frombuffer(raw, dtype=np.uint8).astype(np.uint32)
    _WIKI_CACHE[split] = arr
    return arr


def sample_lm_batches(
    stream: np.ndarray, n_steps: int, batch: int, seq_len: int, seed: int
) -> Tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    max_off = len(stream) - seq_len - 2
    offs = rng.integers(0, max_off, size=(n_steps, batch))
    idx = offs[:, :, None] + np.arange(seq_len + 1, dtype=np.int64)[None, None, :]
    seqs = stream[idx]
    return (
        np.ascontiguousarray(seqs[:, :, :-1], dtype=np.uint32),
        np.ascontiguousarray(seqs[:, :, 1:], dtype=np.uint32),
    )


def make_fixed_eval_batches(
    stream: np.ndarray, n_batches: int, batch: int, seq_len: int
) -> Tuple[np.ndarray, np.ndarray]:
    total_seqs = n_batches * batch
    stride = max(seq_len + 1, (len(stream) - seq_len - 2) // total_seqs)
    offs = np.arange(total_seqs, dtype=np.int64) * stride
    idx = offs[:, None] + np.arange(seq_len + 1, dtype=np.int64)[None, :]
    seqs = stream[idx].reshape(n_batches, batch, seq_len + 1)
    return (
        np.ascontiguousarray(seqs[:, :, :-1], dtype=np.uint32),
        np.ascontiguousarray(seqs[:, :, 1:], dtype=np.uint32),
    )


def cosine_lr_schedule(steps: int, max_lr: float, min_lr: float, warmup: int) -> np.ndarray:
    lrs = np.zeros(steps, dtype=np.float64)
    for s in range(steps):
        if warmup > 0 and s < warmup:
            lrs[s] = max_lr * float(s + 1) / float(warmup)
        else:
            prog = float(s - warmup) / float(max(1, steps - warmup))
            lrs[s] = min_lr + 0.5 * (max_lr - min_lr) * (1.0 + math.cos(math.pi * min(1.0, prog)))
    return lrs


# ==================== Associative Recall & Counterfactual Suite ====================
_KEY_TOKS = np.arange(ord("a"), ord("z") + 1, dtype=np.uint32)
_VAL_TOKS = np.arange(ord("0"), ord("9") + 1, dtype=np.uint32)
_EQ, _SEP, _QRY, _PAD = ord("="), ord(","), ord("?"), ord(" ")


def build_kv_dataset(
    n_seqs: int,
    seq_len: int,
    n_pairs: int,
    seed: int,
    *,
    mode: str = "standard",
    filler_len: int = 0,
    ordered_query: bool = False,
    shuffled_query: bool = False,
) -> Tuple[np.ndarray, np.ndarray]:
    multi = ordered_query or shuffled_query
    if multi:
        filler_tokens = (filler_len + 1) if filler_len > 0 else 0
        req_len = 4 * n_pairs + filler_tokens + 5 * (n_pairs - 1) + 3
        if req_len > seq_len:
            raise ValueError(f"multi-query KV probe requires seq_len >= {req_len}, got {seq_len}")
    rng = np.random.default_rng(seed)
    toks = np.full((n_seqs, seq_len), _PAD, dtype=np.uint32)
    tgts = np.full((n_seqs, seq_len), BCIR_EXT_IGNORE, dtype=np.uint32)
    filler_pool = np.arange(ord("A"), ord("Z") + 1, dtype=np.uint32)
    for i in range(n_seqs):
        keys = rng.choice(_KEY_TOKS, size=n_pairs, replace=False)
        vals = rng.choice(_VAL_TOKS, size=n_pairs, replace=True)
        q_idx = int(rng.integers(0, n_pairs))
        if mode == "value_swap" and n_pairs >= 2:
            other = (q_idx + 1) % n_pairs
            while vals[other] == vals[q_idx]:
                vals[other] = rng.choice(_VAL_TOKS)
            vals[q_idx], vals[other] = vals[other], vals[q_idx]
        elif mode == "query_change" and n_pairs >= 2:
            q_idx = (q_idx + 1) % n_pairs
        seq: List[int] = []
        for p in range(n_pairs):
            seq.extend([int(keys[p]), _EQ, int(vals[p]), _SEP])
        if filler_len > 0:
            seq.extend(int(x) for x in rng.choice(filler_pool, size=filler_len, replace=True))
            seq.append(_SEP)
        if not multi:
            seq.extend([_QRY, int(keys[q_idx]), _EQ])
            if len(seq) > seq_len:
                raise ValueError(f"KV seq len {len(seq)} exceeds {seq_len}")
            toks[i, : len(seq)] = seq
            tgts[i, len(seq) - 1] = int(vals[q_idx])
        else:
            q_order = rng.permutation(n_pairs) if shuffled_query else np.arange(n_pairs)
            for idx_q, p in enumerate(q_order):
                seq.extend([_QRY, int(keys[p]), _EQ])
                tgts[i, len(seq) - 1] = int(vals[p])
                if idx_q + 1 < n_pairs:
                    seq.extend([int(vals[p]), _SEP])
            toks[i, : len(seq)] = seq
    return toks, tgts


def train_with_val_selection(
    model: Abi6Model,
    train_tk: np.ndarray,
    train_tg: np.ndarray,
    val_tk: np.ndarray,
    val_tg: np.ndarray,
    test_tk: np.ndarray,
    test_tg: np.ndarray,
    opt_cfg: AdamWParams,
    lrs: np.ndarray,
    eval_every: int,
    *,
    active_times: Optional[np.ndarray] = None,
    val_active_time: Optional[int] = None,
) -> Dict[str, Any]:
    steps = int(train_tk.shape[0])
    curve: List[Dict[str, Any]] = []
    t0 = time.perf_counter()

    v0 = model.evaluate(val_tk, val_tg, active_time=val_active_time)
    best_val_ce = v0["ce"] if v0["ok"] else float("inf")
    best_step = 0
    best_w = model.copy_weights()
    curve.append({
        "step": 0, "wall_s": 0.0, "train_loss": None,
        "val_ce": v0["ce"], "val_ppl": v0["ppl"], "val_acc": v0["accuracy"],
    })

    step_ms_all: List[float] = []
    s = 0
    rc_last = 0
    while s < steps:
        chunk = min(eval_every, steps - s)
        at_sl = active_times[s : s + chunk] if active_times is not None else None
        rc, evs = model.train_loop(
            train_tk[s : s + chunk], train_tg[s : s + chunk], opt_cfg,
            lrs=lrs[s : s + chunk], active_times=at_sl,
        )
        rc_last = rc
        for e in evs:
            step_ms_all.append(float(e["step_ms"]))
        s += chunk
        if rc != 0:
            break
        vv = model.evaluate(val_tk, val_tg, active_time=val_active_time)
        wall_s = time.perf_counter() - t0
        curve.append({
            "step": s,
            "wall_s": round(wall_s, 3),
            "train_loss": round(float(np.mean([e["loss"] for e in evs[-min(8, len(evs)):]])), 6),
            "grad_norm_mean": round(float(np.mean([e["grad_norm"] for e in evs])), 4),
            "val_ce": vv["ce"],
            "val_ppl": vv["ppl"],
            "val_acc": vv["accuracy"],
            "step_ms_median": round(float(np.median([e["step_ms"] for e in evs])), 3),
        })
        if vv["ok"] and vv["ce"] < best_val_ce:
            best_val_ce = vv["ce"]
            best_step = s
            best_w = model.copy_weights()

    model.set_weights(best_w)
    model.reset_telemetry()
    test_res = model.evaluate(test_tk, test_tg, active_time=val_active_time)
    eval_tel = model.telemetry()
    return {
        "rc": rc_last,
        "steps_completed": s,
        "best_step": best_step,
        "best_val_ce": best_val_ce,
        "test_ce": test_res["ce"],
        "test_ppl": test_res["ppl"],
        "test_acc": test_res["accuracy"],
        "wall_s": round(time.perf_counter() - t0, 3),
        "step_ms_median": round(float(np.median(step_ms_all)) if step_ms_all else 0.0, 3),
        "eval_telemetry": eval_tel,
        "mem_stats": model.mem_stats(),
        "curve": curve,
        "best_weights": best_w,
    }


def host_info() -> Dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    bcir_sha = subprocess.check_output(
        ["git", "-C", str(BCIR_REPO), "rev-parse", "--short", "HEAD"], text=True
    ).strip()
    return {
        "cpu": cpu,
        "cores": os.cpu_count(),
        "platform": platform.platform(),
        "bcir_runtime_commit": bcir_sha,
        "abi_version": 6,
    }


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
