#!/usr/bin/env python
"""Warm the Overpass disk cache for the demo area (ENDPOINT.md section 8).

Run this BEFORE the demo. ENDPOINT.md section 11 lists "Overpass rate limits or
timeouts" as a top risk with the mitigation "prefetch and cache the demo bbox in
hour 1; serve from cache during the demo". This is that mitigation, and it is not
optional: the public instance returned 504 on roughly half our requests while
capturing the demo area, and returns 200-with-an-HTML-error-page often enough
that a naive "did we get JSON" check is not sufficient.

It enumerates the same containment tiles the service uses at request time (see
``shared/geo.tiles_for_bbox``), so whatever it caches is exactly what the service
will later look for. Tiles that are already fresh are skipped, which makes the
script safe to re-run after a partial failure.

Usage::

    python -m scripts.prefetch_demo_area                  # uses DEMO_BBOX
    python -m scripts.prefetch_demo_area --bbox 25.75,-80.37,25.76,-80.36
    python -m scripts.prefetch_demo_area --force          # refetch even if fresh
    python -m scripts.prefetch_demo_area --radius 150
    python -m scripts.prefetch_demo_area --tries 8        # attempts per query
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import DEFAULT_RADIUS_M, SETTINGS  # noqa: E402
from shared.geo import expanded_query_bbox, tile_cache_id, tiles_for_bbox  # noqa: E402
from shared.osm_cache import (  # noqa: E402
    OverpassError,
    afetch_overpass,
    cache_info,
)
from services.s1_legal_spots.overpass import build_point_query, build_road_query  # noqa: E402

#: Overpass etiquette. We are a guest on a free shared service; do not hammer it.
POLITE_DELAY_S = 1.5


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise SystemExit(f"--bbox must be south,west,north,east (got {raw!r})")
    try:
        s, w, n, e = (float(p) for p in parts)
    except ValueError as exc:
        raise SystemExit(f"--bbox must be four numbers (got {raw!r})") from exc
    if not (s < n and w < e):
        raise SystemExit("--bbox must satisfy south<north and west<east")
    return s, w, n, e


async def _fetch_with_retries(
    query: str, cache_id: str, tries: int
) -> tuple[int, str | None]:
    """Try up to ``tries`` times, with backoff. Returns ``(elements, error)``.

    ``afetch_overpass`` already fails over between mirrors, so one "try" here is
    itself a full mirror sweep; the retries exist for the case where every mirror
    is momentarily saturated, which is the common failure on the main instance.
    """
    last: str | None = None
    for attempt in range(1, tries + 1):
        try:
            payload = await afetch_overpass(query, cache_id, use_cache=False)
            return len(payload.get("elements", [])), None
        except OverpassError as exc:
            last = str(exc)
        except Exception as exc:  # keep going; a partial cache beats none
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(min(2.0 * attempt, 10.0))
    return -1, last


async def prefetch(
    bbox: tuple[float, float, float, float], radius: float, force: bool, tries: int
) -> int:
    tiles = tiles_for_bbox(bbox, radius)
    print(f"demo bbox {bbox}  radius {radius:g} m  ->  {len(tiles)} tile(s)")

    builders = {"roads": build_road_query, "points": build_point_query}
    ok = failed = skipped = 0
    total_steps = len(tiles) * len(builders)
    step = 0

    for tile in tiles:
        query_bbox = expanded_query_bbox(tile, radius)
        center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
        for kind, build in builders.items():
            step += 1
            query = build(query_bbox)
            cache_id = tile_cache_id(center[0], center[1], radius, kind)

            info = cache_info(query, cache_id)
            if info is not None and not force and info.fresh:
                skipped += 1
                print(f"  [{step}/{total_steps}] {kind:6s} {tile} -> cached, fresh")
                continue

            n, err = await _fetch_with_retries(query, cache_id, tries)
            if n >= 0:
                print(f"  [{step}/{total_steps}] {kind:6s} {tile} -> {n:5d} elements")
                ok += 1
            else:
                print(
                    f"  [{step}/{total_steps}] {kind:6s} {tile} -> FAILED: {err}",
                    file=sys.stderr,
                )
                failed += 1
            await asyncio.sleep(POLITE_DELAY_S)

    print(f"\nfetched={ok}  already-fresh={skipped}  failed={failed}")
    if failed:
        print("Re-run this script; it skips tiles that already succeeded.")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bbox", default=SETTINGS.bbox_str, help="south,west,north,east")
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    ap.add_argument("--force", action="store_true", help="refetch even if fresh")
    ap.add_argument("--tries", type=int, default=6, help="attempts per query")
    args = ap.parse_args()

    bbox = parse_bbox(args.bbox)
    started = time.time()
    failed = asyncio.run(prefetch(bbox, args.radius, args.force, args.tries))
    print(f"done in {time.time() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
