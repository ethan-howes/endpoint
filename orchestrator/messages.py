"""Rider-facing messages.

ENDPOINT.md section 7 gives a table of six situations with example wording. This
builds them. Two rules apply to every message, and they are the reason this is a
module rather than a few f-strings in the flow:

  * **Say what is happening and why, in the rider's terms.** "Wait under the
    canopy at the Graham Center entrance, 40 m away" is actionable. "Cover
    confidence 0.82" is not, and it is the kind of thing that leaks into a
    product when nobody decides otherwise.

  * **Never claim more certainty than the data has.** This is the whole point of
    §4.6's confidence tiers. If the pickup rests on an inferred curb tag rather
    than a regulation feed, the message says "should be fine to stop here" and
    not "it is legal to stop here". A rider who is told "legal" and finds a tow
    truck has been told something the system did not know.

Degradation is a message case too, not an exception. If S1 was unreachable the
message says the spot is the rider's own location, because that is what it is.
"""

from __future__ import annotations

from shared.models import Condition, Confidence, CurbAccess, RankedSpot, RidePhase, WeatherReport

from .ride import Ride


def _walk(spot: RankedSpot) -> str:
    m = spot.spot.walk_distance_m
    if m < 15:
        return "right where you are"
    if m < 100:
        return f"{m:.0f} m away"
    return f"{m / 1000.0:.1f} km away"


def _where(spot: RankedSpot) -> str:
    """The spot as the rider would refer to it."""
    name = spot.spot.street_name
    return f"on {name}" if name else "at the pickup point"


def _hedge(spot: RankedSpot) -> str:
    """One clause of calibrated confidence, or nothing if we are sure.

    Only ``unverified`` needs a hedge. ``likely`` from a real permissive tag is
    fine to state plainly, and hedging everything trains riders to ignore the
    hedges that matter -- which is the failure mode a confidence label is
    supposed to prevent.
    """
    if spot.confidence == Confidence.UNVERIFIED:
        return " (we're inferring this from the map, not from a curb regulation feed)"
    return ""


def build(ride: Ride) -> str:
    """The one-line explanation for wherever the ride currently stands."""
    spot = ride.resolved_spot
    if spot is None:
        return "Working out where your car can stop."

    phase = ride.phase
    condition = ride.weather.condition if ride.weather else Condition.NEUTRAL

    # --- degraded: the fallback spot at the rider's own location ---
    if spot.spot.source == "fallback":
        return (
            "We couldn't reach the road data service, so we're sending your car "
            "to your exact location. Please stay where you are and look for it there."
        )

    # --- the camera moved the car ---
    # Only when fusion actually switched spots. A camera that kept the prediction
    # adds its finding to the ordinary message below, and a camera that never ran
    # adds nothing: "we moved your pickup" and "the camera confirmed" are claims
    # about events, and they must not appear when the events did not happen.
    if phase == RidePhase.CONFIRMED and ride.final_spot is not None and ride.vision_switched:
        return (
            f"We moved your pickup to the spot {_where(spot)}, {_walk(spot)}. "
            f"{_sentence(ride.vision_reason)}{_access(spot)}"
        ).rstrip()

    # --- no mobility needs: the product is just a pickup, so say that ---
    if not ride.mobility_needs:
        return f"Your car will pick you up {_walk(spot)} {_where(spot)}."

    # --- the section 7 table ---
    if condition == Condition.RAIN:
        where = "under cover" if spot.cover_feature else _where(spot)
        lead = (
            f"It's raining when your car arrives, so wait {where}, {_walk(spot)}."
        )
    elif condition == Condition.SUN:
        where = "in the shade" if spot.cover_feature else _where(spot)
        lead = f"It's sunny when your car arrives, so wait {where}, {_walk(spot)}."
    else:
        lead = f"Your car will pick you up {_walk(spot)} {_where(spot)}."
    lead += _access(spot)

    if phase == RidePhase.PREDICTED:
        lead += f" {_preview(ride)}"
    elif phase == RidePhase.CONFIRMED and ride.vision_reason:
        lead += f" {_sentence(ride.vision_reason)}"

    return lead + _hedge(spot)


def _access(spot: RankedSpot) -> str:
    """What a rider with a walker or a cane meets on the way and at the car door.

    Only for riders who asked for comfort, and only what the data supports. A
    building is named only because the route uses it, and S2 only routes through
    buildings it believes are open at pickup time, so "while it's open" is true
    whichever hours applied. An unmapped kerb is said to be unconfirmed, never
    absent: around FIU mappers recorded ramps, not the kerbs without one.
    """
    parts: list[str] = []
    through = [n[len("through "):] for n in spot.route_notes if n.startswith("through ")]
    if through:
        parts.append(f"Your route goes through {' and '.join(through)} while it's open.")
    if any(n.startswith("leaves by the nearest side") for n in spot.route_notes):
        parts.append("Leave the building on the side nearest the car; "
                     "its doors there aren't on our map.")
    if "route includes steps" in spot.route_notes:
        parts.append("The walk includes steps.")
    if "crosses a road at a raised curb" in spot.route_notes:
        parts.append("One crossing on the way has a raised curb.")

    access = spot.spot.curb_access
    ramp = spot.spot.ramp_distance_m
    if access == CurbAccess.FLUSH:
        parts.append("The curb at the car is level with the road.")
    elif access == CurbAccess.LOWERED:
        if ramp is not None and ramp >= 3:
            parts.append(f"There's a curb ramp {ramp:.0f} m from the car.")
        else:
            parts.append("There's a curb ramp right by the car.")
    elif access == CurbAccess.RAISED:
        parts.append("There's a raised curb at the car.")
    else:
        parts.append("We couldn't confirm a curb ramp at this spot.")
    return " " + " ".join(parts)


def _sentence(text: str) -> str:
    """``text`` as a sentence: capitalised, with a full stop. Empty stays empty."""
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _preview(ride: Ride) -> str:
    """The forward-looking clause during the predictive phase."""
    eta = ride.eta_s
    mins = max(1, round(eta / 60.0))
    if ride.pending_confirmation:
        return (
            f"Getting there in about {mins} min. "
            "We'll check with you before moving you any farther."
        )
    if mins <= 1:
        return "Your car is almost there."
    return f"About {mins} min away."


def confirmation_question(ride: Ride) -> str:
    """The §7 detour prompt, used only when S2 asked for it.

    Shown when a meaningfully better spot exists but costs the rider more effort.
    The wording is explicit about the cost, because the rider is the only one who
    can weigh it -- a walker and a power wheelchair user will answer this
    differently, and the system cannot.
    """
    spot = ride.resolved_spot
    if spot is None:
        return "We found a covered spot a little farther away. Use it, or stay with the closest one?"
    return (
        f"A covered spot is a bit farther to walk. "
        f"Use it, or stay with the closest one at {_walk(spot)}?"
    )


def degraded_note(ride: Ride) -> str | None:
    """A short note when the ride is running on a fallback, for the UI to show.

    Separate from ``build`` because it is diagnostic, not something to read aloud
    at a rider. Returns None when nothing degraded.
    """
    if not ride.fallbacks_used:
        return None
    return "Some data was unavailable: " + "; ".join(ride.fallbacks_used[:3])
