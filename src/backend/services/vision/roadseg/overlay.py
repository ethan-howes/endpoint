"""Rendering road masks for visual inspection.

Produces QC-friendly frames: a translucent road tint over the original image,
a flat mask view, or the two side by side.

Public API:
    ROAD_COLOR_BGR          default road tint
    render_mask()           flat-colour mask image
    overlay_road()          tinted road over the source frame
    compose()               dispatch for the supported layout names
    LAYOUTS                 valid layout names
"""

from __future__ import annotations

import cv2
import numpy as np

ROAD_COLOR_BGR: tuple[int, int, int] = (0, 220, 90)
MASK_BACKGROUND_BGR: tuple[int, int, int] = (18, 18, 18)

LAYOUTS: tuple[str, ...] = ("overlay", "side-by-side", "both")


def render_mask(
    mask: np.ndarray,
    color: tuple[int, int, int] = ROAD_COLOR_BGR,
    background: tuple[int, int, int] = MASK_BACKGROUND_BGR,
) -> np.ndarray:
    """Turn a bool mask into a flat-colour BGR image.

    Args:
        mask: bool array (H, W).
        color: BGR colour for road pixels.
        background: BGR colour for everything else.

    Returns:
        uint8 BGR image (H, W, 3).
    """
    out = np.empty((*mask.shape, 3), dtype=np.uint8)
    out[:, :] = background
    out[mask] = color
    return out


def overlay_road(
    frame: np.ndarray,
    mask: np.ndarray,
    color: tuple[int, int, int] = ROAD_COLOR_BGR,
    alpha: float = 0.45,
) -> np.ndarray:
    """Blend a road tint into the source frame.

    Args:
        frame: source BGR frame.
        mask: bool road mask at the same resolution.
        color: BGR tint colour.
        alpha: blend weight applied to the tint, in [0, 1].

    Returns:
        uint8 BGR image, same size as frame. The input is not modified.

    Raises:
        ValueError: if mask and frame disagree on resolution.
    """
    if mask.shape[:2] != frame.shape[:2]:
        raise ValueError(f"mask {mask.shape[:2]} does not match frame {frame.shape[:2]}")
    out = frame.copy()
    if alpha <= 0:
        return out
    tint = np.array(color, dtype=np.float64)
    blended = frame.astype(np.float64) * (1.0 - alpha) + tint * alpha
    out[mask] = blended[mask].astype(np.uint8)
    return out


def add_label(image: np.ndarray, text: str, origin: tuple[int, int] = (16, 34)) -> np.ndarray:
    """Draw a small caption so a layout is self-describing when reviewed."""
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 0),
        4,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    return image


def compose(
    frame: np.ndarray,
    mask: np.ndarray,
    layout: str = "overlay",
    alpha: float = 0.45,
    caption: str = "",
) -> np.ndarray:
    """Render one QC frame in the requested layout.

    Args:
        frame: source BGR frame.
        mask: bool road mask at the same resolution.
        layout: one of LAYOUTS.
        alpha: tint strength for the overlay.
        caption: optional text drawn on the result.

    Returns:
        uint8 BGR image. "side-by-side" is twice as wide as frame, the others
        match frame's size.

    Raises:
        ValueError: on an unknown layout.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; choose from {LAYOUTS}")

    tinted = overlay_road(frame, mask, alpha=alpha)
    if layout == "overlay":
        result = tinted
    elif layout == "side-by-side":
        flat = render_mask(mask)
        result = np.hstack([frame, flat])
    else:
        flat = render_mask(mask)
        gap = np.full((frame.shape[0], 6, 3), 40, dtype=np.uint8)
        result = np.hstack([tinted, gap, flat])

    if caption:
        add_label(result, caption)
    return result
