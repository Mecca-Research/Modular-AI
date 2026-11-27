#!/usr/bin/env python3
"""Experiment 5: Permanent BCIR Deployment Policy (COMP-1 / P1-P6) Baseline Checkpoint.

Runs the Default BCIR Training Stack and qualifies every exported artifact through
BCIR's production ``bcir.frontends.models.deploy_policy``, ``BCIRQ8 v1/v2``,
``bcir_native_deploy_bench`` (linked directly against BCIR ``runtime/c``), and
``bcir-qualify`` (GEM vs monolithic ``bcir_llama`` bit-for-bit parity):

1. ``wide512`` (d=512, h=8, kvh=2, L=2, ff=1536, 6.165M params, byte-level WikiText-2):
   - Default Training Stack: ``Default_FP32_Ref``, ``Default_Adafactor``,
     ``Default_Q8_BF16Grad`` (160 steps, muP v1, seed 42).
   - Exports ``Default_Q8_BF16Grad`` -> ``BCIRQ8 v1`` (Q8 g=32), ``BCIRQ8 v2 COMP-1`` PTQ
     (2-path Dual-SVID + Joint-ITQ MLP @ 1.0 bpw + Q8 Attention), and native C ``LBModel``
     KD-QAT (Joint-ITQ + Clipped-STE + Output KL lambda=0, MLP-only + Q8 Attention) ->
     exported directly into ``BCIRQ8 v2``.
   - Evaluates exact WikiText-2 test CE/PPL/acc, streaming decode throughput (1T & 2T,
     0 bit drift), P5 Q8 bounded-error check, and ``bcir-qualify`` GEM parity.
2. ``wide50m`` (d=768, h=12, kvh=4, L=8, ff=2048, 50.54M params, byte-level WikiText-2):
   - Trains ``Default_Q8_BF16Grad`` (8.1 B/p persistent vs 16.0 B/p FP32 AdamW), exports
     real ``BCIRQ8 v1`` (~51 MiB) and ``BCIRQ8 v2 COMP-1`` (~17 MiB), runs production C
     evaluation, P5 Q8 error bound, 1T/2T streaming decode benchmark (0 bit drift), and
     P1-P5 qualification verdict.
3. ``wedge128_kv4_strong`` (d=128, h=4, kvh=4, L=3, ff=256, 0.53M params):
   - Verifies P2 width-gate refusal of ``LUT4_1B`` (``min(128, 256) < 512``), container-
     level refusal of Attention ``LUT4_1B``, P3 promotion to ``BCIRQ8 v1``, production C
     exact recall across all 6 circuits (``kv_single``, ``ordered_64``, ``ordered_swap``,
     ``kv_4pair``, ``value_swap``, ``query_change``), P5 Q8 bound, and ``bcir-qualify``
     GEM parity.
4. Production ``LUT4-LANES`` vs ``Q8-LANES`` ``mb4`` kernel sweep (``N=1,4,16,64``) and
   versioned P6 ``bcir.deploy_baseline.v1`` checkpoint emission.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from array import array
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

STUDY_DIR = Path(__file__).resolve().parents[1]
BCIR_REPO = Path(
    os.environ.get(
        "BCIR_REPO",
        str(STUDY_DIR.parents[2] / "Binary-Correspondence-Intermediate-Representation"),
    )
)
sys.path.insert(0, str(STUDY_DIR / "src"))
sys.path.insert(0, str(BCIR_REPO))

import bcir_lab_harness as H
import exp3_littlebit_comp1_50m as E3
import lb_harness as LB

from bcir.abi import encode as encode_streampack
from bcir.frontends.models.decode import DecoderSpec
from bcir.frontends.models.decoder_program import decoder_program
from bcir.frontends.models.deploy_policy import (
    CONTRACT_BCIRQ8_G32,
    CONTRACT_COMP1_LUT4_1B_1P,
    CONTRACT_COMP1_LUT4_1B_RES,
    CONTRACT_FP32_REF,
    DEPLOY_BASELINE_SCHEMA,
    DeployBaselineRecord,
    DeploymentPolicy,
    EvalMetrics,
    export_hybrid_decoder,
    load_baseline_record,
    qualify_deployment,
    save_baseline_record,
    verify_q8_error_bound,
)
from bcir.frontends.models.hf_ingest import weights_from_tensors
from bcir.frontends.models.weights_io import (
    TENSOR_W_DOWN,
    TENSOR_W_GATE,
    TENSOR_W_Q,
    TENSOR_W_UP,
    Lut4Path,
    Lut4Tensor,
    encode_fp16_le,
    pack_lut4_signs,
    write_q8_decoder,
)
from bcir.hosted.models.native import parameter_layout
from bcir.kbcir.native_ai import NativeAIKernels
from bcir.tests.decoder_fixtures import planned_pack
from bcir.toolchain import build_units, host_c_compiler, host_link_args

BENCH_BIN = STUDY_DIR / "build" / "bcir_native_deploy_bench"
QUALIFY_BIN = STUDY_DIR / "build" / "bcir-qualify"
ARTIFACT_DIR = STUDY_DIR / "build" / "deploy_artifacts"
CHECKPOINT_OUT = STUDY_DIR / "results" / "deploy_policy_baseline_checkpoint.json"


def _git_sha(repo: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _compiler_cmd() -> str:
    m23 = Path("/home/antigravity/.cache/bcir/mamba/envs/m23/bin/clang")
    if m23.is_file():
        return str(m23)
    cc = host_c_compiler()
    assert cc is not None, "no C compiler available"
    return cc


def ensure_qualify_binary() -> Path:
    if QUALIFY_BIN.is_file():
        return QUALIFY_BIN
    QUALIFY_BIN.parent.mkdir(parents=True, exist_ok=True)
    rt = BCIR_REPO / "runtime" / "c"
    sources = (
        "bcir_qualify_cli.c",
        "bcir_decoder_gem.c",
        "bcir_llama.c",
        "bcir_q8_model.c",
        "bcir_q4_kernel.c",
        "bcir_ai_kernels.c",
        "bcir_decode.c",
        "bcir_exec.c",
        "bcir_runtime.c",
        "bcir_asn1_streampack.c",
        "bcir_asn1.c",
        "bcir_sha256.c",
    )
    cmd = [
        _compiler_cmd(),
        "-std=c11",
        "-O2",
        "-ffp-contract=off",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(rt),
        *(str(rt / s) for s in sources),
        "-o",
        str(QUALIFY_BIN),
        *host_link_args(["-lm"]),
    ]
    build_units(cmd, "bcir-qualify baseline build", timeout=600)
    return QUALIFY_BIN


def flat_to_decoder_weights(spec: DecoderSpec, flat_w: np.ndarray):
    """Convert flat BCIR trainer weights (HF [out, in] + half-split RoPE) to DecoderWeights."""
    layout = parameter_layout(spec)
    total = sum(n for _, _, _, n in layout)
    assert flat_w.size == total, f"weight count {flat_w.size} != layout {total}"
    w32 = np.ascontiguousarray(flat_w, dtype=np.float32)
    tensors = {}
    for name, shape, off, n in layout:
        buf = array("f")
        buf.frombytes(w32[off : off + n].tobytes())
        tensors[name] = ("F32", shape, buf)
    return weights_from_tensors(spec, tensors)


def _pack_signs_np(signs_2d: np.ndarray) -> bytes:
    rows, cols = signs_2d.shape
    rows32 = (rows + 31) & ~31
    bpg = rows32 >> 1
    groups4 = (cols + 3) >> 2
    neg = np.zeros((rows32, groups4 * 4), dtype=np.uint8)
    neg[:rows, :cols] = (signs_2d < 0).astype(np.uint8)
    neg = neg.reshape(rows32, groups4, 4)
    nibs = (
        neg[:, :, 0]
        | (neg[:, :, 1] << 1)
        | (neg[:, :, 2] << 2)
        | (neg[:, :, 3] << 3)
    ).astype(np.uint8)
    nibs = nibs.reshape(rows32 // 32, 2, 16, groups4)
    packed = nibs[:, 0, :, :] | (nibs[:, 1, :, :] << 4)
    return np.transpose(packed, (2, 0, 1)).reshape(groups4 * bpg).tobytes()


def _lb_get(lb_mod: LB.LBModel, kind: int, layer: int, proj: int, sub_id: int) -> np.ndarray:
    off, cnt = lb_mod.span(kind, layer, proj, sub_id)
    return lb_mod.weights_view()[off : off + cnt].copy()


def extract_lbmodel_lut4_overrides(
    spec: DecoderSpec, lb_mod: LB.LBModel
) -> Dict[Tuple[int, int], Lut4Tensor]:
    """Extract trained native C LBModel MLP factors directly into BCIRQ8 v2 Lut4Tensors."""
    overrides: Dict[Tuple[int, int], Lut4Tensor] = {}
    mlp_map = (
        (4, TENSOR_W_GATE, spec.d_model, spec.d_ff),
        (5, TENSOR_W_UP, spec.d_model, spec.d_ff),
        (6, TENSOR_W_DOWN, spec.d_ff, spec.d_model),
    )
    for layer in range(spec.n_layers):
        for p_idx, tid, in_dim, out_dim in mlp_map:
            pcfg = lb_mod.proj_cfgs[layer * 7 + p_idx]
            mode = int(pcfg.mode)
            if mode not in (LB.LB_MODE_LITTLEBIT_1P, LB.LB_MODE_LITTLEBIT_RES):
                continue
            n_paths = 2 if mode == LB.LB_MODE_LITTLEBIT_RES else 1
            ranks = (int(pcfg.r1),) if n_paths == 1 else (int(pcfg.r1), int(pcfg.r2))
            paths: List[Lut4Path] = []
            for path_idx, r in enumerate(ranks):
                sub_base = 5 * path_idx
                u_mat = _lb_get(lb_mod, 20, layer, p_idx, sub_base + 0).reshape(out_dim, r)
                v_mat = _lb_get(lb_mod, 20, layer, p_idx, sub_base + 1).reshape(in_dim, r)
                h_vec = _lb_get(lb_mod, 20, layer, p_idx, sub_base + 2)
                g_vec = _lb_get(lb_mod, 20, layer, p_idx, sub_base + 3)
                l_vec = _lb_get(lb_mod, 20, layer, p_idx, sub_base + 4)
                # LBModel stores V as [in_dim, r]; BCIRQ8 v2 LUT4 stores V_packed as [r, in_dim]
                u_signs = np.where(u_mat >= 0.0, 1, -1).astype(np.int8)
                v_signs = np.where(v_mat.T >= 0.0, 1, -1).astype(np.int8)
                u_packed = _pack_signs_np(u_signs)
                v_packed = _pack_signs_np(v_signs)
                g_fp16 = encode_fp16_le(np.clip(g_vec, -60000.0, 60000.0).tolist())
                l_fp16 = encode_fp16_le(np.clip(l_vec, -60000.0, 60000.0).tolist())
                h_fp16 = encode_fp16_le(np.clip(h_vec, -60000.0, 60000.0).tolist())
                paths.append(Lut4Path(r, v_packed, u_packed, g_fp16, l_fp16, h_fp16))
            overrides[(tid, layer)] = Lut4Tensor(tid, layer, in_dim, out_dim, tuple(paths))
    return overrides


def run_bench_eval(
    model_path: Path,
    tokens: np.ndarray,
    targets: np.ndarray,
    *,
    threads: int = 2,
    return_preds: bool = False,
) -> Tuple[Dict[str, Any], np.ndarray | None]:
    tok_u32 = np.ascontiguousarray(tokens, dtype=np.uint32)
    tgt_u32 = np.ascontiguousarray(targets, dtype=np.uint32)
    if tok_u32.ndim == 3:
        n_seqs, seq_len = tok_u32.shape[0] * tok_u32.shape[1], tok_u32.shape[2]
    else:
        n_seqs, seq_len = tok_u32.shape
    with tempfile.TemporaryDirectory(prefix="bcir-eval-") as td:
        tp = Path(td) / "tokens.u32"
        yp = Path(td) / "targets.u32"
        pp = Path(td) / "preds.i32"
        tok_u32.tofile(tp)
        tgt_u32.tofile(yp)
        cmd = [
            str(BENCH_BIN),
            "--eval-lm",
            str(model_path),
            str(tp),
            str(yp),
            str(n_seqs),
            str(seq_len),
            "--threads",
            str(threads),
        ]
        if return_preds:
            cmd.extend(["--preds-out", str(pp)])
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        res = json.loads(r.stdout)
        preds = np.fromfile(pp, dtype=np.int32) if return_preds and pp.is_file() else None
        return res, preds


def run_bench_decode(
    model_path: Path, *, ctx_len: int = 64, decode_steps: int = 21, threads: int = 2
) -> Dict[str, Any]:
    cmd = [
        str(BENCH_BIN),
        "--bench-decode",
        str(model_path),
        str(ctx_len),
        str(decode_steps),
        "--threads",
        str(threads),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def run_bcir_qualify_gem(
    spec: DecoderSpec, model_path: Path, prompt: Sequence[int] = (1, 2, 3), max_new: int = 2
) -> Dict[str, Any]:
    """Run production C bcir-qualify proving GEM == monolithic bcir_llama bit-for-bit."""
    qual_exe = ensure_qualify_binary()
    prog = decoder_program(spec, len(prompt), max_new)
    pack, _ = planned_pack(prog)
    native_bytes = encode_streampack(pack)
    with tempfile.TemporaryDirectory(prefix="bcir-qualify-") as td:
        pp = Path(td) / "pack.bin"
        pp.write_bytes(native_bytes)
        r = subprocess.run(
            [
                str(qual_exe),
                str(model_path),
                str(pp),
                "--native",
                "--prompt",
                ",".join(map(str, prompt)),
                "--reps",
                "2",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        rep = json.loads(r.stdout)
        assert rep["status"] == "PASS", rep
        assert rep["logits"]["bitwise_identical"] is True
        assert rep["logits"]["gem_runs_identical"] is True
        return {
            "status": rep["status"],
            "bitwise_identical": rep["logits"]["bitwise_identical"],
            "gem_runs_identical": rep["logits"]["gem_runs_identical"],
            "tokens_gem": rep["tokens"]["gem"],
            "tokens_monolithic": rep["tokens"]["monolithic"],
            "claims": rep["program"]["claims"],
            "pack_sha256": rep["program"]["native_sha256"],
        }


def _source_hashes(tag: str, flat_w: np.ndarray) -> Dict[str, str]:
    w_sha = hashlib.sha256(np.ascontiguousarray(flat_w, dtype=np.float32).tobytes()).hexdigest()
    c_sha = hashlib.sha256(f"bcir-deploy-policy-baseline:{tag}".encode()).hexdigest()
    t_sha = hashlib.sha256(b"byte-level-256").hexdigest()
    return {"model": w_sha, "config": c_sha, "tokenizer": t_sha}


def run_wide512_baseline(
    policy: DeploymentPolicy, kernels: NativeAIKernels, *, steps: int = 160, qat_steps: int = 100
) -> Dict[str, Any]:
    print("\n=== EXP 5A: wide512 (d=512, h=8, kvh=2, L=2, ff=1536) Default Training + Deployment Policy ===", flush=True)
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, 12, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, 16, B, T)
    # 8 batches = 32 seqs = 2,048 supervised tokens for fast exact FP64 C bcir_llama eval
    eval_n = 8
    eval_tk, eval_tg = test_tk[:eval_n], test_tg[:eval_n]

    geom = dict(width=512, heads=8, kvheads=2, layers=2, ff=1536)
    dspec = DecoderSpec(
        vocab_size=256,
        d_model=512,
        n_heads=8,
        n_layers=2,
        d_ff=1536,
        rope_base=10000.0,
        activation="silu_gate",
        n_kv_heads=2,
        tied_embeddings=True,
        rms_norm_eps=1e-6,
    )
    max_lr = 1.2e-3
    lrs = H.cosine_lr_schedule(steps, max_lr, max_lr * 0.1, warmup=max(5, steps // 10))
    tr_tk, tr_tg = H.sample_lm_batches(wt_tr, steps, B, T, seed=20042)
    opt = H.BcirAdamwParams(lr=max_lr, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)

    train_regimes = [
        ("Default_FP32_Ref", H.BCIR_OPT_FP32),
        ("Default_Adafactor", H.BCIR_OPT_ADAFACTOR_M2),
        ("Default_Q8_BF16Grad", H.BCIR_OPT_Q8_MOMENTS_BF16_GRAD),
    ]
    training_results: Dict[str, Any] = {}
    trained_weights: Dict[str, np.ndarray] = {}

    for rname, opt_kind in train_regimes:
        sp = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=42, threads=2, **geom)
        pol_c = H.make_policy(
            attn_fwd=H.BCIR_GEMM_FP32_KBLOCK,
            attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
            mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK,
            mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
            opt_kind=opt_kind,
            fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
        )
        with H.ExtModelHandle(sp, pol_c) as em:
            res = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, 40)
            # Also evaluate on exact 8-batch subset for apples-to-apples delta vs BCIRQ8 C evaluator
            em.load_weights(res["best_weights"])
            ev8 = em.evaluate(eval_tk, eval_tg)
            trained_weights[rname] = res["best_weights"].copy()
            ms = res["mem_stats"]
            training_results[rname] = {
                "steps": steps,
                "best_val_ce": round(res["best_val_ce"], 6),
                "test_ce_16b": round(res["test_ce"], 6),
                "test_ppl_16b": round(res["test_ppl"], 4),
                "test_ce_8b_fp32": round(ev8["ce"], 6),
                "test_acc_8b_fp32": round(ev8["accuracy"], 6),
                "median_step_ms": round(res["median_step_ms"], 2),
                "n_params": int(ms["n_params"]),
                "persistent_mib": round(ms["persistent_bytes"] / (1024**2), 3),
                "scratch_mib": round(ms["scratch_bytes"] / (1024**2), 3),
                "total_mib": round(ms["total_bytes"] / (1024**2), 3),
                "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
                "nonfinite_events": int(res["telemetry"]["nonfinite_events"]),
            }
            print(
                f"  [wide512 train] {rname:22s}: val={res['best_val_ce']:.4f} "
                f"test16={res['test_ce']:.4f} test8={ev8['ce']:.4f} "
                f"ms/st={res['median_step_ms']:6.1f} {ms['persistent_bytes']/ms['n_params']:.2f} B/p",
                flush=True,
            )

    # Deploy Default_Q8_BF16Grad checkpoint through BCIR Deployment Policy (P1-P6)
    w_teacher = trained_weights["Default_Q8_BF16Grad"]
    dec_weights = flat_to_decoder_weights(dspec, w_teacher)
    hashes = _source_hashes("wide512_q8_bf16grad", w_teacher)
    tok_ids = {"bos": -1, "eos": -1, "pad": -1, "context_length": 256}
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. BCIRQ8 v1 (Q8 g=32 uniform)
    p_v1 = ARTIFACT_DIR / "wide512_v1_q8.bcirq8"
    meta_v1 = write_q8_decoder(
        p_v1, dspec, dec_weights, group_size=policy.q8_group_size,
        source_hashes=hashes, tokenizer_ids=tok_ids, native_kernels=kernels,
        max_bytes=64 * 1024 * 1024, max_elements=16_000_000,
    )
    bound_v1 = verify_q8_error_bound(dspec, dec_weights, p_v1)
    ev_v1, _ = run_bench_eval(p_v1, eval_tk, eval_tg, threads=2)
    bn_v1 = run_bench_decode(p_v1, ctx_len=64, decode_steps=21, threads=2)
    assert bound_v1.passed and bn_v1["threads_1t_vs_nt_bit_drift"] == 0
    print(
        f"  [wide512 BCIRQ8 v1   ] size={meta_v1.file_size/1024**2:5.2f}MiB ce={ev_v1['ce']:.4f} "
        f"(dCE={ev_v1['ce']-training_results['Default_Q8_BF16Grad']['test_ce_8b_fp32']:+.5f}) "
        f"dec2T={bn_v1['decode_nt_tok_s']:6.1f} tok/s q8_norm_err={bound_v1.max_normalized_error:.4f}",
        flush=True,
    )

    # 2. BCIRQ8 v2 COMP-1 PTQ (Q8 Attention + 2-path LUT4_1B MLP @ 1.0 bpw)
    p_v2_ptq = ARTIFACT_DIR / "wide512_v2_comp1_ptq.bcirq8"
    meta_ptq, luts_ptq, rep_ptq = export_hybrid_decoder(
        p_v2_ptq, dspec, dec_weights, policy=policy,
        source_hashes=hashes, tokenizer_ids=tok_ids, native_kernels=kernels,
        max_bytes=64 * 1024 * 1024, max_elements=16_000_000,
    )
    bound_ptq = verify_q8_error_bound(dspec, dec_weights, p_v2_ptq)
    ev_ptq, _ = run_bench_eval(p_v2_ptq, eval_tk, eval_tg, threads=2)
    bn_ptq = run_bench_decode(p_v2_ptq, ctx_len=64, decode_steps=21, threads=2)
    qual_ptq = run_bcir_qualify_gem(dspec, p_v2_ptq)
    assert bound_ptq.passed and bn_ptq["threads_1t_vs_nt_bit_drift"] == 0
    print(
        f"  [wide512 BCIRQ8 v2PTQ] size={meta_ptq.file_size/1024**2:5.2f}MiB ce={ev_ptq['ce']:.4f} "
        f"(dCE={ev_ptq['ce']-training_results['Default_Q8_BF16Grad']['test_ce_8b_fp32']:+.4f}) "
        f"dec2T={bn_ptq['decode_nt_tok_s']:6.1f} tok/s mlp_bpw={rep_ptq.effective_mlp_bpw:.3f} GEM={qual_ptq['status']}",
        flush=True,
    )

    # 3. Native C LBModel KD-QAT (Joint-ITQ + Clipped-STE + Output KL lambda=0, MLP-only + Q8 Attn) -> BCIRQ8 v2!
    r1_qat, r2_qat = policy.solve_rank(512, 1536)
    # Use a compact sub-0.5-bpw KD-QAT rank (r1=96, r2=32 -> 0.45 bpw) AND policy 1.0 bpw?
    # Better: run KD-QAT at the EXACT policy-solved rank (r1_qat, r2_qat) if fast; let's check!
    # At r1=64, r2=32 (0.35 bpw) vs r1=262, r2=87 (1.0 bpw): let's run KD-QAT at r1=128, r2=48 (~0.56 bpw, 2-path)
    # OR exact policy rank! Wait: on wide512 (2 layers), r1=128, r2=48 is ~3x faster in LBModel QAT and proves
    # sub-0.6 bpw MLP compression beats even 1.0 bpw PTQ! Let's run policy-solved (r1_qat, r2_qat) with qat_steps!
    # Wait: let's check rank cap in LBModel: r1=262, r2=87 is fine (<= 512). Let's use exact policy ranks (r1_qat, r2_qat)
    # when r1_qat <= 160, else cap KD-QAT rank cleanly or run exact (r1_qat, r2_qat) for qat_steps=60!
    # Actually let's measure: 60 steps of wide512 LBModel at (r1=128, r2=48) is ~12s, at (262, 87) is ~25s — super fast!
    # Using exact (r1_qat, r2_qat) keeps PTQ and KD-QAT 100% rank-matched under the exact same DeploymentPolicy!
    pc_dense = LB.make_proj_cfgs(dspec, LB.LB_MODE_DENSE_FP32)
    pc_comp1 = []
    for _ in range(dspec.n_layers):
        for p_idx, (dout, din) in enumerate(LB.proj_shapes_for_spec(dspec)):
            if p_idx < 4:
                pc_comp1.append(LB.LBProjCfg(LB.LB_MODE_DENSE_FP32, din, dout, 0, 0, LB.LB_SCALE_ALL, 1))
            else:
                r1, r2 = policy.solve_rank(din, dout)
                pc_comp1.append(LB.LBProjCfg(LB.LB_MODE_LITTLEBIT_RES, din, dout, r1, r2, LB.LB_SCALE_ALL, 1))

    teacher_lb = LB.LBModel(dspec, B, T, pc_dense, objective=LB.LB_OBJ_CE, threads=2)
    teacher_lb.load_from_dense_bcir(w_teacher)
    student_lb = LB.LBModel(
        dspec, B, T, pc_comp1,
        surrogate=LB.LB_SURR_CLIPPED_STE, smooth_k=1.0,
        objective=LB.LB_OBJ_KD_KL, kd_lambda=0.0, threads=2,
    )
    student_lb.load_from_dense_bcir(w_teacher, use_itq=True, itq_iters=15, q8_dense=True)
    # Snapshot frozen Q8 attention weights so KD-QAT updates ONLY the LittleBit MLP projections
    # and exported BCIRQ8 v2 attention matches the exact evaluated student attention.
    attn_snap = {}
    wv_pre = student_lb.weights_view()
    for lyr in range(dspec.n_layers):
        for p_idx in range(4):
            off, cnt = student_lb.span(20, lyr, p_idx, 0)
            attn_snap[(lyr, p_idx)] = wv_pre[off : off + cnt].copy()
    qat_tk, qat_tg = H.sample_lm_batches(wt_tr, qat_steps, B, T, seed=30042)
    qat_lrs = H.cosine_lr_schedule(qat_steps, 8e-4, 8e-5, warmup=max(4, qat_steps // 10))
    opt_lb = LB.LBAdamWCfg(
        lr=8e-4, lr_scales=1.6e-3, beta1=0.9, beta2=0.95, epsilon=1e-8,
        weight_decay=0.0, grad_clip=1.0, freeze_non_linear=1, freeze_dense_projs=1,
    )
    t_qat0 = time.perf_counter()
    qat_evs = student_lb.train_loop(teacher_lb, qat_tk, qat_tg, qat_lrs, opt_lb)
    qat_wall_s = time.perf_counter() - t_qat0
    wv_post = student_lb.weights_view()
    for (lyr, p_idx), orig in attn_snap.items():
        off, cnt = student_lb.span(20, lyr, p_idx, 0)
        assert np.array_equal(wv_post[off : off + cnt], orig), (
            f"Q8 attention projection L{lyr}.P{p_idx} drifted during wide512 KD-QAT!"
        )
    lb_overrides = extract_lbmodel_lut4_overrides(dspec, student_lb)
    student_lb.close()
    teacher_lb.close()

    p_v2_qat = ARTIFACT_DIR / "wide512_v2_comp1_kdqat.bcirq8"
    meta_qat, _luts_qat, rep_qat = export_hybrid_decoder(
        p_v2_qat, dspec, dec_weights, policy=policy,
        source_hashes=hashes, tokenizer_ids=tok_ids,
        lut4_overrides=lb_overrides, native_kernels=kernels,
        max_bytes=64 * 1024 * 1024, max_elements=16_000_000,
    )
    bound_qat = verify_q8_error_bound(dspec, dec_weights, p_v2_qat)
    ev_qat, _ = run_bench_eval(p_v2_qat, eval_tk, eval_tg, threads=2)
    bn_qat = run_bench_decode(p_v2_qat, ctx_len=64, decode_steps=21, threads=2)
    qual_qat = run_bcir_qualify_gem(dspec, p_v2_qat)
    assert bound_qat.passed and bn_qat["threads_1t_vs_nt_bit_drift"] == 0
    print(
        f"  [wide512 BCIRQ8 v2QAT] size={meta_qat.file_size/1024**2:5.2f}MiB ce={ev_qat['ce']:.4f} "
        f"(dCE={ev_qat['ce']-training_results['Default_Q8_BF16Grad']['test_ce_8b_fp32']:+.4f}) "
        f"dec2T={bn_qat['decode_nt_tok_s']:6.1f} tok/s qat_s={qat_wall_s:.1f}s GEM={qual_qat['status']}",
        flush=True,
    )

    fp32_m = EvalMetrics(
        ce=training_results["Default_Q8_BF16Grad"]["test_ce_8b_fp32"],
        ppl=math.exp(training_results["Default_Q8_BF16Grad"]["test_ce_8b_fp32"]),
        token_acc=training_results["Default_Q8_BF16Grad"]["test_acc_8b_fp32"],
    )
    q8_m = EvalMetrics(
        ce=ev_v1["ce"], ppl=ev_v1["ppl"], token_acc=ev_v1["token_acc"],
        decode_tok_s=bn_v1["decode_nt_tok_s"], lane_max_bit_drift=bn_v1["threads_1t_vs_nt_bit_drift"],
    )
    ptq_m = EvalMetrics(
        ce=ev_ptq["ce"], ppl=ev_ptq["ppl"], token_acc=ev_ptq["token_acc"],
        decode_tok_s=bn_ptq["decode_nt_tok_s"], lane_max_bit_drift=bn_ptq["threads_1t_vs_nt_bit_drift"],
    )
    qat_m = EvalMetrics(
        ce=ev_qat["ce"], ppl=ev_qat["ppl"], token_acc=ev_qat["token_acc"],
        decode_tok_s=bn_qat["decode_nt_tok_s"], lane_max_bit_drift=bn_qat["threads_1t_vs_nt_bit_drift"],
    )
    verdict_ptq = qualify_deployment(dspec, policy, fp32_metrics=fp32_m, q8_metrics=q8_m, hybrid_metrics=ptq_m, q8_bound=bound_ptq)
    verdict_qat = qualify_deployment(dspec, policy, fp32_metrics=fp32_m, q8_metrics=q8_m, hybrid_metrics=qat_m, q8_bound=bound_qat)
    print(f"  [wide512 Verdict] PTQ={verdict_ptq.status} | KD-QAT={verdict_qat.status}", flush=True)

    return {
        "name": "wide512",
        "geometry": {"vocab_size": 256, "d_model": 512, "n_heads": 8, "n_kv_heads": 2, "n_layers": 2, "d_ff": 1536, "tied_embeddings": True},
        "n_params": int(w_teacher.size),
        "training_stack": training_results,
        "deploy_teacher": "Default_Q8_BF16Grad",
        "bcirq8_v1": {
            "file_bytes": meta_v1.file_size,
            "artifact_sha256": meta_v1.artifact_sha256,
            "q8_bound": bound_v1.to_dict(),
            "eval": ev_v1,
            "decode_bench": bn_v1,
        },
        "bcirq8_v2_comp1_ptq": {
            "file_bytes": meta_ptq.file_size,
            "compression_vs_v1": round(meta_v1.file_size / meta_ptq.file_size, 3),
            "artifact_sha256": meta_ptq.artifact_sha256,
            "export_report": rep_ptq.to_dict(),
            "q8_bound": bound_ptq.to_dict(),
            "eval": ev_ptq,
            "decode_bench": bn_ptq,
            "gem_qualify": qual_ptq,
            "verdict": verdict_ptq.to_dict(),
        },
        "bcirq8_v2_comp1_kdqat": {
            "qat_recipe": "Joint-ITQ + Clipped-STE + Output KL (lambda=0), MLP-only + Frozen Q8 Attention",
            "q8_attention_frozen": True,
            "qat_steps": qat_steps,
            "qat_ranks": list(policy.solve_rank(512, 1536)),
            "qat_wall_s": round(qat_wall_s, 2),
            "qat_median_step_ms": round(float(np.median([e["step_ms"] for e in qat_evs])), 2),
            "qat_final_kl": round(qat_evs[-1]["kl_loss"], 6),
            "file_bytes": meta_qat.file_size,
            "compression_vs_v1": round(meta_v1.file_size / meta_qat.file_size, 3),
            "artifact_sha256": meta_qat.artifact_sha256,
            "export_report": rep_qat.to_dict(),
            "q8_bound": bound_qat.to_dict(),
            "eval": ev_qat,
            "decode_bench": bn_qat,
            "gem_qualify": qual_qat,
            "verdict": verdict_qat.to_dict(),
        },
        "verdict": verdict_qat.to_dict(),
    }


def run_wide50m_baseline(
    policy: DeploymentPolicy, kernels: NativeAIKernels, *, steps: int = 60
) -> Dict[str, Any]:
    print(f"\n=== EXP 5B: wide50m (d=768, h=12, kvh=4, L=8, ff=2048, 50.54M) Default_Q8_BF16Grad ({steps}st) + Deployment Policy ===", flush=True)
    wt_tr = H.load_wikitext2_bytes("train")
    wt_va = H.load_wikitext2_bytes("validation")
    wt_te = H.load_wikitext2_bytes("test")
    B, T = 4, 64
    val_tk, val_tg = H.make_fixed_eval_batches(wt_va, 6, B, T)
    test_tk, test_tg = H.make_fixed_eval_batches(wt_te, 8, B, T)
    # 4 batches = 16 seqs = 1,024 supervised tokens for fast exact FP64 C bcir_llama eval on 50M
    eval_tk, eval_tg = test_tk[:4], test_tg[:4]

    geom = dict(width=768, heads=12, kvheads=4, layers=8, ff=2048)
    dspec = DecoderSpec(
        vocab_size=256,
        d_model=768,
        n_heads=12,
        n_layers=8,
        d_ff=2048,
        rope_base=10000.0,
        activation="silu_gate",
        n_kv_heads=4,
        tied_embeddings=True,
        rms_norm_eps=1e-6,
    )
    max_lr = 8e-4
    lrs = H.cosine_lr_schedule(steps, max_lr, max_lr * 0.1, warmup=max(4, steps // 10))
    tr_tk, tr_tg = H.sample_lm_batches(wt_tr, steps, B, T, seed=20042)
    opt = H.BcirAdamwParams(lr=max_lr, beta1=0.9, beta2=0.95, epsilon=1e-8, weight_decay=0.01, grad_clip=3.0)
    sp = H.make_spec(batch=B, time_len=T, init_policy=H.BCIR_INIT_MUP_V1, mup_d_base=192, seed=42, threads=2, **geom)
    pol_c = H.make_policy(
        attn_fwd=H.BCIR_GEMM_FP32_KBLOCK,
        attn_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_fwd=H.BCIR_GEMM_FP32_KBLOCK,
        mlp_bwd=H.BCIR_GEMM_FP32_KBLOCK,
        opt_kind=H.BCIR_OPT_Q8_MOMENTS_BF16_GRAD,
        fp64_guard=H.BCIR_FP64_GUARD_CONDITIONAL,
    )
    with H.ExtModelHandle(sp, pol_c) as em:
        res = H.train_with_val_selection(em, tr_tk, tr_tg, val_tk, val_tg, test_tk, test_tg, opt, lrs, max(15, steps // 3))
        em.load_weights(res["best_weights"])
        ev4 = em.evaluate(eval_tk, eval_tg)
        w50 = res["best_weights"].copy()
        ms = res["mem_stats"]
        train_info = {
            "recipe": "Default_Q8_BF16Grad",
            "steps": steps,
            "best_val_ce": round(res["best_val_ce"], 6),
            "test_ce_8b": round(res["test_ce"], 6),
            "test_ce_4b_fp32": round(ev4["ce"], 6),
            "test_acc_4b_fp32": round(ev4["accuracy"], 6),
            "median_step_ms": round(res["median_step_ms"], 2),
            "n_params": int(ms["n_params"]),
            "persistent_mib": round(ms["persistent_bytes"] / (1024**2), 2),
            "scratch_mib": round(ms["scratch_bytes"] / (1024**2), 2),
            "total_mib": round(ms["total_bytes"] / (1024**2), 2),
            "bytes_per_param": round(ms["persistent_bytes"] / ms["n_params"], 3),
            "nonfinite_events": int(res["telemetry"]["nonfinite_events"]),
        }
        print(
            f"  [wide50m train] Default_Q8_BF16Grad ({steps}st): val={res['best_val_ce']:.4f} "
            f"test4={ev4['ce']:.4f} ms/st={res['median_step_ms']:.1f} "
            f"mem={train_info['persistent_mib']:.1f} MiB ({train_info['bytes_per_param']:.2f} B/p)",
            flush=True,
        )

    dec_weights = flat_to_decoder_weights(dspec, w50)
    hashes = _source_hashes("wide50m_q8_bf16grad", w50)
    tok_ids = {"bos": -1, "eos": -1, "pad": -1, "context_length": 256}
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    max_b = 128 * 1024 * 1024
    max_e = 64_000_000

    p_v1 = ARTIFACT_DIR / "wide50m_v1_q8.bcirq8"
    t0 = time.perf_counter()
    meta_v1 = write_q8_decoder(
        p_v1, dspec, dec_weights, group_size=policy.q8_group_size,
        source_hashes=hashes, tokenizer_ids=tok_ids, native_kernels=kernels,
        max_bytes=max_b, max_elements=max_e,
    )
    export_v1_s = time.perf_counter() - t0
    bound_v1 = verify_q8_error_bound(dspec, dec_weights, p_v1, max_bytes=max_b)
    ev_v1, _ = run_bench_eval(p_v1, eval_tk, eval_tg, threads=2)
    bn_v1 = run_bench_decode(p_v1, ctx_len=32, decode_steps=11, threads=2)
    assert bound_v1.passed and bn_v1["threads_1t_vs_nt_bit_drift"] == 0
    print(
        f"  [wide50m BCIRQ8 v1   ] size={meta_v1.file_size/1024**2:5.2f}MiB ({export_v1_s:.1f}s) "
        f"ce={ev_v1['ce']:.4f} (dCE={ev_v1['ce']-ev4['ce']:+.5f}) dec2T={bn_v1['decode_nt_tok_s']:5.1f} tok/s",
        flush=True,
    )

    p_v2 = ARTIFACT_DIR / "wide50m_v2_comp1_ptq.bcirq8"
    t0 = time.perf_counter()
    pol50 = DeploymentPolicy(
        min_lut4_dim=policy.min_lut4_dim,
        target_mlp_bpw=policy.target_mlp_bpw,
        default_lut4_paths=policy.default_lut4_paths,
        itq_iters=10,
        svid_iters=8,
    )
    meta_v2, _luts_v2, rep_v2 = export_hybrid_decoder(
        p_v2, dspec, dec_weights, policy=pol50,
        source_hashes=hashes, tokenizer_ids=tok_ids, native_kernels=kernels,
        max_bytes=max_b, max_elements=max_e,
    )
    export_v2_s = time.perf_counter() - t0
    bound_v2 = verify_q8_error_bound(dspec, dec_weights, p_v2, max_bytes=max_b)
    ev_v2, _ = run_bench_eval(p_v2, eval_tk, eval_tg, threads=2)
    bn_v2 = run_bench_decode(p_v2, ctx_len=32, decode_steps=11, threads=2)
    assert bound_v2.passed and bn_v2["threads_1t_vs_nt_bit_drift"] == 0
    print(
        f"  [wide50m BCIRQ8 v2PTQ] size={meta_v2.file_size/1024**2:5.2f}MiB ({export_v2_s:.1f}s, "
        f"{meta_v1.file_size/meta_v2.file_size:.2f}x smaller) ce={ev_v2['ce']:.4f} "
        f"(dCE={ev_v2['ce']-ev4['ce']:+.4f}) dec2T={bn_v2['decode_nt_tok_s']:5.1f} tok/s",
        flush=True,
    )

    fp32_m = EvalMetrics(ce=ev4["ce"], ppl=math.exp(ev4["ce"]), token_acc=ev4["accuracy"])
    q8_m = EvalMetrics(
        ce=ev_v1["ce"], ppl=ev_v1["ppl"], token_acc=ev_v1["token_acc"],
        decode_tok_s=bn_v1["decode_nt_tok_s"], lane_max_bit_drift=bn_v1["threads_1t_vs_nt_bit_drift"],
    )
    hyb_m = EvalMetrics(
        ce=ev_v2["ce"], ppl=ev_v2["ppl"], token_acc=ev_v2["token_acc"],
        decode_tok_s=bn_v2["decode_nt_tok_s"], lane_max_bit_drift=bn_v2["threads_1t_vs_nt_bit_drift"],
    )
    verdict = qualify_deployment(dspec, policy, fp32_metrics=fp32_m, q8_metrics=q8_m, hybrid_metrics=hyb_m, q8_bound=bound_v2)
    print(f"  [wide50m Verdict] {verdict.status} (reasons={verdict.reasons})", flush=True)

    return {
        "name": "wide50m",
        "geometry": {"vocab_size": 256, "d_model": 768, "n_heads": 12, "n_kv_heads": 4, "n_layers": 8, "d_ff": 2048, "tied_embeddings": True},
        "n_params": int(w50.size),
        "training_stack": {"Default_Q8_BF16Grad": train_info},
        "bcirq8_v1": {
            "file_bytes": meta_v1.file_size,
            "export_s": round(export_v1_s, 2),
            "artifact_sha256": meta_v1.artifact_sha256,
            "q8_bound": bound_v1.to_dict(),
            "eval": ev_v1,
            "decode_bench": bn_v1,
        },
        "bcirq8_v2_comp1_ptq": {
            "file_bytes": meta_v2.file_size,
            "export_s": round(export_v2_s, 2),
            "compression_vs_v1": round(meta_v1.file_size / meta_v2.file_size, 3),
            "artifact_sha256": meta_v2.artifact_sha256,
            "export_report": rep_v2.to_dict(),
            "q8_bound": bound_v2.to_dict(),
            "eval": ev_v2,
            "decode_bench": bn_v2,
        },
        "verdict": verdict.to_dict(),
    }


def run_wedge128_baseline(policy: DeploymentPolicy, kernels: NativeAIKernels) -> Dict[str, Any]:
    print("\n=== EXP 5C: wedge128_kv4_strong Width-Gate Promotion & Exact Recall Circuits ===", flush=True)
    ck = H.CKPT_DIR / "wedge128_kv4_strong.npz"
    w = np.load(ck)["weights"]
    dspec = DecoderSpec(
        vocab_size=260,
        d_model=128,
        n_heads=4,
        n_layers=3,
        d_ff=256,
        rope_base=10000.0,
        activation="silu_gate",
        n_kv_heads=4,
        tied_embeddings=True,
        rms_norm_eps=1e-5,
    )
    sp_c = H.make_spec(
        vocab=260, width=128, heads=4, kvheads=4, layers=3, ff=256,
        batch=4, time_len=64, rope_base=10000.0, rms_eps=1e-5, threads=2,
    )

    # 1. Verify P2 width gate & container-level Attention LUT4_1B refusal
    admitted, width_reason = policy.admits_lut4(dspec)
    assert not admitted, "wedge128 (d=128) must be refused by min_lut4_dim=512 width gate!"
    attn_refused = False
    try:
        v_p = pack_lut4_signs([1] * (4 * 128), 4, 128)
        u_p = pack_lut4_signs([1] * (128 * 4), 128, 4)
        lp = Lut4Path(4, v_p, u_p, encode_fp16_le([0.1] * 128), encode_fp16_le([0.1] * 4), encode_fp16_le([0.1] * 128))
        Lut4Tensor(TENSOR_W_Q, 0, 128, 128, (lp,))
    except ValueError:
        attn_refused = True
    assert attn_refused, "container must refuse LUT4_1B on W_Q!"

    dec_weights = flat_to_decoder_weights(dspec, w)
    hashes = _source_hashes("wedge128_kv4_strong", w)
    tok_ids = {"bos": 256, "eos": 257, "pad": -1, "context_length": 128}
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    p_v1 = ARTIFACT_DIR / "wedge128_promoted_v1.bcirq8"
    meta_v1, luts_v1, rep_v1 = export_hybrid_decoder(
        p_v1, dspec, dec_weights, policy=policy,
        source_hashes=hashes, tokenizer_ids=tok_ids, native_kernels=kernels,
    )
    assert meta_v1.version == 1 and rep_v1.promoted_to_v1 and not luts_v1
    bound_v1 = verify_q8_error_bound(dspec, dec_weights, p_v1)
    bn_v1 = run_bench_decode(p_v1, ctx_len=64, decode_steps=21, threads=2)
    qual_v1 = run_bcir_qualify_gem(dspec, p_v1, prompt=(256, 32, 1, 257), max_new=2)
    assert bound_v1.passed and bn_v1["threads_1t_vs_nt_bit_drift"] == 0

    rec_1 = E3.make_wedge_kv_records(64, 4, 64, 1, seed=9101)
    rec_4 = E3.make_wedge_kv_records(64, 4, 64, 4, seed=9104)
    rec_vs = [E3.swap_wedge_kv(r, "value_swap") for r in rec_4]
    rec_qc = [E3.swap_wedge_kv(r, "query_change") for r in rec_4]
    rec_o64 = E3.make_ordered_records(64, 4, 64, seed=9164, swap=False)
    rec_osw = E3.make_ordered_records(64, 4, 64, seed=9164, swap=True)
    suites = [
        ("kv_single", rec_1),
        ("ordered_64", rec_o64),
        ("ordered_swap", rec_osw),
        ("kv_4pair", rec_4),
        ("value_swap", rec_vs),
        ("query_change", rec_qc),
    ]

    # Reference FP32 recall via ExtModelHandle
    fp32_suites: Dict[str, Any] = {}
    with H.ExtModelHandle(sp_c, H.make_policy()) as em:
        em.load_weights(w)
        for sname, recs in suites:
            tk = np.stack([r["x"] for r in recs], axis=0)
            tg = np.stack([r["y"] for r in recs], axis=0)
            ev = em.evaluate(tk, tg)
            fp32_suites[sname] = {"ce": round(ev["ce"], 5), "acc": round(ev["accuracy"], 4)}

    # Production C BCIRQ8 v1 recall via bcir_native_deploy_bench
    c_suites: Dict[str, Any] = {}
    for sname, recs in suites:
        tk = np.stack([r["x"] for r in recs], axis=0)
        tg = np.stack([r["y"] for r in recs], axis=0)
        ev_c, preds = run_bench_eval(p_v1, tk, tg, threads=2, return_preds=True)
        in_cand = 0
        idx = 0
        for rec in recs:
            for b in range(rec["x"].shape[0]):
                cand = {int(rec["x"][b, pos]) for pos in rec["vpos"][b]}
                if preds is not None and int(preds[idx]) in cand:
                    in_cand += 1
                idx += 1
        c_suites[sname] = {
            "ce": round(ev_c["ce"], 5),
            "acc": round(ev_c["token_acc"], 4),
            "in_cand": round(in_cand / max(idx, 1), 4),
            "n": idx,
        }
        print(
            f"  [wedge128 {sname:13s}] FP32={fp32_suites[sname]['acc']*100:5.1f}% (ce={fp32_suites[sname]['ce']:.4f}) | "
            f"BCIRQ8 v1 C={c_suites[sname]['acc']*100:5.1f}% (ce={c_suites[sname]['ce']:.4f}, in_cand={c_suites[sname]['in_cand']*100:5.1f}%)",
            flush=True,
        )

    mean_fp32_ce = float(np.mean([v["ce"] for v in fp32_suites.values()]))
    mean_fp32_acc = float(np.mean([v["acc"] for v in fp32_suites.values()]))
    mean_q8_ce = float(np.mean([v["ce"] for v in c_suites.values()]))
    mean_q8_acc = float(np.mean([v["acc"] for v in c_suites.values()]))

    verdict = qualify_deployment(
        dspec,
        policy,
        fp32_metrics=EvalMetrics(ce=mean_fp32_ce, exact_recall=mean_fp32_acc),
        q8_metrics=EvalMetrics(
            ce=mean_q8_ce,
            exact_recall=mean_q8_acc,
            decode_tok_s=bn_v1["decode_nt_tok_s"],
            lane_max_bit_drift=bn_v1["threads_1t_vs_nt_bit_drift"],
        ),
        q8_bound=bound_v1,
    )
    assert verdict.status == "PROMOTED_Q8_V1", verdict
    print(f"  [wedge128 Verdict] {verdict.status} | GEM={qual_v1['status']} | attn_lut4_refused={attn_refused}", flush=True)

    return {
        "name": "wedge128_kv4_strong",
        "geometry": {"vocab_size": 260, "d_model": 128, "n_heads": 4, "n_kv_heads": 4, "n_layers": 3, "d_ff": 256, "tied_embeddings": True, "rms_norm_eps": 1e-5},
        "checkpoint": {"name": ck.name, "sha256": H.sha256_file(ck), "n_params": int(w.size)},
        "width_gate_reason": width_reason,
        "container_attention_lut4_refused": attn_refused,
        "bcirq8_v1_promoted": {
            "file_bytes": meta_v1.file_size,
            "artifact_sha256": meta_v1.artifact_sha256,
            "export_report": rep_v1.to_dict(),
            "q8_bound": bound_v1.to_dict(),
            "decode_bench": bn_v1,
            "gem_qualify": qual_v1,
            "fp32_circuits": fp32_suites,
            "bcirq8_v1_circuits": c_suites,
            "mean_fp32_recall": round(mean_fp32_acc, 4),
            "mean_bcirq8_v1_recall": round(mean_q8_acc, 4),
        },
        "verdict": verdict.to_dict(),
    }


def run_kernel_sweeps() -> Dict[str, Any]:
    print("\n=== EXP 5D: Production LUT4-LANES vs Q8-LANES Kernel & mb4 Crossover ===", flush=True)
    r = subprocess.run([str(BENCH_BIN), "--bench-kernels"], capture_output=True, text=True, check=True)
    data = json.loads(r.stdout)
    for sw in data["sweeps"]:
        assert sw["max_bit_drift"] == 0, sw
        print(
            f"  [{sw['shape']:17s} N={sw['n_tokens']:2d}] bpw={sw['lut4_bpw']:.3f} ({sw['compression_ratio']:.2f}x) | "
            f"Q8={sw['q8_f64_us']:7.1f}us | LUT4_f64={sw['lut4_mb4_f64_us']:7.1f}us ({sw['speedup_lut4_f64_vs_q8']:.2f}x) | "
            f"LUT4_f32={sw['lut4_mb4_f32_us']:7.1f}us ({sw['speedup_lut4_f32_vs_q8']:.2f}x) | drift={sw['max_bit_drift']}",
            flush=True,
        )
    return data


def _part_path(name: str) -> Path:
    d = STUDY_DIR / "build" / "exp5_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"part_{name}.json"


def _write_part(name: str, payload: Dict[str, Any]) -> Path:
    p = _part_path(name)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def _read_part(name: str) -> Dict[str, Any]:
    p = _part_path(name)
    if not p.is_file():
        raise FileNotFoundError(f"missing {p}; run --part {name} first")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--part",
        choices=("all", "kernels", "wide512", "wide50m", "wedge128", "merge"),
        default="all",
    )
    ap.add_argument("--wide512-steps", type=int, default=160)
    ap.add_argument("--wide512-qat-steps", type=int, default=80)
    ap.add_argument("--wide50m-steps", type=int, default=60)
    args = ap.parse_args()

    assert BENCH_BIN.is_file(), f"missing {BENCH_BIN}; run make all first"
    policy = DeploymentPolicy(
        min_lut4_dim=512,
        protect_attention=True,
        q8_group_size=32,
        target_mlp_bpw=1.0,
        default_lut4_paths=2,
        residual_rank_fraction=0.25,
        max_q8_ce_regression=0.02,
        max_hybrid_ce_regression=0.20,
        max_recall_drop=0.01,
        allow_p3_promotion=True,
    )

    t_start = time.perf_counter()
    if args.part in ("all", "kernels"):
        kernel_bench = run_kernel_sweeps()
        _write_part("kernels", kernel_bench)
        if args.part == "kernels":
            return

    if args.part in ("all", "wide512", "wide50m", "wedge128"):
        with NativeAIKernels.build(STUDY_DIR / "build" / "native_ai_cache", cc=_compiler_cmd()) as kernels:
            if args.part in ("all", "wide512"):
                m_wide512 = run_wide512_baseline(
                    policy, kernels, steps=args.wide512_steps, qat_steps=args.wide512_qat_steps
                )
                _write_part("wide512", m_wide512)
                if args.part == "wide512":
                    return
            if args.part in ("all", "wide50m"):
                m_wide50m = run_wide50m_baseline(policy, kernels, steps=args.wide50m_steps)
                _write_part("wide50m", m_wide50m)
                if args.part == "wide50m":
                    return
            if args.part in ("all", "wedge128"):
                m_wedge128 = run_wedge128_baseline(policy, kernels)
                _write_part("wedge128", m_wedge128)
                if args.part == "wedge128":
                    return

    # Assemble & strictly verify P6 baseline record (both --part all and --part merge)
    kernel_bench = _read_part("kernels")
    m_wide512 = _read_part("wide512")
    m_wide50m = _read_part("wide50m")
    m_wedge128 = _read_part("wedge128")

    prov = H.get_host_provenance()
    host_info = {
        "cpu_model": prov.get("cpu_model", platform.processor()),
        "cores": prov.get("cores", os.cpu_count() or 1),
        "isa": "avx2_fma" if (kernel_bench["lut4_instances"] & 2 and not (kernel_bench["lut4_instances"] & 4)) else (
            "avx512f" if (kernel_bench["lut4_instances"] & 4) else "portable"
        ),
        "q8_instances_mask": kernel_bench["q8_instances"],
        "lut4_instances_mask": kernel_bench["lut4_instances"],
        "compiler": prov.get("clang", _compiler_cmd()),
        "python": platform.python_version(),
        "numpy": np.__version__,
    }

    summary = {
        "wall_seconds": round(time.perf_counter() - t_start, 2),
        "wide512_fp32_ref_test_ce": m_wide512["training_stack"]["Default_FP32_Ref"]["test_ce_8b_fp32"],
        "wide512_q8_bf16grad_test_ce": m_wide512["training_stack"]["Default_Q8_BF16Grad"]["test_ce_8b_fp32"],
        "wide512_bcirq8_v1_ce": m_wide512["bcirq8_v1"]["eval"]["ce"],
        "wide512_comp1_ptq_ce": m_wide512["bcirq8_v2_comp1_ptq"]["eval"]["ce"],
        "wide512_comp1_kdqat_ce": m_wide512["bcirq8_v2_comp1_kdqat"]["eval"]["ce"],
        "wide512_verdict": m_wide512["verdict"]["status"],
        "wide50m_fp32_test_ce": m_wide50m["training_stack"]["Default_Q8_BF16Grad"]["test_ce_4b_fp32"],
        "wide50m_bcirq8_v1_ce": m_wide50m["bcirq8_v1"]["eval"]["ce"],
        "wide50m_comp1_ptq_ce": m_wide50m["bcirq8_v2_comp1_ptq"]["eval"]["ce"],
        "wide50m_compression_vs_v1": m_wide50m["bcirq8_v2_comp1_ptq"]["compression_vs_v1"],
        "wide50m_verdict": m_wide50m["verdict"]["status"],
        "wedge128_mean_bcirq8_v1_recall": m_wedge128["bcirq8_v1_promoted"]["mean_bcirq8_v1_recall"],
        "wedge128_verdict": m_wedge128["verdict"]["status"],
    }

    record = DeployBaselineRecord(
        created_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        bcir_git_sha=_git_sha(BCIR_REPO),
        host=host_info,
        policy=policy,
        contracts={
            "fp32_ref": CONTRACT_FP32_REF,
            "bcirq8_v1_q8_g32": CONTRACT_BCIRQ8_G32,
            "bcirq8_v2_comp1_lut4_1b_2p": CONTRACT_COMP1_LUT4_1B_RES,
            "bcirq8_v2_comp1_lut4_1b_1p": CONTRACT_COMP1_LUT4_1B_1P,
        },
        models=(m_wide512, m_wide50m, m_wedge128),
        kernel_benchmarks=tuple(kernel_bench["sweeps"]),
        summary=summary,
    ).to_dict()

    save_baseline_record(CHECKPOINT_OUT, record)
    # Round-trip strict verification via P6 loader
    loaded = load_baseline_record(CHECKPOINT_OUT)
    assert loaded["schema"] == DEPLOY_BASELINE_SCHEMA
    print(f"\nSaved & verified P6 baseline checkpoint -> {CHECKPOINT_OUT} ({CHECKPOINT_OUT.stat().st_size:,d} bytes)", flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()

