"""Module 1 (ENDPOINT.md section 6): what are the conditions at pickup time?

Two jobs, kept separate on purpose: *fetching* a forecast, and *deciding* what it
means for a rider. The second is where the product's judgement lives, and it is a
pure function over a small dict so it can be tested exhaustively without a
network.

The decision this module makes is the one the whole downstream system turns on:
rain, sun, or neither. Get it wrong and the other two modules rank the wrong
thing -- a spot that is optimally shaded is useless in a thunderstorm, and an
awning is not what you want in 38 degrees.

One deliberate departure from the doc: it reads the weather at a single instant,
``pickup_time``. The rider is not standing on the kerb for an instant, they are
standing there for ``wait_minutes`` (10). Rain that arrives six minutes after the
car does is a rain ride, and classifying on the arrival instant would recommend a
dry walk straight into a downpour. So the classification runs over the whole
window and takes the most rider-relevant outcome in it. That is a three-line
change here and it is the difference between the forecast being informative and
the forecast being wrong exactly when it matters.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from shared.config import SETTINGS
from shared.models import Condition, SunPosition, WeatherReport

log = logging.getLogger("endpoint.s2.weather")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

#: WMO weather codes that mean precipitation. ENDPOINT.md lists 51-57, 61-67,
#: 80-82 and 95-99; 95-99 are thunderstorms. 71-77 (snow) are deliberately
#: excluded -- it does not rain in Miami, and a snow code should not make the
#: service claim a rider needs an awning.
RAIN_CODES = frozenset(
    list(range(51, 58)) + list(range(61, 68)) + list(range(80, 83)) + list(range(95, 100))
)

#: Cap on a single hourly reading. Some stations report 0 in a bucket-averaged
#: hour that is clearly raining; a trace reading of 0.0 with a rain code still
#: counts as rain via RAIN_CODES, so this only guards against absurd values.
MAX_PRECIP_MM_H = 200.0


# --------------------------------------------------------------------------- #
# Classification (pure)
# --------------------------------------------------------------------------- #

def _is_rain(sample: dict[str, Any]) -> bool:
    code = sample.get("weather_code")
    if code is not None and int(code) in RAIN_CODES:
        return True
    precip = sample.get("precipitation")
    return precip is not None and float(precip) >= SETTINGS.rain_precip_mm_h


def _is_sun(sample: dict[str, Any]) -> bool:
    """Too bright and hot enough that shade is worth walking to.

    Requires the sun to actually be out (``direct_radiation``) as well as the
    exposure to be high. Either test alone is wrong: a cloudless January morning
    in Miami has a UV index of 3 and needs no shade, and a hot overcast afternoon
    has a high feels-like temperature but no shadows to sit in.
    """
    if sample.get("is_day") in (0, False):
        return False

    radiation = sample.get("direct_radiation")
    if radiation is None:
        # No radiation in the response: fall back to a clear-sky proxy.
        cloud = sample.get("cloud_cover")
        if cloud is None or float(cloud) >= SETTINGS.sun_cloud_cover_pct:
            return False
    elif float(radiation) < SETTINGS.sun_radiation_w_m2:
        return False

    uv = sample.get("uv_index")
    apparent = sample.get("apparent_temperature")
    uv_high = uv is not None and float(uv) >= SETTINGS.sun_uv_index
    temp_high = apparent is not None and float(apparent) >= SETTINGS.sun_apparent_temp_c
    return bool(uv_high or temp_high)


def classify(sample: dict[str, Any]) -> Condition:
    """One hour of forecast -> which protection the rider needs.

    Rain wins over sun. They are not mutually exclusive (a hot day can rain), and
    if both are true the rain is the one that gets a mobility-impaired rider wet.
    """
    if _is_rain(sample):
        return Condition.RAIN
    if _is_sun(sample):
        return Condition.SUN
    return Condition.NEUTRAL


def classify_window(samples: list[dict[str, Any]]) -> tuple[Condition, str]:
    """The most rider-relevant condition across a whole wait window.

    Returns the condition and the sentence that justifies it, because that
    sentence is what tells a rider (and a judge at a demo) why the system decided
    what it decided. ENDPOINT.md's example is "UV index 8 and feels like 35 C" --
    the same idea, and just as load-bearing.
    """
    if not samples:
        return Condition.NEUTRAL, "No forecast available for the pickup window"

    conditions = [classify(s) for s in samples]
    for wanted in (Condition.RAIN, Condition.SUN):
        for s, c in zip(samples, conditions):
            if c is wanted:
                return wanted, _explain(wanted, s)
    return Condition.NEUTRAL, _explain(Condition.NEUTRAL, samples[0])


def _explain(condition: Condition, s: dict[str, Any]) -> str:
    if condition is Condition.RAIN:
        precip = s.get("precipitation")
        if precip is not None and float(precip) > 0:
            return f"{float(precip):.1f} mm/h of rain when your car arrives"
        return "Rain expected when your car arrives"
    if condition is Condition.SUN:
        bits = []
        if s.get("uv_index") is not None:
            bits.append(f"UV index {float(s['uv_index']):.0f}")
        if s.get("apparent_temperature") is not None:
            bits.append(f"feels like {float(s['apparent_temperature']):.0f} C")
        return " and ".join(bits) + " when your car arrives" if bits else \
            "Bright and hot when your car arrives"
    cloud = s.get("cloud_cover")
    if cloud is not None:
        return f"Overcast ({float(cloud):.0f}% cloud) -- no rain or strong sun"
    return "No rain or strong sun right now"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def build_url(lat: float, lng: float) -> str:
    return (
        f"{OPEN_METEO}?latitude={lat:.5f}&longitude={lng:.5f}"
        "&current=temperature_2m,apparent_temperature,precipitation,rain,"
        "cloud_cover,is_day,weather_code"
        "&hourly=precipitation,precipitation_probability,cloud_cover,is_day,"
        "weather_code,uv_index,direct_radiation,apparent_temperature"
        "&timezone=UTC&forecast_days="
        f"{SETTINGS.weather_forecast_days}"
    )


def _row_to_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Open-Meteo hourly row, dropping the Nones.

    Open-Meteo returns `-` or omits fields it does not have, and every downstream
    threshold treats missing as "no evidence", so absent keys are dropped rather
    than stored as None.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            continue
        if key == "precipitation":
            out["precipitation"] = min(float(value), MAX_PRECIP_MM_H)
        elif key == "is_day":
            out["is_day"] = int(value) == 1
        elif key == "weather_code":
            out["weather_code"] = int(value)
        else:
            out[key] = float(value)
    return out


def parse_hourly(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The hourly block as ``[{when_utc, ...fields}]``, oldest first.

    Open-Meteo is columnar (parallel arrays, not rows), so this is where it
    becomes addressable by time. Every consumer downstream wants to ask "what
    happens at 18:30", not "what is element 12 of the precipitation array".
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    keys = [k for k in hourly if k != "time"]
    out = []
    for i, t in enumerate(times):
        row = {k: hourly[k][i] for k in keys if i < len(hourly.get(k) or [])}
        try:
            when = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        out.append({"when_utc": when, **_row_to_sample(row)})
    return out


def samples_in_window(
    hourly: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Hourly rows overlapping ``[start, end]``.

    A ten minute wait starting at 18:55 touches the 18:00 and 19:00 rows, and
    that overlap is real: the rider is present for part of each. An empty result
    means the requested time is outside the forecast, which the caller reports as
    a fallback rather than silently treating as calm weather.

    Each row is treated as the half-open interval ``[hour, hour+1)``, matching
    how Open-Meteo labels them. The half-openness matters at the left edge: a
    wait starting exactly at 18:00 must *not* pull in the 17:00 row, whose
    bucket ends precisely when the rider arrives. An inclusive test there made
    every on-the-hour pickup read one extra hour of weather, which is a real
    reading and a real hour earlier than the rider.
    """
    start = _aware(start)
    end = _aware(end)
    return [
        r for r in hourly
        if r["when_utc"] <= end and (r["when_utc"] + timedelta(hours=1)) > start
    ]


def _aware(dt: datetime) -> datetime:
    """UTC, whatever the caller passed.

    A naive datetime here would compare against aware ones and raise, or --
    worse -- be assumed to already be local. The orchestrator parses
    ``force_time`` from user input, so this is a real boundary.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

#: ~1 km cells. Coarser would serve a forecast from the wrong side of a
#: thunderstorm; finer would be a cache miss on every metre of rider movement.
_CELL = 0.01
_MEMO: dict[tuple[int, int, str], tuple[float, list[dict[str, Any]]]] = {}


def _cell(lat: float, lng: float) -> tuple[int, int]:
    return int(lat / _CELL), int(lng / _CELL)


async def get_hourly(
    lat: float, lng: float, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Hourly forecast for a cell, cached for ``weather_cache_ttl_s``.

    Raises ``httpx.HTTPError`` on failure. The caller decides what a missing
    forecast means; this function deliberately does not, because "no data" and
    "calm weather" must not look the same.
    """
    key = (*_cell(lat, lng), "v1")
    hit = _MEMO.get(key)
    if hit and (time.time() - hit[0]) < SETTINGS.weather_cache_ttl_s:
        return hit[1]

    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=SETTINGS.weather_timeout_s)
    try:
        resp = await client.get(build_url(lat, lng))
        resp.raise_for_status()
        rows = parse_hourly(resp.json())
    finally:
        if owns:
            await client.aclose()

    _MEMO[key] = (time.time(), rows)
    return rows


def clear_cache() -> None:
    _MEMO.clear()


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #

def sun_position(lat: float, lng: float, when_utc: datetime) -> SunPosition | None:
    """Solar elevation and azimuth via pvlib, or None if it cannot be computed.

    ``when_utc`` must be timezone-aware; pvlib is silently wrong with a naive
    datetime rather than raising, and a 4-hour error in sun position flips which
    side of a street is shaded. So it is checked here.
    """
    if when_utc.tzinfo is None:
        log.warning("sun_position called with a naive datetime; assuming UTC")
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    when_utc = when_utc.astimezone(timezone.utc)

    try:
        import pandas as pd
        import pvlib
    except ImportError:  # pragma: no cover - pvlib is in requirements.txt
        log.warning("pvlib unavailable; cannot compute sun position")
        return None

    try:
        sp = pvlib.solarposition.get_solarposition(
            pd.DatetimeIndex([when_utc]), lat, lng
        )
        return SunPosition(
            elevation_deg=round(float(sp["apparent_elevation"].iloc[0]), 2),
            azimuth_deg=round(float(sp["azimuth"].iloc[0]), 2),
        )
    except Exception:  # noqa: BLE001
        log.exception("pvlib failed to compute the sun position")
        return None


def fallback_weather(
    at: datetime, reason: str = "Weather service unavailable"
) -> WeatherReport:
    """The §6 fallback: a neutral report that says it is a fallback.

    ``source="fallback"`` and a populated ``reason`` are the whole point. A
    neutral report with no explanation would be indistinguishable from a real
    reading of calm weather, and the rider would be told there is no rain on the
    strength of a request that never completed.
    """
    return WeatherReport(
        condition=Condition.NEUTRAL,
        valid_at=_aware(at),
        source="fallback",
        overridden=False,
        reason=reason,
    )


def overridden_weather(
    at: datetime, condition: Condition, when: datetime
) -> WeatherReport:
    """A forced condition, for the demo.

    The numbers are absent on purpose -- there is no measured weather behind a
    forced condition, and inventing plausible values would let a screenshot of
    the demo be mistaken for a real reading. What is *not* absent is
    ``overridden: true`` and a reason, so a recorded run is always tellable apart
    from a real one.
    """
    return WeatherReport(
        condition=condition,
        valid_at=_aware(when),
        source="demo_override",
        overridden=True,
        reason=(
            f"Forced to {condition.value} for the demo; the real weather at this "
            "time was not used"
        ),
    )


# --------------------------------------------------------------------------- #
# assess: the one call the service makes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Assessment:
    """Conditions for one ride, plus the sentence that justifies the decision.

    Returned as a value rather than a tuple because the reason travels with the
    condition everywhere it goes -- into the ranking, into the response, and into
    the rider-facing message. Keeping them adjacent in a type is what stops a
    later refactor from passing the condition around and quietly dropping the
    explanation.
    """

    report: WeatherReport
    mode: Condition
    reason: str
    #: Hourly rows the decision was made from, kept for the dev-only inspect
    #: route. Empty when the condition was forced.
    window: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_fallback(self) -> bool:
        return self.report.source == "fallback"


def _report_from_window(
    samples: list[dict[str, Any]], mode: Condition, at: datetime, reason: str
) -> WeatherReport:
    """A real observation, built from the worst sample in the window.

    The numbers come from the sample that *decided* the mode, not the first or
    the last one, so a report saying "2.4 mm/h" is always backed by an hour that
    actually had 2.4 mm/h in it.
    """
    deciding = next(
        (s for s in samples if classify(s) is mode), samples[0] if samples else {}
    )
    return WeatherReport(
        condition=mode,
        precip_mm_h=deciding.get("precipitation", 0.0),
        cloud_cover_pct=(
            int(deciding["cloud_cover"])
            if deciding.get("cloud_cover") is not None else None
        ),
        uv_index=deciding.get("uv_index"),
        direct_radiation_w_m2=deciding.get("direct_radiation"),
        apparent_temperature_c=deciding.get("apparent_temperature"),
        temperature_c=deciding.get("temperature"),
        is_day=bool(deciding.get("is_day", True)),
        weather_code=(
            int(deciding["weather_code"])
            if deciding.get("weather_code") is not None else None
        ),
        valid_at=_aware(at),
        source="open-meteo",
        reason=reason,
    )


async def assess(
    lat: float,
    lng: float,
    pickup_time: datetime,
    wait_minutes: int,
    force_condition: Condition | None = None,
    client: httpx.AsyncClient | None = None,
) -> Assessment:
    """Conditions over the rider's whole wait, or an honest fallback.

    Never raises. §6 requires that a data problem must not reach the
    orchestrator as an error, and a weather outage is the single most likely
    data problem in this service -- so the failure mode is resolved here rather
    than at the route handler, where it would have to be resolved again.

    Order of precedence is deliberate: an explicit ``force_condition`` beats
    everything, including a live forecast that disagrees. The demo override
    exists precisely to demonstrate behaviour the real sky will not cooperate
    with, and a demo that silently reports the actual weather is not a demo.
    """
    when = _aware(pickup_time)

    if force_condition is not None:
        return Assessment(
            report=overridden_weather(when, force_condition, when),
            mode=force_condition,
            reason=f"Demo override: {force_condition.value}",
        )

    end = when + timedelta(minutes=max(0, wait_minutes))
    try:
        hourly = await get_hourly(lat, lng, client=client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("weather fetch failed (%s: %s)", type(exc).__name__, exc)
        return Assessment(
            report=fallback_weather(when, f"Weather unavailable ({type(exc).__name__})"),
            mode=Condition.NEUTRAL,
            reason="Could not reach the weather service",
        )

    window = samples_in_window(hourly, when, end)
    if not window:
        # Not a failure: the forecast simply does not reach this far out. Saying
        # so beats reporting calm weather we did not observe.
        return Assessment(
            report=fallback_weather(when, "Pickup time is outside the forecast"),
            mode=Condition.NEUTRAL,
            reason="The pickup time is outside the available forecast",
        )

    mode, reason = classify_window(window)
    return Assessment(
        report=_report_from_window(window, mode, when, reason),
        mode=mode,
        reason=reason,
        window=window,
    )
