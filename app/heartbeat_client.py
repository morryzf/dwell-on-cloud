"""跟 heartbeat 网关通信。dwell 自己不调 LLM，转发给 heartbeat。

heartbeat 现在跑在 HEARTBEAT_URL 上，暴露 OpenAI 兼容的 /v1/chat/completions，
它内部再转到中转站。我们只当 heartbeat 是"更聪明的 OpenAI"。
"""

import json
import os

import httpx

HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "").rstrip("/")
MODEL_NAME = os.environ.get("MODEL_NAME", "cloudy")


async def stream_chat(messages: list, chat_id: str):
    """向 heartbeat 发起流式对话。yield 出每一段文本增量。

    messages 是 OpenAI 格式：[{"role": "user"/"assistant", "content": "..."}]
    返回的是 async generator，每次 yield 一小段文本。
    """
    if not HEARTBEAT_URL:
        yield "[配置错误] 没设 HEARTBEAT_URL"
        return

    url = f"{HEARTBEAT_URL}/v1/chat/completions"
    payload = {
        "messages": messages,
        "stream": True,
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"[heartbeat 错误 {resp.status_code}] {body.decode('utf-8', errors='ignore')[:500]}"
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
    except httpx.RequestError as exc:
        yield f"[网络错误] {exc}"
    except Exception as exc:
        yield f"[未知错误] {exc}"
