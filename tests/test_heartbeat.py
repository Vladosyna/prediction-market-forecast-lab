"""Phase 18: dead-man heartbeat (src/lab/heartbeat.py).

HEARTBEAT_URL unset => silent no-op with zero network calls (verified by
making the client's own .get raise if it's ever reached). A configured URL
gets pinged; failures are swallowed and never propagate (guardrail 9).
"""

from __future__ import annotations

import asyncio

import httpx

from lab.heartbeat import send_heartbeat


class _AssertNotCalledClient:
    """Fake AsyncClient whose .get() blows up if the early-return path is
    ever bypassed -- proves the no-op case never reaches the network."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        raise AssertionError("network call attempted despite HEARTBEAT_URL being unset")


class _RecordingClient:
    """Fake AsyncClient recording every GET target, returning a 200."""

    def __init__(self, requests: list[str]) -> None:
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        self._requests.append(url)
        return httpx.Response(200)


class _RaisingClient:
    """Fake AsyncClient whose .get() raises a network-level httpx error."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        raise httpx.ConnectError("boom")


def test_send_heartbeat_noop_when_url_unset(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _AssertNotCalledClient())

    result = asyncio.run(send_heartbeat("collector"))

    assert result is False


def test_send_heartbeat_success(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc-ping.com/fake-uuid")
    requests: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _RecordingClient(requests))

    result = asyncio.run(send_heartbeat("backup"))

    assert result is True
    assert requests == ["https://hc-ping.com/fake-uuid"]


def test_send_heartbeat_swallows_network_error(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc-ping.com/fake-uuid")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _RaisingClient())

    result = asyncio.run(send_heartbeat("collector"))

    assert result is False


def test_send_heartbeat_swallows_malformed_url(monkeypatch):
    """A malformed HEARTBEAT_URL (e.g. a typo) raises httpx.InvalidURL, which
    is NOT a subclass of httpx.HTTPError -- must be swallowed too, or a bad
    env value would break the module's "never raises" contract."""
    monkeypatch.setenv("HEARTBEAT_URL", "http://[::1")
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)

    result = asyncio.run(send_heartbeat("collector"))

    assert result is False


# --- data-health alarms (2026-09-25) -----------------------------------------

class _FullRecordingClient:
    def __init__(self, calls, status=200):
        self.calls, self.status = calls, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.calls.append(("GET", url, None))

    async def post(self, url, content=None):
        self.calls.append(("POST", url, content))

        class _R:
            status_code = self.status
        return _R()


def test_an_alarm_turns_the_ping_into_fail_with_the_reason(monkeypatch):
    """The dead-man ping only ever proved the PROCESS was alive; three Kalshi
    blackouts and a backup that stopped leaving the host all reached nobody."""
    calls = []
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc-ping.example/abc")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FullRecordingClient(calls))
    assert asyncio.run(send_heartbeat("collector", fail_reason="coverage: kalshi 0%")) is True
    assert calls == [("POST", "https://hc-ping.example/abc/fail", b"coverage: kalshi 0%")]

    calls.clear()
    assert asyncio.run(send_heartbeat("collector")) is True
    assert calls == [("GET", "https://hc-ping.example/abc", None)]


def test_an_alarm_never_degrades_into_a_success_ping(monkeypatch):
    """An endpoint without a /fail route must not get a success ping instead:
    at worst the grace period raises the alert."""
    calls = []
    monkeypatch.setenv("HEARTBEAT_URL", "https://other.example/ping")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FullRecordingClient(calls, 404))
    assert asyncio.run(send_heartbeat("collector", fail_reason="backup: refused")) is False
    assert all(method == "POST" for method, _, _ in calls), "no success GET in an alarm state"


def test_alarms_are_raised_and_cleared(tmp_path):
    from lab.heartbeat import active_alarms, set_alarm
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    set_alarm(conn, "backup", "push refused")
    set_alarm(conn, "coverage", "kalshi 0%")
    assert active_alarms(conn) == {"backup": "push refused", "coverage": "kalshi 0%"}
    set_alarm(conn, "backup", None)
    assert active_alarms(conn) == {"coverage": "kalshi 0%"}
    conn.close()
