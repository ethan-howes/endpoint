# Project: Trustworthy Perception in Adverse Weather (Hackathon)

## Goal
Demonstrate that signal-level preprocessing + temporal consistency filtering
produces measurably more stable, accurate object detections from camera and
LiDAR data in adverse weather (fog/rain), compared to raw pretrained-model
output with no preprocessing.

Motivating context: Waymo is launching in Singapore (2028 target), where
monsoon rain and haze are explicitly flagged by Waymo as adaptation
challenges. This project targets the "sensor data reliability in adverse
conditions" problem in Waymo's perception stack. This connects to my
background at the UF TEA Lab (https://tea.ece.ufl.edu/), which focuses on
trustworthy sensing in adverse environments.

## Scope
- Camera pipeline and LiDAR pipeline are developed and evaluated SEPARATELY.
  No sensor fusion / cross-modal projection in this version.
- For each modality: raw baseline -> add preprocessing -> add temporal
  consistency filtering. Compare all three stages against each other.
- All algorithms are pretrained / off-the-shelf. NO model training.

## Dataset
Seeing Through Fog (STF / DENSE dataset) — real-world fog/rain/snow drives
with synchronized camera + multiple LiDAR sensors + calibration + 3D box
labels.

Access is via a manifest of presigned S3 URLs: `data/manifest.json`.
IMPORTANT: presigned URLs expire (observed `X-Amz-Expires=432000`, i.e. 5
days from generation). The manifest currently in this repo was generated
2026-09-26 — re-request/redownload if working past ~2026-10-01.

Manifest format:
```json
{
  "urls": [
    {"key": "SeeingThroughFog/<relative/path>", "url": "<presigned-url>"},
    ...
  ]
}
```
Entries whose `key` ends in `/` are directory markers, not files — skip them
when downloading.

Known STF layout patterns to expect in the manifest:
- `calib_*.json` — top-level calibration files (stereo cameras, gated
  camera, full tf tree)
- `cam_stereo_left/`, `cam_stereo_right/` — camera image archives, often
  split into multi-part zips: `cam_stereo_left.zip`, `.z01`, `.z02`, etc.
  These must be reassembled before extraction (see
  `scripts/extract_archives.py`).
- LiDAR, radar, gated camera, and label directories are expected as
  additional top-level folders — confirm exact names once the full
  manifest is available.

Fallback datasets if STF coverage/quality is insufficient for a given clip:
RADIATE, then CADC.

## Hardware / Environment
- Remote machine via SSH, GPU: RTX 5060 Ti 16GB (Blackwell architecture,
  sm_120). CUDA already verified working on this machine as of 2026-09-26.
- Use `tmux` for any long-running job (downloads, extraction, inference) so
  SSH disconnects don't kill it.
- Keep batch size = 1 for detection inference (process frame-by-frame /
  point-cloud-by-point-cloud); LiDAR detection is memory-heavy.

## Tech stack (all pretrained / off-the-shelf, NO training)

### Camera pipeline
- Dehazing: Dark Channel Prior (`image-dehazer` package)
- De-raining (stretch goal): Restormer, pretrained checkpoint
- Detection: YOLOv8 (Ultralytics), pretrained COCO weights
- Temporal filtering: Ultralytics built-in ByteTrack (`model.track()`),
  + custom persistence filter (drop tracks shorter than N frames)

### LiDAR pipeline
- Noise filtering: Open3D `statistical_outlier_removal` /
  `radius_outlier_removal`; DROR as stretch goal if time allows
- Detection: OpenPCDet with a pretrained Waymo-trained checkpoint
  (PointPillars or CenterPoint)
- Temporal filtering: AB3DMOT (Kalman filter + Hungarian matching) +
  same persistence filter concept as camera

### Metrics
- Precision/recall vs. STF ground-truth boxes
- Detection flicker rate (track birth/death frequency) as a
  no-ground-truth-needed proxy metric
- Run all metrics on both a degraded (fog/rain) clip AND a clear-weather
  clip, to show preprocessing doesn't hurt performance in good conditions

### Visualization
- Side-by-side video: raw vs. fully-processed pipeline, bounding boxes
  overlaid, using OpenCV/moviepy
- Bar/line charts of metrics: matplotlib or plotly

## Working conventions
- Each module should be independently testable: input format and output
  format documented in a docstring at the top of the file.
- After writing/editing any module, write or update a smoke test script
  that runs it on ONE sample and prints/saves output, before moving to
  the next module.
- Commit to git after each module's smoke test passes.
- Do NOT combine camera and LiDAR pipeline work in the same session/task —
  keep them fully separate, mirroring the repo structure.

## Repo structure
```
data/               # raw downloads, manifest.json, processed data
scripts/            # download_data.py, extract_archives.py, sanity checks
data_loader/        # STF camera + lidar loading, calibration parsing
preprocessing/
  camera/           # dehazing, deraining
  lidar/            # outlier removal, DROR
detection/
  camera/           # YOLOv8 wrapper
  lidar/            # OpenPCDet wrapper
tracking/
  camera/           # ByteTrack + persistence filter
  lidar/            # AB3DMOT + persistence filter
metrics/            # precision/recall, flicker rate
visualization/      # side-by-side video, metric plots
notebooks/          # quick exploration only, not final code
results/
  videos/
  charts/
```

## Build order
1. Get full manifest.json, run scripts/download_data.py, verify download
2. Run scripts/extract_archives.py to reassemble/unzip multi-part archives
3. data_loader/: verify one camera frame + one point cloud load correctly
4. Camera: YOLOv8 baseline on raw fog/rain clip (no preprocessing)
5. Camera: add dehazing preprocessing, rerun, compare
6. Camera: add ByteTrack + persistence filter, rerun, compare
7. Camera: compute metrics (precision/recall, flicker rate) for all 3 stages
8. LiDAR: OpenPCDet pretrained model running on ONE point cloud (env check)
9. LiDAR: add Open3D outlier removal preprocessing, rerun, compare
10. LiDAR: add AB3DMOT + persistence filter, rerun, compare
11. LiDAR: compute metrics for all 3 stages
12. Build side-by-side videos (camera and LiDAR separately)
13. Polish charts + pitch deck

## Known risks / things to check early
- Blackwell GPU (sm_120) may need a very recent PyTorch build — verify no
  "unsupported architecture" warnings before building on top. (Already
  checked working as of 2026-09-26 per user.)
- OpenPCDet custom CUDA op compilation is the most likely install failure
  point — test this in isolation before writing LiDAR pipeline code.
- STF presigned URLs expire — redownload manifest if working past
  ~2026-10-01.
- The manifest currently in this repo is INCOMPLETE (only ~7-8 entries
  seen so far, cut off mid multi-part-archive listing). Do not assume the
  dataset is fully described until the full manifest has been captured.
