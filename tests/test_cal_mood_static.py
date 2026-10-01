import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_calendar_actions_sent_by_page_are_handled_by_backend():
    """日历页发出去的每个动作，后端都要认得——心情曾经发 day_record，后端只认 set_mood。"""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    start = main.index("async def cal_post(")
    handled = set(re.findall(r'action == "(\w+)"', main[start:start + 4000]))
    sent = set(re.findall(r"calAct\(\{\s*action:\s*'(\w+)'", html))
    assert sent, "没找到日历页发出的动作"
    assert sent <= handled, f"后端不认得：{sent - handled}"
    assert "day_record" not in sent
