// Pure helpers that decide what the UI says about who produced the answer.
// Kept separate from App.jsx so they can be tested with `node --test` (no extra packages).

// Badge text + css class. Never says "Gemini" unless the backend said mode === "gemini".
export function modeBadge(result) {
  if (!result) return null;
  if (result.mode === "gemini") {
    const r = result.gemini_retries ?? 0;
    return {
      cls: "ai",
      text: r > 0 ? `Gemini agent (after ${r} ${r === 1 ? "retry" : "retries"})` : "Gemini agent",
    };
  }
  return { cls: "fixed", text: "Fixed backup" };
}

// One line explaining why the backup ran, or null when Gemini ran.
export function fallbackNote(result) {
  if (!result || result.mode === "gemini") return null;
  return result.fallback_reason || "The fixed backup produced this answer.";
}

// Warning when Gemini mentioned IDs that no tool returned.
export function groundingWarning(result) {
  const ids = result?.ungrounded_ids ?? [];
  if (!ids.length) return null;
  return `Check this answer: ${ids.join(", ")} did not come from any graph query.`;
}

// Short label for a notify_team step result.
export function notifyLabel(result) {
  if (!result || typeof result !== "object" || !("sent" in result)) return null;
  if (result.sent) return "sent";
  if (result.simulated) return "simulated – not sent";
  return "failed – not sent";
}
