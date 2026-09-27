"""The arithmetic both cover modules share, kept in one place and kept pure.

`rain_cover` and `sun_shade` differ in where their cover comes from and how they
measure a gap, but they agree on what a good pickup looks like: close to the
protection, short walk, and a feature we actually trust. Expressing that once
means a change to the walk penalty cannot land in the rain path and miss the sun
path -- which is the kind of drift that only shows up as "why does the sun demo
prefer the far spot".

No I/O, no settings lookups beyond the tunables, no shapely. Every function here
is a scalar in, scalar out, which is what makes the thresholds in
``shared/config.py`` testable on their own.
"""

from __future__ import annotations

from shared.config import SETTINGS
from shared.models import Confidence, RankedSpot


# --------------------------------------------------------------------------- #
# Distance factors
# --------------------------------------------------------------------------- #

def gap_factor(
    gap_m: float,
    max_gap_m: float,
    free_m: float | None = None,
) -> float:
    """How much a cover feature at ``gap_m`` is worth, 1.0 at the kerb to 0 at
    ``max_gap_m``. Straight from ENDPOINT.md section 6.

    The flat region below ``free_m`` is the point of it: a rider standing exactly
    on the boundary of an awning and one standing a metre inside are not
    meaningfully differently protected, and a linear ramp from zero would rank
    the metre-inside one worse for no reason the rider can perceive.
    """
    free_m = SETTINGS.cover_gap_free_m if free_m is None else free_m
    if gap_m <= free_m:
        return 1.0
    span = max_gap_m - free_m
    if span <= 0:
        return 1.0
    return max(0.0, 1.0 - (gap_m - free_m) / span)


def walk_factor(walk_m: float) -> float:
    """1.0 at the kerb, down to ``walk_penalty`` (0.5) at or past the reference.

    The penalty deliberately never reaches 1.0. Walk distance is the tie-breaker
    between two protected spots, not a disqualifier -- ENDPOINT.md section 6
    scores it additively alongside cover, and a factor that could zero a score
    would make a long walk to real cover look worse than no cover at all.
    """
    ref = SETTINGS.walk_reference_m
    ratio = min(max(walk_m, 0.0) / ref, 1.0) if ref > 0 else 0.0
    return 1.0 - SETTINGS.walk_penalty * ratio


# --------------------------------------------------------------------------- #
# Trust weights
# --------------------------------------------------------------------------- #

def conf_weight(confidence: Confidence) -> float:
    """How much a cover feature's own provenance is worth.

    A *relative* discount, not an absolute score: the tier OSM data can reach is
    the 1.0 baseline and better provenance scales above it. ENDPOINT.md line 512
    had this as an absolute table whose ``unverified`` row was 0.3, which is the
    same number it separately awards a spot with no cover at all (line 517) --
    so every OSM-sourced feature, and every spot with no feature, scored
    identically and the rain ranking collapsed to walk distance. See
    ``scripts/show_rain_score_defect.py`` for the arithmetic.

    What survives from the doc is the thing it was reaching for: provenance is
    worth more, and it starts being worth more the moment a real feed exists
    rather than in the demo area.
    """
    table = SETTINGS.cover_confidence_weight
    return float(table.get(confidence.value, 1.0))


def source_weight(source: str) -> float:
    """How much to trust a shade *measurement*, by where it came from.

    Relative, for the same reason as ``conf_weight``: ``osm_geometry`` is the
    only source this service can produce, so scoring it as a discount capped
    every sun score at 0.6 and flattened the lower half of the persistence
    range onto the no-shade floor.

    ``osm_geometry`` is still a model -- a building with no height tag is assumed
    6 m tall, and in the demo area only 3 of 52 buildings carry
    ``building:levels`` -- so a mis-modelled building casts a shadow in the wrong
    place and confidently. That is why ``cover_feature`` outranks it: somebody
    standing in an arcade is better evidence than a sweep of a guessed footprint.
    """
    table = SETTINGS.shade_source_weight
    return float(table.get(source, 1.0))


# --------------------------------------------------------------------------- #
# Combination
# --------------------------------------------------------------------------- #

def covered_score(
    gap_m: float,
    confidence: Confidence,
    walk_m: float,
    max_gap_m: float,
) -> float:
    """``conf_weight * gap_factor * walk_factor``, with a floor.

    The product is the doc's formula, and it is the right shape: cover quality,
    proximity to it, and walk length are three independent ways to be a bad
    choice, so a spot has to do well on all three.

    The floor is a correction. ENDPOINT.md's ``gap_factor`` reaches 0 at
    ``max_gap_m``, and the same section separately awards ``0.3 * walk_factor``
    to spots with *no cover within* ``max_gap_m``. Those two rules cross: a spot
    with a shelter 4.9 m away scores ~0, while a spot with nothing at all scores
    0.3, so walking slightly closer to real cover makes a spot rank worse. A
    ranking that inverts on "is there shelter or not" is not a rounding error, it
    is the ranking pointing the rider away from the awning. Flooring the product
    costs one comparison and removes the inversion.

    With ``conf_weight`` rebased to 1.0 for the tier OSM can reach, the floor sits
    below the entire covered range and only decides the band where cover is too
    far away to be worth anything -- which is the band where "as good as standing
    in the open" is the correct answer. The rankings break ties on ``gap_m``
    within that band, so approaching real cover still orders correctly.
    """
    return max(
        conf_weight(confidence) * gap_factor(gap_m, max_gap_m) * walk_factor(walk_m),
        no_cover_score(walk_m),
    )


def no_cover_score(walk_m: float) -> float:
    """What a spot with nothing overhead is worth: ``0.3 * walk_factor``.

    Not zero. An uncovered spot on the kerb beside the rider is still the right
    answer when there is no covered spot nearby, and scoring it 0 would let a
    covered spot 200 m away win a ride where the nearest option is standing right
    there.
    """
    return SETTINGS.no_cover_score * walk_factor(walk_m)


def needs_detour_confirmation(best_walk_m: float, nearest_walk_m: float) -> bool:
    """Should the rider be asked whether the extra walk is worth it?

    ENDPOINT.md section 6 line 519: past 120 m of extra walking, ask. The
    threshold is a product decision about a rider's energy, not a geometric one,
    so it belongs in config next to every other number someone will argue about.
    """
    return (best_walk_m - nearest_walk_m) > SETTINGS.detour_confirm_m


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def rank_key(r: RankedSpot) -> tuple[float, float, float, str]:
    """Best-first sort key, shared by the rain and sun paths.

    Score first, then **gap**, then walk, then ``spot_id``. The gap term is the
    one that is easy to leave out: inside the no-cover floor band every score is
    equal by construction, and without it a spot with a shelter 4 m away would tie
    with a spot 300 m from one. The floor exists to stop the ranking inverting on
    "is there cover at all"; the gap tiebreak is what keeps it still preferring
    real cover *within* the band where the floor is what decides.

    ``spot_id`` last is what makes the output reproducible. Two spots with
    identical geometry, gap and walk are otherwise returned in whatever order the
    spatial index happened to yield, and a demo that reorders its own
    recommendations between runs reads as broken.
    """
    return (-r.score, r.gap_m if r.gap_m is not None else float("inf"),
            r.spot.walk_distance_m, r.spot.spot_id)
