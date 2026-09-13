from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_tts_player_uses_unframed_transport_icons():
    assert 'id="ttsPlayerPrevious"' in HTML
    assert 'id="ttsPlayerToggle"' in HTML
    assert 'id="ttsPlayerNext"' in HTML
    assert 'data-i="mediaPrevious"' in HTML
    assert 'data-i="mediaPause"' in HTML
    assert 'data-i="mediaNext"' in HTML
    assert ".tts-transport-button {" in HTML
    assert "background:none; border:0; border-radius:0; box-shadow:none;" in HTML


def test_tts_player_offers_stop_and_continuous_modes():
    assert 'data-tts-play-mode="stop">播完暂停</button>' in HTML
    assert 'data-tts-play-mode="continuous">连续播放</button>' in HTML
    assert "const TTS_PLAY_MODE_KEY = 'dwellTtsPlayMode'" in HTML
    assert "ttsState.playMode === 'continuous' && next" in HTML


def test_cached_queue_never_generates_missing_audio():
    assert '@app.get("/api/tts/cache/messages", dependencies=authed)' in APP
    assert "async def tts_message_audio(message_id: str, cached_only: bool = False):" in APP
    assert 'if not path.exists() and cached_only:' in APP
    assert '?cached_only=true' in HTML
    assert "void playTts(next.message_id, false, true)" in HTML


def test_cached_queue_is_scoped_to_current_chat_and_current_voice_config():
    assert "chat_id = _get_or_create_current_chat()" in APP
    assert "details = _tts_message_cache_details(chat_id, message_id, cfg)" in APP
    assert '"items": list(cached_by_turn.values())' in APP
