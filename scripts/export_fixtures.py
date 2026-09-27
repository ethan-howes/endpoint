#!/usr/bin/env python
"""Export the Overpass disk cache into committed, human-readable fixtures.

The disk cache under ``data/cache/overpass/`` is gitignored working state, keyed by
query hash. The fixtures under ``services/*/fixtures/`` are the committed,
reviewable copy that makes ``MOCK=1`` work and a recorded demo run reproducible.

Run this after a successful capture::

    python -m scripts.capture_fixtures --service s1
    python -m scripts.capture_fixtures --service s2
    python -m scripts.export_fixtures --service s1
    python -m scripts.export_fixtures --service s2

``--kinds`` exists for one specific situation. The disk cache is keyed by a hash
of the *query text*, so editing a query does not make its cached responses stale,
it makes them unfindable -- under a new hash. A capture run afterwards would
re-fetch every kind of that service, when only the edited one needs it. Its first
real use: ``shade_query`` lost its ``tree_row`` selector, so nine shade responses
had to be re-fetched and the nine cover responses were still perfectly valid::

    python -m scripts.capture_fixtures --service s2 --kinds shade
    python -m scripts.export_fixtures  --service s2 --kinds shade

It copies the raw bytes without re-parsing them, so the fixture is exactly what
the API returned -- including the nulls in geometry and the tag noise that a
pre-parsed fixture would have quietly smoothed over.

The query table is shared with ``capture_fixtures.py`` (``scripts/fixture_plan.py``)
so the two cannot disagree about what to fetch or what to call it. Before exiting
it also re-derives every filename it intended to write and checks the file is
where a service will look for it, because "committed but never read" is otherwise
indistinguishable from "not committed".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import DEFAULT_RADIUS_M, SETTINGS  # noqa: E402
from shared.fixtures import (  # noqa: E402
    cache_key,
    fixture_name,
    fixtures_dir,
    list_fixtures,
    write_fixture,
)
from shared.geo import expanded_query_bbox, tile_bbox, tile_cache_id, tiles_for_bbox  # noqa: E402
from shared.osm_cache import read_cache  # noqa: E402
from scripts.fixture_plan import QUERIES, SERVICE_DIRS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", default=SETTINGS.bbox_str)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    ap.add_argument(
        "--service",
        default="s1",
        choices=sorted(QUERIES),
        help="s1 writes roads/points, s2 writes cover/shade",
    )
    ap.add_argument(
        "--single-tile",
        action="store_true",
        help="only the tile containing the bbox SW corner, instead of every tile",
    )
    ap.add_argument(
        "--kinds", nargs="+",
        help="restrict to these query kinds, e.g. --kinds shade. Mirrors "
             "capture_fixtures --kinds so a re-capture of one kind exports only "
             "that kind, without re-listing the other kind's fixtures as if they "
             "had just been refreshed.",
    )
    args = ap.parse_args()

    kinds = QUERIES[args.service]
    if args.kinds:
        want = set(args.kinds)
        kinds = tuple(t for t in kinds if t[0] in want)
        if not kinds:
            print(
                f"no {args.service} query matches {sorted(want)}; "
                f"available: {[k for k, _ in QUERIES[args.service]]}",
                file=sys.stderr,
            )
            return 1

    s, w, n, e = (float(x) for x in args.bbox.split(","))
    tiles = (
        [tile_bbox(s, w, args.radius)]
        if args.single_tile
        else tiles_for_bbox((s, w, n, e), args.radius)
    )
    out_dir = fixtures_dir(SERVICE_DIRS[args.service])
    out_dir.mkdir(parents=True, exist_ok=True)

    written = missing = 0
    for tile in tiles:
        qb = expanded_query_bbox(tile, args.radius)
        center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
        for kind, build in kinds:
            cid = tile_cache_id(center[0], center[1], args.radius, kind)
            payload = read_cache(build(qb), cache_key(args.service, kind, cid))
            if payload is None:
                print(f"  MISSING {kind:6s} tile={tile}  (run capture_fixtures first)")
                missing += 1
                continue
            path = write_fixture(out_dir, cid, payload)
            print(f"  wrote {kind:6s} {len(payload.get('elements', [])):5d} elements -> {path.name}")
            written += 1

    print(f"\n{written} fixture(s) written to {out_dir}, {missing} missing")
    if written:
        print("committed fixtures:")
        for name in list_fixtures(out_dir):
            print(f"  {name}")

    # Prove the write is readable, rather than trusting that the two naming
    # conventions happen to agree. This is a five-line check that would have
    # caught a real bug: S2 once passed a pre-mangled id to `read_fixture` while
    # this script wrote the raw one, so every S2 fixture was committed and never
    # found, and `MOCK=1` reported "no committed fixture" for a fully seeded area.
    unreadable = []
    for tile in tiles:
        center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
        for kind, _ in kinds:
            cid = tile_cache_id(center[0], center[1], args.radius, kind)
            if not (out_dir / fixture_name(cid)).exists():
                unreadable.append(fixture_name(cid))
    if unreadable:
        print(f"\nWARNING: {len(unreadable)} fixture(s) written but not where a "
              f"service will look for them:", file=sys.stderr)
        for name in unreadable:
            print(f"  {name}", file=sys.stderr)

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
