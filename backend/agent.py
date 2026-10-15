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
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import graph

load_dotenv()

BACKEND = Path(__file__).resolve().parent
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_TURNS = 10           # safety limit on model calls per investigation
TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = """You are RootTrace, an AI on-call engineer for an online shop made of microservices.
The service dependency graph, deploys, owners and past incidents live in a graph database.
You can only learn facts by calling tools. Never guess service names, people or fixes.

How to investigate an alert:
1. Call root_cause_path on the service that alerted. The last service in the path is the root cause.
2. Call recall_incidents on the root cause to check if we have seen this before.
3. Call blast_radius on the root cause to measure impact.
4. Call find_owner on the root cause.
5. Call save_incident once, with the alert id, the root cause and a short title.
6. Call notify_team on the root cause with a short message: root cause, the bad deploy,
   impact, and the runbook that worked before (if any).
If a tool returns an error or no result, say so honestly and continue with what you know.

Final answer: 3 to 5 short sentences in plain English. Include the root cause, the path,
the deploy that changed it, how many services are affected, and the owner team.
If there is a past incident, start one sentence with "I have seen this before:" and name
who fixed it, the runbook, and how many times that runbook has worked."""


# --------------------------------------------------------------------------
# Gemini + MCP
# --------------------------------------------------------------------------

def _clean_schema(schema):
    """Remove JSON-schema 'title' fields that Gemini does not need."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


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


async def _gemini_loop(alert, declarations, call_tool):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=declarations)],
        temperature=0,
    )
    prompt = (f"New alert {alert['id']} ({alert['severity']}) fired on service "
              f"'{alert['service']}': {alert['message']}. Investigate it.")
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    steps, answer = [], None

    for _ in range(MAX_TURNS):
        resp = await client.aio.models.generate_content(model=MODEL, contents=contents, config=config)
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
        raise RuntimeError(f"Gemini did not finish within {MAX_TURNS} turns")
    return steps, answer


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


def _summarise(alert, steps, answer):
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
    incident_id = saved.get("incident_id") if isinstance(saved, dict) else None
    if incident_id is None and root:
        incident_id = graph.save_incident(alert["id"], root, f"{alert['message']} (root cause: {root})")
        steps.append({"tool": "save_incident", "args": {"auto": True}, "cypher": "CREATE (i:Incident ...)",
                      "result": {"incident_id": incident_id}})

    seen_before = None
    if past:
        seen_before = {"incident_id": past[0]["incident"], "fixed_by": past[0]["fixed_by"],
                       "runbook": past[0]["runbook"]}

    return {"mode": "gemini", "steps": steps, "root_cause": root, "path": path,
            "seen_before": seen_before, "incident_id": incident_id, "answer": answer}


def investigate(alert_id):
    """Main entry point used by the API."""
    alert = graph.get_alert(alert_id)
    if alert is None:
        return {"error": f"Alert {alert_id} not found"}
    if not os.getenv("GEMINI_API_KEY"):
        return _fallback(alert_id, "GEMINI_API_KEY is not set")
    try:
        steps, answer = asyncio.run(asyncio.wait_for(_run_with_mcp(alert), TIMEOUT_SECONDS))
    except Exception as e:
        while getattr(e, "exceptions", None):  # unwrap MCP task-group errors
            e = e.exceptions[0]
        return _fallback(alert_id, f"{type(e).__name__}: {e}")
    return _summarise(alert, steps, answer)


def _fallback(alert_id, reason):
    result = run_fixed_investigation(alert_id)
    result["mode"] = "fixed"
    result["fallback_reason"] = reason[:300]
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
    cypher, rc = graph.root_cause_path(alert["service"])
    step("root_cause_path", cypher, rc)
    if rc is None:
        return {"steps": steps, "root_cause": None, "path": [alert["service"]],
                "seen_before": None, "incident_id": None,
                "answer": f"No recent change found below {alert['service']}."}
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

    # 5. Save this incident to memory.
    incident_id = graph.save_incident(alert_id, root, f"{alert['message']} (root cause: {root})")
    steps.append({"tool": "save_incident", "cypher": "CREATE (i:Incident ...)",
                  "result": incident_id})

    seen_before = None
    answer = (f"Root cause is {root}. Deploy {rc['deployment']} ({rc['version']}) changed it. "
              f"Path: {' -> '.join(rc['path'])}. "
              f"{len(affected)} services are affected. Owner: {owner['team'] if owner else 'unknown'}.")
    if past:
        last = past[0]
        seen_before = {"incident_id": last["incident"], "fixed_by": last["fixed_by"],
                       "runbook": last["runbook"]}
        answer += (f" I have seen this before: {last['incident']} - {last['title']}. "
                   f"{last['fixed_by']} fixed it with {last['runbook']} "
                   f"({last['runbook_title']}), which has worked {last['runbook_wins']} times.")

    return {"steps": steps, "root_cause": root, "path": rc["path"],
            "seen_before": seen_before, "incident_id": incident_id, "answer": answer}


if __name__ == "__main__":
    # Quick test from the terminal:  python agent.py payment-db
    target = sys.argv[1] if len(sys.argv) > 1 else "payment-db"
    broke = graph.break_service(target)
    print(f"Broke {target}: alert {broke['alert_id']} on {broke['alert_on']}\n")
    out = investigate(broke["alert_id"])
    print(f"Mode: {out.get('mode')}  {out.get('fallback_reason', '')}")
    for s in out.get("steps", []):
        print(f"  -> {s['tool']}({s.get('args', '')})")
    print(f"\nPath: {' -> '.join(out.get('path') or [])}")
    print(f"Answer: {out.get('answer')}")
