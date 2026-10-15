"""Loads the JSON files in data/ into FalkorDB.

Run:  python seed.py
It deletes the old 'roottrace' graph first, so you always get the same clean demo data.
"""
import json
from datetime import datetime
from pathlib import Path

import graph

DATA = Path(__file__).resolve().parent.parent / "data"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def to_epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def seed():
    g = graph.get_graph()
    try:
        g.delete()  # start clean
    except Exception:
        pass  # graph did not exist yet

    # Indexes make the {name: ...} and {id: ...} lookups fast.
    for label, prop in [("Service", "name"), ("Team", "name"), ("Person", "name"),
                        ("Deployment", "id"), ("Alert", "id"), ("Incident", "id"),
                        ("Runbook", "id")]:
        graph.write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")

    graph.write("""
        UNWIND $rows AS row
        MERGE (:Team {name: row.name, discord_channel: row.discord_channel})
    """, {"rows": load("teams.json")})

    graph.write("""
        UNWIND $rows AS row
        MATCH (t:Team {name: row.team})
        MERGE (p:Person {name: row.name})
        SET p.email = row.email
        MERGE (p)-[:MEMBER_OF]->(t)
    """, {"rows": load("people.json")})

    services = load("services.json")
    graph.write("""
        UNWIND $rows AS row
        MATCH (t:Team {name: row.team})
        MERGE (s:Service {name: row.name})
        SET s.kind = row.kind, s.tier = row.tier
        MERGE (t)-[:OWNS]->(s)
    """, {"rows": services})

    edges = [{"from": s["name"], "to": d} for s in services for d in s["depends_on"]]
    graph.write("""
        UNWIND $rows AS row
        MATCH (a:Service {name: row.from}), (b:Service {name: row.to})
        MERGE (a)-[:DEPENDS_ON]->(b)
    """, {"rows": edges})

    deployments = load("deployments.json")
    for d in deployments:
        d["at"] = to_epoch(d["at"])
    graph.write("""
        UNWIND $rows AS row
        MATCH (s:Service {name: row.service})
        MERGE (d:Deployment {id: row.id})
        SET d.version = row.version, d.author = row.author, d.at = row.at
        MERGE (d)-[:CHANGED]->(s)
    """, {"rows": deployments})

    graph.write("""
        UNWIND $rows AS row
        MERGE (r:Runbook {id: row.id})
        SET r.title = row.title, r.steps = row.steps, r.success_count = 0
    """, {"rows": load("runbooks.json")})

    incidents = load("incidents.json")
    for i in incidents:
        i["started_at"] = to_epoch(i["started_at"])
        i["resolved_at"] = to_epoch(i["resolved_at"])
    graph.write("""
        UNWIND $rows AS row
        MATCH (alerted:Service {name: row.alert_on}),
              (cause:Service {name: row.root_cause}),
              (r:Runbook {id: row.runbook}),
              (p:Person {name: row.resolved_by})
        MERGE (a:Alert {id: 'A-' + row.id})
        SET a.message = row.alert_message, a.severity = row.severity, a.at = row.started_at
        MERGE (a)-[:FIRED_ON]->(alerted)
        MERGE (i:Incident {id: row.id})
        SET i.title = row.title, i.status = 'resolved',
            i.started_at = row.started_at, i.resolved_at = row.resolved_at
        MERGE (i)-[:TRIGGERED_BY]->(a)
        MERGE (i)-[:ROOT_CAUSE]->(cause)
        MERGE (i)-[:FIXED_WITH]->(r)
        MERGE (i)-[:RESOLVED_BY]->(p)
        SET r.success_count = r.success_count + 1
    """, {"rows": incidents})

    counts = graph.read("MATCH (n) RETURN labels(n)[0] AS label, count(n) ORDER BY label")
    rels = graph.read("MATCH ()-[r]->() RETURN count(r)")[0][0]
    print("Seeded graph 'roottrace':")
    for label, n in counts:
        print(f"  {label:<11} {n}")
    print(f"  relationships {rels}")


if __name__ == "__main__":
    seed()
