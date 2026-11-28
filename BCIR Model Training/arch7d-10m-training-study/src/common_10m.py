#!/usr/bin/env python3
"""Shared BCIR ABI 6 harness, crash-atomic checkpoints, BCIRQ8/COMP-1 tools, and forensics
for the BCIR-Arch7d-10M multi-stage training study."""
from __future__ import annotations

import csv
import ctypes as ct
import hashlib
import json
import math
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

STUDY_DIR = Path(__file__).resolve().parent.parent
BUILD_DIR = STUDY_DIR / "build"
RESULTS_DIR = STUDY_DIR / "results"
FIG_DIR = STUDY_DIR / "figures"
CKPT_DIR = STUDY_DIR / "checkpoints"
LIB_PATH = BUILD_DIR / "libbcir_mixer_ext.so"
WIKITEXT_DIR = STUDY_DIR.parent / "wedge-pr818" / "data"

for _d in (RESULTS_DIR, FIG_DIR, CKPT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ABI 6 constants (must match runtime/c/bcir_mixer_ext.h & bcir_precision.h)
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

GEMM_FP32_KBLOCK = 0
OPT_FP32 = 0
OPT_Q8_MOMENTS = 1
OPT_Q8_BF16_GRAD = 2
OPT_ADAFACTOR_M2 = 3
OPT_ADAFACTOR_BF16_GRAD = 4
OPT_MUON_FP32 = 5
OPT_Q8_MUON_BF16_GRAD = 6

INIT_MUP_V1 = 2
FP64_GUARD_CONDITIONAL = 1

OPT_NAMES = {
    OPT_FP32: "Opt_FP32_AdamW",
    OPT_Q8_MOMENTS: "Opt_Q8_Moments",
    OPT_Q8_BF16_GRAD: "Opt_Q8_BF16Grad",
    OPT_ADAFACTOR_M2: "Opt_Adafactor",
    OPT_ADAFACTOR_BF16_GRAD: "Opt_Adafactor_BF16Grad",
    OPT_MUON_FP32: "Opt_Muon_FP32",
    OPT_Q8_MUON_BF16_GRAD: "Opt_Q8_Muon_BF16Grad",
}


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


class AdamWParams(ct.Structure):
    _fields_ = [
        ("lr", ct.c_double),
        ("beta1", ct.c_double),
        ("beta2", ct.c_double),
        ("epsilon", ct.c_double),
        ("weight_decay", ct.c_double),
        ("grad_clip", ct.c_double),
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


def ensure_built() -> Path:
    if not LIB_PATH.exists():
        subprocess.run(["make", "-C", str(STUDY_DIR), "all"], check=True)
    return LIB_PATH


def load_lib() -> ct.CDLL:
    global _LIB
    if _LIB is not None:
        return _LIB
    lib = ct.CDLL(str(ensure_built()))
    lib.bcir_mixer_ext_abi_version.restype = ct.c_uint32
    assert lib.bcir_mixer_ext_abi_version() == 6

    lib.bcir_prec_policy_default.argtypes = [ct.POINTER(PrecPolicy)]
    lib.bcir_ext_create.argtypes = [ct.POINTER(ExtSpec), ct.POINTER(PrecPolicy)]
    lib.bcir_ext_create.restype = ct.c_void_p
    lib.bcir_ext_destroy.argtypes = [ct.c_void_p]
    lib.bcir_ext_get_mem_stats.argtypes = [ct.c_void_p, ct.POINTER(MemStats)]
    lib.bcir_ext_get_telemetry.argtypes = [ct.c_void_p, ct.POINTER(PrecTelemetry)]
    lib.bcir_ext_reset_telemetry.argtypes = [ct.c_void_p]
    lib.bcir_ext_reset_opt.argtypes = [ct.c_void_p]
    lib.bcir_ext_opt_state_bytes.argtypes = [ct.c_void_p]
    lib.bcir_ext_opt_state_bytes.restype = ct.c_size_t
    lib.bcir_ext_save_opt_state.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_size_t]
    lib.bcir_ext_save_opt_state.restype = ct.c_int
    lib.bcir_ext_load_opt_state.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_size_t]
    lib.bcir_ext_load_opt_state.restype = ct.c_int
    lib.bcir_ext_weights_ptr.argtypes = [ct.c_void_p]
    lib.bcir_ext_weights_ptr.restype = ct.POINTER(ct.c_float)
    lib.bcir_ext_grads_fp32_ptr.argtypes = [ct.c_void_p]
    lib.bcir_ext_grads_fp32_ptr.restype = ct.POINTER(ct.c_float)
    lib.bcir_ext_param_count.argtypes = [ct.c_void_p]
    lib.bcir_ext_param_count.restype = ct.c_size_t
    lib.bcir_ext_tensor_count.argtypes = [ct.c_void_p]
    lib.bcir_ext_tensor_count.restype = ct.c_size_t
    lib.bcir_ext_tensor_info_at.argtypes = [ct.c_void_p, ct.c_size_t, ct.POINTER(TensorInfo)]
    lib.bcir_ext_tensor_info_at.restype = ct.c_int

    lib.bcir_ext_forward.argtypes = [
        ct.c_void_p,
        ct.POINTER(ct.c_uint32),
        ct.c_uint32,
        ct.POINTER(ct.c_float),
    ]
    lib.bcir_ext_forward.restype = ct.c_int

    lib.bcir_ext_loss_backward.argtypes = [
        ct.c_void_p,
        ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32),
        ct.c_uint32,
        ct.c_int,
        ct.POINTER(ct.c_double),
        ct.POINTER(ct.c_uint64),
    ]
    lib.bcir_ext_loss_backward.restype = ct.c_int

    lib.bcir_ext_train_loop.argtypes = [
        ct.c_void_p,
        ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_double),
        ct.c_size_t,
        ct.POINTER(AdamWParams),
        ct.POINTER(StepMetrics),
    ]
    lib.bcir_ext_train_loop.restype = ct.c_int

    lib.bcir_ext_evaluate.argtypes = [
        ct.c_void_p,
        ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32),
        ct.c_size_t,
        ct.c_uint32,
        ct.POINTER(EvalResult),
    ]
    lib.bcir_ext_evaluate.restype = ct.c_int

    lib.bcir_quant_q8_g32_encode.argtypes = [
        ct.POINTER(ct.c_float),
        ct.POINTER(ct.c_int8),
        ct.POINTER(ct.c_int8),
        ct.c_size_t,
    ]
    lib.bcir_quant_q8_g32_decode.argtypes = [
        ct.POINTER(ct.c_int8),
        ct.POINTER(ct.c_int8),
        ct.POINTER(ct.c_float),
        ct.c_size_t,
    ]
    _LIB = lib
    return lib


def make_policy(
    *,
    opt_kind: int = OPT_Q8_MUON_BF16_GRAD,
    fp64_guard_mode: int = FP64_GUARD_CONDITIONAL,
) -> PrecPolicy:
    lib = load_lib()
    pol = PrecPolicy()
    lib.bcir_prec_policy_default(ct.byref(pol))
    pol.attn_fwd_gemm = GEMM_FP32_KBLOCK
    pol.attn_bwd_gemm = GEMM_FP32_KBLOCK
    pol.mlp_fwd_gemm = GEMM_FP32_KBLOCK
    pol.mlp_bwd_gemm = GEMM_FP32_KBLOCK
    pol.opt_kind = opt_kind
    pol.fp64_guard_mode = fp64_guard_mode
    return pol


def make_spec(
    *,
    vocab: int = 256,
    width: int = 512,
    heads: int = 8,
    kvheads: int = 2,
    layers: int = 4,
    ff: int = 896,
    batch: int = 8,
    time_len: int = 256,
    tied: int = 1,
    layer_mixers: Optional[Sequence[int]] = None,
    layer_use_rope: Optional[Sequence[int]] = None,
    swa_window: int = 16,
    delta_chunk: int = 16,
    mla_dc: int = 192,
    mla_dr: int = 32,
    init_policy: int = INIT_MUP_V1,
    mup_d_base: int = 128,
    seed: int = 42,
    threads: int = 2,
    skip_init: int = 0,
    gate_mode: int = GATE_G1_HEADWISE,
    qk_norm: int = 1,
    canon_mode: int = CANON_AC,
    mhc_streams: int = 2,
    online_softmax: int = 1,
    gate_rank: int = 32,
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
    mixers = list(layer_mixers) if layer_mixers is not None else [MIXER_GDN2, MIXER_GDN2, MIXER_GDN2, MIXER_MLA]
    ropes = list(layer_use_rope) if layer_use_rope is not None else [0] * layers
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


def spec_to_dict(sp: ExtSpec) -> Dict[str, Any]:
    L = int(sp.layers)
    return {
        "vocab": int(sp.vocab),
        "width": int(sp.width),
        "heads": int(sp.heads),
        "kvheads": int(sp.kvheads),
        "layers": L,
        "ff": int(sp.ff),
        "batch": int(sp.batch),
        "time": int(sp.time),
        "tied": int(sp.tied),
        "rope_base": float(sp.rope_base),
        "rms_eps": float(sp.rms_eps),
        "layer_mixer": [int(sp.layer_mixer[i]) for i in range(L)],
        "layer_use_rope": [int(sp.layer_use_rope[i]) for i in range(L)],
        "swa_window": int(sp.swa_window),
        "delta_chunk": int(sp.delta_chunk),
        "mla_dc": int(sp.mla_dc),
        "mla_dr": int(sp.mla_dr),
        "init_policy": int(sp.init_policy),
        "mup_d_base": int(sp.mup_d_base),
        "seed": int(sp.seed),
        "threads": int(sp.threads),
        "gate_mode": int(sp.gate_mode),
        "qk_norm": int(sp.qk_norm),
        "canon_mode": int(sp.canon_mode),
        "mhc_streams": int(sp.mhc_streams),
        "online_softmax": int(sp.online_softmax),
        "gate_rank": int(sp.gate_rank),
    }


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

    def copy_grads_fp32(self) -> np.ndarray:
        ptr = self.lib.bcir_ext_grads_fp32_ptr(self.ptr)
        if not ptr:
            raise RuntimeError("bcir_ext_grads_fp32_ptr returned NULL (optimizer uses BF16 grad buffer)")
        return np.ctypeslib.as_array(ptr, shape=(self.n_params,)).copy()

    def copy_opt_state(self) -> bytes:
        nbytes = int(self.lib.bcir_ext_opt_state_bytes(self.ptr))
        if nbytes <= 0:
            raise RuntimeError("bcir_ext_opt_state_bytes returned 0")
        buf = ct.create_string_buffer(nbytes)
        rc = self.lib.bcir_ext_save_opt_state(self.ptr, buf, nbytes)
        if rc != 0:
            raise RuntimeError(f"bcir_ext_save_opt_state failed with rc={rc}")
        return buf.raw

    def set_opt_state(self, raw: bytes) -> None:
        buf = ct.create_string_buffer(raw, len(raw))
        rc = self.lib.bcir_ext_load_opt_state(self.ptr, buf, len(raw))
        if rc != 0:
            raise RuntimeError(f"bcir_ext_load_opt_state failed with rc={rc}")

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

    def forward_logits(self, tokens: np.ndarray, *, active_time: Optional[int] = None) -> np.ndarray:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        B = int(self.spec.batch)
        act_t = int(self.spec.time if active_time is None else active_time)
        V = int(self.spec.vocab)
        out = np.empty((B, act_t, V), dtype=np.float32)
        rc = int(self.lib.bcir_ext_forward(
            self.ptr,
            tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            act_t,
            out.ctypes.data_as(ct.POINTER(ct.c_float)),
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_ext_forward failed with rc={rc}")
        return out

    def loss_backward(
        self,
        tokens: np.ndarray,
        targets: np.ndarray,
        *,
        active_time: Optional[int] = None,
        accumulate: int = 0,
    ) -> Tuple[float, int]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        act_t = int(self.spec.time if active_time is None else active_time)
        loss = ct.c_double(0.0)
        sup = ct.c_uint64(0)
        rc = int(self.lib.bcir_ext_loss_backward(
            self.ptr,
            tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tg.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            act_t,
            accumulate,
            ct.byref(loss),
            ct.byref(sup),
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_ext_loss_backward failed with rc={rc}")
        return float(loss.value), int(sup.value)

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
            "bpb": float(ce / math.log(2.0)) if ok else None,
            "accuracy": float(res.accuracy),
            "tokens": int(res.tokens),
            "nonfinite": int(res.nonfinite),
            "eval_ms": float(res.eval_ms),
        }


# ==================== Quantization & COMP-1 Accounting ====================
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


def quantize_bcirq8_per_tensor(model: Abi6Model, w: np.ndarray) -> Tuple[np.ndarray, bytes]:
    """Encode/decode exact per-tensor BCIRQ8 v1 payload (`int8 codes + int8 exponents` per tensor)."""
    lib = load_lib()
    src_all = np.ascontiguousarray(w, dtype=np.float32)
    dst_all = np.empty_like(src_all)
    chunks: List[bytes] = []
    for ti in model.tensor_infos():
        off = ti["offset"]
        sz = ti["rows"] * ti["cols"]
        nb = (sz + 31) // 32
        sub = np.ascontiguousarray(src_all[off : off + sz], dtype=np.float32)
        q = np.empty(sz, dtype=np.int8)
        e = np.empty(nb, dtype=np.int8)
        rec = np.empty(sz, dtype=np.float32)
        lib.bcir_quant_q8_g32_encode(
            sub.ctypes.data_as(ct.POINTER(ct.c_float)),
            q.ctypes.data_as(ct.POINTER(ct.c_int8)),
            e.ctypes.data_as(ct.POINTER(ct.c_int8)),
            sz,
        )
        lib.bcir_quant_q8_g32_decode(
            q.ctypes.data_as(ct.POINTER(ct.c_int8)),
            e.ctypes.data_as(ct.POINTER(ct.c_int8)),
            rec.ctypes.data_as(ct.POINTER(ct.c_float)),
            sz,
        )
        dst_all[off : off + sz] = rec
        chunks.append(q.tobytes())
        chunks.append(e.tobytes())
    return dst_all, b"".join(chunks)


def decode_bcirq8_per_tensor(model: Abi6Model, payload: bytes) -> np.ndarray:
    """Decode exact per-tensor BCIRQ8 v1 payload back to FP32 weights."""
    lib = load_lib()
    out = np.empty(model.n_params, dtype=np.float32)
    pos = 0
    for ti in model.tensor_infos():
        off = ti["offset"]
        sz = ti["rows"] * ti["cols"]
        nb = (sz + 31) // 32
        q = np.frombuffer(payload, dtype=np.int8, count=sz, offset=pos)
        pos += sz
        e = np.frombuffer(payload, dtype=np.int8, count=nb, offset=pos)
        pos += nb
        rec = np.empty(sz, dtype=np.float32)
        lib.bcir_quant_q8_g32_decode(
            q.ctypes.data_as(ct.POINTER(ct.c_int8)),
            e.ctypes.data_as(ct.POINTER(ct.c_int8)),
            rec.ctypes.data_as(ct.POINTER(ct.c_float)),
            sz,
        )
        out[off : off + sz] = rec
    if pos != len(payload):
        raise ValueError(f"BCIRQ8 payload length mismatch: consumed {pos} != {len(payload)}")
    return out


def _rank1_mag_svd(abs_mat: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    u, s, vt = np.linalg.svd(abs_mat.astype(np.float64), full_matrices=False)
    rt = math.sqrt(max(float(s[0]), 0.0)) if s.size else 0.0
    return np.abs(u[:, 0]) * rt, np.abs(vt[0, :]) * rt


def joint_itq_rotate(
    u_prime: np.ndarray, v_prime: np.ndarray, iters: int = 25, seed: int = 42
) -> Tuple[np.ndarray, np.ndarray]:
    """LittleBit-2 (ICML 2026 Alg. 1): Joint Orthogonal Procrustes alignment on Z = [U'; V']."""
    r = u_prime.shape[1]
    if r <= 1 or iters <= 0:
        return u_prime.astype(np.float64), v_prime.astype(np.float64)
    z = np.vstack([u_prime, v_prime]).astype(np.float64)
    rng = np.random.default_rng(seed)
    rot, _ = np.linalg.qr(rng.standard_normal((r, r)))
    for _ in range(iters):
        b = np.where(z @ rot >= 0.0, 1.0, -1.0)
        phi, _s, psi_t = np.linalg.svd(b.T @ z, full_matrices=False)
        rot = psi_t.T @ phi.T
    return u_prime @ rot, v_prime @ rot


def dual_svid_1bit_factorize(
    mat: np.ndarray, rank: int = 16, itq_iters: int = 25, seed: int = 42, scale_bits: int = 32
) -> Dict[str, Any]:
    """Return exact COMP-1 Dual-SVID + Joint-ITQ binary factors and rounded continuous scales."""
    if scale_bits not in (16, 32):
        raise ValueError(f"scale_bits must be 16 or 32, got {scale_bits}")
    scale_dtype = np.float16 if scale_bits == 16 else np.float32
    W = np.asarray(mat, dtype=np.float64)
    m, n = W.shape
    r = min(rank, m, n)
    B = np.where(W >= 0.0, 1.0, -1.0)
    alpha_r, alpha_c = _rank1_mag_svd(np.abs(W))
    alpha_r = np.maximum(alpha_r, 1e-8)
    alpha_c = np.maximum(alpha_c, 1e-8)
    for _ in range(3):
        denom_r = np.sum((B * alpha_c[None, :]) ** 2, axis=1) + 1e-12
        alpha_r = np.maximum(np.sum(W * B * alpha_c[None, :], axis=1) / denom_r, 1e-8)
        denom_c = np.sum((B * alpha_r[:, None]) ** 2, axis=0) + 1e-12
        alpha_c = np.maximum(np.sum(W * B * alpha_r[:, None], axis=0) / denom_c, 1e-8)
    alpha_r_q = alpha_r.astype(scale_dtype).astype(np.float64)
    alpha_c_q = alpha_c.astype(scale_dtype).astype(np.float64)
    base = (alpha_r_q[:, None] * B) * alpha_c_q[None, :]
    res = W - base
    bu = np.zeros((m, 0), dtype=np.float64)
    bv = np.zeros((n, 0), dtype=np.float64)
    h_u_q = np.zeros(m, dtype=np.float64)
    g_v_q = np.zeros(n, dtype=np.float64)
    lam_q = np.zeros(0, dtype=np.float64)
    if r > 0:
        U, S, Vt = np.linalg.svd(res, full_matrices=False)
        rt_s = np.sqrt(np.maximum(S[:r], 0.0))
        u_cont = U[:, :r] * rt_s[None, :]
        v_cont = Vt[:r, :].T * rt_s[None, :]
        u_rot, v_rot = joint_itq_rotate(u_cont, v_cont, iters=itq_iters, seed=seed)
        bu = np.where(u_rot >= 0.0, 1.0, -1.0)
        bv = np.where(v_rot >= 0.0, 1.0, -1.0)
        h_u, _ = _rank1_mag_svd(np.abs(u_rot))
        g_v, _ = _rank1_mag_svd(np.abs(v_rot))
        hu = h_u[:, None] * bu
        vg = (g_v[:, None] * bv).T
        gram = (hu.T @ hu) * (vg @ vg.T)
        rhs = np.sum(hu * (res @ vg.T), axis=0)
        ridge = 1e-8 * (float(np.trace(gram)) / max(r, 1) + 1e-12)
        lam = np.linalg.solve(gram + ridge * np.eye(r), rhs)
        h_u_q = h_u.astype(scale_dtype).astype(np.float64)
        g_v_q = g_v.astype(scale_dtype).astype(np.float64)
        lam_q = lam.astype(scale_dtype).astype(np.float64)
    return {
        "B": B,
        "alpha_r": alpha_r_q,
        "alpha_c": alpha_c_q,
        "bu": bu,
        "bv": bv,
        "h_u": h_u_q,
        "g_v": g_v_q,
        "lam": lam_q,
        "rank": r,
        "scale_bits": scale_bits,
    }


def dual_svid_1bit_reconstruct(fac: Dict[str, Any], *, round_scales: bool = True) -> np.ndarray:
    scale_dtype = np.float16 if fac["scale_bits"] == 16 else np.float32
    ar = fac["alpha_r"].astype(scale_dtype).astype(np.float64) if round_scales else fac["alpha_r"]
    ac = fac["alpha_c"].astype(scale_dtype).astype(np.float64) if round_scales else fac["alpha_c"]
    base = (ar[:, None] * fac["B"]) * ac[None, :]
    if fac["rank"] > 0:
        hu = fac["h_u"].astype(scale_dtype).astype(np.float64) if round_scales else fac["h_u"]
        gv = fac["g_v"].astype(scale_dtype).astype(np.float64) if round_scales else fac["g_v"]
        lm = fac["lam"].astype(scale_dtype).astype(np.float64) if round_scales else fac["lam"]
        recon = base + ((hu[:, None] * fac["bu"]) * lm[None, :]) @ (gv[:, None] * fac["bv"]).T
    else:
        recon = base
    return recon.astype(np.float32)


def dual_svid_1bit_ptq(
    mat: np.ndarray, rank: int = 16, itq_iters: int = 25, seed: int = 42, scale_bits: int = 32
) -> np.ndarray:
    """BCIR COMP-1 Dual-SVID + Joint-ITQ packed-1-bit PTQ with explicit scale_bits (16 or 32)."""
    fac = dual_svid_1bit_factorize(mat, rank=rank, itq_iters=itq_iters, seed=seed, scale_bits=scale_bits)
    return dual_svid_1bit_reconstruct(fac, round_scales=True)


def q8_weight_bytes(model: Abi6Model) -> int:
    total = 0
    for ti in model.tensor_infos():
        sz = ti["rows"] * ti["cols"]
        total += sz + (sz + 31) // 32
    return total


def comp1_weight_bytes(
    model: Abi6Model, rank: int = 16, scale_bits: int = 32
) -> Dict[str, Any]:
    if scale_bits not in (16, 32):
        raise ValueError(f"scale_bits must be 16 or 32, got {scale_bits}")
    s_bytes = scale_bits // 8
    mlp_bytes = 0
    mlp_params = 0
    other_bytes = 0
    elig = 0
    for ti in model.tensor_infos():
        m, n = int(ti["rows"]), int(ti["cols"])
        sz = m * n
        if ti["region"] == 2 and min(m, n) >= 512:
            r = min(rank, m, n)
            bin_b = (sz + 7) // 8
            if r > 0:
                bin_b += (r * m + 7) // 8 + (r * n + 7) // 8
                n_sc = 2 * (m + n) + r
            else:
                n_sc = m + n
            mlp_bytes += bin_b + n_sc * s_bytes
            mlp_params += sz
            elig += 1
        else:
            other_bytes += sz + (sz + 31) // 32
    total_b = mlp_bytes + other_bytes
    bpw = (8.0 * mlp_bytes / mlp_params) if mlp_params > 0 else 0.0
    return {
        "total_bytes": total_b,
        "mlp_comp1_bytes": mlp_bytes,
        "other_q8_bytes": other_bytes,
        "mlp_comp1_params": mlp_params,
        "mlp_comp1_bpw": round(bpw, 4),
        "eligible_tensors": elig,
        "rank": rank,
        "scale_bits": scale_bits,
    }


def apply_comp1_ptq_weights(
    model: Abi6Model, base_w: np.ndarray, rank: int = 16, scale_bits: int = 32, itq_iters: int = 25
) -> np.ndarray:
    out, _ = quantize_bcirq8_per_tensor(model, base_w)
    for ti in model.tensor_infos():
        if ti["region"] == 2 and min(ti["rows"], ti["cols"]) >= 512:
            off = ti["offset"]
            sz = ti["rows"] * ti["cols"]
            mat = base_w[off : off + sz].reshape(ti["rows"], ti["cols"])
            out[off : off + sz] = dual_svid_1bit_ptq(
                mat, rank=rank, itq_iters=itq_iters, scale_bits=scale_bits
            ).reshape(-1)
    return out


# ==================== Crash-Atomic Checkpoints ====================
def _ckpt_stem_and_json(path: Path) -> Tuple[str, Path]:
    base_stem = path.stem if path.suffix in (".json", ".bin") else path.name
    return base_stem, path.parent / f"{base_stem}.json"


def save_ckpt_atomic(
    path: Path,
    weights: np.ndarray,
    meta: Dict[str, Any],
    *,
    opt_state: Optional[bytes] = None,
    best_weights: Optional[np.ndarray] = None,
) -> str:
    """Crash-atomic generation-tagged multi-file checkpoint save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base_stem, json_p = _ckpt_stem_and_json(path)
    prev_meta: Optional[Dict[str, Any]] = None
    prev_gen = 0
    if json_p.exists():
        try:
            prev_meta = json.loads(json_p.read_text(encoding="utf-8"))
            prev_gen = int(prev_meta.get("generation", 0))
        except Exception:
            prev_meta = None
            prev_gen = 0
    gen = prev_gen + 1

    raw = np.ascontiguousarray(weights, dtype=np.float32).tobytes()
    sha = hashlib.sha256(raw).hexdigest()
    gen_prefix = f"{base_stem}.g{gen}_{sha[:8]}"
    bin_p = path.parent / f"{gen_prefix}.bin"
    tmp_b = path.parent / f".{gen_prefix}.bin.tmp"
    tmp_b.write_bytes(raw)
    os.replace(tmp_b, bin_p)

    meta_out = dict(meta, generation=gen, weights_sha256=sha, weights_file=bin_p.name)
    new_files = {bin_p.name}
    if opt_state is not None:
        opt_p = path.parent / f"{gen_prefix}.opt.bin"
        opt_sha = hashlib.sha256(opt_state).hexdigest()
        tmp_o = path.parent / f".{gen_prefix}.opt.bin.tmp"
        tmp_o.write_bytes(opt_state)
        os.replace(tmp_o, opt_p)
        meta_out["opt_state_file"] = opt_p.name
        meta_out["opt_state_sha256"] = opt_sha
        meta_out["opt_state_bytes"] = len(opt_state)
        new_files.add(opt_p.name)
    if best_weights is not None:
        best_raw = np.ascontiguousarray(best_weights, dtype=np.float32).tobytes()
        best_p = path.parent / f"{gen_prefix}.best.bin"
        best_sha = hashlib.sha256(best_raw).hexdigest()
        tmp_bw = path.parent / f".{gen_prefix}.best.bin.tmp"
        tmp_bw.write_bytes(best_raw)
        os.replace(tmp_bw, best_p)
        meta_out["best_weights_file"] = best_p.name
        meta_out["best_weights_sha256"] = best_sha
        new_files.add(best_p.name)

    tmp_j = path.parent / f".{base_stem}.g{gen}.json.tmp"
    tmp_j.write_text(json.dumps(meta_out, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp_j, json_p)

    if prev_meta is not None:
        for key in ("weights_file", "opt_state_file", "best_weights_file"):
            old_name = prev_meta.get(key)
            if isinstance(old_name, str) and old_name not in new_files:
                (path.parent / old_name).unlink(missing_ok=True)
    return sha


def load_ckpt_verified(
    path: Path,
) -> Tuple[np.ndarray, Dict[str, Any], Optional[bytes], Optional[np.ndarray]]:
    _, json_p = _ckpt_stem_and_json(path)
    meta = json.loads(json_p.read_text(encoding="utf-8"))
    raw = (json_p.parent / meta["weights_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["weights_sha256"]:
        raise ValueError(f"SHA-256 mismatch loading weights from {path}")
    opt_state: Optional[bytes] = None
    if "opt_state_file" in meta:
        opt_raw = (json_p.parent / meta["opt_state_file"]).read_bytes()
        if hashlib.sha256(opt_raw).hexdigest() != meta["opt_state_sha256"]:
            raise ValueError(f"SHA-256 mismatch loading optimizer state from {path}")
        opt_state = opt_raw
    best_weights: Optional[np.ndarray] = None
    if "best_weights_file" in meta:
        best_raw = (json_p.parent / meta["best_weights_file"]).read_bytes()
        if hashlib.sha256(best_raw).hexdigest() != meta["best_weights_sha256"]:
            raise ValueError(f"SHA-256 mismatch loading best weights from {path}")
        best_weights = np.frombuffer(best_raw, dtype=np.float32).copy()
    return np.frombuffer(raw, dtype=np.float32).copy(), meta, opt_state, best_weights


def save_bcirq8_ckpt(path: Path, model: Abi6Model, weights: np.ndarray, meta: Dict[str, Any]) -> Tuple[str, np.ndarray]:
    """Save deterministic per-tensor BCIRQ8 v1 weights + verified JSON metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base_stem, json_p = _ckpt_stem_and_json(path)
    dec_w, payload = quantize_bcirq8_per_tensor(model, weights)
    bin_p = path.parent / f"{base_stem}.bin"
    tmp_b = path.parent / f".{base_stem}.bin.tmp"
    tmp_b.write_bytes(payload)
    os.replace(tmp_b, bin_p)
    sha = hashlib.sha256(payload).hexdigest()
    meta_out = dict(
        meta,
        format="bcirq8_v1_per_tensor_g32",
        weights_file=bin_p.name,
        weights_sha256=sha,
        weights_bytes=len(payload),
        fp32_source_sha256=hashlib.sha256(
            np.ascontiguousarray(weights, dtype=np.float32).tobytes()
        ).hexdigest(),
    )
    tmp_j = path.parent / f".{base_stem}.json.tmp"
    tmp_j.write_text(json.dumps(meta_out, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp_j, json_p)
    return sha, dec_w


def load_bcirq8_ckpt(path: Path, model: Abi6Model) -> Tuple[np.ndarray, Dict[str, Any]]:
    _, json_p = _ckpt_stem_and_json(path)
    meta = json.loads(json_p.read_text(encoding="utf-8"))
    payload = (json_p.parent / meta["weights_file"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != meta["weights_sha256"]:
        raise ValueError(f"BCIRQ8 SHA-256 mismatch loading {path}")
    return decode_bcirq8_per_tensor(model, payload), meta


# ==================== Dataset & Scheduling ====================
_WIKI_CACHE: Dict[str, np.ndarray] = {}


def load_wikitext_bytes(split: str) -> np.ndarray:
    if split in _WIKI_CACHE:
        return _WIKI_CACHE[split]
    import pyarrow.parquet as pq

    candidates = [
        WIKITEXT_DIR / f"wikitext2-{split}.parquet",
        WIKITEXT_DIR / f"{split}-00000-of-00001.parquet",
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


def sample_lm_steps(
    stream: np.ndarray, start_step: int, n_steps: int, batch: int, seq_len: int, base_seed: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Deterministic per-step byte sampler invariant to chunk/eval/checkpoint boundaries."""
    max_off = len(stream) - seq_len - 2
    offs = np.empty((n_steps, batch), dtype=np.int64)
    for k in range(n_steps):
        rng = np.random.default_rng(base_seed + start_step + k)
        offs[k] = rng.integers(0, max_off, size=batch)
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
    lrs = np.empty(steps, dtype=np.float64)
    for s in range(steps):
        if s < warmup:
            lrs[s] = max_lr * (s + 1) / max(1, warmup)
        else:
            prog = (s - warmup) / max(1, steps - warmup - 1)
            lrs[s] = min_lr + 0.5 * (max_lr - min_lr) * (1.0 + math.cos(math.pi * min(1.0, prog)))
    return lrs


# ==================== Forensics & Diagnostics ====================
def process_rss_mib() -> Dict[str, float]:
    out = {"rss_mib": 0.0, "hwm_mib": 0.0}
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                out["rss_mib"] = round(int(line.split()[1]) / 1024.0, 2)
            elif line.startswith("VmHWM:"):
                out["hwm_mib"] = round(int(line.split()[1]) / 1024.0, 2)
    except OSError:
        pass
    return out


def _sinkhorn_doubly_stochastic(logits: np.ndarray, t_max: int = 20) -> np.ndarray:
    """Exact mirror of BCIR's mHC Sinkhorn-Knopp Birkhoff projection."""
    m = np.exp(logits - np.max(logits))
    for _ in range(t_max):
        m = m / (np.sum(m, axis=1, keepdims=True) + 1e-12)
        m = m / (np.sum(m, axis=0, keepdims=True) + 1e-12)
    return m


def analyze_weights_forensics(model: Abi6Model, weights: np.ndarray) -> Dict[str, Any]:
    """Extract per-tensor spectral rank, Frobenius/spectral norms, Canon kernels, and mHC matrices."""
    w = np.asarray(weights, dtype=np.float32)
    tensors_out: List[Dict[str, Any]] = []
    mlp_r90_fracs: List[float] = []
    mlp_stable_ranks: List[float] = []
    attn_r90_fracs: List[float] = []
    canon_kernels: Dict[str, Any] = {}
    mhc_summary: Dict[str, Any] = {}

    for ti in model.tensor_infos():
        name = ti["name"]
        off = ti["offset"]
        r, c = ti["rows"], ti["cols"]
        sz = r * c
        arr = w[off : off + sz].astype(np.float64)
        frob = float(np.linalg.norm(arr))
        rms = float(math.sqrt(float(np.mean(arr * arr)))) if sz > 0 else 0.0
        max_abs = float(np.max(np.abs(arr))) if sz > 0 else 0.0
        entry: Dict[str, Any] = {
            "name": name,
            "layer": ti["layer"],
            "region": ti["region"],
            "shape": [r, c],
            "frob_norm": round(frob, 5),
            "rms": round(rms, 6),
            "max_abs": round(max_abs, 5),
        }
        # Compute SVD spectral diagnostics for 2D matrices with min(r, c) >= 8
        if r >= 8 and c >= 8 and min(r, c) <= 512:
            mat = arr.reshape(r, c)
            s = np.linalg.svd(mat, compute_uv=False)
            s1 = float(s[0]) if s.size else 0.0
            s_min = float(s[-1]) if s.size else 0.0
            s2 = s * s
            tot_e = float(np.sum(s2)) + 1e-18
            cum = np.cumsum(s2) / tot_e
            r90 = int(np.searchsorted(cum, 0.90) + 1)
            r95 = int(np.searchsorted(cum, 0.95) + 1)
            sr = float(tot_e / (s1 * s1 + 1e-18))
            r90_frac = float(r90 / min(r, c))
            entry.update({
                "sigma1": round(s1, 5),
                "sigma_min": round(s_min, 5),
                "cond_ratio": round(s1 / max(s_min, 1e-8), 2),
                "stable_rank": round(sr, 2),
                "r90": r90,
                "r95": r95,
                "r90_frac": round(r90_frac, 4),
            })
            if ti["region"] == 2:
                mlp_r90_fracs.append(r90_frac)
                mlp_stable_ranks.append(sr)
            elif ti["region"] == 1:
                attn_r90_fracs.append(r90_frac)
        if "canon" in name:
            # Shape is [d, 4] or [4, d]
            k_mat = arr.reshape(r, c)
            tap_axis = 0 if r == 4 else 1
            tap_rms = np.sqrt(np.mean(k_mat * k_mat, axis=1 - tap_axis))
            tap_mean = np.mean(k_mat, axis=1 - tap_axis)
            canon_kernels[name] = {
                "shape": [r, c],
                "tap_rms": [round(float(x), 5) for x in tap_rms],
                "tap_mean": [round(float(x), 5) for x in tap_mean],
            }
        if "mhc" in name:
            if "res" in name and sz in (4, 16):
                side = int(round(math.sqrt(sz)))
                ds = _sinkhorn_doubly_stochastic(arr.reshape(side, side))
                mhc_summary[name] = {
                    "logits": [[round(float(v), 4) for v in row] for row in arr.reshape(side, side)],
                    "birkhoff_matrix": [[round(float(v), 5) for v in row] for row in ds],
                    "offdiag_mass": round(float(1.0 - np.mean(np.diag(ds))), 5),
                }
            else:
                mhc_summary[name] = [round(float(v), 5) for v in arr.tolist()]
        tensors_out.append(entry)

    return {
        "n_tensors": len(tensors_out),
        "total_frob_norm": round(float(np.linalg.norm(w.astype(np.float64))), 4),
        "mlp_mean_r90_frac": round(float(np.mean(mlp_r90_fracs)), 4) if mlp_r90_fracs else None,
        "mlp_mean_stable_rank": round(float(np.mean(mlp_stable_ranks)), 2) if mlp_stable_ranks else None,
        "attn_mean_r90_frac": round(float(np.mean(attn_r90_fracs)), 4) if attn_r90_fracs else None,
        "canon_kernels": canon_kernels,
        "mhc_summary": mhc_summary,
        "tensors": tensors_out,
    }


def eval_per_position_ce(
    model: Abi6Model, tokens: np.ndarray, targets: np.ndarray
) -> Dict[str, Any]:
    """Compute exact per-position CE, BPB, and top-1 accuracy using bcir_ext_forward."""
    nb, B, T = tokens.shape
    ce_pos = np.zeros(T, dtype=np.float64)
    acc_pos = np.zeros(T, dtype=np.float64)
    cnt_pos = np.zeros(T, dtype=np.int64)
    for b in range(nb):
        logits = model.forward_logits(tokens[b], active_time=T).astype(np.float64)  # [B, T, V]
        mx = np.max(logits, axis=-1, keepdims=True)
        lse = mx[..., 0] + np.log(np.sum(np.exp(logits - mx), axis=-1))
        tg = targets[b].astype(np.int64)
        valid = tg != BCIR_EXT_IGNORE
        tg_safe = np.where(valid, tg, 0)
        gathered = np.take_along_axis(logits, tg_safe[..., None], axis=-1)[..., 0]
        nll = lse - gathered
        preds = np.argmax(logits, axis=-1)
        correct = (preds == tg_safe) & valid
        ce_pos += np.sum(np.where(valid, nll, 0.0), axis=0)
        acc_pos += np.sum(correct, axis=0)
        cnt_pos += np.sum(valid, axis=0)

    mean_ce = ce_pos / np.maximum(cnt_pos, 1)
    mean_acc = acc_pos / np.maximum(cnt_pos, 1)

    edges = [0, 16, 32, 64, 128, 256, 384, 512, 768, 1024, 2048]
    buckets = []
    for a, b_end in zip(edges[:-1], edges[1:]):
        if a >= T:
            break
        hi = min(b_end, T)
        sl = slice(a, hi)
        c_sum = int(np.sum(cnt_pos[sl]))
        if c_sum == 0:
            continue
        ce_b = float(np.sum(ce_pos[sl]) / c_sum)
        acc_b = float(np.sum(acc_pos[sl]) / c_sum)
        buckets.append({
            "pos_range": f"{a}-{hi - 1}",
            "pos_start": a,
            "pos_end": hi - 1,
            "ce": round(ce_b, 5),
            "ppl": round(math.exp(min(ce_b, 50.0)), 4),
            "bpb": round(ce_b / math.log(2.0), 5),
            "accuracy": round(acc_b, 5),
            "tokens": c_sum,
        })
    overall_ce = float(np.sum(ce_pos) / max(int(np.sum(cnt_pos)), 1))
    return {
        "seq_len": T,
        "overall_ce": round(overall_ce, 6),
        "overall_ppl": round(math.exp(min(overall_ce, 50.0)), 4),
        "overall_bpb": round(overall_ce / math.log(2.0), 5),
        "overall_acc": round(float(np.sum(acc_pos) / max(int(np.sum(cnt_pos)), 1)), 5),
        "buckets": buckets,
        "per_pos_ce_stride8": [round(float(np.mean(mean_ce[i : min(i + 8, T)])), 5) for i in range(0, T, 8)],
    }


def generate_bytes(
    model: Abi6Model,
    prompt: bytes,
    max_new: int = 96,
    *,
    temperature: float = 0.0,
    seed: int = 42,
) -> str:
    """Autoregressive raw-byte generation using bcir_ext_forward (B=1 model)."""
    assert int(model.spec.batch) == 1
    T_max = int(model.spec.time)
    seq = list(prompt)
    rng = np.random.default_rng(seed)
    for _ in range(max_new):
        ctx = seq[-T_max:]
        arr = np.zeros((1, T_max), dtype=np.uint32)
        arr[0, : len(ctx)] = ctx
        logits = model.forward_logits(arr, active_time=len(ctx))[0, len(ctx) - 1].astype(np.float64)
        if temperature <= 0.0:
            nxt = int(np.argmax(logits))
        else:
            scaled = logits / temperature
            scaled -= np.max(scaled)
            probs = np.exp(scaled)
            probs /= np.sum(probs)
            nxt = int(rng.choice(len(probs), p=probs))
        seq.append(nxt)
    return bytes(seq).decode("utf-8", errors="replace")


# ==================== I/O & Host Metadata ====================
def host_info() -> Dict[str, Any]:
    cpu = platform.processor() or "unknown"
    flags: List[str] = []
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name") and cpu == "unknown":
                cpu = line.split(":", 1)[1].strip()
            if line.startswith("flags") and not flags:
                raw = line.split(":", 1)[1].strip().split()
                flags = [f for f in ("avx2", "fma", "avx512f", "avx512_bf16", "avx512_vnni", "amx_bf16") if f in raw]
    except OSError:
        pass
    bcir_root = STUDY_DIR.parent.parent.parent / "Binary-Correspondence-Intermediate-Representation"
    bcir_sha = "unknown"
    try:
        bcir_sha = subprocess.check_output(
            ["git", "-C", str(bcir_root), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        pass
    cc_ver = "unknown"
    try:
        cc_ver = subprocess.check_output(["cc", "--version"], text=True).splitlines()[0].strip()
    except Exception:
        pass
    return {
        "cpu_model": cpu,
        "cpu_count": os.cpu_count(),
        "simd_flags": flags,
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "compiler": cc_ver,
        "bcir_commit": bcir_sha,
        "abi_version": 6,
        "date": "2026-11-27",
    }


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_steps_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
