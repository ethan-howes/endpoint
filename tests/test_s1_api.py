"""HTTP surface of S1: routing, validation, readiness, and the degraded path.

These tests inject a hand-built ``StreetNetwork`` into the service's in-process
memo, so they exercise the real route -> service -> ranking path with no disk
cache and no network access. That keeps them fast and deterministic, which
matters because these are the tests that will still be passing when a demo is
five minutes away and Overpass is down.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shared.geo import tile_bbox, tile_cache_id
from shared.models import LatLng, LegalSpotsRequest
from services.s1_legal_spots import main, service
from services.s1_legal_spots.main import app
from services.s1_legal_spots.network import ParkingLot, StreetNetwork

from .conftest import TEST_ORIGIN, make_road, point_restriction
from shared.models import RestrictionKind

RIDER = LatLng(lat=TEST_ORIGIN[0], lng=TEST_ORIGIN[1])
RADIUS = 150.0


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_memo():
    """Each test starts with an empty memo, so injection is explicit and no test
    can accidentally pass on another test's cached network."""
    service._NETWORK_CACHE.clear()
    yield
    service._NETWORK_CACHE.clear()


@pytest.fixture
def no_data(monkeypatch):
    """Force the "there is no map data at all" state.

    Without this, these tests depend on whether someone happens to have run
    ``prefetch_demo_area`` -- so ``test_reports_no_data_before_prefetch`` would
    start failing the moment the demo area was warmed, which is precisely when you
    least want a red suite. The state is simulated rather than assumed.
    """
    monkeypatch.setattr(service, "read_cache", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(service, "read_fixture", lambda *a, **k: None, raising=True)
    # /ready lives in main.py and imports read_cache itself, so patching only the
    # service module would leave the readiness probe reading the real disk.
    monkeypatch.setattr(main, "read_cache", lambda *a, **k: None, raising=True)
    service._NETWORK_CACHE.clear()
    return True


def install_network(rider: LatLng = RIDER, radius: float = RADIUS) -> StreetNetwork:
    """Register a small but realistic network under the key the service will ask for."""
    from shared.geo import LocalFrame

    frame = LocalFrame(rider.lat, rider.lng)
    roads = [
        make_road(frame, way_id="way/1", tags={"name": "SW 2nd St"}),
        make_road(frame, way_id="way/2", tags={"name": "University Ave"}),
    ]
    # a hydrant on the north curb of the first street
    hydrant_xy = frame.to_m(TEST_ORIGIN[0] + 0.0002, TEST_ORIGIN[1])
    restrictions = [
        point_restriction(
            frame, RestrictionKind.FIRE_HYDRANT, "node/1", 4.6, at=TEST_ORIGIN
        )
    ]
    net = StreetNetwork(
        frame=frame,
        bbox=tile_bbox(rider.lat, rider.lng, radius),
        roads=roads,
        restrictions=restrictions,
        lots=[],
        source="test",
    )
    tile = tile_bbox(rider.lat, rider.lng, radius)
    center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
    service._NETWORK_CACHE[tile_cache_id(center[0], center[1], radius, "roads")] = net
    return net


class TestHealth:
    def test_health_is_ok(self, client):
        assert client.get("/health").json() == {"status": "ok"}


class TestReady:
    def test_reports_no_data_before_prefetch(self, client, no_data):
        """A perfectly healthy service with no data loaded is the failure mode you
        least want to discover during a pitch, so `/ready` is separate from
        `/health` and answers 503."""
        r = client.get("/ready", params={"lat": RIDER.lat, "lng": RIDER.lng})
        assert r.status_code == 503
        assert r.json()["status"] == "no_data"

    def test_reports_ready_once_a_network_is_installed(self, client):
        install_network()
        r = client.get("/ready", params={"lat": RIDER.lat, "lng": RIDER.lng})
        assert r.status_code == 200
        assert r.json()["in_memory"] is True

    def test_includes_the_tile_and_query_bbox(self, client):
        install_network()
        body = client.get("/ready", params={"lat": RIDER.lat, "lng": RIDER.lng}).json()
        assert "tile" in body and "query_bbox" in body


class TestSpotsLegal:
    def test_returns_spots_for_a_cached_network(self, client):
        install_network()
        r = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}, "radius_m": RADIUS},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == len(body["spots"])
        assert body["count"] > 0
        assert body["cached"] is True
        assert body["source"] == "test"

    def test_spot_ids_match_the_documented_shape(self, client):
        """ENDPOINT.md's example id is `s1_0042`; the UI and any recorded demo
        reference spots by id, so the shape is part of the contract."""
        install_network()
        body = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}},
        ).json()
        assert body["spots"][0]["spot_id"].startswith("s1_")

    def test_every_spot_carries_every_field_the_ui_needs(self, client):
        install_network()
        body = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}},
        ).json()
        for s in body["spots"]:
            for field in (
                "spot_id", "stop_point", "side", "curb_bearing_deg",
                "walk_distance_m", "confidence", "legality_basis", "segment_id",
            ):
                assert field in s, f"{field} missing from spot"

    def test_never_returns_verified_or_detected(self, client):
        """`verified` is for official sources, `detected` for S3. S1 must not
        claim either, or the whole confidence tier stops meaning anything."""
        install_network()
        body = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}},
        ).json()
        assert {s["confidence"] for s in body["spots"]} <= {"likely", "unverified"}

    def test_reports_total_and_truncation(self, client):
        install_network()
        body = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}},
        ).json()
        assert "total_candidates" in body
        assert isinstance(body["truncated"], bool)
        assert body["truncated"] == (body["total_candidates"] > body["count"])

    def test_rejects_unknown_request_fields(self, client):
        """A typo'd field must fail at the boundary rather than being silently
        ignored and defaulting the wrong way."""
        r = client.post(
            "/spots/legal",
            json={
                "rider_location": {"lat": RIDER.lat, "lng": RIDER.lng},
                "radius": 150,          # typo: the real key is radius_m
            },
        )
        assert r.status_code == 422

    def test_rejects_out_of_range_radius(self, client):
        r = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}, "radius_m": 9999},
        )
        assert r.status_code == 422

    def test_rejects_an_impossible_latitude(self, client):
        r = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": 95.0, "lng": -80.37}},
        )
        assert r.status_code == 422

    def test_a_swapped_lat_lng_is_NOT_caught_by_validation(self, client):
        """An honest negative test, and the reason the contract is built the way it
        is.

        For a US location both -80.37 and 25.75 are plausible values for EITHER
        field, so range validation cannot catch a lat/lng swap -- the request is
        simply valid and points at the wrong place (near Buenos Aires). The
        protection against that is structural, not declarative: ``LatLng`` uses
        named fields so no bare tuple is ever passed around, and the
        ``as_tuple`` / ``as_lnglat`` helpers make the two orderings explicit at
        every boundary. See ``test_geo.py::test_frame_roundtrips_lat_lng``.

        Pinned here so that if someone later "helpfully" loosens validation, this
        documents what was actually being relied on.
        """
        swapped = {"lat": -80.37, "lng": 25.75}
        assert LatLng(**swapped)  # valid model, wrong place
        r = client.post("/spots/legal", json={"rider_location": swapped})
        assert r.status_code == 200  # the service cannot tell; the types must

    def test_clamps_an_absurd_radius_rather_than_failing(self, client):
        """clamp_radius is the service-level backstop behind the 422 on the model.

        The 0 case is a real trap: ``radius_m or DEFAULT`` would turn a requested
        zero into 150 m."""
        assert service.clamp_radius(10_000) == 500.0
        assert service.clamp_radius(0) == 1.0
        assert service.clamp_radius(-5) == 1.0
        assert service.clamp_radius(None) == 150.0
        assert service.clamp_radius(150) == 150.0


class TestDegradedFallback:
    def test_returns_a_labelled_fallback_spot_when_data_is_unavailable(self, client, monkeypatch, no_data):
        """ENDPOINT.md section 4.5: no service failure should break a ride. With no
        data the honest answer is one unverified spot at the rider's own location,
        clearly labelled -- not an error, and not a fabricated confident spot."""

        async def boom(self, tile, radius_m):
            raise RuntimeError("no network in tests")

        monkeypatch.setattr(
            service.OsmRegulationSource, "fetch", boom, raising=True
        )
        r = client.post(
            "/spots/legal",
            json={"rider_location": {"lat": RIDER.lat, "lng": RIDER.lng}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "fallback"
        assert body["count"] == 1
        assert body["spots"][0]["confidence"] == "unverified"
        assert body["fallbacks_used"]
        assert body["spots"][0]["notes"]


class TestExplainRoute:
    def test_explains_why_candidates_were_dropped(self, client):
        install_network()
        r = client.get(
            "/spots/explain", params={"lat": RIDER.lat, "lng": RIDER.lng}
        )
        assert r.status_code == 200
        body = r.json()
        assert "rejected" in body
        assert body["network_summary"]["roads"] == 2
        assert body["error"] is None

    def test_reports_an_error_instead_of_fetching(self, client, no_data):
        """The diagnostic must never be the thing that hangs during a rehearsal."""
        r = client.get(
            "/spots/explain", params={"lat": RIDER.lat, "lng": RIDER.lng}
        )
        assert r.status_code == 200
        assert r.json()["error"] is not None
        assert r.json()["network_summary"] == {}
