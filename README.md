<div align="center">
    <br>
    <header>
        <h3>
            <b>project endpoint</b>
        </h3>
    </header>
    <br>
    <br>
</div>

A robotaxi pickup-spot advisor for riders who need shelter. Given a location,
it tells the rider where to wait so they are not standing in the rain — and
shows its working.

Built from [`ENDPOINT.md`](ENDPOINT.md), which is the source of truth for
intended behaviour.

---

## Status

| Component | Port | State |
|---|---|---|
| `shared/` — contracts, geometry, cache | — | done |
| `services/s1_legal_spots` | 8001 | done — definition-of-done verified on live OSM data |
| `orchestrator` | 8000 | done — ride flow, fusion, car simulation, degradation paths |
| `services/s2_weather_cover` | 8002 | done — rain cover, sun shade, demo fixtures committed |
| `services/s3_vision` | 8003 | out of scope for this build |

S3 is out of scope, so `on_approach` degrades to "confirm the predicted spot"
rather than doing a live camera check. Everything else in §7 is implemented.

---

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python -m scripts.prefetch_demo_area     # one time: warm the OSM cache
./scripts/run_all.sh                     # orchestrator + S1 + S2
python -m scripts.smoke_test             # end-to-end check, exits non-zero on failure
```

Or one service at a time:

```bash
python -m uvicorn services.s1_legal_spots.main:app --port 8001 --reload
python -m uvicorn services.s2_weather_cover.main:app --port 8002 --reload
python -m uvicorn orchestrator.main:app            --port 8000 --reload
```

Interactive API docs at `http://localhost:8000/docs`.

### Before a demo

```bash
python -m scripts.prefetch_demo_area     # ensure the demo area is cached
python -m pytest                         # 432 tests, ~1.5s, no network
python -m scripts.verify_demo_area       # S1 §6 definition of done, independently
python -m scripts.verify_s2_demo         # S2 scenarios can actually be demonstrated
python -m scripts.smoke_test             # proves the whole flow
```

`MOCK=1` replays committed fixtures with no network at all, which is the
configuration to rehearse the demo in. `python -m scripts.prefetch_demo_area`
must have been run at least once first — it is what populates
`services/*/fixtures/`.

---

## Tests

```bash
python -m pytest                    # everything
python -m pytest tests/test_curb.py  # one module
```

| File | Covers |
|---|---|
| `test_geo.py` | local frames, polylines, bearings, cache tiling |
| `test_legality.py` | one-way rules, exclusion buffers, confidence assignment |
| `test_curb.py` | candidate generation, per-side offsets, corridor validation |
| `test_ranking.py` | dedupe, walk distance, ordering determinism |
| `test_s1_api.py` | the S1 service contract |
| `test_models.py` | the shared Pydantic contracts |
| `test_clients.py` | the §4.5 guarantee: no service failure breaks a ride |
| `test_fusion.py` | predictive/vision fusion and the hysteresis rule |
| `test_orchestrator_api.py` | the full ride flow, including every degraded path |
| `test_s2_cover.py` | OSM cover/shade parsing, ring closure, node discs |
| `test_s2_weather.py` | classification, wait-window sampling, the demo override |
| `test_s2_scoring.py` | the rain/sun scoring factors and ordering |
| `test_s2_api.py` | the S2 contract, the Overpass query text, both fallbacks |
| `test_fixture_plan.py` | capture and export derive the same cache ids |

Beyond the unit tests, three scripts check the things unit tests cannot:

- `scripts/verify_demo_area.py` — re-derives S1's legality judgement
  independently and asserts §6's definition of done: spots on both sides of the
  street, none inside a hydrant/crossing/bus-stop/intersection/bike-lane
  exclusion. Run it after changing any threshold in `shared/config.py`.
- `scripts/verify_s2_demo.py` — the same idea for S2, and it exists because S2
  needed it. Every S2 unit test passed and every S2 API test was green while the
  rain demo was dead: the service answered 200 with 30 ranked spots and reported
  "No cover found nearby" for all of them, because not one candidate kerb was
  within `rain_max_gap_m` of any cover in the box. What was wrong was the *data
  the demo runs on*, not the logic, and no unit test can see that. This asserts
  cover is reachable from a kerb, that rain re-orders rather than ties with
  neutral, that the 10:00/16:00 sun side-flip actually changes the answer, and
  that no scenario silently ran on its fallback path.
- `scripts/smoke_test.py` — drives a real ride over HTTP.
- `scripts/show_rain_score_defect.py` — prints the arithmetic behind S2's one
  substantive departure from `ENDPOINT.md` (below). Run it to see the numbers.

---

## Design decisions worth knowing

These are decisions where the build departs from a literal reading of
`ENDPOINT.md`, with the reasoning.

**S1 caps at `likely`.** There is no obtainable official curb-regulation feed for
Miami. Miami-Dade's OMF "SMART Curb" programme is a freight project with no
public endpoint, and no Miami open data portal publishes a curb regulation layer.
OSM is the only source obtainable, so `RegulationSource` is a protocol
(`services/s1_legal_spots/network.py`) with `OsmRegulationSource` as its only
implementation, and `verified` is reserved for a real CDS or CurbLR feed. The
field that tells you *why* a spot was called legal is `Spot.legality_basis`.

**Untagged curb means permissive.** Where OSM says nothing, S1 does not invent a
prohibition. The result is `inferred_standard` / `unverified`, and the rider-facing
message says so in words. See `orchestrator/messages.py::_hedge`.

**The demo area is the Ernest R. Graham Center, FIU Miami**, demo rider at
(25.7584, -80.3725). The *rider position* was chosen by measurement, not by the
cover counts that first picked the spot. The box has 427 building passages, 100
covered walkways and 31 shelters — but at the old rider position the nearest of
30 candidate kerbs was **47 m** from the nearest cover and none were within 40 m,
so rain mode found cover for zero spots and fell back to walk ranking. High cover
counts and cover-adjacent kerbs are different properties; only the second one is
what rain mode reads. Scanning 20 positions in the box and ranking by the mean
gap over a rider's five best kerbs put this one first (12.0 m mean, 5.8 m best,
against 51.3 m / 47.2 m at the old position). See `shared/config.py::demo_rider`.

**`rain_max_gap_m` is 15, not the 5 in `ENDPOINT.md` §6.** No position in the demo
box gets a kerb within 5.8 m of cover, so at 5 the section's headline scenario
demonstrated nothing. 15 is §6's own value for the *sun* module, reused rather
than invented. The hard cut is kept as specified — a kerb just outside it scores
as though there were no cover — but `gap_m` is reported on every ranked spot
regardless, so a UI can still show "awning 6 m away".

**Cache-first, always.** S1 never does a cold Overpass fetch on the request path:
the orchestrator allows it 3 s and a cold query takes longer. On a cache miss it
gets a 2.5 s budget and then degrades. `prefetch_demo_area.py` is the intended way
to warm it.

**`side` is relative to the OSM way's digitisation direction**, not to the
compass and not to which way traffic runs. `curb_bearing_deg` is the absolute
disambiguation and is what S3 uses to aim its camera.

**Ride state is in memory.** Restarting the orchestrator loses every ride, and a
rider polling an old ride gets a 404 that says exactly that. Deliberate: nothing
in the demo needs to survive a restart, and a persistence layer would be a
migration path nobody uses.

**Corridor validation rejects in both directions.** A candidate whose curb point
is much further from the centerline than its own offset is a geometry blowup; one
whose point is much *closer* is inside a doubled-back aisle. The second case
fired on 0.30 % of candidates in the demo area and was the only genuine violation
in §6's definition-of-done check.

### S2 — weather and cover

**The doc's rain scoring is a no-op, and this is the one real fix.** `ENDPOINT.md`
§6 line 512 weights a cover feature by confidence with `unverified: 0.3`, and
line 517 separately awards `0.3 * walk_factor` to a spot with *no* cover. Those
two `0.3`s cancel: a spot with a shelter directly overhead scores exactly what a
spot with nothing at all scores. Since every OSM feature is `unverified` — the doc
puts `verified` behind city open data and `likely` behind Google Places, and
neither exists for the demo area — the rain ranking degenerated to plain walk
distance. `scripts/show_rain_score_defect.py` prints the arithmetic.

The fix is to read the table as a *relative* discount with the reachable tier as
the 1.0 baseline (`unverified: 1.0, detected: 1.1, likely: 1.3, verified: 1.6`).
`no_cover_score` stays a floor at 0.3, now below every covered score, and
`scoring.rank_key` breaks ties on gap inside the band where the floor is what
decides — so approaching real cover still orders correctly. The same reasoning
applies to `shade_source_weight`, where `osm_geometry` is the only source this
service can produce.

**An open OSM way is a line, not a degenerate polygon.** §6 assumes cover
features are areas. Most are not: `way["highway"]["covered"]` is a covered
*walkway*, so being on the way is being covered, and its geometry is a centreline.
The parser used to decide area-vs-line from the element *type*, which forced
every way through `shapely.Polygon` — and `Polygon` on two points is empty, so the
feature silently vanished. Measured over the captured demo responses, 529 of 547
classified cover features are open ways and only 18 are closed, so **97 % of the
cover was discarded, including all 100 `covered=yes` highways.** Rain mode was
scoring against a nearly empty map. `_geometry_in` now decides from whether the
projected ring returns to its first point (within 0.5 mm, because a closed way's
first and last coordinate arrive as two pairs already put through a projection).
After the fix the same responses yield 558 features: 427 building passages, 100
covered walkways, 31 shelters. `tests/test_s2_cover.py` pins it with the real
measured tags and point counts.

**`tree_row` is not in the query.** It is tagged on a *way*, and `tree_shadow`
reads `shape.x` as a single trunk, so a row cannot be consumed by the shadow model
at all — an earlier version selected it and dropped every result. A query selector
whose results are always thrown away is worse than no selector, because the query
implies the capability exists.

**The cache-id namespace is one function, not a convention.** S1 stores under the
bare kind (`roads:…`), S2 under `s2_cover/` and `s2_shade/`. The namespace is only
a label in the `.meta` sidecar — the file name is a hash of the query — so it
cannot be recovered from the cache and has to be declared. It was a literal at
five call sites and **absent from `export_fixtures.py`**, which therefore looked
up the bare kind: a third rule. The exporter reported all 18 S2 fixtures MISSING
straight after a capture that had written all 18, while printing the fixture list
in the same breath. `shared.fixtures.cache_key` is now called by every reader and
every writer, and `tests/test_fixture_plan.py` greps for the literal so a sixth
copy cannot be added. This is the *third* naming mismatch this project hit, all
the same shape — see `shared/fixtures.py` for the earlier two.

**S2's query radius is a configured constant, never derived.** An earlier version
computed it from the spread of the spots S1 returned, which looked like a strict
improvement and was actively harmful: the disk cache is keyed by a hash of the
*query text*, and the query text embeds the margin, so a response captured at
150 m was invisible to a request that derived 71.42 m. Correct cache id,
different query hash, a miss every time. The demo area was fully seeded and S2
still reported "cover fetch timed out" for all of it. It also bought nothing —
71 m and 150 m are both tile level 0, so the tile count was identical either way.
`cover_query_radius_m` is pinned to `DEFAULT_RADIUS_M` so one prefetch serves S1
and S2 over the same grid.

**Conditions are classified over the whole wait window,** not the single instant
the doc names. A rider waiting 10 minutes cares about the worst of those 10
minutes. Hour buckets are half-open `[hour, hour+1)` so an on-the-hour pickup does
not read one extra hour of weather that ended precisely when they arrived. Rain
takes precedence over sun, because rain is what actually gets a rider wet.

**Shade comes from OSM geometry, not Google Solar.** `sun_shade.py` has a provider
seam, but nothing untested ships; the geometry fallback is what the demo uses and
`shade_source` on every response says which method produced the answer. Solar
position is `pvlib`, not hand-rolled — DST, refraction and the equation of time are
not worth reimplementing. Shadows genuinely reverse sides between 10:00 and 16:00
local (elevation 36°/azimuth 113° vs 41°/242°), so §6's "move `force_time` and
watch the recommendation change sides" demo is physically real.

**One frame per request.** `LocalFrame.to_m` returns *absolute* UTM
easting/northing and uses its anchor only to select the EPSG. So within one zone,
two different frames produce bit-identical coordinates and threading a single
frame changes no number — Miami is entirely in 17N. Across a zone boundary the
frames are not merely offset, they are 600 km apart: the same point measures
800 934 E in 17N and 199 066 E in 18N, an apparent 601 869 m. Per-tile frames
would therefore put cover hundreds of kilometres from the spots being ranked,
with no error anywhere. `rank()` picks one frame and every tile is parsed into
it, which makes that unrepresentable rather than merely unlikely.

**A missing `shade_source` means the answer was not a shadow model.** When
`rank_spots` falls back for a low sun or an empty area it returns no geometry, and
the service reports `shade_source: null` rather than claiming `osm_geometry`. The
honesty field has to be able to say "nothing".

**There are no awnings in the demo area.** `man_made=awning|canopy` is in the
query, because §6's definition of done names awnings, but a measured probe
(`scripts/probe_cover_tags.py`) found zero of both inside the demo box. The rain
demo therefore rests entirely on 100 `covered=yes` ways and 427
`tunnel=building_passage` ways. Read the demo as "covered walkway and arcade
shelter", not as a failure to find awnings.

**Building heights are almost all guesses.** 52 buildings in the demo box, of which
3 carry `building:levels`. Everything else falls back to a 6 m default, so a
modelled shadow is confidently in the wrong place as often as not. Carried through
as `ShadeBlock.height_estimated` and paid for by `shade_source_weight`.

---

## Layout

```
shared/                    contracts, geometry, disk cache, fixtures
services/s1_legal_spots/   legal stopping spots from OSM
services/s2_weather_cover/ rain cover and sun shade, ranking S1's spots
orchestrator/              ride flow, fusion, routing, car simulation
scripts/                   prefetch, fixtures, verification, smoke test
tests/
data/cache/overpass/       seeded Overpass responses (gitignored)
```

`shared/` is imported by every service, which is the point: a contract that
drifts breaks all of them at once, visibly, rather than one quietly.

---

## Known limits

- S2 does not exist, so condition-aware ranking and cover overlays are absent.
- S3 is out of scope, so the real-time phase always confirms the prediction. The
  seam is real and tested — `orchestrator/fusion.py` is exercised directly — but
  nothing calls it in production yet.
- All spots at the Graham Center come back `unverified`, because FIU's campus
  roads are `highway=service` with no `parking:*` tags to reason from. S2's cover
  detection is what will carry trust there.
- Campus service roads are unnamed, so rider messages say "at the pickup point"
  rather than naming a street. S2's cover features should supply the landmark.
- Overpass is intermittently slow or 504s. The cache and the fixture replay exist
  because of this.
