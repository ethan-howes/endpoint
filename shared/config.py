"""Thresholds, buffers, weights, and tunables: the single source of truth (ENDPOINT.md section 8).

Everything a judge might ask "where did that number come from?" should live in this
file, not scattered through service code. ENDPOINT.md section 6 S1 notes that the
exclusion buffers are "modeled on common US parking rules; verify them for your demo
city" -- so they are all collected here for exactly that reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OVERPASS_CACHE_DIR = CACHE_DIR / "overpass"

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default

@dataclass(frozen=True)
class Settings:
    """Process-level settings, read from the environment at import time."""

    mock: bool = field(default_factory=lambda: _env_bool("MOCK", False))

    # Demo area: south, west, north, east. FIU Miami, centered on the Ernest R.
    # Graham Center (25.756918, -80.372182). Chosen because the campus has real
    # overhead cover for the rain scenario: 77 `tunnel=building_passage` ways and
    # 59 `covered=yes` ways within this box, versus 2 `covered` ways in the
    # Brickell box from ENDPOINT.md's example.
    demo_bbox: tuple[float, float, float, float] = (25.7533, -80.3762, 25.7605, -80.3682)
    demo_tz: str = "America/New_York"

    #: Where the rider starts in the demo.
    #:
    #: Was the Graham Center entrance (25.756918, -80.372182), chosen on the
    #: strength of the cover counts in ``demo_bbox`` -- 427 building passages, 100
    #: covered walkways, 31 shelters. Those counts turned out to be the wrong
    #: thing to optimise, and choosing by them made S2's rain scenario
    #: impossible: cover exists in the box, but not *next to the kerbs S1 picks*.
    #:
    #: Measured at the old spot: the nearest of 30 candidate kerbs sat 47.2 m from
    #: the nearest cover, the rider 74.1 m from it, and *zero* of the 30 were
    #: within 40 m. The cover is spread +/-535 m east-west, so this was never a
    #: corner-of-the-box problem -- it is that the campus `highway=service` roads
    #: S1 selects run between the buildings rather than under the arcades between
    #: them. High cover counts and cover-adjacent kerbs are different properties,
    #: and only the second one is what rain mode reads.
    #:
    #: This position was picked by scanning 20 points across the bbox and ranking
    #: them by the mean gap over a rider's five best kerbs (the quantity that
    #: decides whether several candidates qualify, not just the luckiest one):
    #:
    #:     position                top-5 mean   best   spots
    #:     25.75840, -80.37250        12.0 m    5.8 m    26
    #:     25.75560, -80.37250        13.6 m   11.5 m    30
    #:     25.75840, -80.37390        13.8 m   10.5 m    30
    #:     25.756918, -80.372182  (old) 51.3 m   47.2 m    30
    #:
    #: No position in the box reaches a 5.8 m *best* gap, which is why
    #: ``rain_max_gap_m`` moved to 15 alongside this. The two changes only work
    #: together: at 15 m this spot has several qualifying kerbs, and at this spot
    #: with a 5 m threshold it would still qualify none.
    demo_rider: tuple[float, float] = (25.7584, -80.3725)

    # Which side of a two-way road a vehicle travels on. "right" = right-hand
    # traffic (US, and the Miami demo area). "left" for right-hand-drive regions.
    traffic_side: str = "right"

    # Service URLs used by the orchestrator (ENDPOINT.md section 8).
    s1_url: str = "http://localhost:8001"
    s2_url: str = "http://localhost:8002"
    s3_url: str = "http://localhost:8003"

    # --- orchestrator: ENDPOINT.md section 4.5 timeouts ---
    # These are the CALLER's budgets. S1 must therefore serve from cache, since a
    # cold Overpass query is slower than the 3s it is allowed.
    timeout_s1_s: float = 3.0
    timeout_s2_s: float = 6.0
    timeout_s3_s: float = 8.0

    # --- orchestrator: car simulation ---
    #: Where the car starts when the request does not say. ENDPOINT.md says
    #: "~1.5 km away"; this is a fixed point west of the Graham Center so the
    #: demo is reproducible instead of dependent on where the rider is.
    demo_car_start: tuple[float, float] = (25.7625, -80.3850)

    #: Simulated seconds per real second. ENDPOINT.md suggests 5x so a 4-minute
    #: approach takes ~50s. A demo you have to sit through for 4 minutes is a
    #: demo the audience watches you wait through.
    sim_speedup: float = 5.0

    #: Ticks the simulated car once per this many real seconds. The UI polls at
    #: 1s, so 1s is the finest granularity worth simulating.
    sim_tick_s: float = 1.0

    # --- orchestrator: real-time phase trigger (ENDPOINT.md section 7) ---
    approach_distance_m: float = 150.0
    approach_eta_s: float = 60.0

    #: How far from the predicted spot an alternative may be and still be worth
    #: showing the rider. ENDPOINT.md section 7 uses 40 m and takes the first 2.
    alternative_radius_m: float = 40.0
    alternative_limit: int = 2

    # --- orchestrator: routing ---
    #: OSRM public demo server (ENDPOINT.md section 7). Intended for light use,
    #: hence the cache.
    osrm_url: str = "https://router.project-osrm.org/route/v1/driving"
    route_cache_ttl_s: float = 3600.0

    # ----------------------------------------------------------------- #
    # S2: weather and cover (ENDPOINT.md section 6, modules 1-3)
    # ----------------------------------------------------------------- #

    # --- weather classification thresholds ---
    # These live here, not in weather.py, because they are policy the product
    # might want to argue with ("is 0.2 mm/h really rain?") and every one of them
    # is a number someone will eventually want to change without reading the
    # classification logic.

    #: At or above this, it is raining. 0.2 mm/h is light enough that a rider
    #: with mobility needs would still rather move -- the point of the product
    #: is not to send someone into drizzle.
    rain_precip_mm_h: float = 0.2

    #: "Too sunny" needs the sun to be genuinely out AND the exposure to be
    #: worth avoiding. Requiring direct_radiation stops us recommending shade at
    #: 7am under overcast skies.
    sun_radiation_w_m2: float = 300.0
    sun_uv_index: float = 6.0
    sun_apparent_temp_c: float = 32.0

    #: Used only when direct_radiation is missing from the response.
    sun_cloud_cover_pct: float = 40.0

    #: Below this the sun is too low for shade to matter. Ranking on shade at
    #: sunset would send a rider walking past perfectly good cover for nothing.
    sun_min_elevation_deg: float = 5.0

    #: Open-Meteo forecast horizon. The demo is "now", so this is generous.
    weather_forecast_days: int = 2

    #: Per ~1 km cell, per ENDPOINT.md. Five minutes is short enough that a
    #: rerun of a demo tells you what is happening now, and long enough that a
    #: polling UI does not hammer a free API.
    weather_cache_ttl_s: float = 300.0

    #: S2 gets 6s from the orchestrator. 4s to Open-Meteo leaves room for the
    #: classification and the cover query.
    weather_timeout_s: float = 4.0

    # --- cover feature scoring (ENDPOINT.md section 6, module 2) ---
    #: Within this, a spot counts as directly under the awning.
    cover_gap_free_m: float = 1.5

    #: Beyond this there is no cover at all. ENDPOINT.md section 6 line 429 says
    #: 5, and an earlier version here believed that literally -- on the grounds
    #: that rain cover you have to walk around a corner to reach is not cover for a
    #: rider waiting for a car. The reasoning is sound and the number is unusable:
    #: measured over 20 rider positions inside the demo bbox, the closest any
    #: legal kerb comes to any cover is 5.8 m, and the mean across a rider's five
    #: best kerbs is 12 m. At 5, rain mode found cover for *zero* of 30 candidate
    #: spots at every location tried, answered "No cover found nearby" for all of
    #: them, and fell back to plain walk-distance ranking -- so section 6's
    #: headline scenario demonstrated nothing at all.
    #:
    #: 15 is section 6's own value for the *sun* module (line 626), reused rather
    #: than invented: a doorway's worth of extra walk to get out of the rain, and
    #: short enough that a shelter across a car park still reads as not worth it.
    #:
    #: The hard cut is kept, as section 6 specifies -- a kerb just outside this
    #: scores as though there were no cover. The discontinuity is real and is a
    #: deliberate simplification, but ``gap_m`` is reported on every ranked spot
    #: regardless, so the UI can show "awning 6 m away" without the ranking
    #: claiming it mattered.
    rain_max_gap_m: float = 15.0

    #: Unchanged from the doc, and for the same reason as ``rain_max_gap_m`` it is
    #: the number that actually matches the data: sun shade is a continuous thing
    #: (a kerb is lit or it is not) rather than a doorway, so 15 m was never at
    #: risk of being unreachable.
    sun_max_gap_m: float = 15.0

    #: Reference walk distance for the walk penalty. At 300 m the penalty is at
    #: its 50% cap: beyond that, "close by" stops being true.
    walk_reference_m: float = 300.0
    walk_penalty: float = 0.5

    #: How much a cover feature's own provenance is worth, as a *relative*
    #: discount on the tier OSM can actually reach.
    #:
    #: ENDPOINT.md line 512 gives an absolute table --
    #: `{"verified": 1.0, ..., "unverified": 0.3}` -- and line 517 separately
    #: awards `0.3 * walk_factor` to a spot with no cover. Those two 0.3s
    #: cancel: a spot with a shelter overhead scores `0.3 * 1.0 * walk`, which
    #: is exactly the score of a spot with no shelter at all. Since every OSM
    #: cover feature is `unverified` (the doc puts `verified` behind city open
    #: data and `likely` behind Google Places, and neither exists for the demo
    #: area), the rain ranking degenerated to plain walk distance and cover did
    #: not influence the answer at all. `scripts/show_rain_score_defect.py`
    #: prints the arithmetic.
    #:
    #: So the baseline is 1.0 and better provenance scales above it. The
    #: ordering the doc was reaching for survives; the cancellation does not.
    #: default_factory, not a literal: a dict default is rejected by dataclasses
    #: even on a frozen instance, and a shared mutable class attribute would let
    #: one caller's edit leak into every later request.
    cover_confidence_weight: dict = field(default_factory=lambda: {
        "unverified": 1.0, "detected": 1.1, "likely": 1.3, "verified": 1.6,
    })

    #: How much to trust a shade *measurement*, by where it came from. Same
    #: relative-baseline reasoning: `osm_geometry` is the only source this
    #: service can produce, so making it a discount capped every sun score at
    #: 0.6 and left the bottom half of the persistence range flattened onto the
    #: no-shade floor. A mapped cover feature still outranks a modelled shadow.
    shade_source_weight: dict = field(default_factory=lambda: {
        "osm_geometry": 1.0, "google_solar": 1.15, "cover_feature": 1.3,
    })

    #: A spot with no cover inside rain_max_gap_m scores this fraction of its
    #: walk score. It is a *floor*, not a competitor: it sits below every covered
    #: score in the table above, so it only decides the case ENDPOINT.md line 517
    #: was reaching for -- a kerb with nothing overhead is still the right answer
    #: when nothing better is close -- and never inverts the covered range. The
    #: rankings break ties on gap within this band, so approaching real cover
    #: still orders correctly even where the floor is what decides.
    no_cover_score: float = 0.3

    #: ENDPOINT.md line 519: past this much extra walking, ask the rider.
    detour_confirm_m: float = 120.0

    # --- cover query (ENDPOINT.md section 6, module 2) ---
    #: `way["building"="roof"]` from the doc's query is dropped: `building=*` is
    #: the real tag, so `building=roof` matches essentially nothing in OSM. It
    #: reads like coverage and provides none.
    #:
    #: `man_made=awning|canopy` is added because the doc's query has no awning
    #: selector at all, yet `awning` is the kind its own definition-of-done names
    #: ("the top spot sits next to a mapped awning"). Measured in the demo box:
    #: zero of both, so this costs nothing here and is the thing that would make
    #: that criterion pass in a city that does map awnings.
    #:
    #: The bbox is repeated on *every* statement rather than applied once to the
    #: union with a trailing `)(bbox);`. Overpass QL rejects that form outright --
    #: `parse error: ';' expected - '(' found` -- and it fails as a clean HTTP 400
    #: from every mirror, which looks exactly like a network problem and is not
    #: one. Verified against overpass-api.de: the group form 400s, the
    #: per-statement form returns 200 with 97 elements for the demo box.
    cover_query: str = """
[out:json][timeout:60];
(
  way["highway"]["covered"~"^(yes|arcade)$"]({bbox});
  nwr["amenity"="shelter"]({bbox});
  node["highway"="bus_stop"]["shelter"="yes"]({bbox});
  nwr["public_transport"="platform"]["covered"="yes"]({bbox});
  nwr["man_made"~"^(awning|canopy)$"]({bbox});
  way["tunnel"="building_passage"]({bbox});
);
out body geom;
"""

    #: Buildings and trees, for the OSM shadow-geometry fallback. Separate from
    #: the cover query because these are large elements that bloat a response
    #: and are only needed in `sun` mode.
    #:
    #: `nwr` rather than `way` for buildings: campus footprints are frequently
    #: mapped as multipolygon *relations*, and a `way`-only query silently drops
    #: every one of them. Per-statement bbox for the reason given on
    #: `cover_query`.
    #:
    #: `natural=tree` is restricted to nodes on purpose. `tree_row` is tagged on a
    #: *way* -- a line of trees, not a point -- and `sun_shade.tree_shadow`
    #: requires a single trunk point, so a row cannot be consumed by the current
    #: shadow model. An earlier version selected it and dropped every result: 0
    #: `tree_row` in the demo box, and `parse_shade` would have discarded any that
    #: appeared. A selector whose results are always thrown away is worse than no
    #: selector, because the query implies the capability is there. Supporting
    #: rows properly means casting a disc at intervals along the line, which is
    #: a real feature and not a stub.
    shade_query: str = """
[out:json][timeout:90];
(
  nwr["building"]({bbox});
  node["natural"="tree"]({bbox});
);
out body geom;
"""

    #: A node-sourced cover feature (a bus-stop shelter) has no area, so distance
    #: to it would measure to its centre and over-report the gap by half the
    #: shelter's length. Sized to a single-person shelter, not a bus station.
    node_cover_radius_m: float = 2.0

    #: Building height assumptions when a footprint carries no height tag. The
    #: doc's defaults; a campus building with 3 floors is ~10 m, so 6 m is
    #: deliberately conservative and under-casts rather than over-casts shade.
    default_building_height_m: float = 6.0
    default_floor_height_m: float = 3.0
    default_tree_height_m: float = 8.0
    default_tree_crown_m: float = 3.0

    #: Radius of the disc used to measure shade_fraction around a wait point.
    shade_probe_radius_m: float = 2.0

    # Overpass public instances, tried in order. The main instance returns 504
    # under load often enough that failover is not optional.
    overpass_mirrors: tuple[str, ...] = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )

    # Total budget for a cold network fetch inside a request. The orchestrator
    # allows S1 only 3s (ENDPOINT.md section 4.5), so we must answer faster than
    # that from cache or not at all.
    cold_fetch_budget_s: float = 2.5
    overpass_timeout_s: float = 60.0

    @property
    def bbox_str(self) -> str:
        s, w, n, e = self.demo_bbox
        return f"{s},{w},{n},{e}"

    # ------------------------------------------------------------------ #
    # S2 query radius
    # ------------------------------------------------------------------ #

    #: The radius S2 expands its tile queries by. Fixed, and equal to
    #: ``DEFAULT_RADIUS_M`` on purpose, so one ``prefetch_demo_area`` run seeds S1
    #: and S2 over the same tile grid.
    #:
    #: It must not be derived per request. The disk cache is keyed by a hash of
    #: the query text, and the query text embeds this margin -- so a request that
    #: computed its own radius could never read a fixture captured at this one.
    #: Correct cache id, different query hash, a miss. That is not hypothetical:
    #: S2 derived 71.42 m from the spread of the spots and reported "cover fetch
    #: timed out" for tiles that were sitting on disk, because the capture radius
    #: could not be read off the code that uses it. See
    #: ``services/s2_weather_cover/service.py::search_radius_m``.
    cover_query_radius_m: float = 150.0


SETTINGS = Settings()

# Honor the DEMO_* / MOCK env vars from .env.example (ENDPOINT.md section 8).
_env_overrides: dict[str, object] = {}
if os.getenv("MOCK") is not None:
    _env_overrides["mock"] = _env_bool("MOCK", False)
if os.getenv("DEMO_BBOX"):
    _parts = [p.strip() for p in os.getenv("DEMO_BBOX", "").split(",")]
    if len(_parts) == 4:
        try:
            _env_overrides["demo_bbox"] = tuple(float(p) for p in _parts)  # type: ignore[assignment]
        except ValueError:
            pass
if os.getenv("DEMO_RIDER"):
    _parts = [p.strip() for p in os.getenv("DEMO_RIDER", "").split(",")]
    if len(_parts) == 2:
        try:
            _env_overrides["demo_rider"] = tuple(float(p) for p in _parts)  # type: ignore[assignment]
        except ValueError:
            pass
if os.getenv("DEMO_TZ"):
    _env_overrides["demo_tz"] = os.getenv("DEMO_TZ", SETTINGS.demo_tz)
if os.getenv("TRAFFIC_SIDE"):
    _env_overrides["traffic_side"] = os.getenv("TRAFFIC_SIDE", SETTINGS.traffic_side)
if _env_overrides:
    SETTINGS = Settings(**_env_overrides)  # type: ignore[arg-type]

# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #

#: Spacing of generated stop points along each curb line (ENDPOINT.md section 6 S1).
SAMPLE_STEP_M: float = 10.0

#: How far a generated point may stray from its parent centerline before we
#: believe the geometry blew up (short segment, sharp corner) and drop it.
#: This is the safety net that lets us sample points directly instead of
#: building offset curb polylines.
CORRIDOR_TOL_M: float = 1.0

#: Assumed width of one travel lane when the `width` tag is missing. Measured on
#: the Miami demo area: `width` is absent on 422/422 ways, `lanes` present on
#: 376/422 -- so lanes are the primary width signal, not the fallback.
LANE_WIDTH_M: float = 3.5

#: Width added to one side of the centerline for an in-lane cycleway.
CYCLEWAY_WIDTH_M: float = 1.8

#: Width added to one side of the centerline for a dedicated parking lane.
PARKING_LANE_WIDTH_M: float = 2.4

#: Default total road width per `highway` class, used only when neither `width`
#: nor `lanes` is present.
DEFAULT_WIDTH_BY_HIGHWAY: dict[str, float] = {
    "motorway": 12.0,
    "trunk": 12.0,
    "primary": 12.0,
    "secondary": 11.0,
    "tertiary": 10.0,
    "unclassified": 8.0,
    "residential": 8.0,
    "living_street": 7.0,
    "pedestrian": 6.0,
    "service": 5.0,
    "track": 4.0,
    "footway": 3.0,
    "path": 3.0,
    "cycleway": 3.5,
}

#: Roads a robotaxi could conceivably stop on. ENDPOINT.md section 6 S1 lists
#: primary..living_street; we add service/pedestrian because a campus pickup is
#: often on a service road or a pedestrian plaza edge.
STOPPABLE_HIGHWAY_CLASSES: frozenset[str] = frozenset(
    {
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "pedestrian",
    }
)

#: `highway` values we refuse to generate candidates on even if they appear in a
#: query, because stopping on them is not legal or not meaningful.
NON_STOPPABLE_HIGHWAY_CLASSES: frozenset[str] = frozenset(
    {"motorway", "motorway_link", "trunk", "trunk_link", "footway", "path", "cycleway", "track", "steps", "construction", "proposed"}
)

# --------------------------------------------------------------------------- #
# Exclusion buffers
# --------------------------------------------------------------------------- #
# Modeled on common US parking rules (ENDPOINT.md section 6 S1). Verify against
# your demo city; FIU campus sits inside Miami-Dade so US rules apply.

BUFFER_FIRE_HYDRANT_M: float = 4.6          # 15 ft
BUFFER_CROSSWALK_M: float = 6.0             # 20 ft, measured ALONG the roadway
BUFFER_INTERSECTION_M: float = 6.0         # 20 ft, measured ALONG the roadway
BUFFER_STOP_SIGN_M: float = 9.0            # 30 ft
BUFFER_TRAFFIC_SIGNAL_M: float = 9.0        # 30 ft
BUFFER_BUS_STOP_M: float = 15.0

#: A stop point must be at least this far from ANY restriction to survive. A
#: small positive margin stops candidates that sit exactly on a buffer edge.
MIN_CLEARANCE_M: float = 0.5

#: Straight-line distance to walk, multiplied by this to approximate a street
#: network detour (ENDPOINT.md section 6 S1 step 5).
DETOUR_FACTOR: float = 1.3

#: Crossing the street to reach a stop point costs more than walking along it.
DETOUR_FACTOR_ACROSS_STREET: float = 1.45

#: Candidates closer together than this are considered the same stopping place.
DEDUPE_RADIUS_M: float = 5.0

#: Maximum candidates returned, per ENDPOINT.md section 6 S1 step 7.
MAX_SPOTS: int = 30

#: Default search radius, per ENDPOINT.md section 3 step 2.
DEFAULT_RADIUS_M: float = 150.0

#: Hard cap on a caller-supplied radius, so one bad request cannot ask us to
#: scan the whole county.
MAX_RADIUS_M: float = 500.0

# --------------------------------------------------------------------------- #
# Curb-side legality
# --------------------------------------------------------------------------- #

#: `parking*` tag values that permit stopping for passenger pickup.
PERMISSIVE_PARKING_VALUES: frozenset[str] = frozenset(
    {
        "yes",
        "parallel",
        "perpendicular",
        "diagonal",
        "designated",
        "street_side",
        "undivided",
        "marked",
        "street",
    }
)

#: Values that forbid passenger pickup, or restrict it to someone else
#: (customers, permit holders, buses, taxis). A robotaxi picking up a
#: non-customer at an `access=customers` lot is a legality bug, not a nitpick.
RESTRICTIVE_PARKING_VALUES: frozenset[str] = frozenset(
    {
        "no",
        "none",
        "no_parking",
        "no_stopping",
        "agricultural",
        "forestry",
        "delivery",
        "delivery_only",
        "loading",
        "loading_only",
        "bus",
        "taxi",
        "car_share",
        "carpool",
        "customers",
        "customer",
        "permit",
        "residents",
        "private",
        "emergency",
        "school_dropzone",
        "bus_stop",
    }
)

#: `cycleway:*` values that place a cycle lane INSIDE the roadway on that side,
#: between the travel lane and the curb. These make the curb unstappable: a car
#: stopping at the curb would sit in the bike lane.
#:
#: `lane` is the plain case. `shared_lane`/`shared`/`share_busway` share the
#: lane with motor vehicles or buses. `buffered` and `segregated` are separated
#: from the travel lane by a paint buffer or a barrier, but they still sit
#: between the travel lane and the curb, so they obstruct just the same.
IN_LANE_CYCLEWAY_VALUES: frozenset[str] = frozenset(
    {
        "lane",
        "shared_lane",
        "shared",
        "share_busway",
        "buffered",
        "segregated",
    }
)

#: `cycleway:*` values that do NOT obstruct the curb on the tagged side.
#: ENDPOINT.md section 6 S1 says "exclude that side of the road" for any
#: `cycleway=*lane` tag, which is too broad in ways that matter:
#:
#: * `separate` is an off-roadway path.
#: * `opposite` belongs to the OTHER carriageway of a divided road, so it never
#:   touches our curb.
#: * `opposite_lane` marks a contraflow lane on the side away from vehicle
#:   travel. We only permit stopping on the travel side of a one-way, so it
#:   never restricts a side we would offer anyway.
#:
#: These are common on exactly the wide, busy roads where deleting the curb would
#: leave a rider who needs a covered spot with nowhere to go.
NON_OBSTRUCTING_CYCLEWAY_VALUES: frozenset[str] = frozenset(
    {"separate", "opposite", "opposite_lane", "opposite_track", "no", "none"}
)
