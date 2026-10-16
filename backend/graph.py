"""Connection to FalkorDB + the core Cypher queries.

Every query is a fixed, tested Cypher string. The agent never writes Cypher.
It only chooses which of these functions to call.
Each function returns (cypher, rows) so the UI can show the exact query that ran.
"""
import logging
import os
import time

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

# Tests set ROOTTRACE_GRAPH to a separate graph so they never touch the demo graph.
GRAPH_NAME = os.getenv("ROOTTRACE_GRAPH", "roottrace")
_graph = None
log = logging.getLogger("roottrace.graph")


class NotFound(Exception):
    """A node the request refers to does not exist."""


class Conflict(Exception):
    """The request clashes with what is already stored."""


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
# Find the service below it with the most recent deploy that happened
# BEFORE the alert (and is not rolled back). That deploy is the suspect.
# Ties (same second) go to the deeper service.
# Returns the full path so the UI can highlight it.
#
# Why "most recent" and not "deepest": if an older breakage is still open
# (say payment-db), a new break on currency must not be blamed on payment-db.
# ---------------------------------------------------------------------------
ROOT_CAUSE_PATH = """
MATCH p = (start:Service {name: $name})-[:DEPENDS_ON*0..5]->(suspect:Service)
MATCH (d:Deployment)-[:CHANGED]->(suspect)
WHERE d.at >= $since AND d.at <= $until AND coalesce(d.rolled_back, false) = false
WITH p, suspect, d, length(p) AS hops
ORDER BY d.at DESC, hops DESC
LIMIT 1
RETURN [n IN nodes(p) | n.name] AS path,
       suspect.name AS root_cause,
       d.id AS deployment, d.version AS version, d.author AS author, d.at AS at
"""


def root_cause_path(name, minutes=60, until=None):
    """`until` = the alert time (epoch seconds). Deploys after the alert cannot have caused it."""
    until = int(until if until is not None else time.time())
    since = until - minutes * 60
    rows = read(ROOT_CAUSE_PATH, {"name": name, "since": since, "until": until})
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
#
# IDs: the next number is computed INSIDE the same write query that creates
# the node. FalkorDB runs write queries on a graph one at a time, so two
# requests can never get the same number. (The old code counted nodes in a
# separate read first, so a failed write or a retry could reuse an ID.)
# ---------------------------------------------------------------------------
def _next_number(var, prefix_len):
    """Cypher expression: 1 + the highest number used so far for this label.
    Seed alerts are named 'A-INC-001' (not a number), so we also take the count
    into account. Must be used right after an aggregation over `var`."""
    return (f"coalesce(max(toInteger(substring({var}.id, {prefix_len}))), 0) AS m_{var}, "
            f"count({var}) AS c_{var}")


def _pick(var):
    return f"(CASE WHEN m_{var} > c_{var} THEN m_{var} ELSE c_{var} END + 1)"


def _fmt(prefix, n_expr):
    return (f"'{prefix}' + CASE WHEN {n_expr} < 1000 THEN right('00' + toString({n_expr}), 3) "
            f"ELSE toString({n_expr}) END")


def break_service(name):
    """Simulate an outage: a bad deploy on `name`, and an alert on a service
    up the chain (users see the symptom far from the real cause).
    Every call creates a NEW deployment and a NEW alert with fresh IDs."""
    now = int(time.time())
    if not read("MATCH (s:Service {name: $name}) RETURN s.name", {"name": name}):
        raise NotFound(f"Unknown service '{name}'")

    # Pick where the alert fires: 2 hops up if possible, else 1 hop, else the service itself.
    rows = read("""
        MATCH (b:Service {name: $name})
        OPTIONAL MATCH (b)<-[:DEPENDS_ON]-(:Service)<-[:DEPENDS_ON]-(up2:Service)
        OPTIONAL MATCH (b)<-[:DEPENDS_ON]-(up1:Service)
        RETURN collect(DISTINCT up2.name), collect(DISTINCT up1.name)
    """, {"name": name})
    up2, up1 = rows[0]
    alert_on = "checkout" if "checkout" in up2 + up1 else (sorted(up2) or sorted(up1) or [name])[0]

    rows = write(f"""
        MATCH (s:Service {{name: $name}}), (a:Service {{name: $alert_on}})
        OPTIONAL MATCH (d0:Deployment)
        WITH s, a, {_next_number('d0', 2)}
        WITH s, a, {_pick('d0')} AS dn
        OPTIONAL MATCH (a0:Alert)
        WITH s, a, dn, {_next_number('a0', 2)}
        WITH s, a, dn, {_pick('a0')} AS an
        CREATE (d:Deployment {{id: {_fmt('D-', 'dn')}, version: 'v9.9.9-bad',
                              author: 'deploy-bot', at: $now}})
        CREATE (d)-[:CHANGED]->(s)
        CREATE (al:Alert {{id: {_fmt('A-', 'an')}, message: $alert_on + ': error rate over 30%',
                          severity: 'critical', at: $now}})
        CREATE (al)-[:FIRED_ON]->(a)
        RETURN d.id, al.id
    """, {"name": name, "alert_on": alert_on, "now": now})
    dep_id, alert_id = rows[0]
    log.info("break %s: deployment %s, alert %s on %s", name, dep_id, alert_id, alert_on)
    return {"alert_id": alert_id, "deployment_id": dep_id, "alert_on": alert_on, "service": name}


def get_alert(alert_id):
    rows = read("""
        MATCH (a:Alert {id: $id})-[:FIRED_ON]->(s:Service)
        RETURN a.id, a.message, a.severity, s.name, a.at
    """, {"id": alert_id})
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "message": r[1], "severity": r[2], "service": r[3], "at": r[4]}


INCIDENT_FIELDS = """
       i.id AS incident_id, i.status AS status, i.title AS title,
       i.alert_id AS alert_id, i.deployment_id AS deployment_id, i.root_cause AS root_cause,
       i.started_at AS started_at, i.resolved_at AS resolved_at,
       i.runbook_id AS runbook_id, i.resolved_by AS resolved_by
"""
_INCIDENT_KEYS = ["incident_id", "status", "title", "alert_id", "deployment_id", "root_cause",
                  "started_at", "resolved_at", "runbook_id", "resolved_by"]


def get_incident(incident_id):
    """The stored incident, read back from FalkorDB (the source of truth)."""
    rows = read(f"""
        MATCH (i:Incident {{id: $id}})
        OPTIONAL MATCH (i)-[:TRIGGERED_BY]->(a:Alert)
        OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(s:Service)
        OPTIONAL MATCH (i)-[:FIXED_WITH]->(r:Runbook)
        OPTIONAL MATCH (i)-[:RESOLVED_BY]->(p:Person)
        RETURN {INCIDENT_FIELDS}, a.id, s.name, r.id, p.name
    """, {"id": incident_id})
    if not rows:
        return None
    r = rows[0]
    out = dict(zip(_INCIDENT_KEYS, r[:10]))
    # Seed incidents only have relationships, so fall back to them.
    out["alert_id"] = out["alert_id"] or r[10]
    out["root_cause"] = out["root_cause"] or r[11]
    out["runbook_id"] = out["runbook_id"] or r[12]
    out["resolved_by"] = out["resolved_by"] or r[13]
    return out


def incident_for_alert(alert_id):
    rows = read("MATCH (i:Incident)-[:TRIGGERED_BY]->(:Alert {id: $a}) RETURN i.id ORDER BY i.id LIMIT 1",
                {"a": alert_id})
    return rows[0][0] if rows else None


def save_incident(alert_id, root_cause, title):
    """Agent memory write: store the incident for this alert and link it to the
    alert and root cause.

    One alert = one incident. If this alert already has an incident (a retry, or
    Gemini saved it and then the backup ran), we return that one instead of
    creating a duplicate. A resolved incident is never re-opened.
    Returns {"incident_id", "status", "created"}. Raises NotFound if the alert
    or service does not exist (the old code returned a made-up ID)."""
    alert = get_alert(alert_id)
    if alert is None:
        raise NotFound(f"Alert '{alert_id}' not found")
    if not read("MATCH (s:Service {name: $n}) RETURN s.name", {"n": root_cause}):
        raise NotFound(f"Service '{root_cause}' not found")

    # The deploy this incident is about: latest live deploy on the root cause, before the alert.
    dep = read("""
        MATCH (d:Deployment)-[:CHANGED]->(:Service {name: $s})
        WHERE d.at <= $until AND d.at >= $until - 3600 AND coalesce(d.rolled_back, false) = false
        RETURN d.id ORDER BY d.at DESC LIMIT 1
    """, {"s": root_cause, "until": alert["at"]})
    deployment_id = dep[0][0] if dep else None

    rows = write(f"""
        MATCH (a:Alert {{id: $alert_id}}), (s:Service {{name: $root_cause}})
        OPTIONAL MATCH (old:Incident)-[:TRIGGERED_BY]->(a)
        WITH a, s, old WHERE old IS NULL
        OPTIONAL MATCH (x:Incident)
        WITH a, s, {_next_number('x', 4)}
        WITH a, s, {_pick('x')} AS n
        CREATE (i:Incident {{id: {_fmt('INC-', 'n')}, title: $title, status: 'open',
                            started_at: $now, alert_id: $alert_id, root_cause: $root_cause,
                            deployment_id: $deployment_id}})
        CREATE (i)-[:TRIGGERED_BY]->(a)
        CREATE (i)-[:ROOT_CAUSE]->(s)
        RETURN i.id
    """, {"alert_id": alert_id, "root_cause": root_cause, "title": title,
          "deployment_id": deployment_id, "now": int(time.time())})
    if rows:
        log.info("incident %s created for alert %s (root cause %s)", rows[0][0], alert_id, root_cause)
        return {"incident_id": rows[0][0], "status": "open", "created": True}

    existing = incident_for_alert(alert_id)
    inc = get_incident(existing)
    log.info("alert %s already has incident %s; not creating a duplicate", alert_id, existing)
    return {"incident_id": existing, "status": inc["status"], "created": False}


RUNBOOK_RECOUNT = """
MATCH (r:Runbook {id: $rb})
OPTIONAL MATCH (x:Incident {status: 'resolved'})-[:FIXED_WITH]->(r)
WITH r, count(x) AS wins
SET r.success_count = wins
RETURN wins, r.title
"""


def resolve_incident(incident_id, runbook_id, resolved_by):
    """Mark ONE incident fixed, link runbook + person, roll back its bad deploy.

    Safe to call twice: the second call changes nothing and reports
    already_resolved. The runbook's success_count is recounted from the stored
    resolved incidents, so it can never be counted twice.
    Raises NotFound (incident/runbook/person) or Conflict (already resolved
    with a different runbook or person)."""
    inc = get_incident(incident_id)
    if inc is None:
        raise NotFound(f"Incident '{incident_id}' not found")
    if not read("MATCH (r:Runbook {id: $rb}) RETURN r.id", {"rb": runbook_id}):
        raise NotFound(f"Runbook '{runbook_id}' not found")
    if not read("MATCH (p:Person {name: $who}) RETURN p.name", {"who": resolved_by}):
        raise NotFound(f"Person '{resolved_by}' not found")

    already = inc["status"] == "resolved"
    if already and (inc["runbook_id"], inc["resolved_by"]) != (runbook_id, resolved_by):
        raise Conflict(f"{incident_id} was already resolved by {inc['resolved_by']} "
                       f"with {inc['runbook_id']}")

    if not already:
        # WHERE status = 'open' makes this a no-op if a parallel request won the race.
        write("""
            MATCH (i:Incident {id: $id}), (r:Runbook {id: $rb}), (p:Person {name: $who})
            WHERE i.status = 'open'
            SET i.status = 'resolved', i.resolved_at = $now,
                i.runbook_id = $rb, i.resolved_by = $who
            MERGE (i)-[:FIXED_WITH]->(r)
            MERGE (i)-[:RESOLVED_BY]->(p)
        """, {"id": incident_id, "rb": runbook_id, "who": resolved_by, "now": int(time.time())})

        # The fix rolled back the bad change, so it is no longer a suspect.
        if inc["deployment_id"]:
            write("MATCH (d:Deployment {id: $d}) SET d.rolled_back = true", {"d": inc["deployment_id"]})
        else:  # incidents saved before this fix have no deployment_id: use the old time window
            write("""
                MATCH (i:Incident {id: $id})-[:ROOT_CAUSE]->(:Service)<-[:CHANGED]-(d:Deployment)
                WHERE d.at >= i.started_at - 3600
                SET d.rolled_back = true
            """, {"id": incident_id})

    wins, rb_title = write(RUNBOOK_RECOUNT, {"rb": runbook_id})[0]
    saved = get_incident(incident_id)
    log.info("incident %s resolved by %s with %s (already=%s)", incident_id, resolved_by, runbook_id, already)
    return {**saved, "runbook": runbook_id, "runbook_wins": wins, "runbook_title": rb_title,
            "already_resolved": already}


def counts():
    """Node counts per label, for /reset and checks."""
    return {label: n for label, n in read("MATCH (n) RETURN labels(n)[0], count(n)")}
