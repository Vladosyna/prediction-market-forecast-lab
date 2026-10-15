"""LLM evidence extraction (strict JSON) + budget-guarded provider calls.

Supports Anthropic (native SDK) and OpenAI-compatible APIs (DeepSeek, etc.).
The prompt includes the verbatim resolution criteria and instructs the model
to judge relevance to those criteria, not to the topic. Invalid JSON is
rejected and retried once; a second failure skips the article batch.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from lab.news.providers import Article
from lab.store import db as dbmod
from lab.store.snapshots import utc_date_str
from lab.util import now_utc

log = logging.getLogger(__name__)

# Default pricing (USD per million tokens) when config omits llm.pricing.
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-5",
        "price_in_per_mtok": 3.0,
        "price_out_per_mtok": 15.0,
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "price_in_per_mtok": 0.14,
        "price_out_per_mtok": 0.28,
    },
}

EXTRACTION_SYSTEM = """You extract structured evidence from news articles for probability forecasting.
Respond ONLY with a JSON array, no prose. Each element:
{"claim": str, "direction": "for_yes"|"for_no"|"neutral", "strength": 1|2|3,
 "source_reliability": 1|2|3, "relevance": float 0.0-1.0, "article_index": int}
Judge relevance STRICTLY against the market's resolution criteria quoted in the
user message -- an article can be on-topic but irrelevant to the specific
resolution conditions; give such items low relevance. strength is how strongly
the claim, if true, moves the outcome. source_reliability reflects the outlet
and sourcing quality. Return [] if no evidence items qualify."""


def extraction_prompt(question: str, description: str | None, end_date_iso: str | None,
                      p_market: float, price_path_7d: list[float],
                      articles: list[Article]) -> str:
    lines = [
        f"MARKET QUESTION: {question}",
        f"RESOLUTION CRITERIA (verbatim): {description or 'NOT AVAILABLE'}",
        f"END DATE: {end_date_iso or 'unknown'}",
        f"CURRENT MARKET PRICE (YES): {p_market:.3f}",
        f"7-DAY PRICE PATH: {[round(p, 3) for p in price_path_7d]}",
        "",
        "ARTICLES:",
    ]
    for i, a in enumerate(articles):
        lines.append(f"[{i}] ({a.published_ts or 'no date'}) {a.title} -- {a.summary[:300]}")
    return "\n".join(lines)


class BudgetExceeded(RuntimeError):
    pass


# Consecutive failed calls after which a client stops calling for the rest of
# its run (2026-10-15). The provider was down from 02:00 to ~04:05 that night;
# every M3 market cost ~15 minutes of timeouts, one after another, and the
# whole forecast pass ran five hours. A day-long outage would have run ~30.
LLM_FAILURE_LIMIT = 3


class LlmUnavailable(BudgetExceeded):
    """Raised instead of calling once the provider has failed LLM_FAILURE_LIMIT
    times in a row. A BudgetExceeded on purpose: every caller already treats
    that as "no more LLM calls this run" -- the forecast pass disables the
    model, the verify job keeps its unasked candidates, post-mortems stop."""


def _llm_section(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("llm", {})


def resolve_llm_provider(config: dict[str, Any]) -> str | None:
    """Return the configured provider name when its API key is present."""
    llm = _llm_section(config)
    provider = llm.get("provider", "anthropic")
    defaults = _PROVIDER_DEFAULTS.get(provider, {})
    key_env = llm.get("api_key_env") or defaults.get("api_key_env", "")
    if key_env and os.environ.get(key_env):
        return provider
    return None


def m3_model_id(config: dict[str, Any]) -> str:
    """Ledger model id; non-Anthropic providers register as challengers."""
    provider = resolve_llm_provider(config) or _llm_section(config).get("provider", "anthropic")
    if provider == "anthropic":
        return "m3_evidence"
    return f"m3_evidence@{provider}"


def create_llm_client(conn, config: dict[str, Any]) -> LlmClient | None:
    """Build an LLM client when the configured provider's API key is set."""
    if resolve_llm_provider(config) is None:
        return None
    return LlmClient(conn, config)


class LlmClient:
    """Provider wrapper with a hard daily USD cap checked BEFORE each call."""

    def __init__(self, conn, config: dict[str, Any]) -> None:
        llm = _llm_section(config)
        provider = llm.get("provider", "anthropic")
        defaults = _PROVIDER_DEFAULTS.get(provider, {})
        key_env = llm.get("api_key_env") or defaults.get("api_key_env", "")
        api_key = os.environ.get(key_env, "")
        if not api_key:
            raise ValueError(f"LLM provider {provider!r} requires {key_env}")

        self._conn = conn
        self._consecutive_failures = 0
        self._provider = provider
        self._model = llm.get("model") or defaults.get("model", "")
        self._cap = float(llm.get("daily_cost_cap_usd", 5.0))
        pricing = llm.get("pricing", {})
        self._price_in = float(pricing.get("input_per_mtok", defaults.get("price_in_per_mtok", 0)))
        self._price_out = float(pricing.get("output_per_mtok", defaults.get("price_out_per_mtok", 0)))

        if provider == "anthropic":
            import anthropic

            self._anthropic = anthropic.Anthropic(api_key=api_key)
            self._base_url = None
            self._api_key = None
        else:
            self._anthropic = None
            self._base_url = (llm.get("base_url") or defaults.get("base_url", "")).rstrip("/")
            self._api_key = api_key
            if not self._base_url:
                raise ValueError(f"LLM provider {provider!r} requires base_url in config")

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    def spend_today(self) -> float:
        return dbmod.llm_spend_today(self._conn, utc_date_str(now_utc()))

    def _record_spend(self, purpose: str, cost: float) -> None:
        dbmod.record_llm_spend(self._conn, utc_date_str(now_utc()), purpose, cost)
        self._conn.commit()

    def complete(self, system: str, prompt: str, purpose: str,
                 max_tokens: int = 2000) -> tuple[str, dict[str, Any]]:
        if self._consecutive_failures >= LLM_FAILURE_LIMIT:
            raise LlmUnavailable(f"LLM provider failed {self._consecutive_failures} calls "
                                 "in a row; no more calls this run")
        spent = self.spend_today()
        if spent >= self._cap:
            raise BudgetExceeded(f"daily LLM cap reached: ${spent:.2f} >= ${self._cap:.2f}")

        try:
            if self._provider == "anthropic":
                result = self._complete_anthropic(system, prompt, purpose, max_tokens)
            else:
                result = self._complete_openai_compatible(system, prompt, purpose, max_tokens)
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures == LLM_FAILURE_LIMIT:
                log.warning("llm: provider failed %d calls in a row -- no more calls this run",
                            LLM_FAILURE_LIMIT,
                            extra={"ctx": {"provider": self._provider, "purpose": purpose,
                                           "last_error": f"{type(exc).__name__}: {exc}"[:200]}})
            raise
        self._consecutive_failures = 0
        return result

    def _complete_anthropic(self, system: str, prompt: str, purpose: str,
                            max_tokens: int) -> tuple[str, dict[str, Any]]:
        resp = self._anthropic.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        tokens_in = resp.usage.input_tokens
        tokens_out = resp.usage.output_tokens
        cost = (tokens_in * self._price_in + tokens_out * self._price_out) / 1e6
        self._record_spend(purpose, cost)
        text = "".join(block.text for block in resp.content if block.type == "text")
        return text, {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}

    def _complete_openai_compatible(self, system: str, prompt: str, purpose: str,
                                    max_tokens: int) -> tuple[str, dict[str, Any]]:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        usage = data.get("usage", {})
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        cost = (tokens_in * self._price_in + tokens_out * self._price_out) / 1e6
        self._record_spend(purpose, cost)
        text = data["choices"][0]["message"]["content"]
        return text, {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def parse_evidence_json(text: str) -> list[dict[str, Any]] | None:
    """Strict parse; returns None on any structural violation."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("items", "evidence", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return None
    if not isinstance(data, list):
        return None
    items: list[dict[str, Any]] = []
    for el in data:
        if not isinstance(el, dict):
            return None
        if el.get("direction") not in ("for_yes", "for_no", "neutral"):
            return None
        try:
            item = {
                "claim": str(el["claim"]),
                "direction": el["direction"],
                "strength": int(el["strength"]),
                "source_reliability": int(el["source_reliability"]),
                "relevance": float(el["relevance"]),
                "article_index": int(el.get("article_index", -1)),
            }
        except (KeyError, TypeError, ValueError):
            return None
        if not (1 <= item["strength"] <= 3 and 1 <= item["source_reliability"] <= 3
                and 0.0 <= item["relevance"] <= 1.0):
            return None
        items.append(item)
    return items


def extract_evidence(llm: LlmClient, question: str, description: str | None,
                     end_date_iso: str | None, p_market: float,
                     price_path_7d: list[float], articles: list[Article],
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One extraction round with a single strict-JSON retry."""
    prompt = extraction_prompt(question, description, end_date_iso, p_market,
                               price_path_7d, articles)
    usage_total = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    for attempt in range(2):
        text, usage = llm.complete(EXTRACTION_SYSTEM, prompt, purpose="m3_extraction")
        for key in usage_total:
            usage_total[key] += usage[key]
        items = parse_evidence_json(text)
        if items is not None:
            for item in items:
                idx = item.pop("article_index", -1)
                if 0 <= idx < len(articles):
                    item["published_ts"] = articles[idx].published_ts
                    item["url"] = articles[idx].url
                else:
                    item["published_ts"] = None
            return items, usage_total
        log.warning("m3: invalid extraction JSON", extra={"ctx": {"attempt": attempt}})
    return [], usage_total
