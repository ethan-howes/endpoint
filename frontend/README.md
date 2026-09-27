# Endpoint rider app (frontend)

A barebones web app with a Waymo / Uber / Lyft style ride flow, set on **FIU's Modesto A. Maidique Campus** in Miami.
The backend services aren't built yet, so the app runs on **placeholder data** that has the same shape as the orchestrator's responses (ENDPOINT.md §5 and §7). The computer vision step (S3) runs behind the scenes in the flow but isn't displayed in the app.

## Run

```bash
cd frontend
npm install
npm run dev        # http://localhost:5500
npm run build      # type-check + production build into dist/
```

The map uses OpenStreetMap tiles, so it needs internet. Nothing else calls the network while placeholder mode is on.

## Pickup options

After picking a destination, the rider chooses how they want to be picked up:

| Option | What it shows | Demo spot |
|---|---|---|
| **Accessible pickup** | Step-free sidewalk route drawn in green along real footpaths, curb-ramp markers, and a checklist (steps, slope, cross slope, surface, width) | GC service drive |
| **Weather-conscious pickup** | Weather at pickup (S2), cover at the spot, and the walk | East Campus Circle (south) |
| Standard pickup | Fastest pickup | East Campus Circle (north) |

The option is sent as `pickup_mode` on `POST /rides/{id}/answer` (a frontend extension). It can be switched on the confirm screen and set as a preference on the home screen. The sidewalk details are placeholders (`AccessibilityInfo` in `src/api/types.ts`) until a sidewalk/accessibility service exists; see ENDPOINT.md §12.

## Weather on the map

When it's raining at pickup (the plan's weather, or the demo control set to **Rain**), the map shows an animated rain layer and a "Raining at pickup" chip. Strong sun gets a warm glow and a "Strong sun at pickup" chip. See `src/components/WeatherOverlay.tsx`.

## Data still to come (placeholders)

Every data source is listed in [src/api/dataSources.ts](src/api/dataSources.ts) with `status: 'placeholder'`. Anything built on placeholder data gets a dashed **PLACEHOLDER** tag or box in the UI. Flip a source to `'live'` once its service is wired in, and the tags disappear. The demo panel's **Data sources** list shows the current status of each.

| Data | Service | Arrives in | UI slot |
|---|---|---|---|
| Actual weather | S2 `weather.py` (Open-Meteo) | `RidePlan.weather` | Weather card, map weather chip, rain/sun overlay |
| Rain cover | S2 `rain_cover.py` | `RankedSpot.cover_feature`, `overlays.cover_features` | "Rain cover at your spot" card, cover polygons on the map |
| Shade from the sun | S2 `sun_shade.py` + a sun shadow layer (ShadeMap) | `overlays.shade_geojson` | "Shade at pickup time" card, `ShadeLayer` in `MapView.tsx` (renders GeoJSON; ShadeMap key via `VITE_SHADEMAP_KEY`) |
| Legal parking spots | S1 Legal Spots | `RideRequestResponse.spots`, `candidates[].spot` | Gray dots, "Legal stopping spot" line on the spot card |
| Sidewalk accessibility | Accessibility service (not assigned yet) | `RankedSpot.accessibility`, `walk_polyline` | Step-free checklist, green sidewalk route, curb-ramp markers |
| Routes | Orchestrator routing adapter | `route_polyline`, `walk_polyline` | Route line, walking path |

## Real FIU routes

The pickup spots are snapped to real drivable roads near the Graham Center. The driving routes (car → pickup, pickup → each destination) and walking routes (rider → pickup) follow real roads and footpaths. They were fetched once from OSRM and saved in `src/api/routes.generated.json`, so the demo doesn't call a routing service live. To regenerate after changing spots or destinations:

```bash
node scripts/build-routes.mjs
```

## Connecting the real backend

Everything goes through [src/api/client.ts](src/api/client.ts). Create `frontend/.env`:

```
VITE_USE_PLACEHOLDER=0
VITE_API_URL=http://localhost:8000
```

The `RideApi` interface lists every call the UI makes. Three of them aren't in ENDPOINT.md yet and need the orchestrator to add them (or the UI to be adjusted):

| Call | Why the UI needs it |
|---|---|
| `POST /rides/{id}/dispatch` | The rider confirms the pickup spot before the car leaves |
| `POST /rides/{id}/start_trip` | The rider is in the car; drive to the destination |
| `trip_status` on `GET /rides/{id}` | Tells the UI when the car is at the pickup and when the trip is done |

`RideRequestResponse.spots`, so legal spots can be drawn before the rider answers, and `RidePlan.overlays` / `needs_rider_confirmation` are optional extra fields. The UI reads them if they're present.

## Ride flow

1. **Where to?** Saved places (placeholder Home / Work) and nearby destinations.
2. **Finding your pickup.** Calls `requestRide`; legal spots from S1 show as gray dots.
3. **How should we pick you up?** Accessible, weather-conscious, or standard. The choice is remembered.
4. **Confirm pickup spot.** Calls `answer`. Shows the chosen spot, the walking route, and the option's details (sidewalk checklist or weather). The rider can switch options here.
5. **Detour prompt**, only if the plan sets `needs_rider_confirmation`.
6. **Car on the way.** Calls `dispatch`, then polls `getRide` every second. The car marker glides between polls.
7. **Your car is here → Unlock → On trip → Arrived**, with a feedback prompt.

## Placeholders

| What | Where | Replaced by |
|---|---|---|
| Rider location, car start, destinations | `src/api/fixtures.ts` | Device location + a places/geocoding search |
| Legal spots around the Graham Center | `src/api/fixtures.ts` | S1 via the orchestrator |
| Ranking, weather, rider messages | `src/api/fixtures.ts` | S2 via the orchestrator |
| Routes (precomputed from OSRM) | `src/api/routes.generated.json` | Orchestrator routing adapter |
| Sidewalk accessibility (steps, ramps, slope) | `placeholderAccessibility` in `src/api/fixtures.ts` | Accessibility / sidewalk service |
| Car movement, phases | `src/api/placeholder.ts` | `GET /rides/{id}` |
| Vehicle, plate, fare, support | `PLACEHOLDER` in `src/components/RidePanel.tsx` | Not in scope yet |
| Feedback answer | `Complete` screen | Not sent anywhere yet |

Spot positions come from real road snapping, but their names and rankings are placeholders.

## Demo controls (press ` to toggle)

- **Weather at pickup:** sends `force_condition` (Live = no override). Changing it on the confirm screen re-plans the ride.
- **Ask the pickup question every ride.**
- **Skip to arrival:** `POST /rides/{id}/skip_to_arrival`.
- **Restart.**
