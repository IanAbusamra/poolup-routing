# PoolUp Routing Optimizer API

This service computes an optimized carpool pickup order and returns route geometry + step-by-step instructions.

It is currently deployed on Cloud Run and is publicly accessible.

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
Visualize the response

After you receive the JSON response, you can visualize it by copying/pasting the response into:

https://poolup-visualizer.netlify.app/
