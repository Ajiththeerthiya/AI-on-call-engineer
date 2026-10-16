"""Connection to FalkorDB + the core Cypher queries.

Every query is a fixed, tested Cypher string. The agent never writes Cypher.
It only chooses which of these functions to call.
Each function returns (cypher, rows) so the UI can show the exact query that ran.
"""
import os
import time

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

GRAPH_NAME = "roottrace"
_graph = None


def get_graph():
    """Open the connection once and reuse it."""
    global _graph
    if _graph is None:
        db = FalkorDB(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            password=os.getenv("FALKORDB_PASSWORD") or None,
        )
        _graph = db.select_graph(GRAPH_NAME)
    return _graph


def read(cypher, params=None):
    """Read-only query. FalkorDB refuses any write inside it."""
    return get_graph().ro_query(cypher, params or {}).result_set


def write(cypher, params=None):
    return get_graph().query(cypher, params or {}).result_set


# ---------------------------------------------------------------------------
# Query 1: Blast radius. "What breaks if this service goes down?"
# Follow DEPENDS_ON backwards, up to 5 hops.
# ---------------------------------------------------------------------------
BLAST_RADIUS = """
MATCH (broken:Service {name: $name})<-[:DEPENDS_ON*1..5]-(affected:Service)
RETURN DISTINCT affected.name AS service, affected.tier AS tier
ORDER BY tier, service
"""


def blast_radius(name):
    rows = read(BLAST_RADIUS, {"name": name})
    return BLAST_RADIUS, [{"service": r[0], "tier": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Query 2: Root-cause path.
# Start at the service that alerted. Walk DEPENDS_ON forwards.
# Find the deepest service that had a deploy recently. That is the suspect.
# Returns the full path so the UI can highlight it.
# ---------------------------------------------------------------------------
ROOT_CAUSE_PATH = """
MATCH p = (start:Service {name: $name})-[:DEPENDS_ON*0..5]->(suspect:Service)
MATCH (d:Deployment)-[:CHANGED]->(suspect)
WHERE d.at >= $since AND coalesce(d.rolled_back, false) = false
WITH p, suspect, d, length(p) AS hops
ORDER BY hops DESC, d.at DESC
LIMIT 1
RETURN [n IN nodes(p) | n.name] AS path,
       suspect.name AS root_cause,
       d.id AS deployment, d.version AS version, d.author AS author, d.at AS at
"""


def root_cause_path(name, minutes=60):
    since = int(time.time()) - minutes * 60
    rows = read(ROOT_CAUSE_PATH, {"name": name, "since": since})
    if not rows:
        return ROOT_CAUSE_PATH, None
    r = rows[0]
    return ROOT_CAUSE_PATH, {
        "path": r[0], "root_cause": r[1], "deployment": r[2],
        "version": r[3], "author": r[4], "at": r[5],
    }


# ---------------------------------------------------------------------------
# Query 3: Memory. "Have we seen this before? Who fixed it? How?"
# ---------------------------------------------------------------------------
RECALL_INCIDENTS = """
MATCH (i:Incident {status: 'resolved'})-[:ROOT_CAUSE]->(:Service {name: $name})
OPTIONAL MATCH (i)-[:FIXED_WITH]->(r:Runbook)
OPTIONAL MATCH (i)-[:RESOLVED_BY]->(p:Person)
RETURN i.id AS incident, i.title AS title, i.resolved_at AS resolved_at,
       r.id AS runbook, r.title AS runbook_title, r.success_count AS runbook_wins,
       p.name AS fixed_by
ORDER BY resolved_at DESC
"""


def recall_incidents(name):
    rows = read(RECALL_INCIDENTS, {"name": name})
    keys = ["incident", "title", "resolved_at", "runbook", "runbook_title", "runbook_wins", "fixed_by"]
    return RECALL_INCIDENTS, [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------------------
# Query 4: Critical services. PageRank over DEPENDS_ON.
# Services that many others depend on get a high score.
# ---------------------------------------------------------------------------
CRITICAL_SERVICES = """
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS service, score
ORDER BY score DESC
LIMIT $limit
"""


def critical_services(limit=5):
    rows = read(CRITICAL_SERVICES, {"limit": limit})
    return CRITICAL_SERVICES, [{"service": r[0], "score": round(r[1], 4)} for r in rows]


# ---------------------------------------------------------------------------
# Helper query: who owns this service, and where do we message them?
# ---------------------------------------------------------------------------
FIND_OWNER = """
MATCH (s:Service {name: $name})<-[:OWNS]-(t:Team)
OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t)
RETURN t.name AS team, t.discord_channel AS channel, collect(p.name) AS people
"""


def find_owner(name):
    rows = read(FIND_OWNER, {"name": name})
    if not rows:
        return FIND_OWNER, None
    r = rows[0]
    return FIND_OWNER, {"team": r[0], "channel": r[1], "people": r[2]}


# ---------------------------------------------------------------------------
# For the UI: the whole service map (matches GET /graph in docs/api.md)
# ---------------------------------------------------------------------------
def service_map():
    nodes = read("""
        MATCH (s:Service)
        OPTIONAL MATCH (t:Team)-[:OWNS]->(s)
        RETURN s.name, s.kind, s.tier, t.name
        ORDER BY s.name
    """)
    edges = read("""
        MATCH (a:Service)-[:DEPENDS_ON]->(b:Service)
        RETURN a.name, b.name
    """)
    return {
        "nodes": [{"id": n[0], "label": "Service", "kind": n[1], "tier": n[2], "team": n[3]}
                  for n in nodes],
        "edges": [{"from": e[0], "to": e[1], "type": "DEPENDS_ON"} for e in edges],
    }


# ---------------------------------------------------------------------------
# Writes used by the demo
# ---------------------------------------------------------------------------
def _next_id(label, prefix):
    rows = read(f"MATCH (n:{label}) RETURN count(n)")
    return f"{prefix}{rows[0][0] + 1:03d}"


def break_service(name):
    """Simulate an outage: a bad deploy on `name`, and an alert on a service
    2 hops up (users see the symptom far from the real cause)."""
    now = int(time.time())
    dep_id = _next_id("Deployment", "D-")
    alert_id = _next_id("Alert", "A-")

    # Pick where the alert fires: 2 hops up if possible, else 1 hop, else the service itself.
    rows = read("""
        MATCH (b:Service {name: $name})
        OPTIONAL MATCH (b)<-[:DEPENDS_ON]-(:Service)<-[:DEPENDS_ON]-(up2:Service)
        OPTIONAL MATCH (b)<-[:DEPENDS_ON]-(up1:Service)
        RETURN collect(DISTINCT up2.name), collect(DISTINCT up1.name)
    """, {"name": name})
    up2, up1 = rows[0] if rows else ([], [])
    alert_on = "checkout" if "checkout" in up2 + up1 else (sorted(up2) or sorted(up1) or [name])[0]

    write("""
        MATCH (s:Service {name: $name}), (a:Service {name: $alert_on})
        CREATE (d:Deployment {id: $dep_id, version: 'v9.9.9-bad', author: 'deploy-bot', at: $now})
        CREATE (d)-[:CHANGED]->(s)
        CREATE (al:Alert {id: $alert_id, message: $alert_on + ': error rate over 30%',
                          severity: 'critical', at: $now})
        CREATE (al)-[:FIRED_ON]->(a)
    """, {"name": name, "alert_on": alert_on, "dep_id": dep_id,
          "alert_id": alert_id, "now": now})
    return {"alert_id": alert_id, "deployment_id": dep_id, "alert_on": alert_on}


def get_alert(alert_id):
    rows = read("""
        MATCH (a:Alert {id: $id})-[:FIRED_ON]->(s:Service)
        RETURN a.id, a.message, a.severity, s.name
    """, {"id": alert_id})
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "message": r[1], "severity": r[2], "service": r[3]}


def save_incident(alert_id, root_cause, title):
    """Agent memory write: store the new incident and link it to the alert and root cause."""
    inc_id = _next_id("Incident", "INC-")
    write("""
        MATCH (a:Alert {id: $alert_id}), (s:Service {name: $root_cause})
        CREATE (i:Incident {id: $id, title: $title, status: 'open', started_at: $now})
        CREATE (i)-[:TRIGGERED_BY]->(a)
        CREATE (i)-[:ROOT_CAUSE]->(s)
    """, {"alert_id": alert_id, "root_cause": root_cause, "id": inc_id,
          "title": title, "now": int(time.time())})
    return inc_id


def resolve_incident(incident_id, runbook_id, resolved_by):
    """Agent memory write: mark fixed, link runbook + person, add 1 to the runbook's wins."""
    rows = write("""
        MATCH (i:Incident {id: $id}), (r:Runbook {id: $rb}), (p:Person {name: $who})
        SET i.status = 'resolved', i.resolved_at = $now,
            r.success_count = coalesce(r.success_count, 0) + 1
        MERGE (i)-[:FIXED_WITH]->(r)
        MERGE (i)-[:RESOLVED_BY]->(p)
        RETURN i.id, r.success_count, r.title
    """, {"id": incident_id, "rb": runbook_id, "who": resolved_by, "now": int(time.time())})
    # The fix rolled back the bad change, so it is no longer a suspect.
    write("""
        MATCH (i:Incident {id: $id})-[:ROOT_CAUSE]->(:Service)<-[:CHANGED]-(d:Deployment)
        WHERE d.at >= i.started_at - 3600
        SET d.rolled_back = true
    """, {"id": incident_id})
    if not rows:
        return None
    return {"incident_id": rows[0][0], "runbook_wins": rows[0][1], "runbook_title": rows[0][2]}
