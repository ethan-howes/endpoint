"""End-to-end smoke test against the *running* services.

    ./scripts/run_all.sh          # in one terminal
    python -m scripts.smoke_test  # in another

This is the script to run before a demo. It exercises the full §3 flow through
real HTTP against whatever is actually up, prints the rider-facing output, and
exits non-zero if the ride could not be completed. Its job is to turn "it works
on my machine" into something you can check in ten seconds, including the
partially-degraded cases that are otherwise only discovered live.

It deliberately makes no assertions about *quality* -- how many spots, how good
the ranking is, whether the message reads well. That is ``verify_demo_area``'s
job for S1. This checks plumbing: that the services answer, the ride completes,
and the response says something true about what happened.

The exception is ``check_s2_conditions``, which calls S2 directly with S1's spots
and asserts the *forcing* works. That is not a quality judgement, it is the one
property a forced demo rests on: if rain mode reports ``rain`` but ranks exactly
like neutral, the demo shows a rider being sent somewhere and nothing visibly
happening. That failure is invisible in a unit test and fatal in front of a
judge, and -- as it happens -- it is what ENDPOINT.md's own scoring table
produces, so it is worth re-checking against live data every single time.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from shared.config import SETTINGS

RIDER = {"lat": SETTINGS.demo_rider[0], "lng": SETTINGS.demo_rider[1]}

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def _fail(msg: str) -> None:
    print(_c(f"  FAIL  {msg}", RED))
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(_c(f"  ok    {msg}", GREEN))


def _note(msg: str) -> None:
    print(_c(f"  --    {msg}", YELLOW))


def get(url: str, **kw) -> httpx.Response:
    return httpx.get(url, timeout=10.0, **kw)


def post(url: str, body: dict, **kw) -> httpx.Response:
    return httpx.post(url, json=body, timeout=30.0, **kw)


def check_up(name: str, url: str) -> None:
    try:
        r = get(f"{url}/health")
    except httpx.HTTPError as exc:
        _fail(f"{name} is not answering on {url} ({type(exc).__name__}). "
              f"Start it with ./scripts/run_all.sh")
        return
    if r.status_code != 200:
        _fail(f"{name} answered /health with {r.status_code}")
    _ok(f"{name} up on {url}")


def run_scenario(orch: str, *, label: str, answer: dict, skip: bool = True) -> dict:
    """One ride, start to finish. Returns the final status payload."""
    print(f"\n{_c('scenario', DIM)}: {label}")

    started = time.time()
    r = post(f"{orch}/rides/request", {"rider_location": RIDER})
    if r.status_code != 200:
        _fail(f"/rides/request -> {r.status_code}: {r.text[:200]}")
    ride_id = r.json()["ride_id"]
    _ok(f"request accepted as {ride_id} in {time.time() - started:.2f}s")

    if not r.json().get("question"):
        _fail("no mobility question in the response -- the rider is stuck at step 1")

    t = time.time()
    r = post(f"{orch}/rides/{ride_id}/answer", answer)
    if r.status_code != 200:
        _fail(f"/answer -> {r.status_code}: {r.text[:200]}")
    plan = r.json()
    _ok(f"predictive phase in {time.time() - t:.1f}s (eta {plan.get('eta_s')}s)")

    spot = (plan.get("final_spot") or plan.get("predicted_spot") or {}).get("spot") or {}
    if not spot:
        _fail("no spot at all in the plan")
    _ok(f"chose {spot.get('spot_id')} "
        f"({spot.get('walk_distance_m', 0):.0f} m walk, "
        f"{spot.get('confidence')}, {spot.get('legality_basis')})")
    _ok(f"rider message: {plan.get('rider_message')}")

    for fb in plan.get("fallbacks_used") or []:
        _note(f"fallback: {fb}")

    if not skip:
        return plan

    t = time.time()
    r = post(f"{orch}/rides/{ride_id}/skip_to_arrival", {})
    if r.status_code != 200:
        _fail(f"/skip_to_arrival -> {r.status_code}: {r.text[:200]}")
    status = r.json()
    _ok(f"real-time phase in {time.time() - t:.1f}s -> {status['phase']}")
    _ok(f"final: {status['plan']['final_spot']['spot']['spot_id'] if status['plan'].get('final_spot') else None}"
        f" | car at {status['car_position']['lat']:.5f},{status['car_position']['lng']:.5f}"
        f" | {status['remaining_m']:.0f} m out")
    if status.get("degraded_note"):
        _note(f"degraded: {status['degraded_note']}")
    for fb in status["plan"].get("fallbacks_used") or []:
        _note(f"fallback: {fb}")

    return status


def fetch_spots(s1: str, radius: float = 150.0) -> list[dict]:
    """S1's candidate spots, straight from the service.

    Going to S1 directly rather than reading them off a ride keeps the S2 checks
    below independent of the orchestrator: if S2's ranking is wrong, the thing
    that reports it should be S2 and S1, not three services and a fusion rule.
    """
    r = post(f"{s1}/spots/legal", {"rider_location": RIDER, "radius_m": radius})
    if r.status_code != 200:
        _fail(f"S1 /spots/legal -> {r.status_code}: {r.text[:200]}")
    spots = r.json().get("spots") or []
    if not spots:
        _fail("S1 returned no spots -- the demo area is not seeded. "
              "Run: python -m scripts.prefetch_demo_area")
    return spots


def rank_once(s2: str, spots: list[dict], **overrides) -> dict:
    body = {
        "rider_location": RIDER,
        "spots": spots,
        "pickup_time": _iso_now(),
        "wait_minutes": 10,
    }
    body.update(overrides)
    r = post(f"{s2}/conditions/rank", body)
    if r.status_code != 200:
        _fail(f"S2 /conditions/rank -> {r.status_code}: {r.text[:200]}")
    return r.json()


def _top(body: dict) -> str | None:
    ranked = body.get("ranked") or []
    return ranked[0]["spot"]["spot_id"] if ranked else None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_s2_conditions(s2: str, spots: list[dict], *, radius: float) -> None:
    """S2 against the three conditions ENDPOINT.md section 6 defines.

    This is the part of the demo that unit tests cannot vouch for, because it
    depends on what is actually mapped in this particular block of Miami. A rain
    mode that reports `rain` while ranking identically to neutral is the failure
    this is here to catch -- and it is exactly the failure the doc's own scoring
    table produces, so it is worth re-checking against real data every time.
    """
    print(f"\n{_c('scenario', DIM)}: S2 conditions, directly, over {len(spots)} S1 spots")

    neutral = rank_once(s2, spots, force_condition="neutral")
    near = _top(neutral)
    if neutral.get("mode") != "neutral":
        _fail(f"forced neutral reported mode={neutral.get('mode')!r}")
    if neutral.get("fallbacks_used"):
        _fail(f"neutral mode should need no data at all, got "
              f"fallbacks: {neutral['fallbacks_used']}")
    _ok(f"neutral -> {near} (nearest spot, no weather ranking)")

    # --- rain ------------------------------------------------------------- #
    rain = rank_once(s2, spots, force_condition="rain")
    if rain.get("mode") != "rain":
        _fail(f"forced rain reported mode={rain.get('mode')!r}")
    if not rain.get("weather", {}).get("overridden"):
        _fail("forced rain did not set weather.overridden -- a forced run must be "
              "tellable from a real one")
    top = (rain.get("ranked") or [{}])[0]
    _ok(f"rain    -> {_top(rain)}  "
        f"({top.get('gap_m') if top.get('gap_m') is None else round(top['gap_m'], 1)} m "
        f"to cover, {top.get('cover_feature', {}).get('kind') if top.get('cover_feature') else 'none'})")
    _ok(f"reason:  {top.get('reason')}")

    with_cover = sum(1 for x in rain.get("ranked", []) if x.get("cover_feature"))
    if with_cover == 0:
        _note("no cover within range of any spot -- the rain demo has nothing to "
              "show here. Precache the demo area, or move the rider.")
    elif _top(rain) == near:
        # Not automatically wrong: the nearest spot may already be the covered
        # one. Say so precisely rather than crying failure.
        if top.get("cover_feature"):
            _ok(f"the nearest spot already has cover ({with_cover} covered), so "
                f"rain keeps it -- ranking moved nothing, correctly")
        else:
            _fail(f"rain mode left {with_cover} covered spots unchosen in favour of "
                  f"an uncovered {near}. The ranking is not using cover at all.")
    else:
        _ok(f"rain moved the pick {near} -> {_top(rain)} because of cover")

    # --- sun, and the side-flip that is section 6's headline demo --------- #
    morning = rank_once(s2, spots, force_condition="sun", force_time=_at_local(10))
    afternoon = rank_once(s2, spots, force_condition="sun", force_time=_at_local(16))
    for label, body in (("10:00", morning), ("16:00", afternoon)):
        if body.get("mode") != "sun":
            _fail(f"forced sun at {label} reported mode={body.get('mode')!r}")
    if morning.get("shade_source") is None:
        _note("no shade geometry in range -- the sun demo has nothing to show here")
    else:
        m_top = (morning.get("ranked") or [{}])[0]
        a_top = (afternoon.get("ranked") or [{}])[0]
        _ok(f"sun 10:00 -> {_top(morning)}  "
            f"({morning['sun']['elevation_deg']:.0f}deg up, az "
            f"{morning['sun']['azimuth_deg']:.0f}deg, "
            f"{(m_top.get('shade_fraction') or 0) * 100:.0f}% shade, "
            f"via {morning['shade_source']})")
        _ok(f"sun 16:00 -> {_top(afternoon)}  "
            f"({afternoon['sun']['elevation_deg']:.0f}deg up, az "
            f"{afternoon['sun']['azimuth_deg']:.0f}deg, "
            f"{(a_top.get('shade_fraction') or 0) * 100:.0f}% shade)")
        if _top(morning) == _top(afternoon):
            _note("same kerb won at 10:00 and 16:00 -- possible when the winning "
                  "spot is shaded at both hours, but check the azimuths reversed")
        else:
            _ok("the recommendation changed sides of the street, as section 6 "
                "promises")


def _at_local(hour: int) -> str:
    """Today at ``hour`` *local demo time*, as an ISO UTC string.

    Via the configured zone rather than a hardcoded +4, because the demo runs
    year-round and Miami is UTC-5 in winter. Hardcoding the offset would quietly
    shift the sun by an hour across the DST boundary, which is enough to move a
    shadow off a kerb and make the side-flip demo fail for the wrong reason.
    """
    local = datetime.now(ZoneInfo(SETTINGS.demo_tz)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return local.astimezone(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orchestrator", default=SETTINGS.s1_url.replace("8001", "8000"))
    ap.add_argument("--force-condition", default="rain",
                    help="demo override: rain | sun | neutral (default rain)")
    ap.add_argument("--radius", type=float, default=150.0)
    args = ap.parse_args()

    orch = args.orchestrator.rstrip("/")
    s1 = SETTINGS.s1_url.rstrip("/")
    s2 = SETTINGS.s2_url.rstrip("/")

    print(_c("Endpoint smoke test", "\033[1m"))
    print(f"  rider at {RIDER['lat']}, {RIDER['lng']}  (the Graham Center, FIU)\n")

    check_up("S1 legal spots", s1)
    try:
        s2_up = get(f"{s2}/health").status_code == 200
    except httpx.HTTPError:
        s2_up = False
    if s2_up:
        _ok(f"S2 weather/cover up on {s2}")
    else:
        _note(f"S2 is down on {s2} -- rides will run degraded (this is supported)")
    check_up("orchestrator", orch)

    run_scenario(orch, label="no mobility needs (nearest legal spot)",
                 answer={"mobility_needs": False})

    run_scenario(
        orch,
        label=f"mobility needs, forced {args.force_condition} (the product demo)",
        answer={"mobility_needs": True, "force_condition": args.force_condition},
    )

    if s2_up:
        check_s2_conditions(s2, fetch_spots(s1, args.radius), radius=args.radius)

    print(f"\n{_c('smoke test passed', GREEN)} -- the full flow works end to end.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
