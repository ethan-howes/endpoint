// Adapter between the rider app and the real orchestrator (orchestrator/main.py on :8000).
//
// The orchestrator supplies the real-time data: legal spots (S1), weather and cover ranking (S2),
// the car route and the simulated car position. Where its flow doesn't match the app, this file
// fills the gap on the client:
//
//  - /answer starts the car immediately, so option previews are throwaway rides and Confirm
//    starts a fresh ride with the same answer.
//  - Rides without mobility needs are confirmed without simulating the car, so the car is driven
//    here along the orchestrator's route (LocalDriver).
//  - There's no trip endpoint, so the ride to the destination is routed and driven here.
//  - pickup_mode is sent as `priority` ("accessible" | "weather") when the rider asks for comfort;
//    the walking route and its accessibility come from S2.

import polyline from '@mapbox/polyline'
import { distanceAlong, pathFromDistance } from '../lib/geo'
import { fetchRoute } from '../lib/routing'
import type { RideApi } from './client'
import { LocalDriver } from './localDriver'
import type { AnswerBody, LatLng, PickupMode, RankedSpot, RidePlan, RideRequestResponse, RideState, RideStatus } from './types'

/** Must match SETTINGS.sim_speedup in shared/config.py, so locally driven cars move like simulated ones. */
const SIM_SPEEDUP = 5
/** The destination leg is a stand-in; keep it short on screen. */
const TRIP_MAX_SCREEN_S = 20
const APPROACH_M = 150

/**
 * S3 Vision is out of scope, so the orchestrator reports it as degraded on every approach. The app
 * doesn't show vision yet, so drop notes that are only about S3 and keep anything else.
 */
function riderFacingNote(note: string | null | undefined): string | null {
  if (!note) return null
  const prefix = 'Some data was unavailable: '
  if (!note.startsWith(prefix)) return note
  const parts = note.slice(prefix.length).split('; ').filter((part) => !/^S3 /.test(part))
  return parts.length ? prefix + parts.join('; ') : null
}

const decode = (encoded: string): LatLng[] => polyline.decode(encoded).map(([lat, lng]) => ({ lat, lng }))
const encode = (path: LatLng[]) => polyline.encode(path.map((p) => [p.lat, p.lng]))

interface Entry {
  rider: LatLng
  answer?: AnswerBody
  /** Orchestrator ride holding the current preview (the app keeps using its first ride id). */
  previewId?: string
  declinedDetour?: boolean
  plan?: RidePlan
  /** Set while the car is driven on the client rather than by the orchestrator. */
  local?: { driver: LocalDriver; trip: 'to_pickup' | 'to_destination'; plan: RidePlan }
}

export function orchestratorApi(baseUrl: string): RideApi {
  const entries = new Map<string, Entry>()

  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    let res: Response
    try {
      res = await fetch(`${baseUrl}${path}`, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      throw new Error(
        `Can't reach the orchestrator at ${baseUrl}. Start the backend (./scripts/run_all.sh, or with MOCK=1), ` +
        'or set VITE_USE_PLACEHOLDER=1 to run on placeholder data.',
      )
    }
    if (!res.ok) {
      let detail = ''
      try { detail = JSON.stringify((await res.json()).detail) } catch { /* not JSON */ }
      throw new Error(`${method} ${path} failed with ${res.status}${detail ? `: ${detail}` : ''}`)
    }
    return res.json() as Promise<T>
  }

  const entry = (id: string): Entry => {
    const e = entries.get(id)
    if (!e) throw new Error(`Unknown ride ${id}`)
    return e
  }

  /** Only the fields the orchestrator accepts (its request models forbid extras). */
  const wireAnswer = (b: AnswerBody) => ({
    mobility_needs: b.mobility_needs,
    force_condition: b.mobility_needs ? b.force_condition ?? null : null,
    force_time: b.mobility_needs ? b.force_time ?? null : null,
    priority: b.pickup_mode === 'accessible' ? 'accessible' : 'weather',
  })

  /** Adds the client-only fields: pickup mode, the walking route and (placeholder) sidewalk data. */
  async function decorate(plan: RidePlan, mode: PickupMode | undefined, rider: LatLng): Promise<RidePlan> {
    const active = plan.final_spot ?? plan.predicted_spot
    let extra: Partial<RankedSpot> = {}
    if (active) {
      // Prefer S2's route: it is the walk the rain exposure was measured along. Fall back to
      // a browser-routed foot path when S2 didn't send one (gap model, sun, neutral).
      const path = active.walk_polyline
        ? decode(active.walk_polyline)
        : (await fetchRoute('foot', rider, active.spot.stop_point))?.path ?? [rider, active.spot.stop_point]
      extra = {
        walk_polyline: encode(path),
        accessibility: mode === 'accessible' ? active.accessibility ?? undefined : undefined,
      }
    }
    const withExtra = (r: RankedSpot | null) => (r && active && r.spot.spot_id === active.spot.spot_id ? { ...r, ...extra } : r)
    return { ...plan, pickup_mode: mode, predicted_spot: withExtra(plan.predicted_spot), final_spot: withExtra(plan.final_spot) }
  }

  function localState(id: string, e: Entry): RideState {
    const l = e.local!
    const { position, heading } = l.driver.snapshot()
    const done = l.driver.arrived
    return {
      ...l.plan,
      ride_id: id,
      phase: 'confirmed',
      eta_s: l.driver.etaS,
      car_position: position,
      car_heading_deg: heading,
      trip_status: l.trip === 'to_pickup' ? (done ? 'at_pickup' : 'to_pickup') : done ? 'completed' : 'to_destination',
    }
  }

  async function backendState(id: string, status: RideStatus): Promise<RideState> {
    const e = entry(id)
    // The plan only changes at answer/confirm/approach time; re-decorate only when the spot changes.
    const spotId = (p: RidePlan | undefined) => (p?.final_spot ?? p?.predicted_spot)?.spot.spot_id
    if (!e.plan || spotId(e.plan) !== spotId(status.plan) || e.plan.phase !== status.plan.phase) {
      e.plan = await decorate(status.plan, e.answer?.pickup_mode, e.rider)
    }
    const arrived = status.phase === 'confirmed' && status.remaining_m <= 1
    return {
      ...e.plan,
      rider_message: status.plan.rider_message,
      phase: status.phase,
      eta_s: status.remaining_eta_s,
      car_position: status.car_position,
      trip_status: arrived ? 'at_pickup' : 'to_pickup',
      degraded_note: riderFacingNote(status.degraded_note),
    }
  }

  /** request + answer on the orchestrator, then read the detour question (it's only on GET). */
  async function createAnswered(rider: LatLng, body: AnswerBody) {
    const req = await call<RideRequestResponse>('POST', '/rides/request', { rider_location: rider })
    entries.set(req.ride_id, { rider, answer: body })
    const plan = await call<RidePlan>('POST', `/rides/${req.ride_id}/answer`, wireAnswer(body))
    const status = await call<RideStatus>('GET', `/rides/${req.ride_id}`)
    return { id: req.ride_id, plan, status }
  }

  return {
    async requestRide(rider_location) {
      const res = await call<RideRequestResponse>('POST', '/rides/request', { rider_location })
      entries.set(res.ride_id, { rider: rider_location })
      // S1 runs in the background on the orchestrator; spots arrive with the plan.
      return { ...res, spots: [] }
    },

    async answer(id, body) {
      const e = entry(id)
      let raw: RidePlan
      let status: RideStatus
      if (!e.answer) {
        raw = await call<RidePlan>('POST', `/rides/${id}/answer`, wireAnswer(body))
        status = await call<RideStatus>('GET', `/rides/${id}`)
        e.previewId = id
      } else {
        // Each answer starts the car, and re-answering the same orchestrator ride corrupts the
        // car's position, so switching options previews on a fresh ride.
        const fresh = await createAnswered(e.rider, body)
        ;({ plan: raw, status } = fresh)
        e.previewId = fresh.id
      }
      e.answer = body
      e.declinedDetour = undefined
      e.plan = await decorate(raw, body.pickup_mode, e.rider)
      const q = status.confirmation_question
      return {
        ...e.plan,
        ride_id: id,
        needs_rider_confirmation: !!q,
        rider_message: q ?? e.plan.rider_message,
        degraded_note: riderFacingNote(status.degraded_note),
      }
    },

    async confirm(id, accept_detour) {
      const e = entry(id)
      e.declinedDetour = !accept_detour
      const raw = await call<RidePlan>('POST', `/rides/${e.previewId ?? id}/confirm`, { accept_detour })
      e.plan = await decorate(raw, e.answer?.pickup_mode, e.rider)
      return { ...e.plan, ride_id: id }
    },

    async dispatch(id) {
      const prev = entry(id)
      if (!prev.answer) throw new Error('Choose a pickup option first')
      // The preview ride's car has been driving since /answer; start a fresh one for the real trip.
      const fresh = await createAnswered(prev.rider, prev.answer)
      const e = entry(fresh.id)
      e.declinedDetour = prev.declinedDetour
      let plan = fresh.plan
      if (fresh.status.confirmation_question && prev.declinedDetour !== undefined) {
        plan = await call<RidePlan>('POST', `/rides/${fresh.id}/confirm`, { accept_detour: !prev.declinedDetour })
      }
      e.plan = await decorate(plan, prev.answer.pickup_mode, prev.rider)

      if (plan.phase === 'predicted') {
        return backendState(fresh.id, await call<RideStatus>('GET', `/rides/${fresh.id}`))
      }
      // No mobility needs: the orchestrator confirmed the spot but won't move the car. Drive it here.
      const path = plan.route_polyline ? decode(plan.route_polyline) : []
      const driver = new LocalDriver(path, 0)
      const realSpeed = plan.eta_s > 0 ? driver.totalM / plan.eta_s : 11
      e.local = { driver: new LocalDriver(path, realSpeed * SIM_SPEEDUP, 1 / realSpeed), trip: 'to_pickup', plan: e.plan }
      return localState(fresh.id, e)
    },

    async getRide(id) {
      const e = entry(id)
      if (e.local) return localState(id, e)
      return backendState(id, await call<RideStatus>('GET', `/rides/${id}`))
    },

    async skipToArrival(id) {
      const e = entry(id)
      if (e.local) {
        e.local.driver.jumpToRemaining(APPROACH_M - 1)
        return localState(id, e)
      }
      // The orchestrator jumps the car to ~135 m out and confirms the spot, but cancels its simulator
      // without restarting it, so the car would stop there. Drive the last stretch here.
      const status = await call<RideStatus>('POST', `/rides/${id}/skip_to_arrival`)
      const state = await backendState(id, status)
      const route = status.plan.route_polyline ? decode(status.plan.route_polyline) : []
      if (route.length > 1 && status.remaining_m > 1) {
        const rest = pathFromDistance(route, distanceAlong(route, status.car_position))
        const realSpeed = status.remaining_eta_s > 0 ? status.remaining_m / status.remaining_eta_s : 11
        e.local = { driver: new LocalDriver(rest, realSpeed * SIM_SPEEDUP, 1 / realSpeed), trip: 'to_pickup', plan: { ...e.plan!, ...state } }
        return localState(id, e)
      }
      return state
    },

    async startTrip(id, destination) {
      const e = entry(id)
      const spot = e.plan?.final_spot ?? e.plan?.predicted_spot
      const from = spot?.spot.stop_point ?? e.rider
      const routed = await fetchRoute('driving', from, destination.location)
      const path = routed?.path ?? [from, destination.location]
      const probe = new LocalDriver(path, 0)
      const realSpeed = routed && routed.duration_s > 0 ? probe.totalM / routed.duration_s : 11
      const screenSpeed = Math.max(realSpeed * SIM_SPEEDUP, probe.totalM / TRIP_MAX_SCREEN_S)
      const plan = { ...(e.plan as RidePlan), route_polyline: encode(path), rider_message: '' }
      e.local = { driver: new LocalDriver(path, screenSpeed, 1 / realSpeed), trip: 'to_destination', plan }
      return localState(id, e)
    },
  }
}
