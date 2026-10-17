"""Check your Discord setup: send ONE test message to every configured webhook
and print what Discord really answered. Changes nothing in the graph.

Run from the backend folder:   python discord_check.py
"""
import os

from dotenv import load_dotenv

load_dotenv()

import notify  # noqa: E402  (after load_dotenv, so it sees the webhooks)

TEAMS = ["Payments", "Platform", "Commerce", "Growth", "Web"]


def main():
    targets = {}
    if os.getenv("DISCORD_WEBHOOK_URL", "").strip():
        targets["default (DISCORD_WEBHOOK_URL)"] = os.environ["DISCORD_WEBHOOK_URL"].strip()
    for team in TEAMS:
        url = os.getenv(notify.env_name(team), "").strip()
        if url:
            targets[f"{team} ({notify.env_name(team)})"] = url
    if not targets:
        print("No Discord webhook is configured. Set DISCORD_WEBHOOK_URL in .env (see .env.example).")
        return 1

    ok = True
    for name, url in targets.items():
        text = (f"🧪 **ROOTTRACE TEST MESSAGE**\n- Webhook: {name}\n"
                f"- If you can read this, RootTrace notifications for this channel work.")
        r = notify.deliver(url, text)
        line = f"{name:<40} {r['status'].upper()}"
        if r["status"] == "sent":
            line += f"  (Discord message id {r.get('message_id')})"
        else:
            ok = False
            line += f"  – {r.get('reason')}"
        print(line)
    for team in TEAMS:
        if not os.getenv(notify.env_name(team), "").strip():
            print(f"{team:<40} uses the default webhook (no {notify.env_name(team)})")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
