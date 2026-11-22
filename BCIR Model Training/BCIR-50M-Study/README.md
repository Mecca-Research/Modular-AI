# BCIR 50M scaling and weight-space experiments — 2026-11-04

This is an isolated research workbench for merged PR #822, source commit
`80e9507531b7597ef81d0348db75f0fd5dcd8134`. It imports BCIR from the sibling
`BCIR` directory. It does not modify the repository or run the older research
folders' retained binaries. Native forward, backward, loss and AdamW execute
in freshly built BCIR C. Python supplies data and records results.

Read the generated `BCIR_50M_Weight_Space_Report_2026-11-04.md` first. The
protocols, per-update events, validation histories, final tests, edit controls,
source hashes and measured host configuration are retained under this folder.
These are small-token early-learning experiments, not converged pretraining.

## Fresh reproduction

Use a new work directory rather than overwriting the retained results. Place
this study's Python scripts there and clone the repository beside it:

```bash
git clone https://github.com/Mecca-Research/Binary-Correspondence-Intermediate-Representation.git BCIR
git -C BCIR checkout 80e9507531b7597ef81d0348db75f0fd5dcd8134
python -m venv study-env
study-env/bin/python -m pip install numpy==2.3.5 pyarrow==26.0.1 matplotlib==3.10.8 Markdown==3.11.2
cd bcir-study-2026-11-04
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
../study-env/bin/python benchmark.py
../study-env/bin/python validate.py
../study-env/bin/python train.py --freeze
../study-env/bin/python -u train.py --ladder
../study-env/bin/python -u extend_architectures.py
../study-env/bin/python -u execution_check.py
../study-env/bin/python -u weight_space.py
../study-env/bin/python report.py
../study-env/bin/python audit.py
```

Run heavy stages sequentially. The experiments use two native threads; BLAS
inside NumPy uses one. No PyTorch package, GPU, OpenAI API key or external model
service is required. A complete fresh run takes tens of minutes on the measured
CPU; scheduling and compiler/libm differences can change time and floating-point
results. The original per-file and per-array hashes identify the recorded run.

`benchmark.py` builds the current trainer with GCC through `NativeDecoder.build`
and measures five updates after two warmups per shape. Those short timings
establish feasibility, not a statistically resolved hardware speedup. `validate.py`
checks a separate scalar forward implementation, sampled finite differences,
causality, exact thread state, checkpoint resume and capacity refusal. Full
repository CI and latest-toolchain/device qualification are outside this research
execution; no repository patch was published.

`train.py --ladder` trains the pilot and two 10M candidates for both seeds. It
screens two 50M candidates at 200 updates, finishes the selected model to 600,
then trains the second seed. All completed runs use 153,600 supervised bytes.
The initial screen was ambiguous, so `extension-protocol.json` declares a
pre-test extension: both 50M recipes complete 600 updates on both seeds; the
lower mean validation CE selects the final candidate. Its timing and per-seed
differences remain descriptive. Preserve this extension when reproducing.
Runs resume only at saved checkpoint boundaries. Existing completed result files
are deliberately reused; remove neither archived results nor protocols for a
fresh study—create a new directory instead.

`weight_space.py` freezes its protocol before any test evaluation. It separately
tests exact neural arithmetic compilation, norm-based repair of a planted fault,
neuron-coordinate permutations, and validation-selected edits to clean models.
The program is authored by this assistant; it is not a reproduction of the
article's independent Astra trials. Fault repair never reads the clean reference
inside the diagnosis function. Test outcomes cannot select an edit.

## Checkpoints

The workbench archive contains small circuit/patch checkpoints. Large language
model states are separate archives. Each 50M state has `weights.zip` and
`optimizer.zip` parts. Extract both parts into the same empty directory, then:

```bash
python restore_checkpoint.py --parts /absolute/path/to/extracted-parts --output restored.npz
```

The tool checks all array hashes before rebuilding the study's full-state NPZ.
This retains the optimizer step, both moments, beta powers, model specification
and numerical contract. `common.restore` restores these arrays into the current
native trainer. The archive of smaller model states contains their original
selected full-state NPZ files. `artifacts.json` lists sizes, hashes and state
identities; use it to avoid mixing seeds or model families.

No pickle is used. `np.load(..., allow_pickle=False)` is required throughout.
The study checkpoint format is a research wrapper, distinct from BCIR's normal
native checkpoint wire format. Tiny-model full-state resume and the selected
50M candidate's actual 200→600 continuation exercise its use.

## Data and attribution

Input: unchanged retained WikiText-2 raw train/validation/test parquets from
`docs/research/gp-global/data` at the pinned repository commit. Join each split's
text rows with a newline and encode UTF-8 bytes as IDs 0–255, with a 260-token
vocabulary matching the earlier studies. Split hashes and encoded-array hashes
are recorded in `results/data.json`. Samples are deterministic and paired across
architectures. Validation selects checkpoints; test is never training data.

WikiText-2: Merity, Xiong, Bradbury and Socher, and Wikipedia contributors;
dataset card: https://huggingface.co/datasets/Salesforce/wikitext . Retain the
CC BY-SA 3.0/GFDL attribution and the repository's source-data provenance. The
workbench does not redistribute those already repository-backed input files.
BCIR retains its original noncommercial license; obtain and follow `BCIR/LICENSE`
when fetching its source. This workbench does not relicense BCIR.

Primary research context: https://rikiyat.github.io/blog/weight-space-programming/
and https://github.com/RikiyaT/lm_understand-logs . Related prior mechanisms:
https://arxiv.org/abs/2301.05062 and https://arxiv.org/abs/2202.05262 .
