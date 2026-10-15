"""RootTrace API.

Right now every endpoint returns FAKE data (a "mock") that matches docs/api.md.
Person B builds the UI against this. Person A later swaps in real graph queries.
Run:  uvicorn api:app --reload --port 8000   (then open http://localhost:8000/docs)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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


@app.get("/graph")
def get_graph():
    # TODO (Person A): replace with a real FalkorDB query
    return {
        "nodes": [
            {"id": "frontend", "label": "Service", "kind": "service", "team": "Web"},
            {"id": "checkout", "label": "Service", "kind": "service", "team": "Payments"},
            {"id": "payment", "label": "Service", "kind": "service", "team": "Payments"},
            {"id": "payment-db", "label": "Service", "kind": "database", "team": "Payments"},
        ],
        "edges": [
            {"from": "frontend", "to": "checkout", "type": "DEPENDS_ON"},
            {"from": "checkout", "to": "payment", "type": "DEPENDS_ON"},
            {"from": "payment", "to": "payment-db", "type": "DEPENDS_ON"},
        ],
    }


@app.post("/break")
def break_service(req: BreakRequest):
    # TODO (Person A): create a real Alert + bad Deployment in the graph
    return {"alert_id": "A-101", "deployment_id": "D-021"}


@app.post("/investigate")
def investigate(req: InvestigateRequest):
    # TODO (Person A): run the real agent
    return {
        "steps": [
            {
                "tool": "blast_radius",
                "cypher": "MATCH (b:Service {name:$name})<-[:DEPENDS_ON*1..5]-(a) RETURN DISTINCT a.name",
                "result": ["payment", "checkout", "frontend"],
            }
        ],
        "root_cause": "payment-db",
        "path": ["frontend", "checkout", "payment", "payment-db"],
        "seen_before": {"incident_id": "INC-007", "fixed_by": "Priya", "runbook": "RB-3"},
        "incident_id": "INC-009",
        "answer": "Root cause is payment-db. A deploy 20 minutes ago changed it. "
                  "Priya fixed the same problem last time with runbook RB-3.",
    }


@app.post("/resolve")
def resolve(req: ResolveRequest):
    # TODO (Person A): save the fix to the graph as memory
    return {"ok": True}


@app.post("/reset")
def reset():
    # TODO (Person A): delete the graph and re-run seed.py
    return {"ok": True}
