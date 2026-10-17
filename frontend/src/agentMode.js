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
  if (result.deferred) return "posted with the summary";
  if (result.sent) return "sent";
  if (result.simulated) return "simulated – not sent";
  return "failed – not sent";
}

// One Discord notification record from the backend -> what the UI shows.
// "Sent" is shown ONLY when the backend says Discord confirmed it.
const EVENT_NAMES = {
  incident_created: "Incident created",
  investigation_completed: "Investigation summary",
  incident_resolved: "Incident resolved",
  incident_resolve_failed: "Fix failed",
  breakages_cleared: "Rollback summary",
  clear_nothing: "Nothing to clear",
  clear_failed: "Clear failed",
  demo_reset: "Demo reset",
  demo_reset_failed: "Reset failed",
};

export function notificationLabel(rec) {
  if (!rec) return null;
  const what = EVENT_NAMES[rec.event] ?? rec.event;
  const where = rec.channel ?? "Discord";
  const via = rec.routed_via === "default" ? " (via the default webhook channel)" : "";
  switch (rec.status) {
    case "sent":
      return { cls: "ok", text: `📣 ${what} → ${where}: delivered ✓${via}` };
    case "pending":
      return { cls: "off", text: `⏳ ${what} → ${where}: sending, not confirmed yet` };
    case "duplicate":
      return { cls: "off", text: `${what} → ${where}: already posted, not sent again` };
    case "not_configured":
      return { cls: "off", text: `${what} → ${where}: NOT sent – no Discord webhook configured` };
    default:
      return { cls: "bad", text: `${what} → ${where}: NOT sent – ${rec.reason ?? "unknown error"}` };
  }
}
