// Run: npm test   (from the frontend folder, Node 18+)
import { test } from "node:test";
import assert from "node:assert/strict";
import { modeBadge, fallbackNote, groundingWarning, notifyLabel } from "./agentMode.js";

test("Gemini mode shows the Gemini badge and no fallback note", () => {
  const r = { mode: "gemini", gemini_retries: 0 };
  assert.deepEqual(modeBadge(r), { cls: "ai", text: "Gemini agent" });
  assert.equal(fallbackNote(r), null);
});

test("Gemini after retries says so", () => {
  assert.equal(modeBadge({ mode: "gemini", gemini_retries: 2 }).text, "Gemini agent (after 2 retries)");
});

test("Backup mode never claims Gemini and shows the reason", () => {
  const r = { mode: "fixed", fallback_kind: "transient", fallback_reason: "Gemini is temporarily unavailable (503/5xx)" };
  const b = modeBadge(r);
  assert.equal(b.text, "Fixed backup");
  assert.ok(!b.text.includes("Gemini"));
  assert.match(fallbackNote(r), /503/);
});

test("Missing mode is treated as backup, not Gemini", () => {
  assert.equal(modeBadge({}).text, "Fixed backup");
});

test("Invented IDs produce a warning", () => {
  assert.equal(groundingWarning({ ungrounded_ids: [] }), null);
  assert.match(groundingWarning({ ungrounded_ids: ["INC-999"] }), /INC-999/);
});

test("Notification labels are honest", () => {
  assert.equal(notifyLabel({ sent: true }), "sent");
  assert.equal(notifyLabel({ sent: false, simulated: true }), "simulated – not sent");
  assert.equal(notifyLabel({ sent: false, simulated: false }), "failed – not sent");
  assert.equal(notifyLabel([1, 2]), null);
});
