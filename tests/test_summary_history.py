import os
import unittest
import uuid

from app import db


class SummaryHistoryRetentionTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"summary-history-test-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()
        self.chat = db.chat_add("summary history test")

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            path = self.path + suffix
            if os.path.exists(path):
                os.remove(path)

    def test_only_the_latest_ten_summary_versions_are_retained(self):
        for index in range(13):
            db.chat_memory_save_overview(self.chat["id"], f"版本 {index}")

        versions = db.chat_memory_versions(self.chat["id"])

        self.assertEqual(len(versions), 10)
        self.assertEqual(versions[0]["overview"], "版本 11")
        self.assertEqual(versions[-1]["overview"], "版本 2")
        self.assertEqual(db.chat_memory_get(self.chat["id"])["version_count"], 10)


if __name__ == "__main__":
    unittest.main()
