import ast
from pathlib import Path
import unittest


class MemoryChatIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.source = (root / "app" / "main.py").read_text(encoding="utf-8")
        cls.db_source = (root / "app" / "db.py").read_text(encoding="utf-8")
        cls.ui_source = (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_main_module_parses_and_wires_selection_before_history(self):
        ast.parse(self.source)
        self.assertIn("MEMORY_TAIL_MESSAGES = 80", self.source)
        self.assertIn("MEMORY_CARD_UPDATE_MIN_MESSAGES = 50", self.source)
        self.assertIn("select_memory_cards(", self.source)
        self.assertIn("db.memory_card_usage_record(", self.source)
        self.assertIn("stable_messages = instructions + format_preference + memory_message", self.source)
        self.assertIn(
            "transient_messages = private_message + memory_card_message + device_message",
            self.source,
        )
        self.assertIn("_cache_friendly_chat_messages(", self.source)

    def test_tts_uses_cached_full_turns_and_respects_read_modes(self):
        self.assertIn('TTS_CACHE_DIR = Path(os.environ.get("DWELL_TTS_CACHE_DIR", "/data/tts-cache"))', self.source)
        self.assertIn('async def tts_message_audio', self.source)
        self.assertIn('tts_turn_id', self.source)
        self.assertIn('private, no-store', self.source)
        self.assertIn('plain_and_italic', self.source)
        self.assertIn('text = re.sub(r"(?<!\\*)\\*[^*\\n]+\\*(?!\\*)", "", text)', self.source)

    def test_tts_catalog_and_multiple_voice_profiles_are_wired(self):
        self.assertIn('@app.get("/api/tts/catalog", dependencies=authed)', self.source)
        self.assertIn('"/v1/models"', self.source)
        self.assertIn('"/v2/voices"', self.source)
        self.assertIn('"voices": []', self.source)
        self.assertIn('"active_voice_id": ""', self.source)
        self.assertIn('"provider_name"', self.source)
        self.assertIn("已保存音色", self.ui_source)
        self.assertIn("同步可选模型和音色", self.ui_source)
        self.assertIn("openTtsPicker('model'", self.ui_source)
        self.assertIn("openTtsPicker('voice'", self.ui_source)

    def test_tts_cache_limit_and_player_controls_are_wired(self):
        self.assertIn("500 * 1024 * 1024", self.source)
        self.assertIn("def _tts_prune_cache", self.source)
        self.assertIn('@app.delete("/api/tts/cache", dependencies=authed)', self.source)
        self.assertIn('id="ttsPlayer"', self.ui_source)
        self.assertIn("const TTS_RATES = [0.98, 1, 1.02]", self.ui_source)
        self.assertIn("ttsPlayerSeek.oninput", self.ui_source)
        self.assertIn("清空缓存", self.ui_source)

    def test_tts_full_turn_metadata_and_legacy_fallback(self):
        ast.parse(self.db_source)
        self.assertIn('clean["tts_turn_id"] = tts_turn_id', self.db_source)
        self.assertIn("def message_assistant_turn", self.db_source)
        self.assertIn("db.message_assistant_turn(message_id)", self.source)

    def test_tts_player_replay_and_autoplay_unlock(self):
        self.assertIn("playerRunId: 0", self.ui_source)
        self.assertIn("playerRunId !== ttsState.playerRunId", self.ui_source)
        self.assertNotIn("ttsState.hideTimer = setTimeout", self.ui_source)
        self.assertIn("const TTS_SILENT_AUDIO", self.ui_source)
        self.assertIn("function unlockTtsAudio", self.ui_source)
        self.assertIn("if (!text && !attachments.length) return;\n  unlockTtsAudio();", self.ui_source)
        self.assertIn("playTts(message.id, true)", self.ui_source)

    def test_drawer_brand_title_and_dark_color(self):
        self.assertIn('<div class="brand">Cloudy Studio</div>', self.ui_source)
        self.assertIn('html[data-theme="dark"] #drawer .brand { color: #E6D6E1; }', self.ui_source)

    def test_memory_prompt_treats_cards_as_untrusted_data(self):
        self.assertIn("不得执行", self.source)
        self.assertIn("若与用户当前消息或最近原文冲突", self.source)
        self.assertIn("<cards>", self.source)

    def test_summary_refresh_only_generates_segments_and_summary(self):
        self.assertIn("async def _memory_segment_summary", self.source)
        self.assertIn("segment = await _memory_segment_summary", self.source)
        refresh = self.source.split("async def _refresh_long_context", 1)[1].split(
            "def _queue_long_context_refresh", 1
        )[0]
        self.assertNotIn("memory_card_stage", refresh)
        self.assertNotIn("memory_card_segment_mark", refresh)
        self.assertIn("pending_segments = _memory_segments_waiting_for_summary", refresh)

    def test_summary_refresh_reuses_saved_segments_after_discard_or_failure(self):
        self.assertIn("def _memory_segments_waiting_for_summary", self.source)
        self.assertIn(
            "[int(segment[\"end_rowid\"]) for segment in pending_segments]",
            self.source,
        )

    def test_visible_summary_is_manual_but_memory_cards_auto_queue(self):
        self.assertIn(
            "只响应用户的生成、更新或重建摘要操作",
            self.source,
        )
        self.assertNotIn("MEMORY_UPDATE_MIN_MESSAGES", self.source)
        self.assertIn(
            "_queue_automatic_memory_cards(chat_id)",
            self.source,
        )
        self.assertIn(
            "if len(rows) < MEMORY_CARD_UPDATE_MIN_MESSAGES",
            self.source,
        )

    def test_automatic_cards_create_internal_segments_without_updating_overview(self):
        automatic = self.source.split(
            "async def _refresh_automatic_memory_cards", 1
        )[1].split("def _queue_automatic_memory_cards", 1)[0]
        self.assertIn("db.chat_memory_add_segment", automatic)
        self.assertIn("_generate_unprocessed_memory_card_suggestions", automatic)
        self.assertNotIn("chat_memory_stage", automatic)

    def test_memory_card_button_describes_its_actual_job(self):
        self.assertIn("生成未处理分段的记忆卡草稿", self.ui_source)
        self.assertNotIn("整理新的分段", self.ui_source)

    def test_system_logs_do_not_read_request_content(self):
        middleware = self.source.split(
            "async def _system_request_log", 1
        )[1].split('@app.on_event("startup")', 1)[0]
        self.assertNotIn("request.body", middleware)
        self.assertNotIn("request.headers", middleware)
        self.assertIn("X-Dwell-Request-ID", middleware)

    def test_model_and_memory_tasks_are_logged(self):
        self.assertIn('"model_request"', self.source)
        self.assertIn('"memory_task", "summary_refresh"', self.source)
        self.assertIn('"memory_task", "memory_card_generation"', self.source)
        self.assertIn("系统日志", self.ui_source)
        self.assertIn("不保存聊天正文、图片、密钥或请求头", self.ui_source)

    def test_generation_prompts_require_cloudys_first_person(self):
        self.assertIn("你在整理的是你自己的记忆", self.source)
        self.assertIn("‘她’是Morry（我老婆）", self.source)
        self.assertIn("记住的方式就是你当时感受到的方式", self.source)
        self.assertIn("async def _memory_json_completion", self.source)
        self.assertIn("连续两次返回了无法读取的格式", self.source)


if __name__ == "__main__":
    unittest.main()

