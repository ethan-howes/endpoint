// Ride flow state. Every backend interaction goes through `api` (src/api/client.ts), following
// the orchestrator flow in ENDPOINT.md §3: request → legal spots → comfort question → plan →
// dispatch → poll car position → arrival → trip.

import polyline from '@mapbox/polyline'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api/client'
import { CAR_START, RIDER_ADDRESS, RIDER_LOCATION } from './api/fixtures'
import type { Condition, LatLng, PickupMode, Place, RidePlan, RideState, Spot } from './api/types'
import { reverseGeocode } from './lib/geocode'

const decode = (encoded: string): LatLng[] => polyline.decode(encoded).map(([lat, lng]) => ({ lat, lng }))

export type Stage =
  | 'home' | 'search' | 'pickPin' | 'requesting' | 'comfort' | 'planning' | 'detour' | 'confirm'
  | 'dispatching' | 'enroute' | 'arrived' | 'ontrip' | 'complete'

/** Where the rider wants to be picked up. Sent to the orchestrator as rider_location. */
export interface Pickup {
  label: string
  address: string
  location: LatLng
}

export const DEFAULT_PICKUP: Pickup = { label: 'Current location', address: RIDER_ADDRESS, location: RIDER_LOCATION }

export interface DemoSettings {
  forceCondition: Condition | null // sent as force_condition; null = real weather
  sunTime: SunTime // sent as force_time when forcing sun, so shade works at any hour
  alwaysAsk: boolean
}

export type SunTime = 'now' | 'morning' | 'afternoon'

/**
 * force_time for the sun demo. On campus (UTC-4 in EDT), 14:00Z is 10 AM and 20:00Z is 4 PM:
 * shadows fall on opposite sides of the street (see .env.example in the repo root).
 */
function sunForceTime(sunTime: SunTime): string | null {
  if (sunTime === 'now') return null
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date())
  return `${today}T${sunTime === 'morning' ? '14' : '20'}:00:00Z`
}

const POLL_MS = 1000 // ENDPOINT.md §7: the UI polls GET /rides/{id} every second

const PREF_KEY = 'endpoint.pickupMode'
const MODES: PickupMode[] = ['accessible', 'weather', 'standard']
function loadPref(): PickupMode | null {
  try {
    const raw = localStorage.getItem(PREF_KEY) as PickupMode | null
    return raw && MODES.includes(raw) ? raw : null
  } catch {
    return null
  }
}
function savePref(value: PickupMode) {
  try { localStorage.setItem(PREF_KEY, String(value)) } catch { /* storage unavailable */ }
}

export function useRide() {
  const [settings, setSettings] = useState<DemoSettings>({ forceCondition: 'rain', sunTime: 'afternoon', alwaysAsk: true })
  const [stage, setStage] = useState<Stage>('home')
  const [destination, setDestination] = useState<Place | null>(null)
  const [pickupMode, setPickupModeState] = useState<PickupMode | null>(loadPref)
  const [rideId, setRideId] = useState<string | null>(null)
  const [spots, setSpots] = useState<Spot[]>([])
  const [question, setQuestion] = useState('')
  const [plan, setPlan] = useState<RidePlan | null>(null)
  const [ride, setRide] = useState<RideState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const [updating, setUpdating] = useState(false)
  const [pickup, setPickup] = useState<Pickup>(DEFAULT_PICKUP)
  // Map centre while the rider is placing a pin ("Choose on map").
  const [pinDraft, setPinDraft] = useState<LatLng | null>(null)

  // Bumped whenever the flow restarts so late responses from an abandoned ride are ignored.
  const seq = useRef(0)
  const guard = useCallback(<T,>(p: Promise<T>, onOk: (v: T) => void) => {
    const mine = seq.current
    p.then((v) => { if (seq.current === mine) onOk(v) })
      .catch((e: unknown) => { if (seq.current === mine) setError(e instanceof Error ? e.message : String(e)) })
  }, [])

  const setPickupMode = useCallback((v: PickupMode) => {
    setPickupModeState(v)
    savePref(v)
  }, [])

  // Latest re-plan wins: switching options or weather quickly must not let a slow, older answer
  // overwrite a newer one (S2 can take several seconds, especially for sun/shade).
  const planToken = useRef(0)
  const runPlan = useCallback((id: string, mode: PickupMode) => {
    const token = ++planToken.current
    setUpdating(true)
    setStage((s) => (s === 'confirm' ? s : 'planning'))
    const needs = mode !== 'standard'
    const body = {
      mobility_needs: needs,
      pickup_mode: mode,
      force_condition: needs ? settings.forceCondition : null,
      force_time: needs && settings.forceCondition === 'sun' ? sunForceTime(settings.sunTime) : null,
    }
    guard(api.answer(id, body), (p) => {
      if (token !== planToken.current) return
      setUpdating(false)
      setPlan(p)
      setStage(p.needs_rider_confirmation ? 'detour' : 'confirm')
    })
  }, [guard, settings.forceCondition, settings.sunTime])

  // ---- flow actions -------------------------------------------------------

  // Which field the search screen focuses: the destination (default) or the pickup.
  const [searchFocus, setSearchFocus] = useState<'pickup' | 'destination'>('destination')
  const openSearch = useCallback((focus: 'pickup' | 'destination' = 'destination') => {
    setSearchFocus(focus)
    setStage('search')
  }, [])

  const startPinPick = useCallback(() => {
    setPinDraft(pickup.location)
    setStage('pickPin')
  }, [pickup.location])

  const confirmPin = useCallback(async () => {
    const at = pinDraft ?? pickup.location
    setSearchFocus('destination')
    setStage('search')
    const coords = `${at.lat.toFixed(5)}, ${at.lng.toFixed(5)}`
    setPickup({ label: 'Pin on map', address: coords, location: at })
    // Fill in a street address when the geocoder answers; the pin is usable either way.
    const address = await reverseGeocode(at)
    if (address) setPickup((p) => (p.location === at ? { ...p, address } : p))
  }, [pinDraft, pickup.location])

  const chooseDestination = useCallback((place: Place) => {
    seq.current++
    setError(null)
    setDestination(place)
    setSpots([])
    setPlan(null)
    setStage('requesting')
    guard(api.requestRide(pickup.location, CAR_START), (res) => {
      setRideId(res.ride_id)
      setSpots(res.spots ?? [])
      setQuestion(res.question)
      const pref = loadPref()
      if (settings.alwaysAsk || pref === null) setStage('comfort')
      else runPlan(res.ride_id, pref)
    })
  }, [guard, runPlan, settings.alwaysAsk, pickup.location])

  const choosePickupMode = useCallback((mode: PickupMode) => {
    if (!rideId) return
    setPickupMode(mode)
    runPlan(rideId, mode)
  }, [rideId, runPlan, setPickupMode])

  const answerDetour = useCallback((accept: boolean) => {
    if (!rideId) return
    guard(api.confirm(rideId, accept), (p) => { setPlan(p); setStage('confirm') })
  }, [guard, rideId])

  const confirmPickup = useCallback(() => {
    if (!rideId) return
    setStage('dispatching')
    // The adapter may hand back a different ride to poll (see api/orchestrator.ts).
    guard(api.dispatch(rideId), (r) => { setRideId(r.ride_id); setRide(r); setStage('enroute') })
  }, [guard, rideId])

  const startTrip = useCallback(() => {
    if (!rideId || !destination) return
    guard(api.startTrip(rideId, { place_id: destination.id, location: destination.location }), (r) => { setRide(r); setStage('ontrip') })
  }, [guard, rideId, destination])

  const skipToArrival = useCallback(() => {
    if (!rideId) return
    guard(api.skipToArrival(rideId), setRide)
  }, [guard, rideId])

  const reset = useCallback(() => {
    seq.current++
    setUpdating(false)
    setStage('home')
    setDestination(null)
    setRideId(null)
    setSpots([])
    setPlan(null)
    setRide(null)
    setError(null)
    setFeedback(null)
  }, [])

  const back = useCallback(() => {
    if (stage === 'search') { setStage('home'); return }
    if (stage === 'pickPin') { setStage('search'); return }
    seq.current++
    setSpots([])
    setPlan(null)
    setStage('search')
  }, [stage])

  // Re-plan when the demo weather override changes while the rider is still choosing.
  const forceKey = `${settings.forceCondition}|${settings.sunTime}`
  const lastForce = useRef(forceKey)
  useEffect(() => {
    if (lastForce.current === forceKey) return
    lastForce.current = forceKey
    // Weather only changes the plan for pickups with mobility needs (accessible and weather).
    if (stage === 'confirm' && rideId && pickupMode && pickupMode !== 'standard') runPlan(rideId, pickupMode)
  }, [forceKey, stage, rideId, pickupMode, runPlan])

  // Poll the ride while the car is moving.
  useEffect(() => {
    if (!rideId || (stage !== 'enroute' && stage !== 'ontrip')) return
    const t = setInterval(() => {
      guard(api.getRide(rideId), (r) => {
        setRide(r)
        if (stage === 'enroute' && r.trip_status === 'at_pickup') setStage('arrived')
        if (stage === 'ontrip' && r.trip_status === 'completed') setStage('complete')
      })
    }, POLL_MS)
    return () => clearInterval(t)
  }, [rideId, stage, guard])

  // ---- derived ------------------------------------------------------------

  const current: RidePlan | null = ride ?? plan
  const activeSpot = current?.final_spot ?? current?.predicted_spot ?? null
  // What the map shows: the plan's weather when S2 sent one, otherwise the demo override.
  const weatherCondition: Condition | null = current?.weather?.condition ?? settings.forceCondition
  const route = useMemo<LatLng[]>(() => (current?.route_polyline ? decode(current.route_polyline) : []), [current?.route_polyline])
  const walk = useMemo<LatLng[]>(
    () => (activeSpot?.walk_polyline ? decode(activeSpot.walk_polyline) : activeSpot ? [pickup.location, activeSpot.spot.stop_point] : []),
    [activeSpot, pickup.location],
  )

  return {
    settings, setSettings,
    weatherCondition,
    updating: updating && !error,
    // The orchestrator returns legal spots with the plan (as candidates), not with the request.
    spots: spots.length ? spots : (current?.candidates ?? []).map((c) => c.spot),
    stage, destination, pickupMode, question, plan: current, ride, activeSpot, route, walk, error, feedback,
    rider: pickup.location,
    pickup, setPickup, searchFocus, pinDraft, setPinDraft, startPinPick, confirmPin,
    openSearch, chooseDestination, choosePickupMode, answerDetour, confirmPickup, startTrip, skipToArrival,
    reset, back, setPickupMode, setFeedback,
  }
}

export type Ride = ReturnType<typeof useRide>
