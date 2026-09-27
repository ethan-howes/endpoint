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

/**
 * UTM (WGS84) easting/northing in meters -> lat/lng. S2 currently writes cover geometry in the
 * local UTM zone (EPSG:326xx) rather than lng/lat, so the map converts it back here.
 */
export function utmToLatLng(easting: number, northing: number, zone: number, northern = true): LatLng {
  const a = 6378137
  const f = 1 / 298.257223563
  const k0 = 0.9996
  const e2 = f * (2 - f)
  const ep2 = e2 / (1 - e2)
  const x = easting - 500000
  const y = northern ? northing : northing - 10000000
  const m = y / k0
  const mu = m / (a * (1 - e2 / 4 - (3 * e2 ** 2) / 64 - (5 * e2 ** 3) / 256))
  const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2))
  const phi1 = mu
    + ((3 * e1) / 2 - (27 * e1 ** 3) / 32) * Math.sin(2 * mu)
    + ((21 * e1 ** 2) / 16 - (55 * e1 ** 4) / 32) * Math.sin(4 * mu)
    + ((151 * e1 ** 3) / 96) * Math.sin(6 * mu)
  const n1 = a / Math.sqrt(1 - e2 * Math.sin(phi1) ** 2)
  const t1 = Math.tan(phi1) ** 2
  const c1 = ep2 * Math.cos(phi1) ** 2
  const r1 = (a * (1 - e2)) / (1 - e2 * Math.sin(phi1) ** 2) ** 1.5
  const d = x / (n1 * k0)
  const lat = phi1 - ((n1 * Math.tan(phi1)) / r1) * (
    d ** 2 / 2
    - ((5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4) / 24
    + ((61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6) / 720
  )
  const lng0 = rad((zone - 1) * 6 - 180 + 3)
  const lng = lng0 + (
    d
    - ((1 + 2 * t1 + c1) * d ** 3) / 6
    + ((5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5) / 120
  ) / Math.cos(phi1)
  return { lat: (lat * 180) / Math.PI, lng: (lng * 180) / Math.PI }
}

export const utmZoneOf = (p: LatLng) => Math.floor((p.lng + 180) / 6) + 1
