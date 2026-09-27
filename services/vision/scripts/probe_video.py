"""Inspect a video's geometry and timing before running segmentation.

Prints resolution, fps, frame count, duration, and codec so sensible defaults
for --stride, --batch-size, and --max-frames can be chosen.

Usage:
    python scripts/probe_video.py VIDEO [VIDEO ...]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roadseg.video_io import probe


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    for path in argv:
        try:
            info = probe(path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(info.describe())
        print(f"    codec={info.fourcc} ({_fourcc(info.fourcc)})")
        megapixels = (info.width * info.height) / 1e6
        if info.frame_count > 0:
            eta = info.frame_count / 8.0
            print(
                f"    {megapixels:.1f} MP | at ~8 fps inference this is "
                f"about {eta / 60:.1f} min of compute"
            )
    return 0


def _fourcc(code: int) -> str:
    if not code:
        return "unknown"
    return "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
