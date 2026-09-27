"""Model presets for road segmentation.

Maps a short preset name to a HuggingFace Mask2Former repo plus the class names
that should count as "road". Class names are matched case-insensitively against
the model's TOP-LEVEL ``config.id2label`` (the segmentation label set), never
against ``config.backbone_config.id2label`` (ImageNet-1k, unrelated).

Public API:
    ModelPreset        named tuple describing one checkpoint + its road classes
    PRESETS            dict[str, ModelPreset] keyed by preset name
    get_preset(name)   look up a preset by name, raising a helpful error
    resolve_road_ids() map class names in id2label to integer class ids
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ModelPreset:
    """One Mask2Former checkpoint and how to read a road mask out of it.

    Attributes:
        repo: HuggingFace model id to load.
        road_classes: class names treated as road. A pixel is road when the
            model's argmax lands on one of these.
        extra_road_classes: names that can optionally be folded into the road
            set, offered by the CLI but off by default.
        notes: short human-readable description.
    """

    repo: str
    road_classes: tuple[str, ...]
    extra_road_classes: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    @property
    def label(self) -> str:
        return self.repo.rsplit("/", 1)[-1]


PRESETS: dict[str, ModelPreset] = {
    "mapillary": ModelPreset(
        repo="facebook/mask2former-swin-large-mapillary-vistas-semantic",
        road_classes=("Road", "Service Lane"),
        extra_road_classes=("Bike Lane",),
        notes=(
            "Mapillary Vistas: street-level driving imagery, closest domain "
            "match to a dashcam. 65 classes with a rich drivable-surface "
            "taxonomy (Road, Service Lane, Bike Lane, Curb, Sidewalk)."
        ),
    ),
    "cityscapes": ModelPreset(
        repo="facebook/mask2former-swin-large-cityscapes-semantic",
        road_classes=("road",),
        extra_road_classes=(),
        notes=(
            "Cityscapes: 19-class urban street scenes, 'road' is the primary "
            "stuff class. Cleaner and more reliably validated than Mapillary "
            "on cluttered urban road, but sees a narrower slice of surface."
        ),
    ),
    "ade": ModelPreset(
        repo="facebook/mask2former-swin-large-ade-semantic",
        road_classes=("road",),
        extra_road_classes=("sidewalk",),
        notes=(
            "ADE20K: 150 general-scene classes. Weakest match for driving "
            "footage but most likely to survive odd surfaces (gravel, dirt, "
            "unusual viewpoints) that the driving datasets never saw."
        ),
    ),
}


def get_preset(name: str) -> ModelPreset:
    """Look up a preset by name.

    Args:
        name: preset key, e.g. "mapillary".

    Returns:
        The matching ModelPreset.

    Raises:
        KeyError: if the name is not a known preset.
    """
    key = name.strip().lower()
    if key not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; choose from {sorted(PRESETS)}")
    return PRESETS[key]


def resolve_road_ids(
    id2label: Mapping[int | str, str],
    names: Sequence[str],
) -> tuple[tuple[int, ...], list[str]]:
    """Resolve class names to integer ids against a label map.

    Matching is case-insensitive and whitespace-trimmed, because checkpoints
    are inconsistent ("Road" in Mapillary, "road" in Cityscapes/ADE20K).

    Args:
        id2label: the model's top-level id2label mapping.
        names: class names to resolve.

    Returns:
        Tuple of (resolved ids sorted ascending, names that were not found).

    Raises:
        ValueError: if none of the requested names exist in id2label.
    """
    lookup = {str(label).strip().lower(): int(idx) for idx, label in id2label.items()}
    resolved: set[int] = set()
    missing: list[str] = []
    for name in names:
        key = name.strip().lower()
        if key in lookup:
            resolved.add(lookup[key])
        else:
            missing.append(name)
    if names and not resolved:
        preview = sorted({str(v) for v in id2label.values()})[:8]
        raise ValueError(
            f"none of {list(names)} exist in this model's label set "
            f"(num_labels={len(id2label)}, first labels: {preview}...)"
        )
    return tuple(sorted(resolved)), missing
