from pathlib import Path

HTML = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def test_glass_effect_settings_cover_messages_and_interface_surfaces():
    assert '>玻璃效果<' in HTML
    assert '消息 / 界面' in HTML
    assert "const INTERFACE_GLASS_SURFACES" in HTML
    assert "{ key: 'composer', label: '输入框' }" in HTML
    assert "{ key: 'drawer', label: '左侧边栏' }" in HTML
    assert "{ key: 'ttsPlayer', label: '语音浮窗' }" in HTML


def test_interface_glass_controls_persist_separate_light_and_dark_values():
    assert "const INTERFACE_GLASS_KEY = 'dwellInterfaceGlass'" in HTML
    assert "const out = { light: {}, dark: {} }" in HTML
    assert "localStorage.setItem(INTERFACE_GLASS_KEY" in HTML
    assert "MESSAGE_STYLE_MODES.forEach(mode =>" in HTML


def test_interface_glass_values_drive_the_live_surfaces():
    assert "background: var(--interface-composer-bg) !important" in HTML
    assert "blur(var(--interface-composer-blur))" in HTML
    assert "background: var(--interface-drawer-bg) !important" in HTML
    assert "blur(var(--interface-drawer-blur))" in HTML
    assert "background: var(--interface-tts-bg)" in HTML
    assert "blur(var(--interface-tts-blur))" in HTML


def test_message_actions_use_requested_size_spacing_and_order():
    assert "gap: 12px; max-width: calc(100vw - 36px)" in HTML
    assert "button.appendChild(icEl(icon, 18));" in HTML
    assert "more.appendChild(icEl('dots', 18));" in HTML
    edit_pos = HTML.index("addAction('pen', '编辑消息'")
    voice_pos = HTML.index("addAction('volume', '播放整轮回复'")
    regenerate_pos = HTML.index("addAction('refresh', '重新生成'")
    assert edit_pos < voice_pos < regenerate_pos


def test_glass_preview_opens_with_the_effective_theme():
    open_handler = HTML[
        HTML.index("document.getElementById('messageStyleRow').onclick"):
        HTML.index("applyMessageStyles();", HTML.index("document.getElementById('messageStyleRow').onclick"))
    ]
    assert "messageStyleTab = effectiveMessageStyleMode();" in open_handler
    assert "if (sheets.messageStyle.classList.contains('open'))" in HTML
