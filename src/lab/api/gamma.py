"""Gamma API client -- market/event metadata (public, no auth).

Only the fields the lab consumes are modeled; everything else in the Gamma
payload is ignored. Gamma serializes some list fields (outcomes, token ids)
as JSON strings -- validators below normalize that.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from lab.api.http import BaseClient, TokenBucket

log = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
PAGE_SIZE = 100  # 2026 pagination limits: assume <=100 per page


def _parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


class GammaMarket(BaseModel):
    """Subset of a Gamma /markets item used by the lab."""

    condition_id: str = Field(alias="conditionId")
    slug: str | None = None
    question: str | None = None
    category: str | None = None
    description: str | None = None  # verbatim resolution criteria (critical for M3)
    end_date_iso: str | None = Field(default=None, alias="endDate")
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[str] = Field(default_factory=list, alias="outcomePrices")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    neg_risk: bool = Field(default=False, alias="negRisk")
    active: bool | None = None
    closed: bool | None = None
    enable_order_book: bool | None = Field(default=None, alias="enableOrderBook")
    accepting_orders: bool | None = Field(default=None, alias="acceptingOrders")
    liquidity_num: float | None = Field(default=None, alias="liquidityNum")
    volume_num: float | None = Field(default=None, alias="volumeNum")
    # 24h volume, a Phase 15 forecast covariate. Free: it rides along on the
    # same market object the universe sync already fetches.
    volume_24h: float | None = Field(default=None, alias="volume24hr")
    uma_resolution_statuses: list[str] = Field(
        default_factory=list, alias="umaResolutionStatuses"
    )
    start_date_iso: str | None = Field(default=None, alias="startDate")
    # The venue's own close/resolution time (2026-09-25); formats vary, see
    # util.parse_venue_ts. Recorded as resolutions.venue_resolved_ts.
    closed_time: str | None = Field(default=None, alias="closedTime")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    _norm_outcomes = field_validator("outcomes", "outcome_prices", "clob_token_ids",
                                     "uma_resolution_statuses", mode="before")(_parse_json_list)

    @property
    def is_binary(self) -> bool:
        return [o.lower() for o in self.outcomes] == ["yes", "no"] and len(self.clob_token_ids) == 2

    @property
    def token_id_yes(self) -> str | None:
        return self.clob_token_ids[0] if self.is_binary else None

    @property
    def token_id_no(self) -> str | None:
        return self.clob_token_ids[1] if self.is_binary else None


class GammaEvent(BaseModel):
    """Subset of a Gamma /events item: tags carry the category signal."""

    slug: str | None = None
    neg_risk: bool = Field(default=False, alias="negRisk")
    tags: list[dict] = Field(default_factory=list)
    markets: list[GammaMarket] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def tag_slugs(self) -> list[str]:
        return [t.get("slug", "") for t in self.tags if isinstance(t, dict)]


class GammaClient(BaseClient):
    def __init__(self, bucket: TokenBucket, base_url: str = GAMMA_BASE_URL) -> None:
        super().__init__(base_url, bucket)

    async def markets_page(self, offset: int, **filters: Any) -> list[GammaMarket]:
        params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": offset, **filters}
        raw = await self.get_json("/markets", params=params)
        items = raw if isinstance(raw, list) else raw.get("data", [])
        markets: list[GammaMarket] = []
        for item in items:
            try:
                markets.append(GammaMarket.model_validate(item))
            except Exception:
                log.warning("gamma: skipping unparseable market", extra={"ctx": {"id": item.get("id")}})
        return markets

    async def iter_markets(self, max_pages: int = 200, **filters: Any) -> list[GammaMarket]:
        """Paginate /markets until a short page. Filters pass through (active, closed, ...).

        Gamma caps `offset` (observed: 422 beyond ~2000), so pages are ordered
        by volume descending -- the offset window then covers the most liquid
        markets, which is exactly the universe the lab tracks. The 422 is
        treated as end-of-pagination, logged loud.
        """
        filters.setdefault("order", "volumeNum")
        filters.setdefault("ascending", "false")
        out: list[GammaMarket] = []
        for page in range(max_pages):
            try:
                batch = await self.markets_page(offset=page * PAGE_SIZE, **filters)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    log.warning("gamma: offset cap reached, stopping pagination",
                                extra={"ctx": {"page": page}})
                    break
                raise
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return out

    async def iter_events(self, max_pages: int = 200, **filters: Any) -> list[GammaEvent]:
        """Paginate /events (embeds markets + tags), volume-ordered like iter_markets."""
        filters.setdefault("order", "volume")
        filters.setdefault("ascending", "false")
        out: list[GammaEvent] = []
        for page in range(max_pages):
            params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE, **filters}
            try:
                raw = await self.get_json("/events", params=params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    log.warning("gamma: events offset cap reached, stopping pagination",
                                extra={"ctx": {"page": page}})
                    break
                raise
            items = raw if isinstance(raw, list) else raw.get("data", [])
            for item in items:
                try:
                    out.append(GammaEvent.model_validate(item))
                except Exception:
                    log.warning("gamma: skipping unparseable event",
                                extra={"ctx": {"slug": item.get("slug")}})
            if len(items) < PAGE_SIZE:
                break
        return out

    async def market_by_condition(self, condition_id: str) -> GammaMarket | None:
        # Gamma's /markets defaults to closed=false when the param is omitted,
        # so a plain condition_ids lookup silently finds nothing once a market
        # closes -- exactly the markets the resolution watcher and historical
        # bootstrap need. Try the unfiltered (open-market) case first, then
        # fall back to closed=true.
        for params in (
            {"condition_ids": condition_id},
            {"condition_ids": condition_id, "closed": "true"},
        ):
            raw = await self.get_json("/markets", params=params)
            items = raw if isinstance(raw, list) else raw.get("data", [])
            for item in items:
                try:
                    return GammaMarket.model_validate(item)
                except Exception:
                    continue
        return None
