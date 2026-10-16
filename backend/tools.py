"""RootTrace MCP server: the agent's tools.

Each tool wraps one fixed, tested Cypher query from graph.py.
The agent (Gemini) chooses WHICH tool to call. It never writes Cypher itself.

Every tool returns {"cypher": ..., "result": ...} so the UI can show
exactly which query ran (explainable results).

Run on its own (for any MCP client):  python tools.py
agent.py starts it automatically and talks to it over MCP (stdio).

IMPORTANT: never print() in this file. stdout is the MCP channel.
"""
import json
import os
import urllib.request

from mcp.server.mcpserver import MCPServer

import graph
import notify

server = MCPServer(
    "roottrace",
    instructions="Tools for investigating outages in a microservice dependency graph stored in FalkorDB.",
)


def _known_services():
    return [r[0] for r in graph.read("MATCH (s:Service) RETURN s.name ORDER BY s.name")]


def _check_service(name):
    """Return an error dict if the service does not exist, else None."""
    known = _known_services()
    if name not in known:
        return {"error": f"Unknown service '{name}'. Known services: {', '.join(known)}"}
    return None


# ----------------------------------------------------------------- read tools

@server.tool()
def root_cause_path(service: str, minutes: int = 60) -> dict:
    """Start at the service that alerted and follow DEPENDS_ON downwards.
    Returns the deepest dependency that had a deploy in the last `minutes`,
    the full path from the alerting service to it, and the deploy details.
    Use this FIRST to find the root cause of an alert."""
    if err := _check_service(service):
        return err
    cypher, result = graph.root_cause_path(service, minutes)
    return {"cypher": cypher.strip(), "result": result}


@server.tool()
def blast_radius(service: str) -> dict:
    """List every service that breaks if `service` goes down
    (follows DEPENDS_ON backwards up to 5 hops). Use it to judge impact."""
    if err := _check_service(service):
        return err
    cypher, result = graph.blast_radius(service)
    return {"cypher": cypher.strip(), "result": result}


@server.tool()
def recall_incidents(service: str) -> dict:
    """Agent memory. Past resolved incidents whose root cause was `service`,
    newest first, with the runbook that fixed each one, how many times that
    runbook has worked, and who fixed it. Use it to answer 'have we seen this before?'."""
    if err := _check_service(service):
        return err
    cypher, result = graph.recall_incidents(service)
    return {"cypher": cypher.strip(), "result": result}


@server.tool()
def find_owner(service: str) -> dict:
    """The team that owns `service`, its Discord channel, and its members."""
    if err := _check_service(service):
        return err
    cypher, result = graph.find_owner(service)
    return {"cypher": cypher.strip(), "result": result}


@server.tool()
def critical_services(limit: int = 5) -> dict:
    """Rank services by PageRank over DEPENDS_ON. A high score means many
    services depend on it, directly or indirectly."""
    cypher, result = graph.critical_services(limit)
    return {"cypher": cypher.strip(), "result": result}


# ----------------------------------------------------------------- action tools

@server.tool()
def save_incident(alert_id: str, root_cause: str, title: str) -> dict:
    """Write a new open Incident to the graph, linked to the alert and the
    root-cause service, so future investigations can remember it.
    Call this once, after you know the root cause."""
    if err := _check_service(root_cause):
        return err
    incident_id = graph.save_incident(alert_id, root_cause, title)
    return {"cypher": "CREATE (i:Incident)-[:TRIGGERED_BY]->(alert), (i)-[:ROOT_CAUSE]->(service)",
            "result": {"incident_id": incident_id}}


@server.tool()
def notify_team(service: str, message: str) -> dict:
    """Send `message` to the Discord channel of the team that owns `service`.
    Keep the message short: root cause, impact, and the suggested fix."""
    if err := _check_service(service):
        return err
    cypher, _ = graph.find_owner(service)
    return {"cypher": cypher.strip(), "result": notify.send(service, message)}


if __name__ == "__main__":
    server.run("stdio")
