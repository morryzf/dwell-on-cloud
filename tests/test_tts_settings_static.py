from pathlib import Path

HTML = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def test_tts_connection_credentials_are_collapsible():
    assert 'id="ttsConnectionToggle"' in HTML
    assert 'id="ttsConnection"' in HTML
    assert 'aria-expanded="' in HTML
    assert "connection.hidden = !willOpen" in HTML


def test_tts_cache_is_a_global_storage_group():
    provider_start = HTML.index("tts-provider-card")
    storage_start = HTML.index('class="group tts-storage"')
    save_start = HTML.index('id="ttsSave"')
    assert provider_start < storage_start < save_start
    assert 'class="tts-cache-row"' not in HTML


def test_tts_selection_uses_dwell_accent():
    assert '.tts-settings input[type="checkbox"]' in HTML
    assert '.tts-settings input[type="radio"]' in HTML
    assert 'accent-color:var(--accent)' in HTML
    assert '.tts-voice-card:has(input[type="radio"]:checked)' in HTML
