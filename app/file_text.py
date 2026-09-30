"""把上传的文件读成文字，好放进这一轮的对话里。

模型看不了原始文件，只看得了文字。这里认得的有：各种编码的纯文本、PDF、
Word（.docx）、PowerPoint（.pptx）、Excel（.xlsx）。老格式（.doc/.xls/.ppt）
和压缩包、音视频读不出来，直接说清楚，不假装收下了。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

# 读不出文字的类型，与其解码出一堆乱码，不如当场说明白。
UNREADABLE_EXT = {
    ".doc": "老版 Word（.doc）读不了，另存成 .docx 再发",
    ".xls": "老版 Excel（.xls）读不了，另存成 .xlsx 再发",
    ".ppt": "老版 PPT（.ppt）读不了，另存成 .pptx 再发",
    ".zip": "压缩包读不了，解压后把里面的文件发过来",
    ".rar": "压缩包读不了，解压后把里面的文件发过来",
    ".7z": "压缩包读不了，解压后把里面的文件发过来",
}
MEDIA_EXT = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mov", ".avi", ".mkv", ".webm")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _ext(name: str) -> str:
    match = re.search(r"\.[A-Za-z0-9]{1,8}$", name or "")
    return match.group(0).lower() if match else ""


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")        # 只挡复制、不挡打开的那种，空密码就能开
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        raise ValueError("这个 PDF 打不开（" + type(exc).__name__ + "），可能加了密码或者文件坏了") from exc
    pages = [text for text in pages if text]
    if not pages:
        raise ValueError("这个 PDF 里读不出文字，可能是扫描件或者整页都是图片")
    return "\n\n".join(pages)


def _zip_xml(archive: zipfile.ZipFile, path: str) -> ET.Element:
    return ET.fromstring(archive.read(path))


def _numbered(archive: zipfile.ZipFile, pattern: str) -> list[str]:
    found = []
    for path in archive.namelist():
        match = re.fullmatch(pattern, path)
        if match:
            found.append((int(match.group(1)), path))
    return [path for _, path in sorted(found)]


def _docx(archive: zipfile.ZipFile) -> str:
    root = _zip_xml(archive, "word/document.xml")
    lines = []
    for para in root.iter(W + "p"):
        parts = []
        for node in para.iter():
            if node.tag == W + "t" and node.text:
                parts.append(node.text)
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _pptx(archive: zipfile.ZipFile) -> str:
    slides = []
    for number, path in enumerate(_numbered(archive, r"ppt/slides/slide(\d+)\.xml"), 1):
        root = _zip_xml(archive, path)
        paras = ["".join(t.text or "" for t in para.iter(A + "t")) for para in root.iter(A + "p")]
        body = "\n".join(p for p in paras if p.strip())
        if body:
            slides.append(f"【第 {number} 页】\n{body}")
    return "\n\n".join(slides)


def _xlsx(archive: zipfile.ZipFile) -> str:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        for item in _zip_xml(archive, "xl/sharedStrings.xml").iter(S + "si"):
            shared.append("".join(t.text or "" for t in item.iter(S + "t")))
    sheets = []
    for number, path in enumerate(_numbered(archive, r"xl/worksheets/sheet(\d+)\.xml"), 1):
        rows = []
        for row in _zip_xml(archive, path).iter(S + "row"):
            cells = []
            for cell in row.iter(S + "c"):
                kind = cell.get("t")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(S + "t"))
                else:
                    raw = cell.find(S + "v")
                    value = raw.text if raw is not None and raw.text else ""
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                cells.append(value)
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells).rstrip())
        if rows:
            sheets.append(f"【工作表 {number}】\n" + "\n".join(rows))
    return "\n\n".join(sheets)


def _office(data: bytes, ext: str) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        reader = {".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx}[ext]
        return reader(archive)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("这个 " + ext + " 文件打不开，可能已经损坏") from exc


def _plain(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise ValueError("这种文件读不出文字，目前能读的是文本、PDF、Word、PPT、Excel")
    for encoding in ("utf-8-sig", "gb18030", "big5", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("认不出这个文件的文字编码")


def extract_text(name: str, data: bytes) -> str:
    """读出文件里的文字；读不出来就抛 ValueError，理由能直接给用户看。"""
    ext = _ext(name)
    if ext in UNREADABLE_EXT:
        raise ValueError(UNREADABLE_EXT[ext])
    if ext in MEDIA_EXT:
        raise ValueError("音频和视频读不了，只能读文本、PDF、Word、PPT、Excel")
    if ext == ".pdf" or data[:5] == b"%PDF-":
        text = _pdf(data)
    elif ext in (".docx", ".pptx", ".xlsx"):
        text = _office(data, ext)
    else:
        text = _plain(data)
    text = _tidy(text)
    if not text:
        raise ValueError("这个文件里没有读到文字")
    return text
