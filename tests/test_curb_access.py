"""Curb access at the stop point: which kerb a rider meets at the car door.

The rule under test (``services/s1_legal_spots/curb_access.py``): the nearest
flush or lowered kerb within 15 m on the same side of the road wins; a raised one
in range makes the spot ``raised``; nothing mapped makes it ``unknown``.
"""

from __future__ import annotations

import pytest

from services.s1_legal_spots.curb import Candidate
from services.s1_legal_spots.curb_access import assess
from services.s1_legal_spots.network import Kerb
from services.s1_legal_spots.overpass import parse_kerbs
from shared.models import CurbAccess

from .conftest import make_road


@pytest.fixture
def road(frame):
    # West-to-east, so the right-hand side is south (negative y).
    return make_road(frame)


def _candidate(road, frame, side: str = "right", along_m: float = 100.0) -> Candidate:
    x0, y0 = road.polyline.xs[0], road.polyline.ys[0]
    dy = -3.5 if side == "right" else 3.5
    return Candidate(
        road=road, side=side, arc_m=along_m, x=x0 + along_m, y=y0 + dy,
        curb_bearing_deg=180.0 if side == "right" else 0.0, offset_m=3.5,
    )


def _kerb(cand: Candidate, dx: float, dy: float, kind: str, **kw) -> Kerb:
    return Kerb(node_id="node/1", kind=kind, x=cand.x + dx, y=cand.y + dy, **kw)


class TestAssess:
    def test_a_lowered_kerb_in_range_on_the_same_side(self, road, frame):
        c = _candidate(road, frame)
        access, d, src = assess(c, [_kerb(c, 8.0, -1.0, "lowered")])
        assert access is CurbAccess.LOWERED
        assert d == pytest.approx(8.1, abs=0.1)
        assert src == "osm"

    def test_a_ramp_across_the_street_does_not_count(self, road, frame):
        """Reaching it means crossing the road the car is stopped on."""
        c = _candidate(road, frame, side="right")
        across = _kerb(c, 2.0, 7.0, "lowered")  # 3.5 m north of the centreline
        assert assess(c, [across])[0] is CurbAccess.UNKNOWN

    def test_beyond_fifteen_metres_is_unknown(self, road, frame):
        c = _candidate(road, frame)
        assert assess(c, [_kerb(c, 16.0, 0.0, "lowered")]) == (CurbAccess.UNKNOWN, None, None)

    def test_nothing_mapped_is_unknown_not_raised(self, road, frame):
        """Around FIU only ramps were mapped, so absence says nothing."""
        c = _candidate(road, frame)
        assert assess(c, [])[0] is CurbAccess.UNKNOWN

    def test_the_nearest_step_free_kerb_wins(self, road, frame):
        c = _candidate(road, frame)
        access, d, _ = assess(c, [
            _kerb(c, 12.0, 0.0, "lowered"),
            _kerb(c, 4.0, 0.0, "flush"),
        ])
        assert access is CurbAccess.FLUSH and d == pytest.approx(4.0)

    def test_a_ramp_beats_a_nearer_raised_kerb(self, road, frame):
        """The rider can use the ramp; the raised kerb beside the door is
        what they would otherwise have to step off."""
        c = _candidate(road, frame)
        access, d, _ = assess(c, [
            _kerb(c, 1.0, 0.0, "raised"),
            _kerb(c, 10.0, 0.0, "lowered"),
        ])
        assert access is CurbAccess.LOWERED and d == pytest.approx(10.0)

    def test_only_raised_in_range_is_raised(self, road, frame):
        c = _candidate(road, frame)
        assert assess(c, [_kerb(c, 3.0, 0.0, "raised")])[0] is CurbAccess.RAISED

    def test_a_crossing_node_kerb_applies_to_both_sides(self, road, frame):
        """Older tagging puts ``kerb=*`` on the crossing node in the middle of
        the road, describing both of its ends."""
        left = _candidate(road, frame, side="left")
        right = _candidate(road, frame, side="right")
        mid = Kerb(node_id="node/2", kind="lowered", x=left.x + 5.0,
                   y=road.polyline.ys[0], on_centerline=True)
        assert assess(left, [mid])[0] is CurbAccess.LOWERED
        assert assess(right, [mid])[0] is CurbAccess.LOWERED


class TestParseKerbs:
    def _payload(self, *tag_sets):
        return {"elements": [
            {"type": "node", "id": i, "lat": 25.7569, "lon": -80.3722, "tags": t}
            for i, t in enumerate(tag_sets, start=1)
        ]}

    def test_maps_heights_and_skips_unknown_values(self, frame):
        kerbs = parse_kerbs(self._payload(
            {"barrier": "kerb", "kerb": "lowered"},
            {"barrier": "kerb", "kerb": "flush"},
            {"barrier": "kerb", "kerb": "rolled"},
            {"barrier": "kerb"},                 # no height: skipped
            {"barrier": "kerb", "kerb": "weird"},
        ), frame)
        assert [k.kind for k in kerbs] == ["lowered", "flush", "raised"]

    def test_marks_crossing_node_kerbs_as_centreline(self, frame):
        kerbs = parse_kerbs(self._payload({"highway": "crossing", "kerb": "lowered"}), frame)
        assert kerbs[0].on_centerline is True

    def test_ignores_ways(self, frame):
        payload = {"elements": [{"type": "way", "id": 1, "tags": {"kerb": "lowered"}}]}
        assert parse_kerbs(payload, frame) == []
