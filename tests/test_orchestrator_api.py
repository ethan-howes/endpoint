"""End-to-end tests for the ride flow (ENDPOINT.md §7).

Every outbound service call is stubbed at ``clients.call_service`` -- the single
seam all three services funnel through. That is not just convenience: it means
these tests exercise the real timeout/fallback/validation path in
``clients.call_service`` rather than replacing it, so "the ride survived S1 being
down" is a claim about the shipped code.

Routing is stubbed too. The real adapter talks to a shared public OSRM server,
which is not something a test suite should depend on -- and §4.5's guarantee has
to hold when routing is *also* unavailable, which is a real demo condition.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from orchestrator import flow, main, routing
from orchestrator.clients import ServiceResult
from orchestrator.ride import RIDES
from shared.config import SETTINGS
from shared.models import (
    Condition,
    ConditionsResult,
    Confidence,
    CoverFeature,
    LatLng,
    LegalSpotsResponse,
    Overlays,
    RankRequest,
    RankedSpot,
    Side,
    Spot,
    WeatherReport,
)
from shared.models import utcnow

RIDER = LatLng(lat=SETTINGS.demo_rider[0], lng=SETTINGS.demo_rider[1])


def _parse_iso(s: str):
    from datetime import datetime

    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _spot(spot_id: str, walk_m: float, *, lat: float, lng: float,
          conf=Confidence.LIKELY, name: str = "SW 109th Ave") -> Spot:
    return Spot(
        spot_id=spot_id,
        stop_point=LatLng(lat=lat, lng=lng),
        side=Side.RIGHT,
        curb_bearing_deg=90.0,
        walk_distance_m=walk_m,
        street_name=name,
        confidence=conf,
        segment_id="way/299562027",
    )


def _weather(condition=Condition.RAIN) -> WeatherReport:
    return WeatherReport(condition=condition, valid_at=utcnow(), source="open-meteo")


def _ranked(spot: Spot, score: float, reason: str = "", cover=None) -> RankedSpot:
    return RankedSpot(
        spot=spot,
        wait_point=spot.stop_point,
        cover_feature=cover,
        gap_m=spot.walk_distance_m,
        score=score,
        reason=reason,
        confidence=spot.confidence,
    )


class Stub:
    """Records every call and returns canned responses, with per-service failure."""

    def __init__(self) -> None:
        self.spots = [
            _spot("s1_0001", 20.0, lat=25.75700, lng=-80.37210),
            _spot("s1_0002", 45.0, lat=25.75720, lng=-80.37180),
            _spot("s1_0003", 90.0, lat=25.75760, lng=-80.37120),
        ]
        self.s1_ok = True
        self.s2_ok = True
        self.s3_ok = False          # S3 is out of scope in this build
        #: Raw assessment dicts S3 returns when ``s3_ok`` is set.
        self.assessments: list[dict] = []
        self.conditions: ConditionsResult | None = None
        self.assessment_delay = 0.0
        self.s1_delay = 0.0
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict | None]] = []

    def called(self, service: str) -> bool:
        return any(c[0] == service for c in self.calls)

    def last_body(self, service: str) -> dict | None:
        """The most recent request body sent to ``service``.

        Lets a test assert what the orchestrator *forwarded*, which is the part it
        is responsible for -- as distinct from what S2 then echoed back in its
        response, which is S2's business.
        """
        for svc, body in reversed(self.bodies):
            if svc == service:
                return body
        return None

    async def call(self, service, method, path, *, json_body=None, params=None,
                   model=None, client=None):
        self.calls.append((service, path))
        self.bodies.append((service, json_body))

        if service == "S1":
            if self.s1_delay:
                await asyncio.sleep(self.s1_delay)
            if not self.s1_ok:
                return ServiceResult(ok=False, reason="S1 timed out after 3s")
            resp = LegalSpotsResponse(
                spots=list(self.spots), count=len(self.spots), source="osm", cached=True
            )
        elif service == "S2":
            if not self.s2_ok:
                return ServiceResult(ok=False, reason="S2 unreachable (ConnectError)")
            # A real S2 returns best-first, and the orchestrator deliberately
            # trusts that ordering rather than re-sorting: S2 owns the ranking,
            # and re-sorting here would need a second, subtly different copy of
            # its scoring rules.
            resp = self.conditions or ConditionsResult(
                weather=_weather(
                    (json_body or {}).get("force_condition") or Condition.RAIN
                ),
                mode=Condition.RAIN,
                ranked=[_ranked(self.spots[1], 0.9, "awning over the kerb"),
                        _ranked(self.spots[0], 0.4, "partial cover")],
                nearest_spot_id=self.spots[0].spot_id,
                overlays=Overlays(cover_features=[
                    CoverFeature(
                        feature_id="osm_way_1", kind="awning",
                        geometry_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
                        provides=["rain"], source="osm",
                    )
                ]),
            )
        elif service == "S3":
            if self.assessment_delay:
                await asyncio.sleep(self.assessment_delay)
            if not self.s3_ok:
                return ServiceResult(ok=False, reason="S3 unreachable (ConnectError)")
            resp = {"assessments": list(self.assessments)}
        else:
            return ServiceResult(ok=False, reason=f"unexpected service {service}")

        if model is not None:
            resp = model.model_validate(resp.model_dump(mode="json")
                                        if hasattr(resp, "model_dump") else resp)
        return ServiceResult(ok=True, data=resp)


@pytest.fixture
def stub(monkeypatch) -> Stub:
    s = Stub()
    # flow and main each hold their own reference, so both need patching.
    monkeypatch.setattr(flow, "call_service", s.call)
    monkeypatch.setattr(main, "call_service", s.call)
    monkeypatch.setattr(routing, "route_osrm", _fake_route)
    routing.clear_route_cache()
    RIDES.clear()
    yield s
    RIDES.clear()
    routing.clear_route_cache()


async def _fake_route(origin, dest, client=None):
    """A long, slow route so the simulator does not immediately trip the approach
    threshold and confirm the ride out from under the test.

    Carries a real encoded polyline and a vertex per 100 m, not a bare two-point
    line: the UI draws this, and a stub that returns no polyline would let a bug
    where the route never reaches the response pass unnoticed.
    """
    import polyline as _poly

    frame = routing.LocalFrame(dest.lat, dest.lng)
    o = frame.to_m(origin.lat, origin.lng)
    d = frame.to_m(dest.lat, dest.lng)
    steps = 15
    pts = [(o[0] + (d[0] - o[0]) * i / steps, o[1] + (d[1] - o[1]) * i / steps)
           for i in range(steps + 1)]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + ((pts[i][0] - pts[i - 1][0]) ** 2
                              + (pts[i][1] - pts[i - 1][1]) ** 2) ** 0.5)
    dist = cum[-1]
    return routing.Route(
        polyline=_poly.encode([(frame.to_ll(x, y)) for x, y in pts], precision=5),
        distance_m=dist,
        eta_s=300,
        points=pts,
        cum=cum,
    )


@pytest.fixture
def client(stub) -> TestClient:
    with TestClient(main.app) as c:
        yield c


def _request(client, **kw) -> str:
    r = client.post("/rides/request",
                    json={"rider_location": RIDER.model_dump(mode="json"), **kw})
    assert r.status_code == 200, r.text
    return r.json()["ride_id"]


# --------------------------------------------------------------------------- #
# health / readiness
# --------------------------------------------------------------------------- #

class TestHealth:
    def test_health_is_ok(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_ready_reports_each_dependency_separately(self, client):
        """One boolean for four dependencies hides which one is down, and finding
        that out during a rehearsal is the worst possible moment."""
        r = client.get("/ready")
        assert r.status_code == 200
        checks = r.json()["checks"]
        assert checks["S1_legal_spots"] == "ok"
        assert "S2_weather_cover" in checks
        # S3 absent is expected, and must not be presented as a failure.
        assert "out of scope" in checks["S3_vision"]

    def test_ready_reports_no_data_when_s1_is_down(self, client, stub):
        stub.s1_ok = False
        assert client.get("/ready").json()["status"] == "no_data"


# --------------------------------------------------------------------------- #
# /rides/request
# --------------------------------------------------------------------------- #

class TestRequest:
    def test_returns_a_ride_id_and_the_mobility_question(self, client, stub):
        r = client.post("/rides/request",
                        json={"rider_location": RIDER.model_dump(mode="json")})
        body = r.json()
        assert r.status_code == 200
        assert body["ride_id"].startswith("r_")
        assert "assistance" in body["question"].lower()

    def test_returns_the_question_without_waiting_for_s1(self, client, stub):
        """§3 orders it request -> S1 -> question, but the question text does not
        depend on S1's answer at all. S1 is started in the background and awaited
        in /answer, so the rider is not held for a call capped at 3 s."""
        stub.s1_delay = 1.5
        r = client.post("/rides/request",
                        json={"rider_location": RIDER.model_dump(mode="json")})
        assert r.status_code == 200
        assert r.json()["question"]

    def test_does_not_claim_to_have_a_prediction_yet(self, client, stub):
        """There is no spot, no ETA and no message at this point. Returning a
        half-built plan would imply a prediction that does not exist."""
        body = client.post("/rides/request",
                           json={"rider_location": RIDER.model_dump(mode="json")}).json()
        assert set(body) == {"ride_id", "spot_count", "question"}
        assert body["spot_count"] == 0

    def test_rejects_a_typo_in_a_field_name(self, client, stub):
        r = client.post("/rides/request",
                        json={"rider_lat": 25.75, "rider_lng": -80.37})
        assert r.status_code == 422

    def test_accepts_an_explicit_car_start(self, client, stub):
        start = LatLng(lat=25.7600, lng=-80.3800)
        rid = _request(client, car_start=start.model_dump(mode="json"))
        assert client.get(f"/rides/{rid}").status_code == 200

    def test_rides_get_distinct_ids(self, client, stub):
        a, b = _request(client), _request(client)
        assert a != b


# --------------------------------------------------------------------------- #
# /rides/{id}/answer -- the predictive phase
# --------------------------------------------------------------------------- #

class TestAnswerWithoutMobilityNeeds:
    def test_picks_the_nearest_spot_and_confirms_immediately(self, client, stub):
        rid = _request(client)
        r = client.post(f"/rides/{rid}/answer", json={"mobility_needs": False})
        plan = r.json()
        assert r.status_code == 200
        assert plan["phase"] == "confirmed"
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0001"
        assert plan["final_spot"]["spot"]["spot_id"] == "s1_0001"

    def test_never_calls_s2(self, client, stub):
        """Not a saving -- a correctness point. §3 step 4: without mobility needs
        this is just a pickup, so telling the rider about weather they did not
        ask about is noise, and an S2 outage must not be able to affect it."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": False})
        assert not stub.called("S2")

    def test_message_is_a_plain_pickup_instruction(self, client, stub):
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        msg = plan["rider_message"]
        assert "pick you up" in msg
        assert "rain" not in msg.lower() and "shade" not in msg.lower()

    def test_reports_a_real_eta(self, client, stub):
        """REGRESSION. The no-mobility-needs path returns before `_dispatch`, so
        the ETA assigned there never ran and every such ride reported eta_s 0 --
        which reads to the rider as "your car is here now" and to the UI as a
        finished trip."""
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        assert plan["eta_s"] > 0
        status = client.get(f"/rides/{rid}").json()
        assert status["remaining_eta_s"] > 0
        assert status["remaining_m"] > 0

    def test_carries_a_route_polyline(self, client, stub):
        """Without one the UI has no line to draw, so the demo shows a car
        materialising at the pickup with no approach at all."""
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        assert plan["route_polyline"], "no route polyline for the UI to draw"

    def test_a_fully_degraded_s1_still_produces_a_ride(self, client, stub):
        """§4.5's headline case: the service is down, the ride still happens."""
        stub.s1_ok = False
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0001"
        assert plan["fallbacks_used"], "a degraded ride must say why"
        assert any("timed out" in f for f in plan["fallbacks_used"])


class TestAnswerWithMobilityNeeds:
    def test_ranks_by_conditions_and_stays_predicted(self, client, stub):
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True}).json()
        assert plan["phase"] == "predicted"
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0002"  # the covered one
        assert plan["final_spot"] is None

    def test_calls_s2_exactly_once_with_the_spots_and_a_pickup_time(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        s2 = [c for c in stub.calls if c[0] == "S2"]
        assert len(s2) == 1

    def test_forwards_a_pickup_time_an_eta_into_the_future(self, client, stub):
        """S2 must rank for the moment the rider will actually be on the kerb, not
        the moment they tapped the button. A car four minutes away in a rainstorm
        is a different decision than one arriving in dry weather, so using 'now'
        would quietly get that wrong every time."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        body = stub.last_body("S2")
        assert body is not None
        eta = client.get(f"/rides/{rid}").json()["plan"]["eta_s"]
        assert eta > 0, "no ETA means no pickup time could have been computed"

        sent_at = _parse_iso(body["pickup_time"])
        assert sent_at is not None
        assert (sent_at - utcnow()).total_seconds() > 0, \
            "pickup time is in the past, so S2 ranked for the wrong moment"

    def test_says_why_it_chose_that_spot(self, client, stub):
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True}).json()
        assert "rain" in plan["rider_message"].lower()
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0002"

    def test_survives_s2_being_down(self, client, stub):
        """The degradation that matters most: weather is the reason this rider
        needed help, so losing S2 must still get them a ride."""
        stub.s2_ok = False
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True}).json()
        assert plan["phase"] == "predicted"
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0001"
        assert any("S2" in f for f in plan["fallbacks_used"])
        assert plan["rider_message"]

    def test_falls_back_to_nearest_when_s2_returns_nothing_ranked(self, client, stub):
        stub.conditions = ConditionsResult(weather=_weather(), mode=Condition.RAIN)
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True}).json()
        assert plan["predicted_spot"]["spot"]["spot_id"] == "s1_0001"
        assert any("S2 returned no ranked" in f for f in plan["fallbacks_used"])

    def test_falls_back_to_the_riders_own_location_when_s1_found_nothing(self, client, stub):
        """Nothing legal within the radius. §6 S1's degraded answer is a single
        unverified spot at the rider's own location, and it is the right one."""
        stub.spots = []
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        s = plan["predicted_spot"]["spot"]
        assert s["source"] == "fallback"
        assert s["stop_point"]["lat"] == pytest.approx(RIDER.lat, abs=1e-6)
        assert "couldn't reach" in plan["rider_message"].lower()


# --------------------------------------------------------------------------- #
# demo overrides (ENDPOINT.md section 6 -- never cut these)
# --------------------------------------------------------------------------- #

class TestDemoOverrides:
    def test_forwards_a_forced_condition_to_s2(self, client, stub):
        """The orchestrator's job is to pass the override through untouched. The
        echo back on the response is S2's job, and the override has to be visible
        in the data so a recorded demo run can be told apart from a real
        forecast."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer",
                    json={"mobility_needs": True, "force_condition": "sun"})
        body = stub.last_body("S2")
        assert body is not None
        assert body["force_condition"] == "sun"

    def test_the_forced_condition_reaches_the_rider(self, client, stub):
        """End to end, because an override that stops at the orchestrator would
        produce a rain demo on a sunny day and look like a weather bug."""
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True,
                                 "force_condition": "sun"}).json()
        assert plan["weather"]["condition"] == "sun"
        assert "sun" in plan["rider_message"].lower()

    def test_forwards_a_forced_time_to_s2(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer",
                    json={"mobility_needs": True,
                          "force_time": "2026-09-27T18:30:00Z"})
        body = stub.last_body("S2")
        assert body is not None
        assert body["force_time"].startswith("2026-09-27T18:30")

    def test_sends_the_default_ten_minute_wait(self, client, stub):
        """§7 uses 10 minutes: long enough to be worth standing under cover for,
        short enough that the forecast is still relevant."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        assert stub.last_body("S2")["wait_minutes"] == 10

    def test_a_forced_time_must_be_iso8601(self, client, stub):
        rid = _request(client)
        r = client.post(f"/rides/{rid}/answer",
                        json={"mobility_needs": True, "force_time": "tomorrow"})
        assert r.status_code == 422

    def test_a_valid_forced_time_is_accepted(self, client, stub):
        rid = _request(client)
        r = client.post(f"/rides/{rid}/answer",
                        json={"mobility_needs": True,
                              "force_time": "2026-09-27T18:30:00Z"})
        assert r.status_code == 200

    def test_an_unknown_condition_is_rejected(self, client, stub):
        rid = _request(client)
        r = client.post(f"/rides/{rid}/answer",
                        json={"mobility_needs": True, "force_condition": "snow"})
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# GET /rides/{id}
# --------------------------------------------------------------------------- #

class TestRideStatus:
    def test_polls_with_car_state_and_remaining_distance(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        r = client.get(f"/rides/{rid}").json()
        assert r["phase"] in {"predicted", "approaching", "confirmed"}
        assert "car_position" in r
        assert r["remaining_m"] >= 0
        assert r["remaining_eta_s"] >= 0

    def test_car_moves_toward_the_pickup(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        first = client.get(f"/rides/{rid}").json()
        client.post(f"/rides/{rid}/skip_to_arrival")
        later = client.get(f"/rides/{rid}").json()
        assert later["remaining_m"] < first["remaining_m"] or later["phase"] == "confirmed"

    def test_unknown_ride_says_why_it_is_missing(self, client, stub):
        """Ride state is in memory, so the overwhelmingly likely cause of a 404 is
        a restart. Saying so beats a bare 404 during a demo."""
        r = client.get("/rides/r_999999")
        assert r.status_code == 404
        assert "in memory" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# /rides/{id}/skip_to_arrival -- the real-time phase
# --------------------------------------------------------------------------- #

class TestSkipToArrival:
    def test_confirms_the_prediction_when_s3_is_absent(self, client, stub):
        """S3 is out of scope, so this is the expected path: the S2 pick stands and
        the ride completes rather than failing."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        r = client.post(f"/rides/{rid}/skip_to_arrival")
        status = r.json()
        assert r.status_code == 200
        assert status["phase"] == "confirmed"
        assert status["plan"]["final_spot"]["spot"]["spot_id"] == "s1_0002"
        assert any("S3" in f for f in status["plan"]["fallbacks_used"])

    def test_the_response_already_reflects_the_confirmed_spot(self, client, stub):
        """Runs the real-time phase inline rather than waiting for the next
        simulator tick, so this is usable as a single-call demo trigger."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        status = client.post(f"/rides/{rid}/skip_to_arrival").json()
        assert status["plan"]["final_spot"] is not None
        assert status["plan"]["phase"] == "confirmed"

    def test_it_is_idempotent(self, client, stub):
        """The simulator checks the approach threshold on every tick; without a
        guard the real-time phase would fire repeatedly for as long as the car sat
        within 150 m."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        a = client.post(f"/rides/{rid}/skip_to_arrival").json()
        b = client.post(f"/rides/{rid}/skip_to_arrival").json()
        assert a["plan"]["final_spot"]["spot"]["spot_id"] == \
               b["plan"]["final_spot"]["spot"]["spot_id"]

    def test_no_camera_means_no_camera_claim_in_the_message(self, client, stub):
        """REGRESSION. With S3 down, every rain/sun ride used to end with "We
        moved your pickup ... camera confirmed the predicted spot": the car had
        not moved and no camera had looked."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        msg = client.post(f"/rides/{rid}/skip_to_arrival").json()["plan"]["rider_message"]
        assert "moved" not in msg.lower()
        assert "camera" not in msg.lower()
        assert "raining" in msg.lower()

    def test_a_camera_confirmation_is_reported_as_one(self, client, stub):
        stub.s3_ok = True
        stub.assessments = [{
            "spot_id": "s1_0002", "mode": "rain", "cover_present": True,
            "vision_score": 0.9, "model_confidence": 0.9, "reason": "awning visible",
        }]
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        status = client.post(f"/rides/{rid}/skip_to_arrival").json()
        assert status["plan"]["final_spot"]["spot"]["spot_id"] == "s1_0002"
        msg = status["plan"]["rider_message"]
        assert "camera confirmed" in msg.lower()
        assert "moved" not in msg.lower()

    def test_a_vision_switch_routes_the_car_to_the_new_spot(self, client, stub):
        """REGRESSION. `_dispatch` always routed to `predicted_spot`, so a vision
        switch changed `final_spot` while the car kept driving to the old spot,
        and the reroute kept the old route's odometer."""
        import polyline as _poly

        stub.s3_ok = True
        stub.assessments = [
            {"spot_id": "s1_0002", "mode": "rain", "vision_score": 0.05,
             "model_confidence": 0.9, "reason": "no awning in view"},
            {"spot_id": "s1_0001", "mode": "rain", "vision_score": 0.95,
             "model_confidence": 0.9, "reason": "covered entrance beside the kerb"},
        ]
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        status = client.post(f"/rides/{rid}/skip_to_arrival").json()

        assert status["plan"]["final_spot"]["spot"]["spot_id"] == "s1_0001"
        assert status["plan"]["rider_message"].startswith("We moved your pickup")

        ride = RIDES.get(rid)
        end_lat, end_lng = _poly.decode(ride.route.polyline)[-1]
        target = stub.spots[0].stop_point
        assert abs(end_lat - target.lat) < 1e-4 and abs(end_lng - target.lng) < 1e-4
        # The new route starts where the car is, so the whole of it is still ahead.
        assert status["remaining_m"] == pytest.approx(ride.route.distance_m, abs=0.5)

    def test_neutral_weather_skips_the_look_entirely(self, client, stub):
        """§3 step 7: there is nothing to protect against, so looking is wasted
        time and image budget."""
        stub.conditions = ConditionsResult(
            weather=_weather(Condition.NEUTRAL), mode=Condition.NEUTRAL,
            ranked=[_ranked(stub.spots[0], 0.9, "closest")],
        )
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        client.post(f"/rides/{rid}/skip_to_arrival")
        assert not stub.called("S3"), "S3 should not be called in neutral mode"


# --------------------------------------------------------------------------- #
# /rides/{id}/confirm
# --------------------------------------------------------------------------- #

class TestConfirm:
    def test_decline_the_detour_keeps_the_covered_prediction(self, client, stub):
        """§7 line 519: the question is "shall we keep you here, closer to walk",
        not "shall we move you to the cover". S2's best pick is what the car was
        already dispatched to, so accepting the detour is simply leaving it
        alone."""
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": True}).json()
        predicted = plan["predicted_spot"]["spot"]["spot_id"]
        after = client.post(f"/rides/{rid}/confirm",
                            json={"accept_detour": True}).json()
        # Still the predictive phase: the car is on its way and vision has not
        # looked yet (§3 steps 7-9 run on approach).
        assert after["phase"] == "predicted"
        assert after["predicted_spot"]["spot"]["spot_id"] == predicted
        final = client.post(f"/rides/{rid}/skip_to_arrival").json()
        assert final["phase"] == "confirmed"
        assert final["plan"]["final_spot"]["spot"]["spot_id"] == predicted

    def test_decline_moves_to_the_nearest_legal_spot(self, client, stub):
        """The whole point of the question: a rider who would rather not walk the
        extra distance gets the spot they are already standing next to."""
        import polyline as _poly

        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        after = client.post(f"/rides/{rid}/confirm",
                            json={"accept_detour": False}).json()
        assert after["phase"] == "predicted"
        assert after["predicted_spot"]["spot"]["spot_id"] == "s1_0001"  # nearest
        # And the car is actually routed there, not to the covered spot.
        end_lat, end_lng = _poly.decode(RIDES.get(rid).route.polyline)[-1]
        target = stub.spots[0].stop_point
        assert abs(end_lat - target.lat) < 1e-4 and abs(end_lng - target.lng) < 1e-4
        final = client.post(f"/rides/{rid}/skip_to_arrival").json()
        assert final["plan"]["final_spot"]["spot"]["spot_id"] == "s1_0001"

    def test_answering_keeps_the_car_moving(self, client, stub):
        """REGRESSION. The route handler cancelled the simulator and never
        restarted it, so the car froze wherever it was when the rider answered and
        the real-time phase never ran."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        client.post(f"/rides/{rid}/confirm", json={"accept_detour": False})
        ride = RIDES.get(rid)
        assert ride.sim_task is not None and not ride.sim_task.done()
        assert ride.approach_done is False

    def test_a_decline_after_arrival_still_moves_the_car(self, client, stub):
        """The question can still be open when the car reaches the approach
        threshold. The rider's answer wins, and the car is driven to it."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        client.post(f"/rides/{rid}/skip_to_arrival")
        after = client.post(f"/rides/{rid}/confirm",
                            json={"accept_detour": False}).json()
        assert after["phase"] == "confirmed"
        assert after["final_spot"]["spot"]["spot_id"] == "s1_0001"
        ride = RIDES.get(rid)
        assert ride.sim_task is not None and not ride.sim_task.done()

    def test_the_question_is_offered_only_when_s2_asks_for_it(self, client, stub):
        """§7: the route is "only used when S2 sets needs_rider_confirmation".
        A prompt the rider cannot act on is worse than no prompt."""
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        assert client.get(f"/rides/{rid}").json()["confirmation_question"] is None

    def test_a_pending_question_reaches_the_polling_endpoint(self, client, stub):
        """The UI only polls GET /rides/{id}, so a question that lives anywhere
        else is a question the rider never sees."""
        stub.conditions = ConditionsResult(
            weather=_weather(), mode=Condition.RAIN,
            ranked=[_ranked(stub.spots[2], 0.9, "deep awning")],
            needs_rider_confirmation=True,
        )
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        q = client.get(f"/rides/{rid}").json()["confirmation_question"]
        assert q and "farther" in q.lower()

    def test_the_question_disappears_once_answered(self, client, stub):
        stub.conditions = ConditionsResult(
            weather=_weather(), mode=Condition.RAIN,
            ranked=[_ranked(stub.spots[2], 0.9, "deep awning")],
            needs_rider_confirmation=True,
        )
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        client.post(f"/rides/{rid}/confirm", json={"accept_detour": False})
        assert client.get(f"/rides/{rid}").json()["confirmation_question"] is None

    def test_requires_the_field_rather_than_defaulting_it(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": True})
        assert client.post(f"/rides/{rid}/confirm", json={}).status_code == 422


# --------------------------------------------------------------------------- #
# degraded rides stay coherent
# --------------------------------------------------------------------------- #

class TestDegradedRidesStayUsable:
    def test_fallbacks_are_surfaced_as_a_separate_note_not_in_the_message(
        self, client, stub
    ):
        """A diagnostic, not something to read aloud at a rider. §4.6's whole
        point is being honest about data gaps, but the rider-facing line stays
        short and calm."""
        stub.s1_ok = False
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": False})
        status = client.get(f"/rides/{rid}").json()
        assert status["degraded_note"] is not None
        assert "unavailable" in status["degraded_note"]

    def test_a_clean_ride_has_no_degraded_note(self, client, stub):
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": False})
        assert client.get(f"/rides/{rid}").json()["degraded_note"] is None

    def test_the_plan_shape_is_identical_degraded_or_not(self, client, stub):
        """A client must not need a second code path for the unhappy path."""
        stub.s1_ok = False
        rid = _request(client)
        client.post(f"/rides/{rid}/answer", json={"mobility_needs": False})
        keys = set(client.get(f"/rides/{rid}").json()["plan"])
        assert keys == {"ride_id", "phase", "mobility_needs", "weather", "candidates",
                        "predicted_spot", "final_spot", "route_polyline", "eta_s",
                        "rider_message", "fallbacks_used"}

    def test_routing_failure_does_not_break_the_ride(self, client, stub, monkeypatch):
        """Routing is an external service like the others, and it is the one most
        likely to be down on a shared public host at demo time."""
        async def boom(origin, dest, client=None):
            raise httpx.ConnectError("osrm down")

        import httpx
        monkeypatch.setattr(routing, "route_osrm", boom)
        routing.clear_route_cache()
        rid = _request(client)
        plan = client.post(f"/rides/{rid}/answer",
                           json={"mobility_needs": False}).json()
        assert plan["phase"] == "confirmed"
        assert plan["predicted_spot"] is not None


# --------------------------------------------------------------------------- #
# simulator
# --------------------------------------------------------------------------- #

class TestSimulator:
    """Driven directly with a fast tick, so these take milliseconds."""

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        import dataclasses

        from orchestrator import simulator

        # ~100 m per 50 ms tick, so a 1.5 km demo route drives in under a second.
        monkeypatch.setattr(simulator, "SETTINGS", dataclasses.replace(
            SETTINGS, sim_tick_s=0.05, sim_speedup=400.0,
            approach_distance_m=0.0, approach_eta_s=0.0,
        ))

    @staticmethod
    def _ride():
        from orchestrator.ride import Ride

        return Ride(ride_id="r_test", rider_location=RIDER,
                    car_start=LatLng(lat=25.7625, lng=-80.3850),
                    car_position=LatLng(lat=25.7625, lng=-80.3850))

    def test_follows_a_route_swapped_in_mid_run(self):
        """REGRESSION. Speed was derived once from the first route, and progress
        was never reset, so a reroute drove the new route at the old pace from
        the old odometer reading."""
        from orchestrator import simulator

        ride = self._ride()
        near = _spot("s1_0001", 20.0, lat=25.75700, lng=-80.37210)
        far = LatLng(lat=25.7625, lng=-80.3850)

        async def go():
            ride.route = await _fake_route(far, near.stop_point)
            ride.predicted_spot = _ranked(near, 0.9)
            task = asyncio.create_task(simulator.run(ride, _noop))
            await asyncio.sleep(0.12)
            assert ride.car_travelled_m > 0
            # Reroute the way flow._dispatch does.
            ride.route = await _fake_route(ride.car_position, near.stop_point)
            ride.car_travelled_m = 0.0
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(go())
        assert ride.car_travelled_m == pytest.approx(ride.route.distance_m)
        assert ride.car_position == near.stop_point

    def test_an_unusable_route_still_runs_the_approach(self):
        from orchestrator import simulator
        from orchestrator.ride import Route

        ride = self._ride()
        ride.route = Route()  # no route at all
        calls = []

        async def on_approach():
            calls.append(1)

        asyncio.run(simulator.run(ride, on_approach))
        assert calls == [1]


async def _noop():
    return None
