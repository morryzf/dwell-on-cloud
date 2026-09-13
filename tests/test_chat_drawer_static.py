import unittest
from pathlib import Path


class ChatDrawerStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_old_full_page_chat_list_is_removed(self):
        self.assertNotIn('id="chatsSheet"', self.html)
        self.assertNotIn("sheets.chats", self.html)
        self.assertNotIn('id="newSession"', self.html)

    def test_assistant_switcher_sits_below_brand_and_above_navigation(self):
        brand = self.html.index('class="brand"')
        assistants = self.html.index('class="assistant-switch"')
        navigation = self.html.index('class="nav"')
        recents = self.html.index('id="drawerChatHeading"')
        history = self.html.index('id="drawerChats"')
        self.assertLess(brand, assistants)
        self.assertLess(assistants, navigation)
        self.assertLess(navigation, recents)
        self.assertLess(recents, history)
        self.assertIn('id="recGu" aria-pressed="true"', self.html)
        self.assertIn('id="recGong" aria-pressed="false"', self.html)
        self.assertNotIn('<div class="sect">Assistants</div>', self.html)
        self.assertNotIn('class="assistant-avatar"', self.html)

    def test_drawer_uses_uniform_more_translucent_surfaces(self):
        self.assertIn(
            "background: rgba(247, 236, 241, .51) !important;",
            self.html,
        )
        self.assertEqual(
            self.html.count("background: rgba(68,61,66,.60) !important;"),
            2,
        )
        self.assertNotIn("#drawer::before", self.html)

    def test_redundant_chat_navigation_item_is_removed(self):
        self.assertNotIn('id="navChat"', self.html)

    def test_drawer_navigation_and_brand_are_compact(self):
        self.assertIn(
            'padding: 10px 14px; border-radius: 12px; font-size: 14px;',
            self.html,
        )
        self.assertIn('.nav .item .ic { width: 16px; height: 16px; }', self.html)
        self.assertIn('family=Pinyon+Script', self.html)
        self.assertIn('font-family: "Pinyon Script", cursive;', self.html)
        self.assertIn('<div class="brand">Cloudy studio</div>', self.html)

    def test_brand_and_theme_control_live_in_the_drawer(self):
        self.assertIn('family=Ephesis', self.html)
        self.assertIn('font-family: "Ephesis", cursive;', self.html)
        self.assertIn('text-align: center; color: #5A4454;', self.html)
        self.assertIn('id="drawerThemeBtn"', self.html)
        self.assertNotIn('id="themeRow"', self.html)
        self.assertIn('drawerThemeBtn.dataset.i = dark ? "moon" : "sun"', self.html)

    def test_dark_recent_chats_stay_borderless(self):
        selector = (
            'html[data-theme="dark"] #drawer '
            ".drawer-chat-list .chatrow .crow"
        )
        self.assertIn(selector, self.html)
        self.assertIn("background-image: none !important;", self.html)
        self.assertIn("border-color: transparent !important;", self.html)

    def test_drawer_history_keeps_chat_actions(self):
        self.assertIn("rename.onclick = () => renameChat(it)", self.html)
        self.assertIn("del.onclick = (e) => deleteChat(it, e)", self.html)
        self.assertIn("bindChatRowSwipe(row, b)", self.html)
        self.assertIn('id="drawerNewChat"', self.html)
        self.assertIn('id="drawerChatScope"', self.html)

    def test_chat_swipe_does_not_compete_with_drawer_dismissal(self):
        self.assertIn(
            "'.nav button, .drawer-foot, .drawer-chat-list'",
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
