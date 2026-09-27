import type { LatLng } from '../api/types'

const R = 6371000
const rad = (d: number) => (d * Math.PI) / 180

/** Great-circle distance in meters. */
export function distanceM(a: LatLng, b: LatLng): number {
  const dLat = rad(b.lat - a.lat)
  const dLng = rad(b.lng - a.lng)
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

/** Compass bearing from a to b, degrees clockwise from north. */
export function bearingDeg(a: LatLng, b: LatLng): number {
  const y = Math.sin(rad(b.lng - a.lng)) * Math.cos(rad(b.lat))
  const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lng - a.lng))
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360
}

export function pathLengthM(path: LatLng[]): number {
  let total = 0
  for (let i = 1; i < path.length; i++) total += distanceM(path[i - 1], path[i])
  return total
}

/** Position and heading at distance d (meters) along a path. */
export function pointAlong(path: LatLng[], d: number): { position: LatLng; heading: number } {
  let remaining = Math.max(0, d)
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1]
    const b = path[i]
    const seg = distanceM(a, b)
    if (remaining <= seg && seg > 0) {
      const t = remaining / seg
      return { position: { lat: a.lat + (b.lat - a.lat) * t, lng: a.lng + (b.lng - a.lng) * t }, heading: bearingDeg(a, b) }
    }
    remaining -= seg
  }
  const n = path.length
  return { position: path[n - 1], heading: n > 1 ? bearingDeg(path[n - 2], path[n - 1]) : 0 }
}

/**
 * How far along a path (meters) the closest point to `at` lies. Pass `from` (meters) to ignore
 * earlier parts of the path, so a route that doubles back doesn't snap the car backwards.
 */
export function distanceAlong(path: LatLng[], at: LatLng, from = 0): number {
  if (path.length < 2) return 0
  // Local flat projection is plenty accurate at street scale.
  const k = Math.cos(rad(at.lat))
  const toXY = (p: LatLng) => ({ x: p.lng * k * 111320, y: p.lat * 111320 })
  const q = toXY(at)
  let best = Infinity
  let bestAlong = 0
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const a = toXY(path[i - 1])
    const b = toXY(path[i])
    const dx = b.x - a.x
    const dy = b.y - a.y
    const seg = Math.hypot(dx, dy)
    if (acc + seg < from - 25) { acc += seg; continue }
    const t = seg ? Math.max(0, Math.min(1, ((q.x - a.x) * dx + (q.y - a.y) * dy) / (seg * seg))) : 0
    const d = Math.hypot(a.x + dx * t - q.x, a.y + dy * t - q.y)
    if (d < best) { best = d; bestAlong = acc + seg * t }
    acc += seg
  }
  return bestAlong
}

/** The part of a path from distance d (meters) to the end. */
export function pathFromDistance(path: LatLng[], d: number): LatLng[] {
  let acc = 0
  for (let i = 1; i < path.length; i++) {
    const seg = distanceM(path[i - 1], path[i])
    if (acc + seg >= d) return [pointAlong(path, d).position, ...path.slice(i)]
    acc += seg
  }
  return path.slice(-1)
}
