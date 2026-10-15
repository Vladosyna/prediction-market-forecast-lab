"""The LLM client stops calling a provider that keeps failing (2026-10-15)."""

from __future__ import annotations

import pytest

from lab.news.extract import LLM_FAILURE_LIMIT, BudgetExceeded, LlmClient, LlmUnavailable
from lab.store import db


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    conn = db.connect(tmp_path / "lab.db")
    config = {"llm": {"provider": "deepseek", "daily_cost_cap_usd": 5.0}}
    return LlmClient(conn, config)


def test_a_provider_that_keeps_failing_is_not_called_again_this_run(tmp_path, monkeypatch):
    """The night of 10-15 the provider was down for two hours, each M3 market
    cost ~15 minutes of timeouts one after another, and the forecast pass ran
    five hours. After LLM_FAILURE_LIMIT failures in a row the client raises
    LlmUnavailable -- a BudgetExceeded, which every caller already treats as
    "no more LLM this run" -- without touching the network."""
    llm = _client(tmp_path, monkeypatch)
    calls = []

    def down(*args, **kwargs):
        calls.append(1)
        raise KeyError("choices")

    monkeypatch.setattr(llm, "_complete_openai_compatible", down)
    for _ in range(LLM_FAILURE_LIMIT):
        with pytest.raises(KeyError):
            llm.complete("s", "p", purpose="m3_extraction")
    with pytest.raises(LlmUnavailable) as caught:
        llm.complete("s", "p", purpose="m3_extraction")
    assert isinstance(caught.value, BudgetExceeded)
    assert len(calls) == LLM_FAILURE_LIMIT


def test_a_success_resets_the_count(tmp_path, monkeypatch):
    llm = _client(tmp_path, monkeypatch)
    outcomes = iter([KeyError("choices")] * (LLM_FAILURE_LIMIT - 1) + ["ok"]
                    + [KeyError("choices")] * (LLM_FAILURE_LIMIT - 1) + ["ok"])

    def flaky(*args, **kwargs):
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o, {}

    monkeypatch.setattr(llm, "_complete_openai_compatible", flaky)
    results = []
    for _ in range(2 * LLM_FAILURE_LIMIT):
        try:
            results.append(llm.complete("s", "p", purpose="m3b_direct")[0])
        except KeyError:
            results.append("error")
    assert results.count("ok") == 2, "an isolated failure streak below the limit must not trip it"
