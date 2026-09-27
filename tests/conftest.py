"""Shared test fixtures: tiny hand-built street networks.

ENDPOINT.md section 6 S1's definition of done is "for 3 test locations in the demo
area, the UI shows spots on both sides of the street, none within the hydrant,
crosswalk, bus stop, or bike-lane exclusions". That is not checkable by eye on a
map, so these fixtures encode each rule as an isolated scenario with an
assertion a human wrote down.

Everything here is in METERS via a test frame anchored near the demo area, so
distances in the assertions are literal.
"""

from __future__ import annotations

import pytest

from shared.geo import LocalFrame, Polyline
from services.s1_legal_spots.network import ParkingLot, Restriction, Road, StreetNetwork
from shared.models import RestrictionKind

#: Anchored on the FIU demo area so `to_m`/`to_ll` round-trips are realistic.
TEST_ORIGIN = (25.756918, -80.372182)


@pytest.fixture
def frame() -> LocalFrame:
    return LocalFrame(*TEST_ORIGIN)


def make_road(
    frame: LocalFrame | None = None,
    way_id: str = "way/1",
    *,
    latlngs: list[tuple[float, float]] | None = None,
    tags: dict[str, str] | None = None,
    oneway: bool = False,
    oneway_reversed: bool = False,
    left_offset: float = 3.5,
    right_offset: float = 3.5,
    nodes: tuple[str, ...] = (),
) -> Road:
    """A straight west-to-east road, ~200 m long, unless ``latlngs`` overrides it.

    ``frame`` may be None: tag-only rules (side legality, cycleway
    classification) do not need geometry, and building a real polyline for them
    would obscure what the test is actually about.
    """
    if frame is None:
        frame = LocalFrame(*TEST_ORIGIN)
    if latlngs is None:
        base_lat, base_lng = TEST_ORIGIN
        latlngs = [(base_lat, base_lng - 0.001), (base_lat, base_lng + 0.001)]  # ~200 m

    tags = dict(tags or {})
    tags.setdefault("highway", "residential")
    tags.setdefault("lanes", "2")
    tags.setdefault("name", "Test St")

    return Road(
        way_id=way_id,
        polyline=Polyline.from_latlngs(frame, latlngs),
        highway=tags["highway"],
        name=tags.get("name"),
        ref=tags.get("ref"),
        oneway=oneway,
        oneway_reversed=oneway_reversed,
        left_offset_m=left_offset,
        right_offset_m=right_offset,
        width_known=True,
        lanes=2,
        nodes=nodes,
        tags=tags,
    )


def make_network(
    frame: LocalFrame,
    roads: list[Road] | None = None,
    restrictions: list[Restriction] | None = None,
    lots: list[ParkingLot] | None = None,
) -> StreetNetwork:
    return StreetNetwork(
        frame=frame,
        bbox=(25.75, -80.38, 25.76, -80.37),
        roads=roads or [],
        restrictions=restrictions or [],
        lots=lots or [],
        source="test",
    )


def point_restriction(
    frame: LocalFrame,
    kind: RestrictionKind,
    source_id: str,
    buffer_m: float,
    at: tuple[float, float],
    label: str = "",
) -> Restriction:
    """A disc obstacle at a lat/lng."""
    return Restriction(
        kind=kind, source_id=source_id, buffer_m=buffer_m, label=label
    ).with_xy(frame.to_m(*at))


def arc_restriction(
    kind: RestrictionKind,
    source_id: str,
    road_key: str,
    anchor_m: float,
    extent_m: float,
    buffer_m: float,
    side: str | None = None,
    label: str = "",
) -> Restriction:
    """A linear obstacle measured ALONG a roadway."""
    return Restriction(
        kind=kind,
        source_id=source_id,
        buffer_m=buffer_m,
        road_key=road_key,
        arc_m=extent_m,
        anchor_m=anchor_m,
        side=side,
        label=label,
    )


# --------------------------------------------------------------------------- #
# async tests
# --------------------------------------------------------------------------- #
# `anyio` is already in the venv as an httpx/starlette dependency and ships a
# pytest plugin, so async tests need no extra install. Declaring the backend once
# here keeps every async test module free of boilerplate: mark the module with
# `pytestmark = pytest.mark.anyio` and write `async def test_...`.

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
