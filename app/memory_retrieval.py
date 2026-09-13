"""Small, local relevance ranker for approved Dwell memory cards.

The ranker is intentionally deterministic: selecting memory must not add a second
model request, leak cards to another provider, or make every reply noticeably slower.
"""

from __future__ import annotations

import re
from datetime import date, datetime


TOPIC_HINTS = {
    "identity": ("名字", "年龄", "生日", "性格", "身份", "自己", "关于我"),
    "daily_life": ("日常", "最近", "今天", "生活", "习惯", "家里", "每天"),
    "place": ("哪里", "地点", "住", "搬家", "城市", "旅行", "天气", "上海", "北京"),
    "food": ("吃", "喝", "菜", "饭", "餐厅", "口味", "咖啡", "奶茶", "食物"),
    "books": ("书", "阅读", "小说", "作者", "读完", "读到"),
    "work_creativity": ("工作", "项目", "创作", "设计", "代码", "产品", "写作", "dwell"),
    "schedule": ("日程", "几点", "什么时候", "明天", "今天", "下周", "计划", "提醒"),
    "relationship": ("关系", "相处", "我们", "陪伴", "聊天", "联系", "在意"),
    "health_safety": ("身体", "健康", "生病", "疼", "睡眠", "药", "医院", "安全", "情绪"),
    "entertainment": ("电影", "电视剧", "视频", "游戏", "音乐", "歌", "综艺", "动漫"),
    "family_friends": ("家人", "朋友", "妈妈", "爸爸", "同事", "同学", "亲人"),
}

TYPE_HINTS = {
    "preference": ("喜欢", "不喜欢", "偏好", "想要", "讨厌"),
    "plan": ("计划", "准备", "打算", "以后", "明天", "下周"),
    "open_thread": ("继续", "进展", "完成", "做到哪", "后来", "项目"),
    "recent_event": ("最近", "刚才", "昨天", "今天", "发生"),
    "quote": ("原话", "说过", "怎么说", "那句话"),
}

COMMON_CJK_GRAMS = {
    "一个", "一些", "这个", "那个", "什么", "怎么", "可以", "需要", "觉得", "还是",
    "已经", "正在", "现在", "今天", "最近", "我们", "你们", "他们", "自己", "事情",
    "喜欢", "知道", "记得", "以后", "时候", "因为", "所以", "但是", "如果", "没有",
}


def _normalized(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "").casefold())


def _terms(text: object) -> set[str]:
    raw = str(text or "").casefold()
    terms = {word for word in re.findall(r"[a-z0-9_]{2,}", raw)}
    for chunk in re.findall(r"[\u3400-\u9fff]{2,}", raw):
        for size in (2, 3):
            terms.update(chunk[index:index + size] for index in range(len(chunk) - size + 1))
    return {term for term in terms if term not in COMMON_CJK_GRAMS}


def _valid_on(card: dict, today: date) -> bool:
    if str(card.get("status") or "active") != "active":
        return False
    valid_until = str(card.get("valid_until") or "").strip()
    if not valid_until:
        return True
    try:
        return datetime.strptime(valid_until, "%Y-%m-%d").date() >= today
    except ValueError:
        return False


def _score(card: dict, query: str, query_terms: set[str], now: datetime) -> float:
    content = str(card.get("content") or "").strip()
    if not content:
        return 0.0
    content_terms = _terms(content)
    shared = query_terms & content_terms
    score = sum(1.6 if len(term) >= 3 else 1.0 for term in shared)
    normalized_query, normalized_content = _normalized(query), _normalized(content)
    if len(normalized_content) >= 4 and normalized_content in normalized_query:
        score += 5.0

    topic_hits = 0
    for topic in card.get("topics") or []:
        hints = TOPIC_HINTS.get(str(topic), ())
        if any(hint in normalized_query for hint in hints):
            topic_hits += 1
    score += min(2, topic_hits) * 2.4

    memory_type = str(card.get("memory_type") or "")
    if any(hint in normalized_query for hint in TYPE_HINTS.get(memory_type, ())):
        score += 1.4

    # Importance and freshness can reorder relevant cards, but never make an
    # unrelated card eligible by themselves.
    if score <= 0:
        return 0.0
    score += {"high": 0.8, "normal": 0.3, "low": 0.0}.get(str(card.get("importance")), 0.0)
    try:
        age_days = max(0, (now - datetime.fromtimestamp(int(card.get("updated") or 0))).days)
    except (TypeError, ValueError, OSError):
        age_days = 9999
    if age_days <= 30:
        score += 0.35
    elif age_days <= 180:
        score += 0.12
    if str(card.get("retention")) == "fading":
        score -= 0.2
    return round(score, 4)


def select_memory_cards(
    cards: list[dict], context: str, *, limit: int = 5, now: datetime | None = None
) -> list[dict]:
    """Return at most ``limit`` relevant, active and unexpired cards."""
    now = now or datetime.now()
    query = str(context or "").strip()
    if not query:
        return []
    query_terms = _terms(query)
    ranked = []
    for card in cards:
        if not _valid_on(card, now.date()):
            continue
        score = _score(card, query, query_terms, now)
        if score <= 0:
            continue
        ranked.append({**card, "selection_score": score})
    ranked.sort(
        key=lambda item: (
            -float(item["selection_score"]),
            {"high": 0, "normal": 1, "low": 2}.get(str(item.get("importance")), 3),
            -int(item.get("updated") or 0),
            str(item.get("id") or ""),
        )
    )
    return ranked[: max(0, min(5, int(limit)))]

