#!/usr/bin/env python
"""Fetch and cache raw Overpass fixtures for a bbox, using the real query builders.

This is the same code path ``scripts/prefetch_demo_area.py`` uses, so what lands
on disk is exactly what the service will read. Written as a separate script only
so fixtures can be captured in bulk and committed; ``prefetch_demo_area.py``
remains the operator-facing tool.

``--kinds`` re-fetches one query kind without touching the others. Needed when a
query's *text* changes: the disk cache is keyed by a hash of the query, so the
old responses stop being stale and start being invisible under a new hash. See
``export_fixtures`` for the first time this was needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import DEFAULT_RADIUS_M  # noqa: E402
from shared.fixtures import cache_key  # noqa: E402
from shared.geo import expanded_query_bbox, tile_bbox, tiles_for_bbox  # noqa: E402
from shared.osm_cache import afetch_overpass  # noqa: E402
from scripts.fixture_plan import QUERIES, SERVICE_DIRS, BUDGETS  # noqa: E402


async def fetch_one(query: str, cache_id: str, tries: int) -> int:
    for attempt in range(1, tries + 1):
        try:
            payload = await afetch_overpass(query, cache_id, use_cache=False)
            n = len(payload.get("elements", []))
            print(f"  OK attempt={attempt} {n} elements -> {cache_id}", flush=True)
            return n
        except Exception as exc:  # noqa: BLE001
            print(f"  attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
            await asyncio.sleep(min(2.0 * attempt, 8.0))
    return -1


async def main_async(args: argparse.Namespace) -> int:
    s, w, n, e = (float(x) for x in args.bbox.split(","))
    radius = args.radius
    tiles = [tile_bbox(s, w, radius)] if args.tile else tiles_for_bbox((s, w, n, e), radius)
    targets = QUERIES[args.service]
    if args.kinds:
        want = set(args.kinds)
        targets = tuple(t for t in targets if t[0] in want)
        if not targets:
            print(
                f"no {args.service} query matches {sorted(want)}; "
                f"available: {[k for k, _ in QUERIES[args.service]]}",
                file=sys.stderr,
            )
            return 1
    print(f"{args.service}: {len(tiles)} tile(s) x {len(targets)} kind(s) "
          f"for {args.bbox} r={radius}")

    failed = 0
    for tile in tiles:
        qb = expanded_query_bbox(tile, radius)
        center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
        from shared.geo import tile_cache_id

        print(f"tile {tile} query {qb}")
        for kind, build in targets:
            cid = tile_cache_id(center[0], center[1], radius, kind)
            got = await fetch_one(
                build(qb), cache_key(args.service, kind, cid), args.tries
            )
            failed += 1 if got < 0 else 0
            await asyncio.sleep(1.5)
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    ap.add_argument("--tries", type=int, default=10)
    ap.add_argument("--service", choices=sorted(QUERIES), default="s1")
    ap.add_argument(
        "--kinds", nargs="+",
        help="restrict to these query kinds, e.g. --kinds shade. Needed whenever a "
             "query's *text* changes but its kind does not: the disk cache is keyed "
             "by a hash of the query, so the old response is not merely stale but "
             "unfindable, and re-capturing the untouched kinds would burn Overpass "
             "for responses that are still perfectly valid.",
    )
    ap.add_argument("--tile", action="store_true", help="only the tile containing bbox SW corner")
    args = ap.parse_args()
    return 1 if asyncio.run(main_async(args)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
