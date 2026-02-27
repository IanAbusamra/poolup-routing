import os
import json
from typing import List, Optional, Any, Dict

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="PoolUp Routing Service")

ROUTES_API_KEY = os.environ.get("MAPS_API_KEY")  # set in Cloud Run via Secret Manager


# -------------------------
# Data models
# -------------------------
class LatLng(BaseModel):
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")


class ComputeRoutesRequest(BaseModel):
    origin: LatLng
    destination: LatLng
    intermediates: Optional[List[LatLng]] = []
    travelMode: str = "DRIVE"  # DRIVE, BICYCLE, WALK, TWO_WHEELER (per Routes API)
    routingPreference: str = "TRAFFIC_AWARE"  # TRAFFIC_AWARE, TRAFFIC_AWARE_OPTIMAL, etc.
    computeAlternativeRoutes: bool = False


class ComputeMatrixRequest(BaseModel):
    origins: List[LatLng]
    destinations: List[LatLng]
    travelMode: str = "DRIVE"
    routingPreference: str = "TRAFFIC_AWARE"


class PlanRequest(BaseModel):
    event_location: LatLng
    # Minimal user model for now; expand later
    users: List[Dict[str, Any]]  # each user should include id, location{lat,lng}, has_car, seats_available


# -------------------------
# Helpers
# -------------------------
def require_api_key():
    if not ROUTES_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="MAPS_API_KEY is not set. Add it as a Secret env var in Cloud Run.",
        )


def _latlng_obj(p: LatLng) -> Dict[str, Any]:
    return {"latitude": p.lat, "longitude": p.lng}


def _parse_matrix_response(resp: requests.Response) -> Any:
    """
    computeRouteMatrix can return JSON array in REST. Some clients encounter streaming-like output.
    We'll handle both plain JSON and newline-delimited JSON (NDJSON).
    """
    try:
        return resp.json()
    except Exception:
        # Attempt NDJSON fallback
        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        out = []
        for ln in lines:
            out.append(json.loads(ln))
        return out


# -------------------------
# Routes
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/routes")
def routes(req: ComputeRoutesRequest):
    """
    Proxy to Google Routes API computeRoutes:
    POST https://routes.googleapis.com/directions/v2:computeRoutes
    """
    require_api_key()

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": ROUTES_API_KEY,
        # Field mask is REQUIRED; request only what you need to reduce cost/latency.
        "X-Goog-FieldMask": ",".join(
            [
                "routes.duration",
                "routes.distanceMeters",
                "routes.polyline.encodedPolyline",
                "routes.legs.duration",
                "routes.legs.distanceMeters",
            ]
        ),
    }

    body = {
        "origin": {"location": {"latLng": _latlng_obj(req.origin)}},
        "destination": {"location": {"latLng": _latlng_obj(req.destination)}},
        "travelMode": req.travelMode,
        "routingPreference": req.routingPreference,
        "computeAlternativeRoutes": req.computeAlternativeRoutes,
    }

    if req.intermediates:
        body["intermediates"] = [{"location": {"latLng": _latlng_obj(p)}} for p in req.intermediates]

    r = requests.post(url, headers=headers, json=body, timeout=20)

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"google_status": r.status_code, "google_body": r.text},
        )

    return r.json()


@app.post("/matrix")
def matrix(req: ComputeMatrixRequest):
    """
    Proxy to Google Routes API computeRouteMatrix:
    POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix
    """
    require_api_key()

    url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": ROUTES_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,distanceMeters,duration",
    }

    body = {
        "origins": [{"waypoint": {"location": {"latLng": _latlng_obj(p)}}} for p in req.origins],
        "destinations": [{"waypoint": {"location": {"latLng": _latlng_obj(p)}}} for p in req.destinations],
        "travelMode": req.travelMode,
        "routingPreference": req.routingPreference,
    }

    r = requests.post(url, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail={"google_status": r.status_code, "google_body": r.text})

    return _parse_matrix_response(r)


@app.post("/plan")
def plan(req: PlanRequest):
    """
    Placeholder MVP: returns input + TODO.
    Next step: implement assignment (riders->cars) + per-car stop ordering using /matrix then /routes.
    """
    # This is intentionally a stub so you can deploy and iterate quickly.
    return {
        "message": "plan() not implemented yet. Service is live.",
        "event_location": req.event_location,
        "user_count": len(req.users),
        "next_steps": [
            "Identify drivers (has_car=true) and capacities",
            "Call /matrix for travel-time costs",
            "Assign riders to drivers (greedy first, OR-Tools later)",
            "Order pickups per car (nearest-neighbor / small TSP heuristic)",
            "Call /routes per car to get final ETAs + polyline",
        ],
    }
