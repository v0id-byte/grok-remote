from __future__ import annotations

import json

from grok_bridge import cli


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_health_probes_the_requested_bridge_url(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response({"status": "ok"})

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    result = cli._health("http://bridge.example/")

    assert result["reachable"] is True
    assert result["url"] == "http://bridge.example/health"
    assert seen == {"url": "http://bridge.example/health", "timeout": 3}


def test_status_returns_failure_when_bridge_is_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_health",
        lambda url: {"reachable": False, "url": url, "error": "offline"},
    )

    assert cli.main(["status", "--url", "http://offline.test"]) == 1
    assert "offline" in capsys.readouterr().out
