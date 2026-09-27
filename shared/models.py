"""Pydantic contracts shared by every service (ENDPOINT.md section 5).

The doc's rule: "Put these Pydantic models in ``shared/models.py``; every service
imports them so contracts can't drift." Deviations from the literal JSON in the
doc, all deliberate and all discussed before implementation:

* ``Spot.side`` is ``"left"``/``"right"`` relative to the OSM way's digitization
  direction, not a compass value like the doc's ``"east"``. S2's bike-lane side
  exclusion and S1's one-way rule both need an unambiguous side identity;
  ``curb_bearing_deg`` remains the absolute disambiguator.
* ``SpotType`` drops ``driveway_pullout``. Nothing in the spec's algorithm ever
  generated one, and a contract should only describe what the service produces.
* ``Spot`` gains ``segment_id``, ``clearance_m`` and ``legality_basis``.
* ``WeatherReport`` is the UPDATED version from ENDPOINT.md section 6 (the doc
  says "replace the section 5 version"), so it carries ``uv_index``,
  ``direct_radiation_w_m2``, ``apparent_temperature_c`` and ``reason``.

Request models set ``extra="forbid"`` so a typo'd field fails loudly at the
boundary instead of being silently ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class Confidence(str, Enum):
    """ENDPOINT.md section 4.6. Shown in the UI; builds trust by being honest
    about data gaps.

    S1 can only ever emit ``LIKELY`` or ``UNVERIFIED`` (it caps at ``likely``;
    ``verified`` is reserved for genuinely official sources and ``detected`` for
    S3). That restriction is enforced in code, not just by convention.
    """

    VERIFIED = "verified"
    LIKELY = "likely"
    DETECTED = "detected"
    UNVERIFIED = "unverified"

    @property
    def rank(self) -> int:
        """Trust ordering, higher is better.

        Lives here rather than in ``ranking.py`` because it is a property of the
        tier itself, not of any one consumer: the first version had a private
        ``_CONFIDENCE_RANK`` dict in the ranking module, which is one careless
        edit away from disagreeing with what the tier means.

        ``DETECTED`` outranks ``UNVERIFIED`` on purpose. S3 observing a real awning
        is better evidence than an inferred curb tag, even though neither is
        official.
        """
        return {
            Confidence.VERIFIED: 3,
            Confidence.LIKELY: 2,
            Confidence.DETECTED: 1,
            Confidence.UNVERIFIED: 0,
        }[self]


class Condition(str, Enum):
    """ENDPOINT.md section 6 S2 Module 1. Which protection the rider needs."""

    RAIN = "rain"
    SUN = "sun"
    NEUTRAL = "neutral"


class Side(str, Enum):
    """Which side of an OSM way, relative to the way's digitization direction."""

    LEFT = "left"
    RIGHT = "right"


class SpotType(str, Enum):
    CURB = "curb"
    LOADING_ZONE = "loading_zone"
    PARKING_LOT = "parking_lot"


class LegalityBasis(str, Enum):
    """WHY a curb side was considered legal, independent of ``Confidence``.

    Needed because S1 caps at ``likely``: both an explicit permissive parking tag
    and a strong structural inference land on the same tier, so the tier alone
    cannot produce the explanation ENDPOINT.md section 4.7 requires.

    ``OFFICIAL_REGULATION`` is the only value that would justify
    ``Confidence.VERIFIED``, and nothing emits it today -- there is no official
    curb regulation feed for the Miami demo area (Miami-Dade's OMF "SMART Curb"
    programme is a freight project with no public endpoint, and no Miami open data
    portal publishes a curb regulation layer). It exists so that a future CDS or
    CurbLR source drops in without a contract change.
    """

    OFFICIAL_REGULATION = "official_regulation"
    TAGGED_PERMISSIVE = "tagged_permissive"
    INFERRED_STANDARD = "inferred_standard"
    UNKNOWN = "unknown"


class RestrictionKind(str, Enum):
    """Typed exclusion sources. The geometry differs by kind, which is the whole
    point -- see ``legality.py``."""

    FIRE_HYDRANT = "fire_hydrant"
    CROSSING = "crossing"
    INTERSECTION = "intersection"
    STOP_SIGN = "stop_sign"
    TRAFFIC_SIGNAL = "traffic_signal"
    BUS_STOP = "bus_stop"


class CurbAccess(str, Enum):
    """How a rider gets between the sidewalk and the car at a stop point.

    From OSM ``kerb=*`` on ``barrier=kerb`` nodes within
    ``CURB_RAMP_MAX_DISTANCE_M`` on the same side of the road. ``UNKNOWN`` means
    nothing is mapped there, NOT that there is no ramp: around FIU mappers
    recorded ramps and flush kerbs but no raised ones, so absence is uninformative.
    """

    FLUSH = "flush"
    LOWERED = "lowered"
    RAISED = "raised"
    UNKNOWN = "unknown"


class ShadeSource(str, Enum):
    GOOGLE_SOLAR = "google_solar"
    OSM_GEOMETRY = "osm_geometry"


class RidePhase(str, Enum):
    PREDICTED = "predicted"
    APPROACHING = "approaching"
    CONFIRMED = "confirmed"


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

class StrictModel(BaseModel):
    """Base for REQUEST models: reject unknown fields so typos surface."""

    model_config = ConfigDict(extra="forbid")


class LatLng(BaseModel):
    """Named fields only. This is the structural answer to ENDPOINT.md's
    "the #1 bug tonight will be lat/lng order": nothing in our own code ever
    passes a bare tuple, so an accidental swap cannot compile into a request."""

    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lng: Annotated[float, Field(ge=-180.0, le=180.0)]

    def as_tuple(self) -> tuple[float, float]:
        """(lat, lng) -- our own internal convention."""
        return self.lat, self.lng

    def as_lnglat(self) -> tuple[float, float]:
        """(lng, lat) -- GeoJSON / OSRM / always_xy convention."""
        return self.lng, self.lat

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.lat:.6f},{self.lng:.6f}"


class BBox(BaseModel):
    """south, west, north, east -- Overpass/Mercator convention."""

    south: float
    west: float
    north: float
    east: float

    @model_validator(mode="after")
    def _check_orientation(self) -> "BBox":
        """Reject an inverted box.

        A reversed bbox is not an error anywhere downstream -- Overpass accepts it
        and returns zero elements, so the service quietly reports "no roads here"
        and falls back. That is exactly the kind of failure worth a few lines of
        validation: it presents as missing data rather than as a bug, which is the
        hardest kind to notice before a demo.
        """
        if self.south >= self.north:
            raise ValueError(f"south ({self.south}) must be < north ({self.north})")
        if self.west >= self.east:
            raise ValueError(f"west ({self.west}) must be < east ({self.east})")
        return self

    @property
    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.south, self.west, self.north, self.east

    @property
    def as_overpass(self) -> str:
        return f"{self.south},{self.west},{self.north},{self.east}"

    def contains(self, pt: LatLng) -> bool:
        return self.south <= pt.lat <= self.north and self.west <= pt.lng <= self.east

    def expanded(self, meters: float) -> "BBox":
        """Grow by roughly ``meters`` on every side (latitude-corrected)."""
        dlat = meters / 111_320.0
        import math as _m

        dlng = meters / (111_320.0 * max(_m.cos(_m.radians(self.south)), 1e-6))
        return BBox(
            south=self.south - dlat,
            west=self.west - dlng,
            north=self.north + dlat,
            east=self.east + dlng,
        )


# --------------------------------------------------------------------------- #
# S1: Legal Spots
# --------------------------------------------------------------------------- #

class Spot(BaseModel):
    """A place the car may legally stop (ENDPOINT.md section 5)."""

    spot_id: str
    stop_point: LatLng
    street_name: str | None = None
    #: Which side of the way. ``None`` is the honest value for the degraded
    #: fallback spot at the rider's own location, where we have no road to have
    #: a side. The first version defaulted to ``Side.RIGHT``, which meant a spot
    #: that forgot to set the field silently claimed to be on the right -- a
    #: fabrication that reads as data. Consumers must handle ``None``.
    side: Side | None = None
    #: Compass direction from the stop point toward the sidewalk. S3 uses this
    #: to aim the camera (ENDPOINT.md section 5), so it is wrapped on the way in
    #: rather than trusted: a raw 370 deg would rotate a camera past north.
    curb_bearing_deg: float = 0.0
    spot_type: SpotType = SpotType.CURB
    #: Never negative. A negative value would sort ahead of every other candidate
    #: in the ranking and render as the closest spot, which is the opposite of
    #: the truth and invisible in a list sorted by distance.
    walk_distance_m: float = Field(default=0.0, ge=0.0)
    source: str = "osm"
    confidence: Confidence = Confidence.UNVERIFIED
    notes: list[str] = Field(default_factory=list)

    # --- additions beyond the doc's JSON, all additive ---
    #: Parent OSM way id, so the UI can highlight the whole street.
    segment_id: str | None = None
    #: Distance to the nearest exclusion buffer. A genuine safety readout: on an
    #: unverified spot, "8 m from the nearest hydrant" is the most useful thing
    #: we can tell a rider, and it is the sane tie-break when deduplicating.
    clearance_m: float | None = Field(default=None, ge=0.0)
    legality_basis: LegalityBasis = LegalityBasis.UNKNOWN
    #: Kerb between the sidewalk and the car door. See ``CurbAccess``.
    curb_access: CurbAccess = CurbAccess.UNKNOWN
    #: Distance to the kerb that decided ``curb_access``; None when unknown.
    ramp_distance_m: float | None = Field(default=None, ge=0.0)
    #: Where ``curb_access`` came from (``"osm"``), or None when unknown.
    curb_access_source: str | None = None

    @field_validator("curb_bearing_deg")
    @classmethod
    def _wrap_bearing(cls, v: float) -> float:
        """Normalize to [0, 360).

        S3 aims a camera with this value, so anything outside the compass range
        is a real error, not a cosmetic one. Wrapping rather than rejecting is
        deliberate: our own generator is already in range, so an out-of-range
        value can only come from arithmetic upstream, and quietly normalising it
        is both safe and self-documenting.
        """
        return v % 360.0


class LegalSpotsRequest(StrictModel):
    rider_location: LatLng
    radius_m: float = Field(default=150.0, gt=0.0, le=500.0)


class LegalSpotsResponse(BaseModel):
    spots: list[Spot] = Field(default_factory=list)
    count: int = 0
    source: str = "osm"
    cached: bool = False

    # --- additions beyond the doc's JSON, all additive ---
    #: Candidates found BEFORE the MAX_SPOTS cap, so the orchestrator can tell
    #: it was not handed everything.
    total_candidates: int = 0
    truncated: bool = False
    bbox: BBox | None = None
    generated_at: datetime | None = None
    #: Age of the underlying OSM network document. Large values mean the demo is
    #: running on stale data and should be re-prefetched.
    cache_age_s: float | None = None
    fallbacks_used: list[str] = Field(default_factory=list)


class RejectedCandidate(BaseModel):
    """A candidate that was generated and then excluded. Powers the dev-only
    ``/spots/explain`` route, which is how buffers get tuned against real data
    and how the section 6 S1 definition of done is actually checked."""

    stop_point: LatLng
    street_name: str | None = None
    side: Side = Side.RIGHT
    walk_distance_m: float = 0.0
    reason: str
    restriction_kind: RestrictionKind | None = None
    restriction_source_id: str | None = None
    buffer_m: float | None = None


class SpotsExplainResponse(BaseModel):
    rider_location: LatLng
    radius_m: float
    accepted: list[Spot] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    restriction_counts: dict[str, int] = Field(default_factory=dict)
    network_summary: dict[str, int] = Field(default_factory=dict)
    #: Set when the diagnostic could not run (e.g. nothing cached yet).
    error: str | None = None


# --------------------------------------------------------------------------- #
# S2: Weather and Cover
# --------------------------------------------------------------------------- #

class WeatherReport(BaseModel):
    """UPDATED version from ENDPOINT.md section 6 Module 1 (replaces section 5)."""

    condition: Condition
    precip_mm_h: float = 0.0
    cloud_cover_pct: int | None = None
    uv_index: float | None = None
    direct_radiation_w_m2: float | None = None
    apparent_temperature_c: float | None = None
    temperature_c: float | None = None
    is_day: bool = True
    weather_code: int | None = None
    valid_at: datetime
    source: str = "open-meteo"
    overridden: bool = False
    reason: str = ""


class CoverFeature(BaseModel):
    """Something overhead that protects a rider (ENDPOINT.md section 5)."""

    feature_id: str
    kind: Literal[
        "awning",
        "canopy",
        "covered_walkway",
        "shelter",
        "building_passage",
        "tree",
        "building_shadow",
    ]
    geometry_wkt: str
    provides: list[Literal["rain", "sun"]] = Field(default_factory=list)
    source: str = "osm"
    confidence: Confidence = Confidence.UNVERIFIED


class RankedSpot(BaseModel):
    spot: Spot
    wait_point: LatLng | None = None
    cover_feature: CoverFeature | None = None
    gap_m: float | None = None
    score: float = 0.0
    confidence: Confidence = Confidence.UNVERIFIED
    reason: str = ""

    # --- additions for the rain exposure ranking, all optional ---
    #: Metres of the rider's walk that are out in the rain, and under cover or
    #: indoors. Set when S2 ranked by exposure; None for the gap model and in
    #: sun/neutral mode.
    wet_m: float | None = None
    dry_m: float | None = None
    #: The walking route from the rider to the stop point, as an encoded polyline
    #: (precision 5, lat/lng). The route the exposure was measured along, so a UI
    #: can draw exactly the path the rider was scored on.
    walk_polyline: str | None = None

    # --- accessible walking routes (walk_network.py), all optional ---
    #: Metres of the walk inside a building (door to door). None when S2 had no
    #: walking network for this ride.
    indoor_m: float | None = None
    #: Plain-language facts about the route: "through Ernest R. Graham Center",
    #: "route includes steps", "2 crossings with no mapped curb ramp".
    route_notes: list[str] = Field(default_factory=list)


class SunPosition(BaseModel):
    elevation_deg: float
    azimuth_deg: float


class RankRequest(StrictModel):
    rider_location: LatLng
    spots: list[Spot] = Field(default_factory=list)
    pickup_time: datetime
    wait_minutes: int = 10
    force_condition: Condition | None = None
    force_time: datetime | None = None


class WalkRoutesRequest(StrictModel):
    """``POST /walk/routes``: real walking routes, nothing else.

    For rides that asked for no comfort features. The orchestrator uses it to
    pick the nearest spot by the walk the rider will actually make, rather than
    S1's straight-line estimate, and to hand the UI that route. ``pickup_time``
    matters only for building hours.
    """

    rider_location: LatLng
    spots: list[Spot] = Field(default_factory=list)
    pickup_time: datetime | None = None


class WalkRoute(BaseModel):
    spot_id: str
    walk_m: float
    walk_polyline: str
    indoor_m: float = 0.0
    route_notes: list[str] = Field(default_factory=list)


class WalkRoutesResponse(BaseModel):
    #: One per spot the network could reach; unreachable spots are omitted.
    routes: list[WalkRoute] = Field(default_factory=list)
    fallbacks_used: list[str] = Field(default_factory=list)


class Overlays(BaseModel):
    cover_features: list[CoverFeature] = Field(default_factory=list)
    shade_geojson: dict | None = None


class ConditionsResult(BaseModel):
    weather: WeatherReport
    mode: Condition
    ranked: list[RankedSpot] = Field(default_factory=list)
    needs_rider_confirmation: bool = False
    nearest_spot_id: str | None = None
    sun: SunPosition | None = None
    shade_source: ShadeSource | None = None
    overlays: Overlays = Field(default_factory=Overlays)
    fallbacks_used: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# S3: Vision
# --------------------------------------------------------------------------- #

class VisionAssessment(BaseModel):
    spot_id: str
    mode: Condition
    cover_present: bool = False
    cover_kind: str | None = None
    shade_fraction: float | None = None
    vision_score: float | None = None
    model_confidence: float = 0.0
    image_ref: str | None = None
    reason: str = ""


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

class RidePlan(BaseModel):
    ride_id: str
    phase: RidePhase = RidePhase.PREDICTED
    mobility_needs: bool = False
    weather: WeatherReport | None = None
    candidates: list[RankedSpot] = Field(default_factory=list)
    predicted_spot: RankedSpot | None = None
    final_spot: RankedSpot | None = None
    route_polyline: str | None = None
    eta_s: int = 0
    rider_message: str = ""
    fallbacks_used: list[str] = Field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
