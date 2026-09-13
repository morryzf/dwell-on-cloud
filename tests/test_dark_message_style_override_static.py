from pathlib import Path

INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_explicit_dark_theme_uses_saved_message_variables_at_matching_specificity():
    assert 'html[data-theme="dark"] #log .bubble {' in INDEX
    assert 'html[data-theme="dark"] #log .gu {' in INDEX


def test_automatic_dark_theme_uses_saved_message_variables_after_legacy_rules():
    marker = '@media (prefers-color-scheme: dark) {\n    html:not([data-theme="light"]) #log .bubble {'
    assert marker in INDEX
    final_block = INDEX.rsplit(marker, 1)[1]
    assert "background: var(--msg-me-bg" in final_block
    assert "background: var(--msg-cloudy-bg" in final_block
