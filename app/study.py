"""Cloudy's study: EPUB ingestion, reading progress, visible notes and shared threads."""

import io
import json
import posixpath
import re
import time
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from . import db

MAX_EPUB_BYTES = 25 * 1024 * 1024
MAX_UNPACKED_BYTES = 60 * 1024 * 1024
MAX_TEXT_BYTES = 12 * 1024 * 1024
MAX_COVER_BYTES = 3 * 1024 * 1024
SHELF_CAPACITY = 8
DEFAULT_READING_TOKENS = 4000
MIN_READING_TOKENS = 1000
MAX_READING_TOKENS = 12000
TOTAL_INPUT_TOKENS = 32000


SCHEMA = """
CREATE TABLE IF NOT EXISTS study_books (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    filename        TEXT NOT NULL DEFAULT '',
    chapter_count   INTEGER NOT NULL DEFAULT 0,
    current_chapter INTEGER NOT NULL DEFAULT 0,
    current_offset  INTEGER NOT NULL DEFAULT 0,
    finished        INTEGER NOT NULL DEFAULT 0,
    made            INTEGER NOT NULL,
    updated         INTEGER NOT NULL,
    last_read       INTEGER NOT NULL DEFAULT 0,
    cover_data      BLOB,
    cover_mime      TEXT NOT NULL DEFAULT '',
    collected       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_study_books_reading
ON study_books(finished, last_read, made);

CREATE TABLE IF NOT EXISTS study_chapters (
    book_id TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    title   TEXT NOT NULL DEFAULT '',
    body    TEXT NOT NULL,
    PRIMARY KEY (book_id, idx),
    FOREIGN KEY (book_id) REFERENCES study_books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS study_notes (
    id          TEXT PRIMARY KEY,
    book_id     TEXT NOT NULL,
    chapter_idx INTEGER NOT NULL DEFAULT 0,
    anchor      TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'private',
    who         TEXT NOT NULL DEFAULT 'cloudy',
    made        INTEGER NOT NULL,
    FOREIGN KEY (book_id) REFERENCES study_books(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_study_notes_book
ON study_notes(book_id, kind, made DESC);

CREATE TABLE IF NOT EXISTS study_replies (
    id             TEXT PRIMARY KEY,
    note_id        TEXT NOT NULL,
    who            TEXT NOT NULL,
    text           TEXT NOT NULL,
    made           INTEGER NOT NULL,
    seen_by_cloudy INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (note_id) REFERENCES study_notes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_study_replies_note
ON study_replies(note_id, made);

CREATE TABLE IF NOT EXISTS study_activity (
    id          TEXT PRIMARY KEY,
    book_id     TEXT NOT NULL,
    chapter_idx INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL,
    made        INTEGER NOT NULL,
    FOREIGN KEY (book_id) REFERENCES study_books(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_study_activity_made ON study_activity(made DESC);
"""


class EpubError(ValueError):
    pass


class _Text(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "blockquote", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.hidden = 0
        self.in_title = False
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "svg"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2"} and not self.heading_parts:
            self.in_heading = True
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self.hidden:
            self.hidden -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"h1", "h2"}:
            self.in_heading = False
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            return
        self.parts.append(text)
        if self.in_title:
            self.title_parts.append(text)
        if self.in_heading:
            self.heading_parts.append(text)

    def result(self) -> tuple[str, str]:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        title = "".join(self.heading_parts).strip() or "".join(self.title_parts).strip()
        return title[:160], text


def init_db() -> None:
    with db.conn() as cx:
        cx.executescript(SCHEMA)
        columns = {row["name"] for row in cx.execute("PRAGMA table_info(study_books)").fetchall()}
        if "cover_data" not in columns:
            cx.execute("ALTER TABLE study_books ADD COLUMN cover_data BLOB")
        if "cover_mime" not in columns:
            cx.execute("ALTER TABLE study_books ADD COLUMN cover_mime TEXT NOT NULL DEFAULT ''")
        if "collected" not in columns:
            cx.execute("ALTER TABLE study_books ADD COLUMN collected INTEGER NOT NULL DEFAULT 0")
        # Older versions had no shelf limit. Keep the eight most recently touched
        # books visible and move any overflow into the recoverable collection.
        cx.execute(
            """UPDATE study_books SET collected=1
               WHERE collected=0 AND id NOT IN (
                   SELECT id FROM study_books WHERE collected=0 ORDER BY updated DESC LIMIT ?
               )""",
            (SHELF_CAPACITY,),
        )
        # PR #68 called these private notes.  They are now the visible reading notebook;
        # a genuinely private diary will be a separate feature later.
        cx.execute("UPDATE study_notes SET kind='reading' WHERE kind='private'")


def estimate_tokens(text: str) -> int:
    """Conservative model-independent estimate for mixed English/CJK text.

    Providers in Dwell can use different tokenizers, so an exact universal count is
    impossible.  We keep a ten percent safety margin when selecting book text.
    """
    total = 0
    for piece in re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff\uf900-\ufaff]|[^\s]", text or ""):
        if re.fullmatch(r"[A-Za-z0-9_]+", piece):
            total += max(1, (len(piece) + 3) // 4)
        else:
            total += 1
    return total


def messages_tokens(messages: list[dict]) -> int:
    total = 0
    for item in messages:
        total += 12 + estimate_tokens(str(item.get("content") or ""))
    return total


def _safe_member(path: str) -> str:
    value = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if value == ".." or value.startswith("../"):
        raise EpubError("书里有不安全的文件路径")
    return value


def _xml_text(root: ET.Element, local_name: str) -> str:
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] == local_name and (item.text or "").strip():
            return (item.text or "").strip()
    return ""


def parse_epub(raw: bytes, filename: str) -> tuple[str, str, list[tuple[str, str]], bytes, str]:
    if not filename.lower().endswith(".epub"):
        raise EpubError("请上传 .epub 格式的书")
    if not raw or len(raw) > MAX_EPUB_BYTES:
        raise EpubError("这本书太大了，目前每本最多 25 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise EpubError("这个文件不像完整的 EPUB") from exc
    with zf:
        infos = zf.infolist()
        if sum(item.file_size for item in infos) > MAX_UNPACKED_BYTES:
            raise EpubError("这本书展开后太大了")
        names = {_safe_member(item.filename): item for item in infos}
        container_name = "META-INF/container.xml"
        if container_name not in names:
            raise EpubError("EPUB 里缺少目录信息")
        try:
            container = ET.fromstring(zf.read(names[container_name]))
            rootfile = next(
                item.attrib.get("full-path", "") for item in container.iter()
                if item.tag.rsplit("}", 1)[-1] == "rootfile"
            )
            opf_name = _safe_member(rootfile)
            package = ET.fromstring(zf.read(names[opf_name]))
        except Exception as exc:
            raise EpubError("没有读懂这本 EPUB 的目录") from exc

        title = _xml_text(package, "title") or PurePosixPath(filename).stem
        author = _xml_text(package, "creator")
        manifest: dict[str, tuple[str, str, str]] = {}
        spine: list[str] = []
        cover_id = ""
        for item in package.iter():
            local = item.tag.rsplit("}", 1)[-1]
            if local == "item" and item.attrib.get("id") and item.attrib.get("href"):
                manifest[item.attrib["id"]] = (
                    item.attrib["href"], item.attrib.get("media-type", ""),
                    item.attrib.get("properties", ""),
                )
            elif local == "itemref" and item.attrib.get("idref"):
                spine.append(item.attrib["idref"])
            elif local == "meta" and item.attrib.get("name", "").lower() == "cover":
                cover_id = item.attrib.get("content", "")
        if not spine:
            spine = [key for key, value in manifest.items() if "html" in value[1]]

        base = posixpath.dirname(opf_name)
        cover_data = b""
        cover_mime = ""
        cover_candidates = []
        if cover_id and cover_id in manifest:
            cover_candidates.append(manifest[cover_id])
        cover_candidates.extend(
            value for key, value in manifest.items()
            if "cover-image" in value[2].split() or ("cover" in key.lower() and value[1].startswith("image/"))
        )
        allowed_covers = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        for href, media_type, _properties in cover_candidates:
            if media_type not in allowed_covers:
                continue
            member = _safe_member(posixpath.join(base, href.split("#", 1)[0]))
            if member not in names or names[member].file_size > MAX_COVER_BYTES:
                continue
            candidate = zf.read(names[member])
            if candidate:
                cover_data, cover_mime = candidate, media_type
                break
        chapters: list[tuple[str, str]] = []
        total_text = 0
        for item_id in spine[:400]:
            entry = manifest.get(item_id)
            if not entry:
                continue
            href, media_type, _properties = entry
            if "html" not in media_type and not href.lower().endswith((".html", ".xhtml", ".htm")):
                continue
            member = _safe_member(posixpath.join(base, href.split("#", 1)[0]))
            if member not in names:
                continue
            try:
                source = zf.read(names[member]).decode("utf-8", "replace")
                parser = _Text()
                parser.feed(source)
                chapter_title, body = parser.result()
            except Exception:
                continue
            if len(body) < 40:
                continue
            body = body[:250_000]
            total_text += len(body.encode("utf-8"))
            if total_text > MAX_TEXT_BYTES:
                raise EpubError("这本书文字太多了，目前还放不下")
            chapters.append((chapter_title or f"第 {len(chapters) + 1} 节", body))
        if not chapters:
            raise EpubError("没有从这本 EPUB 里读到正文")
        return title[:240], author[:160], chapters[:300], cover_data, cover_mime


def add_book(raw: bytes, filename: str) -> dict:
    with db.conn() as cx:
        shelf_count = cx.execute("SELECT COUNT(*) AS n FROM study_books WHERE collected=0").fetchone()["n"]
    if shelf_count >= SHELF_CAPACITY:
        raise EpubError("书架已经放满八本了，请先把一本书收进收藏")
    title, author, chapters, cover_data, cover_mime = parse_epub(raw, filename)
    now = int(time.time())
    book_id = db.new_id()
    row = {
        "id": book_id, "slug": book_id, "title": title, "author": author,
        "filename": filename[:260], "chapter_count": len(chapters),
        "current_chapter": 0, "current_offset": 0, "finished": 0,
        "made": now, "updated": now, "last_read": 0,
        "cover_data": cover_data or None, "cover_mime": cover_mime, "collected": 0,
    }
    with db.conn() as cx:
        cx.execute(
            """INSERT INTO study_books
               (id,title,author,filename,chapter_count,current_chapter,current_offset,finished,made,updated,last_read,cover_data,cover_mime,collected)
               VALUES (:id,:title,:author,:filename,:chapter_count,:current_chapter,:current_offset,:finished,:made,:updated,:last_read,:cover_data,:cover_mime,:collected)""",
            row,
        )
        cx.executemany(
            "INSERT INTO study_chapters (book_id,idx,title,body) VALUES (?,?,?,?)",
            [(book_id, idx, chapter_title, body) for idx, (chapter_title, body) in enumerate(chapters)],
        )
    row.pop("cover_data", None)
    row["has_cover"] = bool(cover_data)
    row["chapters"] = [item[0] for item in chapters]
    return row


def books() -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT id,title,author,filename,chapter_count,current_chapter,current_offset,
                      finished,made,updated,last_read,collected,cover_mime,
                      CASE WHEN cover_data IS NOT NULL THEN 1 ELSE 0 END AS has_cover
               FROM study_books ORDER BY collected, finished, updated DESC"""
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["slug"] = item["id"]
            item["chapters"] = [r["title"] for r in cx.execute(
                "SELECT title FROM study_chapters WHERE book_id=? ORDER BY idx", (item["id"],)
            ).fetchall()]
            out.append(item)
    return out


def book_cover(book_id: str) -> tuple[bytes, str] | None:
    with db.conn() as cx:
        row = cx.execute("SELECT cover_data,cover_mime FROM study_books WHERE id=?", (book_id,)).fetchone()
    if not row or not row["cover_data"] or not row["cover_mime"]:
        return None
    return bytes(row["cover_data"]), str(row["cover_mime"])


def collect_book(book_id: str, collected: bool) -> bool:
    with db.conn() as cx:
        row = cx.execute("SELECT collected FROM study_books WHERE id=?", (book_id,)).fetchone()
        if not row:
            return False
        if not collected and row["collected"]:
            count = cx.execute("SELECT COUNT(*) AS n FROM study_books WHERE collected=0").fetchone()["n"]
            if count >= SHELF_CAPACITY:
                raise ValueError("书架已经放满八本了，请先收起另一本")
        cx.execute("UPDATE study_books SET collected=?,updated=? WHERE id=?",
                   (1 if collected else 0, int(time.time()), book_id))
    return True


def delete_book(book_id: str) -> bool:
    with db.conn() as cx:
        cur = cx.execute("DELETE FROM study_books WHERE id=?", (book_id,))
    return cur.rowcount > 0


def progress() -> dict:
    with db.conn() as cx:
        rows = cx.execute("SELECT id,current_chapter,current_offset,finished FROM study_books").fetchall()
    return {r["id"]: {"ch": r["current_chapter"], "offset": r["current_offset"], "finished": bool(r["finished"])} for r in rows}


def set_progress(book_id: str, chapter: int, offset: int = 0) -> bool:
    with db.conn() as cx:
        row = cx.execute("SELECT chapter_count FROM study_books WHERE id=?", (book_id,)).fetchone()
        if not row:
            return False
        chapter = max(0, min(int(chapter), max(0, row["chapter_count"] - 1)))
        cx.execute(
            "UPDATE study_books SET current_chapter=?,current_offset=?,finished=0,updated=? WHERE id=?",
            (chapter, max(0, int(offset)), int(time.time()), book_id),
        )
    return True


def chapter(book_id: str, idx: int) -> dict | None:
    with db.conn() as cx:
        book = cx.execute("SELECT * FROM study_books WHERE id=?", (book_id,)).fetchone()
        item = cx.execute("SELECT * FROM study_chapters WHERE book_id=? AND idx=?", (book_id, idx)).fetchone()
        if not book or not item:
            return None
        titles = [r["title"] for r in cx.execute(
            "SELECT title FROM study_chapters WHERE book_id=? ORDER BY idx", (book_id,)
        ).fetchall()]
    return {
        "book": book["title"], "title": item["title"], "text": item["body"],
        "index": item["idx"], "total": book["chapter_count"], "chapters": titles,
    }


def _fmt_ts(value: int) -> str:
    return datetime.fromtimestamp(value, db.CN_TZ).strftime("%m月%d日 %H:%M")


def annotations(book_id: str, chapter_idx: int) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT * FROM study_notes
               WHERE book_id=? AND chapter_idx=? AND kind='annotation' ORDER BY made""",
            (book_id, chapter_idx),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["ts"] = _fmt_ts(item["made"])
            item["note"] = item.pop("text")
            replies = cx.execute("SELECT * FROM study_replies WHERE note_id=? ORDER BY made", (item["id"],)).fetchall()
            item["replies"] = [{**dict(r), "ts": _fmt_ts(r["made"])} for r in replies]
            out.append(item)
    return out


def add_annotation(book_id: str, chapter_idx: int, anchor: str, note: str, who: str = "user") -> dict:
    if not chapter(book_id, chapter_idx):
        raise KeyError(book_id)
    row = {
        "id": db.new_id(), "book_id": book_id, "chapter_idx": chapter_idx,
        "anchor": anchor.strip()[:280], "text": note.strip()[:4000],
        "kind": "annotation", "who": "cloudy" if who == "ai" else "user", "made": int(time.time()),
    }
    if not row["anchor"]:
        raise ValueError("没有选中文字")
    with db.conn() as cx:
        cx.execute(
            """INSERT INTO study_notes (id,book_id,chapter_idx,anchor,text,kind,who,made)
               VALUES (:id,:book_id,:chapter_idx,:anchor,:text,:kind,:who,:made)""", row,
        )
    return row


def add_reply(note_id: str, text: str, who: str = "user") -> dict:
    text = text.strip()[:4000]
    if not text:
        raise ValueError("回复是空的")
    with db.conn() as cx:
        if not cx.execute("SELECT 1 FROM study_notes WHERE id=?", (note_id,)).fetchone():
            raise KeyError(note_id)
        row = {
            "id": db.new_id(), "note_id": note_id, "who": "cloudy" if who in {"cloudy", "ai"} else "user",
            "text": text, "made": int(time.time()), "seen_by_cloudy": 1 if who in {"cloudy", "ai"} else 0,
        }
        cx.execute(
            """INSERT INTO study_replies (id,note_id,who,text,made,seen_by_cloudy)
               VALUES (:id,:note_id,:who,:text,:made,:seen_by_cloudy)""", row,
        )
    return row


def shares(limit: int = 80) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT n.*,b.title AS book_title,c.title AS chapter_title
               FROM study_notes n JOIN study_books b ON b.id=n.book_id
               LEFT JOIN study_chapters c ON c.book_id=n.book_id AND c.idx=n.chapter_idx
               WHERE n.kind='share' ORDER BY n.made DESC LIMIT ?""", (max(1, min(limit, 200)),)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["ts"] = _fmt_ts(item["made"])
            replies = cx.execute("SELECT * FROM study_replies WHERE note_id=? ORDER BY made", (item["id"],)).fetchall()
            item["replies"] = [{**dict(r), "ts": _fmt_ts(r["made"])} for r in replies]
            out.append(item)
    return out


def pending_replies(limit: int = 20) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT r.id AS reply_id,r.note_id,r.text,r.made,n.text AS cloudy_text,
                      b.title AS book_title,c.title AS chapter_title
               FROM study_replies r JOIN study_notes n ON n.id=r.note_id
               JOIN study_books b ON b.id=n.book_id
               LEFT JOIN study_chapters c ON c.book_id=n.book_id AND c.idx=n.chapter_idx
               WHERE r.who='user' AND r.seen_by_cloudy=0
               ORDER BY r.made LIMIT ?""", (max(1, min(limit, 50)),)
        ).fetchall()
    return [dict(r) for r in rows]


def next_pending_thread() -> dict | None:
    with db.conn() as cx:
        pending = cx.execute(
            """SELECT note_id FROM study_replies
               WHERE who='user' AND seen_by_cloudy=0 ORDER BY made LIMIT 1"""
        ).fetchone()
        if not pending:
            return None
        note = cx.execute(
            """SELECT n.*,b.title AS book_title,b.author,c.title AS chapter_title
               FROM study_notes n JOIN study_books b ON b.id=n.book_id
               LEFT JOIN study_chapters c ON c.book_id=n.book_id AND c.idx=n.chapter_idx
               WHERE n.id=? AND n.kind='share'""", (pending["note_id"],)
        ).fetchone()
        if not note:
            return None
        replies = cx.execute(
            "SELECT * FROM study_replies WHERE note_id=? ORDER BY made", (pending["note_id"],)
        ).fetchall()
    return {
        **dict(note),
        "replies": [{**dict(r), "ts": _fmt_ts(r["made"])} for r in replies],
    }


def record_thread_reply(note_id: str, text: str) -> dict:
    text = text.strip()[:1600]
    if not text:
        raise ValueError("Cloudy 没有留下回信")
    now = int(time.time())
    with db.conn() as cx:
        note = cx.execute(
            """SELECT n.*,b.title AS book_title FROM study_notes n
               JOIN study_books b ON b.id=n.book_id WHERE n.id=? AND n.kind='share'""",
            (note_id,),
        ).fetchone()
        if not note:
            raise KeyError(note_id)
        row = {
            "id": db.new_id(), "note_id": note_id, "who": "cloudy", "text": text,
            "made": now, "seen_by_cloudy": 1,
        }
        cx.execute(
            """INSERT INTO study_replies (id,note_id,who,text,made,seen_by_cloudy)
               VALUES (:id,:note_id,:who,:text,:made,:seen_by_cloudy)""", row,
        )
        cx.execute(
            "UPDATE study_replies SET seen_by_cloudy=1 WHERE note_id=? AND who='user'",
            (note_id,),
        )
        summary = f"读了小猫在《{note['book_title']}》分享页留下的话，也回了一页"
        cx.execute(
            "INSERT INTO study_activity (id,book_id,chapter_idx,summary,made) VALUES (?,?,?,?,?)",
            (db.new_id(), note["book_id"], note["chapter_idx"], summary, now),
        )
    return {"reply": row, "summary": summary, "book_id": note["book_id"]}


def all_reading_notes(limit: int = 300) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT n.*,b.title AS book_title,c.title AS chapter_title
               FROM study_notes n JOIN study_books b ON b.id=n.book_id
               LEFT JOIN study_chapters c ON c.book_id=n.book_id AND c.idx=n.chapter_idx
               WHERE n.kind='reading' ORDER BY n.made DESC LIMIT ?""",
            (max(1, min(limit, 1000)),),
        ).fetchall()
    return [{**dict(r), "ts": _fmt_ts(r["made"])} for r in rows]


def _cut_text(text: str, token_limit: int) -> int:
    """Return a natural-boundary character offset that fits token_limit."""
    if not text or token_limit <= 0:
        return 0
    if estimate_tokens(text) <= token_limit:
        return len(text)
    low, high = 1, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= token_limit:
            low = mid
        else:
            high = mid - 1
    end = low
    natural_start = max(1, int(end * .7))
    paragraph = text.rfind("\n", natural_start, end)
    sentence = max(text.rfind(". ", natural_start, end), text.rfind("。", natural_start, end))
    cut = max(paragraph, sentence + 1 if sentence >= 0 else -1)
    return cut if cut > 0 else end


def next_passage(token_budget: int = DEFAULT_READING_TOKENS) -> dict | None:
    with db.conn() as cx:
        book = cx.execute(
            "SELECT * FROM study_books WHERE finished=0 AND collected=0 ORDER BY last_read ASC,made ASC LIMIT 1"
        ).fetchone()
        if not book:
            return None
        start_chapter = int(book["current_chapter"])
        start_offset = int(book["current_offset"])
        chapter_rows = cx.execute(
            "SELECT * FROM study_chapters WHERE book_id=? AND idx>=? ORDER BY idx",
            (book["id"], start_chapter),
        ).fetchall()
        if not chapter_rows:
            return None
    token_budget = max(MIN_READING_TOKENS, min(int(token_budget), MAX_READING_TOKENS))
    safe_budget = max(1, int(token_budget * .9))
    parts: list[str] = []
    sections: list[dict] = []
    used_tokens = 0
    next_chapter = start_chapter
    next_offset = start_offset
    finished = False

    for row in chapter_rows:
        chapter_idx = int(row["idx"])
        body = row["body"]
        offset = start_offset if chapter_idx == start_chapter else 0
        if offset >= len(body):
            next_chapter, next_offset = chapter_idx + 1, 0
            continue
        heading = f"[章节：{row['title']}]\n"
        heading_tokens = estimate_tokens(heading)
        available = safe_budget - used_tokens - heading_tokens
        if available <= 0:
            break
        remaining = body[offset:]
        take = _cut_text(remaining, available)
        if take <= 0:
            break
        excerpt = remaining[:take].strip()
        if excerpt:
            parts.append(heading + excerpt)
            sections.append({
                "chapter_idx": chapter_idx, "chapter_title": row["title"],
                "start_offset": offset, "end_offset": offset + take,
            })
            used_tokens += estimate_tokens(heading + excerpt)
        if offset + take < len(body):
            next_chapter, next_offset = chapter_idx, offset + take
            break
        next_chapter, next_offset = chapter_idx + 1, 0
    else:
        finished = next_chapter >= int(book["chapter_count"])

    if not parts:
        return None
    if next_chapter >= int(book["chapter_count"]):
        next_chapter = max(0, int(book["chapter_count"]) - 1)
        next_offset = 0
        finished = True
    passage_text = "\n\n".join(parts)
    first = sections[0]
    return {
        "book_id": book["id"], "book_title": book["title"], "author": book["author"],
        "chapter_idx": first["chapter_idx"], "chapter_title": first["chapter_title"],
        "start_offset": first["start_offset"], "end_offset": sections[-1]["end_offset"],
        "chapter_count": book["chapter_count"], "text": passage_text,
        "sections": sections, "next_chapter": next_chapter, "next_offset": next_offset,
        "finished": finished,
        "estimated_tokens": estimate_tokens(passage_text), "token_budget": token_budget,
    }


def reading_notes(book_id: str) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT n.id,n.text,n.anchor,n.chapter_idx,c.title AS chapter_title,n.made
               FROM study_notes n LEFT JOIN study_chapters c
               ON c.book_id=n.book_id AND c.idx=n.chapter_idx
               WHERE n.book_id=? AND n.kind='reading' ORDER BY n.made""",
            (book_id,),
        ).fetchall()
    return [{**dict(r), "ts": _fmt_ts(r["made"])} for r in rows]


def record_session(passage: dict, reading_note: str, share_text: str = "", share_anchor: str = "") -> dict:
    now = int(time.time())
    book_id = passage["book_id"]
    next_chapter = int(passage["next_chapter"])
    next_offset = int(passage["next_offset"])
    finished = 1 if passage.get("finished") else 0
    share_id = ""
    with db.conn() as cx:
        note_row = (
            db.new_id(), book_id, passage["chapter_idx"], passage["text"][:220],
            reading_note.strip()[:2400], "reading", "cloudy", now,
        )
        cx.execute(
            """INSERT INTO study_notes (id,book_id,chapter_idx,anchor,text,kind,who,made)
               VALUES (?,?,?,?,?,?,?,?)""", note_row,
        )
        if share_text.strip():
            share_id = db.new_id()
            cx.execute(
                """INSERT INTO study_notes (id,book_id,chapter_idx,anchor,text,kind,who,made)
                   VALUES (?,?,?,?,?,'share','cloudy',?)""",
                (share_id, book_id, passage["chapter_idx"], share_anchor.strip()[:280], share_text.strip()[:1600], now),
            )
        cx.execute(
            """UPDATE study_books SET current_chapter=?,current_offset=?,finished=?,last_read=?,updated=?
               WHERE id=?""", (next_chapter, next_offset, finished, now, now, book_id),
        )
        titles = [item["chapter_title"] for item in passage.get("sections", [])]
        chapter_label = " → ".join(dict.fromkeys(titles)) or passage["chapter_title"]
        summary = f"读了《{passage['book_title']}》的「{chapter_label}」"
        if share_id:
            summary += "，在分享本里留了一页"
        else:
            summary += "，写了一页读书笔记"
        cx.execute(
            "INSERT INTO study_activity (id,book_id,chapter_idx,summary,made) VALUES (?,?,?,?,?)",
            (db.new_id(), book_id, passage["chapter_idx"], summary, now),
        )
    return {"summary": summary, "share_id": share_id, "finished": bool(finished)}


def activities(limit: int = 12) -> list[dict]:
    with db.conn() as cx:
        rows = cx.execute(
            """SELECT a.*,b.title AS book_title FROM study_activity a
               JOIN study_books b ON b.id=a.book_id ORDER BY a.made DESC LIMIT ?""",
            (max(1, min(limit, 50)),),
        ).fetchall()
    return [{**dict(r), "ts": _fmt_ts(r["made"])} for r in rows]


def config() -> dict:
    def integer(key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(db.setting_get(key, str(default))), high))
        except (TypeError, ValueError):
            return default
    today = db.today_str()
    raw_times = db.setting_get("study_read_times", '["10:30","16:00","22:00"]')
    try:
        times = normalize_times(json.loads(raw_times))
    except Exception:
        times = ["10:30", "16:00", "22:00"]
    raw_slots = db.setting_get("study_completed_slots", "")
    try:
        slot_state = json.loads(raw_slots) if raw_slots else {}
    except Exception:
        slot_state = {}
    if slot_state.get("date") != today:
        slot_state = {"date": today, "slots": []}
        db.setting_set("study_completed_slots", json.dumps(slot_state, ensure_ascii=False))
    completed = [item for item in slot_state.get("slots", []) if item in times]
    model_provider_id = db.setting_get("study_model_provider_id", "").strip()
    model_id = db.setting_get("study_model_id", "").strip()
    model_provider = db.provider_get(model_provider_id) if model_provider_id else None
    return {
        "on": db.setting_get("study_on", "0") == "1",
        "times": times,
        "daily_sessions": len(times),
        "count": len(completed),
        "completed_slots": completed,
        "reading_tokens": integer("study_reading_tokens", DEFAULT_READING_TOKENS,
                                  MIN_READING_TOKENS, MAX_READING_TOKENS),
        "chat_id": db.setting_get("study_chat_id", "").strip(),
        "model_provider_id": model_provider_id,
        "model_id": model_id,
        "model_provider_name": (model_provider or {}).get("name", ""),
        "last_read": integer("study_last_read", 0, 0, 4_000_000_000),
        "last_status": db.setting_get("study_last_status", "idle"),
        "last_error": db.setting_get("study_last_error", ""),
    }


def normalize_times(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("阅读时间必须是一组时间")
    result = []
    for raw in value:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(raw).strip())
        if not match:
            raise ValueError("时间要写成 08:30 这样的格式")
        result.append(match.group(1) + ":" + match.group(2))
    result = sorted(set(result))
    if not result:
        raise ValueError("至少留下一个阅读时间")
    if len(result) > 12:
        raise ValueError("每天最多安排 12 次阅读")
    return result


def set_config(payload: dict) -> dict:
    if "on" in payload:
        db.setting_set("study_on", "1" if bool(payload["on"]) else "0")
    if "times" in payload:
        db.setting_set("study_read_times", json.dumps(normalize_times(payload["times"]), ensure_ascii=False))
    if "reading_tokens" in payload:
        try:
            value = int(payload["reading_tokens"])
        except (TypeError, ValueError):
            raise ValueError("原文 token 数不是有效数字")
        if not MIN_READING_TOKENS <= value <= MAX_READING_TOKENS:
            raise ValueError(f"每次原文请设在 {MIN_READING_TOKENS}–{MAX_READING_TOKENS} tokens")
        db.setting_set("study_reading_tokens", str(value))
    if "chat_id" in payload:
        chat_id = str(payload["chat_id"] or "").strip()
        if not chat_id or not db.chat_get(chat_id):
            raise ValueError("请选择一间仍然存在的聊天")
        db.setting_set("study_chat_id", chat_id)
    if "model_provider_id" in payload or "model_id" in payload:
        current = config()
        provider_id = str(payload.get("model_provider_id", current["model_provider_id"]) or "").strip()
        model_id = str(payload.get("model_id", current["model_id"]) or "").strip()[:200]
        provider = db.provider_get(provider_id)
        if not provider or not provider.get("enabled"):
            raise ValueError("请选择一个已启用的书房模型供应商")
        if not model_id or not any(item["model_id"] == model_id for item in db.provider_model_list(provider_id)):
            raise ValueError("请选择模型目录中已有的书房模型")
        db.setting_set("study_model_provider_id", provider_id)
        db.setting_set("study_model_id", model_id)
    return config()


def due(now: datetime, force: bool = False) -> tuple[bool, str, str]:
    cfg = config()
    if not force and not cfg["on"]:
        return False, "off", ""
    if not any(not item.get("collected") for item in books()):
        return False, "no_books", ""
    if force:
        return True, "ready", "manual"
    current = now.hour * 60 + now.minute
    for slot in cfg["times"]:
        hour, minute = (int(value) for value in slot.split(":"))
        delta = current - (hour * 60 + minute)
        # The background loop runs once a minute.  A two-minute grace window covers
        # ordinary scheduling drift but deliberately does not catch up after downtime.
        if 0 <= delta <= 2 and slot not in cfg["completed_slots"]:
            return True, "ready", slot
    return False, "waiting", ""


def claim_slot(slot: str) -> None:
    if not slot or slot == "manual":
        return
    cfg = config()
    completed = sorted(set(cfg["completed_slots"] + [slot]))
    db.setting_set("study_completed_slots", json.dumps({
        "date": db.today_str(), "slots": completed,
    }, ensure_ascii=False))


def mark_result(status: str, error: str = "", counted: bool = False) -> None:
    db.setting_set("study_last_status", status)
    db.setting_set("study_last_error", error[:500])
    if counted:
        db.setting_set("study_last_read", str(int(time.time())))
