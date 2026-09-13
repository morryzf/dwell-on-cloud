"""OpenRouter account usage helpers.

The browser never receives the provider secret.  This module normalizes the two
small read-only OpenRouter endpoints and builds UTC spending buckets from Dwell's
own persisted request ledger.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


async def _get_data(client: httpx.AsyncClient, url: str, api_key: str) -> tuple[dict | None, str]:
    try:
        response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.RequestError:
        return None, "network"
    if response.status_code >= 400:
        return None, f"http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, "invalid_json"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, "invalid_response"
    return data, ""


async def fetch_openrouter_snapshot(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch account balance and current-key usage without exposing the key."""

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
    root = base_url.rstrip("/")
    try:
        (credits, credits_error), (key, key_error) = await asyncio.gather(
            _get_data(client, root + "/credits", api_key),
            _get_data(client, root + "/key", api_key),
        )
    finally:
        if owns_client:
            await client.aclose()

    balance = {"available": False, "error": credits_error}
    if credits is not None:
        total_credits = _number(credits.get("total_credits"))
        total_usage = _number(credits.get("total_usage"))
        if total_credits is not None and total_usage is not None:
            balance = {
                "available": True,
                "total_credits": round(total_credits, 8),
                "total_usage": round(total_usage, 8),
                "remaining": round(total_credits - total_usage, 8),
                "error": "",
            }
        else:
            balance["error"] = "invalid_response"

    current_key = {"available": False, "error": key_error}
    if key is not None:
        usage = {
            name: _number(key.get(name))
            for name in ("usage", "usage_daily", "usage_weekly", "usage_monthly")
        }
        if any(value is not None for value in usage.values()):
            current_key = {
                "available": True,
                "label": str(key.get("label") or key.get("name") or "")[:120],
                "usage": round(usage["usage"] or 0, 8),
                "usage_daily": round(usage["usage_daily"] or 0, 8),
                "usage_weekly": round(usage["usage_weekly"] or 0, 8),
                "usage_monthly": round(usage["usage_monthly"] or 0, 8),
                "limit": _number(key.get("limit")),
                "limit_remaining": _number(key.get("limit_remaining")),
                "limit_reset": str(key.get("limit_reset") or ""),
                "error": "",
            }
        else:
            current_key["error"] = "invalid_response"

    return {"balance": balance, "key": current_key}


def _month_start(value: datetime, delta: int = 0) -> datetime:
    month_index = value.year * 12 + value.month - 1 + delta
    return datetime(month_index // 12, month_index % 12 + 1, 1, tzinfo=timezone.utc)


def build_utc_cost_series(events: list[dict], *, now: datetime | None = None) -> dict:
    """Build UTC cost buckets with request-level prompt-cache hit rates."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = _month_start(day_start)

    daily_starts = [day_start - timedelta(days=offset) for offset in range(6, -1, -1)]
    weekly_starts = [week_start - timedelta(weeks=offset) for offset in range(7, -1, -1)]
    monthly_starts = [_month_start(month_start, -offset) for offset in range(11, -1, -1)]

    def empty_bucket() -> dict:
        return {"cost": 0.0, "requests": 0, "cache_hits": 0}

    daily = {start.date().isoformat(): empty_bucket() for start in daily_starts}
    weekly = {start.date().isoformat(): empty_bucket() for start in weekly_starts}
    monthly = {start.strftime("%Y-%m"): empty_bucket() for start in monthly_starts}

    oldest_day = daily_starts[0]
    oldest_week = weekly_starts[0]
    oldest_month = monthly_starts[0]
    history_total = 0.0

    def add(bucket: dict, cost: float, observed: bool, hit: bool) -> None:
        bucket["cost"] += cost
        if observed:
            bucket["requests"] += 1
            bucket["cache_hits"] += int(hit)

    for event in events:
        cost = _number(event.get("cost"))
        try:
            made = int(event.get("made") or 0)
        except (TypeError, ValueError):
            continue
        if cost is None or made <= 0:
            continue
        at = datetime.fromtimestamp(made, tz=timezone.utc)
        history_total += cost
        observed = bool(event.get("cache_observed"))
        try:
            hit = observed and int(event.get("cached_tokens") or 0) > 0
        except (TypeError, ValueError):
            hit = False
        if at >= oldest_day:
            key = at.date().isoformat()
            if key in daily:
                add(daily[key], cost, observed, hit)
        if at >= oldest_week:
            bucket_start = (
                at.replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=at.weekday())
            )
            key = bucket_start.date().isoformat()
            if key in weekly:
                add(weekly[key], cost, observed, hit)
        if at >= oldest_month:
            key = at.strftime("%Y-%m")
            if key in monthly:
                add(monthly[key], cost, observed, hit)

    def pack(rows: dict[str, dict]) -> list[dict]:
        packed = []
        for bucket_start, values in rows.items():
            requests = int(values["requests"])
            hits = int(values["cache_hits"])
            packed.append({
                "start": bucket_start,
                "cost": round(values["cost"], 8),
                "requests": requests,
                "cache_hits": hits,
                "cache_hit_rate": round(hits / requests * 100, 1) if requests else None,
            })
        return packed

    return {
        "timezone": "UTC",
        "daily": pack(daily),
        "weekly": pack(weekly),
        "monthly": pack(monthly),
        "history_total": round(history_total, 8),
    }

def parse_usd_cny_rate(payload: Any) -> tuple[float | None, str]:
    if not isinstance(payload, dict):
        return None, ""
    rate = _number(payload.get("rate"))
    return (rate if rate and rate > 0 else None), str(payload.get("date") or "")[:20]
