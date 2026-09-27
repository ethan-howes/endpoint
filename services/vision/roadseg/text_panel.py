"""Render Gemini's reasoning as a text panel beside the segmentation video.

The panel is the "live stream" in the demo: as playback passes each timestamp
that Gemini gave, its line appears. Nothing is typed out character by
character -- the whole point is that the answer is already decided and we are
merely replaying it on a clock, so the demo is repeatable and does not need the
API at showtime.

Pillow does the text work. OpenCV's Hershey fonts cannot word-wrap and only
draw one line per call, which for a paragraph of prose is a non-starter.

The expensive part is layout, so the panel is a pure function of
(len(events revealed), size) and the caller is expected to cache it. Across a
36 s clip the reveal count changes about 37 times against 1083 frames, so
re-rendering on change rather than per frame is a ~30x saving on the dominant
cost of the composite.

Public API:
    PanelStyle     colours, font sizes, margins
    render_panel() events revealed so far -> BGR ndarray for VideoWriter
    default_style() the tuned-for-972px style
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Regular, bold and oblique faces, in preference order. DejaVu is the Debian
# default and is present on this box; Lato is the fallback. Pillow ships no
# fonts of its own, so there is no last-resort bundled face to rely on.
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "regular": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
    ),
    "bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
    ),
    "oblique": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Italic.ttf",
    ),
}


@dataclass
class PanelStyle:
    """Appearance of the panel.

    Attributes:
        background: BGR panel fill.
        title: BGR heading text.
        thought: BGR thought-summary text.
        body: BGR text for a revealed line.
        current: BGR accent for the most recently revealed line.
        past: BGR for lines already superseded.
        rule: BGR for the horizontal separator.
        accent_bar: BGR for the marker beside the current line.
        title_size, thought_size, label_size, body_size: font sizes in px.
        margin: left/right padding in px.
        line_gap: extra px between wrapped lines of one block.
        block_gap: extra px between events.
        thought_max_lines: cap on thought-summary lines; the rest is elided.
    """

    background: tuple[int, int, int] = (26, 24, 22)
    title: tuple[int, int, int] = (150, 150, 150)
    thought: tuple[int, int, int] = (185, 175, 160)
    body: tuple[int, int, int] = (235, 235, 235)
    current: tuple[int, int, int] = (120, 245, 160)
    past: tuple[int, int, int] = (128, 124, 120)
    rule: tuple[int, int, int] = (58, 56, 54)
    accent_bar: tuple[int, int, int] = (90, 220, 120)
    title_size: int = 26
    thought_size: int = 21
    label_size: int = 23
    body_size: int = 25
    margin: int = 26
    line_gap: int = 7
    block_gap: int = 26
    thought_max_lines: int = 7
    title_text: str = "GEMINI  ·  where should the car stop?"
    fonts: dict[str, ImageFont.FreeTypeFont] = field(default_factory=dict)


def bgr_to_rgb(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Convert an OpenCV BGR triple to the RGB triple Pillow wants.

    Args:
        color: (blue, green, red), each 0-255.

    Returns:
        The same triple with the outer components swapped.
    """
    return (color[2], color[1], color[0])


def load_fonts(style: PanelStyle) -> PanelStyle:
    """Fill in `style.fonts`, mutating and returning the style.

    Args:
        style: style to populate.

    Returns:
        The same style, with a loaded font for each of regular/bold/oblique.

    Raises:
        FileNotFoundError: when no usable font file is found, naming the paths
            that were tried. Pillow's fallback bitmap font is deliberately not
            used: it cannot scale to a 25 px body and would silently produce an
            unreadable panel.
    """
    sizes = {
        "regular": style.body_size,
        "bold": style.label_size,
        "oblique": style.thought_size,
    }
    for face, paths in FONT_CANDIDATES.items():
        for path in paths:
            if os.path.exists(path):
                style.fonts[face] = ImageFont.truetype(path, sizes[face])
                break
        else:
            raise FileNotFoundError(
                f"no usable {face} font found; tried: {', '.join(paths)}. "
                f"Install fonts-dejavu-core, or extend FONT_CANDIDATES."
            )
    return style


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    """Measure a single line's width.

    Args:
        draw: a draw context, used for the font's metrics.
        text: the string to measure.
        font: the font to measure in.

    Returns:
        Width in pixels.
    """
    return draw.textlength(text, font=font)


def wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Word-wrap text to a pixel width.

    Args:
        draw: a draw context, used for measuring.
        text: the string to wrap. Embedded newlines start a new line.
        font: the font to wrap in.
        max_width: wrap width in pixels.

    Returns:
        Wrapped lines, none longer than max_width unless a single word is
        itself wider, in which case it is left overlong rather than chopped.
    """
    lines: list[str] = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _elide(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    limit: int,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Cap a block to `limit` lines, trimming the last to fit an ellipsis.

    Args:
        draw: a draw context, used for measuring.
        lines: the wrapped lines.
        limit: maximum lines to keep; 0 or less means no cap.
        font: font the lines are drawn in.
        max_width: the block's wrap width in pixels.

    Returns:
        The lines, shortened to `limit`. The final kept line is trimmed word by
        word until "... " fits within max_width, so the marker never wraps.
    """
    if limit <= 0 or len(lines) <= limit:
        return lines
    kept = list(lines[:limit])
    words = kept[-1].split()
    while words:
        candidate = " ".join(words) + " ..."
        if text_width(draw, candidate, font) <= max_width:
            kept[-1] = candidate
            break
        words.pop()
    else:
        kept[-1] = "..."
    return kept


def render_panel(
    events: Sequence[dict],
    revealed: int,
    size: tuple[int, int],
    style: PanelStyle | None = None,
    *,
    thought_summary: str = "",
    waiting_note: str = "waiting for the model's read...",
) -> np.ndarray:
    """Render the panel showing the first `revealed` events.

    The reveal is the whole mechanism: events are drawn oldest-first, the most
    recently revealed one is highlighted, and anything beyond `revealed` is not
    drawn at all. There is no future state to leak, so a screenshot of frame N
    is exactly what the panel said at time N.

    Args:
        events: the full event list, each a dict with keys timestamp, label,
            text. The whole list is needed even though only a prefix is drawn,
            because total count drives the "n of m" counter.
        revealed: how many events to show. Clamped to [0, len(events)].
        size: (width, height) of the panel in pixels.
        style: appearance; default_style() is used when None.
        thought_summary: Gemini's thought summary, shown as a static header.
        waiting_note: placeholder shown while revealed == 0 and there is
            nothing else to display.

    Returns:
        uint8 BGR ndarray of shape (height, width, 3), ready for
        roadseg.video_io.VideoWriter.

    Raises:
        ValueError: when size is degenerate.
    """
    width, height = size
    if width < 80 or height < 80:
        raise ValueError(f"panel too small to hold text: {width}x{height}")

    style = style or default_style()
    if not style.fonts:
        style = load_fonts(style)
    body_font = style.fonts["regular"]
    label_font = style.fonts["bold"]
    thought_font = style.fonts["oblique"]
    title_font = label_font

    image = Image.new("RGB", (width, height), bgr_to_rgb(style.background))
    draw = ImageDraw.Draw(image)

    inner = width - 2 * style.margin
    y = style.margin

    draw.text(
        (style.margin, y),
        style.title_text,
        font=title_font,
        fill=bgr_to_rgb(style.title),
    )
    y += style.title_size + style.line_gap * 2

    if thought_summary:
        header = _elide(draw, wrap(draw, thought_summary, thought_font, inner),
                        style.thought_max_lines, thought_font, inner)
        for line in header:
            draw.text(
                (style.margin, y),
                line,
                font=thought_font,
                fill=bgr_to_rgb(style.thought),
            )
            y += style.thought_size + style.line_gap
        y += style.block_gap
    elif revealed <= 0:
        draw.text(
            (style.margin, y),
            wrap(draw, waiting_note, body_font, inner)[0],
            font=body_font,
            fill=bgr_to_rgb(style.past),
        )
        y += style.body_size + style.line_gap
        y += style.block_gap

    y += 2
    draw.line(
        [(style.margin, y), (width - style.margin, y)],
        fill=bgr_to_rgb(style.rule),
        width=2,
    )
    y += style.block_gap

    shown = max(0, min(int(revealed), len(events)))
    rendered: list[list[str]] = []
    counter_height = style.body_size + style.line_gap

    # Measure from the newest backwards, so that if the panel overflows it drops
    # the OLDEST lines rather than the newest. A log that forgets your oldest
    # entry is correct; one that hides the newest is worse than useless.
    #
    # The counter's row is subtracted from the budget. Without that, a full
    # panel runs its last line down into the counter's band and the two overlap:
    # the counter is right-aligned at height - margin - body_size, and the body
    # wraps to inner - label_size, so both claim the same pixels.
    budget = height - y - style.margin - counter_height
    for index in range(shown - 1, -1, -1):
        event = events[index]
        body_lines = wrap(draw, str(event.get("text", "")), body_font,
                          inner - style.label_size)
        needed = (style.body_size + style.line_gap) * (1 + len(body_lines))
        needed += style.block_gap
        if needed > budget:
            break
        budget -= needed
        rendered.append(body_lines)
    rendered.reverse()
    first = shown - len(rendered)

    for offset, body_lines in enumerate(rendered):
        index = first + offset
        event = events[index]
        is_current = index == shown - 1
        body_colour = style.current if is_current else style.past
        label_colour = style.current if is_current else style.past

        if is_current:
            draw.rectangle(
                [
                    (style.margin // 2, y - 2),
                    (style.margin // 2 + 5, y + style.body_size + 4),
                ],
                fill=bgr_to_rgb(style.accent_bar),
            )

        stamp = f"[{event.get('timestamp', '--:--')}]"
        tag = str(event.get("label", "NOTE"))
        stamp_w = text_width(draw, stamp, label_font)
        draw.text(
            (style.margin, y), stamp, font=label_font, fill=bgr_to_rgb(label_colour)
        )
        draw.text(
            (style.margin + stamp_w + 10, y),
            tag,
            font=label_font,
            fill=bgr_to_rgb(label_colour),
        )
        y += style.body_size + style.line_gap
        for line in body_lines:
            draw.text(
                (style.margin, y), line, font=body_font, fill=bgr_to_rgb(body_colour)
            )
            y += style.body_size + style.line_gap
        y += style.block_gap

    if len(events) > 0:
        # Report the range actually on screen, not the number revealed. A panel
        # showing the last 6 of 17 revealed events must not claim "17 of 17",
        # or the viewer counts lines that were never drawn and concludes the
        # panel dropped the newest, which is the opposite of what happened.
        if first > 0:
            counter = f"{first + 1}-{shown} of {len(events)}"
        else:
            counter = f"{shown} of {len(events)}"
        draw.text(
            (width - style.margin - text_width(draw, counter, body_font),
             height - style.margin - style.body_size),
            counter,
            font=body_font,
            fill=bgr_to_rgb(style.title),
        )

    return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def default_style() -> PanelStyle:
    """Build the style tuned for a 3460x972 segmentation overlay's height.

    Returns:
        A PanelStyle with fonts already loaded.
    """
    return load_fonts(PanelStyle())
