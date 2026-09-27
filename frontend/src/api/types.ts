// Data contracts from ENDPOINT.md §5 (src/backend/shared/models.py).
// The orchestrator is the source of truth; keep these in sync with it.

export interface LatLng {
  lat: number
  lng: number
}

export type Confidence = 'verified' | 'likely' | 'detected' | 'unverified'
export type Condition = 'rain' | 'sun' | 'neutral'
export type Phase = 'predicted' | 'approaching' | 'confirmed'

/**
 * Which pickup the rider chose (frontend extension, not yet in ENDPOINT.md):
 * - accessible: step-free sidewalk route, curb ramps, level boarding spot
 * - weather: out of the rain and sun (the §6 S2 ranking)
 * - standard: fastest pickup
 */
export type PickupMode = 'accessible' | 'weather' | 'standard'

/** S1 Legal Spots */
export interface Spot {
  spot_id: string
  stop_point: LatLng
  street_name: string
  side: string
  curb_bearing_deg: number
  spot_type: 'curb' | 'loading_zone' | 'parking_lot' | 'driveway_pullout'
  walk_distance_m: number
  source: string
  confidence: Confidence
  notes: string[]
}

/** S2 Weather and Cover, module 1 */
export interface WeatherReport {
  condition: Condition
  precip_mm_h: number
  cloud_cover_pct: number
  uv_index?: number
  direct_radiation_w_m2?: number
  apparent_temperature_c?: number
  is_day: boolean
  weather_code: number
  valid_at: string
  source: string
  overridden: boolean
  reason?: string
}

/** S2 Weather and Cover, modules 2 and 3 */
export interface CoverFeature {
  feature_id: string
  kind: 'awning' | 'canopy' | 'covered_walkway' | 'shelter' | 'building_passage' | 'tree' | 'building_shadow'
  geometry_wkt: string
  provides: Condition[]
  source: string
  confidence: Confidence
}

/** Sidewalk accessibility of the walk to a pickup (frontend extension; see ENDPOINT.md §12 stretch goal). */
export interface AccessibilityInfo {
  step_free: boolean
  curb_ramps: LatLng[]
  max_running_slope_pct: number
  max_cross_slope_pct: number
  surface: string
  min_width_m: number
  notes: string[]
  source: string
}

export interface RankedSpot {
  spot: Spot
  wait_point: LatLng
  cover_feature: CoverFeature | null
  gap_m: number | null
  score: number
  confidence: Confidence
  reason: string
  // Frontend extensions:
  walk_polyline?: string // encoded walking route from the rider to the stop point
  accessibility?: AccessibilityInfo
}

export interface Overlays {
  cover_features: CoverFeature[]
  shade_geojson: unknown | null
}

/** Orchestrator ride plan (ENDPOINT.md §5 RidePlan). */
export interface RidePlan {
  ride_id: string
  phase: Phase
  mobility_needs: boolean
  weather: WeatherReport | null
  candidates: RankedSpot[]
  predicted_spot: RankedSpot | null
  final_spot: RankedSpot | null
  route_polyline: string | null // Google-style encoded polyline
  eta_s: number
  rider_message: string
  fallbacks_used: string[]
  // Fields the frontend reads if the orchestrator adds them (not yet in ENDPOINT.md):
  pickup_mode?: PickupMode
  needs_rider_confirmation?: boolean
  overlays?: Overlays
}

export type TripStatus = 'to_pickup' | 'at_pickup' | 'to_destination' | 'completed'

/** GET /rides/{ride_id}: plan plus live car state. */
export interface RideState extends RidePlan {
  car_position: LatLng | null
  car_heading_deg?: number
  // Frontend extension (not yet in ENDPOINT.md): where the trip is after pickup.
  trip_status: TripStatus
}

export interface RideRequestResponse {
  ride_id: string
  spot_count: number
  question: string
  // Frontend extension: lets the map draw legal spots before the rider answers.
  spots?: Spot[]
}

export interface AnswerBody {
  mobility_needs: boolean
  pickup_mode?: PickupMode // frontend extension
  force_condition?: Condition | null
  force_time?: string | null
}

export interface Place {
  id: string
  name: string
  address: string
  kind: 'home' | 'work' | 'campus' | 'shopping' | 'hospital' | 'park' | 'airport'
  location: LatLng
}
