"""S1 Legal Spots service (ENDPOINT.md section 6). Port 8001.

    POST /spots/legal      every place the car may legally stop near the rider
    GET  /health           liveness
    GET  /ready            cache state, so the demo can be checked before it starts
    GET  /spots/explain    DEV ONLY -- accepted and rejected candidates with reasons.
                           Stripped before the demo (set STRIP_DEV_ROUTES=1).
    POST /admin/prefetch   warm the cache for a bbox
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.config import SETTINGS
from shared.geo import expanded_query_bbox, tile_bbox
from shared.models import BBox, LatLng, LegalSpotsRequest, LegalSpotsResponse
from shared.osm_cache import OverpassError, afetch_overpass, cache_info, read_cache

from .overpass import build_point_query, build_road_query
from .service import clamp_radius, explain, legal_spots, schedule_refresh

app = FastAPI(
    title="S1 Legal Spots",
    version="1.0.0",
    description="Where can the car legally stop near the rider? (ENDPOINT.md section 6)",
)

#: Flip to true (or set STRIP_DEV_ROUTES=1) to remove the diagnostic route from
#: the public surface before the demo. The explain route is a development tool,
#: not a product surface, and it exposes OSM source ids.
STRIP_DEV_ROUTES = os.getenv("STRIP_DEV_ROUTES", "0") in {"1", "true", "yes"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(
    lat: float = Query(...), lng: float = Query(...), radius_m: float = 150.0
) -> JSONResponse:
    """Is the demo area actually cached?

    Worth having separately from ``/health``: a service can be perfectly healthy
    with no data loaded, and finding that out during a pitch is the worst
    possible time.
    """
    from .service import _NETWORK_CACHE, _tile_and_ids

    radius = clamp_radius(radius_m)
    tile = tile_bbox(lat, lng, radius)
    _, roads_id, _ = _tile_and_ids(LatLng(lat=lat, lng=lng), radius)
    query_bbox = expanded_query_bbox(tile, radius)
    road_q = build_road_query(query_bbox)

    info = cache_info(road_q, roads_id)
    in_memory = roads_id in _NETWORK_CACHE
    on_disk = read_cache(road_q, roads_id) is not None
    ready_now = in_memory or on_disk

    return JSONResponse(
        status_code=200 if ready_now else 503,
        content={
            "status": "ready" if ready_now else "no_data",
            "in_memory": in_memory,
            "on_disk": on_disk,
            "cache_age_s": (
                round(info.age_s, 1) if info and info.age_s != float("inf") else None
            ),
            "endpoint": info.endpoint if info else None,
            "tile": BBox(south=tile[0], west=tile[1], north=tile[2], east=tile[3]).as_overpass,
            "query_bbox": BBox(
                south=query_bbox[0], west=query_bbox[1], north=query_bbox[2], east=query_bbox[3]
            ).as_overpass,
            "demo_rider": list(SETTINGS.demo_rider),
            "mock": SETTINGS.mock,
        },
    )


@app.post("/spots/legal", response_model=LegalSpotsResponse)
async def spots_legal(req: LegalSpotsRequest) -> LegalSpotsResponse:
    """Return every legal stopping spot within ``radius_m`` of the rider."""
    return await legal_spots(req)


if not STRIP_DEV_ROUTES:

    @app.get("/spots/explain")
    async def spots_explain(
        lat: float = Query(...),
        lng: float = Query(...),
        radius_m: float = Query(150.0, ge=1.0, le=500.0),
    ) -> JSONResponse:
        """DEV ONLY. Every candidate we generated and why each was kept or dropped.

        This is how the exclusion buffers get tuned against real data and how
        the section 6 S1 definition of done is actually verified.
        """
        req = LegalSpotsRequest(rider_location=LatLng(lat=lat, lng=lng), radius_m=radius_m)
        return JSONResponse(content=explain(req).model_dump(mode="json"))


@app.post("/admin/prefetch")
async def admin_prefetch(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
    radius_m: float = Query(150.0, ge=1.0, le=500.0),
) -> dict[str, object]:
    """Warm the cache for a bbox. Used by ``scripts/prefetch_demo_area.py``."""
    radius = clamp_radius(radius_m)
    tiles = [t for t in _tiles_covering((south, west, north, east), radius)]
    if not tiles:
        return {"status": "empty", "bbox": f"{south},{west},{north},{east}"}

    written = 0
    errors: list[str] = []
    for tile in tiles:
        query_bbox = expanded_query_bbox(tile, radius)
        center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
        for kind, build in (("roads", build_road_query), ("points", build_point_query)):
            from shared.geo import tile_cache_id

            cid = tile_cache_id(center[0], center[1], radius, kind)
            try:
                await afetch_overpass(build(query_bbox), cid)
                written += 1
            except OverpassError as exc:
                errors.append(f"{kind} {tile}: {exc}")

    for tile in tiles:
        schedule_refresh(tile, radius)

    return {
        "status": "cached" if not errors else "partial",
        "tiles": len(tiles),
        "written": written,
        "errors": errors,
    }


def _tiles_covering(
    bbox: tuple[float, float, float, float], radius: float
) -> list[tuple[float, float, float, float]]:
    from shared.geo import tiles_for_bbox

    return tiles_for_bbox(bbox, radius)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
