import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


class ProviderDeleteButtonTest(unittest.TestCase):
    """后端一直有删除接口，缺的只是一个按钮。"""

    def test_delete_sits_next_to_save_only_when_editing(self):
        editor = HTML.index("function openProviderEditorPage(")
        section = HTML[editor:editor + 3000]
        self.assertIn("editingProviderId\n        ? '<div class=\"ap-btns\">", section)
        self.assertIn('id="apDelete"', section)
        # 第一次添加还没有东西可删，只给保存。
        self.assertIn("'<div class=\"ap-btns single\"><button class=\"ap-b go\" id=\"apSave\">", section)

    def test_it_calls_the_existing_endpoint_and_returns_to_the_list(self):
        handler = HTML.index("const deleteButton = document.getElementById('apDelete');")
        section = HTML[handler:handler + 900]
        self.assertIn("method: 'DELETE',", section)
        self.assertIn("'api/providers/' + encodeURIComponent(editingProviderId)", section)
        self.assertIn("loadApi();", section)

    def test_the_in_use_refusal_is_shown_rather_than_swallowed(self):
        handler = HTML.index("const deleteButton = document.getElementById('apDelete');")
        section = HTML[handler:handler + 900]
        self.assertIn("data.detail", section)
        self.assertIn("say('删不掉：' + e.message, true);", section)
        # 后端这条保护是这个提示的来源，别把它改没了。
        self.assertIn("仍有聊天正在使用这个供应商", MAIN)


class ProviderRowDensityTest(unittest.TestCase):
    def test_rows_are_compact_without_touching_other_settings_lists(self):
        self.assertIn(
            ".group .provider-profile-row { padding: 10px 16px; font-size: 14.5px; gap: 10px; }",
            HTML,
        )
        # 通用行距保持原样，否则整个设置页都会跟着变。
        self.assertIn("padding: 15px 18px; font-size: 16px; text-align: left;", HTML)


if __name__ == "__main__":
    unittest.main()
