"""The investigation.

TODAY: a fixed list of steps (no LLM). This already works end to end and is
our safety net for the demo.
FRIDAY (Person A): Gemini chooses which tools to call and writes the answer.
The tools stay the same functions from graph.py.
"""
import graph


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
