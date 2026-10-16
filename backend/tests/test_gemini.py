"""Gemini retry / fallback behaviour with a FAKE Gemini.
The investigation still runs through the real MCP tools (tools.py) and the real FalkorDB.
"""
import asyncio

import pytest
from google.genai import errors, types

import agent
import graph


def err(code, status):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "message": status.lower(), "status": status}})


# ---------------------------------------------------------------- unit: classify + retry

@pytest.mark.parametrize("e,kind", [
    (err(503, "UNAVAILABLE"), "transient"),
    (err(500, "INTERNAL"), "transient"),
    (err(429, "RESOURCE_EXHAUSTED"), "quota"),
    (err(403, "PERMISSION_DENIED"), "auth"),
    (err(400, "INVALID_ARGUMENT"), "bad_request"),
    (err(404, "NOT_FOUND"), "model"),
    (ConnectionError("reset"), "transient"),
    (ValueError("boom"), "other"),
])
def test_classify_error(e, kind):
    assert agent.classify_error(e) == kind


def run(coro):
    return asyncio.run(coro)


def test_503_is_retried_a_bounded_number_of_times():
    calls, sleeps = [], []

    async def call():
        calls.append(1)
        raise err(503, "UNAVAILABLE")

    async def sleep(d):
        sleeps.append(d)

    with pytest.raises(agent.GeminiFailure) as info:
        run(agent.generate_with_retry(call, sleep=sleep, attempts=3, base=1.0))
    assert info.value.kind == "transient" and info.value.attempts == 3
    assert len(calls) == 3 and len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.5 and 2.0 <= sleeps[1] <= 2.5  # exponential + jitter


def test_503_then_success_returns_response():
    seq = [err(503, "UNAVAILABLE"), "ok"]

    async def call():
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    async def sleep(d):
        pass

    assert run(agent.generate_with_retry(call, sleep=sleep, attempts=3)) == ("ok", 1)


@pytest.mark.parametrize("code,status", [(429, "RESOURCE_EXHAUSTED"), (403, "PERMISSION_DENIED"),
                                         (400, "INVALID_ARGUMENT")])
def test_non_transient_errors_are_not_retried(code, status):
    calls = []

    async def call():
        calls.append(1)
        raise err(code, status)

    with pytest.raises(agent.GeminiFailure):
        run(agent.generate_with_retry(call, attempts=3))
    assert len(calls) == 1


# ---------------------------------------------------------------- fake Gemini, real MCP tools

class FakeResponse:
    def __init__(self, calls=None, text=""):
        self.function_calls = calls or None
        self.text = text
        parts = [types.Part(function_call=c) for c in (calls or [])] or [types.Part(text=text)]
        self.candidates = [types.Candidate(content=types.Content(role="model", parts=parts))]


class FakeGemini:
    """Plays a script: each item is a list of (tool, args), a final text, or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = 0
        outer = self

        class Models:
            async def generate_content(self, **_):
                outer.requests += 1
                item = outer.script[0] if len(outer.script) == 1 else outer.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, str):
                    return FakeResponse(text=item)
                return FakeResponse(calls=[types.FunctionCall(name=n, args=a) for n, a in item])

        class Aio:
            models = Models()

        self.aio = Aio()


@pytest.fixture
def gemini(monkeypatch, fresh):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")

    def use(script):
        fake = FakeGemini(script)
        monkeypatch.setattr(agent, "make_client", lambda: fake)
        return fake
    return use


def good_script(alert_id, answer):
    return [
        [("root_cause_path", {"service": "checkout", "alert_id": alert_id})],
        [("recall_incidents", {"service": "payment-db"}), ("blast_radius", {"service": "payment-db"})],
        [("find_owner", {"service": "payment-db"})],
        [("save_incident", {"alert_id": alert_id, "root_cause": "payment-db", "title": "payment-db down"})],
        [("notify_team", {"service": "payment-db", "message": "payment-db broke"})],
        answer,
    ]


def test_7_gemini_503_retries_then_backup(gemini):
    fake = gemini([err(503, "UNAVAILABLE")])
    b = graph.break_service("payment-db")
    res = agent.investigate(b["alert_id"])
    assert res["mode"] == "fixed"
    assert res["fallback_kind"] == "transient" and "503" in res["fallback_reason"]
    assert res["gemini_attempts"] == agent.MAX_ATTEMPTS == fake.requests
    assert res["root_cause"] == "payment-db" and res["incident_status"] == "open"


def test_gemini_success_is_reported_as_gemini_and_grounded(gemini):
    b = graph.break_service("payment-db")
    gemini(good_script(b["alert_id"], f"Root cause is payment-db, deploy {b['deployment_id']}. "
                                      "I have seen this before: INC-007, fixed by Priya with RB-3."))
    res = agent.investigate(b["alert_id"])
    assert res["mode"] == "gemini" and res["gemini_retries"] == 0
    assert [s["tool"] for s in res["steps"]] == ["root_cause_path", "recall_incidents", "blast_radius",
                                                  "find_owner", "save_incident", "notify_team"]
    assert res["root_cause"] == "payment-db" and res["deployment_id"] == b["deployment_id"]
    assert res["ungrounded_ids"] == []
    assert res["seen_before"]["incident_id"] == "INC-007"
    notify = res["steps"][-1]["result"]
    assert notify["sent"] is False and notify["simulated"] is True  # no webhook in tests


def test_invented_ids_are_flagged(gemini):
    b = graph.break_service("payment-db")
    gemini(good_script(b["alert_id"], "Root cause is payment-db. Same as INC-999, fixed with RB-42."))
    res = agent.investigate(b["alert_id"])
    assert res["ungrounded_ids"] == ["INC-999", "RB-42"]


def test_gemini_saves_then_503_backup_reuses_same_incident(gemini):
    b = graph.break_service("payment-db")
    gemini([
        [("root_cause_path", {"service": "checkout", "alert_id": b["alert_id"]})],
        [("save_incident", {"alert_id": b["alert_id"], "root_cause": "payment-db", "title": "t"})],
        err(503, "UNAVAILABLE"),
    ])
    res = agent.investigate(b["alert_id"])
    assert res["mode"] == "fixed"
    n = graph.read("MATCH (i:Incident)-[:TRIGGERED_BY]->(:Alert {id:$a}) RETURN count(i)",
                   {"a": b["alert_id"]})[0][0]
    assert n == 1, "backup must not create a second incident for the same alert"


def test_answer_without_graph_tools_falls_back(gemini):
    gemini(["Root cause is the database."])  # no tool calls at all
    b = graph.break_service("payment-db")
    res = agent.investigate(b["alert_id"])
    assert res["mode"] == "fixed" and res["fallback_kind"] == "ungrounded"
