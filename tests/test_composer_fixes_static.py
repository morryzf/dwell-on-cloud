import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


class CacheGlowOnlyForFiveMinutesTest(unittest.TestCase):
    """那圈光画的就是 5 分钟那条线，选 1h 或关掉缓存时它没有意义。"""

    def test_gate_exists_and_reads_the_chat_setting(self):
        self.assertIn("function cacheGlowApplies()", HTML)
        self.assertIn("return curPromptCacheTtl === '5m';", HTML)

    def test_every_entry_point_is_gated(self):
        start = HTML.index("function cacheGlowStart()")
        self.assertIn("if (!cacheGlowApplies())", HTML[start:start + 200])

        restore = HTML.index("function cacheGlowSetChat(")
        self.assertIn("if (!cacheGlowApplies())", HTML[restore:restore + 300])

    def test_switching_away_from_five_minutes_clears_a_running_countdown(self):
        switch = HTML.index("async function setChatPromptCacheTtl(")
        section = HTML[switch:switch + 800]
        self.assertIn("if (!cacheGlowApplies()) cacheGlowStop(true, false);", section)


class BorderWidthStepTest(unittest.TestCase):
    def test_border_width_is_adjustable_in_tenths(self):
        self.assertIn(
            "{ key: 'borderWidth', label: '边框宽度', type: 'range', min: 0, max: 3, step: .1 }",
            HTML,
        )

    def test_the_readout_still_shows_one_decimal(self):
        self.assertIn("if (key === 'borderWidth') return Number(value).toFixed(1) + ' px';", HTML)


class ComposerPaddingFollowsItsHeightTest(unittest.TestCase):
    """输入卡撑高之后留白要跟上，否则最后一条会被玻璃压住。"""

    def test_footer_height_is_observed(self):
        self.assertIn(").observe(footerEl);", HTML)


if __name__ == "__main__":
    unittest.main()
