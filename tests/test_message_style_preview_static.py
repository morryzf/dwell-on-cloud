from pathlib import Path

INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_dark_glass_preview_matches_dark_chat_surface():
    assert '.msg-style-preview[data-mode="dark"] {' in INDEX
    assert "--msg-preview-base: #393538;" in INDEX
    assert "rgba(238,209,220,.18)" in INDEX
    assert "rgba(231,167,191,.09)" in INDEX
    assert "background-size: 19px 19px, 100% 100%;" in INDEX


def test_preview_uses_custom_chat_background_when_configured():
    assert "html.has-chat-background .msg-style-preview {" in INDEX
    assert "var(--chat-bg-image);" in INDEX
    assert "linear-gradient(145deg, #eee8eb, #d8cbd1)" not in INDEX
