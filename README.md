# Fuel Route Planner API

A Django API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON) and a link to an interactive map,
- where to fuel up along the way at the lowest total cost, for a vehicle with a **500-mile range** (50-gallon tank),
- the **total fuel cost** at **10 miles per gallon**.

Each new route makes **one** call to the routing API. Plain `"City, ST"` inputs are resolved locally and make no geocoding call. Repeated requests are served from cache in about 2 ms.

## Quick start

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
- Tests: `python manage.py test fuel` (40 tests, no network needed)
- Postman: import `postman_collection.json`

## API

### `GET /api/route/?start=…&finish=…` (or `POST` with a JSON body)

| Parameter | Description |
|---|---|
| `start`, `finish` | `"City, ST"` / `"City, State"` (resolved locally), `"lat,lon"`, or a street address (geocoded, see below) |
| `start_fuel_gallons` | Fuel already in the tank (0–50). Omit for "nearly empty": the vehicle has just enough fuel to reach a station within the first 30 route miles. |
| `min_stop_savings` | Default `0` (strictly cheapest plan). Set to e.g. `5` to skip stops that save less than $5. |
| `geometry` | `false` to leave the route line out of the response |

Response (New York → Los Angeles, trimmed):

```json
{
  "start":  {"query_label": "New York, NY", "lat": 40.662712, "lon": -73.938677, "resolved_by": "local"},
  "finish": {"query_label": "Los Angeles, CA", "lat": 34.019394, "lon": -118.410825, "resolved_by": "local"},
  "route": {"distance_miles": 2810.4, "duration_hours": 50.3, "geometry": {"type": "LineString", "coordinates": [[-73.93872, 40.6631], "..."]}},
  "fuel_stops": [
    {"stop": 1, "opis_id": 72087, "name": "DELTA", "address": "US-9", "city": "Jersey City", "state": "NJ",
     "lat": 40.711417, "lon": -74.06476, "price_per_gallon": 3.239, "mile_marker": 8.8,
     "access": "near route", "city_centroid_distance_from_route_miles": 1.5,
     "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 4.1, "cost": 13.29},
    "…"
  ],
  "summary": {
    "total_fuel_cost": 859.91,
    "fuel_purchased_gallons": 280.15, "fuel_consumed_gallons": 281.04,
    "starting_fuel_gallons": 0.88, "fuel_left_at_arrival_gallons": 0.0,
    "average_price_per_gallon": 3.0694, "number_of_stops": 15, "stations_considered": 290,
    "cheapest_possible_cost": 859.91, "cheapest_possible_stops": 15, "extra_cost_for_fewer_stops": 0.0,
    "is_estimate": true
  },
  "meta": {"cached": false, "external_calls": {"routing": 1, "geocoding": 0}, "elapsed_ms": 1416.8,
           "station_dataset_version": "dae6c945…", "assumptions": {"…": "…"}},
  "map_url": "http://127.0.0.1:8000/map/?start=…&finish=…"
}
```

- `total_fuel_cost` is the money spent at the pumps. Fuel adds up as `starting_fuel_gallons + fuel_purchased_gallons − fuel_left_at_arrival_gallons = fuel_consumed_gallons` (route miles ÷ 10), and `fuel_on_arrival_gallons + gallons_purchased` never exceeds the 50-gallon tank.
- `access` gives the evidence that a station can be reached from the route: `"on I80"` (its address names a highway the route uses at that point) or `"near route"`.

Status codes:

| Status | Meaning |
|---|---|
| 400 | Bad input or a location outside the USA |
| 422 | No drivable route, or a stretch with no reachable station within range |
| 502 | The routing service failed |
| 503 | The geocoder failed or is rate-limited (only for street addresses) |

### `GET /map/?start=…&finish=…`

An HTML page (Leaflet + OpenStreetMap tiles) showing the route, numbered fuel stops and the cost breakdown. It accepts the same optional parameters. Map tiles load in the browser and don't use the routing API.

## How it works

```
request ─► resolve start/finish ─► OSRM route (1 call, cached) ─► stations on route ─► optimizer ─► JSON / map
            (local Census table,     (geometry + highway refs)      (KD-tree + highway    (greedy, <1 ms)
             cached geocoder)                                         matching)
```

1. **Station coordinates (offline, once).** The price file has no coordinates, only highway-exit addresses such as `I-44, EXIT 283 & US-69`. `load_stations`:
   - matches each city/state to the US Census Gazetteer places and county-subdivision files, which covers 97% of stations;
   - uses saved coordinates in `data/geocode_overrides.json` for the ~150 small unincorporated places the Census files lack (looked up once with Nominatim via `--geocode-missing`; 5 that Nominatim couldn't find are pinned by hand);
   - keeps the lowest price for duplicate OPIS IDs and drops Canadian rows.

   All 6,626 US stations are placed. Every load publishes a new dataset version (see step 6).
2. **Locations.**
   - Only a plain `"City, ST"` (optionally `, USA` or a ZIP) is resolved from the local Census table.
   - Anything more detailed, such as a street address, goes to Nominatim, so it is never collapsed to a city centre. Nominatim results, including misses, are cached for 30 days. Requests are limited to one per second, as Nominatim's usage policy requires. The limit uses an atomic `cache.add`, so it covers every worker process when the cache is shared (Redis/Memcached); with the default in-process cache it applies per process. If no slot frees up within 5 seconds, the request returns 503 rather than breaking the limit.
   - Every location must fall inside the Census US national boundary (`data/us_boundary.json`, 5 m resolution, checked locally in about 0.1 ms). Windsor, Tijuana and Juárez are rejected; Detroit, San Ysidro and El Paso are accepted.
3. **Route.** One call to the public [OSRM](https://project-osrm.org/) server returns the full geometry (encoded polyline) and the route steps. The steps tell us which Interstate and US highways the route uses, and over which miles. Routes are cached for 24 hours. HTTP errors, malformed responses and `NoRoute` are mapped to 502 or 422.
4. **Stations on the route.** All stations are held in memory in a KD-tree (exact great-circle queries). The route is densified to 1-mile steps, and stations whose city lies within 15 miles are candidates. A candidate is accepted only if:
   - its address names an I-/US- highway that the route drives within 15 miles of that point (the station is at an exit of the road we're on), or
   - its city centre is within 2 miles of the route.

   Being "near the route" alone is not evidence of access. For example, a city 7 miles from I-80 whose station is on a state road is rejected.
5. **Optimizer.** This is the classic fixed-route "gas station problem". Starting fuel is an explicit tank state, tracked through the whole trip and never billed as a purchase. At each stop:
   - if a cheaper station (or the destination) is within range, buy only enough fuel to reach it;
   - otherwise fill up and go to the cheapest station in range.

   This greedy rule gives the minimum cost on the line. Tests check it against a brute-force dynamic program, both with and without starting fuel.
6. **Caching and updates.** Plan cache keys include the dataset version. Each worker rebuilds its station index when the version changes, so reloading prices takes effect on the next request without restarting anything.
7. **Optional fewer stops.** With `min_stop_savings > 0`, a second pass removes a stop by moving its purchase to the previous stop (if the tank has room) or the next one (if it is still reachable), while each removal costs less than that amount. The response always reports `cheapest_possible_cost`.

   | Route | Default (cheapest) | `min_stop_savings=5` |
   |---|---|---|
   | Chicago → Dallas | 7 stops, $274.02 | 3 stops, $275.40 |
   | New York → Los Angeles | 15 stops, $859.91 | 8 stops, $865.08 |
   | Seattle → Miami | 21 stops, $1,027.52 | 9 stops, $1,034.69 |

## Assumptions and limits

- **Results are estimates** (`is_estimate: true`). Stations are placed at their city's centre, so a mile marker can be off by a few miles. Highway matching shows that a station is reachable from the route, but not its exact exit. Detours are not charged: accepted stations are at an exit of the road being driven, or within 2 miles of it.
- **Starting fuel.** Without `start_fuel_gallons`, the vehicle starts nearly empty, with just enough fuel to reach the cheapest station in the first 30 miles. That fuel is reported as `starting_fuel_gallons`, not as a purchase.
- **Arrive empty.** The plan buys only the fuel the trip needs.
- Only Interstate and US routes are matched; state-route names differ too much between OSM and the price file. About 15% of stations name no I-/US- route, and they are used only when within 2 miles of the route.
- All settings (range, mpg, corridor widths, start window, cache lifetimes, service URLs) can be changed through environment variables; see `.env.example`.

## Performance

| Stage | Time (NY → LA, 2,810 mi) |
|---|---|
| Resolve `"City, ST"` (local DB) + US-boundary check | ~2 ms |
| OSRM route with steps (network) | ~1.2–1.8 s |
| Corridor search + highway matching | ~100 ms |
| Optimizer | <1 ms |
| Cached repeat request | ~2 ms |

The public OSRM demo server accounts for almost all of the uncached time. For production, run your own OSRM instance (`OSRM_URL`) and use Redis for the cache, which also makes the geocoder rate limit shared across all workers.

## Project layout

```
config/                         Django settings and URLs
fuel/models.py                  Place (Census centroids), FuelStation, DatasetVersion
fuel/management/commands/       load_stations, build_us_boundary
fuel/services/geo.py            start/finish resolution, geocode cache + rate limit
fuel/services/boundary.py       offline point-in-US-polygon check
fuel/services/routing.py        OSRM client (single call, cached)
fuel/services/highways.py       I-/US- highway refs from addresses and route steps
fuel/services/stations.py       versioned in-memory KD-tree + corridor search
fuel/services/optimizer.py      cheapest refuelling plan + optional stop consolidation
fuel/services/planner.py        ties it together, builds the response
fuel/views.py                   REST endpoint + map page
fuel/templates/fuel/map.html    Leaflet map (data via json_script, text-only DOM)
data/geocode_overrides.json     coordinates for places missing from the Census files
data/us_boundary.json           Census US boundary (built by build_us_boundary)
```
