# Fuel Route Planner API

A Django API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON) and a link to an interactive map,
- where to fuel up along the way to keep the cost lowest, for a vehicle with a **500-mile range**,
- the **total fuel cost** at **10 miles per gallon**.

Each new route makes **one** call to the routing API. Repeated routes are served from cache in a few milliseconds.

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
- Tests: `python manage.py test fuel`
- Postman: import `postman_collection.json`

## API

### `GET /api/route/?start=…&finish=…` (or `POST` with a JSON body)

| Parameter  | Description |
|------------|-------------|
| `start`, `finish` | `"City, ST"` / `"City, State"` (resolved locally), `"lat,lon"`, or a street address (one Nominatim lookup) |
| `geometry` | `false` to leave the route line out of the response (default `true`) |
| `min_stop_savings` | Skip a fuel stop if it saves less than this many dollars (default `5`). `0` gives the strictly cheapest plan. |

Response (trimmed):

```json
{
  "start":  {"query_label": "New York, NY", "lat": 40.662712, "lon": -73.938677, "resolved_by": "local"},
  "finish": {"query_label": "Los Angeles, CA", "lat": 34.019394, "lon": -118.410825, "resolved_by": "local"},
  "route": {"distance_miles": 2810.4, "duration_hours": 50.3, "geometry": {"type": "LineString", "coordinates": [[-73.93872, 40.6631], "..."]}},
  "fuel_stops": [
    {"stop": 1, "opis_id": 62790, "name": "7-ELEVEN #40084", "address": "US-46/US-1/US-9",
     "city": "Palisades Park", "state": "NJ", "lat": 40.847017, "lon": -73.997061,
     "price_per_gallon": 3.099, "mile_marker": 10.1, "distance_from_route_miles": 8.2,
     "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 6.99, "cost": 21.66}
  ],
  "summary": {"total_fuel_cost": 858.35, "total_gallons": 281.04, "average_price_per_gallon": 3.0543,
              "number_of_stops": 8, "stations_considered": 459,
              "cheapest_possible_cost": 852.47, "cheapest_possible_stops": 17, "extra_cost_for_fewer_stops": 5.89},
  "meta": {"cached": false, "external_calls": {"routing": 1, "geocoding": 0}, "elapsed_ms": 1416.8,
           "assumptions": {"max_range_miles": 500, "miles_per_gallon": 10, "...": "..."}},
  "map_url": "http://127.0.0.1:8000/map/?start=…&finish=…"
}
```

Errors return `{"error": "…"}` with status `400` for bad or non-US input, and `422` when no route exists or a stretch of road has no station within range.

### `GET /map/?start=…&finish=…`

An HTML page (Leaflet + OpenStreetMap tiles) showing the route, numbered fuel stops, and the cost breakdown. Map tiles load in the browser and don't use the routing API.

## How it works

```
request ─► resolve start/finish ─► OSRM route (1 call, cached) ─► stations near route ─► optimizer ─► JSON / map
            (local Census table)                                    (KD-tree, ~20 ms)      (greedy, <1 ms)
```

1. **Station coordinates (offline, once).** The price file has no coordinates, only highway-exit addresses. `load_stations`:
   - matches each city/state to the US Census Gazetteer places and county-subdivision files, which covers 97% of stations;
   - geocodes the remaining ~150 small unincorporated places once with Nominatim (`load_stations --geocode-missing`; 5 that Nominatim couldn't find are pinned by hand) and caches them in `data/geocode_overrides.json`, so later loads need no network. All 6,626 US stations are placed;
   - keeps only the lowest price for duplicate OPIS IDs and drops Canadian rows.
2. **Locations.** `"City, ST"` is resolved from the same local Census table, so typical requests make no geocoding call.
3. **Route.** One call to the public [OSRM](https://project-osrm.org/) server returns the full route geometry, as an encoded polyline to keep the transfer small. Results are cached for 24 hours.
4. **Corridor search.** All stations are held in memory in a KD-tree over 3D unit vectors, which gives exact great-circle radius queries. The route is densified to 1-mile steps, and stations within 10 miles of it are kept. Each one gets a mile marker along the route.
5. **Optimizer.** This is the classic fixed-route "gas station problem". At each stop:
   - if a cheaper station (or the destination) is within range, buy only enough fuel to reach it;
   - otherwise fill up and go to the cheapest station in range.

   This greedy rule gives the minimum total cost. The tests check it against a brute-force dynamic program on random cases.
6. **Fewer stops.** The cheapest plan often includes tiny top-ups, such as 1 gallon at one station and then a fill-up 12 miles later at a station that is 4¢ cheaper. A second pass removes a stop by moving its purchase to the stop before it (if the tank has room) or the stop after it (if the vehicle can still reach it). It repeats this, cheapest removal first, while each removal costs less than `min_stop_savings`. The response reports both plans:

   | Route | Cheapest plan | Default (`$5`) plan |
   |---|---|---|
   | Chicago → Dallas | 7 stops, $283.14 | 3 stops, $284.51 |
   | New York → Los Angeles | 17 stops, $852.47 | 8 stops, $858.35 |
   | Seattle → Miami | 20 stops, $1,026.91 | 9 stops, $1,033.89 |

## Assumptions

- **Start with an empty tank.** The first stop is the cheapest station within the first 30 route miles, and the fuel for those miles is charged at that stop. Total gallons therefore always equals route miles ÷ 10.
- **Station positions are city centroids.** Truck stops sit at highway exits, usually close to the route, so stations within 10 miles of the route line count as "on route", and detours are not charged.
- **Arrive empty.** The plan buys only the fuel the trip needs.
- **Cost first, but practical.** By default a stop is dropped if it saves less than $5. Use `min_stop_savings=0` for the strictly cheapest plan; `cheapest_possible_cost` is always included in the response.
- All of these settings (range, mpg, corridor width, start window, cache TTL, routing URL) can be changed through environment variables; see `.env.example`.

## Performance

| Stage | Time (NY → LA, 2,810 mi) |
|---|---|
| Resolve locations (local DB) | ~2 ms |
| OSRM route (network) | ~1.2–1.6 s |
| Corridor search (459 stations found) | ~90 ms |
| Optimizer | <1 ms |
| Cached repeat request | ~2 ms |
| Same route, different `min_stop_savings` (route cached, no API call) | ~20–140 ms |

The public OSRM demo server accounts for almost all of the uncached time. For production, run your own OSRM instance and set `OSRM_URL`, and use Redis instead of the in-process cache.

## Project layout

```
config/                      Django settings and URLs
fuel/models.py               Place (Census centroids), FuelStation
fuel/management/commands/load_stations.py
fuel/services/geo.py         start/finish resolution
fuel/services/routing.py     OSRM client (single call, cached)
fuel/services/stations.py    in-memory KD-tree + route corridor search
fuel/services/optimizer.py   cheapest refuelling plan
fuel/services/planner.py     ties it all together, builds the response
fuel/views.py                REST endpoint + map page
fuel/templates/fuel/map.html Leaflet map
data/geocode_overrides.json  coordinates for the ~150 places not in the Census files
                             (the Census files themselves are downloaded by load_stations)
```
