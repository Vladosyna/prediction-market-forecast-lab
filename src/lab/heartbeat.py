"""Phase 18: dead-man heartbeat -- an outbound ping to an external monitoring
endpoint (healthchecks.io-class), so the collector or nightly backup job dying
silently while the operator is away for weeks still gets caught (brief S11's
named worst failure). Our code only emits a ping; the external service does
the alerting -- not a notification bot (S12 carve-out).

HEARTBEAT_URL unset in .env => send_heartbeat() is a silent no-op, the same
convention every other optional external key in this project follows (FRED,
Metaculus, NewsAPI).
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


ALARM_PREFIX = "alarm:"


def set_alarm(conn, name: str, reason: str | None) -> None:
    """Raise (reason) or clear (None) a data-health alarm (2026-09-25).

    Every large loss this project has had was DETECTED and written to a log
    that nobody read: three Kalshi blackouts (PAP 9.17, 9.26, 9.30 -- 21 of
    81 confirmatory days) with the coverage watchdog firing ERROR on the first
    day of the third, and a backup that stopped leaving the host while its
    heartbeat kept reporting success. The dead-man ping only ever proved the
    PROCESS was alive. An active alarm now turns the collector's ping into a
    healthchecks-style `/fail` carrying the reason, so the external monitor --
    not this code (§12) -- tells the operator that the DATA is failing too.
    Caller commits."""
    from lab.store import db

    db.set_meta(conn, f"{ALARM_PREFIX}{name}", reason or "")


def active_alarms(conn) -> dict[str, str]:
    return {r["key"][len(ALARM_PREFIX):]: r["value"] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? AND value != '' ORDER BY key",
        (f"{ALARM_PREFIX}%",))}


async def send_heartbeat(source: str, fail_reason: str | None = None) -> bool:
    """Ping HEARTBEAT_URL (env var) to signal `source` is alive -- or, with
    `fail_reason`, that it is alive but its data is failing.

    A failure goes to `<HEARTBEAT_URL>/fail` with the reason as the body,
    which healthchecks.io-class services record and put in the alert. If that
    request fails (an endpoint without a /fail route), NOTHING is sent: in an
    alarm state this function never reports success, so at worst the
    dead-man grace period raises the alert instead.

    Returns True on a successful ping, False on a no-op (URL unset) or a
    failed ping. Never raises -- a dead/unreachable monitoring endpoint must
    not take down the collector or the backup job it's meant to be watching
    over (guardrail 9).
    """
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if fail_reason:
                resp = await client.post(url.rstrip("/") + "/fail",
                                         content=fail_reason.encode("utf-8")[:10000])
                if getattr(resp, "status_code", 200) >= 400:
                    log.warning("heartbeat /fail rejected -- sending nothing, the grace "
                                "period will alert", extra={"ctx": {"source": source}})
                    return False
            else:
                await client.get(url)
        return True
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # httpx.InvalidURL (e.g. a malformed HEARTBEAT_URL typo -- unbalanced
        # brackets, bad IDNA host) is NOT a subclass of httpx.HTTPError, so it
        # must be caught explicitly here too -- otherwise a bad env value
        # would violate this function's "never raises" contract and, via
        # jobs.run_publish_job's shared try block, could make a successful
        # backup get reported as a publish failure.
        log.warning("heartbeat ping failed", extra={"ctx": {"source": source, "error": str(exc)}})
        return False
