# AGENTS.md

Zero-shot Mask2Former road/non-road segmentation over video. Off-the-shelf
inference only, no training. Default preset `mapillary` (swin-large).
Repo-level facts (venv, git, history) are in the root `AGENTS.md`.

## Verify before claiming done
There is no test suite: no pytest, no `tests/`, no linter, no typechecker.
Verification is `scripts/self_test.py` plus inline assertions. The interpreter
lives at the repo root, four levels up:

    /home/yart/projects/endpoint/.venv/bin/python -m compileall -q roadseg scripts
    /home/yart/projects/endpoint/.venv/bin/python scripts/self_test.py   # ~2 min, needs network

`self_test.py` fetches stills from Wikimedia (retries on 429) and loads two
checkpoints. Both are already in `~/.cache/huggingface/hub` (~2 GB), so a rerun
is offline-cheap. For pure-logic changes skip it and assert inline instead:

    /home/yart/projects/endpoint/.venv/bin/python -c "from roadseg.config import resolve_road_ids; print(resolve_road_ids({13:'Road'}, ['road']))"
    # -> ((13,), [])

`scripts/run_segmentation.py --list-presets` is free; `--list-classes PRESET`
loads a model to read the label set.

## Environment
- Run everything from this directory. The venv has no pip; install with
  `uv pip install --python /home/yart/projects/endpoint/.venv/bin/python -r requirements.txt`.
- `requirements.txt` is deliberately unpinned. torch/torchvision must stay
  resolver-paired and transformers 5.x needs `torchvision.transforms.v2`.
  Pinning by hand breaks the install; do not "fix" it.
- Verified versions: Python 3.14.4, torch 2.14.0+cu130, torchvision 0.29.0,
  transformers 5.17.0, numpy 2.5.3, scipy 1.18.1. Python 3.14 works only
  because opencv-python-headless ships `cp37-abi3` wheels (stable ABI, valid
  despite having no cp314 tag).
- GPU is RTX 5060 Ti 16GB (`sm_120`, capability 12.0); fp16 autocast on CUDA is
  the default path. `scripts/probe_video.py` prints an ETA from a hardcoded
  ~8 fps — a rough guess, not a measurement.
- `scipy` is a hard requirement though nothing imports it directly: transformers
  constructs `Mask2FormerLoss` inside the model constructor, so the check fires
  during pure inference.

## Traps that fail silently
- **Label set.** Resolve road classes against top-level `config.id2label` only.
  `config.backbone_config.id2label` is the Swin ImageNet-1k head: 1000 entries
  including a decoy `"all-terrain bike, off-roader"` at index 671. Reading it
  yields wrong class ids with no error. See `RoadSegmenter._top_level_id2label`.
- **Load report.** Every load prints `swin.layernorm.{weight,bias}` MISSING and
  `relative_position_index` UNEXPECTED. The checkpoints predate the backbone's
  final layernorm; verified harmless. Do not "fix" it by downgrading
  transformers.
- **`--model` accepts a raw repo id.** `run()` must construct the segmenter from
  `resolve_model(cfg.model)`, never from `RoadSegmenter.from_preset(cfg.model)`.
  `from_preset` re-looks-up the name as a PRESETS key and raises `KeyError` for
  any real `org/name`, so `--model org/name` passes CLI validation and then dies
  before the model loads.
- **Default `-o` is CWD-relative.** With no `--output`, `run_segmentation.py`
  writes `results/videos/<stem>_road.mp4` relative to the shell's cwd, not to
  the script. Launching from elsewhere scatters stray `results/` trees (the
  stale paths inside `results/selftest_report.json` are from exactly that).
  `self_test.py` is the opposite: its paths are `__file__`-relative.
- **Nothing here is actually gitignored.** The root `.gitignore` rules are
  anchored at the repo root while these files live under `src/backend/...`, so
  `git check-ignore` matches no path: the sample JPEGs, the self-test MP4s, and
  a 14 MB `selftest_compare.png` are all tracked. Stage source files explicitly;
  never `git add -A` and never force-add regenerated binaries.
- **`--smooth` is off by default on purpose.** It is an unmotion-compensated
  pixel EMA, so under a forward-moving camera it lags and smears. It makes the
  overlay calmer while making the mask less true. Never enable it as a fix.
- **stride divides output fps** (`source.fps / stride`) so strided output still
  plays at wall-clock speed. That is intentional; do not "correct" it to match
  the source fps.

## Performance
~10-15 processed fps at 1280x720 with swin-large, so roughly 0.3-0.5x real time
against a 30fps source. A 60s 30fps clip (1800 frames) takes ~2-3 min. Use
`tmux` for long runs so an SSH disconnect cannot kill them. Prefer
`--shortest-edge` over a high `--stride`: stride > 2 looks choppy while lower
resolution degrades smoothly.

## State of validation
`self_test.py` defines five CC-licensed dashcam stills and fetches three by
default. Coverage is only measured on those stills plus a synthetic clip built
from them. The committed `results/selftest_report.json` was generated before
the code moved under `src/`, so its `clip` path is dead; regenerate rather than
quote it. No real dashcam video has been run through this; footage is not on the
machine yet. Do not imply real-footage quality is verified.

## Conventions (carried from this repo's prior CLAUDE.md)
- Every module documents its input/output contract in a top-of-file docstring;
  keep modules independently testable.
- After editing a module, smoke-test it on one sample before moving on, then
  commit.
