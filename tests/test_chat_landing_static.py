from pathlib import Path
import unittest


class ChatLandingStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.page = (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_history_load_keeps_holding_the_bottom(self):
        # 进门、每轮回复完重摆历史都走 loadSaid，摆完要扶着底一会儿
        self.assertIn("function lockToBottom(ms = 1200)", self.page)
        self.assertIn("function releaseBottomLock()", self.page)
        self.assertIn("    scroll(true);\n    lockToBottom();", self.page)
        # 手一碰就松开，不跟用户抢滚动条
        self.assertIn("['wheel', 'touchstart', 'mousedown'].forEach(type => {", self.page)
        self.assertIn("log.addEventListener(type, releaseBottomLock, { passive: true });", self.page)
        self.assertIn("document.addEventListener('keydown', releaseBottomLock, { passive: true });", self.page)

    def test_jumping_to_a_searched_line_is_not_dragged_back_down(self):
        jump = self.page.split("await loadSaid(hit.message_id);")[1].split("poll();")[0]
        self.assertIn("releaseBottomLock();", jump)
        self.assertLess(jump.index("releaseBottomLock();"), jump.index("scrollIntoView({ block: 'center' })"))

    def test_scroll_to_latest_button(self):
        self.assertIn('<button id="scrollBottom" aria-label="回到最新">', self.page)
        self.assertIn("function updateScrollBtn() { scrollBottomBtn.classList.toggle('show', !atBottom()); }", self.page)
        self.assertIn("log.addEventListener('scroll', updateScrollBtn, { passive: true });", self.page)


if __name__ == "__main__":
    unittest.main()
