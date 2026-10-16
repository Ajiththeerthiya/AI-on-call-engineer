"""notify_team never claims delivery without Discord confirming it. No network is used."""
import io
import urllib.error

import notify

OWNER = {"team": "Payments", "channel": "#payments-oncall"}


class FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_no_webhook_is_simulated():
    r = notify.send_discord(OWNER, "hi", url="")
    assert r["sent"] is False and r["simulated"] is True


def test_success_only_when_discord_confirms(monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: FakeResp(204))
    r = notify.send_discord(OWNER, "hi", url="https://discord.example/webhook")
    assert r["sent"] is True and r["simulated"] is False and r["status"] == 204


def test_10_http_error_is_reported(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    r = notify.send_discord(OWNER, "hi", url="https://discord.example/webhook/secret-token")
    assert r["sent"] is False and r["status"] == 404
    assert "secret-token" not in str(r)  # the webhook URL is a secret


def test_network_error_is_reported(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    r = notify.send_discord(OWNER, "hi", url="https://discord.example/webhook")
    assert r["sent"] is False and "URLError" in r["reason"]
