"""S2: the weather-and-cover service (ENDPOINT.md section 6). Port 8002.

Three routes, one of which does everything: ``POST /conditions/rank``. The other
two exist so a map UI can draw the cover layer and show a weather badge without
running a full ranking, and so the forecast can be inspected during a demo
rehearsal without having to fake a ride.

Every route returns 200 with a populated ``fallbacks_used`` rather than an error.
That is not a stylistic choice -- ENDPOINT.md section 6 requires it, and it is
the difference between "the weather API was down" and "this rider gets no pickup".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Query

from shared.models import Condition, RankRequest

from . import service, weather

log = logging.getLogger("endpoint.s2")

app = FastAPI(
    title="Endpoint S2 -- weather and cover",
    version="0.1.0",
    description=(
        "Ranks legal pickup spots by the protection a rider will actually have "
        "when their car arrives: rain cover in rain, shade in strong sun, nearest "
        "otherwise. See ENDPOINT.md section 6."
    ),
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(
    lat: float | None = Query(None), lng: float | None = Query(None)
):
    """What is cached, so the demo can be checked before it starts.

    S1 has the equivalent, and it earns its keep the same way: a demo that fails
    because cover data was never fetched is a demo that fails in the first thirty
    seconds, in front of people.
    """
    return {"status": "ok", **service.cache_state(lat, lng)}


@app.post("/conditions/rank")
async def rank(req: RankRequest):
    """Weather at pickup time, then a ranking suited to it.

    The main route. Weather and ranking in one call because the ranking is not
    meaningful without the weather, and splitting them would mean the UI had to
    hold a stale forecast while the ranking waited on a second request.
    """
    result = await service.rank(req)
    log.info(
        "rank mode=%s spots=%d fallbacks=%d",
        result.mode.value, len(result.ranked), len(result.fallbacks_used),
    )
    return result


@app.get("/conditions/weather")
async def conditions_weather(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    at: datetime | None = Query(None, description="UTC; defaults to now"),
    force_condition: Condition | None = Query(None),
):
    """Weather only. For a UI badge, or for checking a forecast mid-rehearsal."""
    when = at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    assessment = await weather.assess(
        lat, lng, when.astimezone(timezone.utc), wait_minutes=0,
        force_condition=force_condition,
    )
    return {
        "weather": assessment.report,
        "mode": assessment.mode,
        "reason": assessment.reason,
        "sun": weather.sun_position(lat, lng, when),
    }


@app.get("/conditions/features")
async def conditions_features(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(150.0, gt=0, le=500),
):
    """Raw cover features near a point, for drawing the cover layer on the map.

    The doc asks for this so the UI can show where the cover *is* regardless of
    where the car is going. It is also the fastest way to see whether the demo
    area actually has any: an empty list here is the reason a rain ranking is
    flat, and it is much easier to diagnose from this route than from a ranking
    that simply looks uninteresting.
    """
    return await service.features(lat, lng, radius_m)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "s2_weather_cover",
        "docs": "/docs",
        "main_route": "POST /conditions/rank",
    }
