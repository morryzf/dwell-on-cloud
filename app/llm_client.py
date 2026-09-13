"""通用 OpenAI 兼容聊天客户端。

这是普通聊天的模型通道；heartbeat 只应负责定时唤醒，不应成为每次对话的必经网关。
"""

import copy
import json
from urllib.parse import urlparse

import httpx

from .provider_secrets import SecretConfigurationError, decrypt_api_key


def _part_texts(value) -> list[str]:
    """从供应商的文本对象中取可展示文字，不碰签名或加密推理数据。"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(_part_texts(item))
        return texts
    if not isinstance(value, dict):
        return []
    texts = []
    for key in ("text", "thinking", "reasoning", "summary"):
        item = value.get(key)
        if isinstance(item, str) and item:
            texts.append(item)
    return texts


def reasoning_texts(delta: dict) -> list[str]:
    """兼容常见 OpenAI 中转的可见 thinking 字段。

    优先使用直接字段，避免同一个中转同时给 reasoning_content 和
    reasoning_details 时重复显示。只有供应商明确返回的文字才会被展示。
    """
    for key in ("reasoning_content", "reasoning", "thinking"):
        texts = _part_texts(delta.get(key))
        if texts:
            return texts

    details = _part_texts(delta.get("reasoning_details"))
    if details:
        return details

    content = delta.get("content")
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "").lower()
            if "thinking" in kind or "reasoning" in kind:
                texts.extend(_part_texts(part))
        return texts
    return []


def content_texts(value) -> list[str]:
    """读取正文；content 数组里的 thinking 部分不会混进最终回答。"""
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    texts = []
    for part in value:
        if isinstance(part, str):
            if part:
                texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "text").lower()
        if "thinking" in kind or "reasoning" in kind:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _token_count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nonnegative_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _usage_dict(raw) -> dict:
    """Normalize token, prompt-cache, and cost metrics without estimating them."""
    if not isinstance(raw, dict):
        return {}
    reported_input_tokens = _token_count(
        raw.get("prompt_tokens", raw.get("input_tokens"))
    )
    output_tokens = _token_count(raw.get("completion_tokens", raw.get("output_tokens")))
    input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    cache_creation = raw.get("cache_creation") or {}
    cached_tokens = _token_count(
        (input_details.get("cached_tokens") if isinstance(input_details, dict) else 0)
        or raw.get("cache_read_input_tokens")
        or raw.get("cached_tokens")
    )
    cache_write_tokens = _token_count(
        (input_details.get("cache_write_tokens") if isinstance(input_details, dict) else 0)
        or raw.get("cache_creation_input_tokens")
        or raw.get("cache_write_tokens")
    )
    cache_write_5m_tokens = _token_count(
        cache_creation.get("ephemeral_5m_input_tokens")
        if isinstance(cache_creation, dict) else 0
    )
    cache_write_1h_tokens = _token_count(
        cache_creation.get("ephemeral_1h_input_tokens")
        if isinstance(cache_creation, dict) else 0
    )
    reasoning_tokens = _token_count(
        (output_details.get("reasoning_tokens") if isinstance(output_details, dict) else 0)
        or raw.get("reasoning_tokens")
    )

    # OpenAI-compatible usage reports prompt_tokens as the whole prompt and
    # exposes cache reads/writes as subsets. Native Anthropic usage reports
    # input_tokens only for the uncached tail, with cache tokens alongside it.
    if "prompt_tokens" in raw:
        context_input_tokens = reported_input_tokens
        input_tokens = max(
            0, reported_input_tokens - cached_tokens - cache_write_tokens
        )
    else:
        input_tokens = reported_input_tokens
        context_input_tokens = input_tokens + cached_tokens + cache_write_tokens
    total_tokens = (
        _token_count(raw.get("total_tokens"))
        if "prompt_tokens" in raw
        else 0
    ) or context_input_tokens + output_tokens
    if total_tokens <= 0:
        return {}

    usage = {
        "input_tokens": input_tokens,
        "context_input_tokens": context_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_write_5m_tokens": cache_write_5m_tokens,
        "cache_write_1h_tokens": cache_write_1h_tokens,
        "reasoning_tokens": reasoning_tokens,
    }
    cost = _nonnegative_float(raw.get("cost"))
    cost_details = raw.get("cost_details") or {}
    upstream_cost = _nonnegative_float(
        cost_details.get("upstream_inference_cost")
        if isinstance(cost_details, dict) else None
    )
    if cost is not None:
        usage["cost"] = cost
    if upstream_cost is not None:
        usage["upstream_cost"] = upstream_cost
    return usage


def prompt_cache_ttl(provider: dict | None, model_id: str,
                     session_id: str | None = None) -> str:
    """Return an explicit Claude cache TTL for a verified, opt-in provider."""
    if not provider or not session_id:
        return ""
    provider_type = str(provider.get("provider_type") or "")
    if provider_type not in {"openrouter", "claude_compatible"}:
        return ""
    if str(provider.get("prompt_cache_ttl") or "off") not in {"5m", "1h"}:
        return ""
    host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
    model = str(model_id or "").lower()
    if provider_type == "openrouter":
        if host != "openrouter.ai" or not model.startswith("anthropic/"):
            return ""
    elif "claude" not in model:
        return ""
    return str(provider["prompt_cache_ttl"])


def prompt_cache_enabled(provider: dict | None, model_id: str) -> bool:
    """Whether ordinary chat may opt into explicit prompt caching."""
    return bool(prompt_cache_ttl(provider, model_id, session_id="configured"))


def _cacheable_messages(messages: list, ttl: str) -> list:
    """Copy messages, normalize assistant text blocks, and mark one stable anchor."""
    prepared = copy.deepcopy(messages)
    for message in prepared:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            message["content"] = [
                {"type": "text", "text": part} if isinstance(part, str) else part
                for part in content
            ]

    for message in reversed(prepared):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index in range(len(content) - 1, -1, -1):
            part = content[index]
            if not isinstance(part, dict) or part.get("type") != "text" or not part.get("text"):
                continue
            cache_control = {"type": "ephemeral"}
            if ttl == "1h":
                cache_control["ttl"] = "1h"
            content[index] = {**part, "cache_control": cache_control}
            return prepared
    return prepared


def _cacheable_tools(tools: list | None) -> list | None:
    if not tools:
        return tools
    prepared = copy.deepcopy(tools)
    return sorted(
        prepared,
        key=lambda tool: str((tool.get("function") or {}).get("name") or ""),
    )


def build_chat_payload(model_id: str, messages: list, tools: list | None = None,
                       max_tokens: int | None = None,
                       reasoning_effort: str | None = None,
                       thinking_enabled: bool = True,
                       provider: dict | None = None,
                       session_id: str | None = None) -> dict:
    """Build one request, adding guarded Claude cache fields when configured.

    Generic providers retain the exact provider-neutral request shape.
    """
    ttl = prompt_cache_ttl(provider, model_id, session_id)
    request_messages = _cacheable_messages(messages, ttl) if ttl else messages
    request_tools = _cacheable_tools(tools) if ttl else tools
    payload = {
        "model": model_id,
        "messages": request_messages,
        "stream": True,
        # OpenAI-compatible providers normally send usage in a final empty-choices chunk.
        "stream_options": {"include_usage": True},
    }
    if request_tools:
        payload["tools"] = request_tools
    if ttl and str((provider or {}).get("provider_type") or "") == "openrouter":
        payload["session_id"] = str(session_id)[:256]
    if max_tokens is not None:
        payload["max_tokens"] = max(1, int(max_tokens))
    # Match Claude-style thinking mode: disabling it changes the model request,
    # rather than merely hiding reasoning returned by the provider.
    effort = str(reasoning_effort or "").strip() if thinking_enabled else "none"
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _anthropic_content_blocks(value) -> list[dict]:
    """Convert OpenAI-compatible content into Anthropic message blocks."""
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        return []

    blocks: list[dict] = []
    for part in value:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "text")
        if kind == "text":
            text_value = str(part.get("text") or "")
            if not text_value:
                continue
            block = {"type": "text", "text": text_value}
            if isinstance(part.get("cache_control"), dict):
                block["cache_control"] = copy.deepcopy(part["cache_control"])
            blocks.append(block)
            continue
        if kind == "image" and isinstance(part.get("source"), dict):
            blocks.append({"type": "image", "source": copy.deepcopy(part["source"])})
            continue
        if kind != "image_url":
            continue
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("data:") and ";base64," in url:
            metadata, data = url.split(",", 1)
            media_type = metadata[5:].split(";", 1)[0] or "image/jpeg"
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
        elif url.startswith(("https://", "http://")):
            blocks.append({
                "type": "image",
                "source": {"type": "url", "url": url},
            })
    return blocks


def _anthropic_tool_input(arguments) -> dict:
    if isinstance(arguments, dict):
        return copy.deepcopy(arguments)
    try:
        parsed = json.loads(str(arguments or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_anthropic_payload(model_id: str, messages: list, tools: list | None = None,
                            max_tokens: int | None = None,
                            reasoning_effort: str | None = None,
                            thinking_enabled: bool = True,
                            provider: dict | None = None,
                            session_id: str | None = None) -> dict:
    """Build a native Anthropic Messages request from the app's neutral history."""
    ttl = prompt_cache_ttl(provider, model_id, session_id)
    prepared_messages = _cacheable_messages(messages, ttl) if ttl else copy.deepcopy(messages)
    system: list[dict] = []
    request_messages: list[dict] = []

    def append_message(role: str, blocks: list[dict]):
        if not blocks:
            return
        if request_messages and request_messages[-1]["role"] == role:
            request_messages[-1]["content"].extend(blocks)
        else:
            request_messages.append({"role": role, "content": blocks})

    for message in prepared_messages:
        role = str(message.get("role") or "")
        if role == "system":
            system.extend(
                block for block in _anthropic_content_blocks(message.get("content"))
                if block.get("type") == "text"
            )
            continue
        if role == "tool":
            tool_content = _anthropic_content_blocks(message.get("content"))
            append_message("user", [{
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": tool_content or [{"type": "text", "text": ""}],
            }])
            continue
        if role not in {"user", "assistant"}:
            continue

        blocks = _anthropic_content_blocks(message.get("content"))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "input": _anthropic_tool_input(function.get("arguments")),
                })
        append_message(role, blocks)

    payload = {
        "model": model_id,
        "messages": request_messages,
        "max_tokens": max(1, int(max_tokens)) if max_tokens is not None else 4096,
        "stream": True,
    }
    if system:
        payload["system"] = system
    request_tools = _cacheable_tools(tools) if ttl else tools
    if request_tools:
        converted_tools = []
        for tool in request_tools:
            function = tool.get("function") or {}
            converted = {
                "name": str(function.get("name") or ""),
                "input_schema": copy.deepcopy(
                    function.get("parameters")
                    or {"type": "object", "properties": {}}
                ),
            }
            description = str(function.get("description") or "")
            if description:
                converted["description"] = description
            converted_tools.append(converted)
        payload["tools"] = converted_tools

    return payload


def _anthropic_url(base_url: str) -> str:
    return str(base_url or "").rstrip("/") + "/messages"


def _anthropic_headers(api_key: str, auth_mode: str) -> dict:
    headers = {
        "Accept": "text/event-stream",
        "anthropic-version": "2023-06-01",
    }
    if auth_mode == "x-api-key":
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _anthropic_response_events(resp):
    calls: dict[int, dict] = {}
    raw_usage: dict = {}
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        if event_type == "error":
            error = event.get("error") or {}
            yield {
                "type": "text",
                "text": f"[供应商错误] {str(error.get('message') or 'Anthropic 流式请求失败')[:500]}",
            }
            return

        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            raw_usage.update(event_usage)
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            raw_usage.update(message["usage"])

        index = int(event.get("index") or 0)
        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            kind = str(block.get("type") or "")
            if kind == "text" and block.get("text"):
                yield {"type": "text", "text": str(block["text"])}
            elif kind == "thinking" and block.get("thinking"):
                yield {"type": "thinking", "thinking": str(block["thinking"])}
            elif kind == "tool_use":
                calls[index] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "arguments": "",
                    "initial_input": block.get("input"),
                }
            continue

        if event_type != "content_block_delta":
            continue
        delta = event.get("delta") or {}
        delta_type = str(delta.get("type") or "")
        if delta_type == "text_delta":
            text_value = str(delta.get("text") or "")
            if text_value:
                yield {"type": "text", "text": text_value}
        elif delta_type == "thinking_delta":
            thinking = str(delta.get("thinking") or "")
            if thinking:
                yield {"type": "thinking", "thinking": thinking}
        elif delta_type == "input_json_delta":
            call = calls.setdefault(index, {
                "id": "", "name": "", "arguments": "", "initial_input": None,
            })
            call["arguments"] += str(delta.get("partial_json") or "")

    if calls:
        completed = []
        for index in sorted(calls):
            call = calls[index]
            arguments = call["arguments"]
            if not arguments:
                arguments = json.dumps(
                    call.get("initial_input") or {}, ensure_ascii=False,
                    separators=(",", ":"),
                )
            completed.append({
                "id": call["id"],
                "name": call["name"],
                "arguments": arguments,
            })
        yield {"type": "tool_calls", "calls": completed}
    usage = _usage_dict(raw_usage)
    if usage:
        yield {"type": "usage", "usage": usage}


async def _openai_response_events(resp):
    calls: dict[int, dict] = {}
    usage = {}
    async for line in resp.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        normalized_usage = _usage_dict(chunk.get("usage"))
        if normalized_usage:
            usage = normalized_usage
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        for thinking in reasoning_texts(delta):
            yield {"type": "thinking", "thinking": thinking}
        for text_value in content_texts(delta.get("content")):
            yield {"type": "text", "text": text_value}
        for part in delta.get("tool_calls") or []:
            index = int(part.get("index", 0))
            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call["id"] += str(part.get("id") or "")
            function = part.get("function") or {}
            call["name"] += str(function.get("name") or "")
            call["arguments"] += str(function.get("arguments") or "")
    if calls:
        yield {"type": "tool_calls", "calls": [calls[index] for index in sorted(calls)]}
    if usage:
        yield {"type": "usage", "usage": usage}


async def _stream_openai(client, provider: dict, api_key: str, model_id: str,
                         messages: list, tools: list | None, max_tokens: int | None,
                         reasoning_effort: str | None, thinking_enabled: bool,
                         session_id: str | None, fallback_reason: str = ""):
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload = build_chat_payload(
        model_id, messages, tools, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
        provider=provider, session_id=session_id,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
    async with client.stream("POST", url, headers=headers, json=payload) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", errors="ignore")[:500]
            yield {"type": "text", "text": f"[供应商错误 {resp.status_code}] {body}"}
            return
        yield {
            "type": "cache_status",
            "protocol": "openai_compatible",
            "auth_mode": "bearer",
            "fallback_reason": fallback_reason,
        }
        async for event in _openai_response_events(resp):
            yield event


async def stream_chat(provider: dict, model_id: str, messages: list, tools: list | None = None,
                      max_tokens: int | None = None, reasoning_effort: str | None = None,
                      thinking_enabled: bool = True, session_id: str | None = None):
    """Stream chat through native Anthropic caching or the OpenAI-compatible path."""
    if not model_id:
        yield {"type": "text", "text": "[配置错误] 这个聊天还没有选择模型"}
        return
    if not provider.get("api_key_box"):
        yield {"type": "text", "text": "[配置错误] 这个供应商还没有保存 API 密钥"}
        return

    try:
        api_key = decrypt_api_key(provider["api_key_box"])
    except SecretConfigurationError as exc:
        yield {"type": "text", "text": f"[配置错误] {exc}"}
        return

    ttl = prompt_cache_ttl(provider, model_id, session_id)
    use_native_anthropic = bool(
        ttl and str(provider.get("provider_type") or "") == "claude_compatible"
    )
    timeout = httpx.Timeout(90.0, connect=20.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if use_native_anthropic:
                payload = build_anthropic_payload(
                    model_id, messages, tools, max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    thinking_enabled=thinking_enabled,
                    provider=provider, session_id=session_id,
                )
                fallback_status = 0
                for auth_mode in ("x-api-key", "bearer"):
                    headers = _anthropic_headers(api_key, auth_mode)
                    async with client.stream(
                        "POST", _anthropic_url(provider["base_url"]),
                        headers=headers, json=payload,
                    ) as resp:
                        if resp.status_code == 200:
                            yield {
                                "type": "cache_status",
                                "protocol": "anthropic_messages",
                                "auth_mode": auth_mode,
                                "fallback_reason": "",
                            }
                            async for event in _anthropic_response_events(resp):
                                yield event
                            return
                        fallback_status = resp.status_code
                        body = (await resp.aread()).decode(
                            "utf-8", errors="ignore"
                        )[:500]
                        if resp.status_code in {401, 403} and auth_mode == "x-api-key":
                            continue
                        if resp.status_code not in {401, 403, 404, 405}:
                            yield {
                                "type": "text",
                                "text": f"[供应商错误 {resp.status_code}] {body}",
                            }
                            return
                        break

                fallback_provider = {
                    **provider,
                    "provider_type": "generic",
                    "prompt_cache_ttl": "off",
                }
                async for event in _stream_openai(
                    client, fallback_provider, api_key, model_id, messages, tools,
                    max_tokens, reasoning_effort, thinking_enabled, None,
                    fallback_reason=f"native_http_{fallback_status}",
                ):
                    yield event
                return

            async for event in _stream_openai(
                client, provider, api_key, model_id, messages, tools,
                max_tokens, reasoning_effort, thinking_enabled, session_id,
            ):
                yield event
    except httpx.RequestError as exc:
        yield {"type": "text", "text": f"[网络错误] 无法连接供应商：{exc}"}
