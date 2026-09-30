import tempfile
import time
import unittest
from pathlib import Path

from app import db
from app.main import _memory_card_segmented_through


def insert_card(chat_id: str, content: str, start: int, end: int) -> None:
    now = int(time.time())
    with db.conn() as cx:
        cx.execute(
            """INSERT INTO memory_cards (id,chat_id,content,memory_type,source_start_rowid,
               source_end_rowid,made,updated) VALUES (?,?,?,?,?,?,?,?)""",
            (db.new_id(), chat_id, content, "preference", start, end, now, now),
        )


def insert_draft(chat_id: str, content: str) -> None:
    with db.conn() as cx:
        cx.execute(
            "INSERT INTO memory_card_drafts (id,chat_id,content,memory_type,made) VALUES (?,?,?,?,?)",
            (db.new_id(), chat_id, content, "preference", int(time.time())),
        )


class BranchMemoryTest(unittest.TestCase):
    """分支要接着原聊天的记忆走：不能把整段旧历史重切一遍、再出一整批重复的卡，也要带上原来的卡。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "branch.db")
        db.init_db()
        # 先垫一个别的聊天，让分支里的 rowid 和原聊天对不上，才测得出映射。
        other = db.chat_add("别的")
        for i in range(7):
            db.message_add(other["id"], "user", f"垫 {i}")
        self.source = db.chat_add("原聊天")
        self.messages = [db.message_add(self.source["id"], "user" if i % 2 == 0 else "assistant", f"第 {i} 条")
                         for i in range(10)]
        with db.conn() as cx:
            self.rowids = [cx.execute("SELECT rowid FROM messages WHERE id=?", (m["id"],)).fetchone()[0]
                           for m in self.messages]
        # 两段已经整理过；第三段跨过分支点（第 6 条）。
        self.seg_a = db.chat_memory_add_segment(self.source["id"], self.rowids[0], self.rowids[2], "段 A")
        self.seg_b = db.chat_memory_add_segment(self.source["id"], self.rowids[3], self.rowids[5], "段 B")
        self.seg_c = db.chat_memory_add_segment(self.source["id"], self.rowids[6], self.rowids[8], "段 C")
        for segment in (self.seg_a, self.seg_b, self.seg_c):
            db.memory_card_segment_mark(self.source["id"], segment["id"], 2)
        with db.conn() as cx:
            cx.execute(
                """INSERT INTO chat_memory_state (chat_id,enabled,overview,through_rowid,status,error,generated_at)
                   VALUES (?,1,'原来的摘要',?,'running','',123)""",
                (self.source["id"], self.rowids[5]),
            )

    def tearDown(self):
        db.DB_PATH = self.previous_path
        self.tmp.cleanup()

    def branch_at(self, index: int) -> str:
        return db.chat_branch_from_message(self.source["id"], self.messages[index]["id"])["id"]

    def branch_rowids(self, branch_id: str) -> list[int]:
        with db.conn() as cx:
            return [row[0] for row in cx.execute(
                "SELECT rowid FROM messages WHERE chat_id=? ORDER BY rowid", (branch_id,))]

    def test_processed_segments_come_along_so_nothing_is_regenerated(self):
        branch = self.branch_at(6)
        new_rowids = self.branch_rowids(branch)

        segments = db.chat_memory_segments(branch)
        self.assertEqual([s["content"] for s in segments], ["段 A", "段 B"])   # 段 C 跨过分支点，不搬
        self.assertEqual((segments[0]["start_rowid"], segments[0]["end_rowid"]), (new_rowids[0], new_rowids[2]))
        self.assertEqual((segments[1]["start_rowid"], segments[1]["end_rowid"]), (new_rowids[3], new_rowids[5]))
        self.assertEqual(db.memory_card_unprocessed_segments(branch), [])
        # 自动出卡从分支里的第 6 条之后接着切，不从头来。
        self.assertEqual(_memory_card_segmented_through(branch), new_rowids[5])

    def test_summary_state_follows_with_the_new_rowids(self):
        branch = self.branch_at(6)
        state = db.chat_memory_get(branch)

        self.assertEqual(state["overview"], "原来的摘要")
        self.assertEqual(state["through_rowid"], self.branch_rowids(branch)[5])
        self.assertNotIn(state["status"], ("queued", "running"))

    def test_injection_switch_follows_the_source_chat(self):
        db.memory_card_injection_set(self.source["id"], False)
        branch = self.branch_at(6)

        self.assertFalse(db.memory_card_injection_enabled(branch))

    def test_the_source_cards_come_along_with_new_rowids(self):
        insert_card(self.source["id"], "原聊天里的卡", self.rowids[0], self.rowids[2])
        branch = self.branch_at(6)

        cards = db.memory_card_list(branch)
        self.assertEqual([c["content"] for c in cards], ["原聊天里的卡"])
        self.assertEqual(cards[0]["source_start_rowid"], self.branch_rowids(branch)[0])
        # 原聊天自己的那张不受影响
        self.assertEqual(len(db.memory_card_list(self.source["id"])), 1)

    def test_discard_all_empties_the_review_list(self):
        for i in range(3):
            insert_draft(self.source["id"], f"候选 {i}")

        self.assertEqual(db.memory_card_drafts_discard_all(self.source["id"]), 3)
        self.assertEqual(db.memory_card_draft_list(self.source["id"]), [])


if __name__ == "__main__":
    unittest.main()
