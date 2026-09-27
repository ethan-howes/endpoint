"""The walking network, for the rain exposure ranking: raw Overpass JSON -> a frozen graph.

Ingestion only, in the same shape as ``cover.py``: a query builder, a pure parser
that runs identically on a live response, a disk-cache hit and a committed fixture,
and a frozen value that ``rain_exposure.py`` computes over.

Why the ranking needs this at all: the gap model measured the distance from the
kerb to the nearest cover and nothing else, so it could not tell a rider who walks
out of a building through a covered passage (dry until the last few metres) from
one who crosses an open car park (wet the whole way). The only way to see that
difference is to follow the walk, and the walk runs along this network.

Nodes are OSM node ids, so ways that share a node are connected exactly as OSM
connects them. Coordinates are absolute UTM metres from the request's single
``LocalFrame`` -- see ``service._merge_maps`` for why one frame matters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from shared.config import SETTINGS
from shared.fixtures import cache_key
from shared.geo import LocalFrame, expanded_query_bbox, tile_cache_id
from shared.osm_cache import afetch_overpass

#: Highway classes nobody walks along. Everything else with a ``highway`` tag is
#: part of the network: footways and paths, but also service roads and car-park
#: aisles, which on a campus are half of every real walking route.
UNWALKABLE_HIGHWAYS = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "construction", "proposed", "raceway", "bus_guideway", "abandoned", "platform",
})


@dataclass(frozen=True)
class PathWay:
    """One walkable OSM way, as the ordered node ids and their projected points."""

    way_id: int
    node_ids: tuple[int, ...]
    points: tuple[tuple[float, float], ...]
    highway: str
    covered: bool = False  # tagged covered / building passage / corridor


@dataclass(frozen=True)
class PathMap:
    """Every walkable way and building entrance in one bbox."""

    frame: LocalFrame
    bbox: tuple[float, float, float, float]
    ways: list[PathWay] = field(default_factory=list)
    #: OSM node id -> projected point, for ``entrance=*`` nodes.
    entrances: dict[int, tuple[float, float]] = field(default_factory=dict)
    generated_at: float = 0.0
    cache_id: str = ""

    def summary(self) -> dict[str, int]:
        return {
            "ways": len(self.ways),
            "covered_ways": sum(1 for w in self.ways if w.covered),
            "entrances": len(self.entrances),
        }


def build_paths_query(bbox: tuple[float, float, float, float]) -> str:
    """The config query with its bbox filled in. See ``cover.build_cover_query``."""
    s, w, n, e = bbox
    return SETTINGS.paths_query.strip().format(bbox=f"{s},{w},{n},{e}")


def is_walkable(tags: dict[str, str]) -> bool:
    """Whether a highway way belongs in the walking network.

    ``foot=no`` and ``access=no`` are respected; ``access=private`` is not, because
    on a campus most service roads are tagged private to cars and are exactly the
    roads a pedestrian uses.
    """
    hw = tags.get("highway")
    if not hw or hw in UNWALKABLE_HIGHWAYS:
        return False
    if tags.get("foot") == "no" or tags.get("access") == "no":
        return False
    return True


def _is_covered(tags: dict[str, str]) -> bool:
    return (
        tags.get("covered") in ("yes", "arcade")
        or tags.get("tunnel") == "building_passage"
        or tags.get("highway") == "corridor"
        or tags.get("indoor") in ("yes", "corridor")
    )


def parse_paths(
    tile: tuple[float, float, float, float],
    radius_m: float,
    payload: dict[str, Any],
    cache_id: str,
    frame: LocalFrame | None = None,
) -> PathMap:
    """Overpass JSON -> a ``PathMap``. No I/O.

    ``nodes`` and ``geometry`` come back aligned from ``out body geom`` (same
    length, same order), which is what lets a way be both a sequence of ids for
    connectivity and a sequence of points for measurement. A way whose two arrays
    disagree, or whose geometry has nulls (clipped at the bbox edge), keeps only
    the pairs that are complete -- a partial way still connects correctly at the
    nodes it does have.
    """
    query_bbox = expanded_query_bbox(tile, radius_m)
    if frame is None:
        frame = LocalFrame((query_bbox[0] + query_bbox[2]) / 2.0, (query_bbox[1] + query_bbox[3]) / 2.0)

    ways: list[PathWay] = []
    entrances: dict[int, tuple[float, float]] = {}
    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") == "node":
            if "entrance" in tags and el.get("lat") is not None and el.get("lon") is not None:
                entrances[int(el["id"])] = frame.to_m(float(el["lat"]), float(el["lon"]))
            continue
        if el.get("type") != "way" or not is_walkable(tags):
            continue
        ids = el.get("nodes") or []
        geom = el.get("geometry") or []
        pairs = [
            (int(nid), frame.to_m(float(g["lat"]), float(g["lon"])))
            for nid, g in zip(ids, geom)
            if g and g.get("lat") is not None and g.get("lon") is not None
        ]
        if len(pairs) < 2:
            continue
        ways.append(PathWay(
            way_id=int(el["id"]),
            node_ids=tuple(p[0] for p in pairs),
            points=tuple(p[1] for p in pairs),
            highway=tags.get("highway", ""),
            covered=_is_covered(tags),
        ))

    return PathMap(
        frame=frame,
        bbox=query_bbox,
        ways=ways,
        entrances=entrances,
        generated_at=time.time(),
        cache_id=cache_id,
    )


async def fetch_paths(
    tile: tuple[float, float, float, float],
    radius_m: float,
    *,
    frame: LocalFrame | None = None,
    use_cache: bool = True,
    store: bool = True,
) -> PathMap:
    """Live Overpass fetch, disk-cached like the cover and shade documents."""
    query_bbox = expanded_query_bbox(tile, radius_m)
    centre = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
    cache_id = tile_cache_id(centre[0], centre[1], radius_m, "paths")
    payload = await afetch_overpass(
        build_paths_query(query_bbox), cache_key("s2", "paths", cache_id),
        use_cache=use_cache, store=store,
    )
    return parse_paths(tile, radius_m, payload, cache_id, frame)


__all__ = [
    "PathMap", "PathWay", "UNWALKABLE_HIGHWAYS",
    "build_paths_query", "fetch_paths", "is_walkable", "parse_paths",
]
