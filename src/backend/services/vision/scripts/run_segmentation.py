"""Segment roads out of a video and write a QC overlay video.

Loads a pretrained Mask2Former checkpoint, runs it over every Nth frame of the
input video, and writes a new video showing the road mask.

Usage:
    python scripts/run_segmentation.py --input dashcam.mp4
    python scripts/run_segmentation.py -i dashcam.mp4 --model cityscapes
    python scripts/run_segmentation.py -i dashcam.mp4 --layout both --min-confidence 0.6
    python scripts/run_segmentation.py --list-presets
    python scripts/run_segmentation.py --list-classes mapillary

The output video keeps the source resolution and real-time duration: when
--stride is greater than 1 the output fps is divided by it, so a strided run
still plays at wall-clock speed instead of fast-forwarding.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roadseg.config import PRESETS, get_preset
from roadseg.overlay import LAYOUTS
from roadseg.pipeline import PipelineConfig, resolve_model, run
from roadseg.segmenter import RoadSegmenter
from roadseg.video_io import probe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", "-i", help="input video path")
    parser.add_argument(
        "--output",
        "-o",
        help="output video path (default: results/videos/<input>_road.mp4)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="mapillary",
        help=f"preset name or raw HF repo id. presets: {', '.join(sorted(PRESETS))}",
    )
    parser.add_argument(
        "--road-classes",
        nargs="+",
        default=None,
        metavar="NAME",
        help="class names counting as road (default: the preset's own set)",
    )
    parser.add_argument(
        "--include-extra-road-classes",
        action="store_true",
        help="also count the preset's optional classes (e.g. Mapillary Bike Lane)",
    )
    parser.add_argument("--layout", default="overlay", choices=LAYOUTS)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="road-score floor in [0,1]; raise to suppress speckle",
    )
    parser.add_argument(
        "--smooth",
        type=float,
        default=None,
        metavar="ALPHA",
        help="temporal EMA factor in (0,1] to reduce flicker (ghosts under motion)",
    )
    parser.add_argument("--shortest-edge", type=int, default=None)
    parser.add_argument("--longest-edge", type=int, default=None)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--device", default=None, help="cuda, cpu, ...")
    parser.add_argument("--no-amp", action="store_true", help="disable fp16 autocast")
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument(
        "--list-classes",
        metavar="PRESET",
        default=None,
        help="print a preset's label set, then exit",
    )
    return parser


def _print_presets() -> None:
    for name, preset in sorted(PRESETS.items()):
        print(f"{name}:")
        print(f"  repo: {preset.repo}")
        print(f"  road classes: {', '.join(preset.road_classes)}")
        if preset.extra_road_classes:
            print(f"  optional:     {', '.join(preset.extra_road_classes)}")
        print(f"  {preset.notes}\n")


def _print_classes(preset_name: str) -> int:
    try:
        preset = get_preset(preset_name)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{preset.repo} ({preset_name})")
    print("loading to read the label set...")
    segmenter = RoadSegmenter(repo=preset.repo, road_classes=preset.road_classes)
    print(f"num_labels={segmenter.num_labels}")
    for idx, name in sorted(segmenter.id2label.items()):
        flag = "  <== road" if idx in segmenter.road_ids else ""
        print(f"  {idx:3d}: {name}{flag}")
    return 0


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_presets:
        _print_presets()
        return 0
    if args.list_classes:
        return _print_classes(args.list_classes)
    if not args.input:
        parser.error("--input is required (or use --list-presets / --list-classes)")

    if not os.path.exists(args.input):
        print(f"error: no such file: {args.input}", file=sys.stderr)
        return 1

    stem, ext = os.path.splitext(os.path.basename(args.input))
    output = args.output or os.path.join("results", "videos", f"{stem}_road.mp4")

    try:
        resolve_model(args.model)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    road_classes = args.road_classes
    if road_classes is None and args.include_extra_road_classes:
        try:
            road_classes = list(get_preset(args.model).road_classes) + list(
                get_preset(args.model).extra_road_classes
            )
        except KeyError:
            road_classes = None

    config = PipelineConfig(
        model=args.model,
        road_classes=road_classes,
        layout=args.layout,
        stride=args.stride,
        batch_size=args.batch_size,
        min_confidence=args.min_confidence,
        smooth_alpha=args.smooth,
        shortest_edge=args.shortest_edge,
        longest_edge=args.longest_edge,
        max_frames=args.max_frames,
        device=args.device,
        amp=not args.no_amp,
    )

    info = probe(args.input)
    print(f"input : {info.describe()}")
    print(f"model : {args.model}")
    print(f"output: {output}")
    print("loading model (first run downloads ~1.7 GB)...")

    try:
        result = run(args.input, output, config)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    seg = result.segmenter
    if seg is not None:
        print(f"road classes: {seg.describe_road_classes()} of {seg.num_labels} labels")
    print()
    print(f"wrote {result.frames_written} frames to {output}")
    print(
        f"elapsed {result.elapsed_seconds:.1f}s "
        f"({result.throughput():.2f} processed fps, mean road coverage "
        f"{result.mean_coverage * 100:.1f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
