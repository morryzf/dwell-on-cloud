from datetime import datetime
import unittest

from app.memory_retrieval import select_memory_cards


NOW = datetime(2026, 8, 31, 12, 0, 0)


def card(card_id, content, *, topics=None, status="active", valid_until=None,
         importance="normal", memory_type="stable_fact", updated=1_787_000_000):
    return {
        "id": card_id,
        "content": content,
        "topics": topics or ["other"],
        "status": status,
        "valid_until": valid_until,
        "importance": importance,
        "memory_type": memory_type,
        "retention": "long_term",
        "updated": updated,
    }


class MemoryRetrievalTest(unittest.TestCase):
    def test_selects_relevant_cards_without_forcing_unrelated_ones(self):
        cards = [
            card("place", "她目前住在上海。", topics=["place"]),
            card("food", "她不吃香菜。", topics=["food"], memory_type="preference"),
            card("book", "她正在读一本推理小说。", topics=["books"]),
        ]
        chosen = select_memory_cards(cards, "上海最近天气怎么样？", now=NOW)
        self.assertEqual([item["id"] for item in chosen], ["place"])

    def test_excludes_hidden_archived_and_expired_cards(self):
        cards = [
            card("active", "她喜欢川菜。", topics=["food"], memory_type="preference"),
            card("hidden", "她喜欢粤菜。", topics=["food"], status="hidden"),
            card("archived", "她喜欢湘菜。", topics=["food"], status="archived"),
            card("expired", "她今天想吃火锅。", topics=["food"], valid_until="2026-08-30"),
        ]
        chosen = select_memory_cards(cards, "我们去吃什么菜？", now=NOW)
        self.assertEqual([item["id"] for item in chosen], ["active"])

    def test_returns_no_more_than_five(self):
        cards = [card(str(index), f"上海地点记录 {index}", topics=["place"]) for index in range(9)]
        chosen = select_memory_cards(cards, "上海有哪些地点？", now=NOW)
        self.assertEqual(len(chosen), 5)

    def test_empty_or_unrelated_context_returns_nothing(self):
        cards = [card("book", "她正在读一本推理小说。", topics=["books"])]
        self.assertEqual(select_memory_cards(cards, "", now=NOW), [])
        self.assertEqual(select_memory_cards(cards, "帮我算 2 加 2", now=NOW), [])


if __name__ == "__main__":
    unittest.main()

