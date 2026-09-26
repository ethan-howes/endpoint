# Trustworthy Perception in Adverse Weather

Hackathon project demonstrating that signal-level preprocessing +
temporal consistency filtering produces measurably more stable, accurate
object detections from camera and LiDAR data in fog/rain, compared to raw
pretrained-model output with no preprocessing.

See `CLAUDE.md` for full project context, motivation, tech stack, and
build order.

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
   version — see the note in `requirements.txt`.)

3. Install `zip`/`unzip` if not already present (needed to reassemble
   STF's split archives):
   ```
   sudo apt-get update && sudo apt-get install -y zip unzip
   ```

4. `data/manifest.json` is already included (472 entries, confirmed valid
   as of 2026-09-26). **This file is gitignored** — it contains presigned
   URLs with embedded AWS credentials/tokens and should never be
   committed. Note: the URLs expire ~5 days from generation
   (2026-09-26 06:52 UTC) — re-request from STF if working past
   ~2026-10-01.

5. Download only what this project's scope needs (camera + LiDAR +
   ground truth + weather metadata — skips gated-camera sensors, raw
   history frames, stereo-right, radar, road friction, and precomputed
   stereo depth, which together make up most of the 472 entries):
   ```
   tmux new -s download
   python scripts/download_data.py --workers 8 \
     --include cam_stereo_left,lidar_hdl64_last,lidar_hdl64_strongest,calib_cam_stereo_left.json,calib_cam_stereo_right.json,calib_tf_tree_full.json,gt_labels,labeltool_labels,weather_station
   ```
   This pulls 50 of the 472 entries. To see all available group names
   (e.g. if you want to add gated-camera data later):
   ```
   python scripts/download_data.py --list-groups
   ```
   - Safe to re-run: already-downloaded files are skipped.
   - If some files fail (network blip, etc.), rerun with:
     ```
     python scripts/download_data.py --only-failed
     ```
   - To sanity-check on a handful of files first:
     ```
     python scripts/download_data.py --include cam_stereo_left --limit 3
     ```

6. Reassemble and extract the split zip archives:
   ```
   python scripts/extract_archives.py --dry-run   # preview first
   python scripts/extract_archives.py
   ```

## Repo layout

See the "Repo structure" section in `CLAUDE.md`.
