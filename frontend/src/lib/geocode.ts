// Address search and reverse geocoding for the pickup field, via OpenStreetMap's Nominatim.
// Nominatim's usage policy allows light use (about 1 request per second, cached), which a
// debounced search box stays within. Failures return empty results so the UI falls back to presets.

import type { LatLng } from '../api/types'

const BASE = 'https://nominatim.openstreetmap.org'
/** Search around FIU and the surrounding area (west, north, east, south). */
const VIEWBOX = '-80.45,25.83,-80.25,25.68'

export interface GeocodeResult {
  name: string
  address: string
  location: LatLng
}

const cache = new Map<string, Promise<unknown>>()

async function getJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const hit = cache.get(url)
  if (hit) return hit
  const p = fetch(url, { signal, headers: { Accept: 'application/json' } }).then((r) => (r.ok ? r.json() : null))
  cache.set(url, p)
  p.catch(() => cache.delete(url))
  return p
}

interface NominatimPlace { lat: string; lon: string; name?: string; display_name: string }

const shorten = (displayName: string) => displayName.split(', ').slice(0, 3).join(', ')

export async function searchAddress(query: string, signal?: AbortSignal): Promise<GeocodeResult[]> {
  if (query.trim().length < 3) return []
  const params = new URLSearchParams({ q: query, format: 'jsonv2', limit: '6', viewbox: VIEWBOX, bounded: '1' })
  try {
    const rows = (await getJson(`${BASE}/search?${params}`, signal)) as NominatimPlace[] | null
    return (rows ?? []).map((r) => ({
      name: r.name || r.display_name.split(', ')[0],
      address: shorten(r.display_name),
      location: { lat: Number(r.lat), lng: Number(r.lon) },
    }))
  } catch {
    return []
  }
}

export async function reverseGeocode(at: LatLng): Promise<string | null> {
  const params = new URLSearchParams({ lat: at.lat.toFixed(6), lon: at.lng.toFixed(6), format: 'jsonv2', zoom: '18' })
  try {
    const r = (await getJson(`${BASE}/reverse?${params}`)) as NominatimPlace | null
    return r?.display_name ? shorten(r.display_name) : null
  } catch {
    return null
  }
}
