"""End-to-end self-test: no dashcam footage required.

Downloads a few license-clean forward-facing dashcam stills from Wikimedia
Commons, assembles them into a short synthetic clip, then drives the real
pipeline over that clip and writes a QC video. Also renders every sample under
both candidate checkpoints so the road masks can be compared before committing
to a default.

What it produces:
    results/videos/selftest_clip.mp4        synthetic test clip
    results/videos/selftest_mapillary.mp4   overlay from the default preset
    results/videos/selftest_cityscapes.mp4  overlay from the comparison preset
    results/videos/selftest_compare.png     per-sample, per-model comparison grid
    results/selftest_report.json           coverage numbers per sample per model

Usage:
    python scripts/self_test.py
    python scripts/self_test.py --samples 2 --clip-frames 12
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roadseg.config import get_preset
from roadseg.overlay import ROAD_COLOR_BGR, add_label, overlay_road
from roadseg.pipeline import PipelineConfig, run
from roadseg.segmenter import RoadSegmenter
from roadseg.video_io import write_synthetic_clip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "assets", "samples")
RESULT_DIR = os.path.join(ROOT, "results")
VIDEO_DIR = os.path.join(RESULT_DIR, "videos")
CLIP_SIZE = (1280, 720)
USER_AGENT = "roadseg-selftest/1.0 (segmentation smoke test; contact: local)"


@dataclass(frozen=True)
class Sample:
    """One downloadable test frame plus its provenance."""

    name: str
    url: str
    license: str
    author: str = "see Commons file page"


SAMPLES: tuple[Sample, ...] = (
    Sample(
        "dashcam_road",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e7/Dashcam_recording_a_road.jpg/1280px-Dashcam_recording_a_road.jpg",
        "CC BY-SA 4.0",
    ),
    Sample(
        "i95_exit",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/Florida_I95nb_Exit_337_exit_only_dashcam_2.jpg/1280px-Florida_I95nb_Exit_337_exit_only_dashcam_2.jpg",
        "CC0",
    ),
    Sample(
        "i75_offramp",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9b/Georgia_I75nb_Exit_18_offramp_Lodging_Logo_road_sign_dashcam.jpg/1280px-Georgia_I75nb_Exit_18_offramp_Lodging_Logo_road_sign_dashcam.jpg",
        "CC BY 4.0",
    ),
    Sample(
        "us129_crossroad",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6a/NB_US129_GA11_GA22_Crossroad_Graham_Road%2C_Homer_Roberts_Road_dashcam.jpg/1280px-NB_US129_GA11_GA22_Crossroad_Graham_Road%2C_Homer_Roberts_Road_dashcam.jpg",
        "CC BY 4.0",
    ),
    Sample(
        "us441_county",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/0/01/SB_US_441_Mikesville%2C_Columbia_County_dashcam.jpg/1280px-SB_US_441_Mikesville%2C_Columbia_County_dashcam.jpg",
        "CC BY 4.0",
    ),
)


def download(sample: Sample, retries: int = 3, pause: float = 1.5) -> str | None:
    """Fetch one sample image, throttling politely.

    Wikimedia rate-limits bursts with HTTP 429, so every request pauses first
    and retries with a growing delay.

    Args:
        sample: what to fetch.
        retries: attempts before giving up.
        pause: seconds to wait before the first attempt.

    Returns:
        Local path, or None if the download could not be completed.
    """
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    dest = os.path.join(SAMPLE_DIR, f"{sample.name}.jpg")
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return dest

    for attempt in range(retries):
        time.sleep(pause * (attempt + 1))
        try:
            request = urllib.request.Request(sample.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
            if len(payload) < 1024:
                raise ValueError(f"suspiciously small payload ({len(payload)} B)")
            with open(dest, "wb") as handle:
                handle.write(payload)
            return dest
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
            reason = getattr(exc, "code", type(exc).__name__)
            print(f"  {sample.name}: attempt {attempt + 1}/{retries} failed ({reason})")
    return None


def to_clip_size(image: np.ndarray, size: tuple[int, int] = CLIP_SIZE) -> np.ndarray:
    """Center-crop to the target aspect ratio, then resize.

    Keeps the geometry plausible when mixing portrait and landscape stills
    into one clip.
    """
    target_w, target_h = size
    h, w = image.shape[:2]
    target_aspect = target_w / target_h
    aspect = w / h
    if aspect > target_aspect:
        new_w = int(round(h * target_aspect))
        left = (w - new_w) // 2
        image = image[:, left : left + new_w]
    else:
        new_h = int(round(w / target_aspect))
        top = (h - new_h) // 2
        image = image[top : top + new_h, :]
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def write_credits(downloaded: list[Sample]) -> None:
    """Record provenance for the downloaded frames.

    CC BY / CC BY-SA require attribution, so the credits travel with the repo
    rather than living only in this script.
    """
    lines = [
        "# Test image credits",
        "",
        "Frames downloaded by `scripts/self_test.py` from Wikimedia Commons,",
        "used only as smoke-test input. Not training data.",
        "",
    ]
    for sample in downloaded:
        lines += [
            f"## {sample.name}",
            "",
            f"- Source: {sample.url}",
            f"- License: {sample.license}",
            f"- Author: {sample.author}",
            "",
        ]
    with open(os.path.join(SAMPLE_DIR, "CREDITS.md"), "w") as handle:
        handle.write("\n".join(lines))


def build_clip(paths: list[str], out_path: str, clip_frames: int) -> str:
    """Turn the sample stills into a short clip, repeating them in order."""
    ordered: list[str] = []
    for index in range(clip_frames):
        ordered.append(paths[index % len(paths)])
    return write_synthetic_clip(out_path, ordered, fps=10.0, size=CLIP_SIZE)


def comparison_grid(
    frames: list[np.ndarray],
    names: list[str],
    per_model: dict[str, list[tuple[np.ndarray, np.ndarray]]],
    out_path: str,
) -> str:
    """Render original + each model's road overlay, one row per sample.

    Args:
        frames: source frames.
        names: sample names, for row captions.
        per_model: model name -> list of (overlay, mask) per sample.
        out_path: destination PNG.

    Returns:
        out_path.
    """
    models = list(per_model)
    tiles: list[list[np.ndarray]] = []
    for index, frame in enumerate(frames):
        row = [frame]
        for model in models:
            overlay, _ = per_model[model][index]
            row.append(overlay)
        tiles.append(row)

    labelled: list[np.ndarray] = []
    for index, row in enumerate(tiles):
        panels = [add_label(row[0].copy(), f"{names[index]} | original")]
        for offset, model in enumerate(models):
            caption = f"{model} road={(per_model[model][index][1].mean() * 100):.0f}%"
            panels.append(add_label(row[offset + 1].copy(), caption))
        gap = np.full((panels[0].shape[0], 6, 3), 40, dtype=np.uint8)
        joined = panels[0]
        for panel in panels[1:]:
            joined = np.hstack([joined, gap, panel])
        labelled.append(joined)

    gap = np.full((6, labelled[0].shape[1], 3), 40, dtype=np.uint8)
    grid = labelled[0]
    for panel in labelled[1:]:
        grid = np.vstack([grid, gap, panel])
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cv2.imwrite(out_path, grid)
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--samples", type=int, default=3, help="how many stills to fetch")
    parser.add_argument("--clip-frames", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--presets", nargs="+", default=["mapillary", "cityscapes"])
    args = parser.parse_args(argv)

    os.makedirs(VIDEO_DIR, exist_ok=True)

    print(f"fetching up to {args.samples} dashcam stills (throttled, ~1.5s apart)...")
    paths: list[str] = []
    got: list[Sample] = []
    for sample in SAMPLES[: args.samples]:
        local = download(sample)
        if local:
            paths.append(local)
            got.append(sample)
            print(f"  {sample.name}: ok")
    if len(paths) < 2:
        print("error: could not fetch enough sample images to run the self-test", file=sys.stderr)
        return 1
    write_credits(got)

    frames = [to_clip_size(cv2.imread(p)) for p in paths]
    frames = [f for f in frames if f is not None]
    print(f"loaded {len(frames)} frames at {CLIP_SIZE[0]}x{CLIP_SIZE[1]}")

    clip_path = os.path.join(VIDEO_DIR, "selftest_clip.mp4")
    build_clip(paths, clip_path, args.clip_frames)
    print(f"synthetic clip: {clip_path}")

    report: dict = {"clip": clip_path, "models": {}}
    per_model: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
    segmenters: dict[str, RoadSegmenter] = {}

    for preset_name in args.presets:
        preset = get_preset(preset_name)
        print(f"\nloading {preset_name} ({preset.repo}) ...")
        segmenter = RoadSegmenter.from_preset(preset_name)
        segmenters[preset_name] = segmenter
        print(f"  loaded in {segmenter.load_seconds:.1f}s")
        print(f"  road classes -> {segmenter.describe_road_classes()}")
        print(f"  labels: {segmenter.num_labels}")

        masks = segmenter.segment(frames)
        overlays = [overlay_road(f, m.mask, ROAD_COLOR_BGR, 0.45) for f, m in zip(frames, masks)]
        per_model[preset_name] = list(zip(overlays, [m.mask for m in masks]))
        report["models"][preset_name] = {
            "repo": segmenter.repo,
            "road_class_ids": list(segmenter.road_ids),
            "road_class_names": [segmenter.label2id[i] for i in segmenter.road_ids],
            "num_labels": segmenter.num_labels,
            "load_seconds": round(segmenter.load_seconds, 2),
            "coverage": {
                os.path.splitext(os.path.basename(p))[0]: round(float(m.coverage), 4)
                for p, m in zip(paths, masks)
            },
        }

        video_out = os.path.join(VIDEO_DIR, f"selftest_{preset_name}.mp4")
        config = PipelineConfig(
            model=preset_name,
            layout="both",
            batch_size=args.batch_size,
            caption_prefix=preset_name,
        )
        result = run(clip_path, video_out, config, segmenter=segmenter)
        print(
            f"  wrote {result.frames_written} frames -> {video_out} "
            f"({result.throughput():.2f} fps, mean road {result.mean_coverage * 100:.1f}%)"
        )
        report["models"][preset_name]["clip_throughput_fps"] = round(result.throughput(), 2)
        report["models"][preset_name]["clip_mean_coverage"] = round(result.mean_coverage, 4)

    grid_path = os.path.join(VIDEO_DIR, "selftest_compare.png")
    comparison_grid(frames, [os.path.splitext(os.path.basename(p))[0] for p in paths], per_model, grid_path)

    report_path = os.path.join(RESULT_DIR, "selftest_report.json")
    with open(report_path, "w") as handle:
        json.dump(report, handle, indent=2)

    print("\n" + "=" * 68)
    print(f"{'sample':22s}" + "".join(f"{m:>16s}" for m in args.presets))
    for name in [os.path.splitext(os.path.basename(p))[0] for p in paths]:
        row = f"{name:22s}"
        for m in args.presets:
            row += f"{report['models'][m]['coverage'].get(name, 0) * 100:15.1f}%"
        print(row)
    print("=" * 68)
    print(f"comparison grid : {grid_path}")
    print(f"report          : {report_path}")
    print("\nself-test PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
