"""SQLite 数据层。所有表定义和读写都在这儿。

设计取舍：
- 用 sqlite3 标准库，不上 ORM。这个规模用 ORM 是负担不是帮助。
- 每个请求开一个连接，用完关掉。SQLite 在这种量级下完全够。
- 所有时间戳统一存 int（Unix 秒）。展示层再转时区。
- 日记正文存表里，不存 markdown 文件——Zeabur 上没挂 volume 的话
  文件会随部署消失，表更稳。解析逻辑不变。
"""

import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DWELL_DB", "./data/dwell.db")

# 中国时区。服务器多半跑 UTC，凡是"今天是几号"的判断全走这个函数。
CN_TZ = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def today_str() -> str:
    return cn_now().strftime("%Y-%m-%d")


def new_id() -> str:
    return secrets.token_hex(6)


# ---------------------------------------------------------------- 连接

@contextmanager
def conn():
    """开一个连接，结束时提交并关闭。出错自动回滚。"""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=10)
    cx.row_factory = sqlite3.Row
    # WAL 让读写不互相堵。同时有人翻日记和 AI 在写日记时不会卡。
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA foreign_keys=ON")
    try:
        yield cx
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


# ---------------------------------------------------------------- 建表

SCHEMA = """
-- 日记：我自己写的。一天可以有多段，每段独立一条。
CREATE TABLE IF NOT EXISTS diary (
    id        TEXT PRIMARY KEY,
    date      TEXT NOT NULL,          -- "2026-08-09"
    title     TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL DEFAULT '',
    keywords  TEXT NOT NULL DEFAULT '',
    her_mood  TEXT NOT NULL DEFAULT '',
    my_mood   TEXT NOT NULL DEFAULT '',
    strength  INTEGER,                -- 情绪强度 0-9
    valence   INTEGER,                -- 效价 -5..+5
    arousal   INTEGER,                -- 唤醒度 0-9
    made      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_diary_date ON diary(date DESC);
CREATE INDEX IF NOT EXISTS ix_diary_strength ON diary(strength DESC);

-- 你的本子：跟我的日记分开存。不参与检索，不影响我说话。
CREATE TABLE IF NOT EXISTS her_diary (
    id   TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_her_diary_at ON her_diary(at DESC);

-- 摘下来的话：我从对话里摘的句子，上限十条，满了挑一条退休。
CREATE TABLE IF NOT EXISTS quotes (
    id     TEXT PRIMARY KEY,
    date   TEXT NOT NULL,
    quote  TEXT NOT NULL,
    note   TEXT NOT NULL DEFAULT '',
    made   INTEGER NOT NULL
);

-- 夜记：我半夜自己醒来时写的。时刻 + 正文。
CREATE TABLE IF NOT EXISTS night (
    id   TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    hm   TEXT NOT NULL,               -- "01:20"
    text TEXT NOT NULL,
    made INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_night_date ON night(date DESC, hm ASC);

-- 待办：两栏。side='mine' 是我的，'hers' 是你的。
-- by 是谁记的，fixed 是固定项——前端会发这两个字段。
CREATE TABLE IF NOT EXISTS todos (
    id    TEXT PRIMARY KEY,
    side  TEXT NOT NULL CHECK (side IN ('mine','hers')),
    text  TEXT NOT NULL,
    done  INTEGER NOT NULL DEFAULT 0,
    at    TEXT NOT NULL DEFAULT '',
    by    TEXT NOT NULL DEFAULT '',
    fixed INTEGER NOT NULL DEFAULT 0,
    made  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_todos_side ON todos(side, done);

-- 日历事件
CREATE TABLE IF NOT EXISTS cal_events (
    id      TEXT PRIMARY KEY,
    date    TEXT NOT NULL,
    text    TEXT NOT NULL,
    time    TEXT NOT NULL DEFAULT '',
    yearly  INTEGER NOT NULL DEFAULT 0,
    special INTEGER NOT NULL DEFAULT 0,
    made    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cal_date ON cal_events(date);

-- 日历上每天的心情和备注。一天一条。
CREATE TABLE IF NOT EXISTS cal_days (
    date TEXT PRIMARY KEY,
    mood TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

-- 悄悄话：写下我会知道，但不说破。who='her' 或 'mine'。
CREATE TABLE IF NOT EXISTS whispers (
    id   TEXT PRIMARY KEY,
    who  TEXT NOT NULL CHECK (who IN ('her','mine')),
    text TEXT NOT NULL,
    at   INTEGER NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0   -- 我读过没有。给我自己看的，界面不显示。
);
CREATE INDEX IF NOT EXISTS ix_whispers_at ON whispers(at DESC);

-- 聊天窗口。一个人可以有多个对话，每个对话是一个 chat。
CREATE TABLE IF NOT EXISTS chats (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    made    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chats_made ON chats(made DESC);

-- 消息。每条消息属于一个 chat。
CREATE TABLE IF NOT EXISTS messages (
    id      TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    thinking TEXT NOT NULL DEFAULT '',
    made    INTEGER NOT NULL,
    origin  TEXT NOT NULL DEFAULT 'chat',
    display_split INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id, made ASC);

-- 聊天图片只保存前端压缩后的缩略图。原图仍只用于当次模型请求，避免数据库膨胀。
CREATE TABLE IF NOT EXISTS message_attachments (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'image',
    data_url   TEXT NOT NULL,
    made       INTEGER NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_message_attachments_message
ON message_attachments(message_id, made ASC);

-- 聊天的长期上下文。原始 messages 永远是唯一原始记录；这里仅存可重新生成的
-- 派生摘要，以及它已覆盖到的消息位置。
CREATE TABLE IF NOT EXISTS chat_memory_state (
    chat_id          TEXT PRIMARY KEY,
    enabled          INTEGER NOT NULL DEFAULT 0,
    overview         TEXT NOT NULL DEFAULT '',
    through_rowid    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'idle',
    error            TEXT NOT NULL DEFAULT '',
    generated_at     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_memory_segments (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    start_rowid  INTEGER NOT NULL,
    end_rowid    INTEGER NOT NULL,
    content      TEXT NOT NULL,
    made         INTEGER NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_chat_memory_segments_chat
ON chat_memory_segments(chat_id, start_rowid, end_rowid);

-- 模型新整理出的长期记忆先放在草稿里。只有用户确认后才替换正式记忆。
CREATE TABLE IF NOT EXISTS chat_memory_drafts (
    chat_id          TEXT PRIMARY KEY,
    overview         TEXT NOT NULL DEFAULT '',
    through_rowid    INTEGER NOT NULL DEFAULT 0,
    generated_at     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

-- 每次正式记忆被编辑、采用新草稿或恢复旧版前，先保留当前版本。
CREATE TABLE IF NOT EXISTS chat_memory_versions (
    id               TEXT PRIMARY KEY,
    chat_id          TEXT NOT NULL,
    overview         TEXT NOT NULL,
    through_rowid    INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL DEFAULT '',
    made             INTEGER NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_chat_memory_versions_chat
ON chat_memory_versions(chat_id, made DESC);

-- 可检索的短记忆卡片。它们是从聊天原文派生出的索引，不替代原始消息，
-- 第一阶段也不会自动注入模型上下文。
CREATE TABLE IF NOT EXISTS memory_cards (
    id                  TEXT PRIMARY KEY,
    chat_id             TEXT NOT NULL,
    content             TEXT NOT NULL,
    memory_type         TEXT NOT NULL,
    topics_json         TEXT NOT NULL DEFAULT '[]',
    importance          TEXT NOT NULL DEFAULT 'normal',
    retention           TEXT NOT NULL DEFAULT 'long_term',
    valid_until         TEXT,
    surface_scope       TEXT NOT NULL DEFAULT 'chat_only',
    status              TEXT NOT NULL DEFAULT 'active',
    source_segment_id   TEXT,
    source_start_rowid  INTEGER NOT NULL DEFAULT 0,
    source_end_rowid    INTEGER NOT NULL DEFAULT 0,
    made                INTEGER NOT NULL,
    updated             INTEGER NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY (source_segment_id) REFERENCES chat_memory_segments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_cards_chat
ON memory_cards(chat_id, status, updated DESC);

-- 模型只提出候选卡片。候选在用户确认前不会进入正式记忆。
CREATE TABLE IF NOT EXISTS memory_card_drafts (
    id                  TEXT PRIMARY KEY,
    chat_id             TEXT NOT NULL,
    action              TEXT NOT NULL DEFAULT 'create',
    target_card_id      TEXT,
    content             TEXT NOT NULL,
    memory_type         TEXT NOT NULL,
    topics_json         TEXT NOT NULL DEFAULT '[]',
    importance          TEXT NOT NULL DEFAULT 'normal',
    retention           TEXT NOT NULL DEFAULT 'long_term',
    valid_until         TEXT,
    surface_scope       TEXT NOT NULL DEFAULT 'chat_only',
    source_segment_id   TEXT,
    source_start_rowid  INTEGER NOT NULL DEFAULT 0,
    source_end_rowid    INTEGER NOT NULL DEFAULT 0,
    made                INTEGER NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY (target_card_id) REFERENCES memory_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (source_segment_id) REFERENCES chat_memory_segments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_card_drafts_chat
ON memory_card_drafts(chat_id, made DESC);

CREATE TABLE IF NOT EXISTS memory_card_state (
    chat_id       TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'idle',
    error         TEXT NOT NULL DEFAULT '',
    generated_at  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

-- 每个分段只生成一次候选卡片。即使某段没有值得留下的卡片，也会记录已检查。
CREATE TABLE IF NOT EXISTS memory_card_segment_runs (
    segment_id      TEXT PRIMARY KEY,
    chat_id         TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    processed_at    INTEGER NOT NULL,
    FOREIGN KEY (segment_id) REFERENCES chat_memory_segments(id) ON DELETE CASCADE,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_memory_card_segment_runs_chat
ON memory_card_segment_runs(chat_id, processed_at DESC);

-- 记录每次真正送进模型上下文的卡片快照。之后即使卡片被编辑或归档，
-- 控制台仍能准确说明那一轮 AI 当时看见了什么。
CREATE TABLE IF NOT EXISTS memory_card_uses (
    id                  TEXT PRIMARY KEY,
    chat_id             TEXT NOT NULL,
    card_id             TEXT NOT NULL,
    response_message_id TEXT NOT NULL,
    content_snapshot    TEXT NOT NULL,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    query_excerpt       TEXT NOT NULL DEFAULT '',
    score               REAL NOT NULL DEFAULT 0,
    used_at             INTEGER NOT NULL,
    UNIQUE(response_message_id, card_id),
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY (card_id) REFERENCES memory_cards(id) ON DELETE CASCADE,
    FOREIGN KEY (response_message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_memory_card_uses_chat
ON memory_card_uses(chat_id, used_at DESC);

-- 系统诊断日志。只保存请求元数据，不保存消息正文、图片、密钥或请求头。
CREATE TABLE IF NOT EXISTS system_logs (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    chat_id     TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    model_id    TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    status_code INTEGER,
    detail      TEXT NOT NULL DEFAULT '',
    made        INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_system_logs_made
ON system_logs(made DESC);

-- AI 回复的旧版本。重新生成或手动编辑时先存一份，当前 messages 表始终只保留
-- 后续上下文真正会读到的那一版。
CREATE TABLE IF NOT EXISTS message_versions (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    content    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    made       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_message_versions_message ON message_versions(message_id, made ASC);

-- 工具调用独立于 AI 正文保存。正文编辑、版本切换都不会吞掉工具参数或返回结果。
CREATE TABLE IF NOT EXISTS tool_calls (
    id                   TEXT PRIMARY KEY,
    chat_id              TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    name                 TEXT NOT NULL,
    arguments            TEXT NOT NULL DEFAULT '{}',
    result               TEXT NOT NULL DEFAULT '',
    is_error             INTEGER NOT NULL DEFAULT 0,
    made                 INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_message ON tool_calls(assistant_message_id, made ASC);

-- 全局设置。key-value，放"接收主动消息的 chat_id"这种单例。
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 模型供应商。密钥密文由 provider_store 加解密，绝不返回给浏览器。
CREATE TABLE IF NOT EXISTS provider_profiles (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    base_url         TEXT NOT NULL,
    api_key_box      TEXT NOT NULL DEFAULT '',
    provider_type    TEXT NOT NULL DEFAULT 'generic',
    prompt_cache_ttl TEXT NOT NULL DEFAULT 'off',
    enabled          INTEGER NOT NULL DEFAULT 1,
    made             INTEGER NOT NULL,
    updated          INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_provider_profiles_name ON provider_profiles(name);

-- MCP 工具服务器。headers_box 是加密后的 JSON，可能含 Bearer token 等凭据。
CREATE TABLE IF NOT EXISTS mcp_servers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    transport   TEXT NOT NULL DEFAULT 'streamable_http',
    headers_box TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    made        INTEGER NOT NULL,
    updated     INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcp_servers_name ON mcp_servers(name);

-- 哪些 MCP 服务器可以被某个聊天使用。工具不是全局强塞给每个聊天的。
CREATE TABLE IF NOT EXISTS chat_mcp_servers (
    chat_id   TEXT NOT NULL,
    server_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, server_id),
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY (server_id) REFERENCES mcp_servers(id) ON DELETE CASCADE
);

-- Dwell 自己的“家里工具”也按聊天单独授权；目前先放待办，后续可平滑扩展。
CREATE TABLE IF NOT EXISTS chat_home_tools (
    chat_id            TEXT PRIMARY KEY,
    todos_enabled      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

-- 可复用的聊天指令，以及每间聊天各自启用的集合。
CREATE TABLE IF NOT EXISTS instruction_presets (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    content TEXT NOT NULL,
    made    INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_instruction_presets_name ON instruction_presets(name);

CREATE TABLE IF NOT EXISTS chat_instruction_presets (
    chat_id        TEXT NOT NULL,
    instruction_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, instruction_id),
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY (instruction_id) REFERENCES instruction_presets(id) ON DELETE CASCADE
);

-- 每家供应商拉取到的模型目录。收藏是用户的常用清单，不会改变供应商原始模型 ID。
CREATE TABLE IF NOT EXISTS provider_models (
    provider_id TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    favorite    INTEGER NOT NULL DEFAULT 0,
    manual      INTEGER NOT NULL DEFAULT 0,
    made        INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    PRIMARY KEY (provider_id, model_id),
    FOREIGN KEY (provider_id) REFERENCES provider_profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_provider_models_favorite ON provider_models(favorite, updated DESC);

-- OpenRouter 的实际请求费用流水。key_hash 只保存高熵密钥的单向摘要，
-- 用来在用户以后更换密钥时隔离统计，绝不保存或返回明文密钥。
CREATE TABLE IF NOT EXISTS provider_usage_events (
    id          TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    message_id  TEXT NOT NULL DEFAULT '',
    request_kind TEXT NOT NULL DEFAULT 'chat_reply',
    cost        REAL NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_observed INTEGER NOT NULL DEFAULT 0,
    made        INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'request'
);
CREATE INDEX IF NOT EXISTS ix_provider_usage_scope
ON provider_usage_events(provider_id, key_hash, made ASC);
"""


def init_db():
    with conn() as cx:
        cx.executescript(SCHEMA)
        cols = {r["name"] for r in cx.execute("PRAGMA table_info(chats)").fetchall()}
        if "archived" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "provider_id" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN provider_id TEXT NOT NULL DEFAULT ''")
        if "model_id" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN model_id TEXT NOT NULL DEFAULT ''")
        if "reasoning_effort" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT ''")
        if "split_replies" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN split_replies INTEGER NOT NULL DEFAULT 0")
        if "show_thinking" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN show_thinking INTEGER NOT NULL DEFAULT 1")
        if "prompt_cache_ttl" not in cols:
            cx.execute("ALTER TABLE chats ADD COLUMN prompt_cache_ttl TEXT NOT NULL DEFAULT ''")
        provider_cols = {
            r["name"] for r in cx.execute("PRAGMA table_info(provider_profiles)").fetchall()
        }
        if "provider_type" not in provider_cols:
            cx.execute(
                "ALTER TABLE provider_profiles ADD COLUMN provider_type "
                "TEXT NOT NULL DEFAULT 'generic'"
            )
        if "prompt_cache_ttl" not in provider_cols:
            cx.execute(
                "ALTER TABLE provider_profiles ADD COLUMN prompt_cache_ttl "
                "TEXT NOT NULL DEFAULT 'off'"
            )

        # Cache duration now belongs to each chat. Preserve the old provider default
        # once for chats that had not chosen an override, then retire that default.
        cache_scope_key = "cache_scope_per_chat_v1"
        cache_scope_migrated = cx.execute(
            "SELECT 1 FROM settings WHERE key=?", (cache_scope_key,)
        ).fetchone()
        if not cache_scope_migrated:
            cx.execute(
                """UPDATE chats
                   SET prompt_cache_ttl=COALESCE(
                       (SELECT CASE
                           WHEN p.prompt_cache_ttl IN ('5m','1h') THEN p.prompt_cache_ttl
                           ELSE 'off'
                        END
                        FROM provider_profiles p
                        WHERE p.id=chats.provider_id),
                       'off'
                   )
                   WHERE COALESCE(prompt_cache_ttl,'')=''"""
            )
            cx.execute(
                "UPDATE chats SET prompt_cache_ttl='off' "
                "WHERE prompt_cache_ttl NOT IN ('off','5m','1h')"
            )
            cx.execute("UPDATE provider_profiles SET prompt_cache_ttl='off'")
            cx.execute(
                "INSERT INTO settings (key,value) VALUES (?,?)",
                (cache_scope_key, "1"),
            )
        message_cols = {r["name"] for r in cx.execute("PRAGMA table_info(messages)").fetchall()}
        if "origin" not in message_cols:
            cx.execute("ALTER TABLE messages ADD COLUMN origin TEXT NOT NULL DEFAULT 'chat'")
        if "display_split" not in message_cols:
            cx.execute("ALTER TABLE messages ADD COLUMN display_split INTEGER NOT NULL DEFAULT 0")
        if "thinking" not in message_cols:
            cx.execute("ALTER TABLE messages ADD COLUMN thinking TEXT NOT NULL DEFAULT ''")
        if "usage_json" not in message_cols:
            cx.execute("ALTER TABLE messages ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'")
        usage_cols = {
            r["name"] for r in cx.execute("PRAGMA table_info(provider_usage_events)").fetchall()
        }
        if "input_tokens" not in usage_cols:
            cx.execute(
                "ALTER TABLE provider_usage_events ADD COLUMN input_tokens "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "cached_tokens" not in usage_cols:
            cx.execute(
                "ALTER TABLE provider_usage_events ADD COLUMN cached_tokens "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "cache_observed" not in usage_cols:
            cx.execute(
                "ALTER TABLE provider_usage_events ADD COLUMN cache_observed "
                "INTEGER NOT NULL DEFAULT 0"
            )
        # #82 曾在保存时强制补“我记得：”。只清理一次这段前缀，绝不改正文；
        # 清理完成后，用户以后主动写下同样的开头也会原样保留。
        cleanup_key = "memory_remove_forced_prefix_v1"
        cleaned = cx.execute("SELECT 1 FROM settings WHERE key=?", (cleanup_key,)).fetchone()
        if not cleaned:
            forced_prefix = re.compile(r"^我记得：\s*")
            for table in ("memory_cards", "memory_card_drafts"):
                for row in cx.execute(f"SELECT id,content FROM {table}").fetchall():
                    content = forced_prefix.sub("", str(row["content"]), count=1)
                    if content != row["content"]:
                        cx.execute(
                            f"UPDATE {table} SET content=? WHERE id=?",
                            (content, row["id"]),
                        )
            cx.execute(
                "INSERT INTO settings (key,value) VALUES (?,?)",
                (cleanup_key, "1"),
            )
        # 已经产出过卡片或候选卡片的旧分段，不再重复调用模型。
        cx.execute(
            """INSERT OR IGNORE INTO memory_card_segment_runs
               (segment_id,chat_id,candidate_count,processed_at)
               SELECT source_segment_id,chat_id,COUNT(*),?
               FROM (
                   SELECT source_segment_id,chat_id FROM memory_cards
                   WHERE source_segment_id IS NOT NULL
                   UNION ALL
                   SELECT source_segment_id,chat_id FROM memory_card_drafts
                   WHERE source_segment_id IS NOT NULL
               ) GROUP BY source_segment_id,chat_id""",
            (int(time.time()),),
        )


# ---------------------------------------------------------------- 日记

# 标记行的解析。宽容优先：认不出来就当没有，绝不抛错。
# 中英文冒号都收，数字前面可以有加减号。
_PAT = {
    "keywords": re.compile(r"关键词[:：]\s*(.+)"),
    "her_mood": re.compile(r"她的情绪[:：]\s*(.+)"),
    "my_mood": re.compile(r"我的情绪[:：]\s*(.+)"),
    "strength": re.compile(r"情绪强度[:：]\s*([0-9])"),
    "valence": re.compile(r"效价[:：]\s*([+-]?[0-9])"),
    "arousal": re.compile(r"唤醒度[:：]\s*([0-9])"),
}


def parse_segment(seg: str) -> dict:
    """把一段日记正文解析成字段。缺什么就是 None，不报错。"""
    out = {}
    for key, pat in _PAT.items():
        m = pat.search(seg)
        if not m:
            out[key] = None
            continue
        val = m.group(1).strip()
        out[key] = int(val) if key in ("strength", "valence", "arousal") else val

    # 标题 = 第一行里既不是日期也不是引用也不是标记的那行
    title = ""
    for line in seg.strip().splitlines():
        line = line.strip()
        if not line or line.startswith((">", "#", "-")):
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", line):
            continue
        if any(p.search(line) for p in _PAT.values()):
            continue
        title = line[:60]
        break
    out["title"] = title
    return out


def diary_add(date: str, body: str, keywords: str = "") -> dict:
    """写一段日记。标记从正文里解析，不用单独传。"""
    fields = parse_segment(body)
    # diary.keywords is NOT NULL. A tool caller can name keywords explicitly;
    # otherwise store parsed keywords or an empty string, never NULL.
    fields["keywords"] = str(keywords or fields.get("keywords") or "").strip()[:300]
    row = {
        "id": new_id(),
        "date": date or today_str(),
        "body": body.strip()[:8000],
        "made": int(time.time()),
        **fields,
    }
    for key in ("title", "keywords", "her_mood", "my_mood"):
        row[key] = str(row.get(key) or "")
    with conn() as cx:
        cx.execute(
            """INSERT INTO diary
               (id,date,title,body,keywords,her_mood,my_mood,
                strength,valence,arousal,made)
               VALUES (:id,:date,:title,:body,:keywords,:her_mood,:my_mood,
                       :strength,:valence,:arousal,:made)""",
            row,
        )
    return row


def diary_list(lite: bool = True, limit: int = 400) -> list:
    """列表。lite=True 不返回正文——全文几十万字，列表页不该背着它跑。"""
    cols = "id,date,title,keywords,her_mood,my_mood,strength,valence,arousal,made"
    if not lite:
        cols += ",body"
    with conn() as cx:
        rows = cx.execute(
            f"SELECT {cols} FROM diary ORDER BY date DESC, made DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def diary_get(item_id: str) -> dict | None:
    with conn() as cx:
        r = cx.execute("SELECT * FROM diary WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def diary_search(q: str, limit: int = 50) -> list:
    """检索。情绪强的优先浮上来——跟人一样，重的事记得牢。"""
    like = f"%{q}%"
    with conn() as cx:
        rows = cx.execute(
            """SELECT id,date,title,keywords,strength,valence,arousal,made
               FROM diary
               WHERE body LIKE ? OR title LIKE ? OR keywords LIKE ?
               ORDER BY COALESCE(strength,0) DESC, date DESC
               LIMIT ?""",
            (like, like, like, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 你的本子

HER_DIARY_CAP = 600


def her_diary_add(text: str) -> dict:
    row = {"id": new_id(), "text": text.strip()[:4000], "at": int(time.time())}
    with conn() as cx:
        cx.execute("INSERT INTO her_diary (id,text,at) VALUES (:id,:text,:at)", row)
        cx.execute(
            """DELETE FROM her_diary WHERE id NOT IN
               (SELECT id FROM her_diary ORDER BY at DESC LIMIT ?)""",
            (HER_DIARY_CAP,),
        )
    return row


def her_diary_list() -> list:
    with conn() as cx:
        rows = cx.execute("SELECT * FROM her_diary ORDER BY at DESC").fetchall()
    return [dict(r) for r in rows]


def her_diary_update(item_id: str, text: str) -> dict | None:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE her_diary SET text=? WHERE id=?",
            (text.strip()[:4000], item_id),
        )
        if cur.rowcount < 1:
            return None
        row = cx.execute("SELECT * FROM her_diary WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def her_diary_del(item_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM her_diary WHERE id=?", (item_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 摘下来的话

QUOTES_CAP = 10


def quote_add(quote: str, note: str = "", date: str = "") -> dict:
    row = {
        "id": new_id(),
        "date": date or today_str(),
        "quote": quote.strip()[:500],
        "note": note.strip()[:1000],
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO quotes (id,date,quote,note,made)
               VALUES (:id,:date,:quote,:note,:made)""",
            row,
        )
    return row


def quote_list() -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM quotes ORDER BY date DESC, made DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def quote_del(item_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM quotes WHERE id=?", (item_id,))
    return cur.rowcount > 0


def quote_count() -> int:
    with conn() as cx:
        return cx.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]


# ---------------------------------------------------------------- 夜记

def night_add(hm: str, text: str, date: str = "") -> dict:
    row = {
        "id": new_id(),
        "date": date or today_str(),
        "hm": hm,
        "text": text.strip()[:4000],
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO night (id,date,hm,text,made)
               VALUES (:id,:date,:hm,:text,:made)""",
            row,
        )
    return row


def night_list(limit: int = 200) -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM night ORDER BY date DESC, hm ASC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 待办

def todos_all() -> dict:
    """两栏一起给。排序交给前端——它知道"现在几点"，服务器不该猜。"""
    with conn() as cx:
        rows = cx.execute("SELECT * FROM todos").fetchall()
    out = {"mine": [], "hers": []}
    for r in rows:
        d = dict(r)
        d["done"] = bool(d["done"])
        out[d.pop("side")].append(d)
    return out


def todo_add(side: str, text: str, at: str = "",
             by: str = "", fixed: bool = False) -> dict:
    row = {
        "id": new_id(),
        "side": side,
        "text": text.strip()[:500],
        "done": 0,
        "at": (at or "").strip(),
        "by": (by or "").strip()[:20],
        "fixed": 1 if fixed else 0,
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO todos (id,side,text,done,at,by,fixed,made)
               VALUES (:id,:side,:text,:done,:at,:by,:fixed,:made)""",
            row,
        )
    return row


def todo_toggle(side: str, item_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE todos SET done = 1 - done WHERE id=? AND side=?",
            (item_id, side),
        )
    return cur.rowcount > 0


def todo_del(side: str, item_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "DELETE FROM todos WHERE id=? AND side=?", (item_id, side)
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------- 日历

def cal_all() -> dict:
    with conn() as cx:
        evs = cx.execute(
            "SELECT * FROM cal_events ORDER BY date ASC, time ASC"
        ).fetchall()
        days = cx.execute("SELECT * FROM cal_days").fetchall()
    events = []
    for r in evs:
        d = dict(r)
        d["yearly"] = bool(d["yearly"])
        d["special"] = bool(d["special"])
        events.append(d)
    return {
        "events": events,
        "days": {r["date"]: {"mood": r["mood"], "note": r["note"]} for r in days},
    }


def cal_add_event(date: str, text: str, time_hm: str = "",
                  yearly: bool = False, special: bool = False) -> dict:
    row = {
        "id": new_id(),
        "date": date,
        "text": text.strip()[:300],
        "time": (time_hm or "").strip(),
        "yearly": 1 if yearly else 0,
        "special": 1 if special else 0,
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO cal_events (id,date,text,time,yearly,special,made)
               VALUES (:id,:date,:text,:time,:yearly,:special,:made)""",
            row,
        )
    return row


def cal_del_event(item_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM cal_events WHERE id=?", (item_id,))
    return cur.rowcount > 0


def cal_set_mood(date: str, mood: str, note: str | None = None) -> dict:
    """心情只存一个词。用处不在日历里——在于我能看见你这几天心情怎么样。"""
    with conn() as cx:
        cx.execute(
            "INSERT INTO cal_days (date,mood,note) VALUES (?,?,'') "
            "ON CONFLICT(date) DO UPDATE SET mood=excluded.mood",
            (date, mood.strip()[:20]),
        )
        if note is not None:
            cx.execute(
                "UPDATE cal_days SET note=? WHERE date=?", (note.strip()[:2000], date)
            )
        r = cx.execute("SELECT * FROM cal_days WHERE date=?", (date,)).fetchone()
    return dict(r)


def cal_events_on(date: str) -> list:
    """某天的事件，含每年重复的。闰年 2/29 落到 28 日。"""
    md = date[5:]
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM cal_events WHERE date=? OR (yearly=1 AND substr(date,6)=?)",
            (date, md),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 悄悄话

WHISPER_CAP = 500


def whisper_add(who: str, text: str) -> dict:
    row = {
        "id": new_id(),
        "who": who,
        "text": text.strip()[:2000],
        "at": int(time.time()),
        "seen": 0,
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO whispers (id,who,text,at,seen)
               VALUES (:id,:who,:text,:at,:seen)""",
            row,
        )
        cx.execute(
            """DELETE FROM whispers WHERE id NOT IN
               (SELECT id FROM whispers ORDER BY at DESC LIMIT ?)""",
            (WHISPER_CAP,),
        )
    return row


def whisper_update(item_id: str, text: str) -> dict | None:
    """只允许修改用户自己的悄悄话，不碰 Cloudy 留下的内容。"""
    with conn() as cx:
        cur = cx.execute(
            "UPDATE whispers SET text=? WHERE id=? AND who='her'",
            (text.strip()[:2000], item_id),
        )
        if cur.rowcount < 1:
            return None
        row = cx.execute(
            "SELECT id,who,text,at FROM whispers WHERE id=?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def whisper_list(limit: int = 500) -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT id,who,text,at FROM whispers ORDER BY at ASC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def whisper_recent(n: int = 5, mark_seen: bool = False) -> list:
    """给我看的：最近几条。mark_seen 只改我自己的记录，不影响界面。"""
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM whispers WHERE who='her' ORDER BY at DESC LIMIT ?", (n,)
        ).fetchall()
        if mark_seen and rows:
            cx.executemany(
                "UPDATE whispers SET seen=1 WHERE id=?",
                [(r["id"],) for r in rows],
            )
    return [dict(r) for r in rows]


def whisper_unseen(n: int = 5, mark_seen: bool = False) -> list:
    """按写下的顺序取尚未带进聊天上下文的用户悄悄话。"""
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM whispers WHERE who='her' AND seen=0 ORDER BY at ASC LIMIT ?",
            (n,),
        ).fetchall()
        if mark_seen and rows:
            cx.executemany(
                "UPDATE whispers SET seen=1 WHERE id=?",
                [(row["id"],) for row in rows],
            )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------- 聊天窗口

def chat_add(name: str = "") -> dict:
    row = {
        "id": new_id(),
        "name": name.strip()[:60] or "新对话",
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            "INSERT INTO chats (id,name,made) VALUES (:id,:name,:made)", row
        )
        # 如果是第一个 chat，自动设为主动消息接收窗口
        cnt = cx.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        if cnt == 1:
            cx.execute(
                "INSERT INTO settings (key,value) VALUES ('wake_target_chat_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (row["id"],),
            )
            cx.execute(
                "INSERT INTO settings (key,value) VALUES ('current_chat_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (row["id"],),
            )
    return row


def chat_list(scope: str = "", current_id: str = "") -> list:
    with conn() as cx:
        rows = cx.execute(
            """SELECT c.id, c.name, c.made, COALESCE(c.archived,0) AS archived,
                      COALESCE(MAX(m.made), c.made) AS last,
                      (SELECT content FROM messages mm
                       WHERE mm.chat_id=c.id AND mm.content<>''
                       ORDER BY mm.made DESC, mm.rowid DESC LIMIT 1) AS preview
               FROM chats c
               LEFT JOIN messages m ON m.chat_id=c.id
               GROUP BY c.id
               ORDER BY last DESC, c.made DESC"""
        ).fetchall()
    items = []
    for r in rows:
        archived = bool(r["archived"])
        if scope == "live" and archived:
            continue
        if scope == "box" and not archived:
            continue
        items.append({
            "id": r["id"],
            "name": r["name"],
            "created": r["made"],
            "made": r["made"],
            "last": r["last"],
            "preview": r["preview"] or "",
            "current": r["id"] == current_id,
            "archived": archived,
        })
    return items


def chat_get(chat_id: str) -> dict | None:
    with conn() as cx:
        r = cx.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    return dict(r) if r else None


def chat_rename(chat_id: str, name: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE chats SET name=? WHERE id=?", (name.strip()[:60], chat_id)
        )
    return cur.rowcount > 0


def chat_archive(chat_id: str, archived: bool = True) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE chats SET archived=? WHERE id=?",
            (1 if archived else 0, chat_id),
        )
    return cur.rowcount > 0


def chat_switch(chat_id: str) -> bool:
    if not chat_get(chat_id):
        return False
    setting_set("current_chat_id", chat_id)
    return True


def chat_branch_from_message(source_chat_id: str, message_id: str) -> dict | None:
    """复制从开头到指定消息的一条时间线，原聊天不受影响。"""
    with conn() as cx:
        source = cx.execute("SELECT * FROM chats WHERE id=?", (source_chat_id,)).fetchone()
        pivot = cx.execute("SELECT rowid,* FROM messages WHERE id=? AND chat_id=?", (message_id, source_chat_id)).fetchone()
        if not source or not pivot:
            return None
        suffix = " · 分支"
        base_name = (source["name"] or "对话").strip() or "对话"
        branch = {"id": new_id(), "name": base_name[:max(1, 60 - len(suffix))] + suffix,
                  "made": int(time.time()), "provider_id": source["provider_id"],
                  "model_id": source["model_id"], "reasoning_effort": source["reasoning_effort"],
                  "show_thinking": source["show_thinking"],
                  "prompt_cache_ttl": source["prompt_cache_ttl"]}
        cx.execute("""INSERT INTO chats
                      (id,name,made,archived,provider_id,model_id,reasoning_effort,show_thinking,prompt_cache_ttl)
                      VALUES
                      (:id,:name,:made,0,:provider_id,:model_id,:reasoning_effort,:show_thinking,:prompt_cache_ttl)""", branch)
        rows = cx.execute("SELECT rowid,* FROM messages WHERE chat_id=? AND rowid<=? ORDER BY rowid ASC",
                          (source_chat_id, pivot["rowid"])).fetchall()
        id_map: dict[str, str] = {}
        for row in rows:
            fresh = new_id(); id_map[row["id"]] = fresh
            cx.execute(
                """INSERT INTO messages
                   (id,chat_id,role,content,thinking,made,origin,display_split)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (fresh, branch["id"], row["role"], row["content"], row["thinking"],
                 row["made"], row["origin"], row["display_split"]),
            )
        for old_id, fresh in id_map.items():
            versions = cx.execute("SELECT content,reason,made FROM message_versions WHERE message_id=? ORDER BY rowid ASC", (old_id,)).fetchall()
            cx.executemany("INSERT INTO message_versions (id,message_id,content,reason,made) VALUES (?,?,?,?,?)",
                           [(new_id(), fresh, item["content"], item["reason"], item["made"]) for item in versions])
            calls = cx.execute("""SELECT name,arguments,result,is_error,made FROM tool_calls
                                  WHERE assistant_message_id=? ORDER BY rowid ASC""", (old_id,)).fetchall()
            cx.executemany("""INSERT INTO tool_calls (id,chat_id,assistant_message_id,name,arguments,result,is_error,made)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           [(new_id(), branch["id"], fresh, item["name"], item["arguments"], item["result"], item["is_error"], item["made"]) for item in calls])
            images = cx.execute(
                "SELECT kind,data_url,made FROM message_attachments WHERE message_id=? ORDER BY rowid ASC",
                (old_id,),
            ).fetchall()
            cx.executemany(
                """INSERT INTO message_attachments (id,message_id,kind,data_url,made)
                   VALUES (?,?,?,?,?)""",
                [(new_id(), fresh, item["kind"], item["data_url"], item["made"]) for item in images],
            )
        cx.execute("INSERT INTO chat_mcp_servers (chat_id,server_id) SELECT ?,server_id FROM chat_mcp_servers WHERE chat_id=?",
                   (branch["id"], source_chat_id))
        cx.execute("INSERT INTO chat_instruction_presets (chat_id,instruction_id) SELECT ?,instruction_id FROM chat_instruction_presets WHERE chat_id=?",
                   (branch["id"], source_chat_id))
    return {"id": branch["id"], "name": branch["name"], "made": branch["made"]}


def chat_model_get(chat_id: str) -> dict:
    with conn() as cx:
        row = cx.execute(
            "SELECT provider_id, model_id, reasoning_effort, show_thinking, prompt_cache_ttl FROM chats WHERE id=?",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else {
        "provider_id": "", "model_id": "", "reasoning_effort": "", "show_thinking": 1,
        "prompt_cache_ttl": "off",
    }


def chat_model_set(chat_id: str, provider_id: str | None = None,
                   model_id: str | None = None, reasoning_effort: str | None = None,
                   show_thinking: bool | None = None,
                   prompt_cache_ttl: str | None = None) -> bool:
    current = chat_model_get(chat_id)
    if not chat_get(chat_id):
        return False
    values = {
        "provider_id": current["provider_id"] if provider_id is None else provider_id,
        "model_id": current["model_id"] if model_id is None else model_id,
        "reasoning_effort": current["reasoning_effort"] if reasoning_effort is None else reasoning_effort,
        "show_thinking": current["show_thinking"] if show_thinking is None else (1 if show_thinking else 0),
        "prompt_cache_ttl": current["prompt_cache_ttl"] if prompt_cache_ttl is None else prompt_cache_ttl,
        "id": chat_id,
    }
    with conn() as cx:
        cx.execute(
            "UPDATE chats SET provider_id=:provider_id, model_id=:model_id, "
            "reasoning_effort=:reasoning_effort, show_thinking=:show_thinking, "
            "prompt_cache_ttl=:prompt_cache_ttl WHERE id=:id",
            values,
        )
    return True

def chat_split_replies_get(chat_id: str) -> bool:
    with conn() as cx:
        row = cx.execute("SELECT COALESCE(split_replies,0) AS split_replies FROM chats WHERE id=?", (chat_id,)).fetchone()
    return bool(row and row["split_replies"])


def chat_split_replies_set(chat_id: str, enabled: bool) -> bool:
    with conn() as cx:
        cur = cx.execute("UPDATE chats SET split_replies=? WHERE id=?", (1 if enabled else 0, chat_id))
    return cur.rowcount > 0


def chat_del(chat_id: str) -> bool:
    """删 chat 会级联删掉它下面所有 messages。
    如果删的是当前 wake_target，自动切到最近创建的那个。"""
    with conn() as cx:
        cur = cx.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        if cur.rowcount == 0:
            return False
        fallback = cx.execute(
            "SELECT id FROM chats ORDER BY made DESC LIMIT 1"
        ).fetchone()
        fallback_id = fallback["id"] if fallback else ""
        # 删除当前聊天或主动消息目标时，都要指向最新剩余聊天。
        for key in ("wake_target_chat_id", "current_chat_id"):
            saved = cx.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if saved and saved["value"] == chat_id:
                cx.execute(
                    "UPDATE settings SET value=? WHERE key=?", (fallback_id, key)
                )
    return True


# ---------------------------------------------------------------- 消息

def message_add(chat_id: str, role: str, content: str, made: int | None = None,
                origin: str = "chat") -> dict:
    row = {
        "id": new_id(),
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "thinking": "",
        "made": int(made if made is not None else time.time()),
        "origin": origin if origin in {"chat", "heartbeat"} else "chat",
        "display_split": 0,
        "usage_json": "{}",
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO messages
               (id,chat_id,role,content,thinking,made,origin,display_split,usage_json)
               VALUES
               (:id,:chat_id,:role,:content,:thinking,:made,:origin,:display_split,:usage_json)""",
            row,
        )
    return row


def message_attachment_add(message_id: str, data_url: str) -> dict:
    """给消息保存一张经过前端压缩的图片缩略图。"""
    if not data_url.startswith("data:image/") or len(data_url) > 700_000:
        raise ValueError("图片缩略图无效或过大")
    row = {
        "id": new_id(),
        "message_id": message_id,
        "kind": "image",
        "data_url": data_url,
        "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO message_attachments (id,message_id,kind,data_url,made)
               VALUES (:id,:message_id,:kind,:data_url,:made)""",
            row,
        )
    return row


def message_attachments(message_ids: list[str]) -> dict[str, list[str]]:
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with conn() as cx:
        rows = cx.execute(
            f"""SELECT message_id,data_url FROM message_attachments
                WHERE message_id IN ({marks}) ORDER BY made ASC, rowid ASC""",
            ids,
        ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["message_id"], []).append(row["data_url"])
    return result


def message_get(message_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT rowid,* FROM messages WHERE id=?", (message_id,)).fetchone()
    return dict(row) if row else None


def message_assistant_turn(message_id: str) -> list[dict]:
    """Return assistant chat bubbles between the surrounding user messages."""
    with conn() as cx:
        selected = cx.execute(
            "SELECT rowid, chat_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if not selected:
            return []
        lower = cx.execute(
            "SELECT COALESCE(MAX(rowid), 0) AS boundary FROM messages "
            "WHERE chat_id=? AND role='user' AND rowid<?",
            (selected["chat_id"], selected["rowid"]),
        ).fetchone()["boundary"]
        upper_row = cx.execute(
            "SELECT MIN(rowid) AS boundary FROM messages "
            "WHERE chat_id=? AND role='user' AND rowid>?",
            (selected["chat_id"], selected["rowid"]),
        ).fetchone()
        upper = upper_row["boundary"] if upper_row and upper_row["boundary"] is not None else selected["rowid"] + 10_000_000_000
        rows = cx.execute(
            "SELECT rowid, * FROM messages WHERE chat_id=? AND role='assistant' "
            "AND origin='chat' AND rowid>? AND rowid<? ORDER BY rowid",
            (selected["chat_id"], lower, upper),
        ).fetchall()
    return [dict(row) for row in rows]


def message_list(chat_id: str, limit: int = 400, before: int | None = None) -> list:
    with conn() as cx:
        if before:
            rows = cx.execute(
                "SELECT rowid, * FROM messages WHERE chat_id=? AND rowid<? "
                "ORDER BY rowid DESC LIMIT ?",
                (chat_id, before, limit),
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT rowid, * FROM messages WHERE chat_id=? "
                "ORDER BY rowid DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


def message_last_made(chat_id: str, role: str = "") -> int:
    """Return the last persisted message timestamp for heartbeat scheduling."""
    with conn() as cx:
        if role:
            row = cx.execute(
                "SELECT COALESCE(MAX(made),0) AS made FROM messages WHERE chat_id=? AND role=?",
                (chat_id, role),
            ).fetchone()
        else:
            row = cx.execute(
                "SELECT COALESCE(MAX(made),0) AS made FROM messages WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
    return int(row["made"] if row else 0)


def find_everywhere(query: str, limit: int = 80) -> list[dict]:
    """按最近更新时间翻聊天与 Dwell 里可见的文字。数据库很小，LIKE 足够稳。"""
    query = query.strip()[:60]
    if not query:
        return []
    like = f"%{query}%"

    def stamp(value: int, fallback: str = "") -> str:
        if fallback:
            return fallback
        return datetime.fromtimestamp(value, CN_TZ).strftime("%m月%d日")

    def snippet(value: str) -> str:
        text = " ".join((value or "").split())
        at = text.lower().find(query.lower())
        if at < 0:
            return text[:180]
        start = max(0, at - 42)
        end = min(len(text), at + len(query) + 110)
        return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")

    hits: list[dict] = []
    with conn() as cx:
        messages = cx.execute(
            """SELECT m.id AS message_id,m.chat_id,m.content,m.made,c.name FROM messages m JOIN chats c ON c.id=m.chat_id
               WHERE m.content LIKE ? ORDER BY m.made DESC LIMIT ?""", (like, limit)
        ).fetchall()
        diary = cx.execute(
            """SELECT date,title,body,keywords,made FROM diary
               WHERE title LIKE ? OR body LIKE ? OR keywords LIKE ?
               ORDER BY date DESC,made DESC LIMIT ?""", (like, like, like, limit)
        ).fetchall()
        personal = cx.execute("SELECT text,at FROM her_diary WHERE text LIKE ? ORDER BY at DESC LIMIT ?", (like, limit)).fetchall()
        quotes = cx.execute("SELECT date,quote,note,made FROM quotes WHERE quote LIKE ? OR note LIKE ? ORDER BY made DESC LIMIT ?", (like, like, limit)).fetchall()
        whispers = cx.execute("SELECT who,text,at FROM whispers WHERE text LIKE ? ORDER BY at DESC LIMIT ?", (like, limit)).fetchall()
        nights = cx.execute("SELECT date,hm,text,made FROM night WHERE text LIKE ? ORDER BY date DESC,hm DESC LIMIT ?", (like, limit)).fetchall()
        events = cx.execute("SELECT date,time,text,made FROM cal_events WHERE text LIKE ? ORDER BY date DESC,time DESC LIMIT ?", (like, limit)).fetchall()

    for row in messages:
        hits.append({"kind": "聊天 · " + (row["name"] or "新对话"), "date": stamp(row["made"]), "snippet": snippet(row["content"]), "at": row["made"], "chat_id": row["chat_id"], "chat_name": row["name"] or "新对话", "message_id": row["message_id"]})
    for row in diary:
        hits.append({"kind": "日记", "date": row["date"], "snippet": snippet(" ".join(filter(None, [row["title"], row["keywords"], row["body"]]))), "at": row["made"]})
    for row in personal:
        hits.append({"kind": "我的日记", "date": stamp(row["at"]), "snippet": snippet(row["text"]), "at": row["at"]})
    for row in quotes:
        hits.append({"kind": "收藏的话", "date": row["date"], "snippet": snippet(row["quote"] + " " + row["note"]), "at": row["made"]})
    for row in whispers:
        hits.append({"kind": "悄悄话 · " + ("你" if row["who"] == "her" else "Cloudy"), "date": stamp(row["at"]), "snippet": snippet(row["text"]), "at": row["at"]})
    for row in nights:
        hits.append({"kind": "夜记", "date": row["date"] + (" · " + row["hm"] if row["hm"] else ""), "snippet": snippet(row["text"]), "at": row["made"]})
    for row in events:
        hits.append({"kind": "日历", "date": row["date"] + (" · " + row["time"] if row["time"] else ""), "snippet": snippet(row["text"]), "at": row["made"]})
    return sorted(hits, key=lambda item: item["at"], reverse=True)[:max(1, min(limit, 80))]


# ---------------------------------------------------------------- 聊天长期上下文

def chat_memory_get(chat_id: str) -> dict:
    """返回派生摘要状态；不存在时给一个未启用的空状态。"""
    with conn() as cx:
        row = cx.execute(
            "SELECT * FROM chat_memory_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        total = cx.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND content<>''", (chat_id,)
        ).fetchone()[0]
        last = cx.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        segments = cx.execute(
            "SELECT COUNT(*) FROM chat_memory_segments WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        draft = cx.execute(
            "SELECT overview,through_rowid,generated_at FROM chat_memory_drafts WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        version_count = cx.execute(
            "SELECT COUNT(*) FROM chat_memory_versions WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
    out = dict(row) if row else {
        "chat_id": chat_id, "enabled": 0, "overview": "", "through_rowid": 0,
        "status": "idle", "error": "", "generated_at": 0,
    }
    out["enabled"] = bool(out["enabled"])
    out["message_count"] = int(total)
    out["last_rowid"] = int(last)
    out["segment_count"] = int(segments)
    out["has_draft"] = bool(draft)
    out["draft_overview"] = str(draft["overview"]) if draft else ""
    out["draft_through_rowid"] = int(draft["through_rowid"]) if draft else 0
    out["draft_generated_at"] = int(draft["generated_at"]) if draft else 0
    out["version_count"] = int(version_count)
    return out


def chat_memory_set_status(chat_id: str, status: str, error: str = "", enabled: bool | None = None) -> None:
    current = chat_memory_get(chat_id)
    values = {
        "chat_id": chat_id,
        "enabled": int(current["enabled"] if enabled is None else enabled),
        "overview": current["overview"],
        "through_rowid": current["through_rowid"],
        "status": status,
        "error": error[:1000],
        "generated_at": current["generated_at"],
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO chat_memory_state
               (chat_id,enabled,overview,through_rowid,status,error,generated_at)
               VALUES (:chat_id,:enabled,:overview,:through_rowid,:status,:error,:generated_at)
               ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,
               status=excluded.status,error=excluded.error""",
            values,
        )


def chat_memory_reset(chat_id: str) -> None:
    """让重建从原文开始；正式记忆和历史版本始终保留到用户采用新草稿。"""
    with conn() as cx:
        cx.execute("DELETE FROM chat_memory_segments WHERE chat_id=?", (chat_id,))
        cx.execute("DELETE FROM chat_memory_drafts WHERE chat_id=?", (chat_id,))
        cx.execute(
            """INSERT INTO chat_memory_state
               (chat_id,enabled,overview,through_rowid,status,error,generated_at)
               VALUES (?,1,'',0,'queued','',0)
               ON CONFLICT(chat_id) DO UPDATE SET enabled=1,status='queued',error=''""",
            (chat_id,),
        )

def chat_memory_add_segment(chat_id: str, start_rowid: int, end_rowid: int, content: str) -> dict:
    row = {
        "id": new_id(), "chat_id": chat_id, "start_rowid": start_rowid,
        "end_rowid": end_rowid, "content": content.strip()[:12000], "made": int(time.time()),
    }
    with conn() as cx:
        cx.execute(
            """INSERT INTO chat_memory_segments (id,chat_id,start_rowid,end_rowid,content,made)
               VALUES (:id,:chat_id,:start_rowid,:end_rowid,:content,:made)""", row
        )
    return row


def chat_memory_segments(chat_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM chat_memory_segments WHERE chat_id=? ORDER BY start_rowid ASC",
            (chat_id,),
        ).fetchall()
    return [dict(row) for row in rows]


CHAT_MEMORY_VERSION_LIMIT = 10


def _chat_memory_prune_versions(
    cx: sqlite3.Connection, chat_id: str, keep: int = CHAT_MEMORY_VERSION_LIMIT
) -> None:
    """每个聊天只保留最近的摘要版本，避免历史无限增长。"""
    rows = cx.execute(
        """SELECT id FROM chat_memory_versions
           WHERE chat_id=? ORDER BY made DESC, rowid DESC""",
        (chat_id,),
    ).fetchall()
    stale = rows[max(1, int(keep)):]
    if stale:
        cx.executemany(
            "DELETE FROM chat_memory_versions WHERE id=?",
            [(row["id"],) for row in stale],
        )


def _chat_memory_archive(cx: sqlite3.Connection, current: dict, reason: str) -> None:
    overview = str(current.get("overview") or "").strip()
    if not overview:
        return
    cx.execute(
        """INSERT INTO chat_memory_versions (id,chat_id,overview,through_rowid,reason,made)
           VALUES (?,?,?,?,?,?)""",
        (
            new_id(), current["chat_id"], overview, int(current.get("through_rowid") or 0),
            reason[:80], int(time.time()),
        ),
    )
    _chat_memory_prune_versions(cx, current["chat_id"])


def chat_memory_stage(chat_id: str, overview: str, through_rowid: int) -> None:
    """保存待确认草稿；Cloudy 继续使用原来的正式记忆。"""
    now = int(time.time())
    with conn() as cx:
        cx.execute(
            """INSERT INTO chat_memory_drafts (chat_id,overview,through_rowid,generated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET overview=excluded.overview,
               through_rowid=excluded.through_rowid,generated_at=excluded.generated_at""",
            (chat_id, overview.strip()[:12000], int(through_rowid), now),
        )
        cx.execute(
            """INSERT INTO chat_memory_state
               (chat_id,enabled,overview,through_rowid,status,error,generated_at)
               VALUES (?,1,'',0,'review','',0)
               ON CONFLICT(chat_id) DO UPDATE SET enabled=1,status='review',error=''""",
            (chat_id,),
        )


def chat_memory_accept_draft(chat_id: str, overview: str) -> None:
    """采用用户审过的草稿，并在替换前保存当前正式版本。"""
    current = chat_memory_get(chat_id)
    now = int(time.time())
    with conn() as cx:
        draft = cx.execute(
            "SELECT through_rowid FROM chat_memory_drafts WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if not draft:
            raise ValueError("没有待确认的长期记忆草稿")
        chosen = overview.strip()[:12000]
        if chosen != str(current.get("overview") or "").strip():
            _chat_memory_archive(cx, current, "采用新草稿")
        cx.execute(
            """INSERT INTO chat_memory_state
               (chat_id,enabled,overview,through_rowid,status,error,generated_at)
               VALUES (?,1,?,?,'ready','',?)
               ON CONFLICT(chat_id) DO UPDATE SET enabled=1,overview=excluded.overview,
               through_rowid=excluded.through_rowid,status='ready',error='',
               generated_at=excluded.generated_at""",
            (chat_id, chosen, int(draft["through_rowid"]), now),
        )
        cx.execute("DELETE FROM chat_memory_drafts WHERE chat_id=?", (chat_id,))


def chat_memory_discard_draft(chat_id: str) -> None:
    current = chat_memory_get(chat_id)
    with conn() as cx:
        cx.execute("DELETE FROM chat_memory_drafts WHERE chat_id=?", (chat_id,))
        cx.execute(
            "UPDATE chat_memory_state SET status=?,error='' WHERE chat_id=?",
            ("ready" if current.get("overview") else "idle", chat_id),
        )


def chat_memory_save_overview(chat_id: str, overview: str) -> None:
    """保存用户直接编辑的正式记忆，并在替换前保留旧版本。"""
    current = chat_memory_get(chat_id)
    chosen = overview.strip()[:12000]
    now = int(time.time())
    with conn() as cx:
        if chosen != str(current.get("overview") or "").strip():
            _chat_memory_archive(cx, current, "手动编辑")
        cx.execute(
            """INSERT INTO chat_memory_state
               (chat_id,enabled,overview,through_rowid,status,error,generated_at)
               VALUES (?,1,?,?,'ready','',?)
               ON CONFLICT(chat_id) DO UPDATE SET enabled=1,overview=excluded.overview,
               status='ready',error='',generated_at=excluded.generated_at""",
            (chat_id, chosen, int(current["through_rowid"]), now),
        )


def chat_memory_versions(
    chat_id: str, limit: int = CHAT_MEMORY_VERSION_LIMIT
) -> list[dict]:
    with conn() as cx:
        _chat_memory_prune_versions(cx, chat_id)
        rows = cx.execute(
            """SELECT id,overview,through_rowid,reason,made FROM chat_memory_versions
               WHERE chat_id=? ORDER BY made DESC, rowid DESC LIMIT ?""",
            (chat_id, max(1, min(int(limit), CHAT_MEMORY_VERSION_LIMIT))),
        ).fetchall()
    return [dict(row) for row in rows]


def chat_memory_restore_version(chat_id: str, version_id: str) -> None:
    """恢复旧文本；保留当前覆盖水位，避免重复压缩已经处理过的消息。"""
    current = chat_memory_get(chat_id)
    now = int(time.time())
    with conn() as cx:
        version = cx.execute(
            "SELECT overview FROM chat_memory_versions WHERE id=? AND chat_id=?",
            (version_id, chat_id),
        ).fetchone()
        if not version:
            raise ValueError("没有找到这个长期记忆版本")
        restored = str(version["overview"]).strip()[:12000]
        if restored != str(current.get("overview") or "").strip():
            _chat_memory_archive(cx, current, "恢复旧版前")
        cx.execute(
            """UPDATE chat_memory_state SET enabled=1,overview=?,status='ready',
               error='',generated_at=? WHERE chat_id=?""",
            (restored, now, chat_id),
        )
        cx.execute("DELETE FROM chat_memory_drafts WHERE chat_id=?", (chat_id,))


def chat_memory_source_messages(chat_id: str, after_rowid: int, before_rowid: int, limit: int) -> list[dict]:
    """取尚未进入摘要的原始消息，按时间顺序，含 rowid 供水位线追踪。"""
    with conn() as cx:
        rows = cx.execute(
            """SELECT rowid,id,role,content,made FROM messages
               WHERE chat_id=? AND rowid>? AND rowid<=? AND content<>''
               ORDER BY rowid ASC LIMIT ?""",
            (chat_id, int(after_rowid), int(before_rowid), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------- 聊天记忆卡片

def _memory_card_dict(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    try:
        topics = json.loads(item.pop("topics_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        topics = []
    item["topics"] = [str(topic) for topic in topics if str(topic).strip()]
    return item


def memory_card_state_get(chat_id: str) -> dict:
    with conn() as cx:
        row = cx.execute(
            "SELECT status,error,generated_at FROM memory_card_state WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        draft_count = cx.execute(
            "SELECT COUNT(*) FROM memory_card_drafts WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
        card_count = cx.execute(
            "SELECT COUNT(*) FROM memory_cards WHERE chat_id=? AND status<>'archived'", (chat_id,)
        ).fetchone()[0]
    out = dict(row) if row else {"status": "idle", "error": "", "generated_at": 0}
    out["draft_count"] = int(draft_count)
    out["card_count"] = int(card_count)
    return out


def memory_card_state_set(chat_id: str, status: str, error: str = "", generated: bool = False) -> None:
    now = int(time.time()) if generated else 0
    with conn() as cx:
        cx.execute(
            """INSERT INTO memory_card_state (chat_id,status,error,generated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET status=excluded.status,error=excluded.error,
               generated_at=CASE WHEN excluded.generated_at>0 THEN excluded.generated_at
                                 ELSE memory_card_state.generated_at END""",
            (chat_id, status[:40], error[:1000], now),
        )


def memory_card_list(chat_id: str, include_archived: bool = False) -> list[dict]:
    with conn() as cx:
        if include_archived:
            rows = cx.execute(
                "SELECT * FROM memory_cards WHERE chat_id=? ORDER BY updated DESC, rowid DESC",
                (chat_id,),
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT * FROM memory_cards WHERE chat_id=? AND status<>'archived' "
                "ORDER BY updated DESC, rowid DESC",
                (chat_id,),
            ).fetchall()
    return [_memory_card_dict(row) for row in rows]


def memory_card_get(chat_id: str, card_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute(
            "SELECT * FROM memory_cards WHERE id=? AND chat_id=?", (card_id, chat_id)
        ).fetchone()
    return _memory_card_dict(row)


def memory_card_draft_list(chat_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM memory_card_drafts WHERE chat_id=? ORDER BY made ASC, rowid ASC",
            (chat_id,),
        ).fetchall()
    return [_memory_card_dict(row) for row in rows]


def memory_card_stage(chat_id: str, proposals: list[dict]) -> int:
    """保存模型建议；相同内容的正式卡片或待审草稿不会重复出现。"""
    now = int(time.time())
    inserted = 0
    with conn() as cx:
        existing = {
            re.sub(r"\s+", "", str(row["content"])).casefold()
            for row in cx.execute(
                "SELECT content FROM memory_cards WHERE chat_id=? AND status<>'archived' "
                "UNION ALL SELECT content FROM memory_card_drafts WHERE chat_id=?",
                (chat_id, chat_id),
            ).fetchall()
        }
        for proposal in proposals[:40]:
            content = str(proposal.get("content") or "").strip()[:1200]
            fingerprint = re.sub(r"\s+", "", content).casefold()
            if not content or fingerprint in existing:
                continue
            segment_id = str(proposal.get("source_segment_id") or "") or None
            if segment_id:
                segment = cx.execute(
                    "SELECT start_rowid,end_rowid FROM chat_memory_segments WHERE id=? AND chat_id=?",
                    (segment_id, chat_id),
                ).fetchone()
                if not segment:
                    continue
                source_start = int(segment["start_rowid"])
                source_end = int(segment["end_rowid"])
            else:
                source_start = max(0, int(proposal.get("source_start_rowid") or 0))
                source_end = max(source_start, int(proposal.get("source_end_rowid") or 0))
            row = {
                "id": new_id(), "chat_id": chat_id, "action": "create", "target_card_id": None,
                "content": content, "memory_type": str(proposal.get("memory_type") or "stable_fact")[:40],
                "topics_json": json.dumps(proposal.get("topics") or [], ensure_ascii=False),
                "importance": str(proposal.get("importance") or "normal")[:20],
                "retention": str(proposal.get("retention") or "long_term")[:20],
                "valid_until": proposal.get("valid_until") or None, "surface_scope": "chat_only",
                "source_segment_id": segment_id, "source_start_rowid": source_start,
                "source_end_rowid": source_end, "made": now,
            }
            cx.execute(
                """INSERT INTO memory_card_drafts
                   (id,chat_id,action,target_card_id,content,memory_type,topics_json,importance,
                    retention,valid_until,surface_scope,source_segment_id,source_start_rowid,
                    source_end_rowid,made)
                   VALUES (:id,:chat_id,:action,:target_card_id,:content,:memory_type,:topics_json,
                           :importance,:retention,:valid_until,:surface_scope,:source_segment_id,
                           :source_start_rowid,:source_end_rowid,:made)""",
                row,
            )
            existing.add(fingerprint)
            inserted += 1
    return inserted


def memory_card_draft_accept(chat_id: str, draft_id: str, chosen: dict) -> dict:
    """采用一条经过用户确认或编辑的建议。第一阶段只生成 create 建议。"""
    now = int(time.time())
    with conn() as cx:
        draft = cx.execute(
            "SELECT * FROM memory_card_drafts WHERE id=? AND chat_id=?", (draft_id, chat_id)
        ).fetchone()
        if not draft:
            raise ValueError("没有找到这条待确认记忆")
        if draft["action"] != "create":
            raise ValueError("暂不支持这种记忆变更")
        row = {
            "id": new_id(), "chat_id": chat_id,
            "content": str(chosen["content"]).strip()[:1200],
            "memory_type": str(chosen["memory_type"])[:40],
            "topics_json": json.dumps(chosen.get("topics") or [], ensure_ascii=False),
            "importance": str(chosen["importance"])[:20],
            "retention": str(chosen["retention"])[:20],
            "valid_until": chosen.get("valid_until") or None,
            "surface_scope": "chat_only", "status": "active",
            "source_segment_id": draft["source_segment_id"],
            "source_start_rowid": int(draft["source_start_rowid"]),
            "source_end_rowid": int(draft["source_end_rowid"]),
            "made": now, "updated": now,
        }
        cx.execute(
            """INSERT INTO memory_cards
               (id,chat_id,content,memory_type,topics_json,importance,retention,valid_until,
                surface_scope,status,source_segment_id,source_start_rowid,source_end_rowid,made,updated)
               VALUES (:id,:chat_id,:content,:memory_type,:topics_json,:importance,:retention,
                       :valid_until,:surface_scope,:status,:source_segment_id,:source_start_rowid,
                       :source_end_rowid,:made,:updated)""",
            row,
        )
        cx.execute("DELETE FROM memory_card_drafts WHERE id=?", (draft_id,))
    return memory_card_get(chat_id, row["id"])


def memory_card_draft_discard(chat_id: str, draft_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "DELETE FROM memory_card_drafts WHERE id=? AND chat_id=?", (draft_id, chat_id)
        )
    return cur.rowcount > 0


def memory_card_update(chat_id: str, card_id: str, chosen: dict) -> dict:
    now = int(time.time())
    with conn() as cx:
        cur = cx.execute(
            """UPDATE memory_cards SET content=?,memory_type=?,topics_json=?,importance=?,
               retention=?,valid_until=?,status=?,updated=? WHERE id=? AND chat_id=?""",
            (
                str(chosen["content"]).strip()[:1200], str(chosen["memory_type"])[:40],
                json.dumps(chosen.get("topics") or [], ensure_ascii=False),
                str(chosen["importance"])[:20], str(chosen["retention"])[:20],
                chosen.get("valid_until") or None, str(chosen.get("status") or "active")[:20],
                now, card_id, chat_id,
            ),
        )
    if not cur.rowcount:
        raise ValueError("没有找到这张记忆卡片")
    return memory_card_get(chat_id, card_id)


def memory_card_unprocessed_segments(chat_id: str) -> list[dict]:
    """每个原文范围只返回一次；同范围任一分段处理过就不再重复产卡。"""
    with conn() as cx:
        rows = cx.execute(
            """SELECT segment.* FROM chat_memory_segments AS segment
               WHERE segment.chat_id=?
               ORDER BY segment.start_rowid ASC,segment.end_rowid ASC,segment.rowid ASC""",
            (chat_id,),
        ).fetchall()
        processed_rows = cx.execute(
            """SELECT source.start_rowid,source.end_rowid
               FROM memory_card_segment_runs AS run
               JOIN chat_memory_segments AS source ON source.id=run.segment_id
               WHERE source.chat_id=?""",
            (chat_id,),
        ).fetchall()
    processed_ranges = {
        (int(row["start_rowid"]), int(row["end_rowid"])) for row in processed_rows
    }
    seen_ranges: set[tuple[int, int]] = set()
    pending = []
    for row in rows:
        key = (int(row["start_rowid"]), int(row["end_rowid"]))
        if key in processed_ranges or key in seen_ranges:
            continue
        seen_ranges.add(key)
        pending.append(dict(row))
    return pending


def memory_card_segment_mark(
    chat_id: str, segment_id: str, candidate_count: int = 0
) -> None:
    with conn() as cx:
        segment = cx.execute(
            "SELECT 1 FROM chat_memory_segments WHERE id=? AND chat_id=?",
            (segment_id, chat_id),
        ).fetchone()
        if not segment:
            raise ValueError("没有找到这段长期上下文")
        cx.execute(
            """INSERT INTO memory_card_segment_runs
               (segment_id,chat_id,candidate_count,processed_at) VALUES (?,?,?,?)
               ON CONFLICT(segment_id) DO UPDATE SET
               candidate_count=excluded.candidate_count,
               processed_at=excluded.processed_at""",
            (segment_id, chat_id, max(0, int(candidate_count)), int(time.time())),
        )


def memory_card_archive(chat_id: str, card_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE memory_cards SET status='archived',updated=? WHERE id=? AND chat_id=?",
            (int(time.time()), card_id, chat_id),
        )
    return cur.rowcount > 0


def memory_card_delete_permanently(chat_id: str, card_id: str) -> bool:
    """Permanently delete an already archived card and its usage audit snapshots."""
    with conn() as cx:
        cur = cx.execute(
            "DELETE FROM memory_cards WHERE id=? AND chat_id=? AND status='archived'",
            (card_id, chat_id),
        )
    return cur.rowcount > 0


def memory_card_injection_enabled(chat_id: str) -> bool:
    return setting_get(f"memory_cards_enabled:{chat_id}", "1") != "0"


def memory_card_injection_set(chat_id: str, enabled: bool) -> None:
    setting_set(f"memory_cards_enabled:{chat_id}", "1" if enabled else "0")


def memory_card_usage_record(
    chat_id: str, response_message_id: str, query: str, cards: list[dict]
) -> None:
    """Replace the usage audit for one response with the exact selected snapshots."""
    now = int(time.time())
    with conn() as cx:
        cx.execute(
            "DELETE FROM memory_card_uses WHERE response_message_id=? AND chat_id=?",
            (response_message_id, chat_id),
        )
        for card in cards[:5]:
            metadata = {
                "memory_type": card.get("memory_type"),
                "topics": card.get("topics") or [],
                "importance": card.get("importance"),
                "retention": card.get("retention"),
                "valid_until": card.get("valid_until"),
                "source_start_rowid": card.get("source_start_rowid") or 0,
                "source_end_rowid": card.get("source_end_rowid") or 0,
            }
            cx.execute(
                """INSERT INTO memory_card_uses
                   (id,chat_id,card_id,response_message_id,content_snapshot,metadata_json,
                    query_excerpt,score,used_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    new_id(), chat_id, card["id"], response_message_id,
                    str(card.get("content") or "")[:1200],
                    json.dumps(metadata, ensure_ascii=False), str(query or "")[-500:],
                    float(card.get("selection_score") or 0), now,
                ),
            )


def memory_card_last_injection(chat_id: str) -> dict | None:
    with conn() as cx:
        latest = cx.execute(
            """SELECT response_message_id,MAX(used_at) AS used_at
               FROM memory_card_uses WHERE chat_id=? GROUP BY response_message_id
               ORDER BY used_at DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if not latest:
            return None
        rows = cx.execute(
            """SELECT card_id,content_snapshot,metadata_json,score,used_at
               FROM memory_card_uses WHERE chat_id=? AND response_message_id=?
               ORDER BY score DESC,rowid ASC""",
            (chat_id, latest["response_message_id"]),
        ).fetchall()
    items = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        items.append({
            "id": row["card_id"], "content": row["content_snapshot"],
            **metadata, "selection_score": float(row["score"]),
        })
    return {
        "response_message_id": latest["response_message_id"],
        "used_at": int(latest["used_at"]), "items": items,
    }


SYSTEM_LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60
SYSTEM_LOG_MAX_ITEMS = 1000


def _system_log_prune(cx: sqlite3.Connection) -> None:
    cutoff = int(time.time()) - SYSTEM_LOG_RETENTION_SECONDS
    cx.execute("DELETE FROM system_logs WHERE made<?", (cutoff,))
    cx.execute(
        """DELETE FROM system_logs WHERE id IN (
               SELECT id FROM system_logs
               ORDER BY made DESC,rowid DESC LIMIT -1 OFFSET ?
           )""",
        (SYSTEM_LOG_MAX_ITEMS,),
    )


def system_log_start(
    category: str,
    action: str,
    *,
    chat_id: str = "",
    message_id: str = "",
    provider: str = "",
    model_id: str = "",
) -> str:
    """开始一条诊断记录；正文、图片和认证信息不得传入。"""
    log_id = new_id()
    now = int(time.time())
    with conn() as cx:
        cx.execute(
            """INSERT INTO system_logs
               (id,category,action,status,chat_id,message_id,provider,model_id,made)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                log_id, str(category)[:40], str(action)[:120], "running",
                str(chat_id)[:80], str(message_id)[:80], str(provider)[:120],
                str(model_id)[:200], now,
            ),
        )
        _system_log_prune(cx)
    return log_id


def system_log_finish(
    log_id: str,
    status: str,
    duration_ms: int,
    *,
    status_code: int | None = None,
    detail: str = "",
) -> None:
    with conn() as cx:
        cx.execute(
            """UPDATE system_logs SET status=?,duration_ms=?,status_code=?,detail=?,
               finished_at=? WHERE id=?""",
            (
                str(status)[:30], max(0, int(duration_ms)),
                int(status_code) if status_code is not None else None,
                str(detail).replace("\r", " ").replace("\n", " ")[:500],
                int(time.time()), log_id,
            ),
        )


def system_log_list(
    *, category: str = "", status: str = "", limit: int = 300
) -> list[dict]:
    clauses = []
    values: list[object] = []
    if category:
        clauses.append("category=?")
        values.append(str(category))
    if status:
        clauses.append("status=?")
        values.append(str(status))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    with conn() as cx:
        _system_log_prune(cx)
        rows = cx.execute(
            """SELECT id,category,action,status,chat_id,message_id,provider,model_id,
                      duration_ms,status_code,detail,made,finished_at
               FROM system_logs"""
            + where + " ORDER BY made DESC,rowid DESC LIMIT ?",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def system_log_clear() -> int:
    with conn() as cx:
        cur = cx.execute("DELETE FROM system_logs")
    return cur.rowcount


def message_ui_list(chat_id: str, limit: int = 400, before: int | None = None) -> dict:
    rows = message_list(chat_id, limit, before)
    # The toggle controls future generation only. Persisted thinking remains part of history.
    assistant_ids = [row["id"] for row in rows if row["role"] == "assistant"]
    images_by_message = message_attachments([row["id"] for row in rows])
    tools_by_message: dict[str, list[dict]] = {}
    if assistant_ids:
        placeholders = ",".join("?" for _ in assistant_ids)
        with conn() as cx:
            tool_rows = cx.execute(
                f"SELECT * FROM tool_calls WHERE assistant_message_id IN ({placeholders}) ORDER BY made ASC, rowid ASC",
                assistant_ids,
            ).fetchall()
        for tool in tool_rows:
            item = dict(tool)
            tools_by_message.setdefault(item["assistant_message_id"], []).append(item)
    msgs = []
    for r in rows:
        role = r["role"]
        usage = {}
        if role == "assistant":
            try:
                parsed_usage = json.loads(r["usage_json"] or "{}")
                if isinstance(parsed_usage, dict):
                    usage = parsed_usage
            except (TypeError, json.JSONDecodeError):
                usage = {}
        msgs.append({
            "seq": r["rowid"],
            "id": r["id"],
            "kind": "me" if role == "user" else ("gu" if role == "assistant" else "system"),
            "role": role,
            "text": r["content"],
            "content": r["content"],
            "thinking": r["thinking"] if role == "assistant" else "",
            "at": r["made"],
            "origin": r["origin"],
            "display_split": bool(r["display_split"]),
            "usage": usage,
            "tools": tools_by_message.get(r["id"], []) if role == "assistant" else [],
            "images": images_by_message.get(r["id"], []),
        })
    more = False
    if msgs:
        with conn() as cx:
            r = cx.execute(
                "SELECT 1 FROM messages WHERE chat_id=? AND rowid<? LIMIT 1",
                (chat_id, msgs[0]["seq"]),
            ).fetchone()
        more = bool(r)
    return {"msgs": msgs, "more": more, "upto": msgs[-1]["seq"] if msgs else message_max_id(chat_id)}


# ---------------------------------------------------------------- 设置

def setting_get(key: str, default: str = "") -> str:
    with conn() as cx:
        r = cx.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def setting_set(key: str, value: str) -> None:
    with conn() as cx:
        cx.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- 模型供应商

def provider_list() -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT id,name,base_url,provider_type,prompt_cache_ttl,"
            "enabled,made,updated,api_key_box FROM provider_profiles ORDER BY made ASC"
        ).fetchall()
    public_keys = (
        "id", "name", "base_url", "provider_type", "prompt_cache_ttl",
        "enabled", "made", "updated",
    )
    return [
        {**{key: row[key] for key in public_keys}, "has_key": bool(row["api_key_box"])}
        for row in rows
    ]


def provider_get(provider_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM provider_profiles WHERE id=?", (provider_id,)).fetchone()
    return dict(row) if row else None


def provider_upsert(provider_id: str, name: str, base_url: str, api_key_box: str | None,
                    enabled: bool = True, provider_type: str = "generic",
                    prompt_cache_ttl: str = "off") -> dict:
    now = int(time.time())
    row = provider_get(provider_id) if provider_id else None
    provider_id = provider_id or new_id()
    box = row["api_key_box"] if row and api_key_box is None else (api_key_box or "")
    provider_type = (
        provider_type
        if provider_type in {"generic", "openrouter", "claude_compatible"}
        else "generic"
    )
    # Kept in the row for backwards compatibility; cache duration is chat-scoped.
    prompt_cache_ttl = "off"
    with conn() as cx:
        cx.execute(
            "INSERT INTO provider_profiles "
            "(id,name,base_url,api_key_box,provider_type,prompt_cache_ttl,enabled,made,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name,base_url=excluded.base_url,api_key_box=excluded.api_key_box,"
            "provider_type=excluded.provider_type,prompt_cache_ttl=excluded.prompt_cache_ttl,"
            "enabled=excluded.enabled,updated=excluded.updated",
            (
                provider_id, name, base_url, box, provider_type, prompt_cache_ttl,
                1 if enabled else 0, now, now,
            ),
        )
    return provider_get(provider_id) or {}


def provider_delete(provider_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM provider_profiles WHERE id=?", (provider_id,))
    return cur.rowcount > 0


def provider_in_use(provider_id: str) -> bool:
    with conn() as cx:
        row = cx.execute(
            "SELECT 1 FROM chats WHERE provider_id=? LIMIT 1", (provider_id,)
        ).fetchone()
    return bool(row)


def provider_model_list(provider_id: str = "") -> list[dict]:
    with conn() as cx:
        if provider_id:
            rows = cx.execute(
                "SELECT * FROM provider_models WHERE provider_id=? ORDER BY favorite DESC, model_id COLLATE NOCASE",
                (provider_id,),
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT * FROM provider_models ORDER BY favorite DESC, updated DESC, model_id COLLATE NOCASE"
            ).fetchall()
    return [{**dict(row), "favorite": bool(row["favorite"]), "manual": bool(row["manual"])} for row in rows]


def provider_model_upsert(provider_id: str, model_id: str, favorite: bool | None = None,
                          manual: bool | None = None) -> dict:
    now = int(time.time())
    old = next((item for item in provider_model_list(provider_id) if item["model_id"] == model_id), None)
    fav = int(favorite if favorite is not None else (old or {}).get("favorite", False))
    is_manual = int(manual if manual is not None else (old or {}).get("manual", False))
    with conn() as cx:
        cx.execute(
            "INSERT INTO provider_models (provider_id,model_id,favorite,manual,made,updated) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(provider_id,model_id) DO UPDATE SET favorite=excluded.favorite,manual=excluded.manual,updated=excluded.updated",
            (provider_id, model_id, fav, is_manual, now, now),
        )
        row = cx.execute("SELECT * FROM provider_models WHERE provider_id=? AND model_id=?", (provider_id, model_id)).fetchone()
    return {**dict(row), "favorite": bool(row["favorite"]), "manual": bool(row["manual"])}


def provider_models_refresh(provider_id: str, model_ids: list[str]) -> int:
    for model_id in dict.fromkeys(model_ids):
        provider_model_upsert(provider_id, model_id, manual=False)
    return len(list(dict.fromkeys(model_ids)))


# ---------------------------------------------------------------- MCP 工具服务器

def mcp_server_list() -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT id,name,url,transport,enabled,made,updated,headers_box FROM mcp_servers "
            "ORDER BY made ASC"
        ).fetchall()
    return [{**{k: row[k] for k in ("id", "name", "url", "transport", "enabled", "made", "updated")},
             "has_credentials": bool(row["headers_box"])} for row in rows]


def mcp_server_get(server_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
    return dict(row) if row else None


def mcp_server_upsert(server_id: str, name: str, url: str, transport: str,
                      headers_box: str | None, enabled: bool = True) -> dict:
    now = int(time.time())
    old = mcp_server_get(server_id) if server_id else None
    server_id = server_id or new_id()
    box = old["headers_box"] if old and headers_box is None else (headers_box or "")
    with conn() as cx:
        cx.execute(
            "INSERT INTO mcp_servers (id,name,url,transport,headers_box,enabled,made,updated) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name,url=excluded.url,transport=excluded.transport,"
            "headers_box=excluded.headers_box,enabled=excluded.enabled,updated=excluded.updated",
            (server_id, name, url, transport, box, 1 if enabled else 0, now, now),
        )
    return mcp_server_get(server_id) or {}


def mcp_server_delete(server_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
    return cur.rowcount > 0


def chat_mcp_server_ids(chat_id: str) -> list[str]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT server_id FROM chat_mcp_servers WHERE chat_id=? ORDER BY server_id", (chat_id,)
        ).fetchall()
    return [row["server_id"] for row in rows]


def chat_mcp_servers(chat_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT s.* FROM mcp_servers s JOIN chat_mcp_servers c ON c.server_id=s.id "
            "WHERE c.chat_id=? AND s.enabled=1 ORDER BY s.made ASC", (chat_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def chat_mcp_servers_set(chat_id: str, server_ids: list[str]) -> bool:
    if not chat_get(chat_id):
        return False
    clean = list(dict.fromkeys(server_id for server_id in server_ids if mcp_server_get(server_id)))
    with conn() as cx:
        cx.execute("DELETE FROM chat_mcp_servers WHERE chat_id=?", (chat_id,))
        cx.executemany(
            "INSERT INTO chat_mcp_servers (chat_id,server_id) VALUES (?,?)",
            [(chat_id, server_id) for server_id in clean],
        )
    return True


def chat_home_todos_enabled(chat_id: str) -> bool:
    with conn() as cx:
        row = cx.execute(
            "SELECT todos_enabled FROM chat_home_tools WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row and row["todos_enabled"])


def chat_home_todos_set(chat_id: str, enabled: bool) -> bool:
    if not chat_get(chat_id):
        return False
    with conn() as cx:
        cx.execute(
            "INSERT INTO chat_home_tools (chat_id,todos_enabled) VALUES (?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET todos_enabled=excluded.todos_enabled",
            (chat_id, 1 if enabled else 0),
        )
    return True


# ---------------------------------------------------------------- 聊天指令

def instruction_list() -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT id,name,content,made,updated FROM instruction_presets ORDER BY made ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def instruction_get(instruction_id: str) -> dict | None:
    with conn() as cx:
        row = cx.execute("SELECT * FROM instruction_presets WHERE id=?", (instruction_id,)).fetchone()
    return dict(row) if row else None


def instruction_upsert(instruction_id: str, name: str, content: str) -> dict:
    now = int(time.time())
    instruction_id = instruction_id or new_id()
    with conn() as cx:
        cx.execute(
            "INSERT INTO instruction_presets (id,name,content,made,updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,content=excluded.content,updated=excluded.updated",
            (instruction_id, name, content, now, now),
        )
    return instruction_get(instruction_id) or {}


def instruction_delete(instruction_id: str) -> bool:
    with conn() as cx:
        cur = cx.execute("DELETE FROM instruction_presets WHERE id=?", (instruction_id,))
    return cur.rowcount > 0


def chat_instruction_ids(chat_id: str) -> list[str]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT instruction_id FROM chat_instruction_presets WHERE chat_id=? ORDER BY instruction_id",
            (chat_id,),
        ).fetchall()
    return [row["instruction_id"] for row in rows]


def chat_instructions(chat_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT i.* FROM instruction_presets i JOIN chat_instruction_presets c "
            "ON c.instruction_id=i.id WHERE c.chat_id=? ORDER BY i.made ASC", (chat_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def chat_instructions_set(chat_id: str, instruction_ids: list[str]) -> bool:
    if not chat_get(chat_id):
        return False
    clean = list(dict.fromkeys(item_id for item_id in instruction_ids if instruction_get(item_id)))
    with conn() as cx:
        cx.execute("DELETE FROM chat_instruction_presets WHERE chat_id=?", (chat_id,))
        cx.executemany(
            "INSERT INTO chat_instruction_presets (chat_id,instruction_id) VALUES (?,?)",
            [(chat_id, item_id) for item_id in clean],
        )
    return True
# ---------------------------------------------------------------- 消息 seq

def message_max_id(chat_id: str) -> int:
    """按 rowid 拿最大值，当增量游标用。"""
    with conn() as cx:
        r = cx.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM messages WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    return int(r[0])


def message_since(chat_id: str, since: int, limit: int = 200) -> list:
    with conn() as cx:
        rows = cx.execute(
            "SELECT rowid, id, chat_id, role, content, made "
            "FROM messages WHERE chat_id=? AND rowid>? "
            "ORDER BY rowid ASC LIMIT ?",
            (chat_id, since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def message_update(msg_id: str, content: str) -> bool:
    with conn() as cx:
        cur = cx.execute("UPDATE messages SET content=? WHERE id=?", (content, msg_id))
    return cur.rowcount > 0


def message_usage_update(msg_id: str, usage: dict) -> bool:
    """Save provider-reported usage only; callers must not synthesize estimates."""
    allowed = (
        "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
        "cache_write_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
        "reasoning_tokens", "cost", "upstream_cost", "duration_ms",
        "model_duration_ms", "tokens_per_second",
    )
    clean = {}
    for key in allowed:
        value = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(value, (int, float)) and value >= 0:
            if isinstance(value, float):
                clean[key] = round(value, 8 if key in {"cost", "upstream_cost"} else 2)
            else:
                clean[key] = value
    if isinstance(usage, dict):
        tts_turn_id = usage.get("tts_turn_id")
        if isinstance(tts_turn_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", tts_turn_id):
            clean["tts_turn_id"] = tts_turn_id
    with conn() as cx:
        cur = cx.execute(
            "UPDATE messages SET usage_json=? WHERE id=?",
            (json.dumps(clean, ensure_ascii=False, separators=(",", ":")), msg_id),
        )
    return cur.rowcount > 0


def message_thinking_update(msg_id: str, thinking: str) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE messages SET thinking=? WHERE id=?",
            (thinking[:200_000], msg_id),
        )
    return cur.rowcount > 0


def message_display_split_set(msg_id: str, enabled: bool) -> bool:
    with conn() as cx:
        cur = cx.execute(
            "UPDATE messages SET display_split=? WHERE id=?",
            (1 if enabled else 0, msg_id),
        )
    return cur.rowcount > 0


def message_version_add(message_id: str, content: str, reason: str = "") -> dict:
    row = {"id": new_id(), "message_id": message_id, "content": content,
           "reason": reason, "made": int(time.time())}
    with conn() as cx:
        cx.execute(
            "INSERT INTO message_versions (id,message_id,content,reason,made) "
            "VALUES (:id,:message_id,:content,:reason,:made)", row,
        )
    return row


def message_versions(message_id: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT * FROM message_versions WHERE message_id=? ORDER BY made DESC, rowid DESC",
            (message_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def tool_call_add(chat_id: str, assistant_message_id: str, name: str, arguments: str) -> dict:
    row = {"id": new_id(), "chat_id": chat_id, "assistant_message_id": assistant_message_id,
           "name": name, "arguments": arguments, "result": "", "is_error": 0,
           "made": int(time.time())}
    with conn() as cx:
        cx.execute(
            "INSERT INTO tool_calls (id,chat_id,assistant_message_id,name,arguments,result,is_error,made) "
            "VALUES (:id,:chat_id,:assistant_message_id,:name,:arguments,:result,:is_error,:made)", row,
        )
    return row


def tool_call_finish(tool_call_id: str, result: str, is_error: bool) -> bool:
    with conn() as cx:
        cur = cx.execute("UPDATE tool_calls SET result=?, is_error=? WHERE id=?", (result, int(is_error), tool_call_id))
    return cur.rowcount > 0


def message_delete(msg_id: str) -> bool:
    with conn() as cx:
        cx.execute("DELETE FROM message_versions WHERE message_id=?", (msg_id,))
        cx.execute("DELETE FROM tool_calls WHERE assistant_message_id=?", (msg_id,))
        cur = cx.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    return cur.rowcount > 0


def message_delete_many(msg_ids: list[str]) -> int:
    """一次删多条消息，也一起清掉各自的版本和工具记录。"""
    ids = list(dict.fromkeys(msg_ids))
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with conn() as cx:
        cx.execute(f"DELETE FROM message_versions WHERE message_id IN ({marks})", ids)
        cx.execute(f"DELETE FROM tool_calls WHERE assistant_message_id IN ({marks})", ids)
        cur = cx.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
    return cur.rowcount



# ---------------------------------------------------------------- 供应商用量流水

def provider_usage_event_add(provider_id: str, key_hash: str, message_id: str,
                             request_kind: str, cost: float, made: int | None = None,
                             *, input_tokens: int = 0, cached_tokens: int = 0,
                             cache_observed: bool = False) -> dict | None:
    try:
        clean_cost = float(cost)
        clean_input_tokens = max(0, int(input_tokens or 0))
        clean_cached_tokens = max(0, int(cached_tokens or 0))
    except (TypeError, ValueError):
        return None
    if clean_cost < 0 or not provider_id or not key_hash:
        return None
    row = {
        "id": new_id(),
        "provider_id": provider_id,
        "key_hash": key_hash,
        "message_id": message_id or "",
        "request_kind": (request_kind or "chat_reply")[:80],
        "cost": round(clean_cost, 8),
        "input_tokens": clean_input_tokens,
        "cached_tokens": clean_cached_tokens,
        "cache_observed": int(bool(cache_observed)),
        "made": int(made or time.time()),
        "source": "request",
    }
    with conn() as cx:
        cx.execute(
            "INSERT INTO provider_usage_events "
            "(id,provider_id,key_hash,message_id,request_kind,cost,input_tokens,"
            "cached_tokens,cache_observed,made,source) "
            "VALUES (:id,:provider_id,:key_hash,:message_id,:request_kind,:cost,"
            ":input_tokens,:cached_tokens,:cache_observed,:made,:source)",
            row,
        )
    return row


def provider_usage_backfill_legacy(provider_id: str, key_hash: str) -> int:
    """Import old provider-reported message costs once for the unchanged OpenRouter key."""
    if not provider_id or not key_hash:
        return 0
    inserted = 0
    with conn() as cx:
        rows = cx.execute(
            "SELECT id,made,usage_json FROM messages "
            "WHERE role='assistant' AND usage_json NOT IN ('', '{}')"
        ).fetchall()
        for row in rows:
            try:
                usage = json.loads(row["usage_json"] or "{}")
                cost = float(usage.get("cost"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if cost < 0:
                continue
            event_id = "legacy:" + row["id"]
            cur = cx.execute(
                "INSERT OR IGNORE INTO provider_usage_events "
                "(id,provider_id,key_hash,message_id,request_kind,cost,made,source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (event_id, provider_id, key_hash, row["id"], "legacy_message",
                 round(cost, 8), int(row["made"]), "legacy_message"),
            )
            inserted += max(0, cur.rowcount)
    return inserted


def provider_usage_events(provider_id: str, key_hash: str) -> list[dict]:
    with conn() as cx:
        rows = cx.execute(
            "SELECT cost,made,input_tokens,cached_tokens,cache_observed "
            "FROM provider_usage_events "
            "WHERE provider_id=? AND key_hash=? ORDER BY made ASC",
            (provider_id, key_hash),
        ).fetchall()
    return [dict(row) for row in rows]
