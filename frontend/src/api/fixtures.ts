// PLACEHOLDER DATA for FIU Modesto A. Maidique Campus (Miami, FL) and the surrounding area.
// Pickup spots are snapped to real drivable roads, and routes come from real road/footpath
// routing (see scripts/build-routes.mjs → routes.generated.json). Everything else here —
// names, rankings, weather, sidewalk accessibility details — is made up until the services exist:
//   legal spots → S1, weather + cover ranking → S2, sidewalk data → accessibility service,
//   routes → orchestrator routing adapter.

import polyline from '@mapbox/polyline'
import { pointAlong, pathLengthM } from '../lib/geo'
import routes from './routes.generated.json'
import type { AccessibilityInfo, Condition, LatLng, PickupMode, Place, RankedSpot, Spot, WeatherReport } from './types'

export const MAP_CENTER: LatLng = { lat: 25.7574, lng: -80.3733 }
export const DEFAULT_ZOOM = 17

/**
 * The backend's DEMO_RIDER (shared/config.py): near Green Library, picked because its best
 * curbs sit ~6 m from overhead cover, so rain mode has something real to rank.
 */
export const RIDER_LOCATION: LatLng = { lat: 25.7584, lng: -80.3725 }
export const RIDER_ADDRESS = 'Near Green Library, FIU'

/**
 * Campus pickup presets (coordinates from OpenStreetMap). All sit inside the area the backend has
 * cached OSM data for, so S1 can find legal spots around them even in MOCK mode.
 */
export const PICKUP_PRESETS: Place[] = [
  { id: 'gc', name: 'Graham Center (GC)', address: 'SW 14th St, FIU', kind: 'campus', location: { lat: 25.75622, lng: -80.3727 } },
  { id: 'gl', name: 'Green Library', address: 'SW 8th St, FIU', kind: 'campus', location: { lat: 25.7571, lng: -80.37375 } },
  { id: 'cp', name: 'Chemistry & Physics', address: 'University Dr, FIU', kind: 'campus', location: { lat: 25.75853, lng: -80.37202 } },
  { id: 'ahc5', name: 'Academic Health Center 5', address: 'East Campus Circle, FIU', kind: 'campus', location: { lat: 25.75915, lng: -80.37132 } },
  { id: 'pg5', name: 'PG5 Market Station', address: 'University Dr, FIU', kind: 'campus', location: { lat: 25.75986, lng: -80.37124 } },
  { id: 'rb', name: 'Ryder Business Building', address: 'SW 112th Ave, FIU', kind: 'campus', location: { lat: 25.75747, lng: -80.37612 } },
  { id: 'pc', name: 'Charles E. Perry Building (PC)', address: 'SW 14th St, FIU', kind: 'campus', location: { lat: 25.75552, lng: -80.37379 } },
  { id: 'wrc', name: 'Wellness & Recreation Center', address: 'East Campus Circle, FIU', kind: 'campus', location: { lat: 25.75576, lng: -80.37804 } },
  { id: 'arch', name: 'School of Architecture', address: 'University Dr, FIU', kind: 'campus', location: { lat: 25.75891, lng: -80.37565 } },
  { id: 'frost', name: 'Frost Art Museum', address: 'SW 17th St, FIU', kind: 'campus', location: { lat: 25.75383, lng: -80.37304 } },
  { id: 'wpac', name: 'Wertheim Performing Arts Center', address: 'FIU', kind: 'campus', location: { lat: 25.75249, lng: -80.37262 } },
]

/** Where the backend has cached OSM data (S1/S2 fixtures): south, west, north, east. */
export const DEMO_AREA = { south: 25.752, west: -80.38, north: 25.764, east: -80.368 }
export const inDemoArea = (p: LatLng) =>
  p.lat >= DEMO_AREA.south && p.lat <= DEMO_AREA.north && p.lng >= DEMO_AREA.west && p.lng <= DEMO_AREA.east

/** The backend's demo_car_start (shared/config.py); used by placeholder mode. */
export const CAR_START: LatLng = { lat: 25.7625, lng: -80.385 }

export const PLACES: Place[] = [
  { id: 'home', name: 'Home', address: 'Placeholder: set a home address', kind: 'home', location: { lat: 25.7631, lng: -80.3835 } },
  { id: 'work', name: 'Work', address: 'Placeholder: set a work address', kind: 'work', location: { lat: 25.769, lng: -80.3671 } },
  { id: 'ec', name: 'FIU Engineering Center', address: '10555 W Flagler St', kind: 'campus', location: { lat: 25.769, lng: -80.3671 } },
  { id: 'dolphin', name: 'Dolphin Mall', address: '11401 NW 12th St', kind: 'shopping', location: { lat: 25.7887, lng: -80.3806 } },
  { id: 'tamiami', name: 'Tamiami Park', address: '11201 SW 24th St', kind: 'park', location: { lat: 25.75, lng: -80.379 } },
  { id: 'baptist', name: 'Baptist Hospital of Miami', address: '8900 N Kendall Dr', kind: 'hospital', location: { lat: 25.6858, lng: -80.3397 } },
  { id: 'mia', name: 'Miami International Airport', address: '2100 NW 42nd Ave', kind: 'airport', location: { lat: 25.7953, lng: -80.2789 } },
]

// ---- routes ---------------------------------------------------------------

interface GeneratedRoute { polyline: string; distance_m: number; duration_s: number }
const DRIVE = routes.drive as Record<string, GeneratedRoute>
const WALK = routes.walk as Record<string, GeneratedRoute>

export const decode = (encoded: string): LatLng[] => polyline.decode(encoded).map(([lat, lng]) => ({ lat, lng }))
export const encode = (path: LatLng[]) => polyline.encode(path.map((p) => [p.lat, p.lng]))

/** Real driving route between two known points, or a straight line if we don't have one cached. */
export function driveRoute(fromKey: string, toKey: string, from: LatLng, to: LatLng): LatLng[] {
  const r = DRIVE[`${fromKey}>${toKey}`]
  return r ? decode(r.polyline) : [from, to]
}

// ---- legal spots (placeholder for S1) -------------------------------------

/** Spot keys match the ones in scripts/build-routes.mjs. */
const SPOT_DEFS: { key: string; lat: number; lng: number; street: string; side: string; type: Spot['spot_type'] }[] = [
  { key: 'ecc_north', lat: 25.75701, lng: -80.37096, street: 'East Campus Circle (north)', side: 'west', type: 'curb' },
  { key: 'gc_service', lat: 25.75592, lng: -80.37249, street: 'GC service drive', side: 'north', type: 'loading_zone' },
  { key: 'ecc_south', lat: 25.75656, lng: -80.37102, street: 'East Campus Circle (south)', side: 'west', type: 'curb' },
  { key: 'sw14', lat: 25.75518, lng: -80.37365, street: 'SW 14th St', side: 'north', type: 'curb' },
  { key: 'library_west', lat: 25.75713, lng: -80.37445, street: 'Green Library drive', side: 'east', type: 'curb' },
  { key: 'gc_loading', lat: 25.75648, lng: -80.37224, street: 'GC loading area drive', side: 'west', type: 'loading_zone' },
]

export const PLACEHOLDER_SPOTS: Spot[] = SPOT_DEFS.map((d) => ({
  spot_id: `s1_${d.key}`,
  stop_point: { lat: d.lat, lng: d.lng },
  street_name: d.street,
  side: d.side,
  curb_bearing_deg: 0,
  spot_type: d.type,
  walk_distance_m: WALK[`rider>${d.key}`]?.distance_m ?? 0,
  source: 'placeholder',
  confidence: 'unverified',
  notes: [],
}))

export const spotKey = (spot: Spot) => spot.spot_id.replace(/^s1_/, '')

// ---- ranking (placeholder for S2 + accessibility service) -----------------

/** Placeholder sidewalk data: curb ramps at the end of the walk and partway along it. */
export function placeholderAccessibility(walk: LatLng[], stepFree: boolean): AccessibilityInfo {
  const total = pathLengthM(walk)
  return {
    step_free: stepFree,
    curb_ramps: walk.length > 1 ? [pointAlong(walk, total * 0.55).position, walk[walk.length - 1]] : [],
    unramped_crossings: stepFree ? 0 : 1,
    raised_crossings: 0,
    through_buildings: [],
    notes: [],
    source: 'placeholder',
  }
}

const MODE_ORDER: Record<PickupMode, string[]> = {
  standard: ['ecc_north', 'ecc_south', 'gc_service'],
  accessible: ['gc_service', 'ecc_south', 'ecc_north'],
  weather: ['ecc_south', 'gc_service', 'ecc_north'],
}

function reasonFor(mode: PickupMode, condition: Condition): string {
  if (mode === 'standard') return 'Fastest pickup for your car'
  if (mode === 'accessible') return 'Step-free sidewalk route with curb ramps (placeholder)'
  if (condition === 'rain') return 'Covered walkway beside the curb (placeholder)'
  if (condition === 'sun') return 'Shade from nearby buildings (placeholder)'
  return 'No rain or strong sun right now, so the fastest pickup'
}

export function placeholderRanking(mode: PickupMode, condition: Condition = 'neutral'): RankedSpot[] {
  // ENDPOINT.md §6: with neutral weather S2 ranks by walk distance, i.e. the same as a standard pickup.
  const order = mode === 'weather' && condition === 'neutral' ? MODE_ORDER.standard : MODE_ORDER[mode]
  return order.map((key, i) => {
    const spot = PLACEHOLDER_SPOTS.find((s) => spotKey(s) === key)!
    const walkEncoded = WALK[`rider>${key}`]?.polyline
    const walk = walkEncoded ? decode(walkEncoded) : [RIDER_LOCATION, spot.stop_point]
    return {
      spot,
      wait_point: spot.stop_point,
      cover_feature: mode === 'weather' && condition === 'rain' && i === 0
        ? { feature_id: 'placeholder_cover', kind: 'covered_walkway', geometry_wkt: '', provides: ['rain', 'sun'], source: 'placeholder', confidence: 'likely' }
        : null,
      gap_m: null,
      score: Math.round((0.9 - i * 0.12) * 100) / 100,
      confidence: mode === 'standard' || (mode === 'weather' && condition === 'neutral') ? 'unverified' : 'likely',
      reason: i === 0 ? reasonFor(mode, condition) : 'Alternative (placeholder)',
      walk_polyline: walkEncoded,
      accessibility: mode === 'accessible' ? placeholderAccessibility(walk, i === 0) : undefined,
    }
  })
}

export function placeholderWeather(condition: WeatherReport['condition'] | null | undefined): WeatherReport {
  const c = condition ?? 'neutral'
  return {
    condition: c,
    precip_mm_h: c === 'rain' ? 2.4 : 0,
    cloud_cover_pct: c === 'sun' ? 10 : 90,
    uv_index: c === 'sun' ? 8 : 2,
    apparent_temperature_c: c === 'sun' ? 35 : 27,
    is_day: true,
    weather_code: c === 'rain' ? 63 : c === 'sun' ? 1 : 3,
    valid_at: new Date().toISOString(),
    source: 'placeholder',
    overridden: condition != null,
    reason: condition == null ? 'Live weather is not connected yet' : 'Demo override',
  }
}
