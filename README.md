# Trustworthy Perception in Adverse Weather

Hackathon project demonstrating that signal-level preprocessing +
temporal consistency filtering produces measurably more stable, accurate
object detections from camera and LiDAR data in adverse weather, compared
to raw pretrained-model output with no preprocessing.

**Camera pipeline**: real rain images (ACDC dataset) + real detection
ground truth.
**LiDAR pipeline**: real snow point clouds (CADC dataset) + real 3D
annotations.

See `CLAUDE.md` for full project context, the reasoning behind the
camera=rain / LiDAR=snow dataset split, tech stack, and build order.

## Current status (2026-09-26)

- [x] ACDC camera data: `rgb_anon_trainvaltest.zip` downloaded, rain
      subset extracted to `data/processed/rgb_anon/rain/`
- [x] ACDC detection labels: `gt_detection/` extracted (real COCO-format
      boxes, not segmentation masks)
- [ ] CADC LiDAR data: not yet downloaded
- [ ] Camera pipeline: not yet built
- [ ] LiDAR pipeline: not yet built

## Setup (on the remote GPU machine, over SSH)

1. Verify CUDA works:
   ```
   python scripts/sanity_check_cuda.py
   ```

2. Install base dependencies:
   ```
   pip install -r requirements.txt
   ```
   (Install PyTorch separately first, matched to your CUDA toolkit
   version.)

### Camera data (ACDC) — already done, documented here for reference

1. Register at https://acdc.vision.ee.ethz.ch/register, accept terms,
   request `rgb_anon_trainvaltest.zip` and `gt_detection_trainval.zip`
   at the packages page.
2. Download with `wget -c` rather than a browser for the 15.6 GB file —
   browser downloads of large single files have failed silently before
   (empty 0-byte result) in this project.
3. Extract ONLY the rain subset (this is a normal zip, not split, so
   selective extraction works):
   ```
   unzip rgb_anon_trainvaltest.zip 'rgb_anon/rain/*' -d data/processed/
   rm rgb_anon_trainvaltest.zip   # reclaim ~15.6 GB
   ```
4. Extract labels:
   ```
   unzip gt_detection_trainval.zip -d data/gt_detection/
   ```

### LiDAR data (CADC) — not yet done

CADC has no login gate and is organized per-sequence (not one bulk
archive), so check size before downloading:
```
curl -sI "http://wiselab.uwaterloo.ca/cadcd_data/2019_02_27/0002/labeled.zip" | grep -i content-length
```
Then download 1-2 sequences with `wget`. See `CLAUDE.md` for the full
URL pattern and available dates/sequences.

## Repo layout

See the "Repo structure" section in `CLAUDE.md`.

## Working with Claude Code on this project

This repo is set up so Claude Code can pick up work module-by-module
using `CLAUDE.md` as its context. See the project's chat history / your
own notes for the recommended workflow: give it one scoped module at a
time (e.g. "write data_loader/acdc_camera.py per the CLAUDE.md spec"),
have it write a smoke test, commit once that passes, then move to the
next build-order step.
