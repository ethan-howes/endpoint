import { useEffect, type Dispatch, type SetStateAction } from 'react'
import { USING_PLACEHOLDER } from '../api/client'
import { DATA_SOURCES } from '../api/dataSources'
import type { Condition } from '../api/types'
import type { Ride } from '../useRide'
import { IconClose, IconSliders } from './Icons'

type Force = Condition | 'live'

/** Presenter-only controls. They map to the orchestrator's demo overrides (ENDPOINT.md §7). */
export function DemoPanel({ ride, open, setOpen }: { ride: Ride; open: boolean; setOpen: Dispatch<SetStateAction<boolean>> }) {
  const { settings, setSettings } = ride

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement && e.target.type === 'text') return
      if (e.key === '`') setOpen((o) => !o)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setOpen])

  if (!open) {
    return (
      <button className="demo-fab" onClick={() => setOpen(true)} aria-label="Open demo controls">
        <IconSliders />
      </button>
    )
  }

  const force: Force = settings.forceCondition ?? 'live'
  const options: { value: Force; label: string }[] = [
    { value: 'live', label: 'Live' },
    { value: 'rain', label: 'Rain' },
    { value: 'sun', label: 'Sun' },
    { value: 'neutral', label: 'Clear' },
  ]

  return (
    <section className="demo" aria-label="Demo controls">
      <div className="demo-head">
        <strong>Demo controls</strong>
        <button className="icon-btn icon-btn--sm" onClick={() => setOpen(false)} aria-label="Hide demo controls"><IconClose /></button>
      </div>

      <div className="demo-field">
        <span className="demo-label">Weather at pickup (force_condition)</span>
        <div className="seg" role="radiogroup" aria-label="Weather at pickup">
          {options.map((o) => (
            <button
              key={o.value}
              role="radio"
              aria-checked={o.value === force}
              className={o.value === force ? 'on' : ''}
              onClick={() => setSettings((s) => ({ ...s, forceCondition: o.value === 'live' ? null : o.value }))}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {settings.forceCondition === 'sun' && (
        <div className="demo-field">
          <span className="demo-label">Sun time (force_time): shade flips sides of the street</span>
          <div className="seg" role="radiogroup" aria-label="Sun time">
            {([['now', 'Now'], ['morning', '10 AM'], ['afternoon', '4 PM']] as const).map(([value, label]) => (
              <button
                key={value}
                role="radio"
                aria-checked={settings.sunTime === value}
                className={settings.sunTime === value ? 'on' : ''}
                onClick={() => setSettings((s) => ({ ...s, sunTime: value }))}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      <label className="demo-check">
        <input type="checkbox" checked={settings.alwaysAsk} onChange={(e) => setSettings((s) => ({ ...s, alwaysAsk: e.target.checked }))} />
        Ask the pickup question every ride
      </label>

      <div className="demo-actions">
        <button className="mini" onClick={ride.skipToArrival} disabled={ride.stage !== 'enroute'}>Skip to arrival</button>
        <button className="mini" onClick={ride.reset}>Restart</button>
      </div>
      <details className="demo-sources">
        <summary>Data sources</summary>
        <ul>
          {Object.entries(DATA_SOURCES).map(([id, src]) => (
            <li key={id} title={`${src.service} → ${src.field}`}>
              <i className={`dot dot--${src.status}`} />
              <span>{src.label}</span>
              <em>{src.status === 'live' ? 'Live' : 'Placeholder'}</em>
            </li>
          ))}
        </ul>
      </details>
      <p className="demo-hint">
        {USING_PLACEHOLDER ? 'Using placeholder data (no backend).' : 'Connected to the orchestrator.'} Press ` to hide.
      </p>
    </section>
  )
}
