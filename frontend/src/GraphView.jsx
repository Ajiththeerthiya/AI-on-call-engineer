// Draws the service dependency graph with Cytoscape.
// Arrows point from a service to what it DEPENDS_ON.
// After an investigation, the path is red and the root cause gets a thick ring.
import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";

const STYLE = [
  {
    selector: "node",
    style: {
      label: "data(label)",
      "background-color": "#3b82c4",
      color: "#1f2933",
      "font-size": 11,
      "text-valign": "bottom",
      "text-margin-y": 5,
      width: 28,
      height: 28,
    },
  },
  { selector: 'node[kind = "database"]', style: { shape: "barrel", "background-color": "#7b8794" } },
  { selector: "node[tier = 1]", style: { "border-width": 2, "border-color": "#1f3b57" } },
  {
    selector: "edge",
    style: {
      width: 1.5,
      "line-color": "#c5ccd3",
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "target-arrow-color": "#c5ccd3",
    },
  },
  { selector: ".affected", style: { "background-color": "#f0a35e" } },
  {
    selector: ".onpath",
    style: {
      "background-color": "#d64545",
      "line-color": "#d64545",
      "target-arrow-color": "#d64545",
      width: 4,
      "z-index": 10,
    },
  },
  { selector: "node.onpath", style: { width: 34, height: 34 } },
  { selector: "node.healed", style: { "background-color": "#2f9e55", width: 34, height: 34 } },
  { selector: "edge.healed", style: { "line-color": "#2f9e55", "target-arrow-color": "#2f9e55", width: 4 } },
  { selector: ".root", style: { "border-width": 5, "border-color": "#7a1010", width: 44, height: 44 } },
  { selector: "node.focus", style: { "border-width": 5, "border-color": "#7c3aed", width: 40, height: 40 } },
];

export default function GraphView({ graph, path = [], rootCause, affected = [], healed = false, focus }) {
  const box = useRef(null);
  const cy = useRef(null);

  // Draw the map when the graph data arrives.
  useEffect(() => {
    if (!graph.nodes.length) return;
    cy.current = cytoscape({
      container: box.current,
      elements: [
        ...graph.nodes.map((n) => ({ data: { id: n.id, label: n.id, kind: n.kind, tier: n.tier } })),
        ...graph.edges.map((e) => ({ data: { id: `${e.from}->${e.to}`, source: e.from, target: e.to } })),
      ],
      layout: { name: "breadthfirst", directed: true, roots: ["frontend"], spacingFactor: 1.15, padding: 20 },
      style: STYLE,
      userZoomingEnabled: true,
      wheelSensitivity: 0.2,
    });
    // Keep the whole map visible when the window size changes (phones, split screen).
    const onResize = () => {
      if (!cy.current) return;
      cy.current.resize();
      cy.current.fit(undefined, 20);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      cy.current && cy.current.destroy();
    };
  }, [graph]);

  // Highlight what the agent found.
  useEffect(() => {
    const c = cy.current;
    if (!c) return;
    c.elements().removeClass("onpath root affected healed focus");
    if (focus) c.getElementById(focus).addClass("focus"); // picked in the PageRank panel
    const cls = healed ? "healed" : "onpath"; // green after "Mark as fixed"
    affected.forEach((id) => c.getElementById(id).addClass("affected"));
    path.forEach((id, i) => {
      c.getElementById(id).addClass(cls);
      if (i > 0) c.getElementById(`${path[i - 1]}->${id}`).addClass(cls);
    });
    if (rootCause && !healed) c.getElementById(rootCause).addClass("root");
  }, [path, rootCause, affected, healed, focus, graph]);

  return <div ref={box} className="graph" />;
}
