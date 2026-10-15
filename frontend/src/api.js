// All calls to the backend live here. Must match docs/api.md.
const BASE = "http://localhost:8000";

async function post(path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  return res.json();
}

export const getGraph = () => fetch(BASE + "/graph").then((r) => r.json());
export const breakService = (service) => post("/break", { service });
export const investigate = (alert_id) => post("/investigate", { alert_id });
export const resolve = (incident_id, runbook_id, resolved_by) =>
  post("/resolve", { incident_id, runbook_id, resolved_by });
export const reset = () => post("/reset");
