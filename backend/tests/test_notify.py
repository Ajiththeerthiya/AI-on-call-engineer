"""Low-level Discord delivery: only report 'sent' when Discord confirms. No network is used."""
import io
import json
import urllib.error

import notify

URL = "https://discord.com/api/webhooks/123/secret-token"


class FakeResp:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sent_only_when_discord_confirms(monkeypatch):
    seen = {}

    def ok(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return FakeResp(200, b'{"id": "998877"}')
    monkeypatch.setattr(notify.urllib.request, "urlopen", ok)
    r = notify.deliver(URL, "hello")
    assert r == {"status": "sent", "http_status": 200, "message_id": "998877"}
    assert seen["url"].endswith("?wait=true")            # ask Discord to confirm the message
    assert seen["body"]["allowed_mentions"] == {"parse": []}  # never pings @everyone


def test_http_error_is_reported_without_the_secret_url(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    r = notify.deliver(URL, "hi")
    assert r["status"] == "failed" and r["http_status"] == 404
    assert "secret-token" not in json.dumps(r)


def test_network_error_message_is_scrubbed(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.URLError(f"cannot reach {URL}")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    r = notify.deliver(URL, "hi")
    assert r["status"] == "failed" and "URLError" in r["reason"]
    assert "secret-token" not in r["reason"]


def test_rate_limit_is_retried_once(monkeypatch):
    calls, sleeps = [], []

    def limited(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"retry_after": 0.5}'))
        return FakeResp(204)
    monkeypatch.setattr(notify.urllib.request, "urlopen", limited)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    assert notify.deliver(URL, "hi")["status"] == "sent"
    assert len(calls) == 2 and sleeps == [0.5]


def test_old_send_discord_api_still_works(monkeypatch):
    r = notify.send_discord({"team": "Payments", "channel": "#team-payments"}, "hi", url="")
    assert r["sent"] is False and r["simulated"] is True
