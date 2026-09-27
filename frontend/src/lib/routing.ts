// Road and footpath routes fetched from the browser, for the legs the orchestrator doesn't route yet
// (the rider's walk to the pickup, and the trip to the destination). Both public OSRM servers
// allow cross-origin requests. Results are cached; failures return null so callers can fall back.

import polyline from '@mapbox/polyline'
import type { LatLng } from '../api/types'

export type Profile = 'driving' | 'foot'

const BASE: Record<Profile, string> = {
  driving: 'https://router.project-osrm.org/route/v1/driving',
  foot: 'https://routing.openstreetmap.de/routed-foot/route/v1/driving',
}

export interface FetchedRoute {
  path: LatLng[]
  distance_m: number
  duration_s: number
}

const cache = new Map<string, Promise<FetchedRoute | null>>()
const key = (p: Profile, a: LatLng, b: LatLng) => `${p}:${a.lat.toFixed(5)},${a.lng.toFixed(5)}>${b.lat.toFixed(5)},${b.lng.toFixed(5)}`

export function fetchRoute(profile: Profile, from: LatLng, to: LatLng, timeoutMs = 6000): Promise<FetchedRoute | null> {
  const k = key(profile, from, to)
  const hit = cache.get(k)
  if (hit) return hit
  const p = (async () => {
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      // OSRM wants lng,lat.
      const url = `${BASE[profile]}/${from.lng},${from.lat};${to.lng},${to.lat}?overview=full&geometries=polyline`
      const res = await fetch(url, { signal: ctrl.signal })
      if (!res.ok) return null
      const body = await res.json()
      const r = body?.routes?.[0]
      if (body?.code !== 'Ok' || !r?.geometry) return null
      const path = polyline.decode(r.geometry).map(([lat, lng]) => ({ lat, lng }))
      return path.length > 1 ? { path, distance_m: r.distance, duration_s: r.duration } : null
    } catch {
      return null
    } finally {
      clearTimeout(t)
    }
  })()
  cache.set(k, p)
  // Don't cache failures, so a flaky network can recover on the next ride.
  p.then((r) => { if (!r) cache.delete(k) })
  return p
}
