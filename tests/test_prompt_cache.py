import os
from pathlib import Path
import unittest
import uuid

from app import db


class ProviderPromptCacheDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"provider-cache-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_existing_provider_defaults_remain_generic_and_uncached(self):
        saved = db.provider_upsert(
            "", "Existing relay", "https://relay.example/v1", "encrypted", True
        )

        self.assertEqual(saved["provider_type"], "generic")
        self.assertEqual(saved["prompt_cache_ttl"], "off")

    def test_provider_profiles_no_longer_retain_cache_duration(self):
        saved = db.provider_upsert(
            "", "OpenRouter", "https://openrouter.ai/api/v1", "encrypted", True,
            provider_type="openrouter", prompt_cache_ttl="1h",
        )
        public = next(item for item in db.provider_list() if item["id"] == saved["id"])

        self.assertEqual(public["provider_type"], "openrouter")
        self.assertEqual(public["prompt_cache_ttl"], "off")
        self.assertTrue(public["has_key"])

    def test_generic_provider_cannot_retain_a_cache_ttl(self):
        saved = db.provider_upsert(
            "", "Relay", "https://relay.example/v1", "encrypted", True,
            provider_type="generic", prompt_cache_ttl="1h",
        )

        self.assertEqual(saved["prompt_cache_ttl"], "off")

    def test_old_provider_default_is_migrated_once_to_unchosen_chats(self):
        saved = db.provider_upsert(
            "", "Claude relay", "https://relay.example/v1", "encrypted", True,
            provider_type="claude_compatible",
        )
        chat = db.chat_add("Legacy")
        with db.conn() as cx:
            cx.execute(
                "UPDATE provider_profiles SET prompt_cache_ttl='1h' WHERE id=?",
                (saved["id"],),
            )
            cx.execute(
                "UPDATE chats SET provider_id=?, prompt_cache_ttl='' WHERE id=?",
                (saved["id"], chat["id"]),
            )
            cx.execute("DELETE FROM settings WHERE key='cache_scope_per_chat_v1'")

        db.init_db()

        self.assertEqual(db.chat_model_get(chat["id"])["prompt_cache_ttl"], "1h")
        self.assertEqual(db.provider_get(saved["id"])["prompt_cache_ttl"], "off")

    def test_chat_cache_ttl_round_trips_independently(self):
        first = db.chat_add("First")
        second = db.chat_add("Second")

        db.chat_model_set(first["id"], prompt_cache_ttl="1h")
        db.chat_model_set(second["id"], prompt_cache_ttl="5m")

        self.assertEqual(db.chat_model_get(first["id"])["prompt_cache_ttl"], "1h")
        self.assertEqual(db.chat_model_get(second["id"])["prompt_cache_ttl"], "5m")


class PromptCacheIntegrationSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.main = (root / "app" / "main.py").read_text(encoding="utf-8")
        cls.ui = (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_ordinary_chat_places_transient_context_after_stable_history(self):
        self.assertIn("def _cache_friendly_chat_messages(", self.main)
        self.assertIn("return stable + history[:-1] + [current]", self.main)
        self.assertIn(
            "transient_messages = private_message + memory_card_message + device_message",
            self.main,
        )
        self.assertIn('session_id=f"dwell-chat:{chat_id}" if cache_friendly else None', self.main)

    def test_provider_ui_only_selects_the_transport(self):
        self.assertIn('id="apProviderType"', self.ui)
        self.assertNotIn('id="apCacheTtl"', self.ui)
        self.assertIn('value="claude_compatible">Anthropic Messages', self.ui)
        self.assertIn("缓存时长在每间聊天的模型选择里设置", self.ui)
        self.assertIn("https://openrouter.ai/api/v1", self.ui)

    def test_chat_cache_ttl_is_exposed_and_applied_to_both_request_paths(self):
        self.assertIn('"prompt_cache_ttl": _chat_prompt_cache_ttl(selection, provider)', self.main)
        self.assertIn("def _chat_cache_supported(", self.main)
        self.assertEqual(self.main.count("_chat_cache_provider(provider, selection)"), 2)
        self.assertIn("prompt_cache_ttl=prompt_cache_ttl", self.main)
        self.assertIn("function renderPromptCachePicker()", self.ui)
        self.assertIn("function setChatPromptCacheTtl(ttl, button)", self.ui)
        self.assertIn("{id: '5m', name: '缓存 5 分钟'}", self.ui)
        self.assertIn("{id: '1h', name: '缓存 1 小时'}", self.ui)
        self.assertIn('"cache_write_tokens"', self.main)
        self.assertIn("缓存写入", self.ui)
        self.assertIn("缓存读取", self.ui)
        self.assertNotIn("provider.provider_type === 'generic'", self.ui)
        self.assertNotIn('prepared["provider_type"] = "claude_compatible"', self.main)
        self.assertIn("这间聊天的缓存时长", self.ui)
        self.assertIn("缓存 5 分钟", self.ui)
        self.assertIn('f"缓存：{cache_outcome}', self.main)
        self.assertIn("未缓存输入", self.main)
        self.assertIn("context_input_tokens", self.ui)
        self.assertIn("usage：", self.main)
        self.assertIn('event["type"] == "cache_status"', self.main)
        self.assertIn('"anthropic_messages": "Anthropic Messages"', self.main)
        self.assertIn("cache_fallback_reason", self.main)
        self.assertIn("回退 {cache_fallback_reason or '无'}", self.main)


if __name__ == "__main__":
    unittest.main()
