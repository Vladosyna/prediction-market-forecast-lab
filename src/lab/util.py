"""Shared utilities: the single clock call, config loading, logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def use_stable_event_loop() -> None:
    """Select a Windows-stable asyncio event loop before any ``asyncio.run``.

    The default Windows Proactor loop crashes with a native access violation
    (exit code 0xC0000005) under sustained async HTTP traffic -- a long-known
    CPython issue in the Proactor's ``_loop_writing``/``send`` path. Our request
    pattern is sequential and rate-limited (well under the Selector loop's fd
    ceiling), so the Selector loop is both stable and sufficient. No-op off
    Windows.
    """
    if not sys.platform.startswith("win"):
        return
    import asyncio

    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None and not isinstance(asyncio.get_event_loop_policy(), policy):
        asyncio.set_event_loop_policy(policy())


def now_utc() -> datetime:
    """The only clock call allowed in this codebase (guardrail 6)."""
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return now_utc().isoformat(timespec="seconds")


_VENUE_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?(?::\d{2})?)?$")


def parse_venue_ts(value: str | None) -> str | None:
    """A venue's own timestamp, normalised to this lab's form
    ("YYYY-MM-DDTHH:MM:SS+00:00", UTC) -- or None if it cannot be read.

    Venues do not agree with each other or with themselves. Seen live on
    2026-09-25: Kalshi `settlement_ts` "2026-09-14T13:35:38.40392Z"; Gamma
    `closedTime` "2026-09-25 06:26:15+00" and "2026-09-25 06:35:18.320219+00";
    Gamma `umaEndDate` "2026-09-25 06:35:18.320219+00:00:00". Only UTC offsets
    have ever been observed; anything else is returned as None rather than
    silently shifted, because a wrong resolution time is worse than none.
    """
    if not value:
        return None
    m = _VENUE_TS.match(value.strip())
    if not m:
        return None
    day, clock, offset = m.groups()
    if offset not in (None, "Z") and offset.replace(":", "").lstrip("+-").strip("0"):
        return None
    try:
        parsed = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_CONFIG_PATH
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class JsonLinesFormatter(logging.Formatter):
    """Structured logging: one JSON object per line (guardrail 9)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "ctx", None)
        if extra:
            entry["ctx"] = extra
        return json.dumps(entry, ensure_ascii=False)


# Query-string secrets that must never reach a log line. FRED's api_key param
# is the concrete case (M5 macro adapter/refit); kept generic since any future
# adapter using a URL-embedded key would hit the same httpx auto-logging.
_SECRET_QUERY_PARAM_RE = re.compile(r"(?<=[?&])(api_key|apikey|access_token)=[^&\s\"']+", re.I)


class RedactSecretsFilter(logging.Filter):
    """Strips URL-embedded API keys from every log record before it's emitted.

    httpx logs each request's full URL at INFO level. For endpoints that pass
    a key as a query param (FRED) that would otherwise put a live secret in
    data/logs/lab.jsonl on every single call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_QUERY_PARAM_RE.sub(r"\1=***REDACTED***", msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(config: dict[str, Any] | None = None, level: int = logging.INFO) -> None:
    """Console handler (human-readable) + rotating JSONL file handler."""
    config = config or load_config()
    logs_dir = PROJECT_ROOT / config["storage"]["logs_dir"]
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:  # idempotent: safe to call more than once
        return
    root.setLevel(level)
    redact = RedactSecretsFilter()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console.addFilter(redact)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "lab.jsonl", maxBytes=20_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonLinesFormatter())
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    # httpx logs one INFO line per request. A snapshot round issues thousands,
    # so the collector wrote ~507,000 lines a day and journald's 300MB cap kept
    # only the last ~16 hours. That cost real diagnosis three times during the
    # 2026-08 audit week -- the evidence for the first five days of the Kalshi
    # blackout, and for a service restart, had already been evicted by the time
    # anyone looked. Failures still log: this raises the floor to WARNING, it
    # does not silence the client.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
