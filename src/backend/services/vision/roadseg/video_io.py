"""Video probing, frame iteration, and writing.

Thin OpenCV wrappers that keep source fps and resolution intact so an overlay
video can be diffed against the original frame-for-frame.

Public API:
    VideoInfo              probed metadata (fps, frame count, size, duration)
    probe(path)            read metadata without decoding frames
    iter_frames(path, ...) generator yielding (frame_index, BGR frame)
    VideoWriter            context-managed mp4 writer
    write_synthetic_clip() build a small test clip from image files
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator, Sequence

import cv2
import numpy as np


@dataclass
class VideoInfo:
    """Metadata for a video file.

    Attributes:
        path: source path.
        width, height: frame dimensions in pixels.
        fps: frames per second; falls back to a sane default when the
            container reports 0 or a bogus value.
        frame_count: total frames, or -1 when the container cannot say.
        fourcc: four-character codec code as an int.
    """

    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    fourcc: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.fps <= 0 or self.frame_count < 0:
            return -1.0
        return self.frame_count / self.fps

    def describe(self) -> str:
        count = "unknown" if self.frame_count < 0 else f"{self.frame_count}"
        dur = "unknown" if self.duration_seconds < 0 else f"{self.duration_seconds:.1f}s"
        return (
            f"{os.path.basename(self.path)}: {self.width}x{self.height} @ "
            f"{self.fps:.3f} fps, {count} frames ({dur})"
        )


def _fourcc_to_str(fourcc: int) -> str:
    if not fourcc:
        return "unknown"
    return "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")


def probe(path: str) -> VideoInfo:
    """Read video metadata without decoding any frames.

    Args:
        path: video file path.

    Returns:
        VideoInfo for the file.

    Raises:
        FileNotFoundError: if the path does not exist.
        ValueError: if OpenCV cannot open the file as a video.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"OpenCV could not open {path!r} as a video")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or -1)
    info = VideoInfo(
        path=path,
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=fps if fps > 0 else 30.0,
        frame_count=count,
        fourcc=int(cap.get(cv2.CAP_PROP_FOURCC)),
    )
    cap.release()
    return info


def iter_frames(
    path: str,
    stride: int = 1,
    start: int = 0,
    max_frames: int | None = None,
) -> Iterator[tuple[int, np.ndarray]]:
    """Yield (source_index, BGR frame) pairs.

    Args:
        path: video file path.
        stride: advance this many source frames per yielded frame. Values > 1
            cut runtime proportionally; with --smooth off, stride > 2 makes the
            overlay visibly choppy, so prefer it over a smaller model instead.
        start: first source frame index to decode.
        max_frames: stop after yielding this many frames.

    Yields:
        Tuples of (source frame index, BGR uint8 frame).
    """
    if stride < 1:
        raise ValueError("stride must be >= 1")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"OpenCV could not open {path!r} as a video")

    index = 0
    emitted = 0
    try:
        if start > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start)
            index = start
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if (index - start) % stride == 0:
                yield index, frame
                emitted += 1
                if max_frames is not None and emitted >= max_frames:
                    return
            index += 1
    finally:
        cap.release()


class VideoWriter:
    """Context-managed MP4 writer that mirrors the source geometry.

    Args:
        path: output .mp4 path.
        width, height: output frame size.
        fps: output frame rate, copied from the source video.
        fourcc: codec code, defaults to mp4v.

    Usage:
        with VideoWriter(out, w, h, fps) as writer:
            writer.write(frame)
    """

    def __init__(
        self,
        path: str,
        width: int,
        height: int,
        fps: float,
        fourcc: int | None = None,
    ) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        code = cv2.VideoWriter_fourcc(*"mp4v") if fourcc is None else fourcc
        self.width = int(width)
        self.height = int(height)
        self.writer = cv2.VideoWriter(path, code, fps, (self.width, self.height))
        if not self.writer.isOpened():
            raise RuntimeError(f"could not open VideoWriter for {path!r} (codec {_fourcc_to_str(code)})")
        self.frames_written = 0

    def write(self, frame: np.ndarray) -> None:
        """Append one frame, resizing if it does not match the writer geometry."""
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            frame = cv2.resize(frame, (self.width, self.height))
        self.writer.write(frame)
        self.frames_written += 1

    def close(self) -> None:
        self.writer.release()

    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_synthetic_clip(
    out_path: str,
    images: Sequence[str],
    fps: float = 10.0,
    size: tuple[int, int] | None = None,
) -> str:
    """Assemble image files into a short test clip.

    Lets the full video path be exercised before any real dashcam footage is
    available.

    Args:
        out_path: destination .mp4.
        images: paths to readable images, in order.
        fps: frame rate for the synthetic clip.
        size: (width, height); defaults to the first image's size.

    Returns:
        out_path.

    Raises:
        ValueError: if images is empty or none can be read.
    """
    if not images:
        raise ValueError("no images supplied")
    frames: list[np.ndarray] = []
    target = size
    for path in images:
        img = cv2.imread(path)
        if img is None:
            continue
        if target is None:
            target = (img.shape[1], img.shape[0])
        elif (img.shape[1], img.shape[0]) != target:
            img = cv2.resize(img, target)
        frames.append(img)
    if not frames:
        raise ValueError("none of the supplied images could be read")
    target = target or (frames[0].shape[1], frames[0].shape[0])
    with VideoWriter(out_path, target[0], target[1], fps) as writer:
        for frame in frames:
            writer.write(frame)
    return out_path
