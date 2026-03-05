from __future__ import annotations

import os
import json
import itertools
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# =========================
# Config
# =========================
MAPS_API_KEY = os.environ.get("MAPS_API_KEY")
ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_COMPUTE_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

DEFAULT_OPTIONS = {
    "travelMode": "DRIVE",
    "routingPreference": "TRAFFIC_AWARE",
    "bruteforceMaxEvaluations": 3000,
    "bruteforceMaxStopsPerDriver": 8,   # pickup stops per driver (passengers)
    "polylineMaxPoints": 500,           # downsample decoded polyline to max points
    "refineTopK": 3                     # computeRoutes calls for top candidates
}


# =========================
# Models (contract)
# =========================
class LatLng(BaseModel):
    lat: float
    lng: float

class Event(BaseModel):
    eventId: str
    destination: LatLng
    departureTime: Optional[str] = None  # ISO8601 string (we pass through)

class Person(BaseModel):
    userId: str
    role: str = Field(..., description="DRIVER or PASSENGER")
    start: Optional[LatLng] = None       # DRIVER
    pickup: Optional[LatLng] = None      # PASSENGER
    seats: Optional[int] = 0             # DRIVER passenger seats (not counting driver)
    mustRideWith: Optional[List[str]] = []
    avoidRideWith: Optional[List[str]] = []

class Constraints(BaseModel):
    maxPickupDetourMinutes: Optional[int] = None  # not enforced in v1 (placeholder)

class Options(BaseModel):
    travelMode: Optional[str] = None
    routingPreference: Optional[str] = None
    bruteforceMaxEvaluations: Optional[int] = None
    bruteforceMaxStopsPerDriver: Optional[int] = None
    polylineMaxPoints: Optional[int] = None
    refineTopK: Optional[int] = None

class OptimizeRequest(BaseModel):
    event: Event
    people: List[Person]
    constraints: Optional[Constraints] = Constraints()
    options: Optional[Options] = Options()


app = FastAPI(title="PoolUp Routing Optimizer", version="1.0")


# =========================
# Polyline decoding
# =========================
def decode_polyline(encoded: str) -> List[Dict[str, float]]:
    """Decodes a Google encoded polyline into [{'lat':..,'lng':..}, ...]."""
    points = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        points.append({"lat": lat / 1e5, "lng": lng / 1e5})
    return points

def downsample_points(points: List[Dict[str, float]], max_points: int) -> List[Dict[str, float]]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = points[::step]
    # ensure last point included
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


# =========================
# Google Routes API calls
# =========================
def require_key():
    if not MAPS_API_KEY:
        raise HTTPException(500, "MAPS_API_KEY is not set")

def _latlng_obj(p: LatLng) -> Dict[str, Any]:
    return {"latitude": p.lat, "longitude": p.lng}

def compute_routes(origin: LatLng, destination: LatLng, intermediates: List[LatLng],
                   travel_mode: str, routing_pref: str, departure_time: Optional[str]) -> Dict[str, Any]:
    """
    Calls computeRoutes and requests steps + polyline.
    """
    require_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": MAPS_API_KEY,
        # Field mask is REQUIRED. :contentReference[oaicite:2]{index=2}
        "X-Goog-FieldMask": ",".join([
            "routes.duration",
            "routes.distanceMeters",
            "routes.polyline.encodedPolyline",
            "routes.legs.duration",
            "routes.legs.distanceMeters",
            "routes.legs.steps.navigationInstruction.instructions",
            "routes.legs.steps.distanceMeters",
            "routes.legs.steps.staticDuration",
            "routes.legs.steps.navigationInstruction.maneuver",
        ]),
    }

    body: Dict[str, Any] = {
        "origin": {"location": {"latLng": _latlng_obj(origin)}},
        "destination": {"location": {"latLng": _latlng_obj(destination)}},
        "travelMode": travel_mode,
        "routingPreference": routing_pref,
        "computeAlternativeRoutes": False,
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    if intermediates:
        body["intermediates"] = [{"location": {"latLng": _latlng_obj(p)}} for p in intermediates]

    # departureTime is allowed; if omitted Google defaults to request time. :contentReference[oaicite:3]{index=3}
    if departure_time:
        body["departureTime"] = departure_time

    r = requests.post(ROUTES_COMPUTE_URL, headers=headers, json=body, timeout=25)
    if r.status_code != 200:
        raise HTTPException(502, {"google_status": r.status_code, "google_body": r.text})
    return r.json()

def compute_matrix(points: List[LatLng], travel_mode: str, routing_pref: str) -> List[Dict[str, Any]]:
    """
    Full NxN duration matrix for the given points.
    """
    require_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": MAPS_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,condition,distanceMeters,duration",
    }
    body = {
        "origins": [{"waypoint": {"location": {"latLng": _latlng_obj(p)}}} for p in points],
        "destinations": [{"waypoint": {"location": {"latLng": _latlng_obj(p)}}} for p in points],
        "travelMode": travel_mode,
        "routingPreference": routing_pref,
    }
    r = requests.post(MATRIX_COMPUTE_URL, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, {"google_status": r.status_code, "google_body": r.text})

    # Matrix may come as JSON array or NDJSON; handle both
    try:
        return r.json()
    except Exception:
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

def dur_to_seconds(d: Any) -> int:
    # most commonly "123s"
    if isinstance(d, str) and d.endswith("s"):
        return int(float(d[:-1]))
    if isinstance(d, dict) and "seconds" in d:
        return int(d["seconds"])
    return int(d)

def build_cost_lookup(matrix: List[Dict[str, Any]]) -> Dict[Tuple[int, int], int]:
    out = {}
    for e in matrix:
        if e.get("condition") != "ROUTE_EXISTS":
            continue
        out[(e["originIndex"], e["destinationIndex"])] = dur_to_seconds(e["duration"])
    return out


# =========================
# Optimization (bruteforce with safety)
# =========================
@dataclass
class Driver:
    userId: str
    start: LatLng
    seats: int

@dataclass
class Passenger:
    userId: str
    pickup: LatLng
    mustRideWith: List[str]
    avoidRideWith: List[str]

def check_ride_constraints(assign: Dict[str, str], passengers: List[Passenger]) -> bool:
    """
    assign maps passengerId -> driverId (only assigned passengers included).
    Enforces:
      - mustRideWith: if both passengers present in request, they must share driver
      - avoidRideWith: if both present, they must not share driver
    """
    pmap = {p.userId: p for p in passengers}
    for p in passengers:
        if p.userId not in assign:
            continue
        d = assign[p.userId]
        for q in (p.mustRideWith or []):
            if q in pmap and q in assign and assign[q] != d:
                return False
        for q in (p.avoidRideWith or []):
            if q in pmap and q in assign and assign[q] == d:
                return False
    return True

def approx_route_duration(cost: Dict[Tuple[int,int], int], idx_order: List[int]) -> int:
    """
    Approx duration for visiting points in idx_order (list of point indices),
    computed from matrix lookup.
    """
    total = 0
    for a, b in zip(idx_order, idx_order[1:]):
        total += cost.get((a, b), 10**9)
    return total

def optimize(drivers: List[Driver], passengers: List[Passenger], destination: LatLng,
             travel_mode: str, routing_pref: str, departure_time: Optional[str],
             max_evals: int, max_stops_per_driver: int, refine_top_k: int) -> Dict[str, Any]:
    """
    Strategy:
      1) Build a shared point set for each driver evaluation (driver start + all passenger pickups + destination)
      2) Use computeRouteMatrix once per driver-specific point set? (too much)
         Instead: we compute per-driver point sets inside scoring (small sizes only).
      3) Enumerate assignments (passenger -> driver) and for each driver enumerate pickup permutations
         using matrix-approx scoring.
      4) Keep top K candidates and call computeRoutes to get exact duration + steps/polyline.
    """
    # Safety: if too big, fallback early
    if len(passengers) > 10 or len(drivers) > 4:
        return {"fallback": True, "reason": "Too many passengers/drivers for brute force"}

    passenger_ids = [p.userId for p in passengers]
    driver_ids = [d.userId for d in drivers]
    driver_index = {d.userId: i for i, d in enumerate(drivers)}

    evals = 0
    best_candidates: List[Tuple[int, Dict[str, List[str]]]] = []  # (approx_total_seconds, per_driver_ordered_pickups)

    # Enumerate assignments: each passenger chooses a driver (no "unassigned" here unless seats insufficient)
    # Represent as a list of driver indices of length N.
    # Total combos = D^N (works for small N).
    for choice in itertools.product(range(len(drivers)), repeat=len(passengers)):
        # capacity check + build assignment map
        counts = [0] * len(drivers)
        assign_map: Dict[str, str] = {}
        for pid, didx in zip(passenger_ids, choice):
            counts[didx] += 1
            assign_map[pid] = drivers[didx].userId

        if any(counts[i] > drivers[i].seats for i in range(len(drivers))):
            continue
        if not check_ride_constraints(assign_map, passengers):
            continue

        # For each driver, generate pickup permutations (bounded)
        per_driver_pickups: Dict[str, List[str]] = {d.userId: [] for d in drivers}
        for pid, didx in zip(passenger_ids, choice):
            per_driver_pickups[drivers[didx].userId].append(pid)

        # If any driver has too many stops, we refuse brute force (fallback)
        if any(len(per_driver_pickups[d.userId]) > max_stops_per_driver for d in drivers):
            return {"fallback": True, "reason": "Too many stops per driver for brute force"}

        approx_total = 0

        # For each driver, score best permutation by matrix approx (one matrix call per driver)
        per_driver_best_order: Dict[str, List[str]] = {}

        for d in drivers:
            pickups = per_driver_pickups[d.userId]
            if not pickups:
                per_driver_best_order[d.userId] = []
                continue

            # Build points: [driver_start] + [pickups...] + [destination]
            pts: List[LatLng] = [d.start] + [next(p.pickup for p in passengers if p.userId == pid) for pid in pickups] + [destination]

            matrix = compute_matrix(pts, travel_mode, routing_pref)
            cost = build_cost_lookup(matrix)

            pickup_indices = list(range(1, 1 + len(pickups)))
            best_perm = None
            best_perm_cost = 10**18

            # enumerate permutations of pickup indices
            for perm in itertools.permutations(pickup_indices):
                # route index order: start(0) -> perm... -> dest(last)
                idx_order = [0] + list(perm) + [len(pts) - 1]
                c = approx_route_duration(cost, idx_order)
                if c < best_perm_cost:
                    best_perm_cost = c
                    best_perm = perm

            approx_total += int(best_perm_cost)

            # map perm indices back to passenger ids in that pickup order
            ordered = []
            if best_perm:
                for idx in best_perm:
                    ordered.append(pickups[idx - 1])
            per_driver_best_order[d.userId] = ordered

        evals += 1
        # keep a small heap of best candidates (by approx_total)
        best_candidates.append((approx_total, per_driver_best_order))
        best_candidates.sort(key=lambda x: x[0])
        best_candidates = best_candidates[:max(10, refine_top_k * 5)]  # keep a few for refinement

        if evals >= max_evals:
            break

    if not best_candidates:
        return {"fallback": True, "reason": "No feasible assignment found", "evaluations": evals}

    # Refine top K candidates by exact computeRoutes calls
    refined = []
    for approx_total, per_driver_order in best_candidates[:refine_top_k]:
        exact_total = 0
        exact_plans = {}

        for d in drivers:
            ordered_pickups = per_driver_order.get(d.userId, [])
            # build intermediates in that order
            intermediates = [next(p.pickup for p in passengers if p.userId == pid) for pid in ordered_pickups]
            resp = compute_routes(
                origin=d.start,
                destination=destination,
                intermediates=intermediates,
                travel_mode=travel_mode,
                routing_pref=routing_pref,
                departure_time=departure_time,
            )
            r0 = resp["routes"][0]
            exact_total += dur_to_seconds(r0["duration"])
            exact_plans[d.userId] = resp

        refined.append((exact_total, per_driver_order, exact_plans))

    refined.sort(key=lambda x: x[0])
    best_exact_total, best_order, best_routes = refined[0]
    return {
        "fallback": False,
        "evaluations": evals,
        "bestTotalSeconds": best_exact_total,
        "bestOrder": best_order,
        "bestRoutes": best_routes,
    }


# =========================
# Endpoint
# =========================
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/optimize")
def optimize_endpoint(req: OptimizeRequest):
    require_key()

    opts_in = req.options.model_dump() if req.options else {}
    opts = {**DEFAULT_OPTIONS, **{k: v for k, v in opts_in.items() if v is not None}}

    travel_mode = opts["travelMode"]
    routing_pref = opts["routingPreference"]
    max_evals = int(opts["bruteforceMaxEvaluations"])
    max_stops = int(opts["bruteforceMaxStopsPerDriver"])
    poly_max = int(opts["polylineMaxPoints"])
    refine_k = int(opts["refineTopK"])

    # Split drivers/passengers
    drivers: List[Driver] = []
    passengers: List[Passenger] = []

    for p in req.people:
        role = p.role.upper()
        if role == "DRIVER":
            if not p.start:
                raise HTTPException(400, f"Driver {p.userId} missing start")
            seats = int(p.seats or 0)
            if seats < 0:
                raise HTTPException(400, f"Driver {p.userId} seats must be >= 0")
            drivers.append(Driver(userId=p.userId, start=p.start, seats=seats))
        elif role == "PASSENGER":
            if not p.pickup:
                raise HTTPException(400, f"Passenger {p.userId} missing pickup")
            passengers.append(Passenger(
                userId=p.userId,
                pickup=p.pickup,
                mustRideWith=list(p.mustRideWith or []),
                avoidRideWith=list(p.avoidRideWith or []),
            ))
        else:
            raise HTTPException(400, f"Invalid role for {p.userId}: {p.role}")

    if not drivers:
        return {
            "eventId": req.event.eventId,
            "status": "failed",
            "plans": [],
            "unassignedPassengers": [p.userId for p in passengers],
            "debug": {"evaluations": 0, "fallbackUsed": True, "reason": "No drivers"},
        }

    # Run optimizer
    result = optimize(
        drivers=drivers,
        passengers=passengers,
        destination=req.event.destination,
        travel_mode=travel_mode,
        routing_pref=routing_pref,
        departure_time=req.event.departureTime,
        max_evals=max_evals,
        max_stops_per_driver=max_stops,
        refine_top_k=refine_k,
    )

    if result.get("fallback"):
        # Simple fallback: assign passengers to drivers with remaining seats round-robin, keep given order
        unassigned = []
        plans = []
        seats_left = {d.userId: d.seats for d in drivers}
        assignment: Dict[str, str] = {}
        di = 0
        for pas in passengers:
            placed = False
            for _ in range(len(drivers)):
                d = drivers[di % len(drivers)]
                di += 1
                if seats_left[d.userId] > 0:
                    assignment[pas.userId] = d.userId
                    seats_left[d.userId] -= 1
                    placed = True
                    break
            if not placed:
                unassigned.append(pas.userId)

        # build routes for each driver with their assigned passengers (in input order)
        total = 0
        for d in drivers:
            rider_ids = [pid for pid, did in assignment.items() if did == d.userId]
            intermediates = [next(p.pickup for p in passengers if p.userId == pid) for pid in rider_ids]
            resp = compute_routes(d.start, req.event.destination, intermediates, travel_mode, routing_pref, req.event.departureTime)
            r0 = resp["routes"][0]
            total += dur_to_seconds(r0["duration"])

            poly_enc = r0.get("polyline", {}).get("encodedPolyline")
            pts = downsample_points(decode_polyline(poly_enc), poly_max) if poly_enc else []

            steps = []
            for leg in r0.get("legs", []):
                for step in leg.get("steps", []):
                    instr = (step.get("navigationInstruction") or {}).get("instructions")
                    steps.append({
                        "instruction": instr,
                        "distanceMeters": step.get("distanceMeters"),
                        "durationSeconds": dur_to_seconds(step.get("duration")) if step.get("duration") else None,
                        "maneuver": step.get("maneuver"),
                    })

            plans.append({
                "driverId": d.userId,
                "riders": rider_ids,
                "stopOrder": (
                    [{"type":"DRIVER_START","userId":d.userId,"lat":d.start.lat,"lng":d.start.lng}] +
                    [{"type":"PICKUP","userId":pid,"lat":next(p.pickup for p in passengers if p.userId==pid).lat,
                      "lng":next(p.pickup for p in passengers if p.userId==pid).lng} for pid in rider_ids] +
                    [{"type":"EVENT","userId":None,"lat":req.event.destination.lat,"lng":req.event.destination.lng}]
                ),
                "route": {
                    "durationSeconds": dur_to_seconds(r0["duration"]),
                    "distanceMeters": r0.get("distanceMeters"),
                    "polylineEncoded": poly_enc,
                    "polylinePoints": pts,
                    "steps": steps,
                }
            })

        return {
            "eventId": req.event.eventId,
            "status": "partial" if unassigned else "ok",
            "totalDurationSeconds": total,
            "plans": plans,
            "unassignedPassengers": unassigned,
            "debug": {
                "evaluations": result.get("evaluations", 0),
                "fallbackUsed": True,
                "reason": result.get("reason", "fallback"),
            },
        }

    # Build response from best exact routes
    total = result["bestTotalSeconds"]
    best_order: Dict[str, List[str]] = result["bestOrder"]
    best_routes: Dict[str, Any] = result["bestRoutes"]

    plans = []
    for d in drivers:
        resp = best_routes[d.userId]
        r0 = resp["routes"][0]
        rider_ids = best_order.get(d.userId, [])

        poly_enc = r0.get("polyline", {}).get("encodedPolyline")
        pts = downsample_points(decode_polyline(poly_enc), poly_max) if poly_enc else []

        steps = []
        for leg in r0.get("legs", []):
            for step in leg.get("steps", []):
                instr = (step.get("navigationInstruction") or {}).get("instructions")
                steps.append({
                    "instruction": instr,
                    "distanceMeters": step.get("distanceMeters"),
                    "durationSeconds": dur_to_seconds(step.get("duration")) if step.get("duration") else None,
                    "maneuver": step.get("maneuver"),
                })

        # build stopOrder list for mobile UI
        stop_order = [{"type":"DRIVER_START","userId":d.userId,"lat":d.start.lat,"lng":d.start.lng}]
        for pid in rider_ids:
            pick = next(p.pickup for p in passengers if p.userId == pid)
            stop_order.append({"type":"PICKUP","userId":pid,"lat":pick.lat,"lng":pick.lng})
        stop_order.append({"type":"EVENT","userId":None,"lat":req.event.destination.lat,"lng":req.event.destination.lng})

        plans.append({
            "driverId": d.userId,
            "riders": rider_ids,
            "stopOrder": stop_order,
            "route": {
                "durationSeconds": dur_to_seconds(r0["duration"]),
                "distanceMeters": r0.get("distanceMeters"),
                "polylineEncoded": poly_enc,
                "polylinePoints": pts,
                "steps": steps,
            }
        })

    # Anyone unassigned? (in brute force we force assignment; unassigned only in fallback)
    return {
        "eventId": req.event.eventId,
        "status": "ok",
        "totalDurationSeconds": total,
        "plans": plans,
        "unassignedPassengers": [],
        "debug": {"evaluations": result.get("evaluations", 0), "fallbackUsed": False},
    }
