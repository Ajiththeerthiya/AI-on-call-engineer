import { useEffect, useState } from "react";
import { getGraph, breakService, investigate, resolve, reset } from "./api.js";
import GraphView from "./GraphView.jsx";

// Services affected by the outage, taken from the agent's blast_radius step.
function affectedFrom(steps = []) {
  const step = [...steps].reverse().find((s) => s.tool === "blast_radius" && Array.isArray(s.result));
  return step ? step.result.map((r) => r.service) : [];
}

// "5 minutes ago", "3 weeks ago" from epoch seconds.
function timeAgo(epoch) {
  if (!epoch) return "";
  const s = Math.max(0, Date.now() / 1000 - epoch);
  const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [name, size] of units) {
    const n = Math.floor(s / size);
    if (n >= 1) return `${n} ${name}${n > 1 ? "s" : ""} ago`;
  }
  return "just now";
}

// Discord result from the agent's notify_team step (sent, channel, reason).
function discordFrom(steps = []) {
  const s = [...steps].reverse().find((x) => x.tool === "notify_team");
  return s && typeof s.result === "object" ? s.result : null;
}

function DiscordStatus({ info }) {
  if (!info) return null;
  return info.sent ? (
    <p className="discord ok">📣 Posted to Discord: {info.channel} ({info.team})</p>
  ) : (
    <p className="discord off">Discord not sent: {info.reason}</p>
  );
}

const isRecent = (epoch) => epoch && Date.now() / 1000 - epoch < 6 * 3600;

function Step({ step, index }) {
  return (
    <details className="step">
      <summary>
        <span className="step-num">{index + 1}</span>
        <code>{step.tool}</code>
        {step.args && Object.keys(step.args).length > 0 && (
          <span className="step-args">{JSON.stringify(step.args)}</span>
        )}
      </summary>
      {step.cypher && (
        <>
          <div className="label">Cypher</div>
          <pre className="cypher">{step.cypher}</pre>
        </>
      )}
      <div className="label">Result</div>
      <pre>{JSON.stringify(step.result, null, 2)}</pre>
    </details>
  );
}

// The "I have seen this before" card, with the full incident history from the graph.
function Memory({ seen }) {
  const history = seen.history ?? [];
  const learnedNow = isRecent(seen.resolved_at);
  return (
    <div className="card memory">
      <div className="row">
        <h2>🧠 I have seen this before</h2>
        {learnedNow && <span className="badge new">Learned from your last fix</span>}
      </div>
      <p>
        <b>{seen.incident_id}</b> {seen.title ? `– ${seen.title}` : ""}
        <br />
        Fixed by <b>{seen.fixed_by}</b> {timeAgo(seen.resolved_at)} with runbook <b>{seen.runbook}</b>
        {seen.runbook_title ? ` (${seen.runbook_title})` : ""}.
      </p>
      {seen.runbook_wins != null && (
        <p className="wins">
          Runbook {seen.runbook} has worked <b>{seen.runbook_wins} times</b>.
        </p>
      )}
      {history.length > 0 && (
        <>
          <div className="label">Incident history for this service ({history.length})</div>
          <ul className="history">
            {history.map((h, i) => (
              <li key={h.incident} className={i === 0 ? "latest" : ""}>
                <b>{h.incident}</b> · {h.fixed_by} · {h.runbook} · {timeAgo(h.resolved_at)}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export default function App() {
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [target, setTarget] = useState("payment-db");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getGraph().then(setGraph).catch((e) => setError(`Cannot reach the API: ${e.message}`));
  }, []);

  async function run(label, fn) {
    setBusy(label);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    }
    setBusy("");
  }

  const onBreak = () =>
    run(`Breaking ${target}… the agent is investigating (this can take 10–30 seconds)`, async () => {
      setResult(null);
      const { alert_id } = await breakService(target);
      setResult(await investigate(alert_id));
    });

  const onResolve = () =>
    run("Saving the fix to the graph memory…", async () => {
      const runbook = result.seen_before?.runbook ?? "RB-2";
      const who = result.seen_before?.fixed_by ?? "Priya";
      const saved = await resolve(result.incident_id, runbook, who);
      setResult({ ...result, resolved: saved });
    });

  const onReset = () =>
    run("Resetting demo data…", async () => {
      await reset();
      setResult(null);
      setGraph(await getGraph());
    });

  const services = [...graph.nodes].sort((a, b) => a.id.localeCompare(b.id));
  const fixed = result?.resolved;

  return (
    <div className="page">
      <header>
        <div>
          <h1>RootTrace</h1>
          <p className="tagline">AI on-call engineer. Context from a FalkorDB graph.</p>
        </div>
        <div className="controls">
          <select value={target} onChange={(e) => setTarget(e.target.value)} disabled={!!busy}>
            {services.map((n) => (
              <option key={n.id} value={n.id}>{n.id}</option>
            ))}
          </select>
          <button className="danger" onClick={onBreak} disabled={!!busy || !graph.nodes.length}>
            {fixed ? "Break it again" : "Break it"}
          </button>
          <button onClick={onReset} disabled={!!busy}>Reset demo</button>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <main>
        <section className="card graph-card">
          <GraphView
            graph={graph}
            path={result?.path ?? []}
            rootCause={result?.root_cause}
            affected={fixed ? [] : affectedFrom(result?.steps)}
            healed={!!fixed}
          />
          <div className="legend">
            <span><i className="dot svc" /> service</span>
            <span><i className="dot db" /> database</span>
            <span><i className="dot hot" /> root-cause path</span>
            <span><i className="dot warm" /> affected</span>
            <span><i className="dot ok" /> fixed</span>
          </div>
        </section>

        <section className="side">
          {busy && <div className="card busy"><span className="spinner" /> {busy}</div>}

          {!result && !busy && (
            <div className="card muted">
              Pick a service and click <b>Break it</b>. The alert fires on a service upstream.
              The agent follows the graph to find the real cause.
            </div>
          )}

          {fixed && (
            <div className="card fixed">
              <h2>✅ Fixed and saved to memory</h2>
              <p>
                <b>{fixed.incident_id}</b> is now resolved by <b>{fixed.resolved_by}</b> with runbook{" "}
                <b>{fixed.runbook}</b>. That runbook has now worked <b>{fixed.runbook_wins} times</b>.
              </p>
              <DiscordStatus info={fixed.discord} />
              <p className="small">
                The graph now has a new memory: {fixed.incident_id} →FIXED_WITH→ {fixed.runbook},
                →RESOLVED_BY→ {fixed.resolved_by}. Click <b>Break it again</b> on {result.root_cause} and
                the agent will remember this fix.
              </p>
            </div>
          )}

          {result && (
            <>
              <div className={`card ${fixed ? "dim" : ""}`}>
                <div className="row">
                  <h2>Agent answer</h2>
                  <span className={`badge ${result.mode === "gemini" ? "ai" : "fixed"}`}>
                    {result.mode === "gemini" ? "Gemini agent" : "Fixed backup"}
                  </span>
                </div>
                {result.fallback_reason && <p className="small">Backup used: {result.fallback_reason}</p>}
                <p className="answer">{result.answer}</p>
                {result.path?.length > 0 && (
                  <p className="path">{result.path.join("  →  ")}</p>
                )}
                <DiscordStatus info={discordFrom(result.steps)} />
              </div>

              {result.seen_before && !fixed && <Memory seen={result.seen_before} />}

              {result.incident_id && !fixed && (
                <div className="card row">
                  <span>
                    New incident <b>{result.incident_id}</b> is <b>open</b>.
                  </span>
                  <button className="primary" onClick={onResolve} disabled={!!busy}>
                    Mark as fixed{result.seen_before ? ` (${result.seen_before.fixed_by}, ${result.seen_before.runbook})` : ""}
                  </button>
                </div>
              )}

              <div className="card">
                <h2>Agent steps ({result.steps?.length ?? 0})</h2>
                <p className="small">
                  {result.mode === "gemini"
                    ? "Gemini chose each step. Every step runs a fixed Cypher query in FalkorDB."
                    : "The backup ran these fixed Cypher queries in order."}
                </p>
                {result.steps?.map((s, i) => <Step key={i} step={s} index={i} />)}
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
