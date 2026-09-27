"""Ask Gemini where an AV should stop to pick up a passenger, on a dashcam clip.

Uploads a video through the Gemini Files API, asks a single question framed as
an autonomous-vehicle parking decision, and writes the answer to JSON as a
timestamped event list that scripts/compose_demo.py replays alongside a
segmentation overlay.

The output is deliberately tiny: one line for rain cover, one for
accessibility, one recommendation, and at most two notes. It is meant to be
read on a video panel, not studied.

Usage:
    python scripts/ask_gemini.py assets/samples/fiu-awning-dash-view.mov
    python scripts/ask_gemini.py clip.mov --dry-run          # no API call
    python scripts/ask_gemini.py clip.mov --windows 6        # tighter timestamps
    python scripts/ask_gemini.py clip.mov --out-dir results/gemini

Reads the API key from secrets.env via roadseg.secrets; no key ever appears in
argv, so the process list stays clean.

Requires google-genai, which is not needed by the segmentation pipeline:

    uv pip install --python /home/yart/projects/endpoint/.venv/bin/python google-genai
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roadseg.secrets import get_secret, load_secrets
from roadseg.video_io import probe

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_OUT_DIR = os.path.join("results", "gemini")
POLL_INTERVAL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 900.0

MIME_BY_SUFFIX = {
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
}

SYSTEM_INSTRUCTION = """\
You are the planning module of an autonomous vehicle driving in light rain. \
You are watching forward-facing dashcam footage and must decide where the \
vehicle should stop to pick up a passenger.

Judge only what the camera actually shows. Never invent a measurement you \
could not read off the footage, and say so plainly when a criterion cannot be \
judged at all.

Two criteria, which can conflict:

1. Rain cover for the waiting passenger. Is there an awning, canopy, \
overhang, arcade, or building recess that would keep rain off the spot where \
the passenger stands?

2. Accessibility. Is the kerb there low, cut, or absent? A flush kerb, a \
driveway apron, or a dropped kerb near a crossing is far better than a tall \
one. Avoid spots where the passenger would have to step down into the roadway \
or climb a high kerb.

Only propose places a car could actually legally stop: at the side of the \
road, not in a travel lane, not in a junction, not blocking anything.

Use only the visual stream. Ignore any audio entirely."""

ANSWER_FORMAT = """\
Answer with EXACTLY these lines and nothing else. No preamble, no closing \
summary, no bullet nesting, no markdown headings.

[MM:SS] RAIN COVER - <the single best-covered spot, and what covers it>
[MM:SS] ACCESSIBILITY - <the single most accessible kerb, and why it is easy>
[MM:SS] RECOMMENDATION - <the ONE spot to stop at, trading the two off>
[MM:SS] NOTE - <optional, at most two of these, only if something changes>

Timestamps are MM:SS measured from the start of the clip, and must fall \
within 00:00 to {last}. If the best rain cover and the best accessibility are \
the same spot, say so on the RECOMMENDATION line. If a criterion is \
unjudgeable from the footage, write that on its line instead of guessing. Each \
line must stay under about 20 words."""


@dataclass
class Event:
    """One timestamped line of the answer, for the demo panel to reveal.

    Attributes:
        timestamp: the MM:SS string exactly as the model wrote it.
        t_seconds: the same instant in seconds, clamped into the clip.
        label: the ALL-CAPS tag the model used, or "NOTE".
        text: the remainder of the line, with the timestamp and label removed.
    """

    timestamp: str
    t_seconds: float
    label: str
    text: str


def format_timestamp(seconds: float) -> str:
    """Render whole seconds as MM:SS.

    Args:
        seconds: offset from the start of the clip.

    Returns:
        Zero-padded MM:SS, e.g. 7.4 -> "00:07".
    """
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_events(text: str, duration: float) -> list[Event]:
    """Pull `[MM:SS] LABEL - body` lines out of the model's answer.

    Deliberately forgiving. A model asked for a rigid format will still drift,
    so this scans line by line for the first MM:SS on each line, treats any
    leading run of capital letters as the label, and keeps the rest as text.
    Lines with no timestamp are not events; they stay in the answer prose.

    Timestamps past the end of the clip are clamped to the last second rather
    than dropped, so a slightly-late final note is still shown instead of
    silently vanishing.

    Args:
        text: the model's answer text.
        duration: clip length in seconds, used to clamp.

    Returns:
        Events sorted by time, earliest first.
    """
    stamp = re.compile(r"\b(\d{1,2}):(\d{2})\b")
    # Leading/trailing emphasis is tolerated because models wrap labels in
    # markdown even when told not to: "**ACCESSIBILITY**" parses as ACCESSIBILITY.
    label = re.compile(r"^[*_`\s]*([A-Z][A-Z /&]{1,24}?)\s*[*_`]?\s*[-–—:]\s*")
    events: list[Event] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*• \t")
        if not line:
            continue
        found = stamp.search(line)
        if not found:
            continue
        seconds = int(found.group(1)) * 60 + int(found.group(2))
        seconds = max(0, min(seconds, max(0, int(duration))))
        rest = line[found.end() :].strip()
        rest = rest.lstrip("] \t")
        match = label.match(rest)
        if match:
            tag = match.group(1).strip()
            body = rest[match.end() :].strip()
        else:
            tag = "NOTE"
            body = rest
        body = body.strip(" -–—:\t")
        if not body:
            continue
        events.append(
            Event(
                timestamp=format_timestamp(seconds),
                t_seconds=float(seconds),
                label=tag,
                text=body,
            )
        )
    events.sort(key=lambda e: e.t_seconds)
    return events


def build_prompt(duration: float) -> str:
    """Build the per-request user prompt.

    Args:
        duration: clip length in seconds, injected so the model cannot
            timestamp past the end.

    Returns:
        The user-turn text.
    """
    return (
        f"This clip is {duration:.1f} seconds of forward-facing dashcam footage "
        f"from a vehicle driving along a city street in light rain.\n\n"
        + ANSWER_FORMAT.format(last=format_timestamp(duration))
    )


def upload_video(client, path: str, mime_type: str, *, verbose: bool = True):
    """Upload a video through the Files API and wait until it is usable.

    Video processing happens server-side, so a freshly uploaded file is in
    state PROCESSING and cannot be referenced by a generate_content call yet.
    Polling for ACTIVE is mandatory; a FAILED state means the container or
    codec was rejected and is reported rather than retried.

    Args:
        client: an initialised google.genai Client.
        path: local video path.
        mime_type: MIME type to declare, e.g. "video/quicktime".
        verbose: print progress while polling.

    Returns:
        The ACTIVE google.genai File.

    Raises:
        RuntimeError: when processing fails or does not finish in time.
    """
    from google.genai import types

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if verbose:
        print(f"uploading {os.path.basename(path)} ({size_mb:.1f} MB, {mime_type})...")
    uploaded = client.files.upload(
        file=path, config=types.UploadFileConfig(mime_type=mime_type)
    )
    name = uploaded.name
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        if uploaded.state is not None and uploaded.state.name == "FAILED":
            raise RuntimeError(
                f"Gemini failed to process {os.path.basename(path)}; the file "
                f"state is FAILED. The container or video codec was most likely "
                f"rejected. Re-encoding to H.264 mp4 is the usual fix."
            )
        if uploaded.state is not None and uploaded.state.name == "ACTIVE":
            break
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Gemini did not finish processing {os.path.basename(path)} "
                f"within {POLL_TIMEOUT_SECONDS / 60:.0f} min (last state: "
                f"{uploaded.state})"
            )
        if verbose:
            print(f"  processing... state={uploaded.state}")
        time.sleep(POLL_INTERVAL_SECONDS)
        uploaded = client.files.get(name=name)
    if verbose:
        print(f"  ready: {uploaded.name} -> {uploaded.uri}")
    return uploaded


def video_part(uploaded, *, start: float | None = None, end: float | None = None):
    """Build a Part referencing the uploaded video, optionally windowed.

    Args:
        uploaded: the ACTIVE File from upload_video().
        start: window start in seconds, or None for the whole clip.
        end: window end in seconds, or None for the whole clip.

    Returns:
        A types.Part with per-part media resolution set to high. Video at the
        default resolution is 70 tokens per frame; high is 280, which matters
        because a kerb or an awning edge is exactly the sort of small detail
        that the low setting throws away. On a 36 s clip that is roughly 10k
        tokens, which is not worth economising on.
    """
    from google.genai import types

    part = types.Part(
        file_data=types.FileData(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    )
    if start is not None or end is not None:
        part.video_metadata = types.VideoMetadata(
            start_offset=f"{0.0 if start is None else start}s",
            end_offset=f"{end}s",
        )
    return part


def split_thought_and_answer(response) -> tuple[str, str]:
    """Separate the model's thought summary from its actual answer.

    Thought summaries are opt-in via ThinkingConfig(include_thoughts=True) and
    arrive as parts flagged `thought`. They are a *summary* of reasoning, not
    the reasoning itself, and Google documents that a thought block can carry a
    signature with no summary at all -- notably on short clips. So a missing
    summary is an expected outcome, not an error.

    Args:
        response: a types.GenerateContentResponse.

    Returns:
        (thought_summary, answer_text), either of which may be empty.
    """
    thoughts: list[str] = []
    answer: list[str] = []
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if not part.text:
                continue
            (thoughts if part.thought else answer).append(part.text)
    return "\n".join(thoughts).strip(), "\n".join(answer).strip()


def ask(
    client,
    uploaded,
    model: str,
    prompt: str,
    *,
    start: float | None = None,
    end: float | None = None,
) -> tuple[str, str, object]:
    """Run one non-streaming generate_content call against the video.

    Non-streaming is a deliberate choice, not a simplification. With
    `generate_content_stream`, `chunk.candidates[0].content.parts` returns
    None whenever max_output_tokens sits close to the thinking budget, which is
    exactly the regime this prompt runs in (short, tightly-capped answer with
    thinking on). Unary generate_content has no such failure mode. See
    googleapis/python-genai#1011.

    Args:
        client: an initialised google.genai Client.
        uploaded: the ACTIVE File from upload_video().
        model: model id.
        prompt: the user-turn text.
        start: window start in seconds, or None for the whole clip.
        end: window end in seconds, or None for the whole clip.

    Returns:
        (thought_summary, answer_text, usage_metadata).
    """
    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=[video_part(uploaded, start=start, end=end), prompt],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            thinking_config=types.ThinkingConfig(include_thoughts=True),
        ),
    )
    thought, answer = split_thought_and_answer(response)
    return thought, answer, getattr(response, "usage_metadata", None)


def windows_for(duration: float, count: int) -> list[tuple[float, float]]:
    """Split a clip into `count` contiguous time windows.

    Asking about a short window is how you buy timestamp accuracy: Gemini
    samples video at 1 FPS, so a 36 s clip gives it roughly 37 frames to place
    events on, and it will drift. A 6 s window gives the same 1 FPS sampling but
    a far shorter timeline to be wrong about. The cost is one API call per
    window.

    Args:
        duration: clip length in seconds.
        count: number of windows; values below 2 yield a single whole-clip
            window.

    Returns:
        [(start, end), ...] covering the clip, in order.
    """
    if count < 2:
        return [(0.0, duration)]
    span = duration / count
    return [(i * span, (i + 1) * span) for i in range(count)]


def collect(
    client,
    uploaded,
    model: str,
    duration: float,
    windows: int,
) -> tuple[str, str, object]:
    """Run either one whole-clip call or one call per window, and merge.

    Args:
        client: an initialised google.genai Client.
        uploaded: the ACTIVE File from upload_video().
        model: model id.
        duration: clip length in seconds.
        windows: number of time windows; 1 means a single call.

    Returns:
        (thought_summary, answer_text, summed_usage). Thought summaries and
        usage metadata are concatenated or summed across windows, since the
        SDK gives no combined object for a client-driven loop.
    """
    spans = windows_for(duration, windows)
    thoughts: list[str] = []
    answers: list[str] = []
    totals = {"prompt_token_count": 0, "candidates_token_count": 0,
              "thoughts_token_count": 0, "total_token_count": 0}
    for index, (start, end) in enumerate(spans, start=1):
        if len(spans) > 1:
            print(f"  window {index}/{len(spans)}: {format_timestamp(start)}"
                  f"-{format_timestamp(end)}")
        prompt = build_prompt(duration)
        if len(spans) > 1:
            prompt += (
                f"\n\nJudge ONLY the segment from {format_timestamp(start)} to "
                f"{format_timestamp(end)}. Still report timestamps in absolute "
                f"video time, not relative to the segment. If nothing "
                f"notable happens in this segment, reply with a single line: "
                f"[{format_timestamp(start)}] NOTE - nothing notable here."
            )
        thought, answer, usage = ask(
            client, uploaded, model, prompt, start=start, end=end
        )
        if thought:
            thoughts.append(f"[{format_timestamp(start)}] {thought}")
        if answer:
            answers.append(answer)
        for key in totals:
            totals[key] += int(getattr(usage, key, 0) or 0)
    return "\n\n".join(thoughts), "\n\n".join(answers), totals


def render_markdown(result: dict) -> str:
    """Render the result dict as the human-readable companion file.

    Args:
        result: the dict that was also written as JSON.

    Returns:
        Markdown text.
    """
    lines = [
        "# Gemini parking read",
        "",
        f"- model: `{result['model']}`",
        f"- video: `{result['video']}` ({result['duration_seconds']:.1f}s)",
        f"- media resolution: {result['media_resolution']}",
        f"- windows (API calls): {result['windows']}",
        f"- tokens: {result['usage'].get('total_token_count', 0)} total",
        "",
        "## Thought summary",
        "",
        result["thought_summary"] or "_(none returned)_",
        "",
        "## Answer",
        "",
        result["answer_text"] or "_(empty)_",
        "",
        "## Timeline",
        "",
    ]
    for event in result["events"]:
        lines.append(f"- `[{event['timestamp']}]` **{event['label']}** {event['text']}")
    if not result["events"]:
        lines.append("_(no timestamps found in the answer)_")
    lines.append("")
    return "\n".join(lines)


def write_result(result: dict, out_dir: str) -> tuple[str, str]:
    """Write parking.json and parking.md.

    Args:
        result: the assembled result dict.
        out_dir: destination directory, created if absent.

    Returns:
        (json_path, markdown_path).
    """
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "parking.json")
    md_path = os.path.join(out_dir, "parking.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    return json_path, md_path


def report(result: dict) -> None:
    """Print the answer in the shape the demo panel will show it."""
    print()
    if result["thought_summary"]:
        print("--- thought summary (a summary, not raw reasoning) ---")
        print(result["thought_summary"])
        print()
    print("--- answer ---")
    print(result["answer_text"] or "(empty)")
    print()
    print("--- timeline ---")
    if result["events"]:
        for event in result["events"]:
            print(f"  [{event['timestamp']}] {event['label']}: {event['text']}")
    else:
        print("  (no MM:SS timestamps found; the panel will show static text)")
    usage = result["usage"]
    print(
        f"\ntokens: {usage.get('prompt_token_count', 0)} in, "
        f"{usage.get('candidates_token_count', 0)} out, "
        f"{usage.get('thoughts_token_count', 0)} thought"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The API key is read from secrets.env, never from argv.",
    )
    parser.add_argument("video", help="path to the video to send to Gemini")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"(default: {DEFAULT_MODEL})")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--windows",
        type=int,
        default=1,
        help="split the clip into N API calls for tighter timestamps (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved prompt and key status, then exit without spending tokens",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.video):
        print(f"error: no such file: {args.video}", file=sys.stderr)
        return 1

    try:
        info = probe(args.video)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    duration = info.duration_seconds
    if duration <= 0:
        print(f"error: cannot determine duration of {args.video}", file=sys.stderr)
        return 1

    mime_type = MIME_BY_SUFFIX.get(os.path.splitext(args.video)[1].lower(), "video/mp4")
    prompt = build_prompt(duration)
    windows = windows_for(duration, max(1, args.windows))
    print(f"video : {info.describe()}")
    print(f"model : {args.model}")
    print(f"upload: {mime_type}, {os.path.getsize(args.video) / 1048576:.1f} MB")
    print(f"calls : {len(windows)}")

    if args.dry_run:
        applied = load_secrets()
        try:
            key = get_secret("GEMINI_API_KEY")
        except ValueError as exc:
            print(f"\nkey  : MISSING -- {exc}")
            return 1
        print(f"key  : present ({len(key)} chars, from "
              f"{'secrets.env' if applied.get('GEMINI_API_KEY') else 'environment'})")
        print("\n--- system instruction ---")
        print(SYSTEM_INSTRUCTION)
        print("\n--- user prompt ---")
        print(prompt)
        if len(windows) > 1:
            print("\n--- per-window addendum ---")
            for start, end in windows:
                print(f"  window {format_timestamp(start)}-{format_timestamp(end)}")
        print("\ndry run: no API call made.")
        return 0

    try:
        get_secret("GEMINI_API_KEY")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    load_secrets()

    from google.genai import errors as genai_errors

    try:
        client = _build_client()
        uploaded = upload_video(client, args.video, mime_type)
        print("asking...")
        thought, answer, usage = collect(
            client, uploaded, args.model, duration, max(1, args.windows)
        )
    except genai_errors.APIError as exc:
        print(f"error: Gemini API call failed: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    events = parse_events(answer, duration)
    result = {
        "model": args.model,
        "video": args.video,
        "video_bytes": os.path.getsize(args.video),
        "duration_seconds": round(duration, 3),
        "media_resolution": "MEDIA_RESOLUTION_HIGH",
        "windows": len(windows),
        "thought_summary": thought,
        "answer_text": answer,
        "events": [asdict(event) for event in events],
        "usage": usage,
    }
    json_path, md_path = write_result(result, args.out_dir)
    report(result)
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    if not thought:
        print(
            "\nnote: no thought summary came back. Google documents that a "
            "thought block can carry a signature with no summary, which is "
            "common on short clips. Not an error."
        )
    return 0


def _build_client():
    """Construct the google-genai client from the environment.

    Returns:
        A google.genai Client. Raises ImportError with an install hint if the
        SDK is missing, since it is an optional dependency of this repo.
    """
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Add it with:\n"
            "  uv pip install --python /home/yart/projects/endpoint/.venv/bin/python "
            "google-genai"
        ) from exc
    return genai.Client()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
