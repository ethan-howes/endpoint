# Road segmentation from video with Mask2Former

Runs a pretrained [Mask2Former](https://huggingface.co/docs/transformers/main/en/model_doc/mask2former)
semantic-segmentation checkpoint over a video and writes an overlay video showing
the road mask, for visual QC.

Everything is off-the-shelf inference. No training, no custom weights.

## Setup

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Verified on this box: Python 3.14.4, torch 2.14.0+cu130, torchvision 0.29.0,
transformers 5.17.0, RTX 5060 Ti (`sm_120`). All five dependencies ship
Python 3.14 wheels; `opencv-python` publishes only `cp37-abi3` wheels, which
still install on 3.14 because abi3 is a stable ABI.

`scipy` is not optional despite being used only for post-processing:
transformers constructs `Mask2FormerLoss` inside the model constructor, so the
import check fires even for pure inference.

## Self-test first

No dashcam footage is needed to validate the install:

```bash
.venv/bin/python scripts/self_test.py
```

This pulls a few license-clean forward-facing dashcam stills from Wikimedia
Commons, assembles a synthetic clip, runs the real pipeline over it with both
candidate checkpoints, and writes a side-by-side comparison grid plus a
coverage report. Credits for the downloaded frames land in
`assets/samples/CREDITS.md`.

## Usage

```bash
# inspect a clip before committing compute to it
.venv/bin/python scripts/probe_video.py dashcam.mp4

# default: Mapillary Vistas swin-large, green road tint, same fps/resolution
.venv/bin/python scripts/run_segmentation.py -i dashcam.mp4

# side-by-side original | mask, and count bike lanes as road
.venv/bin/python scripts/run_segmentation.py -i dashcam.mp4 \
    --layout side-by-side --include-extra-road-classes

# cheaper pass: half the frames, lower input resolution
.venv/bin/python scripts/run_segmentation.py -i dashcam.mp4 \
    --stride 2 --shortest-edge 600

.venv/bin/python scripts/run_segmentation.py --list-presets
.venv/bin/python scripts/run_segmentation.py --list-classes mapillary
```

Output defaults to `results/videos/<input>_road.mp4`.

## Model choice

| Preset | Checkpoint | Labels | Road classes |
|---|---|---|---|
| `mapillary` (default) | `mask2former-swin-large-mapillary-vistas-semantic` | 65 | `Road`(13), `Service Lane`(14), optional `Bike Lane`(7) |
| `cityscapes` | `mask2former-swin-large-cityscapes-semantic` | 19 | `road`(0) |
| `ade` | `mask2former-swin-large-ade-semantic` | 150 | `road`(6), optional `sidewalk`(11) |

Mapillary Vistas is street-level driving imagery, so it is the closest domain
match to a dashcam and its label set separates Road / Service Lane / Bike Lane /
Curb, which is what "drivable surface" actually means on the road. Cityscapes
is cleaner and more reliably validated on cluttered urban road. On the
self-test frames the two agree within 1-2 points of road coverage, so pick
either and move on; `ade` is there for footage the driving sets never saw
(gravel, dirt, odd viewpoints).

Any `--model org/name` repo id also works if you want a different checkpoint.

## How a road pixel is decided

1. Mask2Former predicts a class id per pixel. A pixel is road when that
   argmax lands on one of the configured road classes.
2. Optionally gated on `--min-confidence`, the best score among the road
   classes. Raise it to suppress speckle along road edges and distant
   horizons; the mask only ever shrinks as you raise the threshold.

Class names are resolved case-insensitively against the model's **top-level**
`config.id2label`. That distinction matters: `config.backbone_config.id2label`
is the Swin backbone's ImageNet-1k head, 1000 entries wide, and contains a
decoy `"all-terrain bike, off-roader"` at index 671. Reading the wrong one
silently yields wrong class ids. `RoadSegmenter` reads only the top-level map
and `resolve_road_ids` reports any requested name the checkpoint lacks.

## Temporal smoothing

`--smooth ALPHA` applies a pixel-space exponential moving average across
frames. It is **off by default**, and the reason is worth stating plainly: it
has no motion compensation, so under a forward-moving camera it lags and
smears fast-changing geometry. It makes the overlay look calmer while making
the mask less truthful, which is the wrong trade for QC. If you turn it on to
judge flicker, judge the unsmoothed output before drawing conclusions.

## Throughput

About 12-14 processed fps at 1280x720 with swin-large on this GPU, so a
30 fps clip runs at roughly 0.4x real time. `--stride` cuts frames
proportionally. Prefer a smaller checkpoint or lower `--shortest-edge` over a
large stride: stride above 2 makes the output visibly choppy, whereas input
resolution degrades the mask smoothly.

Output fps is divided by `--stride` so a strided run still plays at wall-clock
speed instead of fast-forwarding.

## Layout of the code

```
roadseg/
  config.py      model presets, road class sets, name -> id resolution
  segmenter.py   Mask2Former wrapper; frame(s) -> RoadMask (mask + confidence)
  video_io.py    probe, frame iteration, mp4 writer
  overlay.py     overlay / side-by-side / both renderers
  pipeline.py    orchestration, temporal smoothing, result reporting
scripts/
  probe_video.py       inspect geometry and timing
  run_segmentation.py  main CLI
  self_test.py         end-to-end test with no footage required
```

Each module documents its input/output contract in its top docstring, per the
convention from the previous project in this repo.

## Known quirks

- **transformers load report.** Loading any Mask2Former checkpoint under
  transformers 5.17 reports `swin.layernorm.{weight,bias}` as `MISSING` and
  `relative_position_index` as `UNEXPECTED`. The checkpoints predate the
  backbone's final layernorm, so transformers newly initialises it. Verified
  not to matter: on a dashcam frame the road occupies the lower 84% of the
  frame in a single connected component, which is correct forward-facing
  geometry. Safe to ignore.
- **First run downloads ~1.7 GB** per swin-large checkpoint.
- Sample images are smoke-test inputs only, not training data.
