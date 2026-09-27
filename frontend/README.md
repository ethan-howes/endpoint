# Endpoint rider app (frontend)

A Waymo / Uber / Lyft style rider app on **FIU's Modesto A. Maidique Campus**, connected to the orchestrator on `:8000`. The orchestrator supplies the real-time data:
- legal spots (S1)
- weather and cover/shade ranking (S2)
- the car's route and simulated position

Where the orchestrator's flow doesn't cover something the app needs, the frontend fills it in (see [Gaps the frontend fills](#gaps-the-frontend-fills)). The computer vision step (S3) isn't shown in the app.

## Run

Backend first, from the repo root (see the root README):

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
MOCK=1 ./scripts/run_all.sh        # or run the three uvicorn commands from the root README
curl localhost:8000/ready          # S1 and S2 should be "ok"
```

Then the app:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5500
npm run build      # type-check + production build into dist/
```

The app talks to `http://localhost:8000` by default. Settings in `frontend/.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_URL` | `http://localhost:8000` | Orchestrator URL |
| `VITE_USE_PLACEHOLDER` | `0` | `1` = run without a backend, on in-browser placeholder data |
| `VITE_SHADEMAP_KEY` | empty | Reserved for a sun shadow map layer (not wired yet) |

If the orchestrator isn't reachable, the app says so and tells you how to start it.

Things that need internet:
- the map tiles (OpenStreetMap);
- the rider's walking route and the destination leg (public OSRM servers, fetched in the browser; both fall back to straight lines if unreachable).

## Ride flow

1. **Where to?** Saved places (placeholder Home and Work) and nearby destinations.
2. **How should we pick you up?**
   - **Accessible pickup:** step-free sidewalk route, curb ramps, a level spot to board.
   - **Weather-conscious pickup:** under cover when it rains, in shade when it's hot.
   - **Standard:** the fastest pickup.
3. **Confirm pickup spot.** Shows the orchestrator's chosen spot and reason, its clearance from hydrants, crossings and bus stops, the weather, the cover on the map, the walking route and the rider message. The rider can switch options here.
4. **Detour prompt**, only when S2 asks for one (`confirmation_question`).
5. **Car on the way.** The app polls `GET /rides/{id}` every second, and the car glides along the real route between polls.
6. **Your car is here → Unlock → On trip → Arrived**, then a feedback prompt.

### Pickup location

The pickup defaults to the backend's `DEMO_RIDER` (25.7584, -80.3725, near Green Library). Its curbs are about 6 m from cover, so rain mode has real cover to rank. The rider can change it from the **Pickup** chip on the home screen or the pickup field on the search screen:
- **Campus presets:** Graham Center, Green Library, PG5 and others (`PICKUP_PRESETS` in `src/api/fixtures.ts`, coordinates from OpenStreetMap).
- **Address search:** OpenStreetMap Nominatim, debounced and biased to the FIU area (`src/lib/geocode.ts`). The same search also works for destinations.
- **Choose on map:** the map moves under a fixed pin, Uber-style. The address is filled in by reverse geocoding.

The pickup is sent as `rider_location` on `POST /rides/request`. The backend only has cached street data for campus (`DEMO_AREA`), so pickups outside it show a warning: in `MOCK=1`, S1 may find no legal spots there. Placeholder mode moves the rider marker but keeps its fixed campus spots.

## How the app maps onto the orchestrator

| App action | Orchestrator call |
|---|---|
| Choose an option | `POST /rides/request`, then `POST /rides/{id}/answer` with `{mobility_needs, force_condition, force_time}`, then `GET /rides/{id}` (for the detour question and degraded notes) |
| Accessible or Weather-conscious | `mobility_needs: true` |
| Standard | `mobility_needs: false` |
| Detour answer | `POST /rides/{id}/confirm` |
| Car position | `GET /rides/{id}` every second |
| Skip to arrival | `POST /rides/{id}/skip_to_arrival` |

The adapter is [src/api/orchestrator.ts](src/api/orchestrator.ts). The app only ever calls the `RideApi` interface in [src/api/client.ts](src/api/client.ts).

The orchestrator rejects unknown request fields, so the pickup option (`pickup_mode`), the walking route and the sidewalk data are kept on the client and never sent.

## Gaps the frontend fills

Each of these is handled in `src/api/orchestrator.ts` and can be removed once the backend covers it.

| Gap in the orchestrator | What the frontend does |
|---|---|
| `/answer` starts the car right away; there's no separate dispatch | Option previews are throwaway rides; **Confirm pickup** starts a fresh ride with the same answer |
| Answering the same ride twice corrupts the car's travelled distance | Switching options previews on a fresh ride |
| Rides without mobility needs are confirmed but the car is never simulated | The car is driven locally along the orchestrator's route, at the orchestrator's 5× speed-up |
| `skip_to_arrival` stops the simulator ~135 m out and never restarts it | The last stretch is driven locally |
| No trip to the destination | Routed with OSRM in the browser and driven locally |
| Cover geometry (`geometry_wkt`) is in UTM meters, not lng/lat | Converted back to lat/lng before drawing |
| S3 is out of scope, so every approach reports "S3 unreachable" | S3-only degraded notes are hidden; other notes are shown as a banner |

## Asks for the backend

1. **Sun mode is too slow.** With real candidates, S2's sun/shade ranking takes more than the orchestrator's 6 s S2 timeout, so every sun ride falls back to "Weather service unavailable; using the nearest spot". Rain takes about 2 s. S2 also keeps computing after the timeout, which delays the next request.
2. **Separate dispatch from `/answer`**, e.g. `POST /rides/{id}/dispatch`. The `flow.start_simulation` docstring already anticipates this.
3. **Simulate the car for rides without mobility needs**, and restart the simulator after `skip_to_arrival`.
4. **A trip leg:** `POST /rides/{id}/start_trip {destination}` plus a trip status on `GET /rides/{id}`.
5. **Pass S2 `overlays` through on `RidePlan`**, meaning `cover_features` and `shade_geojson`, so the map can draw all nearby cover and shade.
6. **`geometry_wkt` in lng/lat**, as ENDPOINT.md §5 implies.
7. **The rider message says "camera confirmed the predicted spot" even when S3 was unreachable.**
8. **Optional:** accept `pickup_mode`, and provide the walking route and sidewalk accessibility data (steps, curb ramps, slope) for the accessible option.

## Placeholders still in the UI

Each data source is listed in [src/api/dataSources.ts](src/api/dataSources.ts). Data that isn't real yet gets a dashed **PLACEHOLDER** tag or box. With the backend connected, legal spots, weather, rain cover and routing are live and their tags disappear.

| Still a placeholder | Why | UI slot |
|---|---|---|
| Shade from the sun | The orchestrator doesn't pass S2's `shade_geojson` through | "Shade at pickup time" card, `ShadeLayer` in `MapView.tsx` |
| Sidewalk accessibility | No service yet | Step-free checklist, curb-ramp markers |
| Vehicle, plate, fare, support | Out of scope | `PLACEHOLDER` in `RidePanel.tsx` |
| Feedback answer | Not sent anywhere yet | Complete screen |

## Demo controls (press ` to toggle)

- **Weather at pickup:** sends `force_condition` (Live = no override). Changing it on the confirm screen re-plans the ride. The map shows animated rain when the plan's weather is rain.
- **Sun time:** shown when Sun is selected. Sends `force_time` (10 AM = 14:00Z, 4 PM = 20:00Z on campus) so shade works at any hour.
- **Ask the pickup question every ride.**
- **Skip to arrival.**
- **Restart.**
- **Data sources:** which sources are live and which are placeholders.

## Placeholder mode

`VITE_USE_PLACEHOLDER=1` runs the whole flow in the browser without a backend ([src/api/placeholder.ts](src/api/placeholder.ts), [src/api/fixtures.ts](src/api/fixtures.ts)). Its routes were fetched once from OSRM and saved in `src/api/routes.generated.json`. To regenerate them after changing spots or destinations:

```bash
node scripts/build-routes.mjs
```
