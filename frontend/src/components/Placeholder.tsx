import type { ReactNode } from 'react'
import { DATA_SOURCES, isPlaceholder, type SourceId } from '../api/dataSources'

/** Small pill marking data that isn't coming from its real service yet. Renders nothing once live. */
export function PlaceholderTag({ source }: { source: SourceId }) {
  if (!isPlaceholder(source)) return null
  const s = DATA_SOURCES[source]
  return (
    <span className="ph-tag" title={`${s.label}: placeholder until ${s.service} is connected`}>
      Placeholder
    </span>
  )
}

/** Dashed box standing in for a whole section whose data doesn't exist yet. */
export function PlaceholderBox({ source, title, children }: { source: SourceId; title: string; children?: ReactNode }) {
  const s = DATA_SOURCES[source]
  return (
    <div className="card placeholder-box">
      <div className="ph-head">
        <strong>{title}</strong>
        <PlaceholderTag source={source} />
      </div>
      {children}
      <span className="ph-source">Coming from {s.service}</span>
    </div>
  )
}
