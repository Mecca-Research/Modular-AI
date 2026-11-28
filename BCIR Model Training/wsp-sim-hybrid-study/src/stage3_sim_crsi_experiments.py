#!/usr/bin/env python3
"""Stage 3: Continual Recursive Self-Improvement (CRSI) with Self-Improvement Memory (SIM)
Faithful implementation of Yu et al. (arXiv:2611.18856, Nov 2026) governing BCIR's
3-Tier Hybrid Architecture (Tier 1 SSP-Live + Decoding x Tier 2 WSP WeightPatch x Tier 3 SIM).

Supports crash-atomic per-transition execution via `--only {T1a,T1b,T2a,T2b,T3,S10a,S10b,finalize,all}`.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from common_wsp_sim import (
    Abi6Model,
    CKPT_DIR,
    OPT_Q8_MUON_BF16_GRAD,
    RESULTS_DIR,
    SspConfig,
    StatefulProbe,
    build_comp1_weights,
    composite_behavior_score,
    compute_kl_and_ce,
    compute_rollout_metrics,
    decode_bcirq8_v1_per_tensor,
    extract_domain_prompts,
    get_host_provenance,
    label_loop_tokens,
    load_arch7d_10m_tracked,
    load_wikitext_bytes,
    make_eval_batches,
    make_policy,
    make_ssp_config,
    spec_from_dict,
    write_csv,
    write_json,
)
from stage1_wsp_ssp_locus_audit import (
    apply_canon_scale_edit,
    apply_g1_bias_edit,
    apply_gdn2_state_gate_edit,
    apply_head_wo_zero,
    apply_mhc_mix_edit,
    apply_wdown_sparse_deflation,
    compute_layer_diagnostics,
)


@dataclass
class HybridHarness:
    wsp_l3_wdown_k: int = 0
    wsp_l3_wdown_alpha: float = 0.0
    wsp_gdn_layer: int = -1
    wsp_gdn_k: int = 0
    wsp_gdn_alpha: float = 0.0
    wsp_l3_g1_nheads: int = 0
    wsp_l3_g1_delta: float = 0.0
    wsp_l3_wo_nheads: int = 0
    wsp_l3_wo_scale: float = 1.0
    ssp_weight_kch: int = 0
    ssp_weight_da: float = 0.0
    ssp_weight_de: float = 0.0
    wsp_canon_scale: float = 1.0
    wsp_mhc_delta: float = 0.0
    comp1_mlp_gain: float = 1.0
    ssp_live_damp: float = 0.0
    ssp_live_ashift: float = 0.0
    ssp_live_eshift: float = 0.0
    ssp_live_mla_bias: float = 0.0
    dec_ngram_pen: float = 0.0
    dec_pres_pen: float = 0.0
    dec_rep_win: int = 32
    dec_echo_ng: int = 4


ACTION_FAMILIES: List[Tuple[str, str, str]] = [
    ("wsp_l3_wdown", "P01_L3_RESIDUAL_LOOP", "Global attention layer FFN sparse top-k loop-writing columns"),
    ("wsp_gdn_wdown", "P02_GDN_RECURRENT_FFN", "Recurrent layer FFN sparse columns (collateral state disturbance risk)"),
    ("wsp_l3_g1", "P03_L3_COPY_HEAD_GATE", "Global attention headwise G1 sigmoid output gate bias (1-3 scalars)"),
    ("wsp_l3_wo", "P03_L3_COPY_HEAD_GATE", "Global attention head output projection column scaling"),
    ("ssp_weight_gdn2", "P04_GDN2_STATE_CYCLE", "Persistent GDN-2 ba_chan/be_chan recurrent decay/erase bias edit"),
    ("ssp_live_gdn2", "P04_GDN2_STATE_CYCLE", "Adaptive live GDN-2 recurrent state-echo damping & erase boost (0 weight drift)"),
    ("ssp_live_mla", "P05_GLOBAL_ATTN_ECHO", "Adaptive live global-attention repeat-position logit bias (0 weight drift)"),
    ("harness_ngram", "P06_DECODING_ATTRACTOR", "Decoding harness n-gram echo guard & presence penalty (0 weight drift)"),
    ("wsp_canon", "P07_STRUCTURAL_COLLATERAL", "Canon-AC 4-tap causal conv delayed-tap scaling"),
    ("wsp_mhc", "P07_STRUCTURAL_COLLATERAL", "DeepSeek mHC Birkhoff stream off-diagonal mixing perturbation"),
    ("comp1_scale_calib", "P08_QUANT_LOGIT_SHIFT", "Global MLP down-projection scale recalibration for 1-bit COMP-1 shift"),
]

FAMILY_TO_PROBLEM = {fam: (pid, hyp) for fam, pid, hyp in ACTION_FAMILIES}


class SimLedger:
    """Exact problem-grouped Self-Improvement Memory (arXiv:2611.18856 Section 3.2 & App. D)."""

    def __init__(self, memory_mode: str = "full") -> None:
        self.memory_mode = memory_mode
        self.problems: Dict[str, Dict[str, Any]] = {}

    def clone_for_transfer(self, target_mode: str) -> "SimLedger":
        out = SimLedger(memory_mode=target_mode)
        if target_mode == "none":
            return out
        for pid, p in self.problems.items():
            if target_mode == "problem_only":
                out.problems[pid] = {
                    "id": p["id"], "name": p["name"],
                    "root_cause_hypothesis": p["root_cause_hypothesis"],
                    "opened_round": p["opened_round"], "aliases": list(p["aliases"]),
                    "attempts": [],
                }
            else:
                out.problems[pid] = copy.deepcopy(p)
        return out

    def ensure_problem(self, pid: str, hypothesis: str, round_idx: int) -> Dict[str, Any]:
        if pid not in self.problems:
            self.problems[pid] = {
                "id": pid, "name": pid.replace("_", " ").title(),
                "root_cause_hypothesis": hypothesis, "opened_round": round_idx,
                "aliases": [], "attempts": [],
            }
        return self.problems[pid]

    def record_attempt(
        self, *, action_family: str, round_idx: int, candidate_id: str, accepted: bool,
        mutation_desc: str, rationale: str, predicted_outcome: Dict[str, Any],
        observed_outcome: Dict[str, Any], provenance: Dict[str, Any],
    ) -> None:
        if self.memory_mode == "none":
            return
        pid, hyp = FAMILY_TO_PROBLEM[action_family]
        prob = self.ensure_problem(pid, hyp, round_idx)
        if self.memory_mode == "problem_only":
            return
        prob["attempts"].append({
            "round": round_idx, "candidate_id": candidate_id, "accepted": bool(accepted),
            "action_family": action_family, "mutation_desc": mutation_desc, "rationale": rationale,
            "predicted_outcome": predicted_outcome, "observed_outcome": observed_outcome,
            "provenance": provenance,
        })

    def family_stats(self, current_prov: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        stats: Dict[str, Dict[str, float]] = {
            fam: {"n": 0.0, "n_acc": 0.0, "n_fail": 0.0, "w_gain": 0.0, "w_sum": 0.0,
                  "mean_kl": 0.0, "kl_viol": 0.0, "surprise": 0.0, "problem_opened": 0.0}
            for fam, _, _ in ACTION_FAMILIES
        }
        if self.memory_mode == "none":
            return stats
        for fam, pid, _ in ACTION_FAMILIES:
            if pid in self.problems:
                stats[fam]["problem_opened"] = 1.0
        if self.memory_mode == "problem_only":
            return stats
        for prob in self.problems.values():
            for att in prob["attempts"]:
                fam = att["action_family"]
                if fam not in stats:
                    continue
                prov = att["provenance"]
                sim_w = 0.45
                if prov.get("domain") == current_prov.get("domain"):
                    sim_w += 0.25
                if prov.get("policy_model") == current_prov.get("policy_model"):
                    sim_w += 0.20
                if prov.get("precision_regime") == current_prov.get("precision_regime"):
                    sim_w += 0.10
                obs = att["observed_outcome"]
                pred = att["predicted_outcome"]
                sd = float(obs["score_delta"])
                kl = float(obs["kl_from_clean"])
                st = stats[fam]
                st["n"] += 1.0
                if att["accepted"]:
                    st["n_acc"] += 1.0
                if sd <= 0.05 or kl > 0.045:
                    st["n_fail"] += 1.0
                if kl > 0.045:
                    st["kl_viol"] += 1.0
                st["w_gain"] += sim_w * sd
                st["w_sum"] += sim_w
                st["mean_kl"] += kl
                st["surprise"] += abs(sd - float(pred.get("expected_score_delta", 0.0)))
        for st in stats.values():
            if st["n"] > 0:
                st["mean_kl"] /= st["n"]
                st["surprise"] /= st["n"]
            if st["w_sum"] > 0:
                st["w_gain"] /= st["w_sum"]
        return stats

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "bcir.sim.v1",
            "memory_mode": self.memory_mode,
            "n_problems": len(self.problems),
            "n_attempts": sum(len(p["attempts"]) for p in self.problems.values()),
            "problems": list(self.problems.values()),
        }


def materialize_harness(
    model: Abi6Model, w_base: np.ndarray, diag: Dict[str, Any], h: HybridHarness
) -> Tuple[np.ndarray, SspConfig]:
    w = w_base.copy()
    L = int(model.spec.layers)
    last_l = L - 1
    ld_last = diag["layers"][f"L{last_l}"]

    if abs(h.comp1_mlp_gain - 1.0) > 1e-6:
        tmap = model.tensor_map(w)
        for l in range(L):
            tmap[f"L{l}.wdown"][:] *= np.float32(h.comp1_mlp_gain)

    if h.wsp_l3_wdown_k > 0 and h.wsp_l3_wdown_alpha > 0.0:
        cols = ld_last["top_cols_all"][:min(h.wsp_l3_wdown_k, int(model.spec.ff))]
        w = apply_wdown_sparse_deflation(model, w, last_l, ld_last["u_vec"], cols, h.wsp_l3_wdown_alpha)

    if 0 <= h.wsp_gdn_layer < last_l and h.wsp_gdn_k > 0 and h.wsp_gdn_alpha > 0.0:
        ldg = diag["layers"][f"L{h.wsp_gdn_layer}"]
        cols = ldg["top_cols_all"][:min(h.wsp_gdn_k, int(model.spec.ff))]
        w = apply_wdown_sparse_deflation(model, w, h.wsp_gdn_layer, ldg["u_vec"], cols, h.wsp_gdn_alpha)

    if h.wsp_l3_g1_nheads > 0 and abs(h.wsp_l3_g1_delta) > 1e-6 and int(model.spec.gate_mode) == 1:
        hs = ld_last["top_heads"][:min(h.wsp_l3_g1_nheads, int(model.spec.heads))]
        w = apply_g1_bias_edit(model, w, last_l, hs, h.wsp_l3_g1_delta)

    if h.wsp_l3_wo_nheads > 0 and abs(h.wsp_l3_wo_scale - 1.0) > 1e-6:
        hs = ld_last["top_heads"][:min(h.wsp_l3_wo_nheads, int(model.spec.heads))]
        w = apply_head_wo_zero(model, w, last_l, hs, h.wsp_l3_wo_scale)

    if h.ssp_weight_kch > 0 and (abs(h.ssp_weight_da) > 1e-6 or abs(h.ssp_weight_de) > 1e-6):
        gdn_layers = [l for l in range(L) if int(model.spec.layer_mixer[l]) == 5]
        if gdn_layers:
            w = apply_gdn2_state_gate_edit(model, w, gdn_layers, diag, h.ssp_weight_kch, h.ssp_weight_da, h.ssp_weight_de)

    if abs(h.wsp_canon_scale - 1.0) > 1e-6 and int(model.spec.canon_mode) > 0:
        w = apply_canon_scale_edit(model, w, list(range(L)), h.wsp_canon_scale)

    if abs(h.wsp_mhc_delta) > 1e-6 and int(model.spec.mhc_streams) > 1:
        w = apply_mhc_mix_edit(model, w, h.wsp_mhc_delta)

    ssp = make_ssp_config(
        gdn_alpha_shift=h.ssp_live_ashift,
        gdn_erase_shift=h.ssp_live_eshift,
        state_echo_damp=h.ssp_live_damp,
        mla_repeat_logit_bias=h.ssp_live_mla_bias,
        adaptive_echo_only=1,
        echo_ngram=h.dec_echo_ng,
        echo_window=48,
        ngram_block_penalty=h.dec_ngram_pen,
        presence_penalty=h.dec_pres_pen,
        rep_window=h.dec_rep_win,
    )
    return w, ssp


class RealTaskEnv:
    def __init__(
        self, *, name: str, domain: str, policy_model: str, precision_regime: str,
        model: Abi6Model, w_base: np.ndarray, evolve_prompts: np.ndarray,
        heldout_prompts: np.ndarray, gen_len: int,
        evolve_xs: np.ndarray, evolve_ys: np.ndarray,
        heldout_xs: np.ndarray, heldout_ys: np.ndarray,
    ) -> None:
        self.name = name
        self.domain = domain
        self.policy_model = policy_model
        self.precision_regime = precision_regime
        self.model = model
        self.w_base = w_base.copy()
        self.evolve_prompts = evolve_prompts
        self.heldout_prompts = heldout_prompts
        self.gen_len = gen_len
        self.evolve_xs = evolve_xs
        self.evolve_ys = evolve_ys
        self.heldout_xs = heldout_xs
        self.heldout_ys = heldout_ys
        max_seq = int(max(evolve_prompts.shape[1], heldout_prompts.shape[1]) + gen_len + 16)
        self.probe = StatefulProbe(model, max_seq=max_seq)

        self.model.set_weights(self.w_base)
        self.probe.sync()
        self.ev_ref_ce, self.ev_ref_logits = compute_kl_and_ce(self.model, self.evolve_xs, self.evolve_ys)
        self.te_ref_ce, self.te_ref_logits = compute_kl_and_ce(self.model, self.heldout_xs, self.heldout_ys)

        ev_toks, _, _ = self.probe.generate_batch(self.evolve_prompts, self.gen_len, ssp=None)
        mask = label_loop_tokens(ev_toks, self.evolve_prompts.shape[1])
        contrast = self.probe.probe_contrast(ev_toks, mask)
        self.diag = compute_layer_diagnostics(self.model, contrast, self.w_base)
        self._ce_cache: Dict[str, Dict[str, Any]] = {}
        self._eval_cache: Dict[str, Dict[str, Any]] = {}

    def close(self) -> None:
        self.probe.close()

    def provenance(self, round_idx: int) -> Dict[str, Any]:
        return {
            "domain": self.domain, "benchmark": self.name,
            "policy_model": self.policy_model, "precision_regime": self.precision_regime,
            "round": round_idx,
        }

    def evaluate(self, h: HybridHarness, split: str = "evolve") -> Dict[str, Any]:
        h_key = f"{split}:{json.dumps(asdict(h), sort_keys=True)}"
        if h_key in self._eval_cache:
            return self._eval_cache[h_key]
        w_cand, ssp = materialize_harness(self.model, self.w_base, self.diag, h)
        self.model.set_weights(w_cand)
        self.probe.sync()
        prompts = self.evolve_prompts if split == "evolve" else self.heldout_prompts
        xs = self.evolve_xs if split == "evolve" else self.heldout_xs
        ys = self.evolve_ys if split == "evolve" else self.heldout_ys
        ref_l = self.ev_ref_logits if split == "evolve" else self.te_ref_logits
        base_ce = float(self.ev_ref_ce["ce"] if split == "evolve" else self.te_ref_ce["ce"])

        out_toks, out_ent, _ = self.probe.generate_batch(prompts, self.gen_len, ssp=ssp)
        roll = compute_rollout_metrics(out_toks, prompts.shape[1], out_ent)

        w_key_s = str((
            split, h.wsp_l3_wdown_k, round(h.wsp_l3_wdown_alpha, 4),
            h.wsp_gdn_layer, h.wsp_gdn_k, round(h.wsp_gdn_alpha, 4),
            h.wsp_l3_g1_nheads, round(h.wsp_l3_g1_delta, 4),
            h.wsp_l3_wo_nheads, round(h.wsp_l3_wo_scale, 4),
            h.ssp_weight_kch, round(h.ssp_weight_da, 4), round(h.ssp_weight_de, 4),
            round(h.wsp_canon_scale, 4), round(h.wsp_mhc_delta, 4), round(h.comp1_mlp_gain, 4),
        ))
        changed = int(np.count_nonzero(w_cand - self.w_base))
        if changed == 0:
            ce_res = {"ce": base_ce, "ppl": math.exp(min(base_ce, 50.0)), "kl_from_clean": 0.0, "top1_agreement": 1.0}
        elif w_key_s in self._ce_cache:
            ce_res = self._ce_cache[w_key_s]
        else:
            ce_res, _ = compute_kl_and_ce(self.model, xs, ys, ref_logits=ref_l)
            self._ce_cache[w_key_s] = ce_res

        delta_ce = float(ce_res["ce"] - base_ce)
        kl = float(ce_res["kl_from_clean"])
        score = composite_behavior_score(roll, delta_ce, kl)
        res = {
            "score": score, "loop_seq_rate": roll["loop_seq_rate"],
            "rep4_rate": roll["rep4_rate"], "rep8_rate": roll["rep8_rate"],
            "distinct_4": roll["distinct_4"], "ce": float(ce_res["ce"]),
            "ppl": float(ce_res["ppl"]), "delta_ce": delta_ce,
            "kl_from_clean": kl, "changed_params": changed,
            "loop_flags": roll["loop_flags"],
        }
        self._eval_cache[h_key] = res
        return res


def _mutate_family(
    h: HybridHarness, fam: str, rng: np.random.Generator, env: RealTaskEnv, conservative: bool
) -> Tuple[HybridHarness, str, str]:
    c = copy.deepcopy(h)
    scale = 0.65 if conservative else 1.0
    if fam == "wsp_l3_wdown":
        k = int(rng.choice([16, 32, 64, 96]))
        a = float(np.clip(c.wsp_l3_wdown_alpha + rng.choice([-0.4, 0.35, 0.7, 1.0]) * scale, 0.0, 1.8))
        if a < 0.15:
            a = float(rng.choice([0.4, 0.8, 1.2]))
        c.wsp_l3_wdown_k = k
        c.wsp_l3_wdown_alpha = round(a, 3)
        return c, f"L3.wdown top-{k} alpha={c.wsp_l3_wdown_alpha}", "Deflate loop-writing subspace on global attention FFN"
    if fam == "wsp_gdn_wdown":
        c.wsp_gdn_layer = int(rng.choice([0, 1, 2]))
        c.wsp_gdn_k = int(rng.choice([32, 64]))
        c.wsp_gdn_alpha = round(float(rng.choice([0.6, 1.0, 1.5]) * scale), 3)
        return c, f"L{c.wsp_gdn_layer}.wdown top-{c.wsp_gdn_k} alpha={c.wsp_gdn_alpha}", "Deflate loop columns in recurrent GDN-2 FFN"
    if fam == "wsp_l3_g1":
        if int(env.model.spec.gate_mode) != 1:
            c.wsp_l3_wo_nheads = 1
            c.wsp_l3_wo_scale = round(float(rng.choice([0.5, 0.75])), 3)
            return c, f"L3.wo head scale={c.wsp_l3_wo_scale}", "Fallback head damping (no G1 gate)"
        c.wsp_l3_g1_nheads = int(rng.choice([1, 2]))
        d = float(np.clip(c.wsp_l3_g1_delta + rng.choice([-0.8, -0.4, 0.3, -1.2]) * scale, -2.2, 0.0))
        if abs(d) < 0.15:
            d = -0.6
        c.wsp_l3_g1_delta = round(d, 3)
        return c, f"L3.bg top-{c.wsp_l3_g1_nheads} d={c.wsp_l3_g1_delta}", "Damp global attention repeat-copy head via G1 bias"
    if fam == "wsp_l3_wo":
        c.wsp_l3_wo_nheads = 1
        c.wsp_l3_wo_scale = round(float(rng.choice([0.0, 0.35, 0.65, 0.85])), 3)
        return c, f"L3.wo top-1 scale={c.wsp_l3_wo_scale}", "Scale diagnosed copy-head output projection"
    if fam == "ssp_weight_gdn2":
        c.ssp_weight_kch = int(rng.choice([32, 64, 96]))
        c.ssp_weight_da = round(float(rng.choice([-0.25, -0.5, -0.75]) * scale), 3)
        c.ssp_weight_de = round(float(rng.choice([0.25, 0.5, 0.8]) * scale), 3)
        return c, f"GDN2 gates k={c.ssp_weight_kch} da={c.ssp_weight_da} de={c.ssp_weight_de}", "Shift recurrent decay/erase biases on echo channels"
    if fam == "ssp_live_gdn2":
        c.ssp_live_damp = round(float(np.clip(c.ssp_live_damp + rng.choice([-0.1, 0.15, 0.25, 0.35]) * scale, 0.1, 0.55)), 3)
        c.ssp_live_ashift = round(float(np.clip(c.ssp_live_ashift + rng.choice([-0.5, -0.3, 0.2]) * scale, -1.4, -0.2)), 3)
        c.ssp_live_eshift = round(float(np.clip(c.ssp_live_eshift + rng.choice([0.5, 0.8, -0.3]) * scale, 0.3, 2.0)), 3)
        return c, f"SSP-Live GDN2 damp={c.ssp_live_damp} a={c.ssp_live_ashift} e={c.ssp_live_eshift}", "Adaptive online state-echo damping on GDN-2 (0 weight drift)"
    if fam == "ssp_live_mla":
        c.ssp_live_mla_bias = round(float(np.clip(c.ssp_live_mla_bias + rng.choice([-1.2, -0.7, -2.0, 0.4]) * scale, -3.5, -0.4)), 3)
        return c, f"SSP-Live MLA repeat_bias={c.ssp_live_mla_bias}", "Adaptive global-attention repeat-key logit bias (0 weight drift)"
    if fam == "harness_ngram":
        c.dec_ngram_pen = round(float(np.clip(c.dec_ngram_pen + rng.choice([1.2, 2.2, 3.2, -0.8]) * scale, 0.8, 6.0)), 3)
        c.dec_pres_pen = round(float(rng.choice([0.0, 0.1, 0.2, 0.3])), 3)
        c.dec_rep_win = int(rng.choice([24, 32, 48]))
        c.dec_echo_ng = int(rng.choice([3, 4]))
        return c, f"NgramGuard pen={c.dec_ngram_pen} pres={c.dec_pres_pen} ng={c.dec_echo_ng}", "Decoding n-gram echo guard & presence damping (0 weight drift)"
    if fam == "wsp_canon":
        c.wsp_canon_scale = round(float(rng.choice([0.5, 0.7, 0.85, 1.0])), 3)
        return c, f"Canon-AC tap_scale={c.wsp_canon_scale}", "Attenuate delayed Canon-AC causal conv taps"
    if fam == "wsp_mhc":
        c.wsp_mhc_delta = round(float(rng.choice([-0.4, 0.4, 0.8, 0.0])), 3)
        return c, f"mHC offdiag_delta={c.wsp_mhc_delta}", "Perturb DeepSeek mHC Birkhoff stream mixing"
    c.comp1_mlp_gain = round(float(rng.choice([0.88, 0.94, 1.04, 1.10])), 3)
    return c, f"COMP1 mlp_gain={c.comp1_mlp_gain}", "Recalibrate global MLP down-projection scale under 1-bit COMP-1 shift"


def propose_candidates(
    h_cur: HybridHarness, cur_ev: Dict[str, Any], sim: SimLedger, env: RealTaskEnv,
    k_cands: int, rng: np.random.Generator, round_idx: int,
) -> List[Tuple[str, HybridHarness, str, str, Dict[str, Any]]]:
    prov = env.provenance(round_idx)
    fstats = sim.family_stats(prov)
    fams = [f for f, _, _ in ACTION_FAMILIES]
    weights = np.ones(len(fams), dtype=np.float64)

    for i, fam in enumerate(fams):
        st = fstats[fam]
        if sim.memory_mode == "none":
            weights[i] = 1.0
        elif sim.memory_mode == "problem_only":
            weights[i] = 1.8 if st["problem_opened"] > 0.5 else 0.7
        else:
            if st["n"] > 0:
                if st["kl_viol"] > 0 or (st["n_fail"] >= 2 and st["n_acc"] == 0):
                    weights[i] = 0.05
                else:
                    gain_term = max(0.1, 1.0 + 0.35 * st["w_gain"])
                    acc_ratio = (st["n_acc"] + 0.5) / (st["n"] + 1.0)
                    kl_safe = math.exp(-15.0 * st["mean_kl"])
                    weights[i] = 0.3 + 4.0 * gain_term * acc_ratio * kl_safe
            else:
                weights[i] = 0.65
            if env.precision_regime == "comp1_1bit" and fam == "comp1_scale_calib":
                weights[i] *= 3.0
            if cur_ev["kl_from_clean"] > 0.03 and fam in ("wsp_l3_wdown", "wsp_l3_g1", "ssp_live_gdn2", "harness_ngram"):
                weights[i] *= 1.8

    probs = weights / np.sum(weights)
    chosen_idx = rng.choice(len(fams), size=min(k_cands, len(fams)), replace=False, p=probs)

    proposals = []
    for idx in chosen_idx:
        fam = fams[int(idx)]
        st = fstats[fam]
        conservative = (sim.memory_mode == "full" and (st["surprise"] > 3.0 or cur_ev["kl_from_clean"] > 0.02))
        cand_h, desc, rat = _mutate_family(h_cur, fam, rng, env, conservative=conservative)
        if sim.memory_mode == "full" and cur_ev["kl_from_clean"] > 0.035:
            cand_h.wsp_canon_scale = 1.0
            cand_h.wsp_mhc_delta = 0.0
            cand_h.wsp_gdn_alpha = 0.0
            cand_h.wsp_gdn_k = 0
        exp_gain = float(st["w_gain"]) if (sim.memory_mode == "full" and st["n"] > 0) else 2.0
        exp_kl = float(st["mean_kl"]) if (sim.memory_mode == "full" and st["n"] > 0) else 0.01
        pred = {
            "expected_score_delta": round(exp_gain, 3),
            "expected_kl": round(exp_kl, 5),
            "confidence": round(min(0.95, 0.3 + 0.15 * st["n"]) if sim.memory_mode == "full" else 0.25, 2),
        }
        proposals.append((fam, cand_h, desc, rat, pred))
    return proposals


def run_crsi_session(
    *, env: RealTaskEnv, h_init: HybridHarness, sim_init: SimLedger,
    n_rounds: int, k_cands: int, seed: int, eval_heldout_each_round: bool = True,
) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    h_cur = copy.deepcopy(h_init)
    sim = sim_init
    cur_ev = env.evaluate(h_cur, split="evolve")
    cur_te = env.evaluate(h_cur, split="heldout") if eval_heldout_each_round else None
    action_counts: Dict[str, int] = {}

    history = [{
        "round": 0,
        "evolve_score": round(cur_ev["score"], 3), "evolve_loop": round(cur_ev["loop_seq_rate"], 4),
        "evolve_kl": round(cur_ev["kl_from_clean"], 6),
        "heldout_score": round(cur_te["score"], 3) if cur_te else None,
        "heldout_loop": round(cur_te["loop_seq_rate"], 4) if cur_te else None,
        "heldout_distinct4": round(cur_te["distinct_4"], 4) if cur_te else None,
        "heldout_delta_ce": round(cur_te["delta_ce"], 5) if cur_te else None,
        "heldout_kl": round(cur_te["kl_from_clean"], 6) if cur_te else None,
        "accepted_action": "init",
    }]

    for r in range(1, n_rounds + 1):
        prov = env.provenance(r)
        props = propose_candidates(h_cur, cur_ev, sim, env, k_cands, rng, r)
        best_cand = None
        best_score = cur_ev["score"]
        evals_round = []

        for ci, (fam, cand_h, desc, rat, pred) in enumerate(props):
            action_counts[fam] = action_counts.get(fam, 0) + 1
            ev_m = env.evaluate(cand_h, split="evolve")
            f_prev, f_new = cur_ev["loop_flags"], ev_m["loop_flags"]
            fixed = sum(1 for a, b in zip(f_prev, f_new) if a == 1 and b == 0)
            missed = sum(1 for a, b in zip(f_prev, f_new) if a == 1 and b == 1)
            regressed = sum(1 for a, b in zip(f_prev, f_new) if a == 0 and b == 1)
            obs = {
                "evolve_score_before": round(cur_ev["score"], 3),
                "evolve_score_after": round(ev_m["score"], 3),
                "score_delta": round(ev_m["score"] - cur_ev["score"], 3),
                "loop_delta": round(ev_m["loop_seq_rate"] - cur_ev["loop_seq_rate"], 4),
                "distinct4_delta": round(ev_m["distinct_4"] - cur_ev["distinct_4"], 4),
                "delta_ce": round(ev_m["delta_ce"], 5),
                "kl_from_clean": round(ev_m["kl_from_clean"], 6),
                "fixed_seqs": fixed, "missed_seqs": missed, "regressed_seqs": regressed,
            }
            kl_ok = ev_m["kl_from_clean"] <= 0.06
            reg_ok = (regressed <= fixed) if sim.memory_mode == "full" else True
            evals_round.append((ci, fam, cand_h, desc, rat, pred, obs, ev_m))
            if kl_ok and reg_ok and ev_m["score"] > best_score + 0.05:
                best_score = ev_m["score"]
                best_cand = ci

        acc_desc = "none"
        for ci, fam, cand_h, desc, rat, pred, obs, ev_m in evals_round:
            is_acc = (best_cand == ci)
            sim.record_attempt(
                action_family=fam, round_idx=r, candidate_id=f"r{r}_c{ci}", accepted=is_acc,
                mutation_desc=desc, rationale=rat, predicted_outcome=pred,
                observed_outcome=obs, provenance=prov,
            )
            if is_acc:
                h_cur = cand_h
                cur_ev = ev_m
                acc_desc = f"{fam}: {desc}"

        if eval_heldout_each_round:
            cur_te = env.evaluate(h_cur, split="heldout")
        history.append({
            "round": r,
            "evolve_score": round(cur_ev["score"], 3), "evolve_loop": round(cur_ev["loop_seq_rate"], 4),
            "evolve_kl": round(cur_ev["kl_from_clean"], 6),
            "heldout_score": round(cur_te["score"], 3) if cur_te else None,
            "heldout_loop": round(cur_te["loop_seq_rate"], 4) if cur_te else None,
            "heldout_distinct4": round(cur_te["distinct_4"], 4) if cur_te else None,
            "heldout_delta_ce": round(cur_te["delta_ce"], 5) if cur_te else None,
            "heldout_kl": round(cur_te["kl_from_clean"], 6) if cur_te else None,
            "accepted_action": acc_desc,
        })

    final_te = cur_te if cur_te is not None else env.evaluate(h_cur, split="heldout")
    return {
        "final_harness": asdict(h_cur),
        "final_harness_obj": h_cur,
        "final_evolve": {k: v for k, v in cur_ev.items() if k != "loop_flags"},
        "final_heldout": {k: v for k, v in final_te.items() if k != "loop_flags"},
        "history": history,
        "action_counts": action_counts,
        "sim_ledger": sim,
    }


def run_4_conditions_transition(
    *, trans_id: str, src_env: RealTaskEnv, tgt_env: RealTaskEnv,
    src_rounds: int, tgt_rounds: int, k_cands: int, seed: int,
    eval_heldout_each_round: bool = True, eval_source_retention: bool = True,
) -> Dict[str, Any]:
    src_run = run_crsi_session(
        env=src_env, h_init=HybridHarness(), sim_init=SimLedger("full"),
        n_rounds=src_rounds, k_cands=k_cands, seed=seed, eval_heldout_each_round=False,
    )
    h_s = src_run["final_harness_obj"]
    m_s = src_run["sim_ledger"]
    src_base_te = src_env.evaluate(HybridHarness(), split="heldout")["score"]
    src_opt_te = src_run["final_heldout"]["score"]

    cond_specs = [
        ("Cold_Start", HybridHarness(), SimLedger("none")),
        ("Harness_Transfer", h_s, SimLedger("none")),
        ("Problem_Only", h_s, m_s.clone_for_transfer("problem_only")),
        ("Full_SIM", h_s, m_s.clone_for_transfer("full")),
    ]

    cond_out: Dict[str, Any] = {}
    for cname, h0, sim0 in cond_specs:
        t_c = time.monotonic()
        res = run_crsi_session(
            env=tgt_env, h_init=h0, sim_init=sim0,
            n_rounds=tgt_rounds, k_cands=k_cands, seed=seed + 500,
            eval_heldout_each_round=eval_heldout_each_round,
        )
        ret_score = None
        if eval_source_retention:
            ret_score = round(src_env.evaluate(res["final_harness_obj"], split="heldout")["score"], 3)
        print(f"    [{trans_id} / {cname:16s}] ({time.monotonic()-t_c:.1f}s) "
              f"r0={res['history'][0]['heldout_score']} -> final={res['final_heldout']['score']:.2f} "
              f"(loop={res['final_heldout']['loop_seq_rate']*100:.0f}%, KL={res['final_heldout']['kl_from_clean']:.4f}, "
              f"ret={ret_score})", flush=True)
        cond_out[cname] = {
            "round0_heldout_score": res["history"][0]["heldout_score"] if eval_heldout_each_round
                                    else round(tgt_env.evaluate(h0, split="heldout")["score"], 3),
            "final_heldout": res["final_heldout"],
            "source_retention_heldout": ret_score,
            "history": res["history"],
            "action_counts": res["action_counts"],
            "final_harness": res["final_harness"],
            "sim_summary": {
                "n_problems": len(res["sim_ledger"].problems),
                "n_attempts": sum(len(p["attempts"]) for p in res["sim_ledger"].problems.values()),
            },
            "sim_ledger_dict": res["sim_ledger"].to_dict() if cname == "Full_SIM" else None,
        }
    return {
        "transition_id": trans_id,
        "source": src_env.name, "target": tgt_env.name,
        "source_heldout_init": round(src_base_te, 3),
        "source_heldout_opt": round(src_opt_te, 3),
        "conditions": cond_out,
    }


def load_2m_ckpt(arch_lower: str) -> Tuple[Abi6Model, np.ndarray]:
    meta = json.loads((CKPT_DIR / f"{arch_lower}_bcirq8.json").read_text(encoding="utf-8"))
    sp = spec_from_dict(meta["spec"], batch=4, time_len=128, threads=2)
    m = Abi6Model(sp, make_policy(opt_kind=OPT_Q8_MUON_BF16_GRAD))
    w = decode_bcirq8_v1_per_tensor(CKPT_DIR / meta["weights_file"], m.tensor_infos(), m.n_params)
    m.set_weights(w)
    return m, w


def paired_stats(diffs: List[float]) -> Dict[str, Any]:
    arr = np.asarray(diffs, dtype=np.float64)
    n = len(arr)
    mean = float(np.mean(arr))
    sd_val = float(np.std(arr, ddof=1)) if n > 1 else 0.0
    se = sd_val / math.sqrt(n) if n > 0 else 0.0
    t_stat = mean / se if se > 1e-12 else 0.0
    dz = mean / sd_val if sd_val > 1e-12 else 0.0
    ci_lo = mean - 2.262157 * se
    ci_hi = mean + 2.262157 * se
    df = n - 1
    if abs(t_stat) > 1e-12 and df > 0:
        coeff = math.gamma((df + 1.0) / 2.0) / (math.sqrt(df * math.pi) * math.gamma(df / 2.0))
        steps = 2000
        h_step = abs(t_stat) / steps
        acc = 0.0
        for k in range(steps + 1):
            x = k * h_step
            w = 1.0 if (k == 0 or k == steps) else (4.0 if k % 2 == 1 else 2.0)
            acc += w * ((1.0 + x * x / df) ** (-(df + 1.0) / 2.0))
        half_area = coeff * acc * h_step / 3.0
        p_two = max(0.0, min(1.0, 1.0 - 2.0 * half_area))
    else:
        p_two = 1.0
    wins = int(np.sum(arr > 0.0))
    ties = int(np.sum(arr == 0.0))
    return {
        "n_pairs": n, "wins": wins, "ties": ties, "losses": n - wins - ties,
        "mean_diff": round(mean, 3), "std_diff": round(sd_val, 3),
        "ci95_low": round(ci_lo, 3), "ci95_high": round(ci_hi, 3),
        "t_stat": round(t_stat, 3), "p_two_sided": round(p_two, 5), "cohens_dz": round(dz, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=str, default="all",
                    choices=["all", "T1a", "T1b", "T2a", "T2b", "T3", "S10a", "S10b", "finalize"])
    args = ap.parse_args()

    t0 = time.monotonic()
    partial_p = RESULTS_DIR / "stage3_partial.json"
    cache = json.loads(partial_p.read_text(encoding="utf-8")) if partial_p.exists() else {"transitions": {}, "s10": {}}

    val_stream = load_wikitext_bytes("val")
    test_stream = load_wikitext_bytes("test")

    # Right-sized strict disjoint splits (val=evolve, test=heldout)
    d1_ev_p = extract_domain_prompts(val_stream, "prose_unloop", n_prompts=6, prompt_len=40, seed=101)
    d1_te_p = extract_domain_prompts(test_stream, "prose_unloop", n_prompts=12, prompt_len=40, seed=202)
    d2_ev_p = extract_domain_prompts(val_stream, "structured_heading", n_prompts=6, prompt_len=40, seed=103)
    d2_te_p = extract_domain_prompts(test_stream, "structured_heading", n_prompts=12, prompt_len=40, seed=204)
    d3_ev_p = extract_domain_prompts(val_stream, "longctx_echo", n_prompts=6, prompt_len=96, seed=105)
    d3_te_p = extract_domain_prompts(test_stream, "longctx_echo", n_prompts=10, prompt_len=96, seed=206)

    ev_xs, ev_ys = make_eval_batches(val_stream, batch=4, seq_len=128, n_batches=3, seed=301)
    te_xs, te_ys = make_eval_batches(test_stream, batch=4, seq_len=128, n_batches=6, seed=401)

    need_10m = args.only in ("all", "T1a", "T1b", "T2a", "T2b", "T3")
    need_2m_a7 = args.only in ("all", "T2a", "S10a", "S10b")
    need_2m_gqa = args.only in ("all", "T2b")

    envs: Dict[str, RealTaskEnv] = {}
    models_to_close: List[Abi6Model] = []

    if need_10m:
        m10, w10_q8, _ = load_arch7d_10m_tracked(batch=4, time_len=128, threads=2)
        models_to_close.append(m10)
        envs["10m_d1"] = RealTaskEnv(
            name="Arch7d_10M_D1_Prose", domain="prose_unloop", policy_model="BCIR-Arch7d-10M",
            precision_regime="bcirq8_v1", model=m10, w_base=w10_q8,
            evolve_prompts=d1_ev_p, heldout_prompts=d1_te_p, gen_len=64,
            evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
        )
        if args.only in ("all", "T1a"):
            envs["10m_d2"] = RealTaskEnv(
                name="Arch7d_10M_D2_Structured", domain="structured_heading", policy_model="BCIR-Arch7d-10M",
                precision_regime="bcirq8_v1", model=m10, w_base=w10_q8,
                evolve_prompts=d2_ev_p, heldout_prompts=d2_te_p, gen_len=64,
                evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
            )
        if args.only in ("all", "T1b"):
            envs["10m_d3"] = RealTaskEnv(
                name="Arch7d_10M_D3_LongCtxEcho", domain="longctx_echo", policy_model="BCIR-Arch7d-10M",
                precision_regime="bcirq8_v1", model=m10, w_base=w10_q8,
                evolve_prompts=d3_ev_p, heldout_prompts=d3_te_p, gen_len=64,
                evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
            )
        if args.only in ("all", "T3"):
            w10_c1 = build_comp1_weights(m10, w10_q8, rank=16, fp16_scales=True)
            envs["10m_c1"] = RealTaskEnv(
                name="Arch7d_10M_COMP1_1Bit", domain="prose_unloop", policy_model="BCIR-Arch7d-10M",
                precision_regime="comp1_1bit", model=m10, w_base=w10_c1,
                evolve_prompts=d1_ev_p, heldout_prompts=d1_te_p, gen_len=64,
                evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
            )

    if need_2m_a7:
        m2_a7, w2_a7 = load_2m_ckpt("arch7d_2m")
        models_to_close.append(m2_a7)
        envs["2m_a7_d1"] = RealTaskEnv(
            name="Arch7d_2M_D1_Prose", domain="prose_unloop", policy_model="Arch7d-2M",
            precision_regime="bcirq8_v1", model=m2_a7, w_base=w2_a7,
            evolve_prompts=d1_ev_p, heldout_prompts=d1_te_p, gen_len=64,
            evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
        )
        if args.only in ("all", "S10a", "S10b"):
            envs["2m_a7_d2"] = RealTaskEnv(
                name="Arch7d_2M_D2_Structured", domain="structured_heading", policy_model="Arch7d-2M",
                precision_regime="bcirq8_v1", model=m2_a7, w_base=w2_a7,
                evolve_prompts=d2_ev_p, heldout_prompts=d2_te_p, gen_len=64,
                evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
            )

    if need_2m_gqa:
        m2_gqa, w2_gqa = load_2m_ckpt("gqa_full_2m")
        models_to_close.append(m2_gqa)
        envs["2m_gqa_d1"] = RealTaskEnv(
            name="GQA_Full_2M_D1_Prose", domain="prose_unloop", policy_model="GQA-Full-2M",
            precision_regime="bcirq8_v1", model=m2_gqa, w_base=w2_gqa,
            evolve_prompts=d1_ev_p, heldout_prompts=d1_te_p, gen_len=64,
            evolve_xs=ev_xs, evolve_ys=ev_ys, heldout_xs=te_xs, heldout_ys=te_ys,
        )

    t_specs = {
        "T1a": ("T1a_CrossDataset_10M_Prose_to_Structured", "10m_d1", "10m_d2", 4, 4, 3, 20261),
        "T1b": ("T1b_CrossDataset_10M_Prose_to_LongCtx", "10m_d1", "10m_d3", 4, 4, 3, 20262),
        "T2a": ("T2a_CrossScale_2M_to_10M_Arch7d", "2m_a7_d1", "10m_d1", 5, 4, 3, 20263),
        "T2b": ("T2b_CrossFamily_GQA2M_to_Arch7d10M", "2m_gqa_d1", "10m_d1", 5, 4, 3, 20264),
        "T3":  ("T3_CrossPrecision_Q8_to_COMP1_10M", "10m_d1", "10m_c1", 4, 4, 3, 20265),
    }

    for key, (tid, sk, tk, sr, tr, kc, sd) in t_specs.items():
        if args.only not in ("all", key):
            continue
        t_tr = time.monotonic()
        print(f"--- Running {tid} ({envs[sk].name} -> {envs[tk].name}) ---", flush=True)
        res = run_4_conditions_transition(
            trans_id=tid, src_env=envs[sk], tgt_env=envs[tk],
            src_rounds=sr, tgt_rounds=tr, k_cands=kc, seed=sd,
            eval_heldout_each_round=True, eval_source_retention=True,
        )
        full_ledger = res["conditions"]["Full_SIM"].pop("sim_ledger_dict")
        write_json(RESULTS_DIR / f"sim_ledger_{tid}.json", full_ledger)
        cache["transitions"][tid] = res
        write_json(partial_p, cache)
        c_sim = res["conditions"]["Full_SIM"]["final_heldout"]["score"]
        c_tr = res["conditions"]["Harness_Transfer"]["final_heldout"]["score"]
        c_po = res["conditions"]["Problem_Only"]["final_heldout"]["score"]
        c_cs = res["conditions"]["Cold_Start"]["final_heldout"]["score"]
        print(f"  [{tid}] ({time.monotonic()-t_tr:.1f}s) Cold={c_cs:.2f} | Transfer={c_tr:.2f} | "
              f"ProbOnly={c_po:.2f} | Full_SIM={c_sim:.2f} (SIM-Tr={c_sim-c_tr:+.2f})", flush=True)

    if args.only in ("all", "S10a", "S10b"):
        idx_range = range(0, 5) if args.only == "S10a" else (range(5, 10) if args.only == "S10b" else range(0, 10))
        for i in idx_range:
            sd = 31000 + i * 17
            t_p = time.monotonic()
            res = run_4_conditions_transition(
                trans_id=f"S10_seed{i}", src_env=envs["2m_a7_d1"], tgt_env=envs["2m_a7_d2"],
                src_rounds=5, tgt_rounds=4, k_cands=3, seed=sd,
                eval_heldout_each_round=False, eval_source_retention=True,
            )
            sc_cs = res["conditions"]["Cold_Start"]["final_heldout"]["score"]
            sc_tr = res["conditions"]["Harness_Transfer"]["final_heldout"]["score"]
            sc_po = res["conditions"]["Problem_Only"]["final_heldout"]["score"]
            sc_sm = res["conditions"]["Full_SIM"]["final_heldout"]["score"]
            rt_tr = res["conditions"]["Harness_Transfer"]["source_retention_heldout"]
            rt_sm = res["conditions"]["Full_SIM"]["source_retention_heldout"]
            row = {
                "pair_idx": i, "seed": sd, "source_opt_score": res["source_heldout_opt"],
                "cold_start": round(sc_cs, 3), "harness_transfer": round(sc_tr, 3),
                "problem_only": round(sc_po, 3), "full_sim": round(sc_sm, 3),
                "diff_sim_minus_transfer": round(sc_sm - sc_tr, 3),
                "diff_sim_minus_probonly": round(sc_sm - sc_po, 3),
                "retention_transfer": rt_tr, "retention_full_sim": rt_sm,
                "action_counts_transfer": res["conditions"]["Harness_Transfer"]["action_counts"],
                "action_counts_full_sim": res["conditions"]["Full_SIM"]["action_counts"],
            }
            cache["s10"][str(i)] = row
            write_json(partial_p, cache)
            print(f"  [S10 pair {i}] ({time.monotonic()-t_p:.1f}s) Cold={sc_cs:.2f} Tr={sc_tr:.2f} "
                  f"PO={sc_po:.2f} SIM={sc_sm:.2f} (d={sc_sm-sc_tr:+.2f}) | ret SIM={rt_sm:.2f} vs Tr={rt_tr:.2f}", flush=True)

    # Finalize once all 5 transitions + all 10 paired seeds are complete (or on --only finalize)
    if args.only in ("all", "finalize") or (len(cache["transitions"]) == 5 and len(cache["s10"]) == 10):
        if len(cache["transitions"]) == 5 and len(cache["s10"]) == 10:
            summary_rows: List[Dict[str, Any]] = []
            round_curve_rows: List[Dict[str, Any]] = []
            for tid in sorted(cache["transitions"].keys()):
                res = cache["transitions"][tid]
                for cname in ("Cold_Start", "Harness_Transfer", "Problem_Only", "Full_SIM"):
                    cd = res["conditions"][cname]
                    fh = cd["final_heldout"]
                    summary_rows.append({
                        "transition_id": tid, "condition": cname,
                        "round0_score": cd["round0_heldout_score"],
                        "final_score": round(fh["score"], 3),
                        "final_loop_rate": round(fh["loop_seq_rate"], 4),
                        "final_distinct4": round(fh["distinct_4"], 4),
                        "final_delta_ce": round(fh["delta_ce"], 5),
                        "final_kl": round(fh["kl_from_clean"], 6),
                        "source_retention": cd["source_retention_heldout"],
                        "sim_attempts": cd["sim_summary"]["n_attempts"],
                    })
                    for hrow in cd["history"]:
                        round_curve_rows.append({
                            "transition_id": tid, "condition": cname,
                            "round": hrow["round"], "evolve_score": hrow["evolve_score"],
                            "heldout_score": hrow["heldout_score"],
                            "heldout_loop": hrow["heldout_loop"], "heldout_kl": hrow["heldout_kl"],
                        })

            paired_rows = [cache["s10"][str(i)] for i in range(10)]
            s10_stats = {
                "full_sim_vs_harness_transfer": paired_stats([r["full_sim"] - r["harness_transfer"] for r in paired_rows]),
                "full_sim_vs_problem_only": paired_stats([r["full_sim"] - r["problem_only"] for r in paired_rows]),
                "full_sim_vs_cold_start": paired_stats([r["full_sim"] - r["cold_start"] for r in paired_rows]),
                "mean_cold_start": round(float(np.mean([r["cold_start"] for r in paired_rows])), 3),
                "mean_harness_transfer": round(float(np.mean([r["harness_transfer"] for r in paired_rows])), 3),
                "mean_problem_only": round(float(np.mean([r["problem_only"] for r in paired_rows])), 3),
                "mean_full_sim": round(float(np.mean([r["full_sim"] for r in paired_rows])), 3),
                "mean_retention_transfer": round(float(np.mean([r["retention_transfer"] for r in paired_rows])), 3),
                "mean_retention_full_sim": round(float(np.mean([r["retention_full_sim"] for r in paired_rows])), 3),
            }
            csv_paired = [{k: v for k, v in r.items() if not k.startswith("action_counts_")} for r in paired_rows]
            write_json(RESULTS_DIR / "stage3_sim_crsi_experiments.json", {
                "schema": "bcir.sim.stage3_crsi_experiments.v1",
                "provenance": get_host_provenance(),
                "transitions_table": summary_rows,
                "transitions_detail": cache["transitions"],
                "paired_10seed_stats": s10_stats,
                "paired_10seed_rows": paired_rows,
            })
            write_csv(RESULTS_DIR / "stage3_sim_transitions.csv", summary_rows)
            write_csv(RESULTS_DIR / "stage3_sim_round_curves.csv", round_curve_rows)
            write_csv(RESULTS_DIR / "stage3_sim_10seed_paired.csv", csv_paired)
            if partial_p.exists():
                partial_p.unlink()
            print(f"\n=== Stage 3 FINALIZED! S10 SIM vs Transfer: {s10_stats['full_sim_vs_harness_transfer']} ===", flush=True)

    for e in envs.values():
        e.close()
    for m in models_to_close:
        m.close()
    print(f"=== Stage 3 ({args.only}) finished in {time.monotonic() - t0:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
