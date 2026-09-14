from pathlib import Path


INDEX = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def test_push_setup_checks_the_server_delivery_result():
    assert "if (!response.ok || !saved.ok)" in INDEX
    assert "通知没有验证成功" in INDEX


def test_system_log_exposes_push_delivery_filter():
    assert "push_delivery:'手机通知'" in INDEX
    assert '<option value="push_delivery">手机通知</option>' in INDEX
