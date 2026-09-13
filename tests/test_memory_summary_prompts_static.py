from pathlib import Path


MAIN = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")


def test_memory_summary_prompts_use_the_configured_user_name():
    assert 'def _memory_voice_prompt() -> str:' in MAIN
    assert 'db.setting_get("user_display_name", "")' in MAIN
    assert 'or "用户"' in MAIN
    assert '用户使用称呼“{user_name}”' in MAIN
    assert "Morry（我老婆）" not in MAIN


def test_memory_summary_prompts_refer_to_the_user_consistently():
    assert "用户或我的当时反应" in MAIN
    assert "用户学会了" in MAIN
    assert "用户变得更……" in MAIN
