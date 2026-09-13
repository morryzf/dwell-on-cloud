import os
import unittest
import uuid

from app import db


class SystemLogDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"system-logs-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_start_finish_filter_and_clear(self):
        log_id = db.system_log_start(
            "model_request",
            "chat_reply",
            chat_id="chat-1",
            message_id="message-1",
            provider="Example",
            model_id="model-x",
        )
        running = db.system_log_list(status="running")
        self.assertEqual([item["id"] for item in running], [log_id])

        db.system_log_finish(log_id, "success", 123, status_code=200)
        finished = db.system_log_list(category="model_request")
        self.assertEqual(finished[0]["status"], "success")
        self.assertEqual(finished[0]["duration_ms"], 123)
        self.assertEqual(finished[0]["status_code"], 200)
        self.assertNotIn("content", finished[0])
        self.assertNotIn("headers", finished[0])

        self.assertEqual(db.system_log_clear(), 1)
        self.assertEqual(db.system_log_list(), [])


if __name__ == "__main__":
    unittest.main()
