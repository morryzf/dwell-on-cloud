"""Read-only importer for a Kelivo v2 SQLite backup database.

Only conversations, user/assistant text, timestamps, and finished tool-call traces
are imported.  Provider settings, API keys, memories, and other Kelivo settings are
never opened by this module.
"""

import json
import sqlite3
from pathlib import Path

from . import db


class KelivoImportError(ValueError):
    pass


def _connect(path: str | Path) -> sqlite3.Connection:
    source = Path(path).resolve()
    if not source.is_file():
        raise KelivoImportError("没有找到 Kelivo 数据库文件")
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _check(conn: sqlite3.Connection) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"conversation_rows", "message_rows", "message_part_rows"}
    if not required.issubset(names):
        raise KelivoImportError("这不是可识别的 Kelivo v2 聊天数据库")


def preview(path: str | Path) -> list[dict]:
    with _connect(path) as conn:
        _check(conn)
        rows = conn.execute(
            "SELECT c.id,c.title,c.updated_at,COUNT(m.id) AS message_count,"
            "SUM(CASE WHEN p.kind IN ('image','file') THEN 1 ELSE 0 END) AS attachment_count "
            "FROM conversation_rows c "
            "LEFT JOIN message_rows m ON m.conversation_id=c.id "
            "LEFT JOIN message_part_rows p ON p.revision_id=m.id AND p.kind IN ('image','file') "
            "GROUP BY c.id,c.title,c.updated_at ORDER BY c.updated_at DESC"
        ).fetchall()
    return [{"id": row["id"], "title": row["title"] or "新对话", "updated_at": row["updated_at"],
             "message_count": int(row["message_count"] or 0), "attachment_count": int(row["attachment_count"] or 0)} for row in rows]


def _text(payload: str) -> str:
    """Kelivo currently stores text payloads as raw strings; accept JSON strings too."""
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return str(payload or "")
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        return str(parsed.get("text") or parsed.get("content") or "")
    return str(payload or "")


def import_conversation(path: str | Path, conversation_id: str) -> dict:
    with _connect(path) as conn:
        _check(conn)
        conversation = conn.execute("SELECT id,title FROM conversation_rows WHERE id=?", (conversation_id,)).fetchone()
        if not conversation:
            raise KelivoImportError("没有找到选择的 Kelivo 对话")
        source_messages = conn.execute(
            "SELECT id,role,timestamp FROM message_rows WHERE conversation_id=? "
            "ORDER BY message_order ASC, timestamp ASC", (conversation_id,)
        ).fetchall()
        if not source_messages:
            raise KelivoImportError("这个对话没有可导入的消息")

        chat = db.chat_add(conversation["title"] or "新对话")
        message_count = tool_count = attachment_count = 0
        for source in source_messages:
            parts = conn.execute(
                "SELECT kind,payload FROM message_part_rows WHERE revision_id=? "
                "ORDER BY ordinal ASC", (source["id"],)
            ).fetchall()
            text = "".join(_text(part["payload"]) for part in parts if part["kind"] == "text")
            # Kelivo uses microseconds; Dwell stores Unix seconds.
            made = int(int(source["timestamp"] or 0) / 1_000_000)
            message = db.message_add(chat["id"], source["role"], text, made=made)
            message_count += 1
            if source["role"] == "assistant":
                for part in parts:
                    if part["kind"] != "tool_call":
                        continue
                    try:
                        tool = json.loads(part["payload"])
                    except json.JSONDecodeError:
                        continue
                    name = str(tool.get("name") or "Kelivo tool")
                    args = tool.get("arguments") if isinstance(tool.get("arguments"), dict) else {}
                    call = db.tool_call_add(chat["id"], message["id"], f"mcp__kelivo__{name}", json.dumps(args, ensure_ascii=False))
                    db.tool_call_finish(call["id"], str(tool.get("content") or ""), False)
                    tool_count += 1
            attachment_count += sum(1 for part in parts if part["kind"] in ("image", "file"))
    return {"chat": chat, "message_count": message_count, "tool_count": tool_count,
            "attachment_count": attachment_count}

