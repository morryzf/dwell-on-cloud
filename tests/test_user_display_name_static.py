from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_settings_exposes_a_persisted_user_display_name():
    assert 'id="userNameInput"' in INDEX
    assert 'placeholder="输入称呼"' in INDEX
    assert 'userDisplayName = saved || \'用户\'' in INDEX
    assert '@app.get("/api/user-profile", dependencies=authed)' in MAIN
    assert '@app.post("/api/user-profile", dependencies=authed)' in MAIN
    assert 'db.setting_set("user_display_name", name)' in MAIN


def test_tasks_use_the_configured_user_display_name():
    assert "userDisplayName + ' \\u7684'" in INDEX
    assert "bits.push(t.by === 'gu' ? '\\u987e\\u5c7f' : userDisplayName);" in INDEX
    assert "greeting.textContent = '\\u4eca\\u5929\\u4e5f\\u8f9b\\u82e6\\u4e86\\uff0c' + userDisplayName;" in INDEX
    assert "'Plum \\u7684'" not in INDEX
