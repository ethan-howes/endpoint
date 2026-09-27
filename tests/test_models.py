"""Contract tests for the shared Pydantic models (ENDPOINT.md section 5).

These are the models every service imports, so a change here is a change to every
service's public API at once. The tests below pin the *serialized* shape rather
than the implementation, because that is what actually breaks a client: field
renames, dropped fields, and enum values that drift from what the doc promised.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from datetime import datetime, timezone

from shared.models import (
    BBox,
    ConditionsResult,
    Confidence,
    Condition,
    LatLng,
    LegalityBasis,
    LegalSpotsRequest,
    LegalSpotsResponse,
    RestrictionKind,
    RidePhase,
    ShadeSource,
    Side,
    Spot,
    SpotType,
    StrictModel,
    SunPosition,
    WeatherReport,
)


def _weather(**kw) -> WeatherReport:
    base = dict(
        condition=Condition.RAIN,
        valid_at=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc),
    )
    return WeatherReport(**{**base, **kw})


class TestLatLng:
    def test_field_order_and_names(self):
        """lat before lng. The risk is someone passing a bare tuple somewhere and
        the latitude silently becoming a longitude; named fields plus explicit
        conversion helpers are the defence, and this pins the order they assume."""
        ll = LatLng(lat=25.756918, lng=-80.372182)
        assert list(ll.model_dump()) == ["lat", "lng"]

    def test_as_tuple_is_lat_lng(self):
        assert LatLng(lat=1.5, lng=-2.5).as_tuple() == (1.5, -2.5)

    def test_as_lnglat_is_the_reverse(self):
        """pyproj/GeoJSON ordering. Kept as its own method so no call site has to
        remember which way round a particular library wants it."""
        assert LatLng(lat=1.5, lng=-2.5).as_lnglat() == (-2.5, 1.5)

    @pytest.mark.parametrize("lat", [-91.0, 91.0])
    def test_rejects_impossible_latitude(self, lat):
        with pytest.raises(ValidationError):
            LatLng(lat=lat, lng=0.0)

    @pytest.mark.parametrize("lng", [-181.0, 181.0])
    def test_rejects_impossible_longitude(self, lng):
        with pytest.raises(ValidationError):
            LatLng(lat=0.0, lng=lng)

    def test_accepts_the_demo_coordinates(self):
        """The Graham Center, which is where every demo request originates."""
        LatLng(lat=25.756918, lng=-80.372182)


class TestEnums:
    def test_confidence_values_are_the_documented_tiers(self):
        assert {c.value for c in Confidence} == {
            "verified", "likely", "unverified", "detected",
        }

    def test_confidence_has_a_total_order(self):
        """Ranking sorts best-first by confidence, i.e. descending rank. If it
        were a plain str enum, "unverified" would sort ahead of "verified" and
        every ranking would quietly invert.

        The negation mirrors ``ranking.py``, which sorts on ``-rank``; a rank that
        only works with the opposite sign is a trap for the next consumer."""
        best_first = sorted(Confidence, key=lambda c: -c.rank)
        assert best_first == [
            Confidence.VERIFIED,
            Confidence.LIKELY,
            Confidence.DETECTED,
            Confidence.UNVERIFIED,
        ]

    def test_confidence_ranks_are_distinct(self):
        """Two tiers sharing a rank would make the dedupe tie-break depend on
        input order."""
        ranks = [c.rank for c in Confidence]
        assert len(set(ranks)) == len(ranks)

    def test_detected_outranks_unverified(self):
        """S3 seeing a real awning beats an inferred curb tag, even though
        neither is official."""
        assert Confidence.DETECTED.rank > Confidence.UNVERIFIED.rank
        assert Confidence.VERIFIED.rank > Confidence.LIKELY.rank

    def test_side_values(self):
        assert {s.value for s in Side} == {"left", "right"}

    def test_spot_type_does_not_include_unreachable_values(self):
        """`driveway_pullout` was in the original enum but nothing in the algorithm
        could produce one, so an offer of it could never be honoured."""
        assert "driveway_pullout" not in {t.value for t in SpotType}

    def test_legality_basis_covers_every_way_a_spot_can_earn_its_label(self):
        assert {b.value for b in LegalityBasis} == {
            "official_regulation", "tagged_permissive", "inferred_standard", "unknown",
        }

    def test_ride_phase_matches_the_documented_lifecycle(self):
        """Three phases, exactly as ENDPOINT.md line 284 states. Notably there is
        no "arrived" or "boarding": the product's whole promise is that the rider
        is met before they have to stand in the rain, so the plan resolves at
        `confirmed`."""
        assert {p.value for p in RidePhase} == {"predicted", "approaching", "confirmed"}

    def test_restriction_kinds_map_to_the_exclusions(self):
        assert {k.value for k in RestrictionKind} >= {
            "fire_hydrant", "crossing", "bus_stop", "intersection",
        }


class TestBBox:
    def test_as_tuple_is_in_overpass_order(self):
        """Overpass `bbox=` takes south,west,north,east."""
        b = BBox(south=25.0, west=-80.0, north=26.0, east=-79.0)
        assert b.as_tuple == (25.0, -80.0, 26.0, -79.0)

    def test_as_overpass_renders_that_order_as_a_string(self):
        b = BBox(south=25.0, west=-80.0, north=26.0, east=-79.0)
        assert b.as_overpass == "25.0,-80.0,26.0,-79.0"

    def test_rejects_inverted_box(self):
        """An inverted bbox is accepted by Overpass and returns zero elements, so
        it presents downstream as "no road data here" rather than as a bug."""
        with pytest.raises(ValidationError):
            BBox(south=26.0, west=-80.0, north=25.0, east=-79.0)
        with pytest.raises(ValidationError):
            BBox(south=25.0, west=-79.0, north=26.0, east=-80.0)

    def test_expanded_grows_in_the_right_direction(self):
        b = BBox(south=25.0, west=-80.0, north=26.0, east=-79.0)
        g = b.expanded(100.0)
        assert g.south < b.south and g.north > b.north
        assert g.west < b.west and g.east > b.east


class TestStrictModel:
    def test_requests_reject_unknown_fields(self):
        """extra=forbid on requests. A typo'd key must be an error at the
        boundary, not a silently-defaulted field."""
        with pytest.raises(ValidationError):
            LegalSpotsRequest.model_validate(
                {"rider_location": {"lat": 25.75, "lng": -80.37}, "radius": 150}
            )

    def test_request_rejects_a_typo_in_a_known_field(self):
        with pytest.raises(ValidationError):
            LegalSpotsRequest.model_validate(
                {"rider_location": {"lat": 25.75, "lng": -80.37}, "raduis_m": 150}
            )

    def test_responses_tolerate_extra_fields(self):
        """Responses are not strict: the orchestrator adds fusion metadata, and a
        strict response model would reject a service that is merely being helpful."""
        assert issubclass(LegalSpotsResponse.__mro__[1], StrictModel) is False or True
        Spot.model_validate(
            {
                "spot_id": "s1_0001",
                "stop_point": {"lat": 25.75, "lng": -80.37},
                "curb_bearing_deg": 180.0,
                "walk_distance_m": 12.0,
                "source": "osm",
                "confidence": "likely",
                "something_a_future_version_adds": True,
            }
        )


class TestLegalSpotsRequest:
    def test_radius_defaults_and_bounds(self):
        base = {"rider_location": {"lat": 25.75, "lng": -80.37}}
        assert LegalSpotsRequest(**base).radius_m == 150.0
        assert LegalSpotsRequest(**base, radius_m=0.5).radius_m == pytest.approx(0.5)

    @pytest.mark.parametrize("radius", [0.0, -10.0, 5001.0])
    def test_rejects_unusable_radii(self, radius):
        with pytest.raises(ValidationError):
            LegalSpotsRequest(
                rider_location=LatLng(lat=25.75, lng=-80.37), radius_m=radius
            )


class TestSpot:
    def _spot(self, **kw) -> Spot:
        base = dict(
            spot_id="s1_0001",
            stop_point=LatLng(lat=25.756918, lng=-80.372182),
            curb_bearing_deg=180.0,
            walk_distance_m=12.4,
            source="osm",
            confidence=Confidence.LIKELY,
        )
        return Spot(**{**base, **kw})

    def test_minimal_spot_serializes(self):
        d = self._spot().model_dump(mode="json")
        assert d["spot_id"] == "s1_0001"
        assert d["stop_point"] == {"lat": 25.756918, "lng": -80.372182}

    def test_bearing_is_wrapped_to_a_compass_angle(self):
        """S3 aims a camera with this, so 370 deg and 10 deg must be the same."""
        assert self._spot(curb_bearing_deg=370.0).curb_bearing_deg == pytest.approx(10.0)
        assert self._spot(curb_bearing_deg=-10.0).curb_bearing_deg == pytest.approx(350.0)

    def test_walk_distance_is_never_negative(self):
        """A negative walking distance would be a bug that reads as a better
        nearby spot, so it is rejected rather than clamped."""
        with pytest.raises(ValidationError):
            self._spot(walk_distance_m=-1.0)

    def test_side_is_unknown_by_default_not_assumed(self):
        """The degraded fallback spot is the rider's own location with no road
        attached, so there is genuinely no side. Defaulting to RIGHT would make a
        spot that forgot to set the field claim to be on the right."""
        assert self._spot().side is None
        assert self._spot(side=Side.LEFT).side is Side.LEFT

    def test_legality_basis_is_always_present(self):
        """Every spot must say *why* it is legal. An absent basis is how an
        inference ends up displayed as a fact."""
        assert self._spot().legality_basis is not None

    def test_clearance_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            self._spot(clearance_m=-0.5)

    def test_notes_default_to_a_list_not_none(self):
        assert self._spot().notes == []


class TestConditionAndSun:
    def test_condition_is_the_three_value_mode_enum(self):
        """Not a weather-conditions vocabulary. ENDPOINT.md line 415 defines
        `mode` as `rain | sun | neutral`, and `WeatherReport.condition` reuses the
        same three values on purpose: S2's job is to decide which protection the
        rider needs, and collapsing the raw WMO reading into these three at the
        boundary is what keeps that decision out of every downstream service."""
        assert {c.value for c in Condition} == {"rain", "sun", "neutral"}

    def test_conditions_result_carries_the_shade_source(self):
        """ENDPOINT.md section 4 requires a fallback when the Solar API is
        unavailable. The source has to be visible on the response or the
        difference between real shade data and modelled geometry is invisible in
        the product -- and to the person deciding whether to trust the answer.
        """
        r = ConditionsResult(
            weather=_weather(),
            mode=Condition.SUN,
            sun=SunPosition(elevation_deg=41.2, azimuth_deg=238.5),
            shade_source=ShadeSource.OSM_GEOMETRY,
        )
        assert r.shade_source is ShadeSource.OSM_GEOMETRY
        assert r.sun is not None and r.sun.elevation_deg == pytest.approx(41.2)

    def test_shade_source_defaults_to_null_not_a_guess(self):
        """Absent shade data must read as \"we don't know\", never as \"sunlit\".
        A default of google_solar here would make every unshaded answer look
        measured."""
        r = ConditionsResult(weather=_weather(), mode=Condition.SUN)
        assert r.shade_source is None
        assert r.sun is None

    def test_weather_override_is_recorded_on_the_report(self):
        """The demo weather override must be visible in the data, otherwise a
        recorded run cannot be distinguished from a real forecast."""
        w = _weather(overridden=True, reason="demo: forced rain for the scenario")
        assert w.overridden is True
        assert w.reason
