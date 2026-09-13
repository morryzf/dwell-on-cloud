"""Dwell 的通用 MCP 客户端。

只负责标准 MCP 的连接、列工具和调用工具；它不了解 Ombre Brain 或任意特定工具名称。
"""

import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .provider_secrets import SecretConfigurationError, decrypt_api_key


class McpConnectionError(RuntimeError):
    pass


def _headers(server: dict) -> dict[str, str]:
    box = server.get("headers_box") or ""
    if not box:
        return {}
    try:
        raw = json.loads(decrypt_api_key(box))
    except (SecretConfigurationError, json.JSONDecodeError) as exc:
        raise McpConnectionError("MCP 凭据无法解密") from exc
    if not isinstance(raw, dict):
        raise McpConnectionError("MCP 凭据格式无效")
    return {str(key): str(value) for key, value in raw.items() if str(key).strip()}


@asynccontextmanager
async def _session(server: dict):
    headers = _headers(server)
    transport = server.get("transport", "streamable_http")
    try:
        if transport == "sse":
            async with sse_client(server["url"], headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif transport == "streamable_http":
            async with create_mcp_http_client(headers=headers) as client:
                async with streamable_http_client(server["url"], http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        else:
            raise McpConnectionError("不支持的 MCP 传输方式")
    except McpConnectionError:
        raise
    except Exception as exc:
        raise McpConnectionError(f"MCP 连接或调用失败：{exc}") from exc


async def list_tools(server: dict) -> list[dict]:
    """返回 OpenAI tools 参数可直接使用的 JSON Schema。"""
    async with _session(server) as session:
        result = await session.list_tools()
    tools = []
    for tool in result.tools:
        raw = tool.model_dump(by_alias=True, exclude_none=True)
        tools.append({
            "type": "function",
            "function": {
                "name": f"mcp__{server['id']}__{raw['name']}",
                "description": raw.get("description") or raw["name"],
                "parameters": raw.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return tools


async def call_tool(server: dict, tool_name: str, arguments: dict) -> str:
    """运行 MCP 工具并转成可交给模型的文本；不泄露连接凭据。"""
    async with _session(server) as session:
        result = await session.call_tool(tool_name, arguments)
    content = []
    for item in result.content:
        raw = item.model_dump(by_alias=True, exclude_none=True)
        content.append(raw)
    payload = {"is_error": bool(getattr(result, "isError", False)), "content": content}
    return json.dumps(payload, ensure_ascii=False)[:16000]

