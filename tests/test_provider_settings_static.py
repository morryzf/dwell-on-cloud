from pathlib import Path
import unittest


class ProviderSettingsStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.ui = (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_saved_providers_are_name_only_rows_with_a_separate_add_action(self):
        self.assertIn('id="apProviderList"', self.ui)
        self.assertIn('id="apAddProvider"', self.ui)
        self.assertIn("row.onclick = () => openProviderEditorPage(p);", self.ui)
        self.assertIn("name.textContent = p.name;", self.ui)
        self.assertNotIn('id="apProfile"', self.ui)
        self.assertNotIn("＋ 新供应商</option>", self.ui)

    def test_provider_configuration_uses_a_dedicated_page(self):
        self.assertIn('id="apiBack"', self.ui)
        self.assertIn('id="apiTitle"', self.ui)
        self.assertIn("function openProviderEditorPage(provider)", self.ui)
        self.assertIn("setApiHeader(p ? p.name : '添加供应商', true);", self.ui)
        self.assertIn(
            "document.getElementById('apAddProvider').onclick = () => "
            "openProviderEditorPage(null);",
            self.ui,
        )
        self.assertIn(
            "back.appendChild(icEl(backToList ? 'chevL' : 'x', 16));",
            self.ui,
        )
        self.assertNotIn('id="apEditor" hidden', self.ui)

    def test_provider_editor_does_not_summon_the_keyboard(self):
        self.assertNotIn("document.getElementById('apName').focus", self.ui)
        self.assertNotIn("editor.scrollIntoView", self.ui)

    def test_editing_without_a_new_token_preserves_the_saved_key(self):
        self.assertIn("if (nextToken || !editingProviderId) value.token = nextToken;", self.ui)

    def test_settings_catalog_does_not_expose_chat_model_switching(self):
        self.assertIn("openProviderModelPicker('browse', 'settings')", self.ui)
        self.assertIn("const settingsCatalog = catalogOrigin === 'settings';", self.ui)
        self.assertIn("if (!settingsCatalog) {", self.ui)
        self.assertIn("if (!settingsCatalog && catalogMode === 'favorites') {", self.ui)
        self.assertIn("modelBack.appendChild(icEl(settingsCatalog ? 'chevL' : 'x', 16));", self.ui)
        self.assertIn("openProviderModelPicker('favorites', 'chat')", self.ui)

    def test_model_catalog_description_is_visually_secondary(self):
        self.assertIn(
            ".ap-section-description { margin: 2px 0 0; color: var(--dim); "
            "font-size: 12.5px; line-height: 1.55; }",
            self.ui,
        )

    def test_eight_drawer_tools_use_the_larger_label_size(self):
        self.assertEqual(self.ui.count('class="item drawer-tool"'), 8)
        self.assertIn(
            '#drawer .nav .drawer-tool-grid .drawer-tool {',
            self.ui,
        )
        drawer_rule = self.ui.split(
            '#drawer .nav .drawer-tool-grid .drawer-tool {', 1
        )[1].split('}', 1)[0]
        self.assertIn('font-size: 20px;', drawer_rule)


if __name__ == "__main__":
    unittest.main()
