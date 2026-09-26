#!/usr/bin/env python3
"""
Download STF (Seeing Through Fog) dataset files from a manifest of
presigned S3 URLs.

Input:
    A JSON file (default: data/manifest.json) shaped like:
    {
      "urls": [
        {"key": "SeeingThroughFog/<relative/path>", "url": "<presigned-url>"},
        ...
      ]
    }

Output:
    Each file downloaded to data/raw/<relative/path>, where
    <relative/path> is the key with the leading "SeeingThroughFog/"
    prefix stripped (configurable via --strip-prefix).

Behavior:
    - Skips manifest entries whose key ends in "/" (directory markers).
    - Skips files that already exist locally with a size > 0 (resumable) —
      pass --force to redownload everything.
    - Downloads in parallel (default 6 workers) since these are presigned
      GET URLs, not something to hammer sequentially for hours.
    - Retries each file up to 3 times on transient failure.
    - Prints a final summary of successes / failures / skips, and writes
      a list of failed keys to data/download_failures.json so a rerun can
      target just those.

Usage:
    python scripts/download_data.py
    python scripts/download_data.py --manifest data/manifest.json --workers 8
    python scripts/download_data.py --only-failed   # retry previous failures only

    # Only download specific top-level STF folders (recommended -- the
    # full STF manifest includes many sensors/folders this project's
    # camera+LiDAR-only scope doesn't need):
    python scripts/download_data.py --include cam_stereo_left,lidar_hdl64_last,gt_labels,labeltool_labels,weather_station
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

DEFAULT_MANIFEST = "data/manifest.json"
DEFAULT_OUT_DIR = "data/raw"
DEFAULT_STRIP_PREFIX = "SeeingThroughFog/"
FAILURES_FILE = "data/download_failures.json"
CHUNK_SIZE = 1024 * 1024  # 1 MB


def load_manifest(manifest_path: str) -> list[dict]:
    with open(manifest_path, "r") as f:
        data = json.load(f)
    urls = data.get("urls", data if isinstance(data, list) else [])
    if not urls:
        print(f"ERROR: no 'urls' entries found in {manifest_path}", file=sys.stderr)
        sys.exit(1)
    return urls


def key_to_local_path(key: str, out_dir: Path, strip_prefix: str) -> Path:
    rel = key[len(strip_prefix):] if key.startswith(strip_prefix) else key
    return out_dir / rel


def top_level_group(key: str, strip_prefix: str) -> str:
    """
    Return the top-level folder name for a key, e.g.
    'SeeingThroughFog/cam_stereo_left/cam_stereo_left.z01' -> 'cam_stereo_left'
    'SeeingThroughFog/calib_cam_stereo_left.json' -> 'calib_cam_stereo_left.json'
    (root-level files group under their own filename since there's no folder)
    """
    rel = key[len(strip_prefix):] if key.startswith(strip_prefix) else key
    rel = rel.strip("/")
    if not rel:
        return ""  # the bare "SeeingThroughFog/" directory marker
    return rel.split("/")[0]


def download_one(entry: dict, out_dir: Path, strip_prefix: str, force: bool,
                  max_retries: int = 3) -> tuple[str, bool, str]:
    key = entry["key"]
    url = entry["url"]

    if key.endswith("/"):
        return key, True, "skipped (directory marker)"

    local_path = key_to_local_path(key, out_dir, strip_prefix)

    if not force and local_path.exists() and local_path.stat().st_size > 0:
        return key, True, "skipped (already exists)"

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".part")

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
            tmp_path.rename(local_path)
            size_mb = local_path.stat().st_size / (1024 * 1024)
            return key, True, f"ok ({size_mb:.1f} MB)"
        except Exception as e:  # noqa: BLE001 - want to catch+retry broadly here
            last_err = str(e)
            time.sleep(min(2 ** attempt, 10))
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    return key, False, f"FAILED after {max_retries} attempts: {last_err}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--strip-prefix", default=DEFAULT_STRIP_PREFIX)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--force", action="store_true",
                         help="Redownload even if local file already exists")
    parser.add_argument("--only-failed", action="store_true",
                         help=f"Only retry keys listed in {FAILURES_FILE}")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N entries (for a quick test run)")
    parser.add_argument("--include", default=None,
                         help="Comma-separated list of top-level STF folder/file names "
                              "to include (e.g. 'cam_stereo_left,lidar_hdl64_last,gt_labels'). "
                              "All other entries are skipped. Omit to download everything.")
    parser.add_argument("--list-groups", action="store_true",
                         help="Print the top-level group names + file counts found in "
                              "the manifest, then exit (use this to decide --include values)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = load_manifest(args.manifest)
    print(f"Loaded {len(entries)} entries from {args.manifest}")

    if args.list_groups:
        from collections import Counter
        counts = Counter(
            top_level_group(e["key"], args.strip_prefix) for e in entries
        )
        print(f"\n{len(counts)} top-level groups:")
        for name, count in sorted(counts.items()):
            print(f"  {name:35s} {count:4d} entries")
        sys.exit(0)

    if args.include:
        include_set = {g.strip() for g in args.include.split(",") if g.strip()}
        before = len(entries)
        entries = [
            e for e in entries
            if top_level_group(e["key"], args.strip_prefix) in include_set
        ]
        print(f"--include filter: {before} -> {len(entries)} entries "
              f"(groups: {sorted(include_set)})")
        if not entries:
            print("WARNING: --include matched zero entries. Run --list-groups "
                  "to see valid group names.")
            sys.exit(1)

    if args.only_failed:
        if not os.path.exists(FAILURES_FILE):
            print(f"No failures file at {FAILURES_FILE}; nothing to retry.")
            sys.exit(0)
        with open(FAILURES_FILE) as f:
            failed_keys = set(json.load(f))
        entries = [e for e in entries if e["key"] in failed_keys]
        print(f"Retrying {len(entries)} previously failed entries")

    if args.limit:
        entries = entries[: args.limit]
        print(f"Limiting to first {len(entries)} entries (--limit)")

    successes = 0
    skips = 0
    failures = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_one, e, out_dir, args.strip_prefix, args.force): e
            for e in entries
        }
        for i, future in enumerate(as_completed(futures), 1):
            key, ok, msg = future.result()
            status = "OK" if ok else "FAIL"
            print(f"[{i}/{len(entries)}] {status}: {key} -- {msg}")
            if ok:
                if "skipped" in msg:
                    skips += 1
                else:
                    successes += 1
            else:
                failures.append(key)

    print("\n--- Summary ---")
    print(f"Downloaded: {successes}")
    print(f"Skipped (already present): {skips}")
    print(f"Failed: {len(failures)}")

    with open(FAILURES_FILE, "w") as f:
        json.dump(failures, f, indent=2)

    if failures:
        print(f"\nFailed keys written to {FAILURES_FILE}")
        print("Rerun with --only-failed to retry just those.")
        sys.exit(1)
    else:
        if os.path.exists(FAILURES_FILE):
            os.remove(FAILURES_FILE)
        print("\nAll files downloaded successfully.")


if __name__ == "__main__":
    main()
