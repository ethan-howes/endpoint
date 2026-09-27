// The frontend's only door to the backend. Everything goes through the orchestrator (ENDPOINT.md §7)
// via the adapter in orchestrator.ts. To run without the backend, set VITE_USE_PLACEHOLDER=1 in
// frontend/.env and the in-browser placeholder (placeholder.ts) is used instead.

import { orchestratorApi } from './orchestrator'
import { placeholderApi } from './placeholder'
import type { AnswerBody, LatLng, RidePlan, RideRequestResponse, RideState } from './types'

export interface RideApi {
  /** POST /rides/request. The orchestrator starts S1 in the background; spots arrive with the plan. */
  requestRide(rider_location: LatLng, car_start?: LatLng): Promise<RideRequestResponse>
  /** POST /rides/{id}/answer: runs S1 + S2 and returns the plan. Called again when the rider switches options. */
  answer(ride_id: string, body: AnswerBody): Promise<RidePlan>
  /** POST /rides/{id}/confirm: the detour answer, only when the plan asked for one. */
  confirm(ride_id: string, accept_detour: boolean): Promise<RidePlan>
  /** Rider tapped Confirm pickup. No orchestrator route yet; the adapter starts a fresh ride. Returns the ride to poll. */
  dispatch(ride_id: string): Promise<RideState>
  /** GET /rides/{id}: polled every second for car position and phase. */
  getRide(ride_id: string): Promise<RideState>
  /** POST /rides/{id}/skip_to_arrival: demo shortcut. */
  skipToArrival(ride_id: string): Promise<RideState>
  /** Rider is in the car. No orchestrator route yet; the adapter routes and drives this leg itself. */
  startTrip(ride_id: string, destination: { place_id: string; location: LatLng }): Promise<RideState>
}

const usePlaceholder = import.meta.env.VITE_USE_PLACEHOLDER === '1'
export const API_URL: string = import.meta.env.VITE_API_URL || 'http://localhost:8000'
export const api: RideApi = usePlaceholder ? placeholderApi : orchestratorApi(API_URL)
export const USING_PLACEHOLDER = usePlaceholder
