import { useEffect, useRef, useState, type ReactNode } from 'react'
import { PICKUP_PRESETS, PLACES, inDemoArea } from '../api/fixtures'
import type { AccessibilityInfo, Confidence, LatLng, PickupMode, Place, RankedSpot, WeatherReport } from '../api/types'
import { searchAddress, type GeocodeResult } from '../lib/geocode'
import { DEFAULT_PICKUP, type Ride } from '../useRide'
import {
  IconAccessible, IconAlert, IconBack, IconBolt, IconCheck, IconClock, IconCloud, IconHome, IconPin, IconRain, IconRamp,
  IconCampus, IconMapPin, IconSearch, IconShield, IconSun, IconThumb, IconUmbrella, IconUnlock, IconWalk, IconWork,
} from './Icons'
import { isPlaceholder } from '../api/dataSources'
import { PlaceholderBox, PlaceholderTag } from './Placeholder'

// Things no service provides yet. Shown as-is so it's obvious they're stand-ins.
const PLACEHOLDER = {
  plate: 'PLATE-000',
  vehicle: 'Vehicle details (placeholder)',
  fare: '$—',
}

const minutes = (s: number) => Math.max(1, Math.round(s / 60))
const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

const CONF_LABEL: Record<Confidence, string> = {
  verified: 'Verified',
  likely: 'Likely',
  detected: 'Seen by camera',
  unverified: 'Unverified',
}

const MODES: { mode: PickupMode; short: string; title: string; blurb: string; icon: ReactNode }[] = [
  { mode: 'accessible', short: 'Accessible', title: 'Accessible pickup', blurb: 'Step-free sidewalk route, curb ramps and a level spot to board', icon: <IconAccessible /> },
  { mode: 'weather', short: 'Weather', title: 'Weather-conscious pickup', blurb: 'Wait under cover when it rains, or in the shade when it’s hot', icon: <IconUmbrella /> },
  { mode: 'standard', short: 'Standard', title: 'Standard pickup', blurb: 'Fastest pickup', icon: <IconBolt /> },
]

function ConfidenceBadge({ c }: { c: Confidence }) {
  return <span className={`badge badge--${c}`}>{CONF_LABEL[c]}</span>
}

/** Weather at pickup from S2. */
function WeatherCard({ weather }: { weather: WeatherReport | null }) {
  if (!weather && !isPlaceholder('weather')) {
    return (
      <div className="weather">
        <IconCloud />
        <div className="grow">
          <strong>Weather unavailable right now</strong>
          <span>The weather service didn't answer in time, so this is the nearest spot.</span>
        </div>
      </div>
    )
  }
  if (!weather || (weather.source === 'placeholder' && !weather.overridden)) {
    return (
      <PlaceholderBox source="weather" title="Weather at pickup">
        <span className="small">Live conditions at pickup time. Use the demo weather control to preview rain or sun.</span>
      </PlaceholderBox>
    )
  }
  const { condition } = weather
  const Icon = condition === 'rain' ? IconRain : condition === 'sun' ? IconSun : IconCloud
  const label = condition === 'rain' ? 'Rain' : condition === 'sun' ? 'Strong sun' : 'No rain or strong sun'
  // A forced (demo) condition carries no forecast numbers, only a reason; fall back to it.
  const detail =
    condition === 'rain' && weather.precip_mm_h > 0 ? `${weather.precip_mm_h} mm/h at pickup`
    : condition === 'sun' && weather.uv_index != null ? `UV ${Math.round(weather.uv_index)}${weather.apparent_temperature_c != null ? ` · feels like ${Math.round(weather.apparent_temperature_c)}°C` : ''}`
    : condition === 'neutral' && weather.cloud_cover_pct != null ? `${weather.cloud_cover_pct}% cloud cover`
    : weather.reason || 'At pickup time'
  return (
    <div className={`weather weather--${condition}`}>
      <Icon />
      <div className="grow">
        <strong>{label}</strong>
        <span>{detail}{weather.overridden ? ' · demo override' : ''}</span>
      </div>
      <PlaceholderTag source="weather" />
    </div>
  )
}

/** Rain cover or sun shade at the chosen spot, depending on the weather at pickup. */
function ConditionsCard({ spot, weather }: { spot: RankedSpot; weather: WeatherReport | null }) {
  const condition = weather?.condition
  if (condition === 'rain') {
    if (!spot.cover_feature && !isPlaceholder('rain_cover')) {
      return (
        <div className="card">
          <div className="card-row">
            <IconUmbrella />
            <div className="grow">
              <strong>No mapped cover near this spot</strong>
              <span className="muted small">None of the nearby legal spots has cover within a few steps, so this is the closest.</span>
            </div>
          </div>
        </div>
      )
    }
    if (!spot.cover_feature) {
      return (
        <PlaceholderBox source="rain_cover" title="Rain cover at your spot">
          <span className="small">Awnings, canopies and shelters within a few steps of the car door.</span>
        </PlaceholderBox>
      )
    }
    return (
      <div className="card">
        <div className="card-row">
          <IconUmbrella />
          <div className="grow">
            <strong>{capitalize(spot.cover_feature.kind.replace('_', ' '))} beside the curb</strong>
            <span className="muted small">Wait here, out of the rain</span>
          </div>
          <PlaceholderTag source="rain_cover" />
        </div>
      </div>
    )
  }
  if (condition === 'sun') {
    return (
      <PlaceholderBox source="sun_shade" title="Shade at pickup time">
        <span className="small">How much of your wait spot is shaded at pickup time and over the next 10 minutes.</span>
      </PlaceholderBox>
    )
  }
  return null
}

/** Nudge toward the weather-conscious option when the weather turns. */
function WeatherHint({ ride }: { ride: Ride }) {
  const c = ride.weatherCondition
  if (ride.pickupMode === 'weather' || (c !== 'rain' && c !== 'sun')) return null
  return (
    <div className="hint">
      {c === 'rain' ? <IconRain /> : <IconSun />}
      <span className="grow small">{c === 'rain' ? 'It’s raining at pickup.' : 'Strong sun at pickup.'} Want a spot with {c === 'rain' ? 'cover' : 'shade'}?</span>
      <button className="link-btn" onClick={() => ride.choosePickupMode('weather')}>Switch</button>
    </div>
  )
}

/** Sidewalk details for the walk to the pickup (placeholder until an accessibility service exists). */
function AccessibilityCard({ info }: { info?: AccessibilityInfo }) {
  if (!info) {
    return (
      <PlaceholderBox source="sidewalk" title="Sidewalk accessibility">
        <span className="small">Steps, curb ramps, slope and surface along your walk to the car.</span>
      </PlaceholderBox>
    )
  }
  const check = (good: boolean, text: string) => (
    <li className={good ? 'ok' : 'warn'}>{good ? <IconCheck width={16} height={16} /> : <IconAlert width={16} height={16} />}{text}</li>
  )
  return (
    <div className="card">
      <div className="card-row">
        <IconAccessible />
        <div className="grow">
          <strong>{info.step_free ? 'Step-free route to your car' : 'This route has steps'}</strong>
          <span className="muted small">Along the sidewalks to your pickup</span>
        </div>
        <PlaceholderTag source="sidewalk" />
      </div>
      <ul className="checks">
        {check(info.step_free, info.step_free ? 'No stairs or steps' : 'Steps on the route')}
        <li className="ok"><IconRamp width={16} height={16} />{info.curb_ramps.length} curb ramp{info.curb_ramps.length === 1 ? '' : 's'} on the way</li>
        {check(info.max_running_slope_pct <= 5, `Slope up to ${info.max_running_slope_pct}%`)}
        {check(info.max_cross_slope_pct <= 2, `Cross slope up to ${info.max_cross_slope_pct}%`)}
        {check(info.min_width_m >= 1.5, `${info.surface}, at least ${info.min_width_m} m wide`)}
      </ul>
      {info.notes.map((n) => <p key={n} className="muted small note">{n}</p>)}
    </div>
  )
}

function ModeSwitch({ ride }: { ride: Ride }) {
  return (
    <div className="mode-switch" role="radiogroup" aria-label="Pickup option">
      {MODES.map((m) => (
        <button
          key={m.mode}
          role="radio"
          aria-checked={ride.pickupMode === m.mode}
          className={ride.pickupMode === m.mode ? 'on' : ''}
          onClick={() => ride.choosePickupMode(m.mode)}
        >
          {m.icon}
          <span>{m.short}</span>
        </button>
      ))}
    </div>
  )
}

function PlaceIcon({ place }: { place: Place }) {
  if (place.kind === 'home') return <IconHome />
  if (place.kind === 'work') return <IconWork />
  return <IconPin />
}

function Row({ icon, title, sub, onClick }: { icon: ReactNode; title: string; sub: string; onClick: () => void }) {
  return (
    <button className="row" onClick={onClick}>
      <span className="row-icon">{icon}</span>
      <span className="row-text">
        <strong>{title}</strong>
        <span>{sub}</span>
      </span>
    </button>
  )
}

function Header({ title, onBack }: { title: string; onBack?: () => void }) {
  return (
    <div className="panel-head">
      {onBack && <button className="icon-btn" onClick={onBack} aria-label="Back"><IconBack /></button>}
      <h1>{title}</h1>
    </div>
  )
}

function Loading({ title, sub, onBack }: { title: string; sub: string; onBack?: () => void }) {
  return (
    <>
      <Header title={title} onBack={onBack} />
      <div className="progress"><span /></div>
      <p className="muted">{sub}</p>
    </>
  )
}

const COMPASS = ['north', 'south', 'east', 'west']

function spotTitle(r: RankedSpot) {
  const street = r.spot.street_name ?? 'Unnamed campus road'
  // The backend's side is left/right of the OSM way, which means nothing to a rider; only show compass sides.
  return r.spot.side && COMPASS.includes(r.spot.side) ? `${street} · ${r.spot.side} side` : street
}

function SpotCard({ spot, showConfidence }: { spot: RankedSpot; showConfidence: boolean }) {
  return (
    <div className="card">
      <div className="card-row">
        <span className="pin-dot" aria-hidden />
        <div className="grow">
          <strong>{spotTitle(spot)}</strong>
          <span className="muted small">{spot.reason}</span>
        </div>
        {showConfidence && <ConfidenceBadge c={spot.confidence} />}
      </div>
      <div className="facts">
        <span className="facts-src">Legal stopping spot <PlaceholderTag source="legal_spots" /></span>
        <span><IconWalk width={16} height={16} /> {Math.round(spot.spot.walk_distance_m)} m walk · {minutes(spot.spot.walk_distance_m / 1.1)} min</span>
        {spot.spot.clearance_m != null && (
          <span>{Math.round(spot.spot.clearance_m)} m from the nearest hydrant, crossing or bus stop</span>
        )}
        {spot.cover_feature && <span><IconUmbrella width={16} height={16} /> {capitalize(spot.cover_feature.kind.replace('_', ' '))}</span>}
      </div>
    </div>
  )
}

function VehicleCard() {
  return (
    <div className="vehicle">
      <div className="option-car" aria-hidden />
      <div className="grow">
        <strong>{PLACEHOLDER.vehicle}</strong>
        <span className="muted small">Fully autonomous</span>
      </div>
      <span className="plate">{PLACEHOLDER.plate}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------

function Home({ ride }: { ride: Ride }) {
  const saved = PLACES.filter((p) => p.kind === 'home' || p.kind === 'work')
  const suggested = PLACES.filter((p) => p.kind !== 'home' && p.kind !== 'work').slice(0, 3)
  const current = MODES.find((m) => m.mode === ride.pickupMode)
  return (
    <>
      <div className="brand"><span className="brand-mark" aria-hidden /><span>endpoint</span></div>
      <h1 className="hello">Where to?</h1>
      <button className="pickup-chip" onClick={() => ride.openSearch('pickup')}>
        <span className="pickup-dot" aria-hidden />
        <span className="grow">
          <span className="small muted">Pickup</span>
          <strong>{ride.pickup.label}</strong>
        </span>
        <span className="link-btn">Change</span>
      </button>
      <button className="search" onClick={() => ride.openSearch('destination')}>
        <IconSearch />
        <span>Search destination</span>
        <span className="chip"><IconClock width={14} height={14} /> Now</span>
      </button>
      <div className="list">
        {saved.map((p) => <Row key={p.id} icon={<PlaceIcon place={p} />} title={p.name} sub={p.address} onClick={() => ride.chooseDestination(p)} />)}
        {suggested.map((p) => <Row key={p.id} icon={<IconClock />} title={p.name} sub={p.address} onClick={() => ride.chooseDestination(p)} />)}
      </div>
      <div className="card card--soft">
        <div className="card-row">
          <IconShield />
          <div className="grow">
            <strong>Pickup preference</strong>
            <span className="muted small">{current ? current.title : 'We’ll ask on your first ride'}</span>
          </div>
        </div>
        <div className="pref-row">
          {MODES.map((m) => (
            <button key={m.mode} className={`chip-btn ${ride.pickupMode === m.mode ? 'on' : ''}`} aria-pressed={ride.pickupMode === m.mode} onClick={() => ride.setPickupMode(m.mode)}>
              {m.short}
            </button>
          ))}
        </div>
      </div>
    </>
  )
}

/** Debounced address search (Nominatim), for both the pickup and the destination field. */
function useAddressSearch(query: string): GeocodeResult[] {
  const [results, setResults] = useState<GeocodeResult[]>([])
  useEffect(() => {
    if (query.trim().length < 3) { setResults([]); return }
    const ctrl = new AbortController()
    const t = setTimeout(() => { searchAddress(query, ctrl.signal).then((r) => { if (!ctrl.signal.aborted) setResults(r) }) }, 450)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [query])
  return results
}

const matchesQuery = (p: Place, q: string) => `${p.name} ${p.address}`.toLowerCase().includes(q.trim().toLowerCase())
const asPlace = (r: GeocodeResult): Place => ({
  id: `addr_${r.location.lat.toFixed(5)}_${r.location.lng.toFixed(5)}`,
  name: r.name,
  address: r.address,
  kind: 'address',
  location: r.location,
})

function OutsideDemoArea() {
  return (
    <div className="hint hint--warn" role="status">
      This pickup is outside the FIU demo area. The backend only has cached street data for campus, so legal pickup spots may be missing.
    </div>
  )
}

function Search({ ride }: { ride: Ride }) {
  const [field, setField] = useState<'pickup' | 'destination'>(ride.searchFocus)
  const [pq, setPq] = useState('')
  const [dq, setDq] = useState('')
  const destRef = useRef<HTMLInputElement>(null)
  const pickupRef = useRef<HTMLInputElement>(null)
  const pickupResults = useAddressSearch(field === 'pickup' ? pq : '')
  const destResults = useAddressSearch(field === 'destination' ? dq : '')

  useEffect(() => {
    ;(ride.searchFocus === 'pickup' ? pickupRef : destRef).current?.focus()
  }, [ride.searchFocus])

  const choosePickup = (label: string, address: string, location: LatLng) => {
    ride.setPickup({ label, address, location })
    setPq('')
    setField('destination')
    destRef.current?.focus()
  }

  const presets = PICKUP_PRESETS.filter((p) => matchesQuery(p, pq))
  const places = PLACES.filter((p) => matchesQuery(p, dq))
  const pickupText = `${ride.pickup.label} · ${ride.pickup.address}`

  return (
    <>
      <Header title="Plan your ride" onBack={ride.back} />
      <div className="route-inputs">
        <div className="route-dots" aria-hidden><i /><b /><i className="sq" /></div>
        <div className="grow">
          <input
            ref={pickupRef}
            className={`input ${field === 'pickup' ? 'input--active' : ''}`}
            placeholder="Pickup location"
            value={field === 'pickup' ? pq : pickupText}
            onFocus={() => { setField('pickup'); setPq('') }}
            onChange={(e) => setPq(e.target.value)}
            aria-label="Pickup location"
          />
          <input
            ref={destRef}
            className={`input ${field === 'destination' ? 'input--active' : ''}`}
            placeholder="Where to?"
            value={dq}
            onFocus={() => setField('destination')}
            onChange={(e) => setDq(e.target.value)}
            aria-label="Destination"
          />
        </div>
      </div>
      {!inDemoArea(ride.pickup.location) && <OutsideDemoArea />}

      {field === 'pickup' ? (
        <div className="list">
          <Row icon={<IconPin />} title="Current location" sub={DEFAULT_PICKUP.address} onClick={() => choosePickup(DEFAULT_PICKUP.label, DEFAULT_PICKUP.address, DEFAULT_PICKUP.location)} />
          <Row icon={<IconMapPin />} title="Choose on map" sub="Move the map to place your pickup pin" onClick={ride.startPinPick} />
          {presets.map((p) => <Row key={p.id} icon={<IconCampus />} title={p.name} sub={p.address} onClick={() => choosePickup(p.name, p.address, p.location)} />)}
          {pickupResults.map((r) => (
            <Row key={`${r.location.lat},${r.location.lng}`} icon={<IconSearch />} title={r.name} sub={r.address} onClick={() => choosePickup(r.name, r.address, r.location)} />
          ))}
          {pq.trim().length >= 3 && !presets.length && !pickupResults.length && <p className="muted small pad">Searching for “{pq}”…</p>}
        </div>
      ) : (
        <div className="list">
          {places.map((p) => <Row key={p.id} icon={<PlaceIcon place={p} />} title={p.name} sub={p.address} onClick={() => ride.chooseDestination(p)} />)}
          {destResults.map((r) => (
            <Row key={`${r.location.lat},${r.location.lng}`} icon={<IconSearch />} title={r.name} sub={r.address} onClick={() => ride.chooseDestination(asPlace(r))} />
          ))}
          {dq.trim().length >= 3 && !places.length && !destResults.length && <p className="muted small pad">Searching for “{dq}”…</p>}
        </div>
      )}
    </>
  )
}

function PickPin({ ride }: { ride: Ride }) {
  const at = ride.pinDraft ?? ride.pickup.location
  return (
    <>
      <Header title="Set your pickup" onBack={ride.back} />
      <p className="lead">Move the map so the pin sits where you'd like to be picked up.</p>
      <div className="card card--soft">
        <div className="card-row">
          <IconMapPin />
          <div className="grow">
            <strong>Pin location</strong>
            <span className="muted small">{at.lat.toFixed(5)}, {at.lng.toFixed(5)}</span>
          </div>
        </div>
      </div>
      {!inDemoArea(at) && <OutsideDemoArea />}
      <button className="btn btn--primary" onClick={ride.confirmPin}>Confirm pickup location</button>
    </>
  )
}

function PickupOptions({ ride }: { ride: Ride }) {
  const main = MODES.filter((m) => m.mode !== 'standard')
  return (
    <>
      <Header title="" onBack={ride.back} />
      {ride.spots.length > 0 && <p className="eyebrow">{ride.spots.length} pickup spots near you</p>}
      <h2 className="question">How should we pick you up?</h2>
      <div className="stack">
        {main.map((m) => (
          <button key={m.mode} className="choice" onClick={() => ride.choosePickupMode(m.mode)}>
            <span className="choice-icon">{m.icon}</span>
            <span className="grow">
              <strong>{m.title}</strong>
              <span className="muted small">{m.blurb}</span>
            </span>
          </button>
        ))}
        <button className="btn btn--secondary" onClick={() => ride.choosePickupMode('standard')}>Standard pickup: fastest</button>
      </div>
      <p className="muted small">We'll remember your choice. You can change it anytime.</p>
    </>
  )
}

function Detour({ ride }: { ride: Ride }) {
  return (
    <>
      <Header title="A better spot is farther" onBack={ride.back} />
      <p className="lead">{ride.plan?.rider_message || 'A better spot is a bit farther to walk. Use it, or stay with the closest spot?'}</p>
      <div className="stack">
        <button className="btn btn--primary" onClick={() => ride.answerDetour(true)}>Use the better spot</button>
        <button className="btn btn--secondary" onClick={() => ride.answerDetour(false)}>Stay with the closest spot</button>
      </div>
    </>
  )
}

function Confirm({ ride }: { ride: Ride }) {
  const { plan, activeSpot, destination } = ride
  if (!plan || !activeSpot || !destination) return null
  return (
    <>
      <Header title="Confirm pickup spot" onBack={ride.back} />
      <ModeSwitch ride={ride} />
      {ride.updating && (
        <div className="updating" role="status"><span className="spinner" aria-hidden /> Updating your pickup for the new conditions…</div>
      )}
      <WeatherHint ride={ride} />
      {plan.pickup_mode === 'weather' && <WeatherCard weather={plan.weather} />}
      <SpotCard spot={activeSpot} showConfidence={plan.pickup_mode !== 'standard'} />
      {plan.pickup_mode === 'weather' && <ConditionsCard spot={activeSpot} weather={plan.weather} />}
      {plan.pickup_mode === 'accessible' && <AccessibilityCard info={activeSpot.accessibility} />}
      {plan.rider_message && <p className="message" aria-live="polite">{plan.rider_message}</p>}

      <div className="option">
        <div className="option-car" aria-hidden />
        <div className="grow">
          <strong>Endpoint</strong>
          <span className="muted small">{ride.pickup.label} → {destination.name} · car {minutes(plan.eta_s)} min away</span>
        </div>
        <strong>{PLACEHOLDER.fare}</strong>
      </div>
      <button className="btn btn--primary" onClick={ride.confirmPickup} disabled={ride.updating}>Confirm pickup</button>
    </>
  )
}

function EnRoute({ ride }: { ride: Ride }) {
  const { ride: state, activeSpot } = ride
  if (!state || !activeSpot) return null
  // Standard pickups are confirmed up front, so phase alone can't say how close the car is.
  const near = state.eta_s <= 60
  const status =
    state.phase === 'approaching' ? 'Checking your spot' : near ? 'Arriving now' : 'Your car is on the way'
  return (
    <>
      <div className="status">
        <div>
          <p className="eyebrow">{state.phase === 'confirmed' ? 'Pickup confirmed' : 'Pickup predicted'}</p>
          <h1>{status}</h1>
        </div>
        <div className="eta"><strong>{minutes(state.eta_s)}</strong><span>min</span></div>
      </div>
      {state.rider_message && <p className="message" aria-live="polite">{state.rider_message}</p>}
      {/* S3 Vision results are not shown in the web app yet. */}
      <SpotCard spot={activeSpot} showConfidence={state.pickup_mode !== 'standard'} />
      {state.pickup_mode === 'weather' && <ConditionsCard spot={activeSpot} weather={state.weather} />}
      {state.pickup_mode === 'accessible' && <AccessibilityCard info={activeSpot.accessibility} />}
      <VehicleCard />
      <button className="btn btn--ghost" onClick={ride.reset}>Cancel ride</button>
    </>
  )
}

function Arrived({ ride }: { ride: Ride }) {
  const [left, setLeft] = useState(300)
  useEffect(() => {
    const t = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000)
    return () => clearInterval(t)
  }, [])
  const spot = ride.activeSpot
  return (
    <>
      <p className="eyebrow">Your car is here</p>
      <h1 className="big">Look for {PLACEHOLDER.plate}</h1>
      {spot && <p className="lead">{spotTitle(spot)}</p>}
      {ride.ride?.rider_message && <p className="message">{ride.ride.rider_message}</p>}
      <div className="card card--soft">
        <div className="card-row">
          <IconClock />
          <div className="grow">
            <strong>Take your time</strong>
            <span className="muted small">Your car will wait {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}</span>
          </div>
        </div>
      </div>
      <VehicleCard />
      <button className="btn btn--primary" onClick={ride.startTrip}><IconUnlock /> Unlock doors &amp; start ride</button>
    </>
  )
}

function OnTrip({ ride }: { ride: Ride }) {
  return (
    <>
      <div className="status">
        <div>
          <p className="eyebrow">On the way</p>
          <h1>{ride.destination?.name}</h1>
        </div>
        <div className="eta"><strong>{minutes(ride.ride?.eta_s ?? 0)}</strong><span>min</span></div>
      </div>
      <p className="muted">{ride.destination?.address}</p>
      <div className="card card--soft">
        <div className="card-row">
          <IconShield />
          <div className="grow">
            <strong>Need help?</strong>
            <span className="muted small">Rider support (placeholder)</span>
          </div>
        </div>
      </div>
      <VehicleCard />
    </>
  )
}

function Complete({ ride }: { ride: Ride }) {
  return (
    <>
      <p className="eyebrow">You've arrived</p>
      <h1 className="big">{ride.destination?.name}</h1>
      <p className="muted">{ride.destination?.address}</p>
      <div className="card">
        <strong>Was your pickup spot easy to reach?</strong>
        <div className="thumbs">
          <button className={`btn btn--secondary ${ride.feedback === 'up' ? 'is-on' : ''}`} onClick={() => ride.setFeedback('up')} aria-pressed={ride.feedback === 'up'}>
            <IconThumb /> Yes
          </button>
          <button className={`btn btn--secondary ${ride.feedback === 'down' ? 'is-on' : ''}`} onClick={() => ride.setFeedback('down')} aria-pressed={ride.feedback === 'down'}>
            <IconThumb style={{ transform: 'scaleY(-1)' }} /> No
          </button>
        </div>
        {/* Placeholder: feedback isn't sent anywhere yet (ENDPOINT.md §12 rider feedback loop). */}
        {ride.feedback && <p className="muted small">Thanks for the feedback.</p>}
      </div>
      <button className="btn btn--primary" onClick={ride.reset}>Done</button>
    </>
  )
}

export function RidePanel({ ride }: { ride: Ride }) {
  let body: ReactNode = null
  switch (ride.stage) {
    case 'home': body = <Home ride={ride} />; break
    case 'search': body = <Search ride={ride} />; break
    case 'pickPin': body = <PickPin ride={ride} />; break
    case 'requesting': body = <Loading title="Finding your pickup" sub="Looking for places your car can legally stop near you." onBack={ride.back} />; break
    case 'comfort': body = <PickupOptions ride={ride} />; break
    case 'planning': body = <Loading title="Choosing the best spot" sub="Checking sidewalks, weather and cover near you." onBack={ride.back} />; break
    case 'detour': body = <Detour ride={ride} />; break
    case 'confirm': body = <Confirm ride={ride} />; break
    case 'dispatching': body = <Loading title="Connecting you to a car" sub="Planning the route to your pickup spot." />; break
    case 'enroute': body = <EnRoute ride={ride} />; break
    case 'arrived': body = <Arrived ride={ride} />; break
    case 'ontrip': body = <OnTrip ride={ride} />; break
    case 'complete': body = <Complete ride={ride} />; break
  }

  return (
    <aside className="panel" aria-label="Ride">
      <div className="panel-grip" aria-hidden />
      <div className="panel-body" key={ride.stage}>
        {(ride.ride ?? ride.plan)?.degraded_note && !ride.error && (
          <div className="hint hint--warn" role="status">{(ride.ride ?? ride.plan)?.degraded_note}</div>
        )}
        {ride.error && (
          <div className="error" role="alert">
            <strong>Something went wrong</strong>
            <span>{ride.error}</span>
            <button className="link-btn" onClick={ride.reset}>Start over</button>
          </div>
        )}
        {body}
      </div>
    </aside>
  )
}
