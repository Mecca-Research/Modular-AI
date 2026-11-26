#!/usr/bin/env python3
"""Portable Python ctypes harness for BCIR Precision Policy (P1-P6),
TRAIN-OPT-1/2/4/6/7, MIXER-EXT (GQA, SWA, Arch-1 DeltaNet, Arch-2 Hybrid-SWA,
Arch-4 MLA), and Whole-Model COMP-1 Deployment.
"""
from __future__ import annotations

import ctypes
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

STUDY_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = STUDY_DIR / "src"
BUILD_DIR = STUDY_DIR / "build"
RESULTS_DIR = STUDY_DIR / "results"
CKPT_DIR = STUDY_DIR / "checkpoints"
FIG_DIR = STUDY_DIR / "figures"
LIB_PATH = BUILD_DIR / "libbcir_lab.so"
WIKITEXT_DIR = STUDY_DIR.parent / "gp-global" / "data"

BCIR_EXT_MAX_LAYERS = 64
BCIR_EXT_IGNORE = 0xFFFFFFFF

# GEMM precision contracts (P1)
BCIR_GEMM_FP32_KBLOCK = 0
BCIR_GEMM_BF16_NDT = 1
BCIR_GEMM_BF16_VDP = 2
BCIR_GEMM_FP8_E4M3_BLK32 = 3
BCIR_GEMM_FP8_E5M2_BLK32 = 4
BCIR_GEMM_FP4_MX_BLK32 = 5
BCIR_GEMM_Q8_BLK32 = 6

# Optimizer state compression contracts (TRAIN-OPT-7)
BCIR_OPT_FP32 = 0
BCIR_OPT_Q8_MOMENTS = 1
BCIR_OPT_Q8_MOMENTS_BF16_GRAD = 2
BCIR_OPT_ADAFACTOR_M2 = 3
BCIR_OPT_ADAFACTOR_BF16_GRAD = 4

# Initialization policies (TRAIN-OPT-1)
BCIR_INIT_LEGACY_002 = 0
BCIR_INIT_RESIDUAL_SCALED = 1
BCIR_INIT_MUP_V1 = 2

# FP64 guard modes (P3)
BCIR_FP64_GUARD_OFF = 0
BCIR_FP64_GUARD_CONDITIONAL = 1
BCIR_FP64_GUARD_ALWAYS = 2

# Sequence mixers (MIXER-EXT)
BCIR_MIXER_GQA = 0
BCIR_MIXER_SWA = 1
BCIR_MIXER_DELTANET = 2
BCIR_MIXER_MLA = 4

# Deployment weight formats (COMP-1)
BCIR_DEPLOY_FP32 = 0
BCIR_DEPLOY_FP16 = 1
BCIR_DEPLOY_BCIRQ8_G32 = 2
BCIR_DEPLOY_LB_PACKED1B = 3

# LittleBit microbenchmark kernel IDs
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


class BcirNumContractDesc(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("operand_fmt", ctypes.c_char_p),
        ("exponent_policy", ctypes.c_char_p),
        ("rounding_mode", ctypes.c_char_p),
        ("accumulator_type", ctypes.c_char_p),
        ("reduction_order", ctypes.c_char_p),
        ("subnormal_handling", ctypes.c_char_p),
        ("overflow_behavior", ctypes.c_char_p),
        ("max_rel_err_bound", ctypes.c_double),
        ("block_size", ctypes.c_int),
        ("bits_per_elem", ctypes.c_double),
    ]


class BcirPrecPolicy(ctypes.Structure):
    _fields_ = [
        ("attn_fwd_gemm", ctypes.c_int),
        ("attn_bwd_gemm", ctypes.c_int),
        ("mlp_fwd_gemm", ctypes.c_int),
        ("mlp_bwd_gemm", ctypes.c_int),
        ("opt_kind", ctypes.c_int),
        ("fp64_guard_mode", ctypes.c_int),
        ("rmsnorm_cond_thresh", ctypes.c_double),
        ("softmax_logit_thresh", ctypes.c_double),
        ("wy_cond_thresh", ctypes.c_double),
        ("use_vdp_portable", ctypes.c_int),
    ]


class BcirPrecTelemetry(ctypes.Structure):
    _fields_ = [
        ("fp64_norm_promotions", ctypes.c_uint64),
        ("fp64_softmax_promotions", ctypes.c_uint64),
        ("fp64_wy_solves", ctypes.c_uint64),
        ("nonfinite_events", ctypes.c_uint64),
        ("grad_clip_hits", ctypes.c_uint64),
        ("total_steps", ctypes.c_uint64),
        ("q8_m2_floor_clamps", ctypes.c_uint64),
        ("gemm_calls_attn", ctypes.c_uint64),
        ("gemm_calls_mlp", ctypes.c_uint64),
        ("max_grad_norm", ctypes.c_double),
        ("max_softmax_logit", ctypes.c_double),
        ("min_rms_observed", ctypes.c_double),
        ("max_wy_offdiag", ctypes.c_double),
    ]


class BcirAdamwParams(ctypes.Structure):
    _fields_ = [
        ("lr", ctypes.c_double),
        ("beta1", ctypes.c_double),
        ("beta2", ctypes.c_double),
        ("epsilon", ctypes.c_double),
        ("weight_decay", ctypes.c_double),
        ("grad_clip", ctypes.c_double),
    ]


class BcirExtSpec(ctypes.Structure):
    _fields_ = [
        ("vocab", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("heads", ctypes.c_uint32),
        ("kvheads", ctypes.c_uint32),
        ("layers", ctypes.c_uint32),
        ("ff", ctypes.c_uint32),
        ("batch", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("tied", ctypes.c_uint32),
        ("rope_base", ctypes.c_double),
        ("rms_eps", ctypes.c_double),
        ("layer_mixer", ctypes.c_uint32 * BCIR_EXT_MAX_LAYERS),
        ("layer_use_rope", ctypes.c_uint32 * BCIR_EXT_MAX_LAYERS),
        ("swa_window", ctypes.c_uint32),
        ("delta_chunk", ctypes.c_uint32),
        ("mla_dc", ctypes.c_uint32),
        ("mla_dr", ctypes.c_uint32),
        ("init_policy", ctypes.c_int),
        ("mup_d_base", ctypes.c_uint32),
        ("seed", ctypes.c_uint64),
        ("threads", ctypes.c_uint32),
        ("skip_init", ctypes.c_int),
    ]


class BcirExtMemStats(ctypes.Structure):
    _fields_ = [
        ("n_params", ctypes.c_size_t),
        ("attn_params", ctypes.c_size_t),
        ("mlp_params", ctypes.c_size_t),
        ("nonlin_params", ctypes.c_size_t),
        ("weight_bytes", ctypes.c_size_t),
        ("opt_only_bytes", ctypes.c_size_t),
        ("persistent_bytes", ctypes.c_size_t),
        ("activation_bytes", ctypes.c_size_t),
        ("mixer_prob_bytes", ctypes.c_size_t),
        ("scratch_bytes", ctypes.c_size_t),
        ("total_bytes", ctypes.c_size_t),
        ("infer_kv_bytes_per_tok", ctypes.c_size_t),
        ("infer_rec_state_bytes", ctypes.c_size_t),
        ("init_ms", ctypes.c_double),
    ]


class BcirExtStepMetrics(ctypes.Structure):
    _fields_ = [
        ("loss", ctypes.c_double),
        ("grad_norm", ctypes.c_double),
        ("step_ms", ctypes.c_double),
        ("clipped", ctypes.c_int),
        ("status", ctypes.c_int),
        ("step", ctypes.c_uint64),
        ("supervised_tokens", ctypes.c_uint64),
        ("fp64_promotions", ctypes.c_uint64),
        ("nonfinite_events", ctypes.c_uint64),
    ]


class BcirExtEvalResult(ctypes.Structure):
    _fields_ = [
        ("ce", ctypes.c_double),
        ("accuracy", ctypes.c_double),
        ("tokens", ctypes.c_uint64),
        ("nonfinite", ctypes.c_uint64),
        ("eval_ms", ctypes.c_double),
    ]


class BcirExtTensorInfo(ctypes.Structure):
    _fields_ = [
        ("offset", ctypes.c_size_t),
        ("rows", ctypes.c_size_t),
        ("cols", ctypes.c_size_t),
        ("layer", ctypes.c_int),
        ("region", ctypes.c_int),
        ("name", ctypes.c_char * 48),
    ]


class BcirDeployStats(ctypes.Structure):
    _fields_ = [
        ("total_artifact_bytes", ctypes.c_size_t),
        ("attn_bytes", ctypes.c_size_t),
        ("mlp_bytes", ctypes.c_size_t),
        ("nonlin_bytes", ctypes.c_size_t),
        ("body_params", ctypes.c_size_t),
        ("total_params", ctypes.c_size_t),
        ("realized_body_bpw", ctypes.c_double),
        ("realized_total_bpw", ctypes.c_double),
        ("kv_cache_bytes_at_64", ctypes.c_size_t),
        ("kv_cache_bytes_at_1024", ctypes.c_size_t),
        ("kv_cache_bytes_at_4096", ctypes.c_size_t),
        ("rec_state_bytes", ctypes.c_size_t),
        ("max_weight_rel_frob_attn", ctypes.c_double),
        ("max_weight_rel_frob_mlp", ctypes.c_double),
        ("max_q8_bound_violation", ctypes.c_double),
        ("projected_fp16_nonlin_bytes", ctypes.c_size_t),
        ("projected_fp16_artifact_bytes", ctypes.c_size_t),
        ("projected_fp16_total_bpw", ctypes.c_double),
        ("projected_fp16_kv_bytes_at_64", ctypes.c_size_t),
        ("projected_fp16_kv_bytes_at_1024", ctypes.c_size_t),
        ("projected_fp16_kv_bytes_at_4096", ctypes.c_size_t),
        ("projected_fp32_rec_state_bytes", ctypes.c_size_t),
    ]


class BcirDeployBench(ctypes.Structure):
    _fields_ = [
        ("decode_median_us", ctypes.c_double),
        ("decode_tok_per_s", ctypes.c_double),
        ("decode_attn_frac", ctypes.c_double),
        ("decode_mlp_frac", ctypes.c_double),
        ("prefill_n16_ms", ctypes.c_double),
        ("prefill_n64_ms", ctypes.c_double),
        ("prefill_n128_ms", ctypes.c_double),
        ("max_portable_avx_diff", ctypes.c_double),
    ]


class LbBenchResult(ctypes.Structure):
    _fields_ = [
        ("median_us", ctypes.c_double),
        ("min_us", ctypes.c_double),
        ("gflops_equiv", ctypes.c_double),
        ("max_abs_err", ctypes.c_double),
        ("weight_bytes", ctypes.c_size_t),
        ("realized_bpw", ctypes.c_double),
        ("ideal_deploy_weight_bytes", ctypes.c_size_t),
        ("ideal_deploy_bpw", ctypes.c_double),
    ]


_LIB: Optional[ctypes.CDLL] = None


def load_lib() -> ctypes.CDLL:
    global _LIB
    if _LIB is not None:
        return _LIB
    if not LIB_PATH.exists():
        raise FileNotFoundError(f"Missing {LIB_PATH}; run `make` in {STUDY_DIR}")
    lib = ctypes.CDLL(str(LIB_PATH))

    lib.bcir_prec_gemm_desc.argtypes = [ctypes.c_int]
    lib.bcir_prec_gemm_desc.restype = ctypes.POINTER(BcirNumContractDesc)
    lib.bcir_prec_opt_name.argtypes = [ctypes.c_int]
    lib.bcir_prec_opt_name.restype = ctypes.c_char_p
    lib.bcir_prec_policy_default.argtypes = [ctypes.POINTER(BcirPrecPolicy)]
    lib.bcir_mm_bf16_vdp_variants.argtypes = []
    lib.bcir_mm_bf16_vdp_variants.restype = ctypes.c_int
    lib.bcir_prec_mm_scratch_bytes.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
    lib.bcir_prec_mm_scratch_bytes.restype = ctypes.c_size_t
    lib.bcir_prec_mm_pool.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_float,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_size_t,
    ]
    lib.bcir_prec_mm_pool.restype = ctypes.c_int
    lib.bcir_tensor_pool_size.argtypes = [ctypes.c_uint]
    lib.bcir_tensor_pool_size.restype = ctypes.c_size_t
    lib.bcir_tensor_pool_init.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
    lib.bcir_tensor_pool_init.restype = ctypes.c_void_p
    lib.bcir_tensor_pool_stop.argtypes = [ctypes.c_void_p]

    lib.bcir_ext_create.argtypes = [ctypes.POINTER(BcirExtSpec), ctypes.POINTER(BcirPrecPolicy)]
    lib.bcir_ext_create.restype = ctypes.c_void_p
    lib.bcir_ext_destroy.argtypes = [ctypes.c_void_p]
    lib.bcir_ext_get_mem_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(BcirExtMemStats)]
    lib.bcir_ext_get_telemetry.argtypes = [ctypes.c_void_p, ctypes.POINTER(BcirPrecTelemetry)]
    lib.bcir_ext_reset_opt.argtypes = [ctypes.c_void_p]
    lib.bcir_ext_weights_ptr.argtypes = [ctypes.c_void_p]
    lib.bcir_ext_weights_ptr.restype = ctypes.POINTER(ctypes.c_float)
    lib.bcir_ext_param_count.argtypes = [ctypes.c_void_p]
    lib.bcir_ext_param_count.restype = ctypes.c_size_t
    lib.bcir_ext_tensor_count.argtypes = [ctypes.c_void_p]
    lib.bcir_ext_tensor_count.restype = ctypes.c_size_t
    lib.bcir_ext_tensor_info_at.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(BcirExtTensorInfo)]
    lib.bcir_ext_tensor_info_at.restype = ctypes.c_int
    lib.bcir_ext_forward.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    lib.bcir_ext_forward.restype = ctypes.c_int
    lib.bcir_ext_evaluate.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.POINTER(BcirExtEvalResult),
    ]
    lib.bcir_ext_evaluate.restype = ctypes.c_int
    lib.bcir_ext_train_loop.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(BcirAdamwParams), ctypes.POINTER(BcirExtStepMetrics),
    ]
    lib.bcir_ext_train_loop.restype = ctypes.c_int

    lib.bcir_deploy_create.argtypes = [ctypes.POINTER(BcirExtSpec), ctypes.c_uint32]
    lib.bcir_deploy_create.restype = ctypes.c_void_p
    lib.bcir_deploy_destroy.argtypes = [ctypes.c_void_p]
    lib.bcir_deploy_load_from_ext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.bcir_deploy_load_from_ext.restype = ctypes.c_int
    lib.bcir_deploy_set_linear_lb.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.bcir_deploy_set_linear_lb.restype = ctypes.c_int
    lib.bcir_deploy_set_linear_dense.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_void_p]
    lib.bcir_deploy_set_linear_dense.restype = ctypes.c_int
    lib.bcir_deploy_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(BcirDeployStats)]
    lib.bcir_deploy_reset_cache.argtypes = [ctypes.c_void_p]
    lib.bcir_deploy_decode_step.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p]
    lib.bcir_deploy_decode_step.restype = ctypes.c_int
    lib.bcir_deploy_prefill_seq.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p]
    lib.bcir_deploy_prefill_seq.restype = ctypes.c_int
    lib.bcir_deploy_evaluate.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_int, ctypes.POINTER(BcirExtEvalResult),
    ]
    lib.bcir_deploy_evaluate.restype = ctypes.c_int
    lib.bcir_deploy_benchmark.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(BcirDeployBench),
    ]
    lib.bcir_deploy_benchmark.restype = ctypes.c_int

    lib.lb_bench_linear_kernel.argtypes = [
        ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(LbBenchResult),
    ]
    lib.lb_bench_linear_kernel.restype = ctypes.c_int

    _LIB = lib
    return lib


def make_policy(
    attn_fwd: int = BCIR_GEMM_FP32_KBLOCK,
    attn_bwd: int = BCIR_GEMM_FP32_KBLOCK,
    mlp_fwd: int = BCIR_GEMM_FP32_KBLOCK,
    mlp_bwd: int = BCIR_GEMM_FP32_KBLOCK,
    opt_kind: int = BCIR_OPT_FP32,
    fp64_guard: int = BCIR_FP64_GUARD_CONDITIONAL,
    rmsnorm_cond_thresh: float = 1e-3,
    softmax_logit_thresh: float = 20.0,
    wy_cond_thresh: float = 0.99,
    use_vdp_portable: int = 0,
) -> BcirPrecPolicy:
    pol = BcirPrecPolicy()
    load_lib().bcir_prec_policy_default(ctypes.byref(pol))
    pol.attn_fwd_gemm = attn_fwd
    pol.attn_bwd_gemm = attn_bwd
    pol.mlp_fwd_gemm = mlp_fwd
    pol.mlp_bwd_gemm = mlp_bwd
    pol.opt_kind = opt_kind
    pol.fp64_guard_mode = fp64_guard
    pol.rmsnorm_cond_thresh = rmsnorm_cond_thresh
    pol.softmax_logit_thresh = softmax_logit_thresh
    pol.wy_cond_thresh = wy_cond_thresh
    pol.use_vdp_portable = use_vdp_portable
    return pol


def make_spec(
    *,
    vocab: int = 256,
    width: int = 192,
    heads: int = 4,
    kvheads: int = 2,
    layers: int = 2,
    ff: int = 512,
    batch: int = 4,
    time_len: int = 64,
    tied: int = 1,
    rope_base: float = 10000.0,
    rms_eps: float = 1e-6,
    layer_mixers: Optional[Sequence[int]] = None,
    layer_use_rope: Optional[Sequence[int]] = None,
    swa_window: int = 32,
    delta_chunk: int = 16,
    mla_dc: int = 0,
    mla_dr: int = 0,
    init_policy: int = BCIR_INIT_MUP_V1,
    mup_d_base: int = 192,
    seed: int = 42,
    threads: int = 2,
    skip_init: int = 0,
) -> BcirExtSpec:
    sp = BcirExtSpec()
    sp.vocab = vocab
    sp.width = width
    sp.heads = heads
    sp.kvheads = kvheads
    sp.layers = layers
    sp.ff = ff
    sp.batch = batch
    sp.time = time_len
    sp.tied = tied
    sp.rope_base = rope_base
    sp.rms_eps = rms_eps
    sp.swa_window = swa_window
    sp.delta_chunk = delta_chunk
    sp.mla_dc = mla_dc if mla_dc > 0 else width // 2
    sp.mla_dr = mla_dr if mla_dr > 0 else max(2, (width // heads) // 2)
    sp.init_policy = init_policy
    sp.mup_d_base = mup_d_base
    sp.seed = seed
    sp.threads = threads
    sp.skip_init = skip_init
    for l in range(layers):
        mx = int(layer_mixers[l]) if layer_mixers is not None else BCIR_MIXER_GQA
        sp.layer_mixer[l] = mx
        if layer_use_rope is not None:
            sp.layer_use_rope[l] = int(layer_use_rope[l])
        else:
            sp.layer_use_rope[l] = 0 if mx == BCIR_MIXER_DELTANET else 1
    return sp


def get_contract_table() -> Dict[int, Dict[str, Any]]:
    lib = load_lib()
    out: Dict[int, Dict[str, Any]] = {}
    for k in range(7):
        d = lib.bcir_prec_gemm_desc(k).contents
        out[k] = {
            "kind": int(d.kind),
            "name": d.name.decode(),
            "operand_fmt": d.operand_fmt.decode(),
            "exponent_policy": d.exponent_policy.decode(),
            "rounding_mode": d.rounding_mode.decode(),
            "accumulator_type": d.accumulator_type.decode(),
            "reduction_order": d.reduction_order.decode(),
            "subnormal_handling": d.subnormal_handling.decode(),
            "overflow_behavior": d.overflow_behavior.decode(),
            "max_rel_err_bound": float(d.max_rel_err_bound),
            "block_size": int(d.block_size),
            "bits_per_elem": float(d.bits_per_elem),
        }
    return out


def get_host_provenance() -> Dict[str, Any]:
    cpu = platform.processor()
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    clang_ver = subprocess.check_output(["clang", "--version"], text=True).splitlines()[0]
    bcir_sha = subprocess.check_output(
        ["git", "-C", str(STUDY_DIR.parents[2] / "Binary-Correspondence-Intermediate-Representation"), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    return {
        "cpu_model": cpu,
        "cores": os.cpu_count(),
        "clang": clang_ver,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "bcir_head_sha": bcir_sha,
        "vdp_bf16_variants": int(load_lib().bcir_mm_bf16_vdp_variants()),
    }


def struct_to_dict(s: ctypes.Structure) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, _ in s._fields_:
        val = getattr(s, name)
        if isinstance(val, bytes):
            out[name] = val.decode("utf-8", errors="replace").rstrip("\x00")
        elif isinstance(val, (int, float)):
            out[name] = val
    return out


class ExtModelHandle:
    def __init__(self, spec: BcirExtSpec, policy: BcirPrecPolicy):
        self.lib = load_lib()
        self.spec = spec
        self.policy = policy
        self.ptr = self.lib.bcir_ext_create(ctypes.byref(spec), ctypes.byref(policy))
        if not self.ptr:
            raise RuntimeError("bcir_ext_create failed")
        self.n_params = int(self.lib.bcir_ext_param_count(self.ptr))
        self.n_tensors = int(self.lib.bcir_ext_tensor_count(self.ptr))

    def close(self) -> None:
        if getattr(self, "ptr", None):
            self.lib.bcir_ext_destroy(self.ptr)
            self.ptr = None

    def __enter__(self) -> "ExtModelHandle":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def weights_view(self) -> np.ndarray:
        p = self.lib.bcir_ext_weights_ptr(self.ptr)
        return np.ctypeslib.as_array(p, shape=(self.n_params,))

    def copy_weights(self) -> np.ndarray:
        return self.weights_view().copy()

    def load_weights(self, w: np.ndarray) -> None:
        arr = np.ascontiguousarray(w, dtype=np.float32).ravel()
        if arr.size != self.n_params:
            raise ValueError(f"Weight size {arr.size} != {self.n_params}")
        self.weights_view()[:] = arr

    def mem_stats(self) -> Dict[str, Any]:
        ms = BcirExtMemStats()
        self.lib.bcir_ext_get_mem_stats(self.ptr, ctypes.byref(ms))
        return struct_to_dict(ms)

    def telemetry(self) -> Dict[str, Any]:
        t = BcirPrecTelemetry()
        self.lib.bcir_ext_get_telemetry(self.ptr, ctypes.byref(t))
        return struct_to_dict(t)

    def tensors(self) -> List[Dict[str, Any]]:
        res = []
        for i in range(self.n_tensors):
            ti = BcirExtTensorInfo()
            self.lib.bcir_ext_tensor_info_at(self.ptr, i, ctypes.byref(ti))
            res.append({
                "idx": i,
                "name": ti.name.decode(),
                "offset": int(ti.offset),
                "rows": int(ti.rows),
                "cols": int(ti.cols),
                "layer": int(ti.layer),
                "region": int(ti.region),
            })
        return res

    def forward(self, tokens: np.ndarray, active_time: Optional[int] = None) -> np.ndarray:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        at = int(active_time if active_time is not None else self.spec.time)
        out = np.zeros((self.spec.batch, at, self.spec.vocab), dtype=np.float32)
        rc = self.lib.bcir_ext_forward(self.ptr, tk.ctypes.data, at, out.ctypes.data)
        if rc != 0:
            raise RuntimeError(f"bcir_ext_forward rc={rc}")
        return out

    def evaluate(self, tokens: np.ndarray, targets: np.ndarray, active_time: Optional[int] = None) -> Dict[str, Any]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        at = int(active_time if active_time is not None else self.spec.time)
        n_batches = tk.size // (self.spec.batch * at)
        res = BcirExtEvalResult()
        rc = self.lib.bcir_ext_evaluate(self.ptr, tk.ctypes.data, tg.ctypes.data, n_batches, at, ctypes.byref(res))
        d = struct_to_dict(res)
        d["rc"] = int(rc)
        d["ok"] = bool(rc == 0 and d["nonfinite"] == 0 and math.isfinite(d["ce"]))
        d["ppl"] = math.exp(min(d["ce"], 50.0)) if d["ok"] else float("nan")
        return d

    def train_loop(
        self,
        tokens: np.ndarray,
        targets: np.ndarray,
        opt_cfg: BcirAdamwParams,
        lrs: Optional[np.ndarray] = None,
        active_times: Optional[np.ndarray] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        steps = tk.shape[0]
        evs = (BcirExtStepMetrics * steps)()
        lr_ptr = None
        if lrs is not None:
            lrs_c = np.ascontiguousarray(lrs, dtype=np.float64)
            lr_ptr = lrs_c.ctypes.data
        at_ptr = None
        if active_times is not None:
            at_c = np.ascontiguousarray(active_times, dtype=np.uint32)
            at_ptr = at_c.ctypes.data
        rc = self.lib.bcir_ext_train_loop(
            self.ptr, tk.ctypes.data, tg.ctypes.data, at_ptr, lr_ptr,
            steps, ctypes.byref(opt_cfg), evs,
        )
        out = [struct_to_dict(evs[i]) for i in range(steps)]
        return int(rc), out


class DeployModelHandle:
    def __init__(self, spec: BcirExtSpec, max_kv_len: int = 256):
        self.lib = load_lib()
        self.spec = spec
        self.max_kv_len = max_kv_len
        self.ptr = self.lib.bcir_deploy_create(ctypes.byref(spec), max_kv_len)
        if not self.ptr:
            raise RuntimeError("bcir_deploy_create failed")

    def close(self) -> None:
        if getattr(self, "ptr", None):
            self.lib.bcir_deploy_destroy(self.ptr)
            self.ptr = None

    def __enter__(self) -> "DeployModelHandle":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def load_from_ext(self, ext: ExtModelHandle, attn_fmt: int, mlp_fmt: int) -> None:
        rc = self.lib.bcir_deploy_load_from_ext(self.ptr, ext.ptr, attn_fmt, mlp_fmt)
        if rc != 0:
            raise RuntimeError(f"bcir_deploy_load_from_ext rc={rc}")

    def set_linear_dense(self, tensor_idx: int, fmt: int, W: np.ndarray) -> None:
        Wc = np.ascontiguousarray(W, dtype=np.float32)
        rc = self.lib.bcir_deploy_set_linear_dense(self.ptr, tensor_idx, fmt, Wc.ctypes.data)
        if rc != 0:
            raise RuntimeError(f"bcir_deploy_set_linear_dense rc={rc}")

    def set_linear_lb(
        self,
        tensor_idx: int,
        p1: Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        p2: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None,
        W_ref: Optional[np.ndarray] = None,
    ) -> None:
        U1, V1, h1, g1, l1 = [np.ascontiguousarray(a, dtype=np.float32) for a in p1]
        r1 = int(U1.shape[1])
        if p2 is not None:
            U2, V2, h2, g2, l2 = [np.ascontiguousarray(a, dtype=np.float32) for a in p2]
            r2 = int(U2.shape[1])
            p2_ptrs = (U2.ctypes.data, V2.ctypes.data, h2.ctypes.data, g2.ctypes.data, l2.ctypes.data)
        else:
            r2 = 0
            p2_ptrs = (None, None, None, None, None)
        ref_ptr = None
        if W_ref is not None:
            Wrc = np.ascontiguousarray(W_ref, dtype=np.float32)
            ref_ptr = Wrc.ctypes.data
        rc = self.lib.bcir_deploy_set_linear_lb(
            self.ptr, tensor_idx,
            r1, U1.ctypes.data, V1.ctypes.data, h1.ctypes.data, g1.ctypes.data, l1.ctypes.data,
            r2, *p2_ptrs, ref_ptr,
        )
        if rc != 0:
            raise RuntimeError(f"bcir_deploy_set_linear_lb rc={rc}")

    def stats(self) -> Dict[str, Any]:
        st = BcirDeployStats()
        self.lib.bcir_deploy_get_stats(self.ptr, ctypes.byref(st))
        return struct_to_dict(st)

    def prefill_seq(self, tokens: np.ndarray, use_portable: int = 0) -> np.ndarray:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32).ravel()
        out = np.zeros((tk.size, self.spec.vocab), dtype=np.float32)
        rc = self.lib.bcir_deploy_prefill_seq(self.ptr, tk.ctypes.data, tk.size, use_portable, out.ctypes.data)
        if rc != 0:
            raise RuntimeError(f"bcir_deploy_prefill_seq rc={rc}")
        return out

    def evaluate(
        self, tokens: np.ndarray, targets: np.ndarray, batch_size: int, stride_T: int, active_T: int, use_portable: int = 0
    ) -> Dict[str, Any]:
        tk = np.ascontiguousarray(tokens, dtype=np.uint32)
        tg = np.ascontiguousarray(targets, dtype=np.uint32)
        n_batches = tk.size // (batch_size * stride_T)
        res = BcirExtEvalResult()
        rc = self.lib.bcir_deploy_evaluate(
            self.ptr, tk.ctypes.data, tg.ctypes.data,
            n_batches, batch_size, stride_T, active_T, use_portable, ctypes.byref(res),
        )
        d = struct_to_dict(res)
        d["rc"] = int(rc)
        d["ok"] = bool(rc == 0 and d["nonfinite"] == 0 and math.isfinite(d["ce"]))
        d["ppl"] = math.exp(min(d["ce"], 50.0)) if d["ok"] else float("nan")
        return d

    def benchmark(self, ctx_len: int = 64, decode_iters: int = 31, prefill_iters: int = 5) -> Dict[str, Any]:
        b = BcirDeployBench()
        rc = self.lib.bcir_deploy_benchmark(self.ptr, ctx_len, decode_iters, prefill_iters, ctypes.byref(b))
        if rc != 0:
            raise RuntimeError(f"bcir_deploy_benchmark rc={rc}")
        return struct_to_dict(b)


# ==================== WikiText-2 Raw-Byte Loader ====================
_WIKI_CACHE: Dict[str, np.ndarray] = {}


def load_wikitext2_bytes(split: str) -> np.ndarray:
    """Load strictly separated WikiText-2 raw UTF-8 byte stream (train/validation/test)."""
    if split in _WIKI_CACHE:
        return _WIKI_CACHE[split]
    import pyarrow.parquet as pq

    candidates = {
        "train": [WIKITEXT_DIR / "train-00000-of-00001.parquet", WIKITEXT_DIR / "train.parquet"],
        "validation": [WIKITEXT_DIR / "validation-00000-of-00001.parquet", WIKITEXT_DIR / "validation.parquet"],
        "test": [WIKITEXT_DIR / "test-00000-of-00001.parquet", WIKITEXT_DIR / "test.parquet"],
    }[split]
    path = None
    for c in candidates:
        if c.exists():
            path = c
            break
    if path is None:
        # Glob fallback inside WIKITEXT_DIR
        matches = sorted(WIKITEXT_DIR.glob(f"*{split}*.parquet"))
        if matches:
            path = matches[0]
    if path is None:
        raise FileNotFoundError(f"WikiText-2 {split} parquet not found in {WIKITEXT_DIR}")
    tbl = pq.read_table(path)
    col = tbl.column(0).to_pylist()
    raw = "\n".join(s for s in col if s).encode("utf-8")
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
    return np.ascontiguousarray(seqs[:, :, :-1], dtype=np.uint32), np.ascontiguousarray(seqs[:, :, 1:], dtype=np.uint32)


def make_fixed_eval_batches(
    stream: np.ndarray, n_batches: int, batch: int, seq_len: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Deterministic evenly-spaced non-overlapping evaluation windows across split."""
    total_seqs = n_batches * batch
    stride = max(seq_len + 1, (len(stream) - seq_len - 2) // total_seqs)
    offs = np.arange(total_seqs, dtype=np.int64) * stride
    idx = offs[:, None] + np.arange(seq_len + 1, dtype=np.int64)[None, :]
    seqs = stream[idx].reshape(n_batches, batch, seq_len + 1)
    return np.ascontiguousarray(seqs[:, :, :-1], dtype=np.uint32), np.ascontiguousarray(seqs[:, :, 1:], dtype=np.uint32)


# ==================== Associative Recall Circuit Suite ====================
# Raw byte tokens: keys = 'a'..'z' (97..122), vals = '0'..'9' (48..57),
# separators: '=' (61), ',' (44), '?' (63), '\n' (10), pad = 32 (' ')
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
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate exact KV associative-recall sequences for training, validation,
    counterfactual probes (value_swap, query_change), and length extrapolation.
    Sequence layout: k0=v0,k1=v1,...,k_{p-1}=v_{p-1},[filler...]?q=v_q
    Supervision mask targets ONLY the answer token v_q (or all answers if ordered_query).
    """
    if ordered_query:
        if n_pairs <= 0:
            raise ValueError(f"ordered_query requires n_pairs >= 1, got {n_pairs}")
        filler_tokens = (filler_len + 1) if filler_len > 0 else 0
        # 4 tokens per binding + filler + 5 tokens per non-final query + 3 tokens for final query
        req_len = 4 * n_pairs + filler_tokens + 5 * (n_pairs - 1) + 3
        if req_len > seq_len:
            raise ValueError(
                f"ordered KV probe (n_pairs={n_pairs}, filler_len={filler_len}) requires "
                f"seq_len >= {req_len}, got {seq_len}"
            )
    rng = np.random.default_rng(seed)
    toks = np.full((n_seqs, seq_len), _PAD, dtype=np.uint32)
    tgts = np.full((n_seqs, seq_len), BCIR_EXT_IGNORE, dtype=np.uint32)
    filler_pool = np.arange(ord("A"), ord("Z") + 1, dtype=np.uint32)
    for i in range(n_seqs):
        keys = rng.choice(_KEY_TOKS, size=n_pairs, replace=False)
        vals = rng.choice(_VAL_TOKS, size=n_pairs, replace=True)
        q_idx = int(rng.integers(0, n_pairs))
        if mode == "value_swap" and n_pairs >= 2:
            # Swap values of queried key and another distinct key to test binding vs prior
            other = (q_idx + 1) % n_pairs
            while vals[other] == vals[q_idx]:
                vals[other] = rng.choice(_VAL_TOKS)
            vals[q_idx], vals[other] = vals[other], vals[q_idx]
        elif mode == "query_change" and n_pairs >= 2:
            # Query a different key from the exact same prefix
            q_idx = (q_idx + 1) % n_pairs

        seq: List[int] = []
        for p in range(n_pairs):
            seq.extend([int(keys[p]), _EQ, int(vals[p]), _SEP])
        if filler_len > 0:
            fl = rng.choice(filler_pool, size=filler_len, replace=True)
            seq.extend(int(x) for x in fl)
            seq.append(_SEP)
        if not ordered_query:
            seq.extend([_QRY, int(keys[q_idx]), _EQ])
            ans_pos = len(seq) - 1
            ans_val = int(vals[q_idx])
            if len(seq) > seq_len:
                raise ValueError(f"KV seq len {len(seq)} exceeds {seq_len}")
            toks[i, : len(seq)] = seq
            tgts[i, ans_pos] = ans_val
        else:
            # Ordered query of all n_pairs keys (all n_pairs answers guaranteed to fit)
            for p in range(n_pairs):
                seq.extend([_QRY, int(keys[p]), _EQ])
                ans_pos = len(seq) - 1
                ans_val = int(vals[p])
                tgts[i, ans_pos] = ans_val
                if p + 1 < n_pairs:
                    seq.append(ans_val)
                    seq.append(_SEP)
            assert len(seq) <= seq_len
            toks[i, : len(seq)] = seq
    if ordered_query:
        assert int((tgts != BCIR_EXT_IGNORE).sum()) == n_seqs * n_pairs
    return toks, tgts


def cosine_lr_schedule(steps: int, max_lr: float, min_lr: float, warmup: int) -> np.ndarray:
    lrs = np.zeros(steps, dtype=np.float64)
    for s in range(steps):
        if warmup > 0 and s < warmup:
            lrs[s] = max_lr * float(s + 1) / float(warmup)
        else:
            prog = float(s - warmup) / float(max(1, steps - warmup))
            lrs[s] = min_lr + 0.5 * (max_lr - min_lr) * (1.0 + math.cos(math.pi * min(1.0, prog)))
    return lrs


def train_with_val_selection(
    model: ExtModelHandle,
    train_tk: np.ndarray,
    train_tg: np.ndarray,
    val_tk: np.ndarray,
    val_tg: np.ndarray,
    test_tk: np.ndarray,
    test_tg: np.ndarray,
    opt_cfg: BcirAdamwParams,
    lrs: np.ndarray,
    eval_every: int,
    active_times: Optional[np.ndarray] = None,
    val_active_time: Optional[int] = None,
) -> Dict[str, Any]:
    """Train with full learning curve, validation-only checkpoint selection,
    and single final evaluation on untouched held-out test split (P4/P5)."""
    steps = train_tk.shape[0]
    curve: List[Dict[str, Any]] = []
    t_start = time.perf_counter()

    # Step 0 evaluation
    v0 = model.evaluate(val_tk, val_tg, active_time=val_active_time)
    best_val_ce = v0["ce"] if v0["ok"] else float("inf")
    best_step = 0
    best_w = model.copy_weights()
    curve.append({
        "step": 0, "wall_s": 0.0, "train_loss": None,
        "val_ok": v0["ok"], "val_ce": v0["ce"], "val_ppl": v0["ppl"], "val_acc": v0["accuracy"],
    })

    all_step_ms: List[float] = []
    all_gnorms: List[float] = []
    clip_count = 0
    post_warmup_clips = 0
    post_warmup_steps = 0
    warmup_cut = max(1, steps // 10)
    rc_last = 0

    s = 0
    while s < steps:
        chunk = min(eval_every, steps - s)
        at_slice = active_times[s : s + chunk] if active_times is not None else None
        rc, evs = model.train_loop(
            train_tk[s : s + chunk], train_tg[s : s + chunk], opt_cfg,
            lrs=lrs[s : s + chunk], active_times=at_slice,
        )
        rc_last = rc
        for idx, ev in enumerate(evs):
            st = s + idx + 1
            all_step_ms.append(ev["step_ms"])
            all_gnorms.append(ev["grad_norm"])
            if ev["clipped"]:
                clip_count += 1
            if st > warmup_cut:
                post_warmup_steps += 1
                if ev["clipped"]:
                    post_warmup_clips += 1
        s += chunk
        if rc != 0:
            break
        vv = model.evaluate(val_tk, val_tg, active_time=val_active_time)
        wall_s = time.perf_counter() - t_start
        curve.append({
            "step": s,
            "wall_s": round(wall_s, 3),
            "train_loss": round(float(np.mean([e["loss"] for e in evs[-min(10, len(evs)):]])), 6),
            "grad_norm_mean": round(float(np.mean([e["grad_norm"] for e in evs])), 4),
            "val_ok": vv["ok"],
            "val_ce": vv["ce"],
            "val_ppl": vv["ppl"],
            "val_acc": vv["accuracy"],
            "step_ms_median": round(float(np.median([e["step_ms"] for e in evs])), 3),
        })
        if vv["ok"] and vv["ce"] < best_val_ce:
            best_val_ce = vv["ce"]
            best_step = s
            best_w = model.copy_weights()

    # Restore validation-selected checkpoint and evaluate ONCE on untouched test split
    final_w = model.copy_weights()
    model.load_weights(best_w)
    test_res = model.evaluate(test_tk, test_tg, active_time=val_active_time)
    val_best_check = model.evaluate(val_tk, val_tg, active_time=val_active_time)
    best_val_ok = bool(val_best_check["ok"] and math.isfinite(best_val_ce))
    test_ok = bool(rc_last == 0 and test_res["ok"] and best_val_ok)

    return {
        "rc": rc_last,
        "steps_completed": s,
        "best_step": best_step,
        "best_val_ok": best_val_ok,
        "best_val_ce": best_val_ce if best_val_ok else float("nan"),
        "best_val_ppl": math.exp(min(best_val_ce, 50.0)) if best_val_ok else float("nan"),
        "best_val_acc": val_best_check["accuracy"] if best_val_ok else 0.0,
        "test_ok": test_ok,
        "test_ce": test_res["ce"] if test_res["ok"] else float("nan"),
        "test_ppl": test_res["ppl"],
        "test_acc": test_res["accuracy"] if test_res["ok"] else 0.0,
        "test_tokens": test_res["tokens"],
        "test_nonfinite": test_res["nonfinite"],
        "median_step_ms": float(np.median(all_step_ms)) if all_step_ms else 0.0,
        "mean_step_ms": float(np.mean(all_step_ms)) if all_step_ms else 0.0,
        "total_train_s": round(time.perf_counter() - t_start, 3),
        "clip_rate_all": clip_count / max(1, len(all_gnorms)),
        "clip_rate_post_warmup": post_warmup_clips / max(1, post_warmup_steps),
        "grad_norm_median": float(np.median(all_gnorms)) if all_gnorms else 0.0,
        "grad_norm_max": float(np.max(all_gnorms)) if all_gnorms else 0.0,
        "telemetry": model.telemetry(),
        "mem_stats": model.mem_stats(),
        "curve": curve,
        "best_weights": best_w,
        "final_weights": final_w,
    }


# ==================== Dual-SVID + Joint-ITQ Factorizer ====================
def _dual_svid_single(W: np.ndarray, rank: int, iters: int = 8) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Dual-SVID + Alternating Sign-Scale Refinement (LittleBit Eq. 3-7) for W [dout, din].
    Returns (U[dout, r], V[din, r], h[dout], g[din], l[r]) in float32.
    """
    W64 = W.astype(np.float64)
    dout, din = W64.shape
    r = min(rank, dout, din)
    # Row/col L1 multiscale normalization (LittleBit Eq. 4)
    h = np.mean(np.abs(W64), axis=1) + 1e-8
    Wn = W64 / h[:, None]
    g = np.mean(np.abs(Wn), axis=0) + 1e-8
    Wn = Wn / g[None, :]
    # Randomized/exact SVD of normalized matrix
    U_s, S_s, Vt_s = np.linalg.svd(Wn, full_matrices=False)
    U_r = U_s[:, :r] * np.sqrt(np.maximum(S_s[:r], 1e-12))[None, :]
    V_r = Vt_s[:r, :].T * np.sqrt(np.maximum(S_s[:r], 1e-12))[None, :]
    # Orthogonal Procrustes / Joint-ITQ rotation R to align U_r, V_r with {-1, +1}
    R = np.eye(r, dtype=np.float64)
    for _ in range(iters):
        UR = U_r @ R
        VR = V_r @ R
        Bu = np.where(UR >= 0.0, 1.0, -1.0)
        Bv = np.where(VR >= 0.0, 1.0, -1.0)
        # Joint Procrustes alignment on stacked [U_r; V_r]
        M = U_r.T @ Bu + V_r.T @ Bv
        P, _, Qt = np.linalg.svd(M, full_matrices=False)
        R = P @ Qt
    U_rot = U_r @ R
    V_rot = V_r @ R
    Us = np.where(U_rot >= 0.0, 1.0, -1.0)
    Vs = np.where(V_rot >= 0.0, 1.0, -1.0)
    # Optimal latent scale l via ridge least-squares on (Us * l) @ Vs.T ~= Wn
    # Gram matrix G_m,n = (Us.T Us) * (Vs.T Vs)
    Gu = Us.T @ Us
    Gv = Vs.T @ Vs
    G = Gu * Gv + 1e-4 * np.eye(r)
    rhs = np.sum((Us.T @ Wn) * Vs.T, axis=1)
    l = np.linalg.solve(G, rhs)
    # 2 alternating row/col scale refinement steps on original W64
    Bmat = (Us * l[None, :]) @ Vs.T
    for _ in range(2):
        # Optimal h given g: h_i = (W_i . (B_i * g)) / ||B_i * g||^2
        Bg = Bmat * g[None, :]
        h = np.sum(W64 * Bg, axis=1) / (np.sum(Bg * Bg, axis=1) + 1e-12)
        hB = Bmat * h[:, None]
        g = np.sum(W64 * hB, axis=0) / (np.sum(hB * hB, axis=0) + 1e-12)
    # Keep latent signs in U_rot/V_rot (sign(U_rot)==Us, sign(V_rot)==Vs)
    return (
        Us.astype(np.float32),
        Vs.astype(np.float32),
        h.astype(np.float32),
        g.astype(np.float32),
        l.astype(np.float32),
    )


def factorize_littlebit_matrix(
    W: np.ndarray, r1: int, r2: int = 0, iters: int = 8
) -> Tuple[
    Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    float,
]:
    p1 = _dual_svid_single(W, r1, iters=iters)
    U1, V1, h1, g1, l1 = p1
    W_hat = (h1[:, None] * ((U1 * l1[None, :]) @ V1.T)) * g1[None, :]
    p2 = None
    if r2 > 0:
        res = W.astype(np.float64) - W_hat.astype(np.float64)
        p2 = _dual_svid_single(res, r2, iters=iters)
        U2, V2, h2, g2, l2 = p2
        W_hat = W_hat + (h2[:, None] * ((U2 * l2[None, :]) @ V2.T)) * g2[None, :]
    rel_frob = float(np.linalg.norm(W - W_hat) / (np.linalg.norm(W) + 1e-12))
    return p1, p2, rel_frob


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
