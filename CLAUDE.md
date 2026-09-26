# Project: Trustworthy Perception in Adverse Weather (Hackathon)

## Goal
Demonstrate that signal-level preprocessing + temporal consistency filtering
produces measurably more stable, accurate object detections from camera and
LiDAR data in adverse weather (rain / snow), compared to raw pretrained-model
output with no preprocessing.

Motivating context: Waymo is launching in Singapore (2028 target), where
monsoon rain and haze are explicitly flagged by Waymo as adaptation
challenges. Investigated Waymo's own public Open Dataset first and confirmed
(via community GitHub issues, e.g. waymo-research/waymo-open-dataset#593)
that real adverse-weather segments are essentially absent from that public
release despite a `weather` field existing in the schema — this is itself
worth mentioning in the pitch as a genuine finding, not just a data-sourcing
detour. This project targets the "sensor data reliability in adverse
conditions" problem in Waymo's perception stack. Connects to background at
the UF TEA Lab (https://tea.ece.ufl.edu/), which focuses on trustworthy
sensing in adverse environments.

## Scope
- Camera pipeline and LiDAR pipeline are developed and evaluated SEPARATELY,
  on two DIFFERENT datasets (see below) — no sensor fusion, no cross-modal
  projection, no shared frames between the two pipelines.
- For each modality: raw baseline -> add preprocessing -> add temporal
  consistency filtering. Compare all three stages against each other.
- All algorithms are pretrained / off-the-shelf. NO model training.
- Camera pipeline uses REAL rain data. LiDAR pipeline uses REAL snow data
  (not rain — see "Why snow for LiDAR" below). This is an intentional,
  disclosed scope decision, not an oversight.

## Datasets

### Camera: ACDC (Adverse Conditions Dataset with Correspondences)
Source: https://acdc.vision.ee.ethz.ch/ (free registration + terms
acceptance required, then request files at .../packages). Camera-only
(1080p GoPro), NO LiDAR — confirmed from the paper's capture-rig
description, so ACDC can never cover the LiDAR half of this project.

Files used (already downloaded/extracted as of 2026-09-26):
- `rgb_anon_trainvaltest.zip` (15.6 GB) — extract ONLY `rgb_anon/rain/*`
  (skip fog/night/snow entirely) into `data/processed/rgb_anon/rain/`,
  then delete the 15.6 GB zip to reclaim disk space. This is a normal
  (non-split) zip, so selective extraction works fine with plain `unzip`.
- `gt_detection_trainval.zip` (4 MB) — REAL COCO-style 2D object detection
  ground truth (not segmentation masks). Already extracted to
  `data/gt_detection/`. Relevant files:
  - `data/gt_detection/rain/instancesonly_rain_train_gt_detection.json`
  - `data/gt_detection/rain/instancesonly_rain_val_gt_detection.json`
  - `data/gt_detection/rain/instancesonly_rain_test_image_info.json`
    (test split has image info only, NO labels — standard withheld-test-set
    practice; do not expect annotations for these images)

Files NOT downloaded / not needed for this project's scope:
- `gt_panoptic_trainval.zip`, `gt_trainval.zip` (panoptic / semantic
  segmentation — this project uses the real detection-box labels instead)
- `gt_detection_trainval_ref.zip`, `gt_panoptic_trainval_ref.zip`,
  `gt_trainval_ref.zip` (the "_ref" split is normal-condition reference
  images for domain-adaptation research, not adverse-condition images —
  not relevant here)

Image count: ~1,000 rain images total (train+val+test combined, evenly
split across fog/night/rain/snow out of 4,006 adverse-condition images);
of those, the train+val subset (which has real detection labels) is a
fraction of the 2,006 labeled trainval images across all 4 conditions —
confirm the exact rain-only train/val counts by inspecting the JSON
`images` array once data loading starts (see Build order step 1).

### LiDAR: CADC (Canadian Adverse Driving Conditions dataset)
Source: http://wiselab.uwaterloo.ca/cadcd_data/ — no registration/login
gate, direct HTTP downloads, organized per-sequence (NOT one monolithic
archive):
```
{base}/{date}/calib.zip
{base}/{date}/{sequence}/labeled.zip
{base}/{date}/{sequence}/3d_ann.json
```
Dates/sequences: `2018_03_06` (seq 0001-0018), `2018_03_07` (seq
0001-0007), `2019_02_27` (seq 0002-0082). All sequences are SNOW driving
conditions (Waterloo, Canada), not rain.

**Why snow, not rain, for LiDAR**: extensive search (STF: 39GB
non-partitionable split-zip archive; RADIATE: gated access requiring an
org-email Dropbox invite; nuScenes: has real rain scenes but downloads in
large blobs of unconfirmed granularity) found no rain-LiDAR source that is
both immediately accessible AND reasonably sized for a hackathon. CADC is
verified small, per-sequence downloadable, and immediately accessible.
The preprocessing techniques used here (outlier removal targeting
airborne-precipitation-induced spurious near-range returns) are not
rain-specific in their design — the same physics (droplets/flakes
scattering the LiDAR beam) applies to snow. This is a disclosed
generalization, stated explicitly in the pitch: "camera: real rain;
LiDAR: real snow (verified accessible); the method targets
precipitation-induced point cloud noise broadly, not one weather word."

Not yet downloaded as of 2026-09-26. Check size before downloading:
```
curl -sI "http://wiselab.uwaterloo.ca/cadcd_data/2019_02_27/0002/labeled.zip" | grep -i content-length
```

## Hardware / Environment
- Remote machine via SSH, GPU: RTX 5060 Ti 16GB (Blackwell architecture,
  sm_120). CUDA verified working as of 2026-09-26.
- Use `tmux` for any long-running job (downloads, inference) so SSH
  disconnects don't kill it.
- Keep batch size = 1 for detection inference; LiDAR detection is
  memory-heavy.
- Large dataset files (multi-GB zips) should be deleted after extraction
  to keep disk usage manageable — this bit us once already with an
  interrupted 15.6GB browser download; prefer `wget -c` (resumable) over
  browser downloads for any single file over ~1GB.

## Tech stack (all pretrained / off-the-shelf, NO training)

### Camera pipeline (ACDC rain)
- Dehazing/de-raining: Dark Channel Prior (`image-dehazer` package) first;
  Restormer (pretrained checkpoint) as a stretch goal if time allows
- Detection: YOLOv8 (Ultralytics), pretrained COCO weights
- Temporal filtering: Ultralytics built-in ByteTrack (`model.track()`)
  + custom persistence filter (drop tracks shorter than N frames)
  NOTE: ACDC images are NOT necessarily a continuous video sequence in
  original capture order per-file — confirm frame ordering/continuity
  from filenames/metadata before assuming ByteTrack has genuine temporal
  continuity to work with. If frames are not sequential, temporal
  consistency filtering may need reframing (e.g. treat filename groups
  as pseudo-sequences) — resolve this in Build order step 1.

### LiDAR pipeline (CADC snow)
- Noise filtering: Open3D `statistical_outlier_removal` /
  `radius_outlier_removal`; DROR as stretch goal if time allows
- Detection: OpenPCDet with a pretrained checkpoint (PointPillars or
  CenterPoint) — note CADC's LiDAR is a Velodyne HDL-64, same sensor
  family as commonly-pretrained checkpoints (e.g. KITTI/Waymo-trained),
  so a pretrained model should transfer reasonably
- Temporal filtering: AB3DMOT (Kalman filter + Hungarian matching) +
  same persistence filter concept as camera

### Metrics
- Camera: precision/recall vs. ACDC's real COCO-format ground truth boxes
  (`instancesonly_rain_{train,val}_gt_detection.json`)
- LiDAR: precision/recall vs. CADC's `3d_ann.json` labels
- Detection flicker rate (track birth/death frequency) as a
  no-ground-truth-needed proxy metric for both modalities
- Where possible, also run on a small clear-weather comparison set to
  show preprocessing doesn't hurt performance in good conditions (ACDC's
  normal-condition images ship alongside the adverse ones in the same
  zip; CADC's clear-weather comparison would need a different clear-sky
  KITTI-style sequence if pursued)

### Visualization
- Side-by-side: raw vs. fully-processed pipeline, bounding boxes
  overlaid, using OpenCV/moviepy
- Bar/line charts of metrics: matplotlib or plotly

## Working conventions
- Each module independently testable: input/output format documented in
  a docstring at the top of the file.
- After writing/editing any module, write or update a smoke test script
  that runs it on ONE sample and prints/saves output, before moving on.
- Commit to git after each module's smoke test passes.
- Do NOT combine camera and LiDAR pipeline work in the same session/task —
  keep them fully separate, mirroring the repo structure below.

## Repo structure
```
data/
  gt_detection/           # ACDC real detection labels (COCO format), by
                          # condition/split -- already downloaded+extracted
  processed/
    rgb_anon/rain/        # extracted ACDC rain images (train/val/test)
    cadc/                 # CADC sequences once downloaded
  License.pdf, README.md  # ACDC's own license/readme
scripts/
  sanity_check_cuda.py    # GPU/CUDA check (dataset-agnostic, keep)
  download_cadc.py        # TO BE WRITTEN: direct per-sequence CADC
                          # downloader (see Build order step 1)
data_loader/
  acdc_camera.py          # TO BE WRITTEN: load ACDC image + COCO label
  cadc_lidar.py           # TO BE WRITTEN: load CADC point cloud + label
preprocessing/
  camera/                 # dehazing, deraining
  lidar/                  # outlier removal, DROR
detection/
  camera/                 # YOLOv8 wrapper
  lidar/                  # OpenPCDet wrapper
tracking/
  camera/                 # ByteTrack + persistence filter
  lidar/                  # AB3DMOT + persistence filter
metrics/                  # precision/recall, flicker rate
visualization/            # side-by-side video, metric plots
notebooks/                # quick exploration only, not final code
results/
  videos/
  charts/
```

## Build order
1. Camera data loader: confirm ACDC rain image count/structure, confirm
   whether filenames indicate sequence order (needed for temporal
   filtering), write `data_loader/acdc_camera.py` matching images to
   their COCO annotation entries. Smoke test: load 1 image + its labels,
   print/plot them.
2. Camera: YOLOv8 baseline on raw rain images (no preprocessing) —
   compute precision/recall vs. real ACDC labels as the "before" number.
3. Camera: add dehazing preprocessing, rerun YOLOv8, recompute metrics —
   first real before/after comparison.
4. Camera: add ByteTrack + persistence filter (contingent on step 1's
   sequence-order finding), rerun, recompute metrics.
5. Camera: build the side-by-side before/after visualization.
6. LiDAR: download 1-2 CADC sequences (check size with `curl -sI` first,
   per the command in the CADC section above), write
   `scripts/download_cadc.py`.
7. LiDAR: OpenPCDet pretrained model running on ONE CADC point cloud
   (environment/install check — this is the highest-risk install step).
8. LiDAR: add Open3D outlier removal preprocessing, rerun, compare.
9. LiDAR: add AB3DMOT + persistence filter, rerun, compare.
10. LiDAR: compute metrics (precision/recall vs. CADC's `3d_ann.json`,
    flicker rate) for all 3 stages.
11. LiDAR: build the side-by-side before/after visualization.
12. Polish charts + pitch deck. Pitch should explicitly state the
    camera=rain / LiDAR=snow scope decision and why (see "Why snow, not
    rain, for LiDAR" above) rather than let a judge discover the
    mismatch and assume it wasn't considered.

## Known risks / things to check early
- Blackwell GPU (sm_120) may need a very recent PyTorch build — already
  verified working as of 2026-09-26.
- OpenPCDet custom CUDA op compilation is the most likely LiDAR-side
  install failure point — test in isolation before writing pipeline code.
- ACDC image sequence continuity is UNCONFIRMED — do not assume
  ByteTrack/temporal filtering has genuine consecutive frames to work
  with until step 1 confirms it from the actual filenames/metadata.
- Large single-file downloads (15GB+) are unreliable via browser (already
  hit a silent 0-byte-file failure once) — prefer `wget -c` for anything
  over ~1GB.
- CADC sequence sizes are NOT YET CONFIRMED — run the `curl -sI` size
  check before committing to a download.
