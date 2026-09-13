import os
import unittest
import uuid
from pathlib import Path

from app import db
from app.llm_client import content_texts, reasoning_texts


class ThinkingExtractionTest(unittest.TestCase):
    def test_reads_common_reasoning_fields_without_duplication(self):
        delta = {
            "reasoning_content": "先确认条件。",
            "reasoning_details": [{"type": "reasoning.text", "text": "重复内容"}],
        }
        self.assertEqual(reasoning_texts(delta), ["先确认条件。"])
        self.assertEqual(
            reasoning_texts({
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "第一步"},
                    {"type": "reasoning.summary", "summary": "再检查一次"},
                ],
            }),
            ["第一步", "再检查一次"],
        )

    def test_keeps_thinking_parts_out_of_answer_text(self):
        content = [
            {"type": "thinking", "thinking": "内部推理"},
            {"type": "text", "text": "最终回答"},
        ]
        self.assertEqual(reasoning_texts({"content": content}), ["内部推理"])
        self.assertEqual(content_texts(content), ["最终回答"])


class ThinkingDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"thinking-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()
        self.chat = db.chat_add("thinking test")

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_saved_thinking_remains_visible_when_future_thinking_is_disabled(self):
        message = db.message_add(self.chat["id"], "assistant", "回答")
        self.assertTrue(db.message_thinking_update(message["id"], "推理内容"))

        visible = db.message_ui_list(self.chat["id"])["msgs"][0]
        self.assertEqual(visible["thinking"], "推理内容")

        db.chat_model_set(self.chat["id"], show_thinking=False)
        still_visible = db.message_ui_list(self.chat["id"])["msgs"][0]
        self.assertEqual(still_visible["thinking"], "推理内容")

        db.chat_model_set(self.chat["id"], show_thinking=True)
        restored = db.message_ui_list(self.chat["id"])["msgs"][0]
        self.assertEqual(restored["thinking"], "推理内容")


class ThinkingControlStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "static" / "index.html").read_text(encoding="utf-8")
        cls.main_source = (root / "app" / "main.py").read_text(encoding="utf-8")

    def test_composer_toggles_share_one_active_style_and_no_idle_border(self):
        self.assertIn(
            "#instructionsBtn.on, #mcpChatBtn.on, #reasoningBtn.on",
            self.html,
        )
        self.assertNotIn(
            "header .iconbtn, #plusBtn, #instructionsBtn, #mcpChatBtn, .pill",
            self.html,
        )

    def test_chat_requests_apply_the_thinking_switch(self):
        self.assertIn("thinking_enabled=show_thinking", self.main_source)


if __name__ == "__main__":
    unittest.main()
