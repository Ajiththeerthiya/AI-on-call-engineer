"""Send messages to Discord through a webhook (DISCORD_WEBHOOK_URL in .env).

Used by the notify_team MCP tool, the fixed backup, and /resolve.
Never raises: if Discord fails, the agent keeps working and reports why.
"""
import json
import os
import urllib.request

import graph


def send(service, message):
    """Post `message` for the team that owns `service`. Returns a result dict."""
    _, owner = graph.find_owner(service)
    team = owner["team"] if owner else "unknown"
    channel = owner["channel"] if owner else "#on-call"
    text = f"**[RootTrace] {channel} ({team})**\n{message}"
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return {"sent": False, "reason": "DISCORD_WEBHOOK_URL not set", "team": team,
                "channel": channel, "message": text}
    req = urllib.request.Request(
        url, data=json.dumps({"content": text[:1900]}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "RootTrace"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
    except Exception as e:
        return {"sent": False, "reason": str(e)[:200], "team": team, "channel": channel}
    return {"sent": ok, "team": team, "channel": channel}
