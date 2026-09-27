"""Re-encode an overlay video down to a byte budget, for committing to git.

GitHub refuses any file over 100 MB and warns above 50 MB, so the 3846x1080
road overlays that scripts/run_segmentation.py produces cannot be pushed as-is.
This transcode shrinks them in place-quality terms: it drops every Nth frame
(output fps is divided by the same N, so wall-clock duration is unchanged) and
optionally scales the frame down. The result is the same video playing at the
same speed, with fewer frames and softer edges.

Only geometry and frame rate are used to hit the budget. There is no bitrate
knob: cv2.VIDEOWRITER_PROP_QUALITY is silently ignored by the FFMPEG backend for
mp4v -- setting it returns False and the output comes out byte-identical,
verified against this venv's opencv-python-headless. There is no ffmpeg binary
on the machine either, and the bundled avcodec carries neither libx264 nor
libopenh264, so mp4v is the only encoder available and resolution is the only
real lever left.

Candidates are tried highest fidelity first and the first whose projected size
fits the budget wins. The projection comes from a short calibration encode of
the opening frames, because the full decode is far too large to hold in memory
(2165 frames of 3846x1080 BGR is ~13 GB).

Usage:
    python scripts/shrink_video.py VIDEO [VIDEO ...] --max-mb 90
    python scripts/shrink_video.py results/videos/fiu_mapillary_road.mp4 \
        --out-dir /tmp/shrunk --max-mb 50
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from roadseg.video_io import VideoInfo, VideoWriter, iter_frames, probe

DEFAULT_MAX_MB = 90.0
SAFETY_MARGIN = 0.90
CALIBRATION_FRAMES = 60
BYTES_PER_MB = 1024 * 1024

Candidate = tuple[int, int]
"""(max_width, frame_step) -- max_width 0 means keep the source width."""


def _even(value: int) -> int:
    """Round down to an even integer; chroma subsampling needs even dimensions."""
    return max(2, value - (value % 2))


def geometry(info: VideoInfo, candidate: Candidate) -> tuple[tuple[int, int], float]:
    """Resolve a candidate into concrete output dimensions and frame rate.

    Args:
        info: probed source metadata.
        candidate: (max_width, frame_step) to apply.

    Returns:
        ((width, height), output_fps). The height is scaled by the same factor
        as the width and rounded to even.
    """
    max_width, step = candidate
    width = min(info.width, max_width)
    height = _even(int(round(info.height * (width / info.width))))
    return (width, height), info.fps / step


def default_ladder(width: int, height: int) -> list[Candidate]:
    """Build the fidelity ladder for a source frame size, best first.

    The first rung is the source resolution at half rate, which for the 3846x1080
    overlays is roughly a 2x reduction and is usually enough on its own. Later
    rungs trade sharpness for size and only come into play for tight budgets.

    Args:
        width, height: source frame dimensions.

    Returns:
        Candidates ordered from highest to lowest fidelity, deduplicated.
    """
    ladder: list[Candidate] = [(width, 2)]
    ladder += [(_even(int(width * scale)), 2) for scale in (0.9, 0.8, 0.7, 0.6, 0.5)]
    ladder += [(_even(int(width * scale)), 3) for scale in (0.8, 0.6)]
    ladder.append((_even(int(width * 0.5)), 4))

    seen: set[Candidate] = set()
    unique: list[Candidate] = []
    for max_width, step in ladder:
        if max_width > width or (max_width, step) in seen:
            continue
        seen.add((max_width, step))
        unique.append((max_width, step))
    return unique


def _fit(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Resize to `size` if needed, using area averaging for downscaling."""
    if frame.shape[0] == size[1] and frame.shape[1] == size[0]:
        return frame
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def _encode(
    frames: Sequence[np.ndarray],
    size: tuple[int, int],
    fps: float,
    out_path: str,
) -> int:
    """Encode frames to an mp4 and return the file size in bytes.

    Args:
        frames: BGR frames to write, in order.
        size: (width, height) of the output.
        fps: output frame rate.
        out_path: destination path.

    Returns:
        Size of the written file in bytes.
    """
    with VideoWriter(out_path, size[0], size[1], fps) as writer:
        for frame in frames:
            writer.write(_fit(frame, size))
    return os.path.getsize(out_path)


def _read_sample(path: str, wanted: int) -> list[np.ndarray]:
    """Decode up to `wanted` frames from the head of a video."""
    return [frame for _, frame in iter_frames(path, max_frames=wanted)]


def project_size_mb(
    source: str,
    info: VideoInfo,
    candidate: Candidate,
    sample: Sequence[np.ndarray],
    work_dir: str,
) -> float:
    """Estimate the full output size for a candidate, in MB.

    Encodes the sampled frames through the candidate's step and geometry, then
    scales the measured bytes per output frame by the full output frame count.

    Args:
        source: source path, used only for error messages.
        info: probed source metadata.
        candidate: (max_width, frame_step) to evaluate.
        sample: frames decoded from the head of the source.
        work_dir: directory for the throwaway calibration encode.

    Returns:
        Projected output size in MB.

    Raises:
        ValueError: when the sample is too short for the candidate's step.
    """
    _, step = candidate
    kept = sample[::step]
    if not kept:
        raise ValueError(
            f"calibration sample of {len(sample)} frames is too short for step {step} "
            f"({source})"
        )
    size, fps = geometry(info, candidate)
    tmp = os.path.join(work_dir, "calibration.mp4")
    written = _encode(kept, size, fps, tmp)
    os.remove(tmp)
    return (written / len(kept)) * math.ceil(info.frame_count / step) / BYTES_PER_MB


def pick_candidate(
    source: str,
    info: VideoInfo,
    budget_mb: float,
    sample: Sequence[np.ndarray],
    work_dir: str,
) -> Candidate:
    """Choose the highest-fidelity candidate whose projection fits the budget.

    Args:
        source: source path, used for error messages.
        info: probed source metadata.
        budget_mb: hard ceiling on the output size in MB.
        sample: frames decoded from the head of the source.
        work_dir: directory for calibration encodes.

    Returns:
        The chosen (max_width, frame_step).

    Raises:
        RuntimeError: when no rung of the ladder fits the budget.
    """
    target = budget_mb * SAFETY_MARGIN
    for candidate in default_ladder(info.width, info.height):
        size, fps = geometry(info, candidate)
        projected = project_size_mb(source, info, candidate, sample, work_dir)
        print(f"    {size[0]}x{size[1]} @ {fps:.2f} fps -> {projected:.1f} MB")
        if projected <= target:
            return candidate
    raise RuntimeError(
        f"cannot fit {os.path.basename(source)} under {target:.1f} MB with any rung of "
        f"the ladder; raise --max-mb, or cut a shorter clip with "
        f"run_segmentation.py --max-frames"
    )


def transcode(
    source: str,
    dest: str,
    info: VideoInfo,
    candidate: Candidate,
) -> VideoInfo:
    """Write a full frame-stepped, optionally downscaled copy of a video.

    Args:
        source: source path.
        dest: destination .mp4 path.
        info: probed source metadata.
        candidate: (max_width, frame_step) to apply.

    Returns:
        Probed metadata for the written file.
    """
    size, fps = geometry(info, candidate)
    with VideoWriter(dest, size[0], size[1], fps) as writer:
        for _, frame in iter_frames(source, stride=candidate[1]):
            writer.write(_fit(frame, size))
    return probe(dest)


def shrink(source: str, out_dir: str, budget_mb: float) -> tuple[str, VideoInfo]:
    """Write a copy of `source` into `out_dir` that fits `budget_mb`.

    Args:
        source: source video path.
        out_dir: directory to write the result into.
        budget_mb: hard ceiling on the output size in MB.

    Returns:
        (destination path, probed metadata of the written file).

    Raises:
        ValueError: when the source has too few decodable frames to calibrate.
        RuntimeError: when the written file still exceeds the budget.
    """
    info = probe(source)
    sample = _read_sample(source, CALIBRATION_FRAMES)
    if len(sample) < 8:
        raise ValueError(f"{source} yielded only {len(sample)} decodable frames")

    os.makedirs(out_dir, exist_ok=True)
    candidate = pick_candidate(source, info, budget_mb, sample, out_dir)
    dest = os.path.join(out_dir, os.path.basename(source))
    result = transcode(source, dest, info, candidate)

    before = os.path.getsize(source)
    after = os.path.getsize(dest)
    print(
        f"  {os.path.basename(source)}: {before / BYTES_PER_MB:.1f} MB -> "
        f"{after / BYTES_PER_MB:.1f} MB  "
        f"({info.width}x{info.height} @ {info.fps:.2f}fps, "
        f"{info.duration_seconds:.1f}s  ->  "
        f"{result.width}x{result.height} @ {result.fps:.2f}fps, "
        f"{result.duration_seconds:.1f}s)"
    )
    if after > budget_mb * BYTES_PER_MB:
        raise RuntimeError(
            f"{dest} is {after / BYTES_PER_MB:.1f} MB, over the {budget_mb:.1f} MB "
            f"budget despite the projection clearing it"
        )
    return dest, result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The frame rate is divided by the same factor the frame count is, so the "
            "output still plays at wall-clock speed."
        ),
    )
    parser.add_argument("videos", nargs="+", help="source video path(s)")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="destination directory (default: alongside the source)",
    )
    parser.add_argument(
        "--max-mb",
        type=float,
        default=DEFAULT_MAX_MB,
        help=f"per-file size ceiling in MB (default: {DEFAULT_MAX_MB:g})",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir
    for path in args.videos:
        target = out_dir or os.path.dirname(os.path.abspath(path))
        try:
            shrink(path, target, args.max_mb)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
