import os
import unittest
import uuid

from app import db


class MemoryCardDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"memory-cards-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()
        self.chat = db.chat_add("test")
        db.message_add(self.chat["id"], "user", "我搬到上海了")
        db.message_add(self.chat["id"], "assistant", "我会记得")
        self.segment = db.chat_memory_add_segment(
            self.chat["id"], 1, 2, "她搬到了上海。"
        )

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_draft_accept_edit_and_archive_flow(self):
        proposal = {
            "content": "她现在住在上海。",
            "memory_type": "stable_fact",
            "topics": ["place", "daily_life"],
            "importance": "normal",
            "retention": "long_term",
            "valid_until": None,
            "source_segment_id": self.segment["id"],
        }
        self.assertEqual(db.memory_card_stage(self.chat["id"], [proposal]), 1)
        self.assertEqual(db.memory_card_stage(self.chat["id"], [proposal]), 0)

        draft = db.memory_card_draft_list(self.chat["id"])[0]
        self.assertEqual(draft["content"], "她现在住在上海。")
        chosen = {**draft, "importance": "high"}
        card = db.memory_card_draft_accept(self.chat["id"], draft["id"], chosen)

        self.assertEqual(card["topics"], ["place", "daily_life"])
        self.assertEqual(card["source_start_rowid"], 1)
        self.assertEqual(card["source_end_rowid"], 2)
        self.assertEqual(card["importance"], "high")

        updated = db.memory_card_update(
            self.chat["id"], card["id"], {**card, "status": "hidden"}
        )
        self.assertEqual(updated["status"], "hidden")
        self.assertTrue(db.memory_card_archive(self.chat["id"], card["id"]))
        self.assertEqual(db.memory_card_list(self.chat["id"]), [])
        self.assertEqual(
            len(db.memory_card_list(self.chat["id"], include_archived=True)), 1
        )

    def test_records_the_exact_cards_used_for_a_response(self):
        proposal = {
            "content": "她现在住在上海。",
            "memory_type": "stable_fact",
            "topics": ["place"],
            "importance": "normal",
            "retention": "long_term",
            "valid_until": None,
            "source_segment_id": self.segment["id"],
        }
        db.memory_card_stage(self.chat["id"], [proposal])
        draft = db.memory_card_draft_list(self.chat["id"])[0]
        card = db.memory_card_draft_accept(self.chat["id"], draft["id"], draft)
        response = db.message_add(self.chat["id"], "assistant", "")

        db.memory_card_usage_record(
            self.chat["id"], response["id"], "上海天气", [{**card, "selection_score": 3.5}]
        )
        # Later edits do not rewrite what the model saw in the earlier turn.
        db.memory_card_update(
            self.chat["id"], card["id"], {**card, "content": "她已经搬家。"}
        )

        used = db.memory_card_last_injection(self.chat["id"])
        self.assertEqual(used["response_message_id"], response["id"])
        self.assertEqual(used["items"][0]["content"], "她现在住在上海。")
        self.assertEqual(used["items"][0]["topics"], ["place"])

    def test_memory_card_injection_can_be_disabled_per_chat(self):
        self.assertTrue(db.memory_card_injection_enabled(self.chat["id"]))
        db.memory_card_injection_set(self.chat["id"], False)
        self.assertFalse(db.memory_card_injection_enabled(self.chat["id"]))

    def test_each_segment_is_only_offered_for_generation_once(self):
        self.assertEqual(db.memory_card_unprocessed_segments(self.chat["id"]), [self.segment])
        db.memory_card_segment_mark(self.chat["id"], self.segment["id"], 0)
        self.assertEqual(db.memory_card_unprocessed_segments(self.chat["id"]), [])

    def test_duplicate_segment_range_is_only_offered_once(self):
        duplicate = db.chat_memory_add_segment(
            self.chat["id"], 1, 2, "她现在已经住在上海。"
        )
        db.memory_card_segment_mark(self.chat["id"], self.segment["id"], 1)

        self.assertNotEqual(duplicate["id"], self.segment["id"])
        self.assertEqual(db.memory_card_unprocessed_segments(self.chat["id"]), [])

    def test_removes_forced_prefix_once_without_rewriting_future_edits(self):
        proposal = {
            "content": "我记得：她喜欢秋天散步。",
            "memory_type": "preference",
            "topics": ["daily_life"],
            "importance": "normal",
            "retention": "long_term",
            "valid_until": None,
            "source_segment_id": self.segment["id"],
        }
        db.memory_card_stage(self.chat["id"], [proposal])
        draft = db.memory_card_draft_list(self.chat["id"])[0]
        card = db.memory_card_draft_accept(self.chat["id"], draft["id"], draft)
        with db.conn() as cx:
            cx.execute("DELETE FROM settings WHERE key='memory_remove_forced_prefix_v1'")

        db.init_db()
        cleaned = db.memory_card_get(self.chat["id"], card["id"])
        self.assertEqual(cleaned["content"], "她喜欢秋天散步。")

        edited = db.memory_card_update(
            self.chat["id"], card["id"], {**cleaned, "content": "我记得：这是我主动写的。"}
        )
        db.init_db()
        self.assertEqual(
            db.memory_card_get(self.chat["id"], card["id"])["content"],
            edited["content"],
        )


if __name__ == "__main__":
    unittest.main()

