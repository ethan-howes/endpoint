"""End-to-end video -> road-mask-overlay pipeline.

Reads a video, segments it with Mask2Former, renders QC frames, and writes a
new video that preserves the source geometry and real-time duration.

Public API:
    PipelineConfig   every tunable knob
    PipelineResult   what a run produced
    TemporalSmoother optional exponential moving average over frames
    run()            execute the pipeline
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import islice
from typing import Iterator, Sequence

import numpy as np

from .config import PRESETS, get_preset
from .overlay import LAYOUTS, compose
from .segmenter import RoadMask, RoadSegmenter, _warmup
from .video_io import VideoInfo, VideoWriter, iter_frames, probe


@dataclass
class PipelineConfig:
    """Settings for a segmentation run.

    Attributes:
        model: preset name (see roadseg.config.PRESETS) or a raw HF repo id.
        road_classes: override the preset's road class names.
        layout: overlay render style, one of roadseg.overlay.LAYOUTS.
        stride: process every Nth source frame. Output fps is divided by this
            so the result still plays back in real time.
        batch_size: frames per model forward pass.
        min_confidence: road-score floor, in [0, 1]. Raise to suppress speckle.
        smooth_alpha: if set, EMA factor in (0, 1] applied across frames to
            reduce flicker. See TemporalSmoother for the motion caveat.
        shortest_edge / longest_edge: processor resize override; lowering these
            trades mask detail for throughput.
        max_frames: stop after this many processed frames.
        device: torch device override, e.g. "cuda" or "cpu".
        amp: enable fp16 autocast on CUDA.
        caption_prefix: text drawn on each output frame.
    """

    model: str = "mapillary"
    road_classes: Sequence[str] | None = None
    layout: str = "overlay"
    stride: int = 1
    batch_size: int = 4
    min_confidence: float = 0.0
    smooth_alpha: float | None = None
    shortest_edge: int | None = None
    longest_edge: int | None = None
    max_frames: int | None = None
    device: str | None = None
    amp: bool = True
    caption_prefix: str = ""

    def __post_init__(self) -> None:
        if self.layout not in LAYOUTS:
            raise ValueError(f"unknown layout {self.layout!r}; choose from {LAYOUTS}")
        if self.stride < 1:
            raise ValueError("stride must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        if self.smooth_alpha is not None and not 0.0 < self.smooth_alpha <= 1.0:
            raise ValueError("smooth_alpha must be in (0, 1] when set")


@dataclass
class PipelineResult:
    """Summary of a completed run.

    Attributes:
        output_path: written video.
        frames_written: frames actually encoded.
        frames_read: frames consumed from the source.
        source: metadata about the input video.
        elapsed_seconds: wall time for segmentation plus writing.
        mean_coverage: average fraction of pixels classified as road.
        coverage_series: per-frame road coverage, for spotting flicker.
        segmenter: the loaded segmenter, for label reporting.
    """

    output_path: str
    frames_written: int
    frames_read: int
    source: VideoInfo
    elapsed_seconds: float
    mean_coverage: float
    coverage_series: list[float] = field(default_factory=list)
    segmenter: RoadSegmenter | None = None

    def throughput(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.frames_written / self.elapsed_seconds


class TemporalSmoother:
    """Exponential moving average over the road mask.

    Reduces per-frame flicker, which is the usual complaint about per-frame
    segmentation on video.

    Caveat: this is a pixel-space average with no motion compensation, so under
    a forward-moving camera it lags and smears fast-changing geometry. It
    smooths the picture without making the model more accurate. Off by default.
    """

    def __init__(self, alpha: float, shape: tuple[int, int] | None = None) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = float(alpha)
        self._state: np.ndarray | None = None
        self._shape = shape

    def reset(self) -> None:
        self._state = None

    def __call__(self, mask: np.ndarray) -> np.ndarray:
        current = mask.astype(np.float32)
        if self._state is None or self._state.shape != current.shape:
            self._state = current.copy()
            return current >= 0.5
        self._state = self.alpha * current + (1.0 - self.alpha) * self._state
        return self._state >= 0.5


def _chunks(items: Iterator, size: int) -> Iterator[list]:
    while True:
        batch = list(islice(items, size))
        if not batch:
            return
        yield batch


def resolve_model(model: str) -> str:
    """Accept either a preset name or a raw HuggingFace repo id.

    Raises:
        ValueError: if the value is neither a known preset nor a repo id.
            A bare name is treated as a preset typo, since repo ids always
            contain a namespace separator.
    """
    try:
        return get_preset(model).repo
    except KeyError:
        if "/" in model:
            return model
        raise ValueError(
            f"unknown model preset {model!r}; choose from "
            f"{sorted(PRESETS)} or pass a full 'org/name' repo id"
        ) from None


def run(
    video_path: str,
    output_path: str,
    config: PipelineConfig | None = None,
    segmenter: RoadSegmenter | None = None,
) -> PipelineResult:
    """Segment a video and write a road-overlay video.

    Args:
        video_path: input video.
        output_path: destination video. Its fps is source fps divided by
            stride, so strided output still plays at real-world speed.
        config: pipeline settings, defaults to PipelineConfig().
        segmenter: a preloaded segmenter, to avoid paying model load twice when
            comparing presets on the same clip.

    Returns:
        PipelineResult describing the run.
    """
    cfg = config or PipelineConfig()
    source = probe(video_path)
    repo = resolve_model(cfg.model)

    if segmenter is None:
        segmenter = RoadSegmenter(
            repo,
            road_classes=cfg.road_classes,
            device=cfg.device,
            amp=cfg.amp,
            shortest_edge=cfg.shortest_edge,
            longest_edge=cfg.longest_edge,
        )
    if segmenter.repo != repo:
        raise ValueError(
            f"passed segmenter was built for {segmenter.repo!r}, "
            f"but this run asked for {repo!r}"
        )

    _warmup(segmenter)
    smoother = TemporalSmoother(cfg.smooth_alpha) if cfg.smooth_alpha else None

    out_w = source.width * (2 if cfg.layout in ("side-by-side", "both") else 1) + (
        6 if cfg.layout == "both" else 0
    )
    out_fps = source.fps / cfg.stride

    stream = iter_frames(
        video_path,
        stride=cfg.stride,
        max_frames=cfg.max_frames,
    )
    pairs: Iterator[tuple[int, np.ndarray]] = stream
    batched = _chunks(pairs, cfg.batch_size)

    start = time.perf_counter()
    written = 0
    read = 0
    coverages: list[float] = []
    with VideoWriter(output_path, out_w, source.height, out_fps) as writer:
        for batch in batched:
            indices = [idx for idx, _ in batch]
            frames = [f for _, f in batch]
            masks: list[RoadMask] = segmenter.segment(frames, cfg.min_confidence)
            read += len(frames)
            for index, frame, rm in zip(indices, frames, masks):
                mask = smoother(rm.mask) if smoother else rm.mask
                coverage = float(mask.mean()) if mask.size else 0.0
                coverages.append(coverage)
                caption = f"{cfg.caption_prefix} f{index} road={coverage * 100:4.1f}%".strip()
                writer.write(compose(frame, mask, layout=cfg.layout, caption=caption))
                written += 1
            print(
                f"  frames {read:6d}  coverage={np.mean(coverages[-cfg.batch_size:]) * 100:5.1f}%  "
                f"{read / max(time.perf_counter() - start, 1e-9):5.2f} fps",
                end="\r",
                flush=True,
            )
    elapsed = time.perf_counter() - start

    return PipelineResult(
        output_path=output_path,
        frames_written=written,
        frames_read=read,
        source=source,
        elapsed_seconds=elapsed,
        mean_coverage=float(np.mean(coverages)) if coverages else 0.0,
        coverage_series=coverages,
        segmenter=segmenter,
    )
