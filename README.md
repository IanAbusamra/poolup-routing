# PoolUp Routing Service (Cloud Run)

Endpoints:
- GET /health
- POST /routes  -> proxies Google Routes API computeRoutes
- POST /matrix  -> proxies Google Routes API computeRouteMatrix
- POST /plan    -> stub (implement VRP here)

Requires env var:
- MAPS_API_KEY  (store in Secret Manager, attach to Cloud Run)
