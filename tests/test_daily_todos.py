"""「每天」的待办，新的一天要自己把勾清掉。

一天从早 6 点算起：凌晨两点对人来说还是「今天」，勾不该在她还醒着的时候
自己弹回去。和提醒用的是同一个起点。
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
import unittest
import uuid

from app import db
from app import main


HTML = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


class DailyTodoResetTest(unittest.TestCase):
    def setUp(self):
        self.previous_path = db.DB_PATH
        self.path = os.path.abspath(f"daily-todo-{uuid.uuid4().hex}.sqlite3")
        db.DB_PATH = self.path
        db.init_db()
        self.noon = datetime(2026, 9, 25, 12, 0, tzinfo=db.CN_TZ)

    def tearDown(self):
        db.DB_PATH = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def _hers(self, now=None):
        rows = db.todos_all(now or self.noon)["hers"]
        return {item["text"]: item["done"] for item in rows}

    def test_day_starts_at_six(self):
        self.assertEqual(db.DAY_START_HOUR, 6)
        late = datetime(2026, 9, 26, 2, 0, tzinfo=db.CN_TZ)
        self.assertEqual(db.day_key(late), "2026-09-25", "凌晨两点还算前一天")
        self.assertEqual(db.day_key(late.replace(hour=6)), "2026-09-26")

    def test_daily_todo_comes_back_tomorrow(self):
        pill = db.todo_add("hers", "吃药（舍曲林）", at="13:00", fixed=True)
        db.todos_reset_fixed_for_new_day(self.noon)
        db.todo_toggle("hers", pill["id"])
        self.assertTrue(self._hers()["吃药（舍曲林）"], "刚勾上，今天该还是勾着的")

        tomorrow = self.noon + timedelta(days=1)
        db.todos_reset_fixed_for_new_day(tomorrow)
        self.assertFalse(self._hers(tomorrow)["吃药（舍曲林）"], "第二天该自己恢复")

    def test_still_checked_while_she_is_awake_past_midnight(self):
        pill = db.todo_add("hers", "吃药", fixed=True)
        db.todos_reset_fixed_for_new_day(self.noon)
        db.todo_toggle("hers", pill["id"])
        late = self.noon.replace(day=26, hour=2)
        db.todos_reset_fixed_for_new_day(late)
        self.assertTrue(self._hers(late)["吃药"], "凌晨两点不该当场把勾弹回去")

    def test_one_off_todos_are_left_alone(self):
        once = db.todo_add("hers", "剪头发", at="15:00")
        db.todos_reset_fixed_for_new_day(self.noon)
        db.todo_toggle("hers", once["id"])
        tomorrow = self.noon + timedelta(days=1)
        db.todos_reset_fixed_for_new_day(tomorrow)
        self.assertTrue(self._hers(tomorrow)["剪头发"], "一次性的做完就做完了，不该复活")

    def test_reading_the_list_is_enough_to_trigger_it(self):
        pill = db.todo_add("hers", "吃药", fixed=True)
        db.todo_toggle("hers", pill["id"])
        db.setting_set(db.TODOS_FIXED_RESET_KEY, "2020-01-01")
        self.assertFalse(self._hers()["吃药"], "读一次待办就该顺手重置，不用定时任务")

    def test_reset_happens_once_a_day(self):
        pill = db.todo_add("hers", "吃药", fixed=True)
        self.assertTrue(db.todos_reset_fixed_for_new_day(self.noon))
        db.todo_toggle("hers", pill["id"])
        self.assertFalse(db.todos_reset_fixed_for_new_day(self.noon),
                         "同一天不该重置第二次")
        self.assertTrue(self._hers()["吃药"], "她今天勾的不能被同一天的第二次重置抹掉")


class TodoTimeFieldTest(unittest.TestCase):
    """空的 time 输入框什么都不显，在手机上就是个莫名其妙的空药片。"""

    def test_empty_time_field_reads_as_a_label(self):
        self.assertIn(".hadd .tmwrap::before {", HTML)
        self.assertIn("content: '时间';", HTML)
        self.assertIn(".hadd .tmwrap.set::before { content: none; }", HTML)
        self.assertIn(".hadd .tmwrap:not(.set) input[type=time] { color: transparent; }", HTML)

    def test_it_is_the_same_pill_as_the_daily_chip(self):
        # 药片的长相全在壳上，里头的 input 脱得只剩文字，
        # 再用一个看不见的中文字把高度撑到和「每天」一样。
        self.assertIn("border-radius: 999px; padding: 7px 14px; font-size: 12.5px;", HTML)
        self.assertIn("content: '时'; width: 0; overflow: hidden; visibility: hidden;", HTML)
        self.assertIn("height: 1.7em;", HTML)
        self.assertIn("border: 0; padding: 0; margin: 0; background: transparent;", HTML)

    def test_the_label_follows_the_value(self):
        self.assertIn("const tmSync = () => tmWrap.classList.toggle('set', !!tm.value);", HTML)
        self.assertIn("tm.addEventListener('change', tmSync);", HTML)
        self.assertIn("tm.value = ''; tmSync();", HTML, "记上之后要跟着复位")
        self.assertIn("row.append(tmWrap, daily, go);", HTML)


if __name__ == "__main__":
    unittest.main()
