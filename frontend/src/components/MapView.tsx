import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useEffect, useRef } from 'react'
import { CircleMarker, GeoJSON, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from 'react-leaflet'
import { SHADEMAP_KEY, isPlaceholder } from '../api/dataSources'
import { DEFAULT_ZOOM, MAP_CENTER } from '../api/fixtures'
import type { CoverFeature, LatLng } from '../api/types'
import { distanceAlong, pathFromDistance, pathLengthM, pointAlong, utmToLatLng, utmZoneOf } from '../lib/geo'
import type { Ride } from '../useRide'
import { PlaceholderTag } from './Placeholder'
import { WeatherOverlay } from './WeatherOverlay'

const ll = (p: LatLng): [number, number] => [p.lat, p.lng]

const riderIcon = L.divIcon({ className: '', html: '<div class="m-rider"><i></i></div>', iconSize: [36, 36], iconAnchor: [18, 18] })
const pickupIcon = L.divIcon({ className: '', html: '<div class="m-pickup"></div>', iconSize: [28, 38], iconAnchor: [14, 38] })
const destIcon = L.divIcon({ className: '', html: '<div class="m-dest"></div>', iconSize: [18, 18], iconAnchor: [9, 9] })
const rampIcon = L.divIcon({ className: '', html: '<div class="m-ramp" title="Curb ramp"></div>', iconSize: [18, 18], iconAnchor: [9, 9] })
const carIcon = L.divIcon({ className: '', html: '<div class="m-car"><div class="m-car-body"></div></div>', iconSize: [24, 36], iconAnchor: [12, 18] })

type Shape = { kind: 'polygon' | 'line'; points: [number, number][] } | { kind: 'point'; points: [number, number][] }

/**
 * Parses the WKT S2 sends for cover features (POINT, LINESTRING, POLYGON and their MULTI forms).
 * Coordinates are "x y": lng/lat per ENDPOINT.md, but S2 currently sends UTM meters, which are
 * detected (|x| > 180) and converted using the zone around `near`. Polygon holes are ignored.
 */
function wktShapes(wkt: string, near: LatLng): Shape[] {
  const type = /^\s*(\w+)/.exec(wkt)?.[1]?.toUpperCase() ?? ''
  const zone = utmZoneOf(near)
  const coords = (text: string): [number, number][] =>
    text.split(',').map((pair) => {
      const [x, y] = pair.trim().split(/\s+/).map(Number)
      if (Math.abs(x) > 180 || Math.abs(y) > 90) {
        const p = utmToLatLng(x, y, zone, near.lat >= 0)
        return [p.lat, p.lng] as [number, number]
      }
      return [y, x] as [number, number]
    }).filter(([lat, lng]) => Number.isFinite(lat) && Number.isFinite(lng))
  // Innermost parenthesised coordinate lists, e.g. "(x y, x y)".
  const groups = [...wkt.matchAll(/\(([^()]+)\)/g)].map((m) => coords(m[1]))
  if (type.includes('POLYGON')) {
    // First ring of each polygon: for MULTIPOLYGON, rings that start a "((" group.
    const outer = type === 'POLYGON' ? groups.slice(0, 1) : [...wkt.matchAll(/\(\(([^()]+)\)/g)].map((m) => coords(m[1]))
    return outer.filter((g) => g.length > 2).map((points) => ({ kind: 'polygon', points }))
  }
  if (type.includes('LINESTRING')) return groups.filter((g) => g.length > 1).map((points) => ({ kind: 'line', points }))
  if (type.includes('POINT')) return groups.flat().map((pt) => ({ kind: 'point', points: [pt] }))
  return []
}

const COVER_STYLE = { color: '#0a8f7c', fillColor: '#0fb9a0', fillOpacity: 0.4, weight: 1.5 }

/** Fits the map to whatever the current stage is about, leaving room for the demo panel. */
function Framer({ points, insetRight, follow, stage }: { points: LatLng[]; insetRight: number; follow: boolean; stage: string }) {
  const map = useMap()
  // Rounded (~100 m) so a moving car re-frames the map in steps rather than every poll.
  const key = points.map((p) => `${p.lat.toFixed(3)},${p.lng.toFixed(3)}`).join('|')
  useEffect(() => {
    if (!points.length) return
    const opts = { paddingTopLeft: L.point(48, 48), paddingBottomRight: L.point(48 + insetRight, 48), maxZoom: 18, duration: 0.8 }
    // While driving, only move the camera when something is about to leave the view.
    if (follow) {
      const size = map.getSize()
      const safe = L.bounds(L.point(40, 40), L.point(size.x - 40 - insetRight, size.y - 40))
      if (points.every((p) => safe.contains(map.latLngToContainerPoint(ll(p))))) return
    }
    if (points.length === 1) map.flyTo(ll(points[0]), DEFAULT_ZOOM, { duration: 0.8 })
    else map.flyToBounds(L.latLngBounds(points.map(ll)), opts)
  }, [key, insetRight, map, follow, stage])
  return null
}

const POLL_S = 1 // matches the ride poll interval in useRide

/**
 * Car marker that drives along the route between polls instead of jumping or cutting corners.
 * Each poll sets a target distance along the path; every frame the car eases toward it and
 * turns smoothly to face the road ahead.
 */
function CarMarker({ position, path, drawRoute }: { position: LatLng; path: LatLng[]; drawRoute: boolean }) {
  const map = useMap()
  const marker = useRef<L.Marker | null>(null)
  const state = useRef({ shown: 0, target: 0, rate: 0, heading: 0, path, drawRoute })
  state.current.drawRoute = drawRoute

  useEffect(() => {
    // The remaining route is trimmed here, in step with the car, so it never lags behind it.
    const casing = L.polyline([], { color: '#ffffff', weight: 9, opacity: 1, interactive: false }).addTo(map)
    const line = L.polyline([], { color: '#0b0d10', weight: 5, opacity: 1, interactive: false }).addTo(map)
    const m = L.marker(ll(position), { icon: carIcon, interactive: false, zIndexOffset: 1000 }).addTo(map)
    marker.current = m
    let raf = 0
    let last = performance.now()
    const tick = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const s = state.current
      if (s.path.length > 1) {
        s.shown = Math.min(s.target, s.shown + s.rate * dt)
        const here = pointAlong(s.path, s.shown)
        // Face a point a few meters ahead so corners are rounded rather than snapped.
        const ahead = pointAlong(s.path, Math.min(s.shown + 6, pathLengthM(s.path)))
        const want = s.shown >= pathLengthM(s.path) - 0.5 ? here.heading : ahead.heading
        const delta = ((want - s.heading + 540) % 360) - 180
        s.heading = (s.heading + delta * Math.min(1, dt * 6) + 360) % 360
        m.setLatLng(ll(here.position))
        const rest = s.drawRoute ? pathFromDistance(s.path, s.shown).map(ll) : []
        casing.setLatLngs(rest)
        line.setLatLngs(rest)
        const el = m.getElement()?.querySelector<HTMLElement>('.m-car')
        if (el) el.style.transform = `rotate(${s.heading}deg)`
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => { cancelAnimationFrame(raf); m.remove(); line.remove(); casing.remove() }
  }, [map])

  // New route (e.g. trip to the destination): jump to where the car is on it.
  useEffect(() => {
    const s = state.current
    s.path = path
    s.shown = s.target = path.length > 1 ? distanceAlong(path, position) : 0
    s.rate = 0
    if (path.length > 1) s.heading = pointAlong(path, s.shown).heading
  }, [path])

  // New poll: aim to arrive at the reported spot just as the next poll lands.
  useEffect(() => {
    const s = state.current
    if (s.path.length < 2) {
      marker.current?.setLatLng(ll(position))
      return
    }
    s.target = Math.max(s.shown, distanceAlong(s.path, position, s.shown))
    s.rate = (s.target - s.shown) / POLL_S
  }, [position.lat, position.lng])

  return null
}

/**
 * Slot for shade from the sun. Two future sources can fill it:
 *  - S2's shade polygons for pickup time (RidePlan.overlays.shade_geojson), drawn here as GeoJSON;
 *  - a browser-side sun shadow layer (e.g. ShadeMap's leaflet-shadow-simulator, keyed by
 *    VITE_SHADEMAP_KEY), which would be added to the map here.
 * Until then it draws nothing and the map shows a "shade layer" placeholder chip.
 */
function ShadeLayer({ geojson }: { geojson: unknown | null | undefined }) {
  if (!geojson) return null
  return (
    <GeoJSON
      data={geojson as GeoJSON.GeoJsonObject}
      style={{ color: '#28345a', weight: 0, fillColor: '#28345a', fillOpacity: 0.28 }}
      interactive={false}
    />
  )
}

/** While placing a pickup pin, reports the map centre (where the fixed pin points) as the map moves. */
function CenterReporter({ onCenter }: { onCenter: (p: LatLng) => void }) {
  const map = useMapEvents({
    move: () => {
      const c = map.getCenter()
      onCenter({ lat: c.lat, lng: c.lng })
    },
  })
  return null
}

export function MapView({ ride, insetRight }: { ride: Ride; insetRight: number }) {
  const { stage, spots, activeSpot, rider, route, walk, destination, plan } = ride
  const accessible = plan?.pickup_mode === 'accessible'
  const ramps = accessible ? activeSpot?.accessibility?.curb_ramps ?? [] : []
  const car = ride.ride?.car_position ?? null
  const choosing = stage === 'comfort' || stage === 'planning' || stage === 'detour' || stage === 'confirm'
  const beforePickup = choosing || stage === 'dispatching' || stage === 'enroute' || stage === 'arrived'
  const driving = stage === 'enroute' || stage === 'ontrip'
  // S2 overlays when the orchestrator passes them through; otherwise the cover attached to each ranked spot.
  const coverFeatures: CoverFeature[] = plan?.overlays?.cover_features?.length
    ? plan.overlays.cover_features
    : [...new Map((plan?.candidates ?? []).flatMap((c) => (c.cover_feature ? [[c.cover_feature.feature_id, c.cover_feature] as const] : []))).values()]
  const degraded = ride.ride?.degraded_note ?? null
  const condition = ride.weatherCondition
  const shadeGeojson = plan?.overlays?.shade_geojson
  const shadeMissing = condition === 'sun' && !shadeGeojson && !SHADEMAP_KEY

  const frame = ((): LatLng[] => {
    switch (stage) {
      case 'home':
      case 'search':
      case 'pickPin':
      case 'requesting':
        return [rider]
      case 'comfort':
      case 'planning':
      case 'detour':
      case 'confirm':
        return stage === 'confirm' && walk.length ? [rider, ...walk] : [rider, ...spots.map((s) => s.stop_point)]
      case 'dispatching':
      case 'enroute':
        return [rider, ...(activeSpot ? [activeSpot.spot.stop_point] : []), ...(car ? [car] : route.slice(0, 1))]
      case 'arrived':
        return [rider, ...(car ? [car] : [])]
      case 'ontrip':
      case 'complete':
        return [...(car ? [car] : []), ...(destination ? [destination.location] : [])]
    }
  })()

  // While driving, CarMarker draws the remaining route itself.
  const routeToDraw = stage === 'confirm' || stage === 'dispatching' ? route : []

  return (
    <div className={`map ${condition ? `map--${condition}` : ''}`}>
      <MapContainer center={ll(MAP_CENTER)} zoom={DEFAULT_ZOOM} zoomControl={false} className="map-leaflet" attributionControl>
        <TileLayer
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          maxZoom={19}
          className="map-tiles"
        />
        <Framer points={frame} insetRight={insetRight} follow={stage === 'enroute' || stage === 'ontrip'} stage={stage} />

        {/* Shade from the sun (S2 shade polygons / sun shadow layer). */}
        {condition === 'sun' && <ShadeLayer geojson={shadeGeojson} />}

        {/* Cover from S2: awnings, canopies, covered walkways, shelters, building passages. */}
        {beforePickup && coverFeatures.flatMap((f) =>
          wktShapes(f.geometry_wkt, rider).map((shape, i) => {
            const k = `${f.feature_id}_${i}`
            const tip = <Tooltip>{f.kind.replace(/_/g, ' ')}{f.confidence !== 'unverified' ? ` · ${f.confidence}` : ''}</Tooltip>
            if (shape.kind === 'polygon') return <Polygon key={k} positions={shape.points} pathOptions={COVER_STYLE}>{tip}</Polygon>
            if (shape.kind === 'line') return <Polyline key={k} positions={shape.points} pathOptions={{ color: '#0fb9a0', weight: 7, opacity: 0.75, lineCap: 'round' }}>{tip}</Polyline>
            return <CircleMarker key={k} center={shape.points[0]} radius={7} pathOptions={COVER_STYLE}>{tip}</CircleMarker>
          }),
        )}

        {routeToDraw.length > 1 && (
          <>
            <Polyline positions={routeToDraw.map(ll)} pathOptions={{ color: '#ffffff', weight: 9, opacity: 1 }} />
            <Polyline positions={routeToDraw.map(ll)} pathOptions={{ color: '#0b0d10', weight: 5, opacity: 1 }} />
          </>
        )}

        {/* S1 legal spots */}
        {beforePickup && stage !== 'arrived' && spots.map((s) => (
          <CircleMarker key={s.spot_id} center={ll(s.stop_point)} radius={5} pathOptions={{ color: '#fff', weight: 1.5, fillColor: '#8b929c', fillOpacity: 1 }}>
            <Tooltip>
              {s.street_name ?? 'Unnamed road'}
              {s.clearance_m != null ? ` · ${Math.round(s.clearance_m)} m from the nearest no-stopping zone` : ''}
            </Tooltip>
          </CircleMarker>
        ))}

        {/* Walking route to the pickup along real footpaths. Accessible pickups get a solid, highlighted sidewalk route. */}
        {activeSpot && beforePickup && stage !== 'arrived' && walk.length > 1 && (
          accessible ? (
            <>
              <Polyline positions={walk.map(ll)} pathOptions={{ color: '#ffffff', weight: 9, opacity: 0.95, lineCap: 'round', lineJoin: 'round' }} />
              <Polyline positions={walk.map(ll)} pathOptions={{ color: '#1a9e5c', weight: 5, lineCap: 'round', lineJoin: 'round' }} />
            </>
          ) : (
            <Polyline positions={walk.map(ll)} pathOptions={{ color: '#2f6bff', weight: 4, dashArray: '1 9', lineCap: 'round' }} />
          )
        )}
        {beforePickup && stage !== 'arrived' && ramps.map((r, i) => (
          <Marker key={i} position={ll(r)} icon={rampIcon}><Tooltip>Curb ramp (placeholder)</Tooltip></Marker>
        ))}
        {activeSpot && beforePickup && <Marker position={ll(activeSpot.spot.stop_point)} icon={pickupIcon} />}

        {destination && (stage === 'ontrip' || stage === 'complete') && <Marker position={ll(destination.location)} icon={destIcon} />}
        {stage === 'pickPin' && <CenterReporter onCenter={ride.setPinDraft} />}
        {stage !== 'ontrip' && stage !== 'complete' && stage !== 'pickPin' && <Marker position={ll(rider)} icon={riderIcon} />}
        {car && stage !== 'complete' && <CarMarker position={car} path={route} drawRoute={driving} />}
      </MapContainer>

      {stage === 'pickPin' && (
        <div className="center-pin" aria-hidden>
          <div className="center-pin-head" />
          <div className="center-pin-stem" />
          <div className="center-pin-dot" />
        </div>
      )}

      <WeatherOverlay condition={condition} />

      {((condition && condition !== 'neutral') || degraded) && (
        <div className="map-chips">
          {condition && condition !== 'neutral' && (
            <span className={`map-chip map-chip--${condition}`}>
              {condition === 'rain' ? 'Raining at pickup' : 'Strong sun at pickup'}
              <PlaceholderTag source="weather" />
            </span>
          )}
          {condition === 'rain' && beforePickup && plan?.mobility_needs && !coverFeatures.length && (
            isPlaceholder('rain_cover')
              ? <span className="map-chip">Rain cover layer <PlaceholderTag source="rain_cover" /></span>
              : <span className="map-chip">No mapped cover near these spots</span>
          )}
          {shadeMissing && <span className="map-chip">Shade layer <PlaceholderTag source="sun_shade" /></span>}
          {degraded && <span className="map-chip map-chip--warn">{degraded}</span>}
        </div>
      )}

      {choosing && (
        <ul className="legend" aria-label="Map legend">
          <li><i className="lg-spot" />Legal pickup spot <PlaceholderTag source="legal_spots" /></li>
          <li><i className="lg-pickup" />Chosen pickup</li>
          {accessible ? (
            <>
              <li><i className="lg-access" />Step-free sidewalk route <PlaceholderTag source="sidewalk" /></li>
              <li><i className="lg-ramp" />Curb ramp <PlaceholderTag source="sidewalk" /></li>
            </>
          ) : (
            <li><i className="lg-walk" />Your walk</li>
          )}
          {condition === 'rain' && <li><i className="lg-cover" />Rain cover <PlaceholderTag source="rain_cover" /></li>}
          {condition === 'sun' && <li><i className="lg-shade" />Shade at pickup time <PlaceholderTag source="sun_shade" /></li>}
        </ul>
      )}
    </div>
  )
}
