"""The S1 legality fixes from the review.

1. Roads a car may not stop on produce no kerb spots (but stay in the network,
   so their junctions still exclude the public street's kerb).
2. Parking lots are offered, one spot each, at an accessible space, an aisle, or
   the lot's edge, in that order of preference.
3. ``parking:<side>=lane`` is permissive (ENDPOINT.md section 6 S1 step 3).
4. ``likely`` needs an explicit parking tag; a ``lanes`` tag is not one.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Polygon

from services.s1_legal_spots.curb import generate_candidates
from services.s1_legal_spots.legality import judge_candidate, resolve_side_legality
from services.s1_legal_spots.lots import lot_stops, to_spot
from services.s1_legal_spots.network import ParkingLot, StreetNetwork
from services.s1_legal_spots.overpass import estimate_offsets, parse_roads, road_stoppable
from shared.models import Confidence, CurbAccess, LegalityBasis, SpotType

from .conftest import make_road


class TestRoadsCarsCannotUse:
    @pytest.mark.parametrize("tags,why", [
        ({"highway": "service", "access": "private"}, "access=private"),
        ({"highway": "service", "access": "customers"}, "access=customers"),
        ({"highway": "service", "access": "no"}, "access=no"),
        ({"highway": "service", "service": "drive-through"}, "service=drive-through"),
        ({"highway": "service", "service": "emergency_access"}, "service=emergency_access"),
        ({"highway": "service", "service": "parking_aisle"}, "service=parking_aisle"),
        ({"highway": "pedestrian"}, "highway=pedestrian"),
        ({"highway": "pedestrian", "area": "yes", "motor_vehicle": "yes"}, "area=yes"),
    ])
    def test_excluded(self, tags, why):
        ok, reason = road_stoppable(tags)
        assert not ok and why in reason

    @pytest.mark.parametrize("tags", [
        {"highway": "service"},
        {"highway": "residential"},
        {"highway": "service", "access": "private", "motor_vehicle": "destination"},
        {"highway": "pedestrian", "motor_vehicle": "yes"},
    ])
    def test_allowed(self, tags):
        assert road_stoppable(tags)[0]

    def test_an_unstoppable_road_yields_no_candidates(self, frame):
        road = make_road(frame, tags={"highway": "service"})
        blocked = type(road)(**{**road.__dict__, "stoppable": False})
        assert generate_candidates(road, "right")
        assert generate_candidates(blocked, "right") == []

    def test_an_unstoppable_road_stays_in_the_network(self, frame):
        """Its junction with the public street must still exclude that kerb."""
        payload = {"elements": [{
            "type": "way", "id": 1, "nodes": [1, 2],
            "tags": {"highway": "service", "access": "private"},
            "geometry": [{"lat": 25.7569, "lon": -80.3722}, {"lat": 25.7571, "lon": -80.3722}],
        }]}
        roads = parse_roads(payload, frame)
        assert len(roads) == 1 and roads[0].stoppable is False


class TestLaneTag:
    @pytest.mark.parametrize("value", ["lane", "on_kerb", "half_on_kerb", "shoulder"])
    def test_is_permissive_and_tagged(self, value):
        v = resolve_side_legality(make_road(None, tags={"parking:right": value}), "right")  # type: ignore[arg-type]
        assert v.verdict == "permissive" and v.basis == LegalityBasis.TAGGED_PERMISSIVE

    def test_on_kerb_parking_does_not_widen_the_road(self):
        base = estimate_offsets({"highway": "residential", "lanes": "2"})
        lane = estimate_offsets({"highway": "residential", "lanes": "2", "parking:right": "lane"})
        kerb = estimate_offsets({"highway": "residential", "lanes": "2", "parking:right": "on_kerb"})
        assert lane[1] > base[1]
        assert kerb[1] == base[1]


class TestConfidence:
    def test_a_lanes_tag_alone_is_unverified(self, frame):
        road = make_road(frame, tags={"highway": "residential", "lanes": "2"})
        v = judge_candidate(generate_candidates(road, "right")[0], [], [])
        assert v.accepted and v.confidence is Confidence.UNVERIFIED

    def test_an_explicit_parking_tag_is_likely(self, frame):
        road = make_road(frame, tags={"highway": "residential", "parking:both": "lane"})
        v = judge_candidate(generate_candidates(road, "right")[0], [], [])
        assert v.confidence is Confidence.LIKELY


class TestParkingLots:
    """A 40 x 40 m lot 20 m east of the rider, in frame metres."""

    def _net(self, frame, *, aisle=False, space=False, permitted=True, reason=""):
        ox, oy = frame.to_m(25.756918, -80.372182)
        ring = [(ox + 20, oy - 20), (ox + 60, oy - 20), (ox + 60, oy + 20), (ox + 20, oy + 20), (ox + 20, oy - 20)]
        lot = ParkingLot("way/9", LineString(ring), "Test Lot", permitted, reason)
        roads = []
        if aisle:
            lat1, lng1 = frame.to_ll(ox + 40, oy - 15)
            lat2, lng2 = frame.to_ll(ox + 40, oy + 15)
            roads.append(make_road(frame, way_id="way/aisle", latlngs=[(lat1, lng1), (lat2, lng2)],
                                   tags={"highway": "service", "service": "parking_aisle"}))
        spaces = [("way/s", Polygon([(ox + 50, oy + 10), (ox + 53, oy + 10), (ox + 53, oy + 15), (ox + 50, oy + 15)]))] if space else []
        net = StreetNetwork(frame=frame, bbox=(0, 0, 0, 0), roads=roads, lots=[lot], accessible_spaces=spaces)
        return net, (ox, oy)

    def test_no_aisle_means_the_nearest_edge(self, frame):
        net, rider = self._net(frame)
        [ls] = lot_stops(net, rider, 150.0)
        assert ls.placed_on == "edge"
        assert ls.x - rider[0] == pytest.approx(20.0, abs=0.1)

    def test_an_aisle_is_preferred_over_the_edge(self, frame):
        net, rider = self._net(frame, aisle=True)
        [ls] = lot_stops(net, rider, 150.0)
        assert ls.placed_on == "aisle"
        assert ls.x - rider[0] == pytest.approx(40.0, abs=0.1)

    def test_an_accessible_space_is_preferred_over_everything(self, frame):
        net, rider = self._net(frame, aisle=True, space=True)
        [ls] = lot_stops(net, rider, 150.0)
        assert ls.accessible and ls.placed_on == "accessible_space"
        spot = to_spot(frame, net, ls, "s1_0001")
        assert spot.spot_type is SpotType.PARKING_LOT
        assert spot.curb_access is CurbAccess.FLUSH
        assert spot.confidence is Confidence.LIKELY
        assert "at an accessible parking space" in spot.notes

    def test_a_restricted_lot_is_never_offered(self, frame):
        net, rider = self._net(frame, permitted=False, reason="access=customers")
        assert lot_stops(net, rider, 150.0) == []

    def test_a_lot_beyond_the_radius_is_not_offered(self, frame):
        net, rider = self._net(frame)
        assert lot_stops(net, rider, 10.0) == []

    def test_an_untagged_lot_is_unverified(self, frame):
        net, rider = self._net(frame)
        spot = to_spot(frame, net, lot_stops(net, rider, 150.0)[0], "s1_0001")
        assert spot.confidence is Confidence.UNVERIFIED
        assert spot.side is None
