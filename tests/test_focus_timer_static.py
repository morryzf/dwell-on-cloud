from pathlib import Path
import unittest


class FocusTimerStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.page = (root / "static" / "index.html").read_text(encoding="utf-8")
        cls.component = (
            root / "static" / "vendor" / "guided-access-pomodoro.js"
        ).read_text(encoding="utf-8")

    def test_focus_page_and_component_are_connected(self):
        self.assertIn('id="navFocus"', self.page)
        self.assertIn('id="focusSheet"', self.page)
        self.assertIn('src="/vendor/guided-access-pomodoro.js"', self.page)
        self.assertIn("focusApi.configure({ key: 'dwell.focus.pomodoro.v1'", self.page)

    def test_cloudy_sharing_is_opt_in_and_structured(self):
        self.assertIn("localStorage.getItem(FOCUS_SHARE_KEY) === '1'", self.page)
        self.assertIn("if (focusContext) payload.focus_context = focusContext", self.page)
        self.assertNotIn("payload.text += focusApi.prompt()", self.page)

    def test_dwell_adapter_apis_are_exported(self):
        self.assertIn("setDurations:setDurations", self.component)
        self.assertIn("setSingleAppMode:setSingleAppMode", self.component)
        self.assertIn("Source by NYRA", self.component)


if __name__ == "__main__":
    unittest.main()
