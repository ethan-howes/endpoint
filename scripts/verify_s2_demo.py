#!/usr/bin/env python
"""Verify ENDPOINT.md section 6 S2's headline scenarios against the real demo area.

``scripts/verify_demo_area.py`` does the equivalent job for S1: it walks the
answer and asks whether it is genuinely correct, independently of the code that
produced it. S2 had no equivalent, and the gap mattered. Every S2 scenario passed
its unit tests, the API tests were green, and the rain demo was dead -- the
service answered 200, ranked 30 spots, and reported "No cover found nearby" for
all of them because not one candidate kerb was within ``rain_max_gap_m`` of any
cover in the box. Green tests, working code, nothing to demonstrate.

That class of failure is invisible to a test suite, because what is wrong is not
the logic but the *data the demo runs on*. So this script checks the data:

1. Cover exists near the rider's kerbs, not merely somewhere in the box.
2. Rain mode actually re-orders -- the covered answer is not the walk answer.
3. The sun demo's side-flip is real: 10:00 and 16:00 disagree.
4. Every scenario ran without a fallback, so what is measured is the feature and
   not its degradation path.

Independent of the ranking code, and deliberately so: it reads ``gap_m`` and
``spot_id`` off the response and compares orders, rather than re-deriving scores
and checking the arithmetic. The arithmetic has its own tests; what is unverified
is whether the demo can show the thing at all.

Usage::

    python -m scripts.verify_s2_demo
    python -m scripts.verify_s2_demo --at 25.7584,-80.3725
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import SETTINGS  # noqa: E402
from shared.models import Condition, LatLng, LegalSpotsRequest, RankRequest  # noqa: E402
from services.s1_legal_spots.service import legal_spots  # noqa: E402
from services.s2_weather_cover.service import clear_caches, rank  # noqa: E402


@dataclass
class Outcome:
    ok: bool
    label: str
    detail: str


class Report:
    def __init__(self, rider: LatLng, radius: float) -> None:
        self.rider = rider
        self.radius = radius
        self.outcomes: list[Outcome] = []

    def add(self, ok: bool, label: str, detail: str) -> None:
        self.outcomes.append(Outcome(ok, label, detail))

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)

    def render(self) -> None:
        s, w, n, e = SETTINGS.demo_bbox
        print(f"demo bbox     {s},{w},{n},{e}")
        print(f"demo rider    ({self.rider.lat:.5f}, {self.rider.lng:.5f})"
              f"  ({SETTINGS.demo_tz})")
        print(f"search radius {self.radius:g} m")
        print()
        for o in self.outcomes:
            print(f"  {'PASS' if o.ok else 'FAIL'}  {o.label}")
            print(f"        {o.detail}")
        print()
        if self.failed:
            print(f"{self.failed} CHECK(S) FAILED")
        else:
            print("ALL CHECKS PASSED")


def _describe(r) -> str:
    top = r.ranked[0] if r.ranked else None
    if top is None:
        return "no spots returned"
    gap = "-" if top.gap_m is None else f"{top.gap_m:.1f} m"
    kind = top.cover_feature.kind if top.cover_feature else "none"
    return (
        f"top={top.spot.spot_id} walk={top.spot.walk_distance_m:.0f} m "
        f"gap={gap} cover={kind}"
    )


async def evaluate(rider: LatLng, radius: float) -> Report:
    rep = Report(rider, radius)

    spots = (await legal_spots(
        LegalSpotsRequest(rider_location=rider, radius_m=radius)
    )).spots
    rep.add(len(spots) > 0, "S1 returns candidates",
            f"{len(spots)} legal spots, nearest "
            f"{min((s.walk_distance_m for s in spots), default=float('nan')):.0f} m")
    if not spots:
        return rep

    now = datetime.now(timezone.utc)

    async def run(**kw):
        clear_caches()
        return await rank(RankRequest(
            rider_location=rider, spots=spots, pickup_time=now,
            wait_minutes=10, **kw,
        ))

    neutral = await run(force_condition=Condition.NEUTRAL)
    rain = await run(force_condition=Condition.RAIN)
    # 14:00 and 20:00 UTC are 10:00 and 16:00 in America/New_York (EDT). pvlib
    # positions the sun from the instant, so the ranking sees the real geometry
    # for those local times -- not a hard-coded "which side is shaded" answer.
    sun_am = await run(
        force_condition=Condition.SUN,
        force_time=now.replace(hour=14, minute=0, second=0, microsecond=0),
    )
    sun_pm = await run(
        force_condition=Condition.SUN,
        force_time=now.replace(hour=20, minute=0, second=0, microsecond=0),
    )

    rep.add(not neutral.fallbacks_used, "neutral needs no fallback",
            f"fallbacks={neutral.fallbacks_used or 'none'}")

    # --- 1. is cover actually near the kerbs? -----------------------------
    covered = [x for x in rain.ranked if x.cover_feature]
    best = min((x.gap_m for x in rain.ranked if x.gap_m is not None), default=None)
    rep.add(
        bool(covered),
        "cover is reachable from a candidate kerb",
        f"{len(covered)} of {len(rain.ranked)} spots have cover within "
        f"{SETTINGS.rain_max_gap_m:g} m"
        + (f", closest {best:.1f} m" if best is not None else "")
        + (
            f"  [{sorted({x.cover_feature.kind for x in covered})}]"
            if covered else "  <-- rain mode has nothing to rank"
        ),
    )

    # --- 2. does rain re-order? -------------------------------------------
    n_top = neutral.ranked[0].spot.spot_id if neutral.ranked else None
    r_top = rain.ranked[0].spot.spot_id if rain.ranked else None
    rep.add(
        bool(covered) and n_top != r_top,
        "rain changes the answer",
        f"neutral -> {n_top} | rain -> {r_top}"
        + ("" if n_top != r_top else "  <-- identical, nothing was re-ranked"),
    )

    rep.add(
        not rain.fallbacks_used, "rain needs no fallback",
        f"fallbacks={rain.fallbacks_used or 'none'}",
    )

    # --- 3. is the sun side-flip real? ------------------------------------
    a_top = sun_am.ranked[0].spot.spot_id if sun_am.ranked else None
    p_top = sun_pm.ranked[0].spot.spot_id if sun_pm.ranked else None
    for label, r in (("10:00", sun_am), ("16:00", sun_pm)):
        az = r.sun.azimuth_deg if r.sun else float("nan")
        el = r.sun.elevation_deg if r.sun else float("nan")
        rep.add(
            r.shade_source is not None,
            f"sun {label} has real shade geometry",
            f"{_describe(r)}  sun={el:.0f}deg az {az:.0f} "
            f"shade_source={getattr(r.shade_source, 'value', r.shade_source)}"
            + ("" if not r.fallbacks_used else f"  fallbacks={r.fallbacks_used}"),
        )
    rep.add(
        a_top != p_top,
        "the sun demo's side-flip changes the answer",
        f"10:00 -> {a_top} | 16:00 -> {p_top}"
        + ("" if a_top != p_top else "  <-- same spot at both times"),
    )

    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", action="append", help="lat,lng (repeatable); default: demo_rider")
    ap.add_argument("--radius", type=float, default=150.0)
    args = ap.parse_args()

    if args.at:
        riders = [LatLng(lat=float(p.split(",")[0]), lng=float(p.split(",")[1]))
                  for p in args.at]
    else:
        riders = [LatLng(lat=SETTINGS.demo_rider[0], lng=SETTINGS.demo_rider[1])]

    failed = 0
    for i, rider in enumerate(riders):
        if i:
            print()
        report = asyncio.run(evaluate(rider, args.radius))
        report.render()
        failed += report.failed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
