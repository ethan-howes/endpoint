"""Tests for weather classification (ENDPOINT.md section 6, module 1).

This module makes the one decision the rest of S2 turns on: rain, sun, or
neither. A wrong answer here is not a slightly-wrong ranking, it is a confident
recommendation to stand in the rain or to walk past an awning in 38 degrees --
so the thresholds are pinned as tests rather than left to a spot check.

The properties that matter most are the *orderings*, because they encode the
product's judgement: rain outranks sun, a sun verdict needs the sun to actually
be out, and a forced condition outranks a live forecast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services.s2_weather_cover import weather
from shared.models import Condition

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)


def sample(**kw) -> dict:
    base = {"precipitation": 0.0, "cloud_cover": 50.0, "is_day": True,
            "apparent_temperature": 28.0, "uv_index": 2.0,
            "direct_radiation": 100.0, "weather_code": 3}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# rain
# --------------------------------------------------------------------------- #

class TestRain:
    def test_precipitation_at_the_threshold_is_rain(self):
        assert weather.classify(sample(precipitation=0.2)) is Condition.RAIN

    def test_precipitation_below_the_threshold_is_not(self):
        """0.1 mm/h is a mist. Sending someone to an awning for it teaches them
        the feature is noise."""
        assert weather.classify(sample(precipitation=0.1)) is not Condition.RAIN

    @pytest.mark.parametrize("code", [51, 55, 57, 61, 63, 67, 80, 82, 95, 99])
    def test_every_documented_rain_code(self, code):
        """§6's table. A rain code with 0.0 mm/h still counts -- the code is the
        observation, the bucket average is not the same thing."""
        assert weather.classify(sample(weather_code=code, precipitation=0.0)) is Condition.RAIN

    @pytest.mark.parametrize("code", [0, 1, 2, 3, 45, 48, 71, 73, 75, 77])
    def test_non_rain_codes(self, code):
        """Including 71-77 (snow). Miami does not get snow, and a snow code must
        not produce "you need an awning"."""
        assert weather.classify(sample(weather_code=int(code))) is not Condition.RAIN


# --------------------------------------------------------------------------- #
# sun
# --------------------------------------------------------------------------- #

class TestSun:
    def test_hot_bright_day_is_sun(self):
        s = sample(uv_index=8, apparent_temperature=35, direct_radiation=600,
                   cloud_cover=10)
        assert weather.classify(s) is Condition.SUN

    @pytest.mark.parametrize("uv,temp", [(3.0, 35.0), (8.0, 24.0)])
    def test_needs_either_uv_or_heat(self, uv, temp):
        """§6: `uv >= 6 OR apparent >= 32`. Either alone is enough, because in
        Miami you get one without the other surprisingly often."""
        s = sample(uv_index=uv, apparent_temperature=temp, direct_radiation=600)
        assert weather.classify(s) is Condition.SUN

    def test_needs_the_sun_to_actually_be_out(self):
        """Overcast and 35 C is hot but there are no shadows to sit in, so
        recommending shade sends the rider walking for nothing."""
        s = sample(uv_index=8, apparent_temperature=35, direct_radiation=50)
        assert weather.classify(s) is not Condition.SUN

    def test_night_is_never_sun(self):
        s = sample(uv_index=8, apparent_temperature=35, direct_radiation=600,
                   is_day=False)
        assert weather.classify(s) is not Condition.SUN

    def test_falls_back_to_cloud_cover_when_radiation_is_missing(self):
        """§6's documented fallback. Clear sky, high UV, no radiation field."""
        s = sample(uv_index=8, direct_radiation=None, cloud_cover=5)
        assert weather.classify(s) is Condition.SUN

    def test_overcast_is_not_sun_even_without_radiation(self):
        s = sample(uv_index=8, direct_radiation=None, cloud_cover=90)
        assert weather.classify(s) is not Condition.SUN

    def test_rain_outranks_sun(self):
        """They are not mutually exclusive, and rain is the one that gets a rider
        with mobility needs wet."""
        s = sample(precipitation=3.0, uv_index=9, apparent_temperature=36,
                   direct_radiation=600)
        assert weather.classify(s) is Condition.RAIN


# --------------------------------------------------------------------------- #
# the wait window
# --------------------------------------------------------------------------- #

class TestClassifyWindow:
    def test_takes_the_worst_condition_in_the_window(self):
        """The departure from §6, and the reason it exists. The rider is on the
        kerb for ten minutes; a downpour six minutes after the car arrives is a
        rain ride, and reading the arrival instant would recommend a dry walk
        straight into it."""
        window = [
            {"when_utc": NOW, "precipitation": 0.0},
            {"when_utc": NOW + timedelta(hours=1), "precipitation": 4.2},
        ]
        mode, reason = weather.classify_window(window)
        assert mode is Condition.RAIN
        assert "4.2" in reason

    def test_calm_throughout_is_neutral(self):
        window = [{"when_utc": NOW + timedelta(hours=h), "precipitation": 0.0}
                  for h in range(3)]
        assert weather.classify_window(window)[0] is Condition.NEUTRAL

    def test_empty_window_says_so_rather_than_claiming_calm(self):
        """An empty result means "we have no data", and reporting it as calm
        weather would be a confident lie in the shape of a reading."""
        mode, reason = weather.classify_window([])
        assert mode is Condition.NEUTRAL
        assert "No forecast" in reason

    def test_reports_the_sun_reason_with_numbers(self):
        """§6's example reason is "UV index 8 and feels like 35 C". The numbers
        are what makes the decision checkable rather than oracular."""
        window = [{"when_utc": NOW, "uv_index": 8.4, "apparent_temperature": 35.2,
                   "direct_radiation": 600}]
        _, reason = weather.classify_window(window)
        assert "UV index 8" in reason and "feels like 35" in reason


# --------------------------------------------------------------------------- #
# window selection
# --------------------------------------------------------------------------- #

class TestSamplesInWindow:
    def _hourly(self) -> list[dict]:
        return [
            {"when_utc": datetime(2026, 9, 27, h, 0, tzinfo=timezone.utc),
             "precipitation": float(h)}
            for h in range(24)
        ]

    def test_a_late_start_touches_two_hours(self):
        """18:55 plus ten minutes spans part of the 18:00 hour and part of the
        19:00 hour. The rider is present for both."""
        out = weather.samples_in_window(
            self._hourly(),
            datetime(2026, 9, 27, 18, 55, tzinfo=timezone.utc),
            datetime(2026, 9, 27, 19, 5, tzinfo=timezone.utc),
        )
        assert [s["when_utc"].hour for s in out] == [18, 19]

    def test_exactly_on_the_hour_touches_one(self):
        out = weather.samples_in_window(
            self._hourly(),
            datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 27, 18, 10, tzinfo=timezone.utc),
        )
        assert [s["when_utc"].hour for s in out] == [18]

    def test_a_naive_datetime_is_treated_as_utc_not_crashing(self):
        """The orchestrator parses `force_time` from user input, so a naive
        datetime is a real boundary, not a theoretical one."""
        out = weather.samples_in_window(
            self._hourly(),
            datetime(2026, 9, 27, 18, 0),
            datetime(2026, 9, 27, 18, 30),
        )
        assert len(out) == 1

    def test_outside_the_forecast_is_empty(self):
        assert weather.samples_in_window(
            self._hourly(),
            datetime(2027, 1, 1, tzinfo=timezone.utc),
            datetime(2027, 1, 1, 0, 10, tzinfo=timezone.utc),
        ) == []


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

class TestParseHourly:
    def test_columnar_becomes_rows(self):
        """Open-Meteo returns parallel arrays. Every consumer wants to ask about
        a time, not about element 12 of the precipitation array."""
        payload = {"hourly": {
            "time": ["2026-09-27T18:00", "2026-09-27T19:00"],
            "precipitation": [0.0, 2.5],
            "is_day": [1, 1],
            "weather_code": [3, 63],
        }}
        rows = weather.parse_hourly(payload)
        assert len(rows) == 2
        assert rows[1]["precipitation"] == 2.5
        assert rows[1]["weather_code"] == 63
        assert rows[1]["is_day"] is True

    def test_missing_fields_are_dropped_not_stored_as_none(self):
        payload = {"hourly": {"time": ["2026-09-27T18:00"],
                              "precipitation": [None]}}
        row = weather.parse_hourly(payload)[0]
        assert "precipitation" not in row

    def test_absurd_precipitation_is_capped(self):
        """Some stations report a bucket-averaged 0 through a bucket that was
        clearly full. Clamping is better than a 9000 mm/h headline."""
        payload = {"hourly": {"time": ["2026-09-27T18:00"],
                              "precipitation": [99999.0]}}
        assert weather.parse_hourly(payload)[0]["precipitation"] == weather.MAX_PRECIP_MM_H

    def test_a_malformed_timestamp_is_skipped_not_fatal(self):
        payload = {"hourly": {"time": ["not-a-time", "2026-09-27T19:00"],
                              "precipitation": [1.0, 2.0]}}
        assert len(weather.parse_hourly(payload)) == 1

    def test_empty_payload(self):
        assert weather.parse_hourly({}) == []


# --------------------------------------------------------------------------- #
# assess: the whole path
# --------------------------------------------------------------------------- #

def _weather_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _forecast(condition: str) -> callable:
    precip = 5.0 if condition == "rain" else 0.0
    uv = 9.0 if condition == "sun" else 2.0
    def handler(request):
        return httpx.Response(200, json={"hourly": {
            "time": [f"2026-09-27T{h:02d}:00" for h in range(24)],
            "precipitation": [precip] * 24,
            "is_day": [1] * 24,
            "weather_code": [63 if condition == "rain" else 3] * 24,
            "uv_index": [uv] * 24,
            "apparent_temperature": [35.0] * 24,
            "direct_radiation": [600.0] * 24,
            "cloud_cover": [10.0] * 24,
        }})
    return handler


class TestAssess:
    async def test_a_rain_forecast_produces_rain_mode(self):
        weather.clear_cache()
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 client=_weather_client(_forecast("rain")))
        assert a.mode is Condition.RAIN
        assert not a.is_fallback
        assert a.report.precip_mm_h == 5.0

    async def test_a_sunny_forecast_produces_sun_mode(self):
        weather.clear_cache()
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 client=_weather_client(_forecast("sun")))
        assert a.mode is Condition.SUN
        assert a.report.uv_index == 9.0

    async def test_the_numbers_come_from_the_hour_that_decided_it(self):
        """A report saying 4.2 mm/h must be backed by an hour that had 4.2 mm/h
        in it, not by the first or last row of the window."""
        weather.clear_cache()
        def handler(request):
            return httpx.Response(200, json={"hourly": {
                "time": [f"2026-09-27T{h:02d}:00" for h in range(24)],
                "precipitation": [0.0] * 18 + [4.2] * 6,
                "is_day": [1] * 24, "weather_code": [3] * 24,
            }})
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 client=_weather_client(handler))
        assert a.mode is Condition.RAIN
        assert a.report.precip_mm_h == 4.2

    async def test_a_forced_condition_beats_a_live_forecast(self):
        """The override exists to demonstrate behaviour the real sky will not
        cooperate with. A demo that reports the actual weather is not a demo."""
        weather.clear_cache()
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 force_condition=Condition.RAIN,
                                 client=_weather_client(_forecast("sun")))
        assert a.mode is Condition.RAIN
        assert a.report.overridden is True
        assert a.report.source == "demo_override"

    async def test_a_forced_condition_makes_no_request_at_all(self):
        """Checked rather than assumed: MOCK mode depends on it, and a forced
        condition during a network-less demo must not fail."""
        weather.clear_cache()

        def explode(request):
            raise AssertionError("must not call the network when overridden")

        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 force_condition=Condition.SUN,
                                 client=_weather_client(explode))
        assert a.mode is Condition.SUN

    async def test_an_overridden_report_invents_no_measurements(self):
        """Plausible-looking numbers behind a forced condition would let a
        screenshot of the demo be mistaken for a real reading."""
        weather.clear_cache()
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 force_condition=Condition.RAIN,
                                 client=_weather_client(_forecast("rain")))
        assert a.report.precip_mm_h == 0.0
        assert a.report.uv_index is None
        assert a.report.overridden is True
        assert "demo" in a.report.reason.lower()

    async def test_an_outage_degrades_to_an_explicit_fallback(self):
        """Not a crash, and not a claim that the weather is calm: `source` and a
        populated `reason` are what stop "no data" reading as "no rain"."""
        weather.clear_cache()
        def boom(request):
            raise httpx.ConnectError("open-meteo down")
        a = await weather.assess(25.757, -80.372, NOW, 10,
                                 client=_weather_client(boom))
        assert a.mode is Condition.NEUTRAL
        assert a.is_fallback is True
        assert "ConnectError" in a.report.reason

    async def test_a_time_outside_the_forecast_is_a_fallback_not_calm_weather(self):
        weather.clear_cache()
        a = await weather.assess(25.757, -80.372,
                                 datetime(2030, 1, 1, tzinfo=timezone.utc), 10,
                                 client=_weather_client(_forecast("rain")))
        assert a.is_fallback is True
        assert "outside" in a.report.reason

    async def test_caches_within_the_ttl(self):
        """§6: per ~1 km cell for 5 minutes. Without it a polling UI hammers a
        free API."""
        weather.clear_cache()
        calls = {"n": 0}

        def counting(request):
            calls["n"] += 1
            return _forecast("rain")(request)

        c = _weather_client(counting)
        await weather.assess(25.7570, -80.3720, NOW, 10, client=c)
        await weather.assess(25.7571, -80.3721, NOW, 10, client=c)
        assert calls["n"] == 1

    async def test_a_different_cell_is_a_different_cache_entry(self):
        """Coarser would serve a forecast from the wrong side of a thunderstorm."""
        weather.clear_cache()
        calls = {"n": 0}

        def counting(request):
            calls["n"] += 1
            return _forecast("rain")(request)

        c = _weather_client(counting)
        await weather.assess(25.757, -80.372, NOW, 10, client=c)
        await weather.assess(25.780, -80.372, NOW, 10, client=c)
        assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# sun position
# --------------------------------------------------------------------------- #

class TestSunPosition:
    def test_returns_a_position(self):
        sp = weather.sun_position(25.7569, -80.3722, NOW)
        assert sp is not None
        assert -90 <= sp.elevation_deg <= 90
        assert 0 <= sp.azimuth_deg <= 360

    def test_midday_is_higher_than_midnight(self):
        """A smoke test that would catch a units error -- e.g. passing degrees
        where radians belong, which pvlib does not raise on."""
        noon = weather.sun_position(25.7569, -80.3722,
                                    datetime(2026, 9, 27, 16, 30, tzinfo=timezone.utc))
        night = weather.sun_position(25.7569, -80.3722,
                                     datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc))
        assert noon.elevation_deg > night.elevation_deg
        assert night.elevation_deg < 0

    def test_morning_and_afternoon_shadows_point_different_ways(self):
        """The mechanism behind §6's headline demo. 10:00 EDT shadows fall WNW;
        16:00 EDT they fall ENE. If this ever inverts, the demo is showing
        shadows that do not move."""
        am = weather.sun_position(25.7569, -80.3722,
                                  datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc))
        pm = weather.sun_position(25.7569, -80.3722,
                                  datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc))
        assert am.azimuth_deg < 180, "morning sun is in the east"
        assert pm.azimuth_deg > 180, "afternoon sun is in the west"
