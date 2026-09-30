import asyncio
import json
import unittest

import httpx

from app.llm_client import _affordable_tokens, _stream_openai

PROVIDER = {"base_url": "https://openrouter.ai/api/v1", "provider_type": "openrouter"}


def refusal(affordable: int) -> httpx.Response:
    body = {"error": {"code": 402, "message": (
        "This request requires more credits, or fewer max_tokens. "
        f"You requested up to 65536 tokens, but can only afford {affordable}."
    )}}
    return httpx.Response(402, json=body)


def reply(text: str) -> httpx.Response:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})


def run(responses: list[httpx.Response]):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return responses[len(sent) - 1]

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in _stream_openai(
                client, PROVIDER, "key", "anthropic/claude-opus-4.6", [], None,
                None, None, True, None,
            )]

    return asyncio.run(collect()), sent


def texts(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "text")


class AffordableRetryTest(unittest.TestCase):
    """没写上限时 OpenRouter 按模型最大输出预扣；余额差一点就整轮失败。"""

    def test_reads_the_affordable_amount(self):
        self.assertEqual(_affordable_tokens(402, "... but can only afford 65052. To increase ..."), 65052)
        self.assertIsNone(_affordable_tokens(402, "some other payment problem"))
        self.assertIsNone(_affordable_tokens(429, "can only afford 10"))

    def test_retries_once_with_what_the_balance_covers(self):
        events, sent = run([refusal(65052), reply("好")])

        self.assertEqual(texts(events), "好")
        self.assertNotIn("max_tokens", sent[0])
        self.assertEqual(sent[1]["max_tokens"], 65052)

    def test_a_nearly_empty_balance_says_so_plainly(self):
        events, sent = run([refusal(300)])

        self.assertEqual(len(sent), 1)
        self.assertIn("余额快用完了", texts(events))
        self.assertIn("300", texts(events))

    def test_it_does_not_loop_when_the_retry_is_refused_too(self):
        events, sent = run([refusal(40000), refusal(39000)])

        self.assertEqual(len(sent), 2)
        self.assertIn("余额快用完了", texts(events))

    def test_other_errors_pass_through_untouched(self):
        events, sent = run([httpx.Response(500, text="boom")])

        self.assertEqual(len(sent), 1)
        self.assertEqual(texts(events), "[供应商错误 500] boom")


if __name__ == "__main__":
    unittest.main()
