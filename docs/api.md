# API Contract (frozen)

This is the agreement between the backend (Person A) and the frontend (Person B).
Do not rename fields. Adding a new field is OK. Tell your teammate first.

Base URL (local): `http://localhost:8000`

## GET /graph
The whole map, for the UI to draw.
```json
{
  "nodes": [{"id": "payment-db", "label": "Service", "kind": "database", "team": "Payments"}],
  "edges": [{"from": "payment", "to": "payment-db", "type": "DEPENDS_ON"}]
}
```

## POST /break
Breaks a service for the demo.
Input:
```json
{"service": "payment-db"}
```
Output:
```json
{"alert_id": "A-101", "deployment_id": "D-021"}
```

## POST /investigate
The agent runs and returns its work.
Input:
```json
{"alert_id": "A-101"}
```
Output:
```json
{
  "steps": [
    {"tool": "blast_radius", "cypher": "MATCH ...", "result": ["checkout", "frontend"]}
  ],
  "root_cause": "payment-db",
  "path": ["frontend", "checkout", "payment", "payment-db"],
  "seen_before": {"incident_id": "INC-007", "fixed_by": "Priya", "runbook": "RB-3"},
  "incident_id": "INC-009",
  "answer": "Root cause is payment-db. A deploy 20 minutes ago changed it..."
}
```
`seen_before` is `null` when there is no past incident.

Extra fields (added Friday, agent version):

| Field | Meaning |
|---|---|
| `mode` | `"gemini"` = Gemini chose the tools through MCP. `"fixed"` = backup ran the queries in a fixed order. |
| `fallback_reason` | Only when `mode` is `"fixed"`. Why the backup ran (no key, rate limit, network error). |
| `steps[].args` | The inputs Gemini chose for that tool, e.g. `{"service": "checkout"}`. |

Possible `steps[].tool` values: `root_cause_path`, `blast_radius`, `recall_incidents`,
`find_owner`, `critical_services`, `save_incident`, `notify_team`.

## POST /resolve
Saves the fix as memory.
Input:
```json
{"incident_id": "INC-009", "runbook_id": "RB-3", "resolved_by": "Priya"}
```
Output:
```json
{"ok": true}
```

## POST /reset
Reloads clean demo data.
Output:
```json
{"ok": true}
```
