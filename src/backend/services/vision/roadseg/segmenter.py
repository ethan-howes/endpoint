"""Mask2Former wrapper that turns video frames into road masks.

Loads a pretrained Mask2Former universal-segmentation checkpoint, runs
semantic segmentation, and collapses the model's class output into a
road/non-road decision plus a per-pixel road confidence.

Public API:
    RoadMask                  per-frame result: bool mask + float confidence
    RoadSegmenter.from_preset(name, ...)   build from a PRESETS key
    RoadSegmenter(repo, ...)  build from an explicit HF repo id
    .segment(frames)          list[BGR frame] -> list[RoadMask]
    .road_ids                 resolved road class ids
    .label_names              model's full top-level label set
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

import numpy as np
import torch
from transformers import AutoImageProcessor, Mask2FormerForUniversalSegmentation

from .config import ModelPreset, get_preset, resolve_road_ids


@dataclass
class RoadMask:
    """Road segmentation for a single frame.

    Attributes:
        mask: bool array (H, W), True where the pixel is road.
        confidence: float32 array (H, W), road-class score in [0, 1]. Only
            meaningful where the model considered road at all; elsewhere it
            reflects the best road class even if another class won the argmax.
        class_ids: the road class ids this segmenter resolved to.
        argmax_id: int16 array (H, W) of the winning class per pixel. Kept for
            debugging and for picking a different decision rule later.
    """

    mask: np.ndarray
    confidence: np.ndarray
    class_ids: tuple[int, ...]
    argmax_id: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.mask.shape

    @property
    def coverage(self) -> float:
        """Fraction of the frame classified as road, in [0, 1]."""
        if self.mask.size == 0:
            return 0.0
        return float(self.mask.mean())


class RoadSegmenter:
    """Runs Mask2Former over BGR frames and extracts road masks.

    Args:
        repo: HuggingFace model id, e.g.
            "facebook/mask2former-swin-large-mapillary-vistas-semantic".
        road_classes: class names counting as road. Defaults to the preset's
            road_classes when the repo matches a preset, else ("road",).
        device: torch device. Defaults to cuda when available.
        amp: run the forward pass under fp16 autocast on CUDA.
        shortest_edge / longest_edge: override the processor's resize
            shortlist. Lower values are faster but lose thin structures
            (lane markings, distant road edges).
    """

    def __init__(
        self,
        repo: str,
        road_classes: Sequence[str] | None = None,
        device: str | None = None,
        amp: bool = True,
        shortest_edge: int | None = None,
        longest_edge: int | None = None,
    ) -> None:
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.amp = bool(amp) and self.device.type == "cuda"
        self.repo = repo

        load_start = time.perf_counter()
        self.processor = AutoImageProcessor.from_pretrained(repo)
        self.model = Mask2FormerForUniversalSegmentation.from_pretrained(repo)
        self.model.to(self.device)
        self.model.eval()
        self.load_seconds = time.perf_counter() - load_start

        self.id2label = self._top_level_id2label(self.model.config)
        self.label2id = {int(idx): str(name) for idx, name in self.id2label.items()}

        if road_classes is None:
            road_classes = self._default_road_classes(repo)
        self.road_class_names = tuple(road_classes)
        self.road_ids, self.missing_road_classes = resolve_road_ids(
            self.id2label, self.road_class_names
        )

        self._size_override = None
        if shortest_edge is not None or longest_edge is not None:
            base = self.processor.size or {}
            self._size_override = {
                "shortest_edge": shortest_edge or base.get("shortest_edge", 800),
                "longest_edge": longest_edge or base.get("longest_edge", 1333),
            }

    @staticmethod
    def _top_level_id2label(config) -> dict[int, str]:
        """Return the segmentation label set, never the backbone's.

        The backbone carries an ImageNet-1k classifier head whose id2label is
        1000 entries wide and contains decoy substrings ("all-terrain bike,
        off-roader"). Reading it would silently produce wrong class ids.
        """
        id2label = getattr(config, "id2label", None)
        if not id2label:
            raise ValueError("model config carries no top-level id2label")
        return {int(k): str(v) for k, v in id2label.items()}

    @staticmethod
    def _default_road_classes(repo: str) -> tuple[str, ...]:
        for preset in (get_preset("mapillary"), get_preset("cityscapes"), get_preset("ade")):
            if preset.repo == repo:
                return preset.road_classes
        return ("road",)

    @classmethod
    def from_preset(
        cls,
        name: str,
        road_classes: Sequence[str] | None = None,
        **kwargs,
    ) -> "RoadSegmenter":
        """Build a segmenter from a preset name in roadseg.config.PRESETS."""
        preset: ModelPreset = get_preset(name)
        return cls(preset.repo, road_classes=road_classes, **kwargs)

    @property
    def num_labels(self) -> int:
        return len(self.id2label)

    def describe_road_classes(self) -> str:
        parts = [f"{self.label2id[i]}({i})" for i in self.road_ids]
        text = ", ".join(parts)
        if self.missing_road_classes:
            text += f" | not found in this model: {self.missing_road_classes}"
        return text

    def _preprocess(self, frames: Sequence[np.ndarray]) -> dict:
        images = [self._to_rgb(f) for f in frames]
        kwargs = {"return_tensors": "pt"}
        if self._size_override is not None:
            kwargs["size"] = self._size_override
        return self.processor(images=images, **kwargs)

    @staticmethod
    def _to_rgb(frame: np.ndarray) -> np.ndarray:
        """OpenCV hands back BGR; Mask2Former's processor expects RGB."""
        import cv2

        if frame.ndim == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    @torch.inference_mode()
    def segment(
        self,
        frames: Sequence[np.ndarray],
        min_confidence: float = 0.0,
    ) -> list[RoadMask]:
        """Segment a batch of BGR frames into road masks.

        Args:
            frames: BGR uint8 frames. All must share one resolution for the
                batched post-process to be meaningful.
            min_confidence: drop pixels whose road-class score falls below this.
                0.0 keeps every pixel the model assigned to a road class.

        Returns:
            One RoadMask per input frame, at each frame's original resolution.
        """
        if not frames:
            return []
        sizes = [(int(f.shape[0]), int(f.shape[1])) for f in frames]
        inputs = self._preprocess(frames)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        if self.amp:
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                outputs = self.model(**inputs)
        else:
            outputs = self.model(**inputs)

        processed = self.processor.post_process_semantic_segmentation(
            outputs, target_sizes=sizes, return_segmentation_scores=True
        )

        road_index = torch.tensor(self.road_ids, device=self.device, dtype=torch.long)
        results: list[RoadMask] = []
        for item in processed:
            seg = item["segmentation"].to(self.device)
            scores = item["segmentation_scores"]
            road_scores = scores.index_select(0, road_index).float()
            confidence = road_scores.max(dim=0).values
            is_road = torch.isin(seg, road_index)
            if min_confidence > 0.0:
                is_road = is_road & (confidence >= min_confidence)
            results.append(
                RoadMask(
                    mask=is_road.cpu().numpy(),
                    confidence=confidence.cpu().numpy().astype(np.float32),
                    class_ids=self.road_ids,
                    argmax_id=seg.to(torch.int16).cpu().numpy(),
                )
            )
        return results


def _warmup(segmenter: RoadSegmenter, shape: tuple[int, int] = (720, 1280)) -> None:
    """Run one throwaway forward pass so later timings exclude lazy CUDA init."""
    dummy = np.zeros(shape, dtype=np.uint8)
    segmenter.segment([dummy])
