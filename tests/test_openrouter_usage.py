import asyncio
from datetime import datetime, timezone
import json
import os
import unittest
import uuid

import httpx

from app import db
from app.openrouter_usage import (
    build_utc_cost_series,
    fetch_openrouter_snapshot,
    parse_usd_cny_rate,
)


class OpenRouterSnapshotTest(unittest.TestCase):
    def test_regular_api_key_fetches_balance_and_current_period_usage(self):
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer sk-or-test")
            if request.url.path.endswith("/credits"):
                return httpx.Response(200, json={
                    "data": {"total_credits": 20, "total_usage": 7.755}
                })
            return httpx.Response(200, json={"data": {
                "label": "Dwell",
                "usage": 7.755,
                "usage_daily": 0.25,
                "usage_weekly": 1.5,
                "usage_monthly": 4.25,
                "limit": None,
                "limit_remaining": None,
                "limit_reset": None,
            }})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await fetch_openrouter_snapshot(
                    "https://openrouter.ai/api/v1", "sk-or-test", client=client
                )

        result = asyncio.run(run())
        self.assertTrue(result["balance"]["available"])
        self.assertEqual(result["balance"]["remaining"], 12.245)
        self.assertTrue(result["key"]["available"])
        self.assertEqual(result["key"]["usage_daily"], 0.25)
        self.assertEqual(result["key"]["usage_weekly"], 1.5)
        self.assertEqual(result["key"]["usage_monthly"], 4.25)

    def test_credits_failure_does_not_hide_key_usage(self):
        def handler(request):
            if request.url.path.endswith("/credits"):
                return httpx.Response(403, json={"error": "forbidden"})
            return httpx.Response(200, json={"data": {"usage_daily": 0.1}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await fetch_openrouter_snapshot(
                    "https://openrouter.ai/api/v1", "secret", client=client
                )

        result = asyncio.run(run())
        self.assertFalse(result["balance"]["available"])
        self.assertEqual(result["balance"]["error"], "http_403")
        self.assertTrue(result["key"]["available"])
        self.assertNotIn("secret", json.dumps(result))


class OpenRouterSeriesTest(unittest.TestCase):
    def test_builds_utc_daily_weekly_and_monthly_buckets(self):
        now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        events = [
            {"made": int(datetime(2026, 9, 5, 1, tzinfo=timezone.utc).timestamp()),
             "cost": 0.2, "cache_observed": 1, "cached_tokens": 320},
            {"made": int(datetime(2026, 9, 1, 23, tzinfo=timezone.utc).timestamp()),
             "cost": 0.3, "cache_observed": 1, "cached_tokens": 0},
            {"made": int(datetime(2026, 8, 30, 23, tzinfo=timezone.utc).timestamp()),
             "cost": 0.4, "cache_observed": 0, "cached_tokens": 0},
            {"made": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
             "cost": 1.0, "cache_observed": 0, "cached_tokens": 0},
        ]

        result = build_utc_cost_series(events, now=now)

        self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(len(result["daily"]), 7)
        self.assertEqual(len(result["weekly"]), 8)
        self.assertEqual(len(result["monthly"]), 12)
        self.assertEqual(result["daily"][-1], {
            "start": "2026-09-05", "cost": 0.2, "requests": 1,
            "cache_hits": 1, "cache_hit_rate": 100.0,
        })
        current_week = next(row for row in result["weekly"] if row["start"] == "2026-08-31")
        self.assertEqual(current_week["cost"], 0.5)
        self.assertEqual(current_week["requests"], 2)
        self.assertEqual(current_week["cache_hits"], 1)
        self.assertEqual(current_week["cache_hit_rate"], 50.0)
        legacy_week = next(row for row in result["weekly"] if row["start"] == "2026-08-24")
        self.assertIsNone(legacy_week["cache_hit_rate"])
        self.assertEqual(result["history_total"], 1.9)

    def test_parses_frankfurter_v2_rate(self):
        self.assertEqual(
            parse_usd_cny_rate({"date": "2026-09-04", "rate": 6.92}),
            (6.92, "2026-09-04"),
        )


class OpenRouterUsageDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"openrouter-usage-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_legacy_costs_backfill_once_and_new_requests_remain_distinct(self):
        with db.conn() as cx:
            cx.execute(
                "INSERT INTO chats (id,name,made) VALUES (?,?,?)",
                ("chat-1", "Test", 1),
            )
            cx.execute(
                "INSERT INTO messages (id,chat_id,role,content,made,usage_json) "
                "VALUES (?,?,?,?,?,?)",
                ("message-1", "chat-1", "assistant", "hello", 100,
                 json.dumps({"cost": 0.125})),
            )

        self.assertEqual(db.provider_usage_backfill_legacy("provider-1", "hash-1"), 1)
        self.assertEqual(db.provider_usage_backfill_legacy("provider-1", "hash-1"), 0)
        db.provider_usage_event_add(
            "provider-1", "hash-1", "message-2", "regenerate", 0.25, made=200,
            input_tokens=900, cached_tokens=640, cache_observed=True,
        )

        rows = db.provider_usage_events("provider-1", "hash-1")
        self.assertEqual(rows, [
            {"cost": 0.125, "made": 100, "input_tokens": 0,
             "cached_tokens": 0, "cache_observed": 0},
            {"cost": 0.25, "made": 200, "input_tokens": 900,
             "cached_tokens": 640, "cache_observed": 1},
        ])
        self.assertEqual(db.provider_usage_events("provider-1", "other-hash"), [])
        with db.conn() as cx:
            columns = {row["name"] for row in cx.execute(
                "PRAGMA table_info(provider_usage_events)"
            ).fetchall()}
        self.assertTrue({"input_tokens", "cached_tokens", "cache_observed"} <= columns)

    def test_message_usage_preserves_subcent_cost_precision(self):
        with db.conn() as cx:
            cx.execute(
                "INSERT INTO chats (id,name,made) VALUES (?,?,?)",
                ("chat-precision", "Test", 1),
            )
            cx.execute(
                "INSERT INTO messages (id,chat_id,role,content,made) VALUES (?,?,?,?,?)",
                ("message-precision", "chat-precision", "assistant", "hello", 100),
            )

        db.message_usage_update(
            "message-precision", {"cost": 0.00123456, "tokens_per_second": 12.3456}
        )
        usage = json.loads(db.message_get("message-precision")["usage_json"])
        self.assertEqual(usage["cost"], 0.00123456)
        self.assertEqual(usage["tokens_per_second"], 12.35)


if __name__ == "__main__":
    unittest.main()
