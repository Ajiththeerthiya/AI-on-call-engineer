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
        notify.safe(lambda: notify.startup_seed_events(graph.counts()))


def _notified(records):
    """Short text about Discord for error messages shown in the UI."""
    if not records:
        return ""
    r = records[0]
    return f" Discord: {r['status']}{' – ' + r['reason'] if r.get('reason') else ''}."


@app.get("/graph")
def get_graph():
    return graph.service_map()


@app.post("/break")
def break_service(req: BreakRequest):
    """Always a NEW failure: new deployment, new alert. Old incidents are untouched.
    No Discord message yet: the incident (and its message) is created by /investigate,
    which the UI calls right after this."""
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
    # Incident created + investigation completed, each posted once per incident.
    result["notifications"] = notify.safe(notify.investigation_events, result)
    return result


@app.post("/resolve")
def resolve(req: ResolveRequest):
    """Resolve ONE incident. Calling it twice is safe (already_resolved: true, no double count,
    no second Discord message)."""
    try:
        saved = graph.resolve_incident(req.incident_id, req.runbook_id, req.resolved_by)
    except graph.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except graph.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # database down etc.: tell the UI the fix was NOT saved
        log.exception("resolve failed")
        reason = f"Could not save the fix: {type(e).__name__}"
        sent = notify.safe(notify.resolve_failed_events, req.incident_id, reason)
        raise HTTPException(status_code=500, detail=reason + _notified(sent))

    records = notify.safe(notify.resolved_events, saved)  # [] when already resolved
    return {"ok": True, "discord": records[0] if records else None, "notifications": records, **saved}


@app.post("/clear")
def clear_breakages():
    """Undo every open breakage but KEEP history (resolved incidents, runbook counts).
    Different from /reset, which deletes everything and reloads data/."""
    try:
        pending = graph.live_bad_deployments()
    except Exception:
        pending = []
    try:
        cleared = graph.clear_breakages()
    except Exception as e:
        log.exception("clear failed")
        reason = f"Could not clear breakages: {type(e).__name__}"
        sent = notify.safe(notify.clear_failed_events, reason, pending)
        raise HTTPException(status_code=500, detail=reason + _notified(sent))
    records = notify.safe(notify.clear_events, cleared)
    return {"ok": True, **cleared, "incidents": graph.incident_summary(),
            "graph": graph.service_map(), "notifications": records}


@app.get("/critical-services")
def critical_services(limit: int = 5):
    """PageRank over DEPENDS_ON: the services an outage would hurt most."""
    limit = max(1, min(limit, 15))
    cypher, services = graph.critical_overview(limit)
    return {"algorithm": "pageRank", "cypher": cypher.strip(), "services": services}


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    inc = graph.get_incident(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    return inc


@app.get("/notifications")
def recent_notifications(limit: int = 20):
    """The latest Discord messages and their real delivery status (newest first).
    'pending' ones turn into 'sent' or 'failed' here once Discord answers."""
    return {"notifications": notify.recent(limit)}


@app.post("/reset")
def reset():
    """Full reseed, as designed for the demo: deletes the graph (including incidents
    created during the session) and reloads data/. Only this endpoint does that.
    The Discord message is sent only AFTER the reseed succeeded."""
    try:
        seed.seed()
        counts = graph.counts()
    except Exception as e:
        log.exception("reset failed")
        reason = f"Reset failed: {type(e).__name__}"
        sent = notify.safe(notify.reset_failed_events, reason)
        raise HTTPException(status_code=500, detail=reason + _notified(sent))
    notify.forget_dedup()
    records = notify.safe(notify.reset_events, counts)
    return {"ok": True, "counts": counts, "graph": graph.service_map(), "notifications": records}
