"""Demonstrate the arithmetic defect in ENDPOINT.md line 512-517.

The doc's two rain-scoring rules use two numbers that cancel:

    conf_weight["unverified"] = 0.3      (line 512)
    no-cover score  = 0.3 * walk_factor  (line 517)

So a spot with a shelter directly overhead scores

    0.3 * gap_factor(0) * walk_factor = 0.3 * 1.0 * walk_factor

which is *exactly* the score of a spot with no cover within `max_gap_m` at all.
Every OSM cover feature is `unverified` -- ENDPOINT.md line 506 puts `verified`
behind city open data and line 507 puts `likely` behind Google Places, and
neither exists for the demo area -- so in the demo area the entire rain ranking
degenerates to walk distance.

Run this to see the numbers rather than take the claim on trust.
"""

from __future__ import annotations

import sys

CONF = {"verified": 1.0, "likely": 0.7, "detected": 0.8, "unverified": 0.3}
FREE_M = 1.5


def gap_factor(gap_m: float, max_gap_m: float) -> float:
    if gap_m <= FREE_M:
        return 1.0
    return max(0.0, 1.0 - (gap_m - FREE_M) / (max_gap_m - FREE_M))


def walk_factor(walk_m: float) -> float:
    return 1.0 - 0.5 * min(walk_m / 300.0, 1.0)


def main() -> int:
    max_gap = 5.0
    walk = 50.0
    wf = walk_factor(walk)

    print("ENDPOINT.md as written, sun of the document removed.")
    print("Every OSM cover feature is `unverified`, so that is the only row in play.\n")
    print(f"  walk = {walk:g} m -> walk_factor = {wf:.3f}\n")
    print(f"  {'gap to shelter':>16} | {'gap_factor':>10} | {'score':>7}")
    print("  " + "-" * 40)
    for gap in (0.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0):
        gf = gap_factor(gap, max_gap)
        score = CONF["unverified"] * gf * wf
        print(f"  {gap:>13.1f} m | {gf:>10.3f} | {score:>7.3f}")
    print("  " + "-" * 40)
    print(f"  {'no cover at all':>16} | {'-':>10} | {0.3 * wf:>7.3f}")
    print()
    print("  A shelter 4 m away and no shelter at all are the same number.")
    print("  The ranking therefore sorts by walk distance and rain cover")
    print("  does not influence the answer at all.\n")

    print("Also inverted, independent of the cancellation:")
    print(f"  covered at exactly max_gap_m ({max_gap:g} m): "
          f"{CONF['unverified'] * gap_factor(max_gap, max_gap) * wf:.3f}")
    print(f"  no cover within max_gap_m:                  {0.3 * wf:.3f}")
    print("  Walking towards the awning lowers the score.\n")

    # ------------------------------------------------------------------ #
    # The fix
    # ------------------------------------------------------------------ #
    from shared.config import SETTINGS

    conf = SETTINGS.cover_confidence_weight
    print("=" * 60)
    print("The fix: relative weights, reachable tier as the 1.0 baseline.\n")
    print(f"  cover_confidence_weight = {conf}")
    print(f"  shade_source_weight     = {SETTINGS.shade_source_weight}")
    print(f"  no_cover_score          = {SETTINGS.no_cover_score}\n")

    def fixed(gap: float) -> float:
        raw = conf["unverified"] * gap_factor(gap, max_gap) * wf
        return max(raw, SETTINGS.no_cover_score * wf)

    print(f"  {'gap to shelter':>16} | {'was':>7} | {'now':>7} | floor?")
    print("  " + "-" * 46)
    for gap in (0.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0):
        was = CONF["unverified"] * gap_factor(gap, max_gap) * wf
        now = fixed(gap)
        floored = abs(now - SETTINGS.no_cover_score * wf) < 1e-9
        print(f"  {gap:>13.1f} m | {was:>7.3f} | {now:>7.3f} | {'yes' if floored else '-'}")

    floor = SETTINGS.no_cover_score * wf
    print("  " + "-" * 46)
    print(f"  {'no cover at all':>16} | {floor:>7.3f} | {floor:>7.3f} | -")
    print()
    vals = [fixed(g) for g in (0.0, 1.5, 2.0, 3.0, 4.0, 4.5)]
    print(f"  monotonically decreasing: {vals == sorted(vals, reverse=True)}")
    print(f"  adjacent beat no cover:   {vals[0] > floor}")
    print(f"  floor below every covered value: "
          f"{min(v for v in vals) >= floor}")
    print()
    print("  Cover now discriminates; the floor only decides the band where")
    print("  cover is too far to be worth anything, and the ranking breaks")
    print("  ties on gap inside that band. See scoring.rank_key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
