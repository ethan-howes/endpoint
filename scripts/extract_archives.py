#!/usr/bin/env python3
"""
Reassemble and extract multi-part zip archives downloaded by
download_data.py.

STF ships some directories (e.g. cam_stereo_left/, cam_stereo_right/) as
split zip archives: a set of .z01, .z02, ... part files plus a final .zip
(or sometimes .zNN + .zip where .zip is the last part containing the
central directory). A plain `unzip` on just the .zip will fail or produce
garbage without first joining the parts.

Approach:
    For each base archive name found (grouping by stripping .zNN/.zip
    suffixes), use `zip -F` / `zip -FF` (via system zip/unzip tools) to
    fix/join the split archive into a single file, then extract it.

    This relies on the `zip` and `unzip` command-line tools being present
    (available via apt: `sudo apt-get install zip unzip`).

Usage:
    python scripts/extract_archives.py               # scan data/raw, extract all
    python scripts/extract_archives.py --dry-run      # just show what would happen
    python scripts/extract_archives.py --dir data/raw/cam_stereo_left
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_SCAN_DIR = "data/raw"
DEFAULT_OUT_DIR = "data/processed"

# Matches e.g. cam_stereo_left.z01, cam_stereo_left.z02, cam_stereo_left.zip
PART_PATTERN = re.compile(r"^(?P<base>.+)\.(z\d{2}|zip)$")


def check_tools():
    for tool in ("zip", "unzip"):
        if shutil.which(tool) is None:
            print(
                f"ERROR: required tool '{tool}' not found on PATH.\n"
                f"Install with: sudo apt-get update && sudo apt-get install -y zip unzip",
                file=sys.stderr,
            )
            sys.exit(1)


def find_archive_groups(scan_dir: Path) -> dict[Path, list[Path]]:
    """Group split-archive parts by (directory, base_name)."""
    groups: dict[tuple[Path, str], list[Path]] = {}
    for path in scan_dir.rglob("*"):
        if not path.is_file():
            continue
        m = PART_PATTERN.match(path.name)
        if not m:
            continue
        base = m.group("base")
        key = (path.parent, base)
        groups.setdefault(key, []).append(path)
    return groups


def join_and_extract(directory: Path, base: str, parts: list[Path],
                      out_dir: Path, dry_run: bool):
    zip_part = directory / f"{base}.zip"
    if not zip_part.exists():
        print(f"  SKIP {base}: no final .zip part found among {[p.name for p in parts]} "
              f"(need the .zip file, not just .zNN parts, to join)")
        return

    joined_path = directory / f"{base}.joined.zip"
    extract_dir = out_dir / base

    if extract_dir.exists() and any(extract_dir.iterdir()):
        print(f"  SKIP {base}: already extracted to {extract_dir}")
        return

    print(f"  Archive group '{base}': {len(parts)} parts in {directory}")
    if dry_run:
        print(f"    Would join -> {joined_path}")
        print(f"    Would extract -> {extract_dir}")
        return

    # Step 1: join the split zip into a single archive using `zip -FF`
    # (zip -FF fixes/reconstructs a "fixed" copy from split parts; needs
    # the .zip in place alongside its .zNN siblings in the same dir)
    cmd_join = ["zip", "-FF", str(zip_part), "--out", str(joined_path)]
    print(f"    Running: {' '.join(cmd_join)}")
    result = subprocess.run(cmd_join, capture_output=True, text=True, cwd=directory)
    if result.returncode != 0:
        print(f"    ERROR joining {base}:\n{result.stdout}\n{result.stderr}")
        return

    # Step 2: extract the joined archive
    extract_dir.mkdir(parents=True, exist_ok=True)
    cmd_extract = ["unzip", "-o", str(joined_path), "-d", str(extract_dir)]
    print(f"    Running: {' '.join(cmd_extract)}")
    result = subprocess.run(cmd_extract, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ERROR extracting {base}:\n{result.stdout}\n{result.stderr}")
        return

    print(f"    OK: extracted to {extract_dir}")
    joined_path.unlink(missing_ok=True)  # clean up the intermediate joined zip


def extract_plain_zips(scan_dir: Path, out_dir: Path, skip_bases: set[str],
                        dry_run: bool):
    """Handle any standalone .zip files that are NOT part of a split archive."""
    for path in scan_dir.rglob("*.zip"):
        base = path.stem
        if base in skip_bases:
            continue  # already handled as part of a split-archive group
        extract_dir = out_dir / base
        if extract_dir.exists() and any(extract_dir.iterdir()):
            print(f"  SKIP {base}: already extracted to {extract_dir}")
            continue
        print(f"  Standalone zip '{base}' in {path.parent}")
        if dry_run:
            print(f"    Would extract -> {extract_dir}")
            continue
        extract_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["unzip", "-o", str(path), "-d", str(extract_dir)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ERROR extracting {base}:\n{result.stdout}\n{result.stderr}")
        else:
            print(f"    OK: extracted to {extract_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_SCAN_DIR,
                         help="Directory to scan for archives (recursively)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run:
        check_tools()

    scan_dir = Path(args.dir)
    out_dir = Path(args.out_dir)

    if not scan_dir.exists():
        print(f"ERROR: {scan_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {scan_dir} for split/standalone zip archives...")
    groups = find_archive_groups(scan_dir)

    if not groups:
        print("No .zNN/.zip archive parts found.")
    else:
        print(f"Found {len(groups)} archive group(s).")

    handled_bases = set()
    for (directory, base), parts in sorted(groups.items()):
        join_and_extract(directory, base, sorted(parts), out_dir, args.dry_run)
        handled_bases.add(base)

    print("\nChecking for standalone (non-split) .zip files...")
    extract_plain_zips(scan_dir, out_dir, handled_bases, args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
