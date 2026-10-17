"""Discord notifications for every button, through the real API and a real FalkorDB
test graph. Discord itself is FAKED: nothing leaves this machine.

Fake webhooks:  Payments -> PAY, Platform -> PLAT, everything else -> DEFAULT.
(payment-db and checkout belong to Payments, currency to Platform, cart-redis to Commerce.)
"""
import json
import threading
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

import api
import graph
import notify
import seed

DEFAULT = "https://discord.com/api/webhooks/1/default-secret"
PAY = "https://discord.com/api/webhooks/2/payments-secret"
PLAT = "https://discord.com/api/webhooks/3/platform-secret"


class FakeDiscord:
    """Stands in for notify._post. Records (webhook, text) and answers like Discord."""

    def __init__(self):
        self.posts, self.error, self.delay = [], None, 0
        self.lock = threading.Lock()

    def __call__(self, url, text, timeout):
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        with self.lock:
            self.posts.append((url, text))
            return 200, json.dumps({"id": f"msg-{len(self.posts)}"}).encode()

    def to(self, url):
        return [t for u, t in self.posts if u == url]

    def titles(self, url=None):
        return [t.splitlines()[0] for u, t in self.posts if url is None or u == url]


@pytest.fixture
def discord(monkeypatch, fresh):
    fake = FakeDiscord()
    monkeypatch.setattr(notify, "_post", fake)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT)
    monkeypatch.setenv("DISCORD_WEBHOOK_PAYMENTS", PAY)
    monkeypatch.setenv("DISCORD_WEBHOOK_PLATFORM", PLAT)
    monkeypatch.setenv("DISCORD_WAIT_SECONDS", "5")
    notify.forget_dedup()
    notify._recent.clear()
    return fake


@pytest.fixture
def client(discord):
    return TestClient(api.app)


def break_it(client, service="payment-db"):
    broke = client.post("/break", json={"service": service}).json()
    return broke, client.post("/investigate", json={"alert_id": broke["alert_id"]}).json()


def fix(client, incident_id, runbook="RB-3", who="Priya"):
    return client.post("/resolve", json={"incident_id": incident_id, "runbook_id": runbook,
                                         "resolved_by": who})


# 1 -------------------------------------------------------------------------
def test_1_break_it_creates_incident_and_notifies(client, discord):
    broke, res = break_it(client)
    assert [n["event"] for n in res["notifications"]] == ["incident_created", "investigation_completed"]
    assert all(n["status"] == "sent" and n["message_id"] for n in res["notifications"])
    created, done = discord.to(PAY)
    for text in ("ROOTTRACE INCIDENT CREATED", "Incident:** INC-009", "Alert:** A-009",
                 "Deployment:** D-021 (v9.9.9-bad)", "Root cause:** payment-db",
                 "Path:** checkout → payment → payment-db", "Team:** Payments",
                 "Suggested runbook:** RB-3", "Status:** Open"):
        assert text in created, text
    assert "Affected services:** checkout, frontend, payment" in created
    assert "ROOTTRACE INVESTIGATION COMPLETED" in done
    assert "Recommended actions (NOT done yet" in done and "Roll back D-021 on payment-db" in done
    assert discord.to(DEFAULT) == [] and discord.to(PLAT) == []


# 2 -------------------------------------------------------------------------
def test_2_break_again_notifies_new_incident_only(client, discord):
    _, first = break_it(client)
    fix(client, first["incident_id"])
    before = len(discord.posts)
    _, second = break_it(client)
    new = discord.posts[before:]
    assert len(new) == 2
    assert all("INC-010" in t for _, t in new) and not any("INC-009 " in t.split("Seen")[0] for _, t in new)
    assert "Seen before:** INC-009 – fixed by Priya with RB-3" in new[1][1]


# 3 -------------------------------------------------------------------------
def test_3_clear_breakages_sends_one_summary_per_team(client, discord):
    _, pay = break_it(client, "payment-db")
    time.sleep(1.1)
    _, plat = break_it(client, "currency")
    before = len(discord.posts)
    r = client.post("/clear").json()
    assert sorted(n["status"] for n in r["notifications"]) == ["sent", "sent"]
    (pay_msg,) = [t for u, t in discord.posts[before:] if u == PAY]
    (plat_msg,) = [t for u, t in discord.posts[before:] if u == PLAT]
    assert "SIMULATED ROLLBACK COMPLETED" in pay_msg
    assert f"D-021 on payment-db → incident {pay['incident_id']}" in pay_msg
    assert f"{pay['incident_id']} (payment-db, now cancelled)" in pay_msg
    assert "currency" not in pay_msg.split("Team:")[0]
    assert f"D-022 on currency → incident {plat['incident_id']}" in plat_msg
    for msg in (pay_msg, plat_msg):
        assert "Confirmed in FalkorDB" in msg and "no real infrastructure was rolled back" in msg


def test_3b_teams_sharing_one_webhook_get_one_combined_message(client, discord, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_PAYMENTS")
    monkeypatch.delenv("DISCORD_WEBHOOK_PLATFORM")
    break_it(client, "payment-db")
    time.sleep(1.1)
    break_it(client, "currency")
    before = len(discord.posts)
    r = client.post("/clear").json()
    assert len(r["notifications"]) == 1 and len(discord.posts) == before + 1
    msg = discord.posts[-1][1]
    assert discord.posts[-1][0] == DEFAULT
    assert "[Payments]" in msg and "[Platform]" in msg


# 4 -------------------------------------------------------------------------
def test_4_clear_with_nothing_sends_info_once(client, discord):
    r = client.post("/clear").json()
    assert r["notifications"][0]["event"] == "clear_nothing" and r["notifications"][0]["status"] == "sent"
    assert "NOTHING TO CLEAR" in discord.to(DEFAULT)[0]
    again = client.post("/clear").json()
    assert again["notifications"][0]["status"] == "duplicate"
    assert len(discord.posts) == 1


# 5 -------------------------------------------------------------------------
def test_5_reset_notifies_only_after_success(client, discord, monkeypatch):
    r = client.post("/reset").json()
    assert r["notifications"][0]["status"] == "sent"
    msg = discord.to(DEFAULT)[-1]
    assert "ROOTTRACE DEMO RESET" in msg and "Production infrastructure:** Not affected" in msg
    assert "15 Service" in msg

    def broken_seed():
        raise RuntimeError("db down")
    monkeypatch.setattr(seed, "seed", broken_seed)
    before = len(discord.posts)
    bad = client.post("/reset")
    assert bad.status_code == 500 and "Discord: sent" in bad.json()["detail"]
    titles = discord.titles()[before:]
    assert titles == ["❌ **ROOTTRACE DEMO RESET FAILED**"]


# 6 -------------------------------------------------------------------------
def test_6_mark_as_fixed_notifies_after_success(client, discord):
    _, res = break_it(client)
    body = fix(client, res["incident_id"]).json()
    rec = body["notifications"][0]
    assert rec["event"] == "incident_resolved" and rec["status"] == "sent" and body["discord"] == rec
    msg = discord.to(PAY)[-1]
    for text in ("ROOTTRACE INCIDENT RESOLVED", f"Incident:** {res['incident_id']}", "Root cause:** payment-db",
                 "Resolved by:** Priya", "Runbook:** RB-3 – Raise payment-db connection pool",
                 "Successful fixes with this runbook:** 3", "Affected services:** checkout, frontend, payment",
                 "Memory:** Saved to FalkorDB", "Bad deploy marked rolled back:** D-021"):
        assert text in msg, text


# 7 -------------------------------------------------------------------------
def test_7_repeated_clicks_do_not_duplicate(client, discord):
    broke, res = break_it(client)
    client.post("/investigate", json={"alert_id": broke["alert_id"]})  # retry the investigation
    fix(client, res["incident_id"])
    second = fix(client, res["incident_id"]).json()
    assert second["notifications"] == [] and second["discord"] is None
    assert second["runbook_wins"] == 3
    titles = discord.titles(PAY)
    assert titles.count("🚨 **ROOTTRACE INCIDENT CREATED**") == 1
    assert titles.count("🔎 **ROOTTRACE INVESTIGATION COMPLETED**") == 1
    assert titles.count("✅ **ROOTTRACE INCIDENT RESOLVED**") == 1


def test_7b_guard_survives_a_backend_restart(client, discord):
    _, res = break_it(client)
    notify.forget_dedup()  # what a restart does to in-memory state
    assert notify.investigation_events(res) == []  # the guard is stored on the Incident in FalkorDB


# 8 -------------------------------------------------------------------------
def test_8_failed_database_operations_send_no_success(client, discord, monkeypatch):
    _, res = break_it(client)

    def down(*a, **k):
        raise ConnectionError("FalkorDB down")
    monkeypatch.setattr(graph, "resolve_incident", down)
    monkeypatch.setattr(graph, "clear_breakages", down)
    before = len(discord.posts)
    r1 = fix(client, res["incident_id"])
    r2 = client.post("/clear")
    assert r1.status_code == 500 and r2.status_code == 500
    titles = discord.titles()[before:]
    assert titles == ["❌ **ROOTTRACE: MARK AS FIXED FAILED**", "❌ **ROOTTRACE CLEAR BREAKAGES FAILED**"]
    assert "D-021" in discord.posts[-1][1]  # the deploy it tried to roll back


# 9 -------------------------------------------------------------------------
def test_9_discord_down_does_not_break_or_undo_anything(client, discord):
    discord.error = urllib.error.URLError(f"cannot reach {PAY}")
    _, res = break_it(client)
    assert res["incident_status"] == "open"
    assert {n["status"] for n in res["notifications"]} == {"failed"}
    body = fix(client, res["incident_id"])
    assert body.status_code == 200 and body.json()["status"] == "resolved"
    assert graph.get_incident(res["incident_id"])["status"] == "resolved"
    everything = json.dumps(body.json()) + json.dumps(client.get("/notifications").json())
    assert "payments-secret" not in everything and "default-secret" not in everything


# 10 ------------------------------------------------------------------------
def test_10_routing(client, discord, monkeypatch):
    break_it(client, "payment-db")
    time.sleep(1.1)
    break_it(client, "currency")
    assert len(discord.to(PAY)) == 2 and len(discord.to(PLAT)) == 2
    assert "Team:** Platform" in discord.to(PLAT)[0]
    time.sleep(1.1)
    _, res = break_it(client, "cart-redis")  # Commerce has no own webhook -> default
    assert {n["routed_via"] for n in res["notifications"]} == {"default"}
    assert "#team-commerce (Commerce)" in discord.to(DEFAULT)[0]

    for name in ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_PAYMENTS", "DISCORD_WEBHOOK_PLATFORM"):
        monkeypatch.delenv(name)
    before = len(discord.posts)
    r = client.post("/reset").json()
    assert r["notifications"][0]["status"] == "not_configured" and len(discord.posts) == before


# 11 ------------------------------------------------------------------------
def test_11_slow_discord_is_pending_then_confirmed(client, discord, monkeypatch):
    monkeypatch.setenv("DISCORD_WAIT_SECONDS", "0.05")
    discord.delay = 0.4
    t0 = time.time()
    r = client.post("/clear").json()
    assert time.time() - t0 < 0.4  # the UI did not wait for Discord
    assert r["notifications"][0]["status"] == "pending" and r["notifications"][0]["sent"] is False
    time.sleep(0.6)
    latest = client.get("/notifications").json()["notifications"][0]
    assert latest["status"] == "sent" and latest["message_id"] == "msg-1"


def test_messages_of_one_action_arrive_in_order(client, discord):
    discord.delay = 0.05
    for _ in range(3):
        _, res = break_it(client)
        time.sleep(1.1)
    titles = [t for t in discord.titles(PAY)]
    assert titles == ["🚨 **ROOTTRACE INCIDENT CREATED**", "🔎 **ROOTTRACE INVESTIGATION COMPLETED**"] * 3
