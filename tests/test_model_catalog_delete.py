import tempfile
import unittest
from pathlib import Path

from app import db


class ModelCatalogDeleteTest(unittest.TestCase):
    """取消常用只清标记，行还留着——手动加错的名字得真能删掉。"""

    @classmethod
    def setUpClass(cls):
        # db 在别的测试里可能已经导入过了，环境变量这时改不动它，直接换路径。
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous_path = db.DB_PATH
        db.DB_PATH = str(Path(cls.tmp.name) / "catalog.db")
        db.init_db()
        cls.db = db
        # 供应商名字有唯一约束，整个类共用一个。
        cls.provider = db.provider_upsert(
            "", "测试供应商", "https://example.test/v1", "", True,
        )

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls.previous_path
        cls.tmp.cleanup()

    def test_deleting_removes_the_row_entirely(self):
        pid = self.provider["id"]
        self.db.provider_model_upsert(pid, "打错的名字", favorite=True, manual=True)
        self.assertIn(
            "打错的名字",
            [item["model_id"] for item in self.db.provider_model_list(pid)],
        )

        self.assertTrue(self.db.provider_model_delete(pid, "打错的名字"))

        self.assertNotIn(
            "打错的名字",
            [item["model_id"] for item in self.db.provider_model_list(pid)],
        )

    def test_unfavoriting_alone_leaves_it_in_the_catalog(self):
        # 这正是「删不掉」的由来：取消常用之后它还在目录里。
        pid = self.provider["id"]
        self.db.provider_model_upsert(pid, "留着的", favorite=True, manual=True)
        self.db.provider_model_upsert(pid, "留着的", favorite=False)

        rows = {item["model_id"]: item for item in self.db.provider_model_list(pid)}
        self.assertIn("留着的", rows)
        self.assertFalse(rows["留着的"]["favorite"])

    def test_deleting_something_that_is_not_there_reports_it(self):
        self.assertFalse(self.db.provider_model_delete(self.provider["id"], "没有这个"))

    def test_deleting_one_leaves_the_others_alone(self):
        pid = self.provider["id"]
        self.db.provider_model_upsert(pid, "留下", favorite=True, manual=True)
        self.db.provider_model_upsert(pid, "删掉", favorite=True, manual=True)

        self.db.provider_model_delete(pid, "删掉")

        names = [item["model_id"] for item in self.db.provider_model_list(pid)]
        self.assertIn("留下", names)
        self.assertNotIn("删掉", names)

    def test_a_fetched_model_comes_back_on_refresh(self):
        # 抓来的删掉只是眼前清净，下次刷新会自己回来——提示语得跟这个行为一致。
        pid = self.provider["id"]
        self.db.provider_models_refresh(pid, ["抓来的"])
        self.db.provider_model_delete(pid, "抓来的")
        self.assertNotIn(
            "抓来的", [item["model_id"] for item in self.db.provider_model_list(pid)]
        )

        self.db.provider_models_refresh(pid, ["抓来的"])

        self.assertIn(
            "抓来的", [item["model_id"] for item in self.db.provider_model_list(pid)]
        )


if __name__ == "__main__":
    unittest.main()
