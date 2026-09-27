// The frontend's only door to the backend. Everything goes through the orchestrator (ENDPOINT.md §7).
// Until the orchestrator exists, `api` is the in-browser placeholder. To switch to the real one,
// set VITE_USE_PLACEHOLDER=0 and VITE_API_URL=http://localhost:8000 in src/app/frontend/.env.

import { placeholderApi } from './placeholder'
import type { AnswerBody, LatLng, RidePlan, RideRequestResponse, RideState } from './types'

export interface RideApi {
  /** POST /rides/request → calls S1 for legal spots, returns the comfort question. */
  requestRide(rider_location: LatLng, car_start?: LatLng): Promise<RideRequestResponse>
  /** POST /rides/{id}/answer → runs the predictive phase (S2), returns the plan. */
  answer(ride_id: string, body: AnswerBody): Promise<RidePlan>
  /** POST /rides/{id}/confirm → only when the plan sets needs_rider_confirmation. */
  confirm(ride_id: string, accept_detour: boolean): Promise<RidePlan>
  /** POST /rides/{id}/dispatch → not in ENDPOINT.md yet; lets the rider confirm before the car leaves. */
  dispatch(ride_id: string): Promise<RideState>
  /** GET /rides/{id} → polled every second for car position and phase. */
  getRide(ride_id: string): Promise<RideState>
  /** POST /rides/{id}/skip_to_arrival → demo shortcut. */
  skipToArrival(ride_id: string): Promise<RideState>
  /** POST /rides/{id}/start_trip → not in ENDPOINT.md yet; rider is in the car. */
  startTrip(ride_id: string, destination: { place_id: string; location: LatLng }): Promise<RideState>
}

export function httpApi(baseUrl: string): RideApi {
  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${baseUrl}${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!res.ok) throw new Error(`${method} ${path} failed with ${res.status}`)
    return res.json() as Promise<T>
  }
  return {
    requestRide: (rider_location, car_start) => call('POST', '/rides/request', { rider_location, car_start }),
    answer: (id, body) => call('POST', `/rides/${id}/answer`, body),
    confirm: (id, accept_detour) => call('POST', `/rides/${id}/confirm`, { accept_detour }),
    dispatch: (id) => call('POST', `/rides/${id}/dispatch`),
    getRide: (id) => call('GET', `/rides/${id}`),
    skipToArrival: (id) => call('POST', `/rides/${id}/skip_to_arrival`),
    startTrip: (id, destination) => call('POST', `/rides/${id}/start_trip`, { destination }),
  }
}

const usePlaceholder = import.meta.env.VITE_USE_PLACEHOLDER !== '0'
export const api: RideApi = usePlaceholder
  ? placeholderApi
  : httpApi(import.meta.env.VITE_API_URL ?? 'http://localhost:8000')
export const USING_PLACEHOLDER = usePlaceholder
