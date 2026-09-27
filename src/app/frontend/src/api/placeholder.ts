// PLACEHOLDER orchestrator that runs in the browser. It returns fixture data shaped like the real
// contracts so the UI can be built now. Delete this file once the orchestrator is live.

import { pathLengthM, pointAlong } from '../lib/geo'
import type { RideApi } from './client'
import { CAR_START, PLACEHOLDER_SPOTS, driveRoute, encode, placeholderRanking, placeholderWeather, spotKey } from './fixtures'
import type { AnswerBody, LatLng, PickupMode, RidePlan, RideState } from './types'

const PICKUP_SPEED_MPS = 60 // fast so the demo doesn't take minutes
const TRIP_SPEED_MPS = 250 // the ride to the destination is a quick placeholder (at most ~12 s)
const APPROACH_M = 150 // ENDPOINT.md §7: switch to real-time phase within 150 m
const VISION_PLACEHOLDER_MS = 2000 // S3 Vision would run here; the UI doesn't show it yet

interface Stored {
  plan: RidePlan
  path: LatLng[]
  movingSince: number | null // ms timestamp when the car started along `path`
  offsetM: number // meters already covered before movingSince (for skip-to-arrival)
  approachAt: number | null
  trip: 'to_pickup' | 'to_destination'
}

const rides = new Map<string, Stored>()
const delay = <T>(value: T, ms = 350) => new Promise<T>((r) => setTimeout(() => r(value), ms))

function snapshot(s: Stored): RideState {
  const total = pathLengthM(s.path)
  const moved = s.movingSince === null ? 0 : s.offsetM + ((Date.now() - s.movingSince) / 1000) * (s.trip === 'to_pickup' ? PICKUP_SPEED_MPS : Math.max(TRIP_SPEED_MPS, total / 12))
  const along = Math.min(total, moved)
  const { position, heading } = pointAlong(s.path, along)
  const remaining = total - along

  if (s.trip === 'to_pickup' && s.movingSince !== null) {
    if (s.plan.phase === 'predicted' && remaining < APPROACH_M) {
      s.plan.phase = 'approaching'
      s.approachAt = Date.now()
    }
    if (s.plan.phase === 'approaching' && s.approachAt && Date.now() - s.approachAt > VISION_PLACEHOLDER_MS) {
      // Placeholder for S3 Vision + fusion: keep the predicted spot.
      s.plan.phase = 'confirmed'
      s.plan.final_spot = s.plan.predicted_spot
      s.plan.rider_message = 'Pickup confirmed. Your car will stop at the curb. (Placeholder)'
    }
  }

  const trip_status =
    s.trip === 'to_pickup'
      ? remaining <= 1 && s.plan.phase === 'confirmed' ? 'at_pickup' : 'to_pickup'
      : remaining <= 1 ? 'completed' : 'to_destination'

  return {
    ...s.plan,
    eta_s: Math.round(remaining / 11), // shown as if driving at ~25 mph
    car_position: position,
    car_heading_deg: heading,
    trip_status,
  }
}

function get(id: string): Stored {
  const s = rides.get(id)
  if (!s) throw new Error(`Unknown ride ${id}`)
  return s
}

export const placeholderApi: RideApi = {
  async requestRide() {
    const ride_id = `r_placeholder_${Date.now()}`
    rides.set(ride_id, {
      plan: {
        ride_id, phase: 'predicted', mobility_needs: false, weather: null, candidates: [],
        predicted_spot: null, final_spot: null, route_polyline: null, eta_s: 0, rider_message: '', fallbacks_used: [],
      },
      path: [CAR_START],
      movingSince: null,
      offsetM: 0,
      approachAt: null,
      trip: 'to_pickup',
    })
    return delay({
      ride_id,
      spot_count: PLACEHOLDER_SPOTS.length,
      question: "Would you like a pickup spot that's easier to reach and keeps you out of the rain and sun?",
      spots: PLACEHOLDER_SPOTS,
    }, 900)
  },

  async answer(id, body: AnswerBody) {
    const s = get(id)
    const mode: PickupMode = body.pickup_mode ?? (body.mobility_needs ? 'weather' : 'standard')
    const weather = mode === 'weather' ? placeholderWeather(body.force_condition) : null
    const ranked = placeholderRanking(mode, weather?.condition ?? 'neutral')
    const best = ranked[0]
    s.path = driveRoute('car_start', spotKey(best.spot), CAR_START, best.spot.stop_point)
    const walk = `${best.spot.walk_distance_m} m walk`
    s.plan = {
      ...s.plan,
      pickup_mode: mode,
      mobility_needs: mode !== 'standard',
      weather,
      candidates: ranked,
      predicted_spot: best,
      final_spot: null,
      phase: 'predicted',
      route_polyline: encode(s.path),
      eta_s: Math.round(pathLengthM(s.path) / 11),
      rider_message:
        mode === 'accessible' ? `Follow the step-free route to ${best.spot.street_name}, ${walk}. (Placeholder)`
        : mode === 'weather' && weather?.condition === 'rain' ? `Rain expected at pickup. Wait under the covered walkway on ${best.spot.street_name}, ${walk}. (Placeholder)`
        : mode === 'weather' && weather?.condition === 'sun' ? `Strong sun at pickup. Wait in the shade on ${best.spot.street_name}, ${walk}. (Placeholder)`
        : mode === 'weather' ? `No rain or strong sun at pickup, so we picked the fastest spot on ${best.spot.street_name}, ${walk}. (Placeholder)`
        : `Your car will pick you up on ${best.spot.street_name}, ${walk}. (Placeholder)`,
    }
    return delay({ ...s.plan })
  },

  async confirm(id) {
    return delay({ ...get(id).plan })
  },

  async dispatch(id) {
    const s = get(id)
    s.movingSince = Date.now()
    s.offsetM = 0
    return delay(snapshot(s), 1500)
  },

  async getRide(id) {
    return snapshot(get(id))
  },

  async skipToArrival(id) {
    const s = get(id)
    const total = pathLengthM(s.path)
    s.offsetM = Math.max(0, total - APPROACH_M + 1)
    s.movingSince = Date.now()
    return snapshot(s)
  },

  async startTrip(id, destination) {
    const s = get(id)
    const pickupSpot = s.plan.final_spot ?? s.plan.predicted_spot
    const pickup = pickupSpot?.spot.stop_point ?? s.path[s.path.length - 1]
    s.path = pickupSpot
      ? driveRoute(spotKey(pickupSpot.spot), destination.place_id, pickup, destination.location)
      : [pickup, destination.location]
    s.plan.route_polyline = encode(s.path)
    s.trip = 'to_destination'
    s.movingSince = Date.now()
    s.offsetM = 0
    return delay(snapshot(s))
  },
}
