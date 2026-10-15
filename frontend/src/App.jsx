// First test screen: proves the frontend can talk to the backend.
// Person B replaces the lists with a real graph view later.
import { useEffect, useState } from "react";
import { getGraph, breakService, investigate } from "./api.js";

export default function App() {
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [result, setResult] = useState(null);

  useEffect(() => {
    getGraph().then(setGraph);
  }, []);

  async function onBreak() {
    const { alert_id } = await breakService("payment-db");
    setResult(await investigate(alert_id));
  }

  return (
    <main style={{ fontFamily: "sans-serif", padding: 24 }}>
      <h1>RootTrace</h1>
      <h2>Services</h2>
      <ul>
        {graph.edges.map((e) => (
          <li key={e.from + e.to}>
            {e.from} → {e.type} → {e.to}
          </li>
        ))}
      </ul>
      <button onClick={onBreak}>Break payment-db</button>
      {result && (
        <section>
          <h2>Agent answer</h2>
          <p>{result.answer}</p>
          <p>Path: {result.path.join(" → ")}</p>
        </section>
      )}
    </main>
  );
}
