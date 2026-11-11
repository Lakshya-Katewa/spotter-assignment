# Fuel Route Planner API

A Django API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON) and a link to an interactive map,
- where to fuel up along the way at the lowest total cost, for a vehicle with a **500-mile range** (50-gallon tank),
- the **total fuel cost** at **10 miles per gallon**.

Each new route makes **one** call to the routing API. Plain `"City, ST"` inputs are resolved locally and make no geocoding call. Repeated requests are served from cache in about 2 ms.

## Quick start

Requires **Python 3.12+** (Django 6.1's minimum; developed and tested on 3.14).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional, the defaults work as-is
python manage.py migrate
python manage.py load_stations  # first run downloads 2 Census files (~3 MB zipped); then ~3 s, no network
python manage.py runserver
```

Then:

- API: <http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Map: <http://127.0.0.1:8000/map/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Tests: `python manage.py test fuel` (83 tests in `fuel/tests/`, no network needed)
- Postman: import `postman_collection.json`

## API

### `GET /api/route/?start=…&finish=…` (or `POST` with a JSON body)

| Parameter | Description |
|---|---|
| `start`, `finish` | `"City, ST"` / `"City, State"` (resolved locally), `"lat,lon"`, or a street address (geocoded, see below) |
| `start_fuel_gallons` | Fuel in the tank at the start (0–50). Default **3 gallons** (30 miles, enough to reach a station near the start). The default is fixed and does not depend on the plan, so costs are comparable. |
| `min_stop_savings` | Default `0` (strictly cheapest plan). Set to e.g. `5` to skip stops that save less than $5. |
| `geometry` | `false` to leave the route line out of the response |

Response (New York → Los Angeles, trimmed):

```json
{
  "start":  {"query_label": "New York, NY", "lat": 40.662712, "lon": -73.938677, "resolved_by": "local"},
  "finish": {"query_label": "Los Angeles, CA", "lat": 34.019394, "lon": -118.410825, "resolved_by": "local"},
  "route": {"distance_miles": 2810.4, "duration_hours": 50.3, "geometry": {"type": "LineString", "coordinates": [[-73.93872, 40.6631], "..."]}},
  "fuel_stops": [
    {"stop": 2, "opis_id": 69954, "name": "Exxon", "address": "I-80, EXIT 27A & US-46", "city": "Budd Lake", "state": "NJ",
     "lat": 40.890613, "lon": -74.700356, "price_per_gallon": 3.119, "mile_marker": 49.1,
     "location_precision": "highway exit", "route_match": "matched to I-80 exit 27A, 0.0 mi from the route",
     "distance_from_route_miles": 0.0, "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 2.25, "cost": 7.01},
    "…"
  ],
  "summary": {
    "total_fuel_cost": 850.07,
    "fuel_purchased_gallons": 278.04, "fuel_consumed_gallons": 281.04,
    "starting_fuel_gallons": 3.0, "fuel_left_at_arrival_gallons": 0.0,
    "average_price_per_gallon": 3.0574, "number_of_stops": 16, "stops_located_at_exit": 14,
    "stations_considered": 304,
    "cheapest_possible_cost": 850.07, "cheapest_possible_stops": 16, "extra_cost_for_fewer_stops": 0.0,
    "fewer_stops_alternative": {"min_stop_savings": 5.0, "number_of_stops": 9, "total_fuel_cost": 855.05, "extra_cost": 4.98},
    "is_estimate": true
  },
  "meta": {"cached": false, "external_calls": {"routing": 1, "geocoding": 0}, "elapsed_ms": 1416.8,
           "station_dataset_version": "dae6c945…", "assumptions": {"…": "…"}},
  "map_url": "http://127.0.0.1:8000/map/?start=…&finish=…"
}
```

- `total_fuel_cost` is the money spent on fuel **purchased during the trip**; the starting fuel is not included. Fuel adds up as `starting_fuel_gallons + fuel_purchased_gallons − fuel_left_at_arrival_gallons = fuel_consumed_gallons` (route miles ÷ 10), and `fuel_on_arrival_gallons + gallons_purchased` never exceeds the 50-gallon tank.
- `location_precision` says where the station's position comes from: `"highway exit"` (its matched interstate exit, from OpenStreetMap; not the pump's exact position) or `"city centre"`. `route_match` gives the evidence that the station can be used from the route (see "Stations on the route" below).
- `fewer_stops_alternative` shows what skipping stops that save under $5 would give, alongside the cheapest plan (NY→LA: 9 stops instead of 16 for $4.98 more). The map page links to it.

Status codes:

| Status | Meaning |
|---|---|
| 400 | Bad input or a location outside the USA |
| 422 | No drivable route, or a stretch with no reachable station within range |
| 502 | The routing or geocoding service failed or returned a malformed response |
| 429 | Too many requests from this client (default 60/min, `API_THROTTLE_RATE`); includes `Retry-After` |
| 503 | Temporarily busy: an upstream rate limit, waiting for a concurrent identical request, or station data being reloaded; includes `Retry-After` |

All errors use the same shape: `{"error": "…"}`, plus `"details"` for field validation errors. Responses are gzip-compressed when the client accepts it (NY→LA: 86 KB → 26 KB).

### `GET /map/?start=…&finish=…`

An HTML page (Leaflet + OpenStreetMap tiles) showing the route, numbered fuel stops and the cost breakdown. It accepts the same optional parameters. Map tiles load in the browser and don't use the routing API.

## How it works

```
request ─► resolve start/finish ─► OSRM route (1 call, cached) ─► stations on route ─► optimizer ─► JSON / map
            (local Census table,     (geometry + highway refs)      (KD-tree; exit         (greedy, <1 ms)
             cached geocoder)                                         distance or highway
                                                                      evidence)
```

1. **Station coordinates (offline, once).** The price file has no coordinates, only highway-exit addresses such as `I-44, EXIT 283 & US-69`. `load_stations`:
   - matches each city/state to the US Census Gazetteer places and county-subdivision files, which covers 97% of stations;
   - uses saved coordinates in `data/geocode_overrides.json` for the ~150 small unincorporated places the Census files lack (looked up once with Nominatim via `--geocode-missing`; 5 that Nominatim couldn't find are pinned by hand);
   - keeps the lowest price for duplicate OPIS IDs and drops Canadian rows.

   - places **3,089 stations (47%) at their matched interstate exit** instead of their city centre, from `data/exit_locations.json`. That file is built once by `build_exit_locations`: one Overpass query fetches all 34,517 interstate exits in the contiguous US from OpenStreetMap (ODbL), and each station is matched by exit number to the exits within 15 miles of its city. If the same exit number appears more than 2 miles apart nearby (two interstates), the match is ambiguous and the station stays at its city centre (2% of stations). Checked samples: Tomah, WI (I-94 exit 143), Shorter, AL (I-85 exit 22), Gila Bend, AZ (I-8 exit 119).

   All 6,626 US stations are placed. Every load publishes a new dataset version (see step 6).
2. **Locations.**
   - Only a plain `"City, ST"` (optionally `, USA` or a ZIP) is resolved from the local Census table.
   - Anything more detailed, such as a street address, goes to Nominatim, so it is never collapsed to a city centre.
     - Results, including misses, are cached for 30 days, and identical concurrent lookups share one request.
     - Requests are limited to one per second, as Nominatim's usage policy requires. The whole wait is bounded: if no slot frees up within 5 seconds, the request returns 503 rather than breaking the limit.
     - Every response field is validated; malformed data returns 502.
   - Every location must fall inside the Census US national boundary (`data/us_boundary.json`, from the 1:5,000,000-scale cartographic file `cb_2024_us_nation_5m`, checked locally in about 0.1 ms). Windsor, Tijuana and Juárez are rejected; Detroit, San Ysidro and El Paso are accepted.
   - A few real places (~0.2% of names) have a Census internal point that isn't a usable location. San Francisco's is offshore, because the city includes the Farallon Islands; "Honolulu" matches Honolulu CCD, centred in the Northwestern Hawaiian Islands. For these the city is geocoded instead: one cached Nominatim lookup.
3. **Route.** One call to the public [OSRM](https://project-osrm.org/) server returns the full geometry (encoded polyline) and the route steps. The steps tell us which Interstate and US highways the route uses, and over which miles.
   - Routes are cached for 24 hours. The cache keeps the encoded polyline (~120 KB for a cross-country route), not decoded coordinates (~780 KB); decoding on use takes ~20 ms.
   - Calls are rate-limited to **1 per second**, as the demo server's usage policy requires (`fuel/services/ratelimit.py`, shared with Nominatim). Call starts are spaced at least 1.05 s apart. If no slot frees up within 5 seconds, the request returns 503 and the failure is not cached.
   - **Concurrent requests for the same uncached route share one upstream call** (`fuel/services/singleflight.py`): a lock in this process plus a cache lock, with the cache re-checked once the lock is held. Live test: 6 simultaneous cold requests made 1 OSRM call.
     - Within a process this always holds.
     - Across processes (shared cache) it holds unless the upstream call outlives the cache lock. The lock is sized to the worst case (rate-limit wait + connect timeout + read timeout + 5 s, ~50 s), but an upstream that keeps trickling bytes could outlast it, and a waiter could then make a second call. So it is best-effort.
   - Failures are cached briefly (30 s for errors, 1 h for "no route"), so an outage doesn't cause a stampede.
   - The response is validated: distances must be finite and non-negative, the polyline must decode completely, and coordinates must be in range. Anything else returns 502, never 500. Malformed steps are skipped, because they only feed highway matching.
4. **Stations on the route.** All stations are held in memory in an immutable snapshot: a KD-tree for exact great-circle queries, plus each station's details. A request uses one snapshot from start to finish, so a price reload mid-request can't mix two datasets. The route is densified to 1-mile steps, and stations within 15 miles are candidates.
   - **Station placed at its matched exit:** accepted only if the exit is within **1 mile** of the route. This is position evidence, the strongest we have, though it doesn't establish the pump's exact position, access direction or detour. On Chicago → Dallas, on-route exits measure ≤ 0.5 mi and exits on other interstates > 1 mi, so the threshold sits in a clear gap.
   - **Station at its city centre:** accepted if its address names an I-/US- highway the route drives within 15 miles of that point, or the centre is within 2 miles of the route. Being "near the route" alone is not evidence of access; a city 7 miles from I-80 whose station is on a state road is rejected.

   On the sample routes most chosen stops are exit-placed: NY→LA 14 of 16, Chicago→Dallas 4 of 7, Denver→Kansas City 6 of 6.
5. **Optimizer.** This is the classic fixed-route "gas station problem". Starting fuel is a fixed input (3 gallons unless given), tracked through the whole trip and never billed as a purchase. A zero-mile trip returns no stops and $0. At each stop:
   - if a cheaper station (or the destination) is within range, buy only enough fuel to reach it;
   - otherwise fill up and go to the cheapest station in range.

   This greedy rule gives the minimum cost on the line. Tests check it against a brute-force dynamic program, both with and without starting fuel.
6. **Caching and updates.** Plan cache keys include the dataset version. Each worker rebuilds its snapshot when the version changes. It reads the version before and after the rows and retries if it moved, so a snapshot never mixes two loads. Reloaded prices take effect on the next request without restarting anything.
7. **Optional fewer stops.** With `min_stop_savings > 0`, a second pass removes a stop by moving its purchase to the previous stop (if the tank has room) or the next one (if it is still reachable), while each removal costs less than that amount. The response always reports `cheapest_possible_cost`.

   | Route | Default (cheapest) | `min_stop_savings=5` |
   |---|---|---|
   | Chicago → Dallas | 7 stops, $273.91 | 3 stops, $275.28 |
   | New York → Los Angeles | 16 stops, $850.07 | 9 stops, $855.05 |
   | Seattle → Miami | 21 stops, $1,016.11 | 9 stops, $1,023.18 |

## Assumptions and limits

- **Results are estimates** (`is_estimate: true`).
  - Exit-placed stations (47%) are positioned at their **matched highway exit**: the OpenStreetMap junction where the exit ramp leaves the interstate. That makes their mile markers far more precise than a city centre, but it is not the pump's exact position. The pump is usually a short way off the exit, and that ramp detour is not charged.
  - City-centre stations (53%) can be off by a few miles, and their highway match is evidence, not proof.
  - Neither kind verifies access direction (which side of a divided highway), so the 500-mile feasibility of a plan is an estimate too.
  - Exit matching uses the exit number near the city. A wrong match needs the correct exit to be missing from OpenStreetMap *and* a different interstate's exit with the same number to be the only one within 15 miles. That's possible but rare, and ambiguous cases are excluded.
- **Starting fuel.** The default is 3 gallons (30 miles), the same for every plan. Pass `start_fuel_gallons` to change it.
- **Data coverage.** The provided price file has only 8 stations in California (all in Imperial/Riverside County) and none in Hawaii, so many trips there can't be planned. For example, San Francisco → Los Angeles returns 422 on the default 3 gallons, and works with `start_fuel_gallons=50`. The error message says this. It's a limit of the input data, not the planner.
- **Arrive empty.** The plan buys only the fuel the trip needs.
- Only Interstate and US routes are matched; state-route names differ too much between OSM and the price file. About 15% of stations name no I-/US- route, and they are used only when within 2 miles of the route.
- Exit locations cover the contiguous US (the price file has no Alaska or Hawaii stations).
- All settings (range, mpg, corridor widths, default start fuel, cache lifetimes, service URLs) can be changed through environment variables; see `.env.example`.
- **Multiple workers.** By default the cache is in-process: zero setup, and correct for a single process. Set `REDIS_URL` (and `pip install redis`) to share the caches, the request coalescing and both upstream rate limits across all worker processes. Without it, each process coalesces and rate-limits on its own.
- **Free upstream services.** Both allow 1 request per second in total, and the client-side limiter spaces the actual call starts at least 1 second apart. Measured live: 4 concurrent new routes started their OSRM calls 1.054–1.055 s apart.
  - The spacing uses stored timestamps, not key expiry, because some caches (Memcached) can expire a 1-second key early.
  - A per-client throttle on this API (map page included, default 60/min) limits abuse. It does **not** guarantee fair use of the upstream budget: one client making new routes as fast as allowed can still use all of it, and other clients then get 503 until a slot frees up. Preventing that would need per-client quotas on upstream calls, which is out of scope here.
  - Upstream failures are logged as warnings on the `fuel` logger.
- **Deployment.** `DEBUG` defaults to on so reviewers can run this with no setup. With `DJANGO_DEBUG=0`, the app refuses to start without a real `DJANGO_SECRET_KEY`, and it turns on secure cookies, an HTTPS redirect and proxy SSL header handling. `manage.py check --deploy` then reports only the opt-in HSTS warning (`DJANGO_HSTS_SECONDS`). Also set `DJANGO_ALLOWED_HOSTS`.

## Performance

| Stage | Time (NY → LA, 2,810 mi) |
|---|---|
| Resolve `"City, ST"` (local DB) + US-boundary check | ~2 ms |
| OSRM route with steps (network) | ~1.2–1.8 s |
| Corridor search + station matching | ~100 ms |
| Optimizer | <1 ms |
| Cached repeat request | ~2 ms |

The public OSRM demo server accounts for almost all of the uncached time. For production, run your own OSRM instance (`OSRM_URL`) and set `REDIS_URL`.

## Project layout

```
config/                         Django settings and URLs
fuel/models.py                  Place (Census centroids), FuelStation, DatasetVersion
fuel/management/commands/       load_stations, build_exit_locations, build_us_boundary
fuel/services/geo.py            start/finish resolution, geocode cache + rate limit
fuel/services/boundary.py       offline point-in-US-polygon check
fuel/services/routing.py        OSRM client: one call, validated, cached, coalesced
fuel/services/singleflight.py   coalesces concurrent requests for the same key into one upstream call
fuel/services/ratelimit.py      1 request/second limits for OSRM and Nominatim
fuel/services/highways.py       I-/US- highway refs from addresses and route steps
fuel/services/exits.py          match station addresses to OpenStreetMap interstate exits
fuel/services/stations.py       versioned immutable station snapshot + corridor search
fuel/services/optimizer.py      cheapest refuelling plan + optional stop consolidation
fuel/services/planner.py        ties it together, builds the response
fuel/views.py                   REST endpoint + map page
fuel/templates/fuel/map.html    Leaflet map (data via json_script, text-only DOM)
fuel/tests/                     optimizer, geometry, routing, upstream robustness, geocoding,
                                exits, API and settings tests (shared fixtures in helpers.py)
data/geocode_overrides.json     coordinates for places missing from the Census files
data/us_boundary.json           Census US boundary (built by build_us_boundary)
data/exit_locations.json        station positions at interstate exits (built by build_exit_locations)
```
