from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from app import main


class SharedPromptCacheSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).parents[1] / "app" / "main.py"
        ).read_text(encoding="utf-8")

    def test_chat_and_heartbeat_share_stable_prefix_builders(self):
        self.assertIn("def _chat_stable_message_parts(", self.source)
        self.assertIn(
            "_chat_history_messages(chat_id, cache_friendly=cache_friendly)",
            self.source,
        )
        self.assertIn("_chat_history_rows(chat_id, cache_friendly)", self.source)
        self.assertIn("_chat_history_messages_from_rows(history)", self.source)
        self.assertIn(
            "split_replies, instructions, format_preference, memory_message = (",
            self.source,
        )

    def test_chat_and_heartbeat_share_the_complete_tool_surface(self):
        self.assertEqual(self.source.count("await _chat_tools(chat_id)"), 2)
        self.assertIn('name not in readable_tools', self.source)
        self.assertIn('server == "builtin:search"', self.source)
        self.assertIn('server == "builtin:home"', self.source)

    def test_heartbeat_reuses_chat_sticky_session(self):
        heartbeat = self.source[
            self.source.index("async def _heartbeat_decide"):
            self.source.index("async def _heartbeat_once")
        ]
        self.assertIn('session_id=f"dwell-chat:{chat_id}" if cache_friendly else None', heartbeat)
        self.assertIn("prompt_cache_enabled(provider, selection[\"model_id\"])", heartbeat)

    def test_proactive_watch_keeps_dynamic_context_after_the_anchor(self):
        reply = self.source[
            self.source.index("async def _run_ai_reply"):
            self.source.index("@app.post(\"/api/send\"")
        ]
        self.assertNotIn("not proactive_watch\n        and provider", reply)
        self.assertIn("messages = stable_messages + history_messages", reply)
        self.assertIn("_transient_context_blocks(transient_messages)", reply)
        self.assertIn("messages.append({\"role\": \"user\", \"content\": proactive_content})", reply)

    def test_watch_context_preserves_structured_user_content(self):
        self.assertIn("if isinstance(existing, list)", self.source)
        self.assertIn('content.append({"type": "text", "text": note})', self.source)
        self.assertNotIn(
            'str(messages[index]["content"]) + note',
            self.source,
        )


class SharedPromptCacheBehaviorTest(unittest.TestCase):
    def test_transient_context_stays_after_the_stable_assistant_anchor(self):
        stable = [{"role": "system", "content": "identity"}]
        transient = [{"role": "system", "content": "current clock"}]
        history = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "stable answer"},
            {"role": "user", "content": "new question"},
        ]

        prepared = main._cache_friendly_chat_messages(stable, transient, history)

        self.assertEqual(prepared[:3], stable + history[:2])
        self.assertEqual(prepared[-1]["role"], "user")
        self.assertIn("current clock", prepared[-1]["content"][0]["text"])
        self.assertEqual(prepared[-1]["content"][-1]["text"], "new question")
        self.assertEqual(history[-1]["content"], "new question")

    def test_heartbeat_appends_one_dynamic_trigger_after_the_shared_prefix(self):
        stable = [{"role": "system", "content": "identity"}]
        history = [{"role": "assistant", "content": "last answer"}]
        now = datetime(2026, 9, 3, 9, 30, tzinfo=timezone.utc)
        with (
            patch.object(main, "_chat_stable_messages", return_value=stable),
            patch.object(
                main, "_chat_history_messages", return_value=history
            ) as history_messages,
            patch.object(main.db, "message_last_made", side_effect=[1, 2]),
        ):
            prepared = main._heartbeat_context(
                "chat-1", now, 120, cache_friendly=True
            )

        history_messages.assert_called_once_with(
            "chat-1", cache_friendly=True
        )
        self.assertEqual(prepared[:-1], stable + history)
        self.assertEqual(prepared[-1]["role"], "user")
        self.assertIn("2026-09-03 09:30", prepared[-1]["content"])
        self.assertIn("[NO_ACTION]", prepared[-1]["content"])

    def test_heartbeat_tool_filter_rejects_mutations_but_keeps_reads(self):
        read = {"function": {"name": "DiarySearch", "description": "Read diary entries"}}
        write = {"function": {"name": "DiaryCreate", "description": "Create an entry"}}
        toggle = next(
            tool for tool in main.HOME_TOOLS
            if tool["function"]["name"] == "DwellTodoToggle"
        )
        todo_list = next(
            tool for tool in main.HOME_TOOLS
            if tool["function"]["name"] == "DwellTodoList"
        )

        self.assertTrue(main._heartbeat_read_tool(read))
        self.assertFalse(main._heartbeat_read_tool(write))
        self.assertFalse(main._heartbeat_tool_allowed(toggle, "builtin:home"))
        self.assertTrue(main._heartbeat_tool_allowed(todo_list, "builtin:home"))


    def test_cache_history_window_keeps_its_original_head(self):
        initial_rows = [
            {"rowid": rowid, "role": "user", "content": str(rowid)}
            for rowid in range(1, 111)
        ]
        with (
            patch.object(main.db, "message_list", return_value=initial_rows) as message_list,
            patch.object(main.db, "setting_get", return_value="0"),
            patch.object(main.db, "setting_set") as setting_set,
        ):
            selected = main._chat_history_rows("chat-1", cache_friendly=True)

        self.assertEqual([row["rowid"] for row in selected], list(range(11, 111)))
        message_list.assert_called_once_with(
            "chat-1", limit=main.CACHE_HISTORY_MAX_MESSAGES + 1
        )
        setting_set.assert_called_once_with(
            "prompt_cache_history_start:chat-1", "11"
        )

        grown_rows = initial_rows + [
            {"rowid": rowid, "role": "assistant", "content": str(rowid)}
            for rowid in range(111, 116)
        ]
        with (
            patch.object(main.db, "message_list", return_value=grown_rows),
            patch.object(main.db, "setting_get", return_value="11"),
            patch.object(main.db, "setting_set") as setting_set,
        ):
            grown = main._chat_history_rows("chat-1", cache_friendly=True)

        self.assertEqual(grown[0]["rowid"], 11)
        self.assertEqual(grown[-1]["rowid"], 115)
        setting_set.assert_not_called()

    def test_cache_history_window_rotates_only_at_the_hard_limit(self):
        rows = [
            {"rowid": rowid, "role": "user", "content": str(rowid)}
            for rowid in range(11, 172)
        ]
        with (
            patch.object(main.db, "message_list", return_value=rows),
            patch.object(main.db, "setting_get", return_value="11"),
            patch.object(main.db, "setting_set") as setting_set,
        ):
            selected = main._chat_history_rows("chat-1", cache_friendly=True)

        self.assertEqual(len(selected), main.CACHE_HISTORY_TARGET_MESSAGES)
        self.assertEqual(selected[0]["rowid"], 72)
        setting_set.assert_called_once_with(
            "prompt_cache_history_start:chat-1", "72"
        )


if __name__ == "__main__":
    unittest.main()
