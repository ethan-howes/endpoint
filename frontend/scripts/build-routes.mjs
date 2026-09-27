// Precomputes real road-following routes for the FIU demo so the placeholder API needs no network.
// Driving: OSRM public demo server. Walking: routing.openstreetmap.de foot profile.
// Run from frontend:  node scripts/build-routes.mjs
// Output: src/api/routes.generated.json (encoded polylines, precision 5).
//
// Keep the coordinates below in sync with src/api/fixtures.ts.

import { writeFile } from 'node:fs/promises'

const RIDER = [25.7584, -80.3725]
const CAR_START = [25.7625, -80.385]
const SPOTS = {
  gc_loading: [25.75648, -80.37224],
  gc_service: [25.75592, -80.37249],
  ecc_north: [25.75701, -80.37096],
  ecc_south: [25.75656, -80.37102],
  sw14: [25.75518, -80.37365],
  library_west: [25.75713, -80.37445],
}
const DESTINATIONS = {
  home: [25.7631, -80.3835],
  work: [25.769, -80.3671],
  ec: [25.769, -80.3671],
  dolphin: [25.7887, -80.3806],
  tamiami: [25.75, -80.379],
  baptist: [25.6858, -80.3397],
  mia: [25.7953, -80.2789],
}

const DRIVE = 'https://router.project-osrm.org/route/v1/driving'
const FOOT = 'https://routing.openstreetmap.de/routed-foot/route/v1/driving'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function route(base, from, to) {
  // OSRM wants lng,lat.
  const url = `${base}/${from[1]},${from[0]};${to[1]},${to[0]}?overview=full&geometries=polyline`
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(url, { headers: { 'User-Agent': 'endpoint-hackathon-demo/0.1' } })
    if (res.ok) {
      const body = await res.json()
      if (body.code === 'Ok') {
        const r = body.routes[0]
        return { polyline: r.geometry, distance_m: Math.round(r.distance), duration_s: Math.round(r.duration) }
      }
    }
    await sleep(1500)
  }
  throw new Error(`Routing failed: ${url}`)
}

const out = { generated_at: new Date().toISOString(), drive: {}, walk: {} }

for (const [id, p] of Object.entries(SPOTS)) {
  out.drive[`car_start>${id}`] = await route(DRIVE, CAR_START, p)
  out.walk[`rider>${id}`] = await route(FOOT, RIDER, p)
  for (const [dest, d] of Object.entries(DESTINATIONS)) {
    out.drive[`${id}>${dest}`] = await route(DRIVE, p, d)
    await sleep(250) // be gentle with the public demo servers
  }
  console.log('done', id)
}

await writeFile(new URL('../src/api/routes.generated.json', import.meta.url), JSON.stringify(out, null, 1))
console.log('wrote src/api/routes.generated.json')
