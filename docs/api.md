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

Extra fields (added Friday, lifecycle fix):

| Field | Meaning |
|---|---|
| `alert_id`, `deployment_id` | The IDs for THIS failure, read back from the graph. |
| `incident_status` | `"open"` or `"resolved"`, read back from the graph. |
| `fallback_kind` | Only in `"fixed"` mode: `transient` (503), `quota` (429), `auth`, `model`, `bad_request`, `ungrounded`, `timeout`, `no_key`, `other`. |
| `gemini_retries` | Only in `"gemini"` mode: how many 503-type retries were needed. |
| `gemini_attempts` | Only in `"fixed"` mode: Gemini calls tried before giving up. |
| `ungrounded_ids` | IDs in Gemini's answer that no tool returned (should be `[]`). |

Rules: one alert has exactly one incident (calling `/investigate` again returns the same one).
A new `/break` always creates a new deployment, alert and (after investigation) incident.

Possible `steps[].tool` values: `root_cause_path`, `blast_radius`, `recall_incidents`,
`find_owner`, `critical_services`, `save_incident`, `notify_team`.

## POST /resolve
Saves the fix as memory.
Input:
```json
{"incident_id": "INC-009", "runbook_id": "RB-3", "resolved_by": "Priya"}
```
Output (the incident as stored in FalkorDB):
```json
{"ok": true, "incident_id": "INC-009", "status": "resolved", "alert_id": "A-009",
 "deployment_id": "D-021", "root_cause": "payment-db", "started_at": 1792130070,
 "resolved_at": 1792130100, "runbook_id": "RB-3", "resolved_by": "Priya",
 "runbook": "RB-3", "runbook_wins": 3, "runbook_title": "...", "already_resolved": false}
```
Calling it again with the same input is safe: `already_resolved: true`, nothing is counted twice.
Errors: 404 = incident/runbook/person not found, 409 = already resolved with a different runbook or person.

## GET /incidents/{incident_id}
The stored incident (same fields as above, without the runbook extras).

## POST /reset
Reloads clean demo data.
Deletes the whole graph (including incidents made during the demo) and reloads `data/`.
Output:
```json
{"ok": true, "counts": {"Service": 15, "Incident": 8, "...": 0}, "graph": {"nodes": [], "edges": []}}
```
