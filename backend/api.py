"""RootTrace API. Matches docs/api.md.

Run:  uvicorn api:app --reload --port 8000   (then open http://localhost:8000/docs)
"""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent
import graph
import notify
import seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("roottrace.api")

app = FastAPI(title="RootTrace API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BreakRequest(BaseModel):
    service: str


class InvestigateRequest(BaseModel):
    alert_id: str


class ResolveRequest(BaseModel):
    incident_id: str
    runbook_id: str
    resolved_by: str


@app.on_event("startup")
def seed_if_empty():
    """If the graph is empty (new machine, or cloud DB was wiped), load the demo data."""
    try:
        count = graph.read("MATCH (s:Service) RETURN count(s)")[0][0]
    except Exception:
        count = 0
    if count == 0:
        seed.seed()


@app.get("/graph")
def get_graph():
    return graph.service_map()


@app.post("/break")
def break_service(req: BreakRequest):
    """Always a NEW failure: new deployment, new alert. Old incidents are untouched."""
    try:
        result = graph.break_service(req.service)
    except graph.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result  # alert_id, deployment_id (+ alert_on, service)


@app.post("/investigate")
def investigate(req: InvestigateRequest):
    result = agent.investigate(req.alert_id)  # Gemini + MCP, falls back to fixed steps
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/resolve")
def resolve(req: ResolveRequest):
    saved = graph.resolve_incident(req.incident_id, req.runbook_id, req.resolved_by)
    if saved is None:
        raise HTTPException(status_code=404, detail="Incident, runbook or person not found")
    rows = graph.read("MATCH (:Incident {id: $id})-[:ROOT_CAUSE]->(s:Service) RETURN s.name",
                      {"id": req.incident_id})
    discord = None
    if rows:
        discord = notify.send(rows[0][0], f"✅ {req.incident_id} resolved by {req.resolved_by} with "
                                          f"{req.runbook_id}. That runbook has now worked "
                                          f"{saved['runbook_wins']} times. Saved to RootTrace memory.")
    return {"ok": True, "runbook": req.runbook_id, "resolved_by": req.resolved_by,
            "discord": discord, **saved}


@app.post("/reset")
def reset():
    """Full reseed, as designed for the demo: deletes the graph (including incidents
    created during the session) and reloads data/. Only this endpoint does that."""
    seed.seed()
    return {"ok": True, "counts": graph.counts(), "graph": graph.service_map()}
