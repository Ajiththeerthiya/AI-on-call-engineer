"""The investigation agent.

How it works:
1. agent.py starts tools.py as an MCP server and asks it for its tool list.
2. It gives those tools to Gemini, plus the alert.
3. Gemini decides which tool to call. We run the call through MCP and give the
   result back. This repeats until Gemini writes a final answer.
4. Every step (tool, Cypher, result) is recorded for the UI.

Safety net: if Gemini or MCP fails (no key, rate limit, network), we fall back
to run_fixed_investigation(), which runs the same queries in a fixed order.
The response field "mode" says which one ran: "gemini" or "fixed".
"""
import asyncio
import json
import logging
import os
import random
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

import graph

load_dotenv()

BACKEND = Path(__file__).resolve().parent
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_TURNS = 10           # safety limit on model calls per investigation
TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))  # per model call, transient errors only
BACKOFF_SECONDS = float(os.getenv("GEMINI_BACKOFF_SECONDS", "1.0"))

log = logging.getLogger("roottrace.agent")

SYSTEM_PROMPT = """You are RootTrace, an AI on-call engineer for an online shop made of microservices.
The service dependency graph, deploys, owners and past incidents live in a graph database.
You can only learn facts by calling tools. Never guess service names, people or fixes.

How to investigate an alert:
1. Call root_cause_path on the service that alerted, with the alert_id. The last service in the path is the root cause.
2. Call recall_incidents on the root cause to check if we have seen this before.
3. Call blast_radius on the root cause to measure impact.
4. Call find_owner on the root cause.
5. Call save_incident once, with the alert id, the root cause and a short title.
6. Call notify_team on the root cause with a short message: root cause, the bad deploy,
   impact, and the runbook that worked before (if any). RootTrace attaches your message to
   the investigation summary it posts to the team's Discord channel when you finish.
If a tool returns an error or no result, say so honestly and continue with what you know.

Grounding rules:
- Every ID (alert, deployment, incident, runbook), name and number in your answer must come
  from a tool result in this conversation. Never invent or guess one.
- Do not claim the team was notified or that a message was delivered: notify_team only
  queues your note, and RootTrace reports the real delivery result itself.
- Anything you suggest (roll back, apply a runbook) is a recommendation. Say so; never
  claim it was done.

Final answer: 3 to 5 short sentences in plain English. Include the root cause, the path,
the deploy that changed it, how many services are affected, and the owner team.
If there is a past incident, start one sentence with "I have seen this before:" and name
who fixed it, the runbook, and how many times that runbook has worked."""


# --------------------------------------------------------------------------
# Gemini + MCP
# --------------------------------------------------------------------------

def _clean_schema(schema):
    """Remove JSON-schema 'title' labels that Gemini does not need.

    Careful: inside "properties", the keys are argument NAMES, not schema
    labels. save_incident has a real argument called "title", so we must
    keep it there. (Removing it caused Gemini's 400 INVALID_ARGUMENT:
    "requires unspecified property 'title'".)
    """
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "title":
            continue  # schema label, safe to drop
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _clean_schema(sub) for name, sub in v.items()}  # keep every arg name
        else:
            out[k] = _clean_schema(v)
    return out


def _parse_tool_result(res):
    """Turn an MCP CallToolResult into a plain dict."""
    if getattr(res, "structured_content", None):
        data = res.structured_content
        # MCP wraps non-object returns as {"result": ...}
        return data
    text = "".join(getattr(c, "text", "") for c in (res.content or []))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"result": text}


class GeminiFailure(Exception):
    """Gemini could not finish. `kind` says why, for the UI and for retry decisions."""

    def __init__(self, kind, message, attempts=0):
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


def classify_error(e):
    """Sort a Gemini/network error into one kind:
    transient (retry), quota, auth, model, bad_request, other."""
    code = getattr(e, "code", None)
    status = str(getattr(e, "status", "") or "")
    text = f"{status} {e}".lower()
    if code in (500, 502, 503, 504) or status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
        return "transient"
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return "quota"
    if code in (401, 403) or "api_key_invalid" in text or "api key not valid" in text \
            or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return "auth"
    if code == 404 or ("model" in text and "not found" in text):
        return "model"
    if code == 400:
        return "bad_request"
    # No HTTP code: network problems (timeouts, connection reset) are worth a retry.
    if code is None and isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    if code is None and type(e).__name__ in ("ConnectError", "ReadTimeout", "ConnectTimeout",
                                              "RemoteProtocolError", "ReadError"):
        return "transient"
    return "other"


def _short(e):
    return f"{type(e).__name__}: {str(e)[:250]}"


async def generate_with_retry(call, sleep=asyncio.sleep, attempts=None, base=None):
    """Run one model call. Retry ONLY transient errors (503 etc.), at most
    `attempts` times, with exponential backoff + jitter (1s, 2s, ... + up to 0.5s).
    Returns (response, retries_used). Raises GeminiFailure."""
    attempts = attempts or MAX_ATTEMPTS
    base = BACKOFF_SECONDS if base is None else base
    for attempt in range(1, attempts + 1):
        try:
            return await call(), attempt - 1
        except Exception as e:
            kind = classify_error(e)
            if kind != "transient" or attempt == attempts:
                raise GeminiFailure(kind, _short(e), attempt) from e
            delay = base * (2 ** (attempt - 1)) + random.uniform(0, base / 2)
            log.warning("Gemini %s (attempt %d/%d), retrying in %.1fs: %s",
                        kind, attempt, attempts, delay, _short(e))
            await sleep(delay)


def make_client():
    """Separate function so tests can swap in a fake Gemini."""
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def _gemini_loop(alert, declarations, call_tool):
    from google.genai import types

    client = make_client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=declarations)],
        temperature=0,
    )
    prompt = (f"New alert {alert['id']} ({alert['severity']}) fired on service "
              f"'{alert['service']}': {alert['message']}. Investigate it. alert_id = {alert['id']}")
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    steps, answer, retries = [], None, 0

    for _ in range(MAX_TURNS):
        try:
            resp, used = await generate_with_retry(
                lambda: client.aio.models.generate_content(model=MODEL, contents=contents, config=config))
        except GeminiFailure as e:
            e.steps = steps  # keep what Gemini already did, for the record
            raise
        retries += used
        calls = resp.function_calls or []
        if not calls:
            answer = (resp.text or "").strip()
            break
        contents.append(resp.candidates[0].content)  # keep the model's turn (incl. thought signatures)
        reply_parts = []
        for fc in calls:
            args = dict(fc.args or {})
            out = await call_tool(fc.name, args)
            steps.append({"tool": fc.name, "args": args,
                          "cypher": out.get("cypher", ""),
                          "result": out.get("result", out.get("error"))})
            # Gemini only needs the result, not the Cypher text (saves tokens).
            model_view = {k: v for k, v in out.items() if k != "cypher"}
            part = types.Part.from_function_response(name=fc.name, response=model_view)
            if getattr(fc, "id", None):
                part.function_response.id = fc.id
            reply_parts.append(part)
        contents.append(types.Content(role="user", parts=reply_parts))

    if answer is None:
        raise GeminiFailure("other", f"Gemini did not finish within {MAX_TURNS} turns")
    if not any(s["tool"] == "root_cause_path" and s["result"] for s in steps):
        raise GeminiFailure("ungrounded", "Gemini answered without a root_cause_path result")
    return steps, answer, retries


async def _run_with_mcp(alert):
    """Start tools.py as an MCP server (stdio) and let Gemini use its tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(BACKEND / "tools.py")],
                                   cwd=str(BACKEND), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            from google.genai import types
            declarations = [
                types.FunctionDeclaration(name=t.name, description=t.description,
                                          parameters_json_schema=_clean_schema(t.input_schema))
                for t in listed.tools
            ]

            async def call_tool(name, args):
                res = await session.call_tool(name, args)
                return _parse_tool_result(res)

            return await _gemini_loop(alert, declarations, call_tool)


_ID_PATTERN = re.compile(r"\b(?:INC|RB|D|A)-\d+\b")


def ungrounded_ids(answer, steps, alert):
    """IDs in Gemini's answer that never appeared in a tool result (or the alert).
    Anything listed here was invented by the model."""
    seen = json.dumps([s.get("result") for s in steps], default=str) + " " + alert["id"]
    return sorted({i for i in _ID_PATTERN.findall(answer or "") if i not in seen})


def _summarise(alert, steps, answer, retries=0):
    """Build the /investigate response (docs/api.md) from the agent's steps."""
    def last(tool):
        for s in reversed(steps):
            if s["tool"] == tool and isinstance(s["result"], (dict, list)):
                return s["result"]
        return None

    rc = last("root_cause_path")
    past = last("recall_incidents")
    saved = last("save_incident")

    root = rc["root_cause"] if isinstance(rc, dict) else None
    path = rc["path"] if isinstance(rc, dict) else [alert["service"]]

    # Memory must always be written, even if the model forgot to call save_incident.
    # save_incident is idempotent per alert, so this can never create a duplicate.
    incident_id = saved.get("incident_id") if isinstance(saved, dict) else None
    if incident_id is None and root:
        auto = graph.save_incident(alert["id"], root, f"{alert['message']} (root cause: {root})")
        incident_id = auto["incident_id"]
        steps.append({"tool": "save_incident", "args": {"auto": True},
                      "cypher": "CREATE (i:Incident ...)", "result": auto})

    seen_before = seen_before_from(past)
    bad_ids = ungrounded_ids(answer, steps, alert)
    if bad_ids:
        log.warning("Gemini answer mentions IDs not found in any tool result: %s", bad_ids)

    return {"mode": "gemini", "gemini_retries": retries, "steps": steps,
            "root_cause": root, "path": path, "seen_before": seen_before,
            "incident_id": incident_id, "answer": answer, "ungrounded_ids": bad_ids,
            **_current_refs(alert, incident_id)}


def _current_refs(alert, incident_id):
    """The IDs for THIS failure, read back from the graph (source of truth)."""
    inc = graph.get_incident(incident_id) if incident_id else None
    return {"alert_id": alert["id"],
            "deployment_id": inc["deployment_id"] if inc else None,
            "incident_status": inc["status"] if inc else None}


def seen_before_from(past):
    """Memory summary for the UI: the newest past incident plus the full history."""
    if not past:
        return None
    last = past[0]
    return {"incident_id": last["incident"], "title": last["title"],
            "fixed_by": last["fixed_by"], "runbook": last["runbook"],
            "runbook_title": last["runbook_title"], "runbook_wins": last["runbook_wins"],
            "resolved_at": last["resolved_at"], "history": past}


def investigate(alert_id):
    """Main entry point used by the API."""
    alert = graph.get_alert(alert_id)
    if alert is None:
        return {"error": f"Alert {alert_id} not found"}
    if not os.getenv("GEMINI_API_KEY"):
        return _fallback(alert_id, "no_key", "GEMINI_API_KEY is not set")
    try:
        steps, answer, retries = asyncio.run(asyncio.wait_for(_run_with_mcp(alert), TIMEOUT_SECONDS))
    except Exception as e:
        while getattr(e, "exceptions", None):  # unwrap MCP task-group errors
            e = e.exceptions[0]
        if isinstance(e, GeminiFailure):
            kind, msg, attempts = e.kind, str(e), e.attempts
        elif isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            kind, msg, attempts = "timeout", f"Gemini took longer than {TIMEOUT_SECONDS}s", 0
        else:
            kind, msg, attempts = classify_error(e), _short(e), 0
        log.warning("Gemini failed (%s), using the fixed backup: %s", kind, msg)
        return _fallback(alert_id, kind, msg, attempts)
    return _summarise(alert, steps, answer, retries)


FALLBACK_LABELS = {
    "transient": "Gemini is temporarily unavailable (503/5xx), retries used up",
    "quota": "Gemini quota or rate limit reached (429)",
    "auth": "Gemini API key is invalid or not allowed",
    "model": "Gemini model name not found",
    "bad_request": "Gemini rejected the request (400)",
    "ungrounded": "Gemini did not use the graph tools",
    "timeout": "Gemini took too long",
    "no_key": "No Gemini API key configured",
    "other": "Gemini failed",
}


def _fallback(alert_id, kind, reason, attempts=0):
    result = run_fixed_investigation(alert_id)
    result["mode"] = "fixed"
    result["fallback_kind"] = kind
    result["fallback_reason"] = f"{FALLBACK_LABELS.get(kind, kind)}. {reason}"[:300]
    result["gemini_attempts"] = attempts
    return result


# --------------------------------------------------------------------------
# Fixed investigation (no LLM). Our safety net for the demo.
# --------------------------------------------------------------------------

def run_fixed_investigation(alert_id):
    alert = graph.get_alert(alert_id)
    if alert is None:
        return {"error": f"Alert {alert_id} not found"}

    steps = []

    def step(tool, cypher, result):
        steps.append({"tool": tool, "cypher": cypher.strip(), "result": result})
        return result

    # 1. Follow dependencies from the alerting service to a recent change.
    cypher, rc = graph.root_cause_path(alert["service"], until=alert["at"])
    step("root_cause_path", cypher, rc)
    if rc is None:
        return {"steps": steps, "root_cause": None, "path": [alert["service"]],
                "seen_before": None, "incident_id": None,
                "answer": f"No recent change found below {alert['service']}.",
                **_current_refs(alert, None)}
    root = rc["root_cause"]

    # 2. What else is affected?
    cypher, affected = graph.blast_radius(root)
    step("blast_radius", cypher, affected)

    # 3. Memory: have we seen this before?
    cypher, past = graph.recall_incidents(root)
    step("recall_incidents", cypher, past)

    # 4. Who owns it?
    cypher, owner = graph.find_owner(root)
    step("find_owner", cypher, owner)

    # 5. Save this incident to memory (returns the existing one if Gemini already saved it).
    saved = graph.save_incident(alert_id, root, f"{alert['message']} (root cause: {root})")
    incident_id = saved["incident_id"]
    steps.append({"tool": "save_incident", "cypher": "CREATE (i:Incident ...)", "result": saved})

    seen_before = None
    answer = (f"Root cause is {root}. Deploy {rc['deployment']} ({rc['version']}) changed it. "
              f"Path: {' -> '.join(rc['path'])}. "
              f"{len(affected)} services are affected. Owner: {owner['team'] if owner else 'unknown'}.")
    if past:
        last = past[0]
        seen_before = seen_before_from(past)
        answer += (f" I have seen this before: {last['incident']} - {last['title']}. "
                   f"{last['fixed_by']} fixed it with {last['runbook']} "
                   f"({last['runbook_title']}), which has worked {last['runbook_wins']} times.")

    # 6. Telling the owner team on Discord happens in api.py after this returns
    #    (notify.investigation_events), so it is posted once, with the real result.

    return {"steps": steps, "root_cause": root, "path": rc["path"],
            "seen_before": seen_before, "incident_id": incident_id, "answer": answer,
            "ungrounded_ids": [], **_current_refs(alert, incident_id)}


if __name__ == "__main__":
    # Quick test from the terminal:  python agent.py payment-db
    target = sys.argv[1] if len(sys.argv) > 1 else "payment-db"
    broke = graph.break_service(target)
    print(f"Broke {target}: alert {broke['alert_id']} on {broke['alert_on']}\n")
    out = investigate(broke["alert_id"])
    print(f"Mode: {out.get('mode')}  {out.get('fallback_reason', '')}")
    print(f"Incident: {out.get('incident_id')} ({out.get('incident_status')}), "
          f"alert {out.get('alert_id')}, deploy {out.get('deployment_id')}")
    for s in out.get("steps", []):
        print(f"  -> {s['tool']}({s.get('args', '')})")
    print(f"\nPath: {' -> '.join(out.get('path') or [])}")
    print(f"Answer: {out.get('answer')}")
