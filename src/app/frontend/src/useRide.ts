// Ride flow state. Every backend interaction goes through `api` (src/api/client.ts), following
// the orchestrator flow in ENDPOINT.md §3: request → legal spots → comfort question → plan →
// dispatch → poll car position → arrival → trip.

import polyline from '@mapbox/polyline'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api/client'
import { CAR_START, RIDER_LOCATION } from './api/fixtures'

const decode = (encoded: string): LatLng[] => polyline.decode(encoded).map(([lat, lng]) => ({ lat, lng }))
import type { Condition, LatLng, PickupMode, Place, RidePlan, RideState, Spot } from './api/types'

export type Stage =
  | 'home' | 'search' | 'requesting' | 'comfort' | 'planning' | 'detour' | 'confirm'
  | 'dispatching' | 'enroute' | 'arrived' | 'ontrip' | 'complete'

export interface DemoSettings {
  forceCondition: Condition | null // sent as force_condition; null = real weather
  alwaysAsk: boolean
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
  const [settings, setSettings] = useState<DemoSettings>({ forceCondition: 'rain', alwaysAsk: true })
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

  const runPlan = useCallback((id: string, mode: PickupMode) => {
    setStage((s) => (s === 'confirm' ? s : 'planning'))
    const body = { mobility_needs: mode !== 'standard', pickup_mode: mode, force_condition: mode === 'weather' ? settings.forceCondition : null }
    guard(api.answer(id, body), (p) => {
      setPlan(p)
      setStage(p.needs_rider_confirmation ? 'detour' : 'confirm')
    })
  }, [guard, settings.forceCondition])

  // ---- flow actions -------------------------------------------------------

  const openSearch = useCallback(() => setStage('search'), [])

  const chooseDestination = useCallback((place: Place) => {
    seq.current++
    setError(null)
    setDestination(place)
    setSpots([])
    setPlan(null)
    setStage('requesting')
    guard(api.requestRide(RIDER_LOCATION, CAR_START), (res) => {
      setRideId(res.ride_id)
      setSpots(res.spots ?? [])
      setQuestion(res.question)
      const pref = loadPref()
      if (settings.alwaysAsk || pref === null) setStage('comfort')
      else runPlan(res.ride_id, pref)
    })
  }, [guard, runPlan, settings.alwaysAsk])

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
    guard(api.dispatch(rideId), (r) => { setRide(r); setStage('enroute') })
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
    seq.current++
    setSpots([])
    setPlan(null)
    setStage('search')
  }, [stage])

  // Re-plan when the demo weather override changes while the rider is still choosing.
  const lastForce = useRef(settings.forceCondition)
  useEffect(() => {
    if (lastForce.current === settings.forceCondition) return
    lastForce.current = settings.forceCondition
    if (stage === 'confirm' && rideId && pickupMode === 'weather') runPlan(rideId, pickupMode)
  }, [settings.forceCondition, stage, rideId, pickupMode, runPlan])

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
    () => (activeSpot?.walk_polyline ? decode(activeSpot.walk_polyline) : activeSpot ? [RIDER_LOCATION, activeSpot.spot.stop_point] : []),
    [activeSpot],
  )

  return {
    settings, setSettings,
    weatherCondition,
    stage, destination, pickupMode, spots, question, plan: current, ride, activeSpot, route, walk, error, feedback,
    rider: RIDER_LOCATION,
    openSearch, chooseDestination, choosePickupMode, answerDetour, confirmPickup, startTrip, skipToArrival,
    reset, back, setPickupMode, setFeedback,
  }
}

export type Ride = ReturnType<typeof useRide>
