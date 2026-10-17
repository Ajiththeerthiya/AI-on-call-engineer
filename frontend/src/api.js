// All calls to the backend live here. Must match docs/api.md.
// Local: http://localhost:8000. For the live site, set VITE_API_URL in frontend/.env
const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function call(path, options) {
  const res = await fetch(BASE + path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed: ${res.status}`);
  return data;
}

const post = (path, body) =>
  call(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

export const getGraph = () => call("/graph");
export const breakService = (service) => post("/break", { service });
export const investigate = (alert_id) => post("/investigate", { alert_id });
export const resolve = (incident_id, runbook_id, resolved_by) =>
  post("/resolve", { incident_id, runbook_id, resolved_by });
export const reset = () => post("/reset");
export const clearBreakages = () => post("/clear");
