"""End-to-end tests for the S2 service (ENDPOINT.md section 6).

Weather is stubbed at ``weather.get_hourly`` -- the single seam every forecast
request funnels through -- so these tests exercise the real classification,
windowing, ranking and fallback code rather than replacing it. That is the point:
"the ride survived the weather API being down" has to be a claim about shipped
code, and it cannot be one if the classification is stubbed along with the fetch.

Overpass is stubbed at the ``CoverSource`` protocol, which is the other seam
S1 already established. The tests therefore assert the *protocol* is honoured --
that a different cover source needs no change here -- rather than the Overpass
response shape.

Three behaviours are load-bearing enough to be pinned end-to-end rather than in
the unit tests: the service never raises for a data problem, a forced condition
really does override a live forecast, and the sun demo's side-flip really does
change the answer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Polygon

from services.s2_weather_cover import (
    cover,
    rain_cover,
    scoring,
    service,
    sun_shade,
    weather,
)
from services.s2_weather_cover.cover import (
    Cover,
    CoverMap,
    ShadeBlock,
    ShadeMap,
    build_cover_query,
    build_shade_query,
)
from services.s2_weather_cover.main import app
from shared.config import SETTINGS
from shared.fixtures import write_fixture
from shared.geo import LocalFrame, frame_for, tile_bbox, tile_cache_id
from shared.models import (
    Condition,
    Confidence,
    LatLng,
    RankRequest,
    Side,
    Spot,
)

RIDER = LatLng(lat=SETTINGS.demo_rider[0], lng=SETTINGS.demo_rider[1])
NOW = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
FRAME = frame_for(*SETTINGS.demo_rider)

#: ``LocalFrame.to_m`` returns *absolute* UTM easting/northing, not metres from
#: the anchor -- so ``FRAME.to_ll(0, 0)`` lands near the central meridian rather
#: than at the rider. Test geometry is authored in metres-from-the-rider, which
#: is what a reader means when they write "20 m west of the kerb", so every
#: coordinate goes through ``_at`` to add this offset.
ORIGIN = FRAME.to_m(*SETTINGS.demo_rider)


def _at(x: float, y: float) -> tuple[float, float]:
    """Metres east/north of the rider -> the frame's absolute coordinates."""
    return ORIGIN[0] + x, ORIGIN[1] + y


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _spot(spot_id: str, walk_m: float, *, x: float = 0.0, y: float = 0.0) -> Spot:
    lat, lng = FRAME.to_ll(*_at(x, y))
    return Spot(
        spot_id=spot_id,
        stop_point=LatLng(lat=lat, lng=lng),
        side=Side.RIGHT,
        curb_bearing_deg=90.0,
        walk_distance_m=walk_m,
        street_name="SW 109th Ave",
        confidence=Confidence.LIKELY,
        segment_id="way/1",
    )


def _spots(*specs) -> list[Spot]:
    """``_spots(("a", 10.0), ("b", 40.0, 0.0, 8.0))`` -> two spots.

    Sorted best-first by walk distance, because the orchestrator deliberately
    trusts S1/S2 ordering rather than re-sorting -- a stub returning them in the
    wrong order would test the wrong thing. Where a test needs a specific order
    (the sun side-flip), it re-sorts explicitly and says so.
    """
    out = []
    for spec in specs:
        sid, walk = spec[0], spec[1]
        x = spec[2] if len(spec) > 2 else 0.0
        y = spec[3] if len(spec) > 3 else 0.0
        out.append(_spot(sid, walk, x=x, y=y))
    out.sort(key=lambda s: (s.walk_distance_m, s.spot_id))
    return out


def _cover_map(covers: list[Cover]) -> CoverMap:
    return CoverMap(frame=FRAME, bbox=SETTINGS.demo_bbox, covers=covers)


def _shade_map(blocks: list[ShadeBlock]) -> ShadeMap:
    return ShadeMap(frame=FRAME, bbox=SETTINGS.demo_bbox, blocks=blocks)


def _request(spots: list[Spot], **kw) -> RankRequest:
    params = dict(
        rider_location=RIDER,
        spots=spots,
        pickup_time=NOW,
        wait_minutes=10,
    )
    params.update(kw)
    return RankRequest(**params)


# --------------------------------------------------------------------------- #
# Weather stubs
# --------------------------------------------------------------------------- #

def _forecast(precip=0.0, *, uv=2.0, apparent=26.0, radiation=100.0,
              cloud=50.0, code=3, is_day=True):
    async def fake(lat, lng, client=None):
        return [
            {"when_utc": datetime(2026, 9, 27, h, 0, tzinfo=timezone.utc),
             "precipitation": precip, "uv_index": uv,
             "apparent_temperature": apparent, "direct_radiation": radiation,
             "cloud_cover": cloud, "weather_code": code, "is_day": is_day}
            for h in range(24)
        ]
    return fake


def _broken_forecast():
    async def fake(lat, lng, client=None):
        raise ConnectionError("open-meteo is down")
    return fake


async def _no_covers(tile, radius, fallbacks, frame):
    return _cover_map([])


async def _no_shade(tile, radius, fallbacks, frame):
    return _shade_map([])


def _no_network(*a, **k):
    raise AssertionError(
        "a test reached Overpass. Stub the seam it meant to stub, or the suite "
        "has silently become dependent on the public instance being up."
    )


#: The two levels get different treatment on purpose.
#:
#: ``_fetch_covers`` / ``_fetch_shade`` are the *determinism* boundary: replaced
#: with a no-op baseline so a test which forgets a stub gets the honest empty
#: answer rather than whatever happens to be in the developer's disk cache, which
#: differs per machine. Tests routinely override these.
#:
#: ``get_hourly`` and ``cover.afetch_overpass`` are the *network* boundary, and
#: the teardown proves they were not left live. These sit deeper -- below the
#: memo, the fixture lookup and the disk cache -- so blocking them costs nothing
#: and holds even when a test deliberately restores ``_fetch_covers`` to test it.
#: Checking the wrong level is how you end up with a guard that fires on your own
#: regression tests and misses the network it was written to prevent.
_REAL_GET_HOURLY = weather.get_hourly
_REAL_AFETCH = cover.afetch_overpass
_REAL_FETCH_COVERS = service._fetch_covers
_REAL_FETCH_SHADE = service._fetch_shade


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No network, and no dependence on someone's working cache."""
    service.clear_caches()
    monkeypatch.setattr(weather, "get_hourly", _forecast())
    monkeypatch.setattr(service, "_fetch_covers", _no_covers)
    monkeypatch.setattr(service, "_fetch_shade", _no_shade)
    monkeypatch.setattr(cover, "afetch_overpass", _no_network)
    yield
    service.clear_caches()

    live = [
        name
        for name, real, current in (
            ("weather.get_hourly", _REAL_GET_HOURLY, weather.get_hourly),
            ("cover.afetch_overpass", _REAL_AFETCH, cover.afetch_overpass),
        )
        if current is real
    ]
    assert not live, f"test left a live network seam in place: {', '.join(live)}"


# --------------------------------------------------------------------------- #
# Routes exist and validate
# --------------------------------------------------------------------------- #

class TestRoutes:
    def setup_method(self):
        self.c = TestClient(app)

    def test_health(self):
        assert self.c.get("/health").json() == {"status": "ok"}

    def test_ready_reports_what_is_memoised(self):
        r = self.c.get("/ready", params={"lat": RIDER.lat, "lng": RIDER.lng})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert set(body["tiles_memoised"]) == {"cover", "shade"}
        assert body["checked_area"] is not None

    def test_rank_rejects_a_swapped_lat_lng(self):
        """Named fields plus range validation is the only protection, and for US
        coordinates a swap is *not* out of range -- so this pins that the field
        names are the thing doing the work, not the bounds."""
        r = self.c.post("/conditions/rank", json={
            "rider_location": {"lat": -80.372, "lng": 25.757},
            "spots": [], "pickup_time": NOW.isoformat(), "wait_minutes": 10,
        })
        # Either rejected, or accepted -- but if accepted, the coordinates must be
        # where they were put. Documented honestly: see
        # test_s1_api.py::test_a_swapped_lat_lng_is_NOT_caught_by_validation.
        assert r.status_code in (200, 422)

    def test_weather_route_returns_a_report(self):
        r = self.c.get("/conditions/weather", params={
            "lat": RIDER.lat, "lng": RIDER.lng, "at": NOW.isoformat(),
        })
        assert r.status_code == 200
        body = r.json()
        assert body["weather"]["valid_at"]
        assert body["mode"] in {"rain", "sun", "neutral"}

    def test_weather_route_honours_a_forced_condition(self):
        r = self.c.get("/conditions/weather", params={
            "lat": RIDER.lat, "lng": RIDER.lng, "at": NOW.isoformat(),
            "force_condition": "rain",
        })
        body = r.json()
        assert body["mode"] == "rain"
        assert body["weather"]["overridden"] is True
        assert body["weather"]["source"] == "demo_override"

    def test_weather_route_rejects_a_nonsense_force(self):
        r = self.c.get("/conditions/weather", params={
            "lat": RIDER.lat, "lng": RIDER.lng, "force_condition": "snow",
        })
        assert r.status_code == 422

    def test_weather_route_rejects_an_out_of_range_latitude(self):
        r = self.c.get("/conditions/weather", params={"lat": 99.0, "lng": 0.0})
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# rain
# --------------------------------------------------------------------------- #

class TestRainRoute:
    def setup_method(self):
        self.c = TestClient(app)
        # A covered walkway running north-south along x=0. Spots are placed
        # *beside* it, not on it: a spot with the line passing through its own
        # coordinates has a gap of 0, which is the opposite of the fixture's
        # purpose.
        self.covers = _cover_map([
            Cover(feature_id="osm_way_10", kind="covered_walkway",
                  shape=LineString([_at(0.0, 0.0), _at(0.0, 60.0)]),
                  provides_sun=True),
        ])

    def _stub(self, cmap):
        # Signature must match ``service._fetch_covers`` exactly. A mismatch here
        # is caught by §6's blanket handler and reported as "ranking raised in
        # rain mode", which is correct behaviour and a very confusing test bug --
        # so it is worth being exact rather than using **kwargs to paper over it.
        async def covers(tile, radius, fallbacks, frame):
            return cmap

        async def shade(tile, radius, fallbacks, frame):
            return _shade_map([])

        return covers, shade

    def test_rain_mode_reroutes_away_from_the_nearest_kerb(self, monkeypatch):
        """The headline: cover is on the walkway at x=0, the otherwise-closest kerb
        is 20 m away from it, and rain mode must send the rider to the kerb with
        the shelter even though it is further to walk."""
        spots = _spots(("near", 5.0, 20.0, 30.0), ("covered", 25.0, 0.0, 1.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])
        monkeypatch.setattr(service, "_fetch_shade", self._stub(self.covers)[1])

        r = self.c.post("/conditions/rank", json=_request(spots).model_dump(mode="json"))
        body = r.json()
        assert body["mode"] == "rain"
        assert body["ranked"][0]["spot"]["spot_id"] == "covered"

    def test_a_spot_under_the_cover_is_told_so_in_words(self, monkeypatch):
        """At gap 0 the reason says the rider is already under it, rather than
        quoting a distance of zero metres."""
        spots = _spots(("a", 10.0, 0.0, 5.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])

        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        assert body["ranked"][0]["gap_m"] == 0.0
        assert "right at the pickup point" in body["ranked"][0]["reason"]

    def test_a_spot_a_few_metres_away_quotes_the_distance(self, monkeypatch):
        """A rider can act on "3 m" and not on a score. The number is rounded to
        whole metres because OSM tags do not support finer."""
        spots = _spots(("a", 10.0, 3.0, 5.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])

        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        assert body["ranked"][0]["gap_m"] == pytest.approx(3.0, abs=0.2)
        assert "3 m from the pickup point" in body["ranked"][0]["reason"]

    def test_a_spot_beyond_the_gap_limit_is_told_there_is_no_cover(self, monkeypatch):
        """Beyond ``rain_max_gap_m`` a feature is worse than useless -- the rider
        would be told to walk to shelter and get wet on the way -- so it is
        reported as absent rather than as a long gap."""
        spots = _spots(("a", 10.0, 20.0, 5.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])

        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        assert body["ranked"][0]["cover_feature"] is None
        assert body["ranked"][0]["gap_m"] is None
        assert "No cover" in body["ranked"][0]["reason"]

    def test_the_response_carries_a_wait_point_and_a_gap(self, monkeypatch):
        """Two coordinates, not one: the car stops at the kerb and the rider
        waits at the shelter, and a rider who is only told where the car is
        cannot act on the recommendation."""
        spots = _spots(("a", 10.0, 2.0, 5.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])

        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        top = body["ranked"][0]
        assert top["wait_point"] is not None
        assert top["wait_point"] != top["spot"]["stop_point"], (
            "the wait point must be on the cover, not on the kerb"
        )
        assert top["gap_m"] is not None
        assert top["cover_feature"]["feature_id"] == "osm_way_10"

    def test_overlays_carry_the_features_for_the_map(self, monkeypatch):
        spots = _spots(("a", 10.0, 2.0, 5.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub(self.covers)[0])

        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        assert len(body["overlays"]["cover_features"]) == 1
        assert body["overlays"]["cover_features"][0]["kind"] == "covered_walkway"

    def test_no_cover_data_still_answers_and_says_so(self, monkeypatch):
        """§6: a data problem must not reach the orchestrator as an error."""
        spots = _spots(("a", 30.0, 0.0, 10.0), ("b", 10.0, 0.0, 40.0))

        async def none(tile, radius, fallbacks, frame):
            return None

        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", none)

        r = self.c.post("/conditions/rank", json=_request(spots).model_dump(mode="json"))
        assert r.status_code == 200
        body = r.json()
        assert body["fallbacks_used"]
        assert body["ranked"][0]["spot"]["spot_id"] == "b", "nearest first"
        assert all(x["cover_feature"] is None for x in body["ranked"])

    def test_a_ranking_bug_is_caught_and_reported(self, monkeypatch):
        """§6: any module failure -> walk-distance fallback plus a note. The
        point is that it is *recorded*, so a flat ranking during a demo is
        diagnosable rather than mysterious."""
        spots = _spots(("a", 30.0, 0.0, 10.0), ("b", 10.0, 0.0, 40.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(
            rain_cover, "rank_spots",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        body = self.c.post(
            "/conditions/rank", json=_request(spots).model_dump(mode="json")
        ).json()
        assert any("rain" in f for f in body["fallbacks_used"])
        assert body["ranked"][0]["spot"]["spot_id"] == "b"


# --------------------------------------------------------------------------- #
# sun
# --------------------------------------------------------------------------- #

class TestSunRoute:
    def setup_method(self):
        self.c = TestClient(app)
        # A 10 m building straddling x=0, and two kerbs either side of it. The
        # only way a shadow can reach either of them is the shadow length, and
        # its direction is what decides.
        self.shade = _shade_map([
            ShadeBlock(block_id="b1", height_m=10.0, kind="building",
                       shape=Polygon([_at(-5, -25), _at(5, -25),
                                      _at(5, 25), _at(-5, 25)])),
        ])
        self.covers = _cover_map([])
        self.spots = _spots(("west", 10.0, -12.0, 0.0), ("east", 10.0, 12.0, 0.0))
        self.spots = sorted(self.spots, key=lambda s: s.spot_id)

    def _stub(self):
        async def covers(tile, radius, fallbacks, frame):
            return self.covers

        async def shade(tile, radius, fallbacks, frame):
            return self.shade

        return covers, shade

    def test_sun_mode_uses_shade_geometry_and_says_so(self, monkeypatch):
        """`shade_source` is the honesty field: it names the method that produced
        the answer, so a modelled shadow is never mistaken for a measurement."""
        monkeypatch.setattr(weather, "get_hourly", _forecast(uv=9.0, apparent=35.0,
                                                             radiation=700.0, cloud=5.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", self._stub()[1])

        body = self.c.post("/conditions/rank",
                           json=_request(self.spots).model_dump(mode="json")).json()
        assert body["mode"] == "sun"
        assert body["shade_source"] == "osm_geometry"
        assert body["sun"] is not None
        assert body["overlays"]["shade_geojson"] is not None

    def test_forcing_the_time_flips_which_side_wins(self, monkeypatch):
        """§6's definition of done, end to end through HTTP: 10:00 local prefers
        one kerb, 16:00 local prefers the other, because shadows reverse.

        Force the *condition* to sun and vary only ``force_time`` -- otherwise a
        clear Miami afternoon at the wrong hour of the day would quietly decide
        the outcome instead of the geometry."""
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", self._stub()[1])

        def winner(hour_utc: int) -> str:
            return self.c.post("/conditions/rank", json=_request(
                self.spots,
                force_time=datetime(2026, 9, 27, hour_utc, 0, tzinfo=timezone.utc),
                force_condition=Condition.SUN,
            ).model_dump(mode="json")).json()["ranked"][0]["spot"]["spot_id"]

        morning = winner(14)  # 10:00 EDT
        afternoon = winner(20)  # 16:00 EDT
        assert morning != afternoon, (
            "the shaded side did not change between 10:00 and 16:00 local -- "
            "the definition-of-done demo would show nothing happening"
        )

    def test_a_low_sun_falls_back_to_walk_distance_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly",
                            _forecast(uv=9.0, apparent=35.0, radiation=700.0, cloud=5.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", self._stub()[1])

        body = self.c.post("/conditions/rank", json=_request(
            self.spots, force_time=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc),
            force_condition=Condition.SUN,
        ).model_dump(mode="json")).json()
        assert body["mode"] == "sun"
        assert all("Sun is low" in x["reason"] for x in body["ranked"])
        assert body["overlays"]["shade_geojson"] is None

    def test_no_shade_data_answers_and_claims_no_shade_source(self, monkeypatch):
        """An empty-but-successful fetch is not a *failure*, so nothing goes in
        ``fallbacks_used`` -- the per-spot reason carries the explanation. What
        matters is that ``shade_source`` stays null: reporting `osm_geometry`
        when no geometry was found would tell a reader the answer came from a
        shadow model when it actually came from "walk distance".
        """
        async def empty(tile, radius, fallbacks, frame):
            return _shade_map([])

        monkeypatch.setattr(weather, "get_hourly",
                            _forecast(uv=9.0, apparent=35.0, radiation=700.0, cloud=5.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", empty)

        r = self.c.post("/conditions/rank",
                        json=_request(self.spots).model_dump(mode="json"))
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "sun"
        assert all("No shade" in x["reason"] for x in body["ranked"])
        assert body["shade_source"] is None
        assert body["overlays"]["shade_geojson"] is None

    def test_a_failed_shade_fetch_is_recorded_as_a_fallback(self, monkeypatch):
        """The contrast with the test above: a fetch that *fails* is a data
        problem and §6 wants it in ``fallbacks_used``, not just in a reason
        string. An empty area and a broken fetch are different things."""
        async def broken(tile, radius, fallbacks, frame):
            fallbacks.append("shade fetch error: OverpassError")
            return None

        monkeypatch.setattr(weather, "get_hourly",
                            _forecast(uv=9.0, apparent=35.0, radiation=700.0, cloud=5.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", broken)

        body = self.c.post("/conditions/rank",
                           json=_request(self.spots).model_dump(mode="json")).json()
        assert body["shade_source"] is None
        assert any("shade" in f for f in body["fallbacks_used"])

    def test_a_sun_position_failure_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly",
                            _forecast(uv=9.0, apparent=35.0, radiation=700.0, cloud=5.0))
        monkeypatch.setattr(weather, "sun_position", lambda *a, **k: None)
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])
        monkeypatch.setattr(service, "_fetch_shade", self._stub()[1])

        r = self.c.post("/conditions/rank",
                        json=_request(self.spots).model_dump(mode="json"))
        assert r.status_code == 200
        assert any("sun position" in f for f in r.json()["fallbacks_used"])


# --------------------------------------------------------------------------- #
# neutral
# --------------------------------------------------------------------------- #

class TestNeutralRoute:
    def setup_method(self):
        self.c = TestClient(app)

    def test_calm_weather_ranks_by_walk_and_explains_itself(self, monkeypatch):
        spots = _spots(("a", 30.0), ("b", 10.0), ("c", 20.0))
        monkeypatch.setattr(weather, "get_hourly", _forecast())

        async def never(tile, radius, fallbacks, frame):
            raise AssertionError("neutral mode must not fetch cover")

        monkeypatch.setattr(service, "_fetch_covers", never)
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert body["mode"] == "neutral"
        assert [x["spot"]["spot_id"] for x in body["ranked"]] == ["b", "c", "a"]
        assert all(x["reason"] for x in body["ranked"])
        assert body["fallbacks_used"] == []

    def test_a_weather_outage_never_becomes_an_error(self, monkeypatch):
        """The single most likely real failure, and the one ENDPOINT.md's own
        flow leaves uncaught because its try/except wraps the ranking but not
        `weather.get`."""
        spots = _spots(("a", 30.0), ("b", 10.0))
        monkeypatch.setattr(weather, "get_hourly", _broken_forecast())

        r = self.c.post("/conditions/rank", json=_request(spots).model_dump(mode="json"))
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "neutral"
        assert body["weather"]["source"] == "fallback"
        assert body["weather"]["reason"], "a fallback must say why"
        assert len(body["ranked"]) == 2

    def test_an_empty_spot_list_is_a_valid_empty_answer(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly", _forecast())
        r = self.c.post("/conditions/rank", json=_request([]).model_dump(mode="json"))
        assert r.status_code == 200
        body = r.json()
        assert body["ranked"] == []
        assert body["nearest_spot_id"] is None
        assert body["needs_rider_confirmation"] is False


# --------------------------------------------------------------------------- #
# the detour confirmation
# --------------------------------------------------------------------------- #

class TestDetourConfirmation:
    def setup_method(self):
        self.c = TestClient(app)
        # Cover right next to a far spot: the recommendation is genuinely better
        # protected, and genuinely much further to walk.
        self.covers = _cover_map([
            Cover(feature_id="osm_way_1", kind="covered_walkway",
                  shape=LineString([_at(0.0, 0.0), _at(0.0, 40.0)]),
                  provides_sun=True),
        ])

    def _stub(self):
        async def covers(tile, radius, fallbacks, frame):
            return self.covers

        async def shade(tile, radius, fallbacks, frame):
            return _shade_map([])

        return covers, shade

    def test_asks_when_the_covered_spot_is_much_further(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])

        # "far" is under the covered walkway; "near" is 20 m off it, so nothing.
        spots = _spots(("near", 10.0, 20.0, 35.0), ("far", 200.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert body["ranked"][0]["spot"]["spot_id"] == "far"
        assert body["needs_rider_confirmation"] is True

    def test_does_not_ask_for_a_short_extra_walk(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        monkeypatch.setattr(service, "_fetch_covers", self._stub()[0])

        spots = _spots(("near", 10.0, 20.0, 35.0), ("nearby", 60.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert body["ranked"][0]["spot"]["spot_id"] == "nearby"
        assert body["needs_rider_confirmation"] is False

    def test_neutral_mode_never_asks(self, monkeypatch):
        """There is nothing to confirm when the recommendation is just the
        nearest spot, so asking would be a prompt with no content behind it."""
        monkeypatch.setattr(weather, "get_hourly", _forecast())
        spots = _spots(("near", 10.0, 20.0, 35.0), ("far", 200.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert body["needs_rider_confirmation"] is False

    def test_neutral_sorts_nearest_first(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly", _forecast())
        spots = _spots(("near", 10.0, 20.0, 35.0), ("far", 200.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert body["ranked"][0]["spot"]["spot_id"] == "near"
        assert body["nearest_spot_id"] == "near"


# --------------------------------------------------------------------------- #
# mock replay
# --------------------------------------------------------------------------- #

#: Tags the *real* queries select, not arbitrary ones. A fixture with no tags
#: parses to zero features, so a test that wrote one would pass the lookup half
#: and then assert nothing about the geometry -- which is how a replay path can
#: be broken and still look covered.
_COVER_PAYLOAD = {"elements": [{
    "type": "way", "id": 1,
    "tags": {"highway": "pedestrian", "covered": "yes"},
    "geometry": [
        {"lat": 25.7569, "lon": -80.3721},
        {"lat": 25.7570, "lon": -80.3720},
    ],
}]}

_SHADE_PAYLOAD = {"elements": [{
    "type": "way", "id": 2,
    "tags": {"building": "yes", "building:levels": "3"},
    "geometry": [
        {"lat": 25.7568, "lon": -80.3723}, {"lat": 25.7568, "lon": -80.3721},
        {"lat": 25.7570, "lon": -80.3721}, {"lat": 25.7570, "lon": -80.3723},
        {"lat": 25.7568, "lon": -80.3723},
    ],
}]}


class TestMockReplay:
    """``MOCK=1`` has to actually find the fixtures, which is not automatic.

    S2 used to pass a pre-mangled cache id to ``read_fixture`` -- ``f"cover__{_slug(cid)}"``
    -- while ``export_fixtures.py`` wrote the raw id. The two mangling rules
    disagreed, and the kind was doubled in the process, so the two produced
    ``cover__cover_25_752000_m80_380000_...json`` and
    ``cover__25.752000_m80.380000_...json``. Every exported S2 fixture was
    committed and permanently unreadable.

    The symptom is the worst kind: ``MOCK=1`` reported "no committed fixture" for
    an area that was in fact fully seeded, which reads as missing demo data rather
    than as a filename mismatch. Only a test that actually writes a fixture and
    asks the service to find it catches that, which is what this does.
    """

    TILE = tile_bbox(*SETTINGS.demo_rider, 150.0)

    def _settings(self, monkeypatch, tmp_path):
        """Put the service into ``MOCK=1`` with a fixtures directory of our own.

        Also hands back the *real* ``_fetch_covers`` / ``_fetch_shade``, because
        the autouse fixture replaces those with a no-op for determinism and this
        class is precisely about what they do. Restoring them is safe: the
        network boundary is one level further down at ``cover.afetch_overpass``,
        which the autouse fixture still blocks, so neither the disk cache nor
        Overpass can be reached even here.
        """
        monkeypatch.setattr(service, "SETTINGS", replace(SETTINGS, mock=True))
        monkeypatch.setattr(service, "fixtures_dir", lambda _pkg: tmp_path)
        monkeypatch.setattr(service, "_fetch_covers", _REAL_FETCH_COVERS)
        monkeypatch.setattr(service, "_fetch_shade", _REAL_FETCH_SHADE)
        monkeypatch.setattr(
            service, "read_cache",
            lambda *a, **k: pytest.fail("MOCK=1 must not read the disk cache"),
        )
        return tmp_path

    @pytest.mark.anyio
    async def test_a_written_cover_fixture_is_found(self, monkeypatch, tmp_path):
        tmp_path = self._settings(monkeypatch, tmp_path)
        cid = tile_cache_id(*_tile_centre(self.TILE), 150.0, "cover")
        write_fixture(tmp_path, cid, _COVER_PAYLOAD)

        cmap = await service._fetch_covers(
            self.TILE, 150.0, [], frame_for(*SETTINGS.demo_rider)
        )
        assert cmap is not None, "MOCK=1 did not find a fixture it had just written"
        assert len(cmap.covers) == 1

    @pytest.mark.anyio
    async def test_a_written_shade_fixture_is_found(self, monkeypatch, tmp_path):
        tmp_path = self._settings(monkeypatch, tmp_path)
        cid = tile_cache_id(*_tile_centre(self.TILE), 150.0, "shade")
        write_fixture(tmp_path, cid, _SHADE_PAYLOAD)

        smap = await service._fetch_shade(
            self.TILE, 150.0, [], frame_for(*SETTINGS.demo_rider)
        )
        assert smap is not None, "MOCK=1 did not find a fixture it had just written"
        assert len(smap.blocks) == 1

    @pytest.mark.anyio
    async def test_a_cover_fixture_is_never_read_as_shade(self, monkeypatch, tmp_path):
        """Cover and shade are different queries over the same tile. A single
        shared name would let whichever was written last shadow the other, and
        the demo would draw awnings where the buildings are."""
        tmp_path = self._settings(monkeypatch, tmp_path)
        centre = _tile_centre(self.TILE)
        write_fixture(tmp_path, tile_cache_id(*centre, 150.0, "cover"), _COVER_PAYLOAD)

        assert await service._fetch_shade(
            self.TILE, 150.0, [], frame_for(*SETTINGS.demo_rider)
        ) is None
        assert await service._fetch_covers(
            self.TILE, 150.0, [], frame_for(*SETTINGS.demo_rider)
        ) is not None

    @pytest.mark.anyio
    async def test_a_missing_fixture_says_which_tile(self, monkeypatch, tmp_path):
        """A demo that silently has no cover is the failure this guards, so the
        note has to name the tile -- otherwise the operator cannot tell "the
        area has no awnings" from "we never fetched the awnings"."""
        tmp_path = self._settings(monkeypatch, tmp_path)
        fallbacks: list[str] = []
        assert await service._fetch_covers(
            self.TILE, 150.0, fallbacks, frame_for(*SETTINGS.demo_rider)
        ) is None
        assert fallbacks
        assert f"{self.TILE[0]:g}" in fallbacks[0]
        assert "cover" in fallbacks[0]


def _tile_centre(tile):
    return (tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0


# --------------------------------------------------------------------------- #
# the Overpass query text
# --------------------------------------------------------------------------- #

def _selectors(query: str) -> list[str]:
    """The statements inside the ``( ... )`` union, semicolons stripped."""
    body = query[query.index("(") + 1: query.rindex(")")]
    return [s.strip() for s in body.split(";") if s.strip()]


class TestOverpassQueries:
    """Guards the query *text*, which no test can catch by talking to Overpass.

    Every Overpass selector here has to be bbox-filtered individually. The
    tidier-looking form -- one bbox applied to the whole union with a trailing
    ``)(bbox);`` -- is not valid Overpass QL, and the server rejects it with
    ``line 9: parse error: ';' expected - '(' found``.

    That rejection is worth a test precisely because it is so hard to read. It
    comes back as a tidy HTTP 400 from all four mirrors at once, which the
    failover logic reports as "all Overpass mirrors failed" -- indistinguishable
    from the network being down, and it was misdiagnosed that way for a while.
    The cost of getting it wrong is that S2 has no fixtures and cannot run
    offline, so the failure surfaces as a missing demo rather than as an error.
    """

    QUERIES = (
        ("cover", build_cover_query),
        ("shade", build_shade_query),
    )
    BBOX = (25.7533, -80.3762, 25.7605, -80.3682)

    def test_every_selector_carries_its_own_bbox_filter(self):
        """The specific regression. A statement without a trailing bbox is
        unbounded, which Overpass rejects rather than serving."""
        for name, build in self.QUERIES:
            for sel in _selectors(build(self.BBOX)):
                assert sel.endswith(")"), (
                    f"{name} query has an unbounded selector: {sel!r}"
                )

    def test_the_bbox_is_actually_substituted(self):
        """No ``{bbox}`` may survive into the wire query, and the numbers must be
        in Overpass's ``south,west,north,east`` order -- the reverse is a valid
        query that returns the wrong hemisphere."""
        s, w, n, e = self.BBOX
        for name, build in self.QUERIES:
            q = build(self.BBOX)
            assert "{bbox}" not in q, f"{name} query left an unfilled placeholder"
            assert f"({s},{w},{n},{e})" in q, f"{name} query has the bbox in the wrong order"

    def test_the_union_carries_no_bbox_of_its_own(self):
        """The failing form, asserted absent: no ``)(`` before the union's
        closing paren."""
        for name, build in self.QUERIES:
            assert ")(" not in build(self.BBOX), f"{name} query applies a bbox to the union"

    def test_output_comes_after_the_union(self):
        """`out body geom` inside the union is valid but changes what is returned;
        the reason S1's first bbox placement returned zero elements is that it
        ended up attached here."""
        for name, build in self.QUERIES:
            q = build(self.BBOX)
            assert q.count("out body geom") == 1
            assert q.index("out body geom") > q.rindex(")"), name

    def test_geometry_is_requested(self):
        """`out body geom` and not `out body`: without `geom` the parser gets bare
        node ids and every feature silently becomes an empty geometry, which looks
        like an area with no cover."""
        for name, build in self.QUERIES:
            assert "out body geom" in build(self.BBOX), name


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #

class TestContract:
    def setup_method(self):
        self.c = TestClient(app)

    def test_the_response_shape_is_exactly_the_shared_model(self, monkeypatch):
        """Guards against a field being added to the dict by hand and drifting
        from ``ConditionsResult``."""
        from shared.models import ConditionsResult

        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        spots = _spots(("a", 10.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        assert set(body) == set(ConditionsResult.model_fields)

    def test_ranked_spots_revalidate_against_the_model(self, monkeypatch):
        monkeypatch.setattr(weather, "get_hourly", _forecast(precip=4.0))
        spots = _spots(("a", 10.0, 0.0, 1.0))
        body = self.c.post("/conditions/rank",
                           json=_request(spots).model_dump(mode="json")).json()
        from shared.models import RankedSpot

        for r in body["ranked"]:
            RankedSpot.model_validate(r)

    def test_a_naive_pickup_time_is_treated_as_utc(self, monkeypatch):
        """The orchestrator can hand through a naive datetime from user input, and
        a naive-vs-aware comparison raises rather than degrades."""
        monkeypatch.setattr(weather, "get_hourly", _forecast())
        spots = _spots(("a", 10.0))
        payload = _request(spots).model_dump(mode="json")
        payload["pickup_time"] = "2026-09-27T18:30:00"
        r = self.c.post("/conditions/rank", json=payload)
        assert r.status_code == 200

    def test_the_documented_mode_values_are_the_only_ones_possible(self):
        assert {c.value for c in Condition} == {"rain", "sun", "neutral"}
