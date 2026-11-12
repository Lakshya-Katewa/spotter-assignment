# Fuel Route Planner API

A Django API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON) and a link to an interactive map,
- where to fuel up along the way at the lowest total cost, for a vehicle with a **500-mile range** (50-gallon tank),
- the **total fuel cost** at **10 miles per gallon**.

Each new route makes **one** call to the routing API. Plain `"City, ST"` inputs are resolved locally and make no geocoding call. Repeated requests are served from cache in about 2 ms.

## Summary

- **Every gallon is paid for by default.** The vehicle starts with just enough fuel to reach its first stop, charged at that stop's price, so `total_fuel_cost` = route miles ÷ 10 × stop prices. Pass `start_fuel_gallons=50` to leave with a full tank that isn't billed.
- **Optimal, with few stops.** A greedy optimizer finds the cheapest plan (checked against a brute-force solver in tests). By default it then removes stops that save less than $5 (NY→LA: 9 stops instead of 17, for $5.40 more). `min_stop_savings=0` returns the strictly cheapest plan.
- **Station positions are derived offline.** The price file has no coordinates. Stations are placed at their matched interstate exit from OpenStreetMap (48%) or at their city centre from Census data (52%). Only stations with evidence they can be reached from the route are used.
- **Data limits are reported, not hidden.** The price file has almost no stations in California and none near Houston. Such trips return a clear warning or a 422 with the route still included.
- **Quick check:** `python manage.py test fuel` (103 tests, offline) and the Postman collection in `postman_collection.json`.

## Quick start

Requires **Python 3.12+** (Django 6.1's minimum; developed and tested on 3.14).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional, the defaults work as-is
python manage.py migrate
python manage.py load_stations  # ~3 s, fully offline (all data ships in data/)
python manage.py runserver
```

Then:

- API: <http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Map: <http://127.0.0.1:8000/map/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Tests: `python manage.py test fuel` (103 tests in `fuel/tests/`, no network needed; CI runs them on every push)
- Postman: import `postman_collection.json`
- Health: <http://127.0.0.1:8000/healthz>

## API

### `GET /api/route/?start=…&finish=…` (or `POST` with a JSON body)

| Parameter | Description |
|---|---|
| `start`, `finish` | `"City, ST"` / `"City, State"` (resolved locally), `"lat,lon"`, or a street address (geocoded, see below) |
| `start_fuel_gallons` | Fuel already in the tank at the start (0–50), not paid for on this trip. **Default: pay for all fuel** (see "Starting fuel" below). `50` = leave with a full tank. |
| `min_stop_savings` | Default **`5`**: skip stops that save less than $5. `0` = the strictly cheapest plan (whose cost is always reported too). |
| `geometry` | `false` to leave the route line out of the response |

Response (New York → Los Angeles, trimmed):

```json
{
  "start":  {"query_label": "New York, NY", "lat": 40.662712, "lon": -73.938677, "resolved_by": "local"},
  "finish": {"query_label": "Los Angeles, CA", "lat": 34.019394, "lon": -118.410825, "resolved_by": "local"},
  "route": {"distance_miles": 2810.4, "duration_hours": 50.3, "geometry": {"type": "LineString", "coordinates": [[-73.93872, 40.6631], "..."]}},
  "fuel_stops": [
    {"stop": 2, "opis_id": 67918, "name": "DELAWARE TRUCK STOP", "address": "I-80, EXIT 4C/US-46", "city": "Delaware", "state": "NJ",
     "lat": 40.927326, "lon": -75.088914, "price_per_gallon": 3.079, "mile_marker": 71.5,
     "location_precision": "highway exit", "route_match": "matched to I-80 exit 4C, 0.0 mi from the route",
     "distance_from_route_miles": 0.0, "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 32.97, "cost": 101.52},
    "…"
  ],
  "summary": {
    "total_fuel_cost": 865.71,
    "fuel_paid_for_gallons": 281.04, "fuel_purchased_gallons": 280.15, "fuel_consumed_gallons": 281.04,
    "starting_fuel_gallons": 0.88, "starting_fuel_billed": true, "starting_fuel_cost": 2.86,
    "fuel_left_at_arrival_gallons": 0.0,
    "average_price_per_gallon": 3.0804, "number_of_stops": 9, "stops_located_at_exit": 8,
    "stations_considered": 303,
    "cheapest_possible_cost": 860.31, "cheapest_possible_stops": 17, "extra_cost_for_fewer_stops": 5.4,
    "fewer_stops_alternative": null,
    "is_estimate": true
  },
  "meta": {"cached": false, "external_calls": {"routing": 1, "geocoding": 0}, "elapsed_ms": 2384.2, "warnings": [],
           "station_dataset_version": "9e361390…", "assumptions": {"…": "…"}, "attribution": "…OpenStreetMap contributors, ODbL…"},
  "map_url": "http://127.0.0.1:8000/map/?start=…&finish=…"
}
```

- `total_fuel_cost` is the cost of **all the fuel the trip burns**: route miles ÷ 10 gallons (`fuel_paid_for_gallons` = `fuel_consumed_gallons`), at the prices of the planned stops. It is the stop costs plus `starting_fuel_cost`: the fuel for the few miles to the first stop, charged at that stop's price. If you pass `start_fuel_gallons`, that fuel is already in the tank and is not paid for. Either way `starting_fuel_gallons + fuel_purchased_gallons − fuel_left_at_arrival_gallons = fuel_consumed_gallons`, and `fuel_on_arrival_gallons + gallons_purchased` never exceeds the 50-gallon tank.
- `location_precision` says where the station's position comes from: `"highway exit"` (its matched interstate exit, from OpenStreetMap; not the pump's exact position) or `"city centre"`. `route_match` gives the evidence that the station can be used from the route (see "Stations on the route" below).
- By default stops saving under $5 are skipped: NY→LA has 9 stops instead of the strictly cheapest 17, for $5.40 more (`cheapest_possible_cost`, `extra_cost_for_fewer_stops`). With `min_stop_savings=0`, `fewer_stops_alternative` shows the $5 plan alongside the cheapest one, and the map page links to it.
- `meta.warnings` lists estimates worth knowing about, e.g. a first station far from the start (Houston → Phoenix: "the first is at mile 106 … assumed to start with 10.6 gallons, charged at that station's price").
- `average_price_per_gallon` is over the fuel paid for (`fuel_paid_for_gallons`); fuel passed as `start_fuel_gallons` is free and not included.

Status codes:

| Status | Meaning |
|---|---|
| 400 | Bad input or a location outside the USA |
| 422 | No drivable route, or no fuel plan (see `code` below) |
| 502 | The routing or geocoding service failed or returned a malformed response |
| 429 | Too many requests from this client (default 60/min, `API_THROTTLE_RATE`); includes `Retry-After` |
| 503 | Temporarily busy: an upstream rate limit, waiting for a concurrent identical request, or station data being reloaded; includes `Retry-After` |

All errors use the same shape: `{"error": "…"}`, plus `"details"` for field validation errors. When the route exists but no fuel plan does, the 422 also has a `code` and still returns the `route` (with geometry), `start`, `finish` and `map_url`, and the map page draws the route:

| `code` | Meaning |
|---|---|
| `no_stations_on_route` | The price file has no usable station along the route (e.g. San Francisco → Los Angeles) |
| `no_station_near_start` | Pay-for-all-fuel start, but the first station on the route is beyond a full tank's range |
| `start_fuel_too_low` | The given `start_fuel_gallons` can't reach any station |
| `range_gap` | Two consecutive stations (or the last one and the finish) are more than 500 miles apart |

The status is 422, not 200, because there is no fuel plan or total cost to return, and a 200 with a null cost is easy for a client to mistake for "$0". For the coverage errors the message suggests `start_fuel_gallons`. Responses are gzip-compressed when the client accepts it (NY→LA: 86 KB → 26 KB).

### `GET /healthz`

`200 {"status": "ok", "stations": 6626, "dataset_version": "…"}` when the station data is loaded and the in-memory index builds; `503` otherwise. It makes no upstream calls.

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
   - uses the **median price** for duplicate OPIS IDs, and drops Canadian rows. 678 IDs appear 2–6 times with the same Rack ID and prices up to $0.90 apart. The file has no dates, so the rows can't be ordered; they are treated as repeated observations. Taking the minimum, as an earlier version did, biased about 10% of stations low and drew the optimizer to them (Chicago → Dallas looked $5 cheaper).
   - places **3,157 stations (48%) at their matched interstate exit** instead of their city centre, from `data/exit_locations.json`. That file is built once by `build_exit_locations`: one Overpass query fetches all 34,517 interstate exits in the contiguous US from OpenStreetMap (ODbL), **together with the interstate each exit is on** (the `ref` of its motorway ways). A station matches only an exit with its exit number, **on an interstate its address names**, within 15 miles of its city. If the matches are more than 2 miles apart, it's ambiguous and the station stays at its city centre. Checking the interstate dropped 47 matches that were on the wrong highway (e.g. an "I-79, EXIT 40" station had been put at I-64 exit 40) and resolved 115 that were previously ambiguous. Where the price file itself names the wrong interstate (McPherson, KS says I-35 for what is I-135), the station stays at its city centre rather than guessing. Checked samples: Tomah, WI (I-94 exit 143), Shorter, AL (I-85 exit 22), Gila Bend, AZ (I-8 exit 119).

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
     - Across processes (shared cache) it holds unless the upstream call outlives the cache lock. The lock is sized to the worst case (rate-limit wait + connect timeout + read timeout + 5 s, ~30 s with the 10 s `EXTERNAL_TIMEOUT`), but an upstream that keeps trickling bytes could outlast it, and a waiter could then make a second call. So it is best-effort.
   - Failures are cached briefly (30 s for errors, 1 h for "no route"), so an outage doesn't cause a stampede.
   - The response is validated: distances must be finite and non-negative, the polyline must decode completely, and coordinates must be in range. Anything else returns 502, never 500. Malformed steps are skipped, because they only feed highway matching.
4. **Stations on the route.** All stations are held in memory in an immutable snapshot: a KD-tree for exact great-circle queries, plus each station's details. A request uses one snapshot from start to finish, so a price reload mid-request can't mix two datasets. The route is densified to 1-mile steps, and stations within 15 miles are candidates.
   - **Station placed at its matched exit:** accepted only if the exit is within **1 mile** of the route. This is position evidence, the strongest we have, though it doesn't establish the pump's exact position, access direction or detour. On Chicago → Dallas, on-route exits measure ≤ 0.5 mi and exits on other interstates > 1 mi, so the threshold sits in a clear gap.
   - **Station at its city centre:** accepted if its address names an I-/US- highway the route drives within 15 miles of that point, or the centre is within 2 miles of the route. Being "near the route" alone is not evidence of access; a city 7 miles from I-80 whose station is on a state road is rejected.

   On the sample routes most chosen stops are exit-placed (strictly cheapest plans): NY→LA 15 of 17, Chicago→Dallas 4 of 8, Denver→Kansas City 6 of 6.
5. **Optimizer.** This is the classic fixed-route "gas station problem". By default the trip pays for all its fuel: the vehicle starts with just enough to reach its first stop, which can be any station within 30 route miles of the start (`START_APPROACH_MILES`), and that fuel is charged at the first stop's price. Each possible first stop is tried with the greedy rule below, and the cheapest whole-trip plan wins (a handful of sub-millisecond runs). Always taking the very first station instead gives chains of 0.1-gallon top-ups at pricey stations near the start. If no station is that close, the first station on the route is the first stop and `meta.warnings` says how much starting fuel that assumes. The price file has no stations around Houston (first one: Flatonia, 106 miles west) and almost none in California, so this applies to Houston, Los Angeles and San Diego starts. Refusing those trips instead would fail some of the most common requests; the first leg is still paid for, never free, and never longer than a full tank. Fuel passed as `start_fuel_gallons` is tracked through the trip and not billed. A zero-mile trip returns no stops and $0. At each stop:
   - if a cheaper station (or the destination) is within range, buy only enough fuel to reach it;
   - otherwise fill up and go to the cheapest station in range.

   This greedy rule gives the minimum cost on the line. Tests check it against a brute-force dynamic program, for the pay-for-all-fuel start and for given starting fuel.
6. **Caching and updates.** Plan cache keys include the dataset version. Each worker rebuilds its snapshot when the version changes. It reads the version before and after the rows and retries if it moved, so a snapshot never mixes two loads. Reloaded prices take effect on the next request without restarting anything.
7. **Fewer stops (default).** The strictly cheapest plan is full of tiny top-ups (a 1-gallon stop to save 4 cents a gallon), which no driver would make. The assignment says optimal "mostly" means cost-effective, so by default (`min_stop_savings=5`) a second pass removes a stop by moving its purchase to the previous stop (if the tank has room) or the next one (if it is still reachable), while each removal costs less than $5. This pass is a heuristic, not an exact cost-plus-stops optimum. The response always reports `cheapest_possible_cost`, and `min_stop_savings=0` returns that plan.

   | Route | Default (`min_stop_savings=5`) | Strictly cheapest (`0`) |
   |---|---|---|
   | Chicago → Dallas | 3 stops, $290.12 | 8 stops, $288.31 |
   | New York → Los Angeles | 9 stops, $865.71 | 17 stops, $860.31 |
   | Seattle → Miami | 9 stops, $1,034.57 | 21 stops, $1,029.83 |
   | Denver → Kansas City | 2 stops, $183.74 | 6 stops, $182.29 |

## Assumptions and limits

- **Results are estimates** (`is_estimate: true`).
  - Exit-placed stations (48%) are positioned at their **matched highway exit**: the OpenStreetMap junction where the exit ramp leaves the interstate. That makes their mile markers far more precise than a city centre, but it is not the pump's exact position. The pump is usually a short way off the exit, and that ramp detour is not charged.
  - City-centre stations (52%) can be off by a few miles, and their highway match is evidence, not proof.
  - **Detours are not charged.** A station's `distance_from_route_miles` is not a detour: for city-centre stations it is mostly the error of the city-centre position. Caddo Mills, TX is 3.5 mi from the route, but its address is "I-30, EXIT 87", an exit on the route itself; charging 2 × 3.5 miles would bill a drive that doesn't happen. The real detour (off the ramp to the pump and back) is unknown because the price file has no pump positions. It is typically under a mile, so about 0.1 gallon (~$0.30) per stop is not counted.
  - Neither kind verifies access direction (which side of a divided highway), so the 500-mile feasibility of a plan is an estimate too.
  - Exit matching requires the exit to be on an interstate the address names, so a same-numbered exit on another interstate is never used. It still trusts the address: a station whose address names the wrong exit number on the right interstate would be misplaced.
- **Starting fuel: pay for all fuel by default.** This is a product decision: the assignment doesn't say. It asks for "the total money spent on fuel" at 10 mpg, so by default every gallon the trip burns is paid for, and `total_fuel_cost` checks out as miles ÷ 10 × prices. The price file has no station exactly at the start, so the vehicle is assumed to start with just enough fuel to reach its first stop, charged at that stop's price. That is usually 0–3 gallons (first stop within 30 miles); where the data has no station nearby (Houston, Los Angeles, San Diego) it can be 10–30 gallons, and `meta.warnings` says so. Why not start with a free full tank, which many solutions assume:
  - trips under 500 miles would cost $0 with no stops, so the API would never show where to fuel up for them;
  - up to 500 miles of fuel (about $150) would be left out of the total, and how much depends on the trip;
  - where to buy the first tank is part of what the optimizer should decide.

  A truck that really does leave the yard fuelled can pass `start_fuel_gallons` (e.g. `50` for a full tank). That fuel isn't billed, and `total_fuel_cost` then covers only what is bought on the way. To make the full tank the default for every request, set `DEFAULT_START_FUEL_GALLONS=50`.
- **Data coverage.** The provided price file has only 8 stations in California (all in Imperial/Riverside County) and none in Hawaii, so many trips there can't be planned. For example, San Francisco → Los Angeles has no station anywhere along the route, so it returns 422 `no_stations_on_route`, still with the route and map. With `start_fuel_gallons=50` it returns a plan with no stops. This is a limit of the input data, not the planner.
- **Arrive empty.** The plan buys only the fuel the trip needs.
- Only Interstate and US routes are matched; state-route names differ too much between OSM and the price file. About 15% of stations name no I-/US- route, and they are used only when within 2 miles of the route.
- Exit locations cover the contiguous US (the price file has no Alaska or Hawaii stations).
- All settings (range, mpg, corridor widths, default start fuel, cache lifetimes, service URLs) can be changed through environment variables; see `.env.example`.
- **Multiple workers.** By default the cache is in-process: zero setup, and correct for a single process. **Running more than one worker process requires `REDIS_URL`** (and `pip install redis`) to share the caches, the request coalescing and both upstream rate limits. Without it, each process coalesces and rate-limits on its own, so `gunicorn -w 4` could make up to 4 calls/second to the free servers; `manage.py check --deploy` warns about this (`fuel.W001`). The in-process cache holds at most 500 entries (~80 KB per plan, ~120 KB per route: about 100 MB worst case).
- **Free upstream services.** Both allow 1 request per second in total, and the client-side limiter spaces the actual call starts at least 1 second apart. Measured live: 4 concurrent new routes started their OSRM calls 1.054–1.055 s apart.
  - The spacing uses stored timestamps, not key expiry, because some caches (Memcached) can expire a 1-second key early.
  - A per-client throttle on this API (map page included, default 60/min) limits abuse. It does **not** guarantee fair use of the upstream budget: one client making new routes as fast as allowed can still use all of it, and other clients then get 503 until a slot frees up. Preventing that would need per-client quotas on upstream calls, which is out of scope here.
  - Upstream failures are logged as warnings on the `fuel` logger.
- **Deployment.** `DEBUG` defaults to on so reviewers can run this with no setup. With `DJANGO_DEBUG=0`, the app refuses to start unless `DJANGO_SECRET_KEY` is long and random (the dev default, the `.env.example` placeholder and short or repetitive keys are rejected), and it turns on secure cookies, an HTTPS redirect and proxy SSL header handling. Also set `DJANGO_ALLOWED_HOSTS`, `DJANGO_NUM_PROXIES` behind a reverse proxy (so throttling uses the client address, not the proxy's), and `REDIS_URL` for multiple workers. `manage.py check --deploy` then reports only the opt-in HSTS warning (`DJANGO_HSTS_SECONDS`). SQLite runs in WAL mode so requests aren't blocked while `load_stations` reloads prices; use Postgres for real traffic.
- **Upstream errors.** Timeouts are 10 s per connect/read (`EXTERNAL_TIMEOUT`). Error messages to clients are generic; the details are logged on the `fuel` logger.
- **Attribution.** Routes and exit positions come from OpenStreetMap (ODbL); the API's `meta.attribution` and the map page credit it.

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
fuel/views.py                   REST endpoint, map page, /healthz
fuel/checks.py                  deploy check: shared cache needed for multiple workers
fuel/templates/fuel/map.html    Leaflet map (data via json_script, text-only DOM)
fuel/tests/                     optimizer, geometry, routing, upstream robustness, geocoding,
                                exits, load_stations, API and settings tests (shared fixtures in helpers.py)
.github/workflows/tests.yml     CI: checks, missing migrations, tests
data/2024_Gaz_*.txt.gz          Census Gazetteer places + county subdivisions (public domain, 2.7 MB)
data/geocode_overrides.json     coordinates for places missing from the Census files
data/us_boundary.json           Census US boundary (built by build_us_boundary)
data/exit_locations.json        station positions at interstate exits (built by build_exit_locations)
```
