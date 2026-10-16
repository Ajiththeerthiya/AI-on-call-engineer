"""RootTrace API. Matches docs/api.md.

Run:  uvicorn api:app --reload --port 8000   (then open http://localhost:8000/docs)
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent
import graph
import notify
import seed

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
    result = graph.break_service(req.service)
    return {"alert_id": result["alert_id"], "deployment_id": result["deployment_id"]}


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
    seed.seed()
    return {"ok": True}
