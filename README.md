# PoolUp Routing Optimizer API

This service computes an optimized carpool pickup order and returns route geometry + step-by-step instructions.

It is currently deployed on Cloud Run and is publicly accessible.

## Summary: 
Very briefly: the routing algo is a brute-force assignment + local route ordering approach. It tries every passenger→driver assignment with itertools.product(...), filters out invalid ones by seats and mustRideWith / avoidRideWith, then for each driver in that candidate assignment it asks Google for a route matrix, tries every pickup permutation to find the cheapest stop order, keeps the best few candidates, and finally calls computeRoutes on the top refineTopK candidates to get the exact winning routes.  
The main reason for the insane number of API calls is that compute_matrix(...) is called inside the brute-force loop. So for each feasible assignment, and for each driver in that assignment, it makes a fresh Google Route Matrix request; with up to bruteforceMaxEvaluations = 3000, that multiplies fast. The exact-route compute_routes(...) calls for the top candidates are secondary; the real explosion is the repeated matrix calls during search, especially since there’s no caching of repeated driver/pickup subsets.    
A one-line summary: it’s brute force over assignments, and the expensive Google matrix request sits in the innermost search path.

---

## Base URL


https://poolup-routing-main-271904393612.us-central1.run.app


---

## Endpoint

### `POST /optimize`

**Content-Type:** `application/json`

Provide:
- event destination
- people list (drivers + passengers)

The response includes:
- `plans[]`: one plan per driver
- `plans[i].stopOrder`: the optimized stop sequence (driver start → pickups → event)
- `plans[i].route.polylineEncoded` and/or `plans[i].route.polylinePoints`: route geometry
- `plans[i].route.steps`: turn-by-turn instructions

---

## Quick start (cURL)

```bash
curl -X POST "https://poolup-routing-main-271904393612.us-central1.run.app/optimize" \
  -H "Content-Type: application/json" \
  -d '{
    "event": {
      "eventId": "demo_1",
      "destination": { "lat": 33.7488, "lng": -84.3880 }
    },
    "people": [
      { "userId": "d1", "role": "DRIVER", "start": { "lat": 33.7756, "lng": -84.3963 }, "seats": 2 },
      { "userId": "p1", "role": "PASSENGER", "pickup": { "lat": 33.7840, "lng": -84.3730 } },
      { "userId": "p2", "role": "PASSENGER", "pickup": { "lat": 33.7811, "lng": -84.3812 } }
    ]
  }'
```

Optional: easily try out queries at: https://poolup-routing-main-271904393612.us-central1.run.app/docs#/default/optimize_endpoint_optimize_post

## Visualize the response

After you receive the JSON response, you can visualize it by copying/pasting the response into:

https://poolup-visualizer.netlify.app/
