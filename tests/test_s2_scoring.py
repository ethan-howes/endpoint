"""Tests for cover scoring and shadow geometry (ENDPOINT.md section 6, modules 2-3).

Two of these encode a real defect in the doc's own formula, so they are worth
reading before changing the code they cover:

- ``gap_factor`` reaching 0 at ``max_gap_m``, combined with the separate
  ``0.3 * walk_factor`` awarded to spots with *no* cover, means a spot with a
  shelter 4.9 m away ranks *below* a spot with nothing at all. A ranking that
  points the rider away from the awning as they approach it is not a tuning
  issue.
- The doc's shadow geometry is easy to get backwards. If ``az + 180`` is missed
  the shadows point *toward* the sun, and the "shadows flip sides" demo inverts
  into a demo of shadows that do not move -- which still looks plausible in a
  screenshot.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from services.s2_weather_cover import scoring, sun_shade
from services.s2_weather_cover.cover import Cover, CoverMap, ShadeBlock, ShadeMap
from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import Confidence, LatLng, RankedSpot, Side, Spot, SunPosition

RIDER = (25.7570, -80.3720)


def frame() -> LocalFrame:
    return LocalFrame(*RIDER)


def spot(spot_id: str, walk_m: float, x: float, y: float) -> Spot:
    """A spot placed at local-frame metres, so tests can reason in real units."""
    lat, lng = frame().to_ll(x, y)
    return Spot(
        spot_id=spot_id,
        stop_point=LatLng(lat=lat, lng=lng),
        side=Side.LEFT,
        curb_bearing_deg=90.0,
        walk_distance_m=walk_m,
        street_name=None,
        confidence=Confidence.UNVERIFIED,
    )


def cover_line(feature_id: str, x0: float, y0: float, x1: float, y1: float) -> Cover:
    return Cover(
        feature_id=feature_id,
        kind="covered_walkway",
        shape=LineString([(x0, y0), (x1, y1)]),
        provides_sun=True,
    )


# --------------------------------------------------------------------------- #
# the factors
# --------------------------------------------------------------------------- #

class TestFactors:
    def test_gap_factor_is_flat_at_the_kerb(self):
        """A rider on the edge of an awning and one a metre inside are not
        meaningfully differently protected, and a ramp from zero would rank the
        second worse for something the rider cannot perceive."""
        assert scoring.gap_factor(0.0, 5.0) == 1.0
        assert scoring.gap_factor(1.5, 5.0) == 1.0

    def test_gap_factor_decays_to_zero_at_the_limit(self):
        assert scoring.gap_factor(5.0, 5.0) == 0.0
        assert 0.0 < scoring.gap_factor(3.25, 5.0) < 1.0

    def test_gap_factor_never_goes_negative(self):
        assert scoring.gap_factor(50.0, 5.0) == 0.0

    def test_walk_factor_decreases_but_never_reaches_zero(self):
        """Walk distance is a tie-breaker, not a disqualifier. A factor that could
        reach 0 would make a long walk to real cover score worse than no cover."""
        assert scoring.walk_factor(0.0) == 1.0
        assert scoring.walk_factor(300.0) == pytest.approx(0.5)
        assert scoring.walk_factor(100_000.0) == pytest.approx(0.5)

    def test_walk_factor_tolerates_a_negative(self):
        """Not a real input, but a zero score is a silent bug rather than a
        crash, and the alternative is a division producing nonsense."""
        assert scoring.walk_factor(-5.0) == 1.0

    def test_the_reachable_tier_is_the_baseline(self):
        """Both tables are relative, so the tier this service can actually
        produce is 1.0 and better provenance scales above it. ENDPOINT.md's
        absolute tables made the only reachable tier numerically equal to the
        no-cover score, which cancelled the whole ranking -- see
        ``scripts/show_rain_score_defect.py``."""
        assert scoring.conf_weight(Confidence.UNVERIFIED) == 1.0
        assert scoring.source_weight("osm_geometry") == 1.0

    def test_better_provenance_outranks_worse(self):
        for tier in (Confidence.DETECTED, Confidence.LIKELY, Confidence.VERIFIED):
            assert scoring.conf_weight(tier) > scoring.conf_weight(Confidence.UNVERIFIED)
        assert (
            scoring.source_weight("cover_feature")
            > scoring.source_weight("osm_geometry")
        )

    def test_an_unknown_tier_falls_back_to_the_baseline_not_to_zero(self):
        """A zero here would silently rank a feature as worthless. 1.0 is the
        safe direction to fail: the gap and walk terms still discriminate."""
        assert scoring.conf_weight(Confidence.DETECTED) >= 1.0
        assert scoring.source_weight("some_future_source") == 1.0


class TestCoveredScore:
    def test_proximity_to_cover_is_strictly_rewarded(self):
        """The bug ENDPOINT.md's two 0.3s created: a shelter overhead and no
        shelter at all both scored 0.3 * walk_factor, so cover did not influence
        the answer at all and the rain ranking was plain walk distance."""
        scores = [
            scoring.covered_score(g, Confidence.UNVERIFIED, 50.0, 5.0)
            for g in (0.0, 1.5, 2.0, 3.0, 4.0, 4.5)
        ]
        assert scores == sorted(scores, reverse=True), (
            "score must decrease monotonically with the gap to cover"
        )
        assert scores[0] > scores[-1]

    def test_cover_overhead_beat_no_cover_before_the_fix_and_must_still(self):
        """The headline number: 50 m walk, 5 m limit."""
        adjacent = scoring.covered_score(0.0, Confidence.UNVERIFIED, 50.0, 5.0)
        nothing = scoring.no_cover_score(50.0)
        assert adjacent == pytest.approx(0.917, abs=0.01)
        assert adjacent > nothing

    def test_real_cover_never_ranks_below_no_cover(self):
        """The floor's whole job. Without it, a spot with a shelter 4.9 m away
        scores ~0 and a spot with nothing at all scores 0.3 -- so the ranking
        got *worse* as the rider walked towards the awning."""
        floor = scoring.no_cover_score(50.0)
        for gap in (0.0, 1.5, 3.0, 4.5, 4.9, 5.0, 99.0):
            s = scoring.covered_score(gap, Confidence.UNVERIFIED, 50.0, 5.0)
            assert s >= floor - 1e-9, f"gap {gap} scored below the no-cover floor"

    def test_the_floor_only_bites_in_the_useless_band(self):
        """Rebasing conf_weight to 1.0 put the floor below the whole covered
        range. If this ever regresses, cover has stopped discriminating and the
        ranking is walk distance again."""
        for gap in (0.0, 1.5, 2.0, 2.5, 3.0):
            s = scoring.covered_score(gap, Confidence.UNVERIFIED, 50.0, 5.0)
            assert s > scoring.no_cover_score(50.0) + 1e-9, (
                f"gap {gap} is on the floor, so proximity to cover is not counted"
            )

    def test_a_shorter_walk_wins_at_equal_gap(self):
        near_walk = scoring.covered_score(2.0, Confidence.UNVERIFIED, 20.0, 5.0)
        far_walk = scoring.covered_score(2.0, Confidence.UNVERIFIED, 200.0, 5.0)
        assert near_walk > far_walk

    def test_a_confirmed_feature_beats_an_unverified_one(self):
        """A constant across OSM features today, so this pins the seam for when
        a real shelter feed arrives rather than testing live behaviour."""
        assert scoring.conf_weight(Confidence.VERIFIED) > scoring.conf_weight(
            Confidence.UNVERIFIED
        )


class TestRankKey:
    def _rs(self, score: float, gap, walk: float, sid: str) -> RankedSpot:
        ll = frame().to_ll(0.0, 0.0)
        return RankedSpot(
            spot=Spot(
                spot_id=sid,
                stop_point=LatLng(lat=ll[0], lng=ll[1]),
                side=Side.LEFT,
                curb_bearing_deg=90.0,
                walk_distance_m=walk,
                confidence=Confidence.UNVERIFIED,
            ),
            gap_m=gap,
            score=score,
        )

    def test_higher_score_wins(self):
        a = self._rs(0.9, 0.0, 100.0, "a")
        b = self._rs(0.4, 0.0, 10.0, "b")
        assert sorted([b, a], key=scoring.rank_key)[0].spot.spot_id == "a"

    def test_within_the_floor_band_closer_cover_still_wins(self):
        """Both are floored to the same score, so the gap term is the only thing
        left that prefers real cover. Without it a spot 4 m from a shelter would
        tie with one 300 m from it and the ordering would be arbitrary."""
        close = self._rs(0.275, 3.9, 100.0, "close")
        far = self._rs(0.275, 4.9, 100.0, "far")
        assert sorted([far, close], key=scoring.rank_key)[0].spot.spot_id == "close"

    def test_a_missing_gap_sorts_as_infinitely_far(self):
        """`None` means "no cover found", which is worse than any real gap, so it
        must not sort as 0.0 and jump the queue."""
        none_gap = self._rs(0.275, None, 10.0, "none")
        with_gap = self._rs(0.275, 5.0, 100.0, "some")
        assert scoring.rank_key(none_gap) > scoring.rank_key(with_gap)

    def test_identical_everything_is_broken_by_id(self):
        """Reproducibility: the demo must not reshuffle its own recommendation
        between runs on identical input."""
        a = self._rs(0.5, 2.0, 50.0, "aaa")
        b = self._rs(0.5, 2.0, 50.0, "bbb")
        assert scoring.rank_key(a) < scoring.rank_key(b)


class TestDetourConfirmation:
    def test_asks_past_the_threshold(self):
        """§6 line 519: past 120 m of extra walking, ask the rider."""
        assert scoring.needs_detour_confirmation(200.0, 0.0) is True

    def test_does_not_ask_for_a_short_extra_walk(self):
        assert scoring.needs_detour_confirmation(100.0, 0.0) is False

    def test_the_threshold_is_strictly_greater_than(self):
        """Exactly 120 m is not "more than 120 m"."""
        assert scoring.needs_detour_confirmation(120.0, 0.0) is False
        assert scoring.needs_detour_confirmation(120.001, 0.0) is True

    def test_nothing_to_confirm_when_there_is_no_extra_walk(self):
        assert scoring.needs_detour_confirmation(0.0, 0.0) is False


# --------------------------------------------------------------------------- #
# shadow geometry
# --------------------------------------------------------------------------- #

class TestShadowOffset:
    def test_a_midday_shadow_points_north(self):
        """Sun due south at solar noon in the northern hemisphere, so the shadow
        runs north. Azimuth 180 + 180 = 360."""
        dx, dy = sun_shade.shadow_offset(10.0, 45.0, 180.0)
        assert dx == pytest.approx(0.0, abs=1e-9)
        assert dy > 0

    def test_a_morning_shadow_points_west(self):
        """10am: sun in the east-southeast, shadow to the west-northwest."""
        dx, dy = sun_shade.shadow_offset(10.0, 40.0, 113.0)
        assert dx < 0, "morning shadows fall to the west"
        assert dy > 0, "and to the north"

    def test_a_afternoon_shadow_points_east(self):
        """4pm: sun in the west-southwest, shadow to the east-northeast. This is
        the flip the doc's demo rests on."""
        dx, dy = sun_shade.shadow_offset(10.0, 41.0, 242.0)
        assert dx > 0, "afternoon shadows fall to the east"
        assert dy > 0

    def test_morning_and_afternoon_shadows_are_opposite_in_east_west(self):
        """The property, stated directly so an inverted ``az + 180`` fails here
        with a clear message rather than as a subtly wrong demo."""
        am = sun_shade.shadow_offset(10.0, 40.0, 113.0)[0]
        pm = sun_shade.shadow_offset(10.0, 41.0, 242.0)[0]
        assert am < 0 < pm

    def test_shadow_length_matches_the_trig(self):
        dx, dy = sun_shade.shadow_offset(6.0, 45.0, 180.0)
        assert math.hypot(dx, dy) == pytest.approx(6.0, rel=1e-6)

    def test_a_lower_sun_casts_a_longer_shadow(self):
        low = sun_shade.shadow_offset(10.0, 10.0, 180.0)
        high = sun_shade.shadow_offset(10.0, 70.0, 180.0)
        assert abs(low[1]) > abs(high[1])

    def test_the_length_is_capped(self):
        """``height / tan(elev)`` diverges at the horizon, and an uncapped 6 m
        building at 2 degrees would cast a 172 m shadow across the whole demo
        area and rank every spot identically."""
        dx, dy = sun_shade.shadow_offset(10.0, 0.5, 180.0, cap_m=100.0)
        assert math.hypot(dx, dy) <= 100.0 + 1e-9

    def test_survives_an_exactly_zero_elevation(self):
        """Would be a division by zero. The low-sun case is handled upstream by
        refusing to rank on shade at all, but the function should not be a
        landmine if that guard is ever moved."""
        dx, dy = sun_shade.shadow_offset(6.0, 0.0, 180.0)
        assert math.isfinite(dx) and math.isfinite(dy)


class TestBuildingShadow:
    def test_includes_the_footprint(self):
        """The wall itself is shade, not just the ground beyond it."""
        foot = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        s = sun_shade.building_shadow(foot, 8.0, 45.0, 180.0)
        assert s.covers(foot), "the shadow must contain the building it came from"

    def test_extends_away_from_the_sun(self):
        foot = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        s = sun_shade.building_shadow(foot, 8.0, 45.0, 113.0)  # morning
        assert s.bounds[0] < 0, "should reach west of the footprint"

    def test_a_zero_shadow_returns_the_footprint(self):
        foot = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        assert sun_shade.building_shadow(foot, 0.0, 45.0, 180.0).equals(foot)

    def test_handles_an_l_shaped_building(self):
        """The union-then-hull approach covers every wall; a swept rectangle
        would leave the re-entrant corner unshaded and quietly wrong."""
        l_shape = Polygon([(0, 0), (20, 0), (20, 4), (4, 4), (4, 20), (0, 20)])
        s = sun_shade.building_shadow(l_shape, 6.0, 30.0, 180.0)
        assert s.contains(Point(1, 1))
        assert s.area > l_shape.area


class TestTreeShadow:
    def test_at_forty_five_degrees_a_tree_shadows_its_own_height(self):
        """A tree of total height H with crown radius R casts a shadow reaching
        (H - R) / tan(elev) + R. At 45 degrees the first term is H - R, so the
        total is exactly H -- the tree's own height, whatever the crown is.
        Pins the geometry rather than a tuning knob."""
        for crown in (1.0, 3.0, 5.0):
            p = sun_shade.tree_shadow(Point(0, 0), 8.0, crown, 45.0, 180.0)
            assert p.bounds[3] == pytest.approx(8.0, abs=0.2)

    def test_a_crown_as_tall_as_the_tree_casts_no_streak(self):
        """`height - crown_r` is the crown's centre height, so a tree whose crown
        reaches its full height has no bare trunk to project and its shadow is
        just the crown's own footprint. This is the clamp doing its job."""
        squat = sun_shade.tree_shadow(Point(0, 0), 3.0, 3.0, 10.0, 180.0)
        tall = sun_shade.tree_shadow(Point(0, 0), 10.0, 3.0, 10.0, 180.0)
        assert squat.bounds[3] < 5.0
        assert tall.bounds[3] > 30.0

    def test_a_bigger_crown_shortens_a_low_sun_shadow(self):
        """Counter-intuitive but geometric: the crown hangs low, so its shadow
        starts nearer the trunk. At 10 degrees the difference is 20 m, which is
        the difference between shade you can walk to and shade you cannot."""
        thin = sun_shade.tree_shadow(Point(0, 0), 10.0, 1.0, 10.0, 180.0)
        bushy = sun_shade.tree_shadow(Point(0, 0), 10.0, 5.0, 10.0, 180.0)
        assert bushy.bounds[3] < thin.bounds[3]

    def test_crown_radius_sets_the_width(self):
        small = sun_shade.tree_shadow(Point(0, 0), 10.0, 1.0, 45.0, 180.0)
        big = sun_shade.tree_shadow(Point(0, 0), 10.0, 5.0, 45.0, 180.0)
        assert big.bounds[2] - big.bounds[0] > small.bounds[2] - small.bounds[0]


# --------------------------------------------------------------------------- #
# shade_fraction
# --------------------------------------------------------------------------- #

class TestShadeFraction:
    def test_fully_shaded_is_one(self):
        geom = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        assert sun_shade.shade_fraction(geom, Point(5, 5), 2.0) == pytest.approx(1.0)

    def test_fully_exposed_is_zero(self):
        geom = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        assert sun_shade.shade_fraction(geom, Point(50, 50), 2.0) == 0.0

    def test_half_shaded_is_between(self):
        geom = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        f = sun_shade.shade_fraction(geom, Point(10, 5), 2.0)
        assert 0.3 < f < 0.7

    def test_stays_in_range(self):
        geom = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        for x in (-100, 0, 5, 10, 100):
            f = sun_shade.shade_fraction(geom, Point(x, 5), 2.0)
            assert 0.0 <= f <= 1.0


# --------------------------------------------------------------------------- #
# the doc's definition of done: the side flip
# --------------------------------------------------------------------------- #

class TestSunDefinitionOfDone:
    """§6: "moving force_time from 10:00 to 16:00 local visibly changes which
    side of the street wins, because shadows flip sides."

    Built from one long building on the south side of an east-west street, so the
    only shade it can cast is on the north side in the morning and... on the
    north side in the afternoon too, which is not a flip. The geometry that does
    flip needs the obstacle to be *beside* the spot, not across from it -- so
    this uses two spots either side of a north-south building and checks that the
    sun crossing the meridian moves the shadow from one to the other.
    """

    def _street(self):
        """A 10 m building at x=0, and two kerbs 8 m either side of it."""
        f = frame()
        building = Polygon([(x, y) for x, y in
                            [(-5, -20), (5, -20), (5, 20), (-5, 20)]])
        west_kerb = spot("s1_west", 10.0, -8.0, 0.0)
        east_kerb = spot("s1_east", 10.0, 8.0, 0.0)
        smap = ShadeMap(
            frame=f, bbox=(0, 0, 0, 0),
            blocks=[ShadeBlock(block_id="b1", shape=building, height_m=10.0,
                               kind="building", height_estimated=False)],
        )
        cmap = CoverMap(frame=f, bbox=(0, 0, 0, 0), covers=[])
        return smap, cmap, [west_kerb, east_kerb]

    def test_the_winning_side_changes_with_the_time_of_day(self):
        smap, cmap, spots = self._street()

        # 13:00 UTC = 09:00 EDT. Sun in the east -> shadow to the west.
        morning = sun_shade.rank_spots(
            spots, smap, cmap,
            SunPosition(elevation_deg=45.0, azimuth_deg=110.0),
            SunPosition(elevation_deg=42.0, azimuth_deg=125.0),
        )[0]
        # 20:00 UTC = 16:00 EDT. Sun in the west -> shadow to the east.
        afternoon = sun_shade.rank_spots(
            spots, smap, cmap,
            SunPosition(elevation_deg=41.0, azimuth_deg=242.0),
            SunPosition(elevation_deg=35.0, azimuth_deg=255.0),
        )[0]

        assert morning[0].spot.spot_id != afternoon[0].spot.spot_id, (
            "the shaded side did not change with the time of day -- the "
            "definition-of-done demo would show nothing happening"
        )

    def test_the_morning_winner_is_the_west_side(self):
        smap, cmap, spots = self._street()
        ranked = sun_shade.rank_spots(
            spots, smap, cmap,
            SunPosition(elevation_deg=45.0, azimuth_deg=110.0),
            SunPosition(elevation_deg=42.0, azimuth_deg=125.0),
        )[0]
        assert ranked[0].spot.spot_id == "s1_west"

    def test_the_afternoon_winner_is_the_east_side(self):
        smap, cmap, spots = self._street()
        ranked = sun_shade.rank_spots(
            spots, smap, cmap,
            SunPosition(elevation_deg=41.0, azimuth_deg=242.0),
            SunPosition(elevation_deg=35.0, azimuth_deg=255.0),
        )[0]
        assert ranked[0].spot.spot_id == "s1_east"

    def test_a_low_sun_ranks_by_walk_and_explains_itself(self):
        """§6: below 5 degrees, shade is a sliver along a wall, not somewhere to
        send someone. The reason has to say so, or the rider is told to wait
        under an awning for no stated reason."""
        smap, cmap, spots = self._street()
        ranked, geom = sun_shade.rank_spots(
            spots, smap, cmap,
            SunPosition(elevation_deg=2.0, azimuth_deg=110.0),
            SunPosition(elevation_deg=1.0, azimuth_deg=115.0),
        )
        assert all("Sun is low" in r.reason for r in ranked)
        assert geom is None

    def test_no_shade_data_falls_back_with_a_reason(self):
        empty = ShadeMap(frame=frame(), bbox=(0, 0, 0, 0), blocks=[])
        cmap = CoverMap(frame=frame(), bbox=(0, 0, 0, 0), covers=[])
        spots = [spot("a", 30.0, 0.0, 0.0), spot("b", 10.0, 5.0, 0.0)]
        ranked, geom = sun_shade.rank_spots(
            spots, empty, cmap,
            SunPosition(elevation_deg=45.0, azimuth_deg=110.0),
            SunPosition(elevation_deg=42.0, azimuth_deg=125.0),
        )
        assert geom is None
        assert ranked[0].spot.spot_id == "b", "fallback is nearest first"
        assert all("No shade" in r.reason for r in ranked)

    def test_a_covered_walkway_counts_as_shaded_at_night(self):
        """A rider under an arcade is not getting rained on regardless of where
        the sun is, and a mapped feature beats a modelled shadow as evidence."""
        f = frame()
        arcade = cover_line("osm_way_1", -20.0, 0.0, 20.0, 0.0)
        cmap = CoverMap(frame=f, bbox=(0, 0, 0, 0), covers=[arcade])
        smap = ShadeMap(frame=f, bbox=(0, 0, 0, 0), blocks=[])
        on_it = [spot("s1_on", 10.0, 0.0, 0.0), spot("s1_off", 40.0, 30.0, 0.0)]
        ranked = sun_shade.rank_spots(
            on_it, smap, cmap,
            SunPosition(elevation_deg=50.0, azimuth_deg=90.0),
            SunPosition(elevation_deg=45.0, azimuth_deg=100.0),
        )[0]
        assert ranked[0].spot.spot_id == "s1_on"
        assert ranked[0].cover_feature is not None
        assert ranked[0].cover_feature.feature_id == "osm_way_1"

    def test_one_malformed_block_does_not_cost_every_shadow(self):
        """A footprint that survives ingest but throws when shadowed would
        otherwise take the whole shade layer with it."""
        f = frame()
        good = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
        smap = ShadeMap(frame=f, bbox=(0, 0, 0, 0), blocks=[
            ShadeBlock(block_id="bad", shape=Point(0, 0), height_m=10.0,
                       kind="tree", crown_r=None),
            ShadeBlock(block_id="good", shape=good, height_m=10.0, kind="building"),
        ])
        cmap = CoverMap(frame=f, bbox=(0, 0, 0, 0), covers=[])
        geom = sun_shade.shade_geometry(
            smap, cmap, SunPosition(elevation_deg=45.0, azimuth_deg=180.0)
        )
        assert geom is not None
        assert geom.area > 0
