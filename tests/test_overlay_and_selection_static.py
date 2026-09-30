from pathlib import Path
import unittest


class OverlayAndSelectionStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.page = (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_drawer_gesture_stops_at_the_layer_above_it(self):
        self.assertIn("function drawerCovered()", self.page)
        for blocker in (
            ".sheetWrap.open",
            ".image-viewer.open",
            ".menuCard.open",
            "#ttsChoicePicker.on",
            ".cbk-full",
            "#lockWrap:not(.gone)",
        ):
            self.assertIn(blocker, self.page)
        # 手势开始、手势进行中都要看一眼上面盖着什么
        self.assertIn("if (drawerCovered()) { dg = null; return; }", self.page)
        self.assertIn("if (drawerCovered()) { if (dg.live) drawerRelease(0); dg = null; return; }", self.page)

    def test_selecting_messages_keeps_the_view_where_it_was(self):
        self.assertIn("function keepMessageInPlace(anchorRow, mutate)", self.page)
        self.assertIn("keepMessageInPlace(anchorRow, () => {", self.page)
        self.assertIn("setMessageSelectionMode(true, rowEl)", self.page)
        # 选中那条自己当锚点，不是重新滚到某个默认位置
        self.assertIn("log.scrollTop += row.getBoundingClientRect().top - before", self.page)
        self.assertNotIn("close(); setMessageSelectionMode(true);", self.page)


if __name__ == "__main__":
    unittest.main()
