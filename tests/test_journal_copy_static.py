from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def test_journal_home_uses_the_new_minimal_copy():
    html = INDEX.read_text(encoding="utf-8")

    assert "h1.textContent = '笔记本'" in html
    assert "在一起 ' + _days + ' 天" not in html
    assert "attention is all you need, and mine is yours" not in html
    assert "翻的是聊天、笔记本、收藏的话、悄悄话、夜记和日历。" in html


def test_journal_standard_cards_do_not_render_descriptions():
    html = INDEX.read_text(encoding="utf-8")

    for title in ("时间线", "我的日记", "最喜欢的话", "便签墙", "夜记", "悄悄话"):
        assert f"'{title}', ''," in html
