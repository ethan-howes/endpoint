// Every data source the rider app shows, and whether it's real yet. The UI reads this to label
// placeholder data, so flip a source to 'live' when its service is wired into the orchestrator.

export type SourceStatus = 'placeholder' | 'live'

export interface DataSource {
  label: string
  service: string // who provides it (ENDPOINT.md §6 names)
  status: SourceStatus
  field: string // where it arrives in the orchestrator response
}

const SOURCES = {
  legal_spots: {
    label: 'Legal parking spots',
    service: 'S1 Legal Spots (OSM / Overpass)',
    status: 'placeholder',
    field: 'RideRequestResponse.spots, RidePlan.candidates[].spot',
  },
  weather: {
    label: 'Actual weather',
    service: 'S2 weather.py (Open-Meteo)',
    status: 'placeholder',
    field: 'RidePlan.weather',
  },
  rain_cover: {
    label: 'Rain cover',
    service: 'S2 rain_cover.py (OSM awnings, canopies, shelters)',
    status: 'placeholder',
    field: 'RankedSpot.cover_feature, RidePlan.overlays.cover_features',
  },
  sun_shade: {
    label: 'Shade from the sun',
    service: 'S2 sun_shade.py (Google Solar API + pvlib) and a sun shadow map layer (ShadeMap)',
    status: 'placeholder',
    field: 'RidePlan.overlays.shade_geojson',
  },
  sidewalk: {
    label: 'Sidewalk accessibility',
    service: 'Accessibility service (not assigned yet)',
    status: 'placeholder',
    field: 'RankedSpot.accessibility, RankedSpot.walk_polyline',
  },
  routing: {
    label: 'Driving and walking routes',
    service: 'Orchestrator routing adapter (precomputed OSRM routes for now)',
    status: 'placeholder',
    field: 'RidePlan.route_polyline, RankedSpot.walk_polyline',
  },
} satisfies Record<string, DataSource>

export type SourceId = keyof typeof SOURCES
export const DATA_SOURCES: Record<SourceId, DataSource> = SOURCES

export const isPlaceholder = (id: SourceId) => DATA_SOURCES[id].status === 'placeholder'

/**
 * Optional key for a browser-side sun shadow layer (e.g. ShadeMap's leaflet-shadow-simulator).
 * Not wired yet: when it is, render the layer in MapView's <ShadeLayer> slot.
 */
export const SHADEMAP_KEY: string | undefined = import.meta.env.VITE_SHADEMAP_KEY || undefined
