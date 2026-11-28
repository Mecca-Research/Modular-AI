#!/usr/bin/env python3
"""Shared BCIR ABI 6 + Stateful Activation Probe + SSP-Live + WSP-1/2/3 + COMP-1 harness
for the BCIR Weight-Space / State-Space Programming & Self-Improvement Memory (SIM) study."""
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
LIB_PATH = BUILD_DIR / "libbcir_wsp_sim.so"
WIKITEXT_DIR = STUDY_DIR.parent / "wedge-pr818" / "data"
ARCH7D_10M_CKPT_DIR = STUDY_DIR.parent / "arch7d-10m-training-study" / "checkpoints"

for _d in (RESULTS_DIR, FIG_DIR, CKPT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ABI 6 constants (must match BCIR runtime/c/bcir_mixer_ext.h & bcir_precision.h)
BCIR_EXT_MAX_LAYERS = 64
BCIR_EXT_IGNORE = 0xFFFFFFFF
BCIR_PROBE_MAX_HEADS = 32

MIXER_GQA = 0
MIXER_SWA = 1
MIXER_DELTANET = 2
MIXER_KDA = 3
MIXER_MLA = 4
MIXER_GDN2 = 5

MIXER_NAMES = {
    MIXER_GQA: "GQA",
    MIXER_SWA: "SWA",
    MIXER_DELTANET: "DeltaNet",
    MIXER_KDA: "KDA",
    MIXER_MLA: "MLA",
    MIXER_GDN2: "GDN-2",
}

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


class SspConfig(ct.Structure):
    _fields_ = [
        ("gdn_alpha_shift", ct.c_float),
        ("gdn_erase_shift", ct.c_float),
        ("gdn_write_shift", ct.c_float),
        ("state_echo_damp", ct.c_float),
        ("adaptive_echo_only", ct.c_uint32),
        ("echo_ngram", ct.c_uint32),
        ("echo_window", ct.c_uint32),
        ("mla_repeat_logit_bias", ct.c_float),
        ("g1_head_bias", ct.c_float * BCIR_PROBE_MAX_HEADS),
        ("enable_g1_bias", ct.c_uint32),
        ("rep_penalty", ct.c_float),
        ("presence_penalty", ct.c_float),
        ("ngram_block_penalty", ct.c_float),
        ("rep_window", ct.c_uint32),
        ("max_pos_cap", ct.c_uint32),
    ]


class ProbeStepOut(ct.Structure):
    _fields_ = [
        ("resid_post_mlp", ct.POINTER(ct.c_float)),
        ("mlp_hidden", ct.POINTER(ct.c_float)),
        ("g1_gates", ct.POINTER(ct.c_float)),
        ("gdn_alpha", ct.POINTER(ct.c_float)),
        ("gdn_erase", ct.POINTER(ct.c_float)),
        ("gdn_write", ct.POINTER(ct.c_float)),
        ("state_norm", ct.POINTER(ct.c_float)),
        ("state_cos_echo", ct.POINTER(ct.c_float)),
        ("attn_repeat", ct.POINTER(ct.c_float)),
        ("attn_entropy", ct.POINTER(ct.c_float)),
    ]


class ContrastAccum(ct.Structure):
    _fields_ = [
        ("loop_tokens", ct.c_uint64),
        ("clean_tokens", ct.c_uint64),
        ("resid_loop", ct.POINTER(ct.c_double)),
        ("resid_clean", ct.POINTER(ct.c_double)),
        ("mlp_hid_loop", ct.POINTER(ct.c_double)),
        ("mlp_hid_clean", ct.POINTER(ct.c_double)),
        ("mlp_hid_abs_loop", ct.POINTER(ct.c_double)),
        ("mlp_hid_abs_clean", ct.POINTER(ct.c_double)),
        ("g1_loop", ct.POINTER(ct.c_double)),
        ("g1_clean", ct.POINTER(ct.c_double)),
        ("alpha_loop", ct.POINTER(ct.c_double)),
        ("alpha_clean", ct.POINTER(ct.c_double)),
        ("erase_loop", ct.POINTER(ct.c_double)),
        ("erase_clean", ct.POINTER(ct.c_double)),
        ("write_loop", ct.POINTER(ct.c_double)),
        ("write_clean", ct.POINTER(ct.c_double)),
        ("st_echo_loop", ct.POINTER(ct.c_double)),
        ("st_echo_clean", ct.POINTER(ct.c_double)),
        ("attn_rep_loop", ct.POINTER(ct.c_double)),
        ("attn_rep_clean", ct.POINTER(ct.c_double)),
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
    lib.bcir_ext_forward.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32), ct.c_uint32, ct.POINTER(ct.c_float)]
    lib.bcir_ext_forward.restype = ct.c_int
    lib.bcir_ext_loss_backward.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
        ct.c_uint32, ct.c_int, ct.POINTER(ct.c_double), ct.POINTER(ct.c_uint64),
    ]
    lib.bcir_ext_loss_backward.restype = ct.c_int
    lib.bcir_ext_train_loop.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
        ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_double), ct.c_size_t,
        ct.POINTER(AdamWParams), ct.POINTER(StepMetrics),
    ]
    lib.bcir_ext_train_loop.restype = ct.c_int
    lib.bcir_ext_evaluate.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32),
        ct.c_size_t, ct.c_uint32, ct.POINTER(EvalResult),
    ]
    lib.bcir_ext_evaluate.restype = ct.c_int
    lib.bcir_quant_q8_g32_encode.argtypes = [ct.POINTER(ct.c_float), ct.POINTER(ct.c_int8), ct.POINTER(ct.c_int8), ct.c_size_t]
    lib.bcir_quant_q8_g32_decode.argtypes = [ct.POINTER(ct.c_int8), ct.POINTER(ct.c_int8), ct.POINTER(ct.c_float), ct.c_size_t]

    # Stateful probe + SSP-Live bindings
    lib.bcir_stateful_create.argtypes = [ct.c_void_p, ct.POINTER(ExtSpec), ct.c_uint32]
    lib.bcir_stateful_create.restype = ct.c_void_p
    lib.bcir_stateful_destroy.argtypes = [ct.c_void_p]
    lib.bcir_stateful_reset.argtypes = [ct.c_void_p]
    lib.bcir_stateful_sync_weights.argtypes = [ct.c_void_p, ct.c_void_p]
    lib.bcir_stateful_step.argtypes = [
        ct.c_void_p, ct.c_uint32, ct.c_int, ct.POINTER(SspConfig),
        ct.POINTER(ct.c_float), ct.POINTER(ProbeStepOut),
    ]
    lib.bcir_stateful_step.restype = ct.c_int
    lib.bcir_stateful_generate_batch.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.c_size_t, ct.c_uint32, ct.c_uint32,
        ct.POINTER(SspConfig), ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_float), ct.POINTER(ct.c_uint8),
    ]
    lib.bcir_stateful_generate_batch.restype = ct.c_int
    lib.bcir_stateful_probe_contrast.argtypes = [
        ct.c_void_p, ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint8),
        ct.c_size_t, ct.c_uint32, ct.POINTER(ContrastAccum),
    ]
    lib.bcir_stateful_probe_contrast.restype = ct.c_int

    _LIB = lib
    return lib


def make_policy(*, opt_kind: int = OPT_Q8_MUON_BF16_GRAD, fp64_guard_mode: int = FP64_GUARD_CONDITIONAL) -> PrecPolicy:
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
    ff: int = 928,
    batch: int = 4,
    time_len: int = 256,
    tied: int = 1,
    layer_mixers: Optional[Sequence[int]] = None,
    layer_use_rope: Optional[Sequence[int]] = None,
    swa_window: int = 16,
    delta_chunk: int = 16,
    mla_dc: int = 128,
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


def spec_from_dict(d: Dict[str, Any], *, batch: Optional[int] = None, time_len: Optional[int] = None, threads: int = 2) -> ExtSpec:
    return make_spec(
        vocab=int(d["vocab"]),
        width=int(d["width"]),
        heads=int(d["heads"]),
        kvheads=int(d["kvheads"]),
        layers=int(d["layers"]),
        ff=int(d["ff"]),
        batch=int(d["batch"] if batch is None else batch),
        time_len=int(d["time"] if time_len is None else time_len),
        tied=int(d["tied"]),
        layer_mixers=[int(x) for x in d["layer_mixer"]],
        layer_use_rope=[int(x) for x in d["layer_use_rope"]],
        swa_window=int(d.get("swa_window", 16)),
        delta_chunk=int(d.get("delta_chunk", 16)),
        mla_dc=int(d.get("mla_dc", 128)),
        mla_dr=int(d.get("mla_dr", 32)),
        init_policy=int(d.get("init_policy", INIT_MUP_V1)),
        mup_d_base=int(d.get("mup_d_base", 128)),
        seed=int(d.get("seed", 42)),
        threads=threads,
        gate_mode=int(d.get("gate_mode", GATE_G1_HEADWISE)),
        qk_norm=int(d.get("qk_norm", 1)),
        canon_mode=int(d.get("canon_mode", CANON_AC)),
        mhc_streams=int(d.get("mhc_streams", 2)),
        online_softmax=int(d.get("online_softmax", 1)),
        gate_rank=int(d.get("gate_rank", 32)),
    )


def make_ssp_config(**kwargs: Any) -> SspConfig:
    cfg = SspConfig()
    cfg.gdn_alpha_shift = float(kwargs.get("gdn_alpha_shift", 0.0))
    cfg.gdn_erase_shift = float(kwargs.get("gdn_erase_shift", 0.0))
    cfg.gdn_write_shift = float(kwargs.get("gdn_write_shift", 0.0))
    cfg.state_echo_damp = float(kwargs.get("state_echo_damp", 0.0))
    cfg.adaptive_echo_only = int(kwargs.get("adaptive_echo_only", 1))
    cfg.echo_ngram = int(kwargs.get("echo_ngram", 4))
    cfg.echo_window = int(kwargs.get("echo_window", 48))
    cfg.mla_repeat_logit_bias = float(kwargs.get("mla_repeat_logit_bias", 0.0))
    g1b = kwargs.get("g1_head_bias", None)
    if g1b is not None:
        cfg.enable_g1_bias = 1
        for i, v in enumerate(g1b[:BCIR_PROBE_MAX_HEADS]):
            cfg.g1_head_bias[i] = float(v)
    else:
        cfg.enable_g1_bias = 0
    cfg.rep_penalty = float(kwargs.get("rep_penalty", 1.0))
    cfg.presence_penalty = float(kwargs.get("presence_penalty", 0.0))
    cfg.ngram_block_penalty = float(kwargs.get("ngram_block_penalty", 0.0))
    cfg.rep_window = int(kwargs.get("rep_window", 32))
    cfg.max_pos_cap = int(kwargs.get("max_pos_cap", 0))
    return cfg


class Abi6Model:
    def __init__(self, spec: ExtSpec, policy: PrecPolicy):
        self.lib = load_lib()
        self.spec = spec
        self.policy = policy
        self.ptr = self.lib.bcir_ext_create(ct.byref(spec), ct.byref(policy))
        if not self.ptr:
            raise RuntimeError("bcir_ext_create failed")
        self.n_params = int(self.lib.bcir_ext_param_count(self.ptr))
        self._infos: Optional[List[Dict[str, Any]]] = None

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

    def reset_opt(self) -> None:
        self.lib.bcir_ext_reset_opt(self.ptr)

    def tensor_infos(self) -> List[Dict[str, Any]]:
        if self._infos is not None:
            return self._infos
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
        self._infos = out
        return out

    def tensor_map(self, w: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
        flat = self.weights_view() if w is None else w
        out = {}
        for ti in self.tensor_infos():
            off, r, c = ti["offset"], ti["rows"], ti["cols"]
            out[ti["name"]] = flat[off:off + r * c].reshape(r, c)
        return out

    def forward_logits(self, tokens: np.ndarray, *, active_time: Optional[int] = None) -> np.ndarray:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        B = int(self.spec.batch)
        act_t = int(self.spec.time if active_time is None else active_time)
        V = int(self.spec.vocab)
        out = np.empty((B, act_t, V), dtype=np.float32)
        rc = int(self.lib.bcir_ext_forward(
            self.ptr, tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            act_t, out.ctypes.data_as(ct.POINTER(ct.c_float)),
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_ext_forward failed rc={rc}")
        return out

    def evaluate(self, tokens: np.ndarray, targets: np.ndarray, *, active_time: Optional[int] = None) -> Dict[str, Any]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        nb = int(tk.shape[0])
        act_t = int(self.spec.time if active_time is None else active_time)
        res = EvalResult()
        rc = int(self.lib.bcir_ext_evaluate(
            self.ptr, tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tg.ctypes.data_as(ct.POINTER(ct.c_uint32)), nb, act_t, ct.byref(res),
        ))
        ce = float(res.ce)
        ok = rc == 0 and math.isfinite(ce)
        return {
            "ok": ok,
            "ce": ce if ok else None,
            "ppl": float(math.exp(min(ce, 50.0))) if ok else None,
            "bpb": float(ce / math.log(2.0)) if ok else None,
            "accuracy": float(res.accuracy),
            "tokens": int(res.tokens),
            "nonfinite": int(res.nonfinite),
            "eval_ms": float(res.eval_ms),
        }

    def train_loop(
        self, tokens: np.ndarray, targets: np.ndarray, opt_cfg: AdamWParams,
        *, lrs: Optional[np.ndarray] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        steps = int(tokens.shape[0])
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        lr_ptr = None
        if lrs is not None:
            lr_a = np.ascontiguousarray(lrs, dtype=np.float64)
            lr_ptr = lr_a.ctypes.data_as(ct.POINTER(ct.c_double))
        evs = (StepMetrics * steps)()
        rc = int(self.lib.bcir_ext_train_loop(
            self.ptr, tk.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            tg.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            None, lr_ptr, steps, ct.byref(opt_cfg), evs,
        ))
        return rc, [{k: getattr(evs[i], k) for k, _ in StepMetrics._fields_} for i in range(steps)]


class StatefulProbe:
    def __init__(self, model: Abi6Model, max_seq: int = 512):
        self.lib = load_lib()
        self.model = model
        self.max_seq = max_seq
        self.ptr = self.lib.bcir_stateful_create(model.ptr, ct.byref(model.spec), max_seq)
        if not self.ptr:
            raise RuntimeError("bcir_stateful_create failed")

    def close(self) -> None:
        if getattr(self, "ptr", None):
            self.lib.bcir_stateful_destroy(self.ptr)
            self.ptr = None

    def __enter__(self) -> "StatefulProbe":
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def sync(self) -> None:
        self.lib.bcir_stateful_sync_weights(self.ptr, self.model.ptr)

    def reset(self) -> None:
        self.lib.bcir_stateful_reset(self.ptr)

    def step(self, token_id: int, *, in_gen: int = 0, ssp: Optional[SspConfig] = None) -> np.ndarray:
        out = np.empty((int(self.model.spec.vocab),), dtype=np.float32)
        ssp_p = ct.byref(ssp) if ssp is not None else None
        rc = int(self.lib.bcir_stateful_step(
            self.ptr, int(token_id), int(in_gen), ssp_p,
            out.ctypes.data_as(ct.POINTER(ct.c_float)), None,
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_stateful_step failed rc={rc}")
        return out

    def generate_batch(
        self, prompts: np.ndarray, gen_len: int, *, ssp: Optional[SspConfig] = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        self.sync()
        pr = np.ascontiguousarray(prompts, dtype=np.uint32)
        n_p, p_len = pr.shape
        out_toks = np.empty((n_p, p_len + gen_len), dtype=np.uint32)
        out_ent = np.empty((n_p, gen_len), dtype=np.float32)
        out_echo = np.empty((n_p, gen_len), dtype=np.uint8)
        ssp_p = ct.byref(ssp) if ssp is not None else None
        rc = int(self.lib.bcir_stateful_generate_batch(
            self.ptr,
            pr.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            n_p, p_len, gen_len, ssp_p,
            out_toks.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            out_ent.ctypes.data_as(ct.POINTER(ct.c_float)),
            out_echo.ctypes.data_as(ct.POINTER(ct.c_uint8)),
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_stateful_generate_batch failed rc={rc}")
        return out_toks, out_ent, out_echo

    def probe_contrast(self, seqs: np.ndarray, mask_loop: np.ndarray) -> Dict[str, Any]:
        self.sync()
        sq = np.ascontiguousarray(seqs, dtype=np.uint32)
        mk = np.ascontiguousarray(mask_loop, dtype=np.uint8)
        n_s, s_len = sq.shape
        L = int(self.model.spec.layers)
        d = int(self.model.spec.width)
        ff = int(self.model.spec.ff)
        H = int(self.model.spec.heads)

        bufs = {
            "resid_loop": np.zeros((L, d), dtype=np.float64),
            "resid_clean": np.zeros((L, d), dtype=np.float64),
            "mlp_hid_loop": np.zeros((L, ff), dtype=np.float64),
            "mlp_hid_clean": np.zeros((L, ff), dtype=np.float64),
            "mlp_hid_abs_loop": np.zeros((L, ff), dtype=np.float64),
            "mlp_hid_abs_clean": np.zeros((L, ff), dtype=np.float64),
            "g1_loop": np.zeros((L, H), dtype=np.float64),
            "g1_clean": np.zeros((L, H), dtype=np.float64),
            "alpha_loop": np.zeros((L, d), dtype=np.float64),
            "alpha_clean": np.zeros((L, d), dtype=np.float64),
            "erase_loop": np.zeros((L, d), dtype=np.float64),
            "erase_clean": np.zeros((L, d), dtype=np.float64),
            "write_loop": np.zeros((L, d), dtype=np.float64),
            "write_clean": np.zeros((L, d), dtype=np.float64),
            "st_echo_loop": np.zeros((L, H), dtype=np.float64),
            "st_echo_clean": np.zeros((L, H), dtype=np.float64),
            "attn_rep_loop": np.zeros((L, H), dtype=np.float64),
            "attn_rep_clean": np.zeros((L, H), dtype=np.float64),
        }
        acc = ContrastAccum()
        acc.loop_tokens = 0
        acc.clean_tokens = 0
        for k, arr in bufs.items():
            setattr(acc, k, arr.ctypes.data_as(ct.POINTER(ct.c_double)))

        rc = int(self.lib.bcir_stateful_probe_contrast(
            self.ptr, sq.ctypes.data_as(ct.POINTER(ct.c_uint32)),
            mk.ctypes.data_as(ct.POINTER(ct.c_uint8)), n_s, s_len, ct.byref(acc),
        ))
        if rc != 0:
            raise RuntimeError(f"bcir_stateful_probe_contrast failed rc={rc}")
        nl = max(1, int(acc.loop_tokens))
        nc = max(1, int(acc.clean_tokens))
        out: Dict[str, Any] = {"loop_tokens": int(acc.loop_tokens), "clean_tokens": int(acc.clean_tokens)}
        for k, arr in bufs.items():
            denom = nl if k.endswith("_loop") else nc
            out[k] = arr / float(denom)
        return out


# ==================== BCIRQ8 & Tracked 10M Checkpoint Loader ====================
def decode_bcirq8_v1_per_tensor(path: Path, infos: List[Dict[str, Any]], n_params: int) -> np.ndarray:
    lib = load_lib()
    raw = path.read_bytes()
    out = np.zeros(n_params, dtype=np.float32)
    pos = 0
    for ti in infos:
        off, n = ti["offset"], ti["rows"] * ti["cols"]
        nb = (n + 31) // 32
        q = np.frombuffer(raw, dtype=np.int8, count=n, offset=pos)
        pos += n
        ex = np.frombuffer(raw, dtype=np.int8, count=nb, offset=pos)
        pos += nb
        dst = np.empty(n, dtype=np.float32)
        lib.bcir_quant_q8_g32_decode(
            q.ctypes.data_as(ct.POINTER(ct.c_int8)),
            ex.ctypes.data_as(ct.POINTER(ct.c_int8)),
            dst.ctypes.data_as(ct.POINTER(ct.c_float)),
            n,
        )
        out[off:off + n] = dst
    assert pos == len(raw), f"BCIRQ8 size mismatch: consumed {pos} vs {len(raw)}"
    return out


def load_arch7d_10m_tracked(*, batch: int = 4, time_len: int = 256, threads: int = 2) -> Tuple[Abi6Model, np.ndarray, Dict[str, Any]]:
    meta_path = ARCH7D_10M_CKPT_DIR / "arch7d_10m_stage3_best_bcirq8.json"
    bin_path = ARCH7D_10M_CKPT_DIR / "arch7d_10m_stage3_best_bcirq8.bin"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(bin_path.read_bytes()).hexdigest()
    assert actual_sha == meta["weights_sha256"], "Tracked 10M checkpoint SHA-256 mismatch!"
    sp = spec_from_dict(meta["spec"], batch=batch, time_len=time_len, threads=threads)
    pol = make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD)
    model = Abi6Model(sp, pol)
    w = decode_bcirq8_v1_per_tensor(bin_path, model.tensor_infos(), model.n_params)
    model.set_weights(w)
    return model, w, meta


# ==================== COMP-1 Dual-SVID + Joint-ITQ Factorizer ====================
def _dual_svid_joint_itq(w2d: np.ndarray, rank: int = 16, itq_iters: int = 5, fp16_scales: bool = True) -> np.ndarray:
    W = np.asarray(w2d, dtype=np.float64)
    m, n = W.shape
    r = min(rank, m, n)
    B = np.where(W >= 0.0, 1.0, -1.0)
    absW = np.abs(W)
    U, S, Vt = np.linalg.svd(absW, full_matrices=False)
    sq = np.sqrt(np.maximum(S[:r], 1e-12))
    Au = U[:, :r] * sq[None, :]
    Av = Vt[:r, :].T * sq[None, :]

    # Joint-ITQ orthogonal rotation
    R = np.eye(r, dtype=np.float64)
    for _ in range(itq_iters):
        Zu = Au @ R
        Zv = Av @ R
        Bu = np.where(Zu >= 0.0, 1.0, -1.0)
        Bv = np.where(Zv >= 0.0, 1.0, -1.0)
        C = Au.T @ Bu + Av.T @ Bv
        Uc, _, Vtc = np.linalg.svd(C, full_matrices=False)
        R = Uc @ Vtc
    Zu = Au @ R
    Zv = Av @ R
    Bu = np.where(Zu >= 0.0, 1.0, -1.0)
    Bv = np.where(Zv >= 0.0, 1.0, -1.0)
    # Optimal row/col positive scale fit
    su = np.maximum(np.sum(Zu * Bu, axis=1) / max(1, r), 1e-6)
    sv = np.maximum(np.sum(Zv * Bv, axis=1) / max(1, r), 1e-6)
    if fp16_scales:
        su = su.astype(np.float16).astype(np.float64)
        sv = sv.astype(np.float16).astype(np.float64)
    M_approx = np.maximum((su[:, None] * Bu) @ (sv[:, None] * Bv).T, 0.0)
    # Global least-squares scale alignment
    W_hat = B * M_approx
    denom = float(np.sum(W_hat * W_hat))
    if denom > 1e-12:
        g = float(np.sum(W * W_hat) / denom)
        if fp16_scales:
            g = float(np.float16(g))
        W_hat = W_hat * g
    return W_hat.astype(np.float32)


def build_comp1_weights(model: Abi6Model, w_base: np.ndarray, rank: int = 16, fp16_scales: bool = True) -> np.ndarray:
    """Apply BCIR COMP-1 (Dual-SVID + Joint-ITQ) strictly to MLP tensors (region==2, min(m,n)>=512);
    Attention/Embed/Norms stay in deterministic BCIRQ8 v1."""
    w_out = w_base.copy()
    for ti in model.tensor_infos():
        if ti["region"] == 2 and min(ti["rows"], ti["cols"]) >= 512:
            off, r, c = ti["offset"], ti["rows"], ti["cols"]
            mat = w_out[off:off + r * c].reshape(r, c)
            w_out[off:off + r * c] = _dual_svid_joint_itq(mat, rank=rank, fp16_scales=fp16_scales).ravel()
    return w_out


# ==================== WikiText-2 Data Splits (Strict Evolve vs Held-Out Isolation) ====================
_DATA_CACHE: Dict[str, np.ndarray] = {}


def load_wikitext_bytes(split: str) -> np.ndarray:
    if split in _DATA_CACHE:
        return _DATA_CACHE[split]
    import pyarrow.parquet as pq
    name_map = {
        "train": "wikitext2-train.parquet",
        "val": "wikitext2-validation.parquet",
        "validation": "wikitext2-validation.parquet",
        "test": "wikitext2-test.parquet",
    }
    p = WIKITEXT_DIR / name_map[split]
    tbl = pq.read_table(p, columns=["text"])
    raw = "".join(t for t in tbl["text"].to_pylist() if t).encode("utf-8")
    arr = np.frombuffer(raw, dtype=np.uint8).astype(np.uint32)
    _DATA_CACHE[split] = arr
    return arr


def make_eval_batches(stream: np.ndarray, batch: int, seq_len: int, n_batches: int, seed: int) -> Tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    max_start = len(stream) - seq_len - 1
    xs = np.empty((n_batches, batch, seq_len), dtype=np.uint32)
    ys = np.empty((n_batches, batch, seq_len), dtype=np.uint32)
    for bi in range(n_batches):
        starts = rng.integers(0, max_start, size=batch)
        for b, s in enumerate(starts):
            xs[bi, b] = stream[s:s + seq_len]
            ys[bi, b] = stream[s + 1:s + seq_len + 1]
    return xs, ys


def extract_domain_prompts(
    stream: np.ndarray, domain: str, n_prompts: int, prompt_len: int, seed: int
) -> np.ndarray:
    """Extract strictly deterministic domain-specific byte prompts from `stream`.
    - prose_unloop: narrative prose spans without headings
    - structured_heading: spans anchored at WikiText section headings (` = = `) or numeric/list blocks
    - longctx_echo: longer spans (`prompt_len=256`) containing repeated entity/phrase structure
    """
    raw_bytes = stream.astype(np.uint8).tobytes()
    rng = np.random.default_rng(seed)
    cands: List[int] = []

    if domain == "structured_heading":
        needle = b" = = "
        idx = 0
        while True:
            pos = raw_bytes.find(needle, idx)
            if pos < 0:
                break
            start = max(0, pos - 4)
            if start + prompt_len + 128 < len(raw_bytes):
                cands.append(start)
            idx = pos + 64
    elif domain == "longctx_echo":
        # Pick spans with above-median 4-gram self-overlap inside the prompt (natural long-context echo pressure)
        step = 192
        scored: List[Tuple[float, int]] = []
        for s in range(0, len(raw_bytes) - prompt_len - 128, step):
            chunk = raw_bytes[s:s + prompt_len]
            grams = [chunk[i:i + 4] for i in range(len(chunk) - 3)]
            rep = 1.0 - (len(set(grams)) / max(1, len(grams)))
            if rep >= 0.14:
                scored.append((rep, s))
        scored.sort(key=lambda x: (-x[0], x[1]))
        cands = [s for _, s in scored[:max(n_prompts * 4, 256)]]
    else:  # prose_unloop
        step = 256
        for s in range(64, len(raw_bytes) - prompt_len - 128, step):
            chunk = raw_bytes[s:s + prompt_len]
            if b" = = " not in chunk and chunk.count(b"\n") <= 2:
                # Start on a space boundary for clean prose
                sp_idx = chunk.find(b" ")
                if 0 <= sp_idx < 16:
                    cands.append(s + sp_idx + 1)

    if len(cands) < n_prompts:
        # deterministic fallback spacing
        stride = max(1, (len(raw_bytes) - prompt_len - 128) // max(1, n_prompts))
        cands = [i * stride for i in range(n_prompts)]

    chosen_idx = rng.choice(len(cands), size=n_prompts, replace=False)
    chosen_starts = sorted(cands[int(i)] for i in chosen_idx)
    out = np.empty((n_prompts, prompt_len), dtype=np.uint32)
    for i, s in enumerate(chosen_starts):
        out[i] = stream[s:s + prompt_len]
    return out


# ==================== Behavioral Repetition, Fluency & KL Metrics ====================
def compute_rollout_metrics(
    out_tokens: np.ndarray, prompt_len: int, entropies: Optional[np.ndarray] = None
) -> Dict[str, Any]:
    n_seq, total_len = out_tokens.shape
    gen_len = total_len - prompt_len
    rep4_list, rep8_list, dist4_list, dist8_list = [], [], [], []
    loop_flags = []
    max_cycle_runs = []

    for i in range(n_seq):
        gen = [int(x) for x in out_tokens[i, prompt_len:]]
        g4 = [tuple(gen[j:j + 4]) for j in range(max(0, gen_len - 3))]
        g8 = [tuple(gen[j:j + 8]) for j in range(max(0, gen_len - 7))]
        d4 = len(set(g4)) / max(1, len(g4))
        d8 = len(set(g8)) / max(1, len(g8))
        r4 = 1.0 - d4
        r8 = 1.0 - d8
        # Count max occurrences of any 8-gram inside generation
        counts8: Dict[Tuple[int, ...], int] = {}
        for g in g8:
            counts8[g] = counts8.get(g, 0) + 1
        mx8 = max(counts8.values()) if counts8 else 1
        # A sequence is in a hard repetition loop if an 8-gram repeats >= 2 times OR r4 >= 0.30
        is_loop = (mx8 >= 2) or (r4 >= 0.30)
        rep4_list.append(r4)
        rep8_list.append(r8)
        dist4_list.append(d4)
        dist8_list.append(d8)
        loop_flags.append(1 if is_loop else 0)
        max_cycle_runs.append(mx8)

    return {
        "n_sequences": int(n_seq),
        "gen_len": int(gen_len),
        "loop_seq_rate": float(np.mean(loop_flags)),
        "loop_seq_count": int(sum(loop_flags)),
        "rep4_rate": float(np.mean(rep4_list)),
        "rep8_rate": float(np.mean(rep8_list)),
        "distinct_4": float(np.mean(dist4_list)),
        "distinct_8": float(np.mean(dist8_list)),
        "max_8gram_repeats_mean": float(np.mean(max_cycle_runs)),
        "mean_entropy": float(np.mean(entropies)) if entropies is not None else 0.0,
        "loop_flags": loop_flags,
    }


def label_loop_tokens(out_tokens: np.ndarray, prompt_len: int) -> np.ndarray:
    """Create per-token loop mask for activation contrast probing:
      255 = ignore (prompt or ambiguous)
      1   = actively inside a repeating 4-gram/8-gram loop during generation
      0   = clean non-repeating generated token
    """
    n_seq, total_len = out_tokens.shape
    mask = np.full((n_seq, total_len), 255, dtype=np.uint8)
    for i in range(n_seq):
        seq = [int(x) for x in out_tokens[i]]
        seen4 = set()
        for t in range(prompt_len, total_len):
            if t >= 4:
                g4 = tuple(seq[t - 4:t])
                # Check if g4 occurred in the last 48 bytes
                win_start = max(0, t - 52)
                repeated = False
                for j in range(win_start, t - 4):
                    if tuple(seq[j:j + 4]) == g4:
                        repeated = True
                        break
                if repeated:
                    mask[i, t] = 1
                elif g4 not in seen4:
                    mask[i, t] = 0
                seen4.add(g4)
    return mask


def compute_kl_and_ce(
    model: Abi6Model, xs: np.ndarray, ys: np.ndarray, ref_logits: Optional[List[np.ndarray]] = None,
    *, active_time: Optional[int] = None,
) -> Tuple[Dict[str, Any], List[np.ndarray]]:
    nb = xs.shape[0]
    t_len = int(xs.shape[2] if active_time is None else active_time)
    losses = []
    logits_list = []
    kl_sum = 0.0
    agree = 0
    max_diff = 0.0
    correct = 0
    n_tok = 0

    for i in range(nb):
        raw = model.forward_logits(xs[i], active_time=t_len)
        z = raw.astype(np.float64)
        mx = z.max(axis=-1, keepdims=True)
        logp = z - mx - np.log(np.exp(z - mx).sum(axis=-1, keepdims=True))
        yy = ys[i, :, :t_len]
        losses.append(float(-np.take_along_axis(logp, yy[..., None], axis=-1).mean()))
        correct += int((z.argmax(axis=-1) == yy).sum())
        n_tok += int(yy.size)
        if ref_logits is not None:
            r = ref_logits[i].astype(np.float64)
            r_mx = r.max(axis=-1, keepdims=True)
            logr = r - r_mx - np.log(np.exp(r - r_mx).sum(axis=-1, keepdims=True))
            kl_sum += float((np.exp(logr) * (logr - logp)).sum())
            agree += int((r.argmax(axis=-1) == z.argmax(axis=-1)).sum())
            max_diff = max(max_diff, float(np.max(np.abs(raw - ref_logits[i]))))
        else:
            logits_list.append(raw.copy())

    ce = float(np.mean(losses))
    res: Dict[str, Any] = {
        "ce": ce,
        "ppl": float(math.exp(min(ce, 50.0))),
        "bpb": float(ce / math.log(2.0)),
        "accuracy": float(correct / max(1, n_tok)),
        "tokens": int(n_tok),
    }
    if ref_logits is not None:
        res.update({
            "kl_from_clean": float(kl_sum / max(1, n_tok)),
            "top1_agreement": float(agree / max(1, n_tok)),
            "max_abs_logit_delta": float(max_diff),
        })
    return res, logits_list


def composite_behavior_score(
    roll: Dict[str, Any], delta_ce: float, kl_from_clean: float
) -> float:
    """Principled composite score [0, 100]: rewards loop elimination + lexical diversity
    while strictly penalizing held-out CE degradation and KL distribution drift."""
    non_loop = 1.0 - float(roll["loop_seq_rate"])
    dist4 = float(roll["distinct_4"])
    non_rep8 = 1.0 - float(roll["rep8_rate"])
    raw_behav = 0.45 * non_loop + 0.35 * dist4 + 0.20 * non_rep8
    ce_pen = math.exp(-3.5 * max(0.0, float(delta_ce)))
    kl_pen = math.exp(-4.0 * max(0.0, float(kl_from_clean) - 0.015))
    return float(100.0 * raw_behav * ce_pen * kl_pen)


# ==================== WSP-1 & WSP-2 Contracts ====================
def audit_weight_health(model: Abi6Model, w: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """WSP-2 zero-inference structural weight-health sentinel."""
    t0 = time.monotonic()
    tmap = model.tensor_map(w)
    outliers = []
    dead_cols = []
    nonfinite = 0
    layer_stats = {}
    for name, mat in tmap.items():
        if not np.all(np.isfinite(mat)):
            nonfinite += int(np.sum(~np.isfinite(mat)))
        if name.endswith(".wdown"):
            cn = np.linalg.norm(mat.astype(np.float64), axis=0)
            med = float(np.median(cn))
            mx = float(np.max(cn))
            mn = float(np.min(cn))
            flagged = np.flatnonzero(cn > 8.0 * med).tolist()
            dead = np.flatnonzero(cn < 1e-4 * med).tolist()
            if flagged:
                outliers.append({"tensor": name, "cols": flagged, "max_over_med": mx / max(med, 1e-12)})
            if dead:
                dead_cols.append({"tensor": name, "cols": dead})
            layer_stats[name] = {
                "median_col_norm": med,
                "max_col_norm": mx,
                "min_col_norm": mn,
                "max_over_median": mx / max(med, 1e-12),
                "n_outliers_8x": len(flagged),
                "n_dead": len(dead),
            }
    return {
        "schema": "bcir.wsp2.health_sentinel.v1",
        "elapsed_ms": (time.monotonic() - t0) * 1000.0,
        "nonfinite_count": nonfinite,
        "outliers_8x": outliers,
        "dead_columns": dead_cols,
        "wdown_stats": layer_stats,
        "pass": (nonfinite == 0 and len(outliers) == 0),
    }


def make_wsp1_weight_patch(
    *,
    patch_id: str,
    base_sha256: str,
    edited_weights: np.ndarray,
    base_weights: np.ndarray,
    model: Abi6Model,
    patch_kind: str,
    locus: str,
    rationale: str,
    opt_state_policy: str,
    eval_summary: Dict[str, Any],
) -> Dict[str, Any]:
    delta = edited_weights - base_weights
    nz = int(np.count_nonzero(delta))
    l2 = float(np.linalg.norm(delta.astype(np.float64)))
    touched = []
    for ti in model.tensor_infos():
        off, n = ti["offset"], ti["rows"] * ti["cols"]
        sub = delta[off:off + n]
        snz = int(np.count_nonzero(sub))
        if snz > 0:
            touched.append({
                "tensor": ti["name"],
                "layer": ti["layer"],
                "shape": [ti["rows"], ti["cols"]],
                "changed_coords": snz,
                "delta_l2": float(np.linalg.norm(sub.astype(np.float64))),
            })
    return {
        "schema": "bcir.wsp1.weight_patch.v1",
        "patch_id": patch_id,
        "base_checkpoint_sha256": base_sha256,
        "edited_weights_sha256": hashlib.sha256(edited_weights.tobytes()).hexdigest(),
        "patch_kind": patch_kind,
        "locus": locus,
        "rationale": rationale,
        "opt_state_policy": opt_state_policy,
        "changed_params": nz,
        "total_params": int(base_weights.size),
        "changed_pct": 100.0 * nz / max(1, int(base_weights.size)),
        "edit_l2_norm": l2,
        "touched_tensors": touched,
        "eval_summary": eval_summary,
    }


# ==================== JSON / CSV / Provenance Helpers ====================
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    if not rows:
        return
    cols = fieldnames if fieldnames is not None else list(rows[0].keys())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    tmp.replace(path)


def get_host_provenance() -> Dict[str, Any]:
    bcir_root = STUDY_DIR.parent.parent.parent / "Binary-Correspondence-Intermediate-Representation"
    try:
        bcir_sha = subprocess.check_output(["git", "-C", str(bcir_root), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        bcir_sha = "unknown"
    try:
        cpu_model = subprocess.check_output(
            ["bash", "-c", "lscpu | awk -F: '/Model name/{print $2; exit}'"], text=True
        ).strip()
    except Exception:
        cpu_model = platform.processor()
    return {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "bcir_commit": bcir_sha,
    }
