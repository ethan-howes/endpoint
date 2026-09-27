"""Composite the segmentation overlay and Gemini's reasoning into one demo video.

Takes the road-mask overlay produced by scripts/run_segmentation.py and lays a
text panel beside it, revealing each of Gemini's timestamped events as playback
reaches it. The result is a single self-contained mp4 for showing to someone:
the road with its green mask on the left, the model's reasoning appearing on
the right on a clock.

Nothing is generated at showtime. The reasoning is read from the JSON that
scripts/ask_gemini.py wrote earlier, so the demo is repeatable and needs no API
key, no network, and no model on the machine that plays it.

Usage:
    python scripts/compose_demo.py results/videos/fiu_mapillary_road.mp4
    python scripts/compose_demo.py overlay.mp4 --reasoning results/gemini/parking.json
    python scripts/compose_demo.py overlay.mp4 --panel-width 1280 --out /tmp/demo.mp4
    python scripts/compose_demo.py overlay.mp4 --max-frames 60 --out /tmp/smoke.mp4

The output has no audio track. roadseg.video_io.VideoWriter never writes one,
which is what we want here: the reasoning is the message, and a silent clip
cannot be misheard as saying more than it does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roadseg.text_panel import PanelStyle, default_style, render_panel
from roadseg.video_io import VideoWriter, iter_frames, probe

DEFAULT_REASONING = os.path.join("results", "gemini", "parking.json")
GAP_PX = 6
PANEL_WIDTH_RATIO = 0.32
MIN_PANEL_WIDTH = 900
BYTES_PER_MB = 1024 * 1024


def load_reasoning(path: str) -> tuple[list[dict], str, dict]:
    """Read the JSON that scripts/ask_gemini.py wrote.

    Args:
        path: path to parking.json.

    Returns:
        (events, thought_summary, meta) where events is the timestamped list,
        thought_summary is the header text, and meta is the rest of the record
        (model, usage, and so on) for reporting.

    Raises:
        FileNotFoundError: when the file is absent.
        ValueError: when it is not the expected shape.
    """
    with open(path, encoding="utf-8") as handle:
        record = json.load(handle)
    events = record.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{path} has no 'events' list; was it written by ask_gemini.py?")
    for event in events:
        for key in ("timestamp", "label", "text"):
            if key not in event:
                raise ValueError(f"{path}: an event is missing {key!r}: {event}")
    meta = {k: v for k, v in record.items() if k not in ("events", "thought_summary")}
    return events, record.get("thought_summary", ""), meta


def panel_width_for(video_width: int, requested: int | None) -> int:
    """Decide how wide the text panel should be.

    Args:
        video_width: width of the overlay being composited.
        requested: an explicit --panel-width, or None to derive one.

    Returns:
        Panel width in pixels, even, so chroma subsampling is happy.
    """
    if requested:
        width = int(requested)
    else:
        width = max(MIN_PANEL_WIDTH, int(video_width * PANEL_WIDTH_RATIO))
    return max(320, width - (width % 2))


def reveal_count_for(t_seconds: float, events: list[dict]) -> int:
    """How many events should be visible at time `t_seconds`.

    Events are already sorted by ask_gemini.py. An event appears the moment
    playback reaches its timestamp, so the comparison is inclusive.

    Args:
        t_seconds: playback position in seconds.
        events: the sorted event list.

    Returns:
        Count of events to reveal, in [0, len(events)].
    """
    count = 0
    for event in events:
        if float(event.get("t_seconds", 0.0)) <= t_seconds:
            count += 1
        else:
            break
    return count


def compose(
    video_path: str,
    out_path: str,
    events: list[dict],
    thought_summary: str,
    style: PanelStyle,
    *,
    panel_width: int | None = None,
    max_frames: int | None = None,
) -> tuple[int, int, int]:
    """Write the composited video.

    The panel is re-rendered only when the reveal count changes and reused for
    every frame in between. Text layout is the expensive part of this job, and
    across a 36 s clip the count changes about 37 times in 1083 frames, so the
    cache turns ~1083 layouts into ~37.

    Args:
        video_path: the segmentation overlay to composite.
        out_path: destination mp4.
        events: timestamped events, sorted by t_seconds.
        thought_summary: static header text for the panel.
        style: panel appearance.
        panel_width: explicit panel width, or None to derive from video width.
        max_frames: stop after this many frames, for a quick smoke test.

    Returns:
        (frames_written, output_width, output_height).

    Raises:
        ValueError: when the video reports a degenerate frame size.
    """
    info = probe(video_path)
    if info.width < 2 or info.height < 2:
        raise ValueError(f"{video_path} has a degenerate frame size: {info.width}x{info.height}")

    p_width = panel_width_for(info.width, panel_width)
    out_width = info.width + GAP_PX + p_width
    out_height = info.height
    gap = np.empty((out_height, GAP_PX, 3), dtype=np.uint8)
    gap[:, :] = (40, 40, 40)
    cached_count = -1
    cached_panel = None
    written = 0

    print(f"input : {info.describe()}")
    print(f"panel : {p_width}x{out_height} ({len(events)} events)")
    print(f"output: {out_path} -> {out_width}x{out_height} @ {info.fps:.3f} fps")

    with VideoWriter(out_path, out_width, out_height, info.fps) as writer:
        for index, frame in iter_frames(video_path, max_frames=max_frames):
            t_seconds = index / info.fps
            count = reveal_count_for(t_seconds, events)
            if count != cached_count:
                cached_panel = render_panel(
                    events, count, (p_width, out_height), style,
                    thought_summary=thought_summary,
                )
                cached_count = count
            writer.write(np.hstack([frame, gap, cached_panel]))
            written += 1
            if written % 100 == 0:
                print(f"  {written} frames, revealing {count}/{len(events)}")

    return written, out_width, out_height


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Output has no audio track.",
    )
    parser.add_argument("video", help="segmentation overlay from run_segmentation.py")
    parser.add_argument(
        "--reasoning",
        default=DEFAULT_REASONING,
        help=f"parking.json from ask_gemini.py (default: {DEFAULT_REASONING})",
    )
    parser.add_argument("--out", "-o", default=None, help="output mp4 path")
    parser.add_argument("--panel-width", type=int, default=None)
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args(argv)

    if not os.path.exists(args.video):
        print(f"error: no such file: {args.video}", file=sys.stderr)
        return 1
    if not os.path.exists(args.reasoning):
        print(
            f"error: no reasoning at {args.reasoning}\n"
            f"       run scripts/ask_gemini.py first to produce it",
            file=sys.stderr,
        )
        return 1

    try:
        events, thought_summary, meta = load_reasoning(args.reasoning)
        stem, _ = os.path.splitext(os.path.basename(args.video))
        out_path = args.out or os.path.join("results", "videos", f"{stem}_demo.mp4")
        written, out_width, out_height = compose(
            args.video, out_path, events, thought_summary, default_style(),
            panel_width=args.panel_width, max_frames=args.max_frames,
        )
    except (ValueError, FileNotFoundError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size_mb = os.path.getsize(out_path) / BYTES_PER_MB
    print()
    print(f"reasoning: {args.reasoning} ({meta.get('model', '?')}, "
          f"{meta.get('windows', 1)} call(s), "
          f"{meta.get('usage', {}).get('total_token_count', 0)} tokens)")
    print(f"wrote {written} frames, {out_width}x{out_height}, {size_mb:.1f} MB -> {out_path}")
    if not thought_summary:
        print("note: the reasoning record had no thought summary, so the panel header is empty.")
    if size_mb > 90:
        print(
            f"note: {size_mb:.1f} MB is over GitHub's 50 MB warning. Shrink it with:\n"
            f"  python scripts/shrink_video.py {out_path} --max-mb 50"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
