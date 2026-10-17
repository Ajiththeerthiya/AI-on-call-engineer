"""RootTrace Discord notifications: ONE service used by every action.

Who calls it: api.py, AFTER the database change has succeeded
(break+investigate, mark as fixed, clear breakages, reset demo, startup seed).
The agent's notify_team tool does not post by itself any more; its message is
attached to the investigation summary, so each event is posted exactly once.

Routing (central, configurable, in backend/.env only):
    DISCORD_WEBHOOK_<TEAM>   e.g. DISCORD_WEBHOOK_PAYMENTS, DISCORD_WEBHOOK_PLATFORM
    DISCORD_WEBHOOK_URL      default webhook, used when a team has no own webhook
                             and for events that belong to no team (reset, ...)
A Discord webhook always posts into ONE channel. So a message reaches
#team-payments only if DISCORD_WEBHOOK_PAYMENTS points at a webhook made in
that channel. Otherwise it goes to the default webhook's channel, and the
result says routed_via = "default".

Honest delivery:
- We POST with ?wait=true. Discord then answers with the created message.
  status = "sent" ONLY when Discord confirms (2xx), and message_id proves it.
- "not_configured": no webhook for this team and no default. Nothing was sent.
- "failed": Discord refused or the network failed (reason given, URL hidden).
- "pending": Discord had not answered within DISCORD_WAIT_SECONDS. The send
  continues in the background; GET /notifications shows the final status.
- "duplicate": this event was already posted; not sent again.

Never raises. Never logs or returns a webhook URL (anyone holding it can post).
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures

import graph

log = logging.getLogger("roottrace.notify")

DEFAULT_CHANNEL = "#on-call"
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="discord")
_lock = threading.Lock()
_recent = deque(maxlen=100)   # newest first, for GET /notifications
_seen = {}                    # short-lived dedup keys -> time sent
_WEBHOOK_RE = re.compile(r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+", re.I)

SIM_NOTE = ("Simulation only: RootTrace changed `rolled_back` in FalkorDB. "
            "No real deployment system is connected, so no real infrastructure was rolled back.")


def _seconds(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# ------------------------------------------------------------------ routing

def env_name(team):
    """'Payments' -> 'DISCORD_WEBHOOK_PAYMENTS'."""
    return "DISCORD_WEBHOOK_" + re.sub(r"[^A-Z0-9]+", "_", str(team).upper()).strip("_")


def route(team):
    """(webhook_url, routed_via). routed_via is 'team', 'default' or 'none'.
    The URL is only used to send; it never leaves this module."""
    if team:
        url = os.getenv(env_name(team), "").strip()
        if url:
            return url, "team"
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if url:
        return url, "default"
    return "", "none"


def _owner(service):
    """{'team', 'channel'} for a service. Never raises (the DB may be down)."""
    try:
        _, owner = graph.find_owner(service)
        if owner:
            return {"team": owner["team"], "channel": owner["channel"]}
    except Exception as e:
        log.warning("owner lookup for %s failed: %s", service, type(e).__name__)
    return {"team": None, "channel": DEFAULT_CHANNEL}


# ------------------------------------------------------------------ formatting

def _ts(epoch):
    """Discord shows <t:...> in every reader's own time zone."""
    return f"<t:{int(epoch)}:f>" if epoch else "unknown"


def _val(v):
    if v is None or v == "" or v == []:
        return "not available"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def format_message(ev):
    team = ev.get("team")
    lines = [f"{ev['icon']} **{ev['title']}**",
             f"_To: {ev.get('channel') or DEFAULT_CHANNEL}{f' ({team})' if team else ''}_"]
    for label, value in ev.get("fields", []):
        lines.append(f"- **{label}:** {_val(value)}")
    for extra in ev.get("notes", []):
        lines.append(extra)
    text = "\n".join(lines)
    return text if len(text) <= 1990 else text[:1980] + "\n…"


def scrub(text):
    """Remove webhook URLs from any text (error messages can contain them)."""
    text = _WEBHOOK_RE.sub("[webhook hidden]", str(text))
    for key, url in os.environ.items():
        if key.startswith("DISCORD_WEBHOOK") and url and url in text:
            text = text.replace(url, "[webhook hidden]")
    return text


# ------------------------------------------------------------------ sending

def _post(url, text, timeout):
    """One HTTP call. Returns (status, body). Raises urllib errors."""
    sep = "&" if "?" in url else "?"
    payload = {"content": text, "allowed_mentions": {"parse": []}}  # never ping @everyone
    req = urllib.request.Request(
        url + sep + "wait=true", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "RootTrace"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def deliver(url, text, timeout=None):
    """Send and report what Discord actually said. Retries once on 429/5xx."""
    timeout = timeout or _seconds("DISCORD_TIMEOUT_SECONDS", 8)
    for attempt in (1, 2):
        try:
            status, body = _post(url, text, timeout)
        except urllib.error.HTTPError as e:
            if attempt == 1 and (e.code == 429 or e.code >= 500):
                retry_after = 1.0
                try:
                    retry_after = float(json.loads(e.read() or b"{}").get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(min(max(retry_after, 0.2), 3.0))
                continue
            return {"status": "failed", "http_status": e.code,
                    "reason": f"Discord answered HTTP {e.code}"}
        except Exception as e:  # timeout, DNS, connection refused, bad URL
            return {"status": "failed", "reason": scrub(f"{type(e).__name__}: {str(e)[:150]}")}
        if 200 <= status < 300:
            message_id = None
            try:
                message_id = json.loads(body or b"{}").get("id")
            except Exception:
                pass
            return {"status": "sent", "http_status": status, "message_id": message_id}
        return {"status": "failed", "http_status": status, "reason": f"Discord answered HTTP {status}"}
    return {"status": "failed", "reason": "Discord kept refusing (rate limit or server error)"}


def _public(rec):
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["sent"] = rec["status"] == "sent"              # old field, kept for the UI
    out["simulated"] = rec["status"] == "not_configured"
    return out


def _finish(rec, url, text):
    result = deliver(url, text)
    with _lock:
        rec.update(result)
    if result["status"] == "sent":
        log.info("discord %s -> %s: sent (message %s)", rec["event"], rec["channel"], result.get("message_id"))
    else:
        log.warning("discord %s -> %s: %s (%s)", rec["event"], rec["channel"], result["status"],
                    result.get("reason"))


def send_events(events, wait=None):
    """Post a list of events. Waits up to DISCORD_WAIT_SECONDS for Discord, then
    returns one record per event (see module docstring for the statuses)."""
    wait = _seconds("DISCORD_WAIT_SECONDS", 3) if wait is None else wait
    records, jobs = [], []
    now = time.time()
    for ev in events:
        url, via = route(ev.get("team"))
        rec = {"event": ev["event"], "team": ev.get("team"),
               "channel": ev.get("channel") or DEFAULT_CHANNEL, "routed_via": via,
               "status": "pending", "at": int(now), "preview": format_message(ev)}
        ttl = ev.get("dedup_seconds")
        with _lock:
            if ttl and now - _seen.get(ev.get("key"), 0) < ttl:
                rec.update(status="duplicate", reason="Same message was posted moments ago")
                records.append(rec)
                continue
            if ttl:
                _seen[ev["key"]] = now
        if not url:
            rec.update(status="not_configured",
                       reason=f"No webhook set ({env_name(ev['team']) + ' or ' if ev.get('team') else ''}"
                              f"DISCORD_WEBHOOK_URL). Message NOT sent.")
        else:
            jobs.append((rec, url, rec["preview"]))
        with _lock:
            _recent.appendleft(rec)
        records.append(rec)
    if jobs:
        # One background job per action, posting its messages IN ORDER
        # (e.g. "incident created" always before "investigation completed").
        try:
            future = _pool.submit(lambda: [_finish(*job) for job in jobs])
            wait_futures([future], timeout=wait)
        except Exception as e:  # pool shut down etc.
            with _lock:
                for rec, _, _ in jobs:
                    if rec["status"] == "pending":
                        rec.update(status="failed", reason=type(e).__name__)
    with _lock:
        return [_public(r) for r in records]


def safe(build, *args, **kwargs):
    """Build events and send them. Any error here is logged and reported, but
    never undoes or hides the database change that already succeeded."""
    try:
        return send_events(build(*args, **kwargs))
    except Exception as e:
        log.exception("notification step failed")
        return [{"event": getattr(build, "__name__", "notification"), "status": "failed", "sent": False,
                 "simulated": False, "reason": scrub(f"Could not build/send notification: {type(e).__name__}")}]


def recent(limit=20):
    with _lock:
        return [_public(r) for r in list(_recent)[:limit]]


def forget_dedup():
    with _lock:
        _seen.clear()


# ------------------------------------------------------------------ event builders
# Each returns a list of events. Values come from the graph or the agent's tool
# results; missing values are shown as "not available", never invented.

def _claim(incident_id, kind):
    """Persistent once-only guard stored on the Incident (survives restarts)."""
    return graph.claim_notification(incident_id, kind)


def investigation_events(result):
    """'Break it' -> after the investigation: INCIDENT CREATED (once per incident)
    + INVESTIGATION COMPLETED (once per incident)."""
    alert = graph.get_alert(result.get("alert_id")) if result.get("alert_id") else None
    root = result.get("root_cause")
    inc_id = result.get("incident_id")

    if not root or not inc_id:  # nothing found: tell the team of the alerting service
        service = alert["service"] if alert else None
        o = _owner(service) if service else {"team": None, "channel": DEFAULT_CHANNEL}
        return [{"event": "investigation_completed", "icon": "🔎", **o,
                 "title": "ROOTTRACE INVESTIGATION COMPLETED – NO ROOT CAUSE FOUND",
                 "key": f"investigation:alert:{result.get('alert_id')}", "dedup_seconds": 3600,
                 "fields": [("Alert", result.get("alert_id")), ("Alerting service", service),
                            ("Finding", result.get("answer"))]}]

    inc = graph.get_incident(inc_id) or {}
    o = _owner(root)
    _, affected = graph.blast_radius(root)
    affected = [a["service"] for a in affected]
    rc = next((s["result"] for s in reversed(result.get("steps", []))
               if s.get("tool") == "root_cause_path" and isinstance(s.get("result"), dict)), {}) or {}
    version = rc.get("version")
    seen = result.get("seen_before") or {}
    runbook = (f"{seen['runbook']} – {seen.get('runbook_title') or ''} (worked {seen.get('runbook_wins')} times)"
               if seen.get("runbook") else "none on record")
    deployment = f"{result.get('deployment_id')}{f' ({version})' if version else ''}" \
        if result.get("deployment_id") else None
    events = []

    if _claim(inc_id, "created"):
        events.append({
            "event": "incident_created", "icon": "🚨", **o, "title": "ROOTTRACE INCIDENT CREATED",
            "fields": [("Incident", inc_id), ("Alert", f"{result.get('alert_id')} – {alert['message']}"
                                                if alert else result.get("alert_id")),
                       ("Deployment", deployment), ("Root cause", root),
                       ("Path", " → ".join(result.get("path") or [])),
                       ("Affected services", affected), ("Team", o["team"]),
                       ("Suggested runbook", runbook), ("Status", (inc.get("status") or "open").capitalize()),
                       ("Opened", _ts(inc.get("started_at")))]})

    if _claim(inc_id, "investigated"):
        mode = "Gemini agent" if result.get("mode") == "gemini" else \
            f"Fixed backup (Gemini: {result.get('fallback_kind', 'not used')})"
        agent_note = next((s.get("args", {}).get("message") for s in result.get("steps", [])
                           if s.get("tool") == "notify_team" and s.get("args")), None)
        recs = [f"Roll back {result.get('deployment_id') or 'the bad deploy'} on {root}."]
        if seen.get("runbook"):
            recs.append(f"Apply {seen['runbook']} ({seen.get('runbook_title') or 'runbook'}) – "
                        f"{seen.get('fixed_by')} fixed {seen.get('incident_id')} with it.")
        notes = ["**Recommended actions (NOT done yet – a person must do them):**"] + [f"  • {r}" for r in recs]
        if agent_note:
            notes.append(f"**Agent's note:** {agent_note[:400]}")
        if result.get("ungrounded_ids"):
            notes.append(f"⚠️ Check the answer: {', '.join(result['ungrounded_ids'])} did not come from the graph.")
        events.append({
            "event": "investigation_completed", "icon": "🔎", **o,
            "title": "ROOTTRACE INVESTIGATION COMPLETED",
            "fields": [("Incident", inc_id), ("Investigated by", mode), ("Root cause", root),
                       ("Path", " → ".join(result.get("path") or [])), ("Deployment", deployment),
                       ("Affected services", affected),
                       ("Seen before", f"{seen['incident_id']} – fixed by {seen.get('fixed_by')} with "
                                       f"{seen.get('runbook')}" if seen.get("incident_id") else "no")],
            "notes": notes})
    return events


def resolved_events(saved):
    """'Mark as fixed' succeeded (only called when it was not already resolved)."""
    if saved.get("already_resolved") or not _claim(saved["incident_id"], "resolved"):
        return []
    root = saved.get("root_cause")
    o = _owner(root) if root else {"team": None, "channel": DEFAULT_CHANNEL}
    affected = [a["service"] for a in graph.blast_radius(root)[1]] if root else []
    return [{
        "event": "incident_resolved", "icon": "✅", **o, "title": "ROOTTRACE INCIDENT RESOLVED",
        "fields": [("Incident", saved["incident_id"]), ("Root cause", root),
                   ("Resolved by", saved.get("resolved_by")),
                   ("Runbook", f"{saved.get('runbook_id')} – {saved.get('runbook_title') or ''}"),
                   ("Successful fixes with this runbook", saved.get("runbook_wins")),
                   ("Affected services", affected), ("Resolved", _ts(saved.get("resolved_at"))),
                   ("Bad deploy marked rolled back", saved.get("deployment_id")),
                   ("Status", "Resolved"), ("Memory", "Saved to FalkorDB")],
        "notes": [f"_{SIM_NOTE}_"] if saved.get("deployment_id") else []}]


def resolve_failed_events(incident_id, reason):
    return [{"event": "incident_resolve_failed", "icon": "❌", "team": None, "channel": DEFAULT_CHANNEL,
             "title": "ROOTTRACE: MARK AS FIXED FAILED",
             "fields": [("Incident", incident_id), ("Reason", reason), ("Time", _ts(time.time())),
                        ("Result", "Nothing was saved. The incident is unchanged.")]}]


def clear_events(cleared):
    """'Clear breakages' succeeded. One message per Discord channel: teams that
    share a webhook get ONE combined message instead of duplicates."""
    deps, incs = cleared.get("deployments", []), cleared.get("incidents_changed", [])
    now = cleared.get("at") or time.time()
    if not deps and not incs:
        return [{"event": "clear_nothing", "icon": "ℹ️", "team": None, "channel": DEFAULT_CHANNEL,
                 "title": "ROOTTRACE CLEAR BREAKAGES – NOTHING TO CLEAR",
                 "key": "clear_nothing", "dedup_seconds": 60,
                 "fields": [("Result", "No live bad deploys and no open incidents"), ("Time", _ts(now))]}]

    # 1. what changed, per team
    by_team = {}
    for kind, items in (("deps", deps), ("incs", incs)):
        for item in items:
            part = by_team.setdefault(item.get("team"), {"channel": item.get("channel"), "deps": [], "incs": []})
            part[kind].append(item)

    # 2. teams that end up in the same Discord channel are combined
    by_hook = {}
    for team in sorted(by_team, key=str):
        url, _ = route(team)
        by_hook.setdefault(url or f"none:{team}", []).append(team)

    events = []
    for teams in by_hook.values():
        combined = len(teams) > 1
        fields = []
        for team in teams:
            part = by_team[team]
            prefix = f"[{team or 'no team'}] " if combined else ""
            fields.append((f"{prefix}Deployments rolled back (simulated)",
                           [f"{d['id']} on {d.get('service')} → incident "
                            f"{', '.join(d.get('incidents') or []) or 'none'}" for d in part["deps"]] or "none"))
            fields.append((f"{prefix}Incidents cancelled",
                           [f"{i['id']} ({i.get('root_cause')}, now {i['status']})" for i in part["incs"]]
                           or "none"))
        fields += [("Teams" if combined else "Team", [t or "no team" for t in teams]),
                   ("Time", _ts(now)), ("Result", "Confirmed in FalkorDB"),
                   ("Memory", "Resolved incidents and runbook counts kept")]
        events.append({"event": "breakages_cleared", "icon": "↩️",
                       "team": teams[0],  # same webhook for all teams in this group
                       "channel": ", ".join(by_team[t]["channel"] or DEFAULT_CHANNEL for t in teams),
                       "title": "ROOTTRACE SIMULATED ROLLBACK COMPLETED", "fields": fields,
                       "notes": [f"_{SIM_NOTE}_"]})
    return events


def clear_failed_events(reason, deployment_ids):
    return [{"event": "clear_failed", "icon": "❌", "team": None, "channel": DEFAULT_CHANNEL,
             "title": "ROOTTRACE CLEAR BREAKAGES FAILED",
             "fields": [("Deployments it tried to roll back", deployment_ids or "unknown (database unreachable)"),
                        ("Reason", reason), ("Time", _ts(time.time())),
                        ("Result", "Check the graph: some changes may not be saved.")]}]


def reset_events(counts):
    summary = ", ".join(f"{n} {label}" for label, n in sorted(counts.items()))
    return [{"event": "demo_reset", "icon": "🔄", "team": None, "channel": DEFAULT_CHANNEL,
             "title": "ROOTTRACE DEMO RESET",
             "fields": [("Result", "Successful"),
                        ("Scope", f"Demo graph '{graph.GRAPH_NAME}' only – reloaded from data/*.json"),
                        ("Now in the graph", summary),
                        ("Removed", "Incidents, alerts and deploys created during the demo"),
                        ("Time", _ts(time.time())),
                        ("Production infrastructure", "Not affected")]}]


def reset_failed_events(reason):
    return [{"event": "demo_reset_failed", "icon": "❌", "team": None, "channel": DEFAULT_CHANNEL,
             "title": "ROOTTRACE DEMO RESET FAILED",
             "fields": [("Reason", reason), ("Time", _ts(time.time())),
                        ("Result", "The demo graph may be empty or half loaded. Click Reset demo again."),
                        ("Production infrastructure", "Not affected")]}]


def startup_seed_events(counts):
    ev = reset_events(counts)[0]
    ev.update(event="demo_seeded_on_startup", title="ROOTTRACE DEMO GRAPH LOADED ON STARTUP")
    ev["fields"][0] = ("Result", "Graph was empty, demo data loaded")
    return [ev]


# ------------------------------------------------------------------ old helpers (kept)

def send(service, message):
    """Post a free-text message for the team that owns `service` (old API, kept)."""
    o = _owner(service)
    return send_events([{"event": "message", "icon": "📣", **o, "title": "ROOTTRACE",
                         "notes": [message]}])[0]


def send_discord(owner, message, url=None, timeout=10):
    """Old API, kept for compatibility: post `message` with an explicit webhook."""
    owner = owner or {}
    text = f"**[RootTrace] {owner.get('channel')} ({owner.get('team')})**\n{message}"
    base = {"team": owner.get("team"), "channel": owner.get("channel"), "message": text}
    url = url if url is not None else route(owner.get("team"))[0]
    if not url:
        return {**base, "status": "not_configured", "sent": False, "simulated": True,
                "reason": "DISCORD_WEBHOOK_URL not set - message NOT sent (simulated)"}
    r = deliver(url, text, timeout)
    return {**base, **r, "sent": r["status"] == "sent", "simulated": False}
