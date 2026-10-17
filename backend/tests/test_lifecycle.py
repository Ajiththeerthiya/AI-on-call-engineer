"""Incident lifecycle through the real API and a real FalkorDB.
Gemini is off (no key), so /investigate uses the fixed backup.
"""
import time

import pytest
from fastapi.testclient import TestClient

import api
import graph

SEED_COUNTS = {"Alert": 8, "Deployment": 20, "Incident": 8, "Person": 12,
               "Runbook": 6, "Service": 15, "Team": 5}


@pytest.fixture
def client(fresh):
    return TestClient(api.app)


def break_and_investigate(client, service="payment-db"):
    broke = client.post("/break", json={"service": service}).json()
    res = client.post("/investigate", json={"alert_id": broke["alert_id"]}).json()
    return broke, res


def rb3_wins():
    return graph.read("MATCH (r:Runbook {id:'RB-3'}) RETURN r.success_count")[0][0]


def test_1_break_creates_open_incident(client):
    broke, res = break_and_investigate(client)
    assert broke["alert_id"] == "A-009" and broke["deployment_id"] == "D-021"
    assert res["mode"] == "fixed" and res["fallback_kind"] == "no_key"
    assert res["root_cause"] == "payment-db"
    assert res["path"] == ["checkout", "payment", "payment-db"]
    assert res["alert_id"] == broke["alert_id"]
    assert res["deployment_id"] == broke["deployment_id"]
    inc = client.get(f"/incidents/{res['incident_id']}").json()
    assert inc["status"] == "open"
    assert (inc["alert_id"], inc["deployment_id"], inc["root_cause"]) == ("A-009", "D-021", "payment-db")
    assert inc["started_at"] > 0


def test_2_resolve_persists_everything(client):
    _, res = break_and_investigate(client)
    before = rb3_wins()
    r = client.post("/resolve", json={"incident_id": res["incident_id"], "runbook_id": "RB-3",
                                      "resolved_by": "Priya"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["status"] == "resolved" and body["already_resolved"] is False
    assert body["runbook_wins"] == before + 1
    stored = graph.get_incident(res["incident_id"])
    assert stored["status"] == "resolved"
    assert (stored["runbook_id"], stored["resolved_by"]) == ("RB-3", "Priya")
    assert stored["resolved_at"] >= stored["started_at"]
    rels = graph.read("""MATCH (i:Incident {id:$id})-[:FIXED_WITH]->(r:Runbook),
                              (i)-[:RESOLVED_BY]->(p:Person) RETURN r.id, p.name""",
                      {"id": res["incident_id"]})
    assert rels == [["RB-3", "Priya"]]
    assert graph.read("MATCH (d:Deployment {id:'D-021'}) RETURN d.rolled_back")[0][0] is True


def test_3_4_5_break_again_new_incident_old_stays_resolved_and_is_remembered(client):
    _, first = break_and_investigate(client)
    client.post("/resolve", json={"incident_id": first["incident_id"], "runbook_id": "RB-3",
                                  "resolved_by": "Priya"})
    broke2, second = break_and_investigate(client)
    # 3. new failure -> new IDs everywhere
    assert second["incident_id"] != first["incident_id"]
    assert broke2["alert_id"] != first["alert_id"]
    assert second["deployment_id"] == broke2["deployment_id"] != first["deployment_id"]
    assert second["incident_status"] == "open"
    # 4. the old incident is still resolved
    assert graph.get_incident(first["incident_id"])["status"] == "resolved"
    # 5. memory recalls the fix we just saved
    seen = second["seen_before"]
    assert seen["incident_id"] == first["incident_id"]
    assert (seen["fixed_by"], seen["runbook"]) == ("Priya", "RB-3")
    assert seen["runbook_wins"] == 3
    assert first["incident_id"] in [h["incident"] for h in seen["history"]]


def test_6_repeated_resolve_does_not_double_count(client):
    _, res = break_and_investigate(client)
    body = {"incident_id": res["incident_id"], "runbook_id": "RB-3", "resolved_by": "Priya"}
    a = client.post("/resolve", json=body).json()
    b = client.post("/resolve", json=body).json()
    assert a["runbook_wins"] == b["runbook_wins"] == rb3_wins()
    assert b["already_resolved"] is True
    resolved = graph.read("MATCH (:Incident {status:'resolved'})-[:FIXED_WITH]->(:Runbook {id:'RB-3'}) "
                          "RETURN count(*)")[0][0]
    assert rb3_wins() == resolved
    # a different runbook for an already-resolved incident is refused, not silently applied
    other = client.post("/resolve", json={**body, "runbook_id": "RB-1"})
    assert other.status_code == 409


def test_resolve_errors_are_clear(client):
    _, res = break_and_investigate(client)
    assert client.post("/resolve", json={"incident_id": "INC-999", "runbook_id": "RB-3",
                                         "resolved_by": "Priya"}).status_code == 404
    r = client.post("/resolve", json={"incident_id": res["incident_id"], "runbook_id": "RB-99",
                                      "resolved_by": "Priya"})
    assert r.status_code == 404 and "RB-99" in r.json()["detail"]
    assert graph.get_incident(res["incident_id"])["status"] == "open"  # nothing changed


def test_retrying_investigate_does_not_duplicate_incident(client):
    broke, first = break_and_investigate(client)
    again = client.post("/investigate", json={"alert_id": broke["alert_id"]}).json()
    assert again["incident_id"] == first["incident_id"]
    n = graph.read("MATCH (i:Incident)-[:TRIGGERED_BY]->(:Alert {id:$a}) RETURN count(i)",
                   {"a": broke["alert_id"]})[0][0]
    assert n == 1


def test_save_with_unknown_alert_creates_nothing(fresh):
    with pytest.raises(graph.NotFound):
        graph.save_incident("A-NOPE", "payment-db", "x")
    assert graph.counts()["Incident"] == 8
    # the next real save still gets the next number (the old bug reused it)
    b = graph.break_service("payment-db")
    assert graph.save_incident(b["alert_id"], "payment-db", "x")["incident_id"] == "INC-009"


def test_two_open_breaks_each_get_their_own_root_cause(client):
    b1, r1 = break_and_investigate(client, "payment-db")
    time.sleep(1.1)  # deploy times are whole seconds
    b2, r2 = break_and_investigate(client, "currency")
    assert (r1["root_cause"], r1["deployment_id"]) == ("payment-db", b1["deployment_id"])
    assert (r2["root_cause"], r2["deployment_id"]) == ("currency", b2["deployment_id"])
    assert r2["path"] == ["checkout", "currency"]
    assert r1["incident_id"] != r2["incident_id"]


def test_9_reset_reseeds_and_returns_state(client):
    _, res = break_and_investigate(client)
    r = client.post("/reset").json()
    assert r["ok"] is True
    assert r["counts"] == SEED_COUNTS
    assert len(r["graph"]["nodes"]) == 15 and len(r["graph"]["edges"]) > 0
    assert graph.get_incident(res["incident_id"]) is None  # session incidents are gone (by design)
    assert rb3_wins() == 2  # back to the seed history


def test_break_unknown_service_is_404(client):
    assert client.post("/break", json={"service": "nope"}).status_code == 404


# ---------------------------------------------------------------- Clear breakages

def test_clear_breakages_keeps_memory_and_cancels_open_work(client):
    # INC-009: broken and FIXED (this is memory we want to keep)
    _, fixed = break_and_investigate(client, "payment-db")
    client.post("/resolve", json={"incident_id": fixed["incident_id"], "runbook_id": "RB-3",
                                  "resolved_by": "Priya"})
    # INC-010 and INC-011: broken, NOT fixed
    b2, open1 = break_and_investigate(client, "payment-db")
    time.sleep(1.1)
    b3, open2 = break_and_investigate(client, "currency")
    wins_before = rb3_wins()

    r = client.post("/clear").json()
    assert r["ok"] is True
    assert r["cancelled_incidents"] == [open1["incident_id"], open2["incident_id"]]
    assert set(r["rolled_back_deployments"]) == {b2["deployment_id"], b3["deployment_id"]}
    assert r["incidents"] == {"resolved": 9, "cancelled": 2}
    assert len(r["graph"]["nodes"]) == 15

    # memory kept
    assert graph.get_incident(fixed["incident_id"])["status"] == "resolved"
    assert rb3_wins() == wins_before == 3
    # open work cancelled, bad deploys no longer suspects
    assert graph.get_incident(open1["incident_id"])["status"] == "cancelled"
    assert graph.read("MATCH (d:Deployment {version:'v9.9.9-bad'}) WHERE coalesce(d.rolled_back,false)=false "
                      "RETURN count(d)")[0][0] == 0
    # seed deploys untouched
    assert graph.read("MATCH (d:Deployment) WHERE d.version <> 'v9.9.9-bad' AND d.rolled_back = true "
                      "RETURN count(d)")[0][0] == 0


def test_after_clear_next_break_is_new_and_remembers_only_real_fixes(client):
    _, fixed = break_and_investigate(client, "payment-db")
    client.post("/resolve", json={"incident_id": fixed["incident_id"], "runbook_id": "RB-3",
                                  "resolved_by": "Priya"})
    _, left_open = break_and_investigate(client, "payment-db")
    client.post("/clear")

    broke, res = break_and_investigate(client, "payment-db")
    assert res["incident_id"] not in (fixed["incident_id"], left_open["incident_id"])
    assert res["deployment_id"] == broke["deployment_id"]  # the NEW deploy, not an old one
    assert res["seen_before"]["incident_id"] == fixed["incident_id"]  # real fix, not the cancelled one
    assert res["seen_before"]["runbook_wins"] == 3
    history = [h["incident"] for h in res["seen_before"]["history"]]
    assert left_open["incident_id"] not in history


def test_clear_twice_is_safe_and_cancelled_cannot_be_fixed(client):
    _, res = break_and_investigate(client, "payment-db")
    first = client.post("/clear").json()
    second = client.post("/clear").json()
    assert first["cancelled_incidents"] == [res["incident_id"]]
    assert second["cancelled_incidents"] == [] and second["rolled_back_deployments"] == []
    r = client.post("/resolve", json={"incident_id": res["incident_id"], "runbook_id": "RB-3",
                                      "resolved_by": "Priya"})
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]


def test_clear_with_nothing_broken_changes_nothing(client):
    before = graph.counts()
    r = client.post("/clear").json()
    assert r["cancelled_incidents"] == [] and r["rolled_back_deployments"] == []
    assert graph.counts() == before
    assert rb3_wins() == 2
