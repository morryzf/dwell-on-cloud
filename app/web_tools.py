"""Small, credential-free web tools for chat models.

They intentionally accept only public HTTP(S) URLs.  This makes link reading useful
without turning the Dwell service into a path to its own network or cloud metadata.
"""

import asyncio
import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx


class WebToolError(RuntimeError):
    pass


def _public_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return True


async def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise WebToolError("只允许读取完整的公共 http(s) 链接")
    host = parsed.hostname
    if not _public_address(host):
        raise WebToolError("不能读取内网或本机地址")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebToolError("链接域名无法解析") from exc
    if not infos or any(not _public_address(info[4][0]) for info in infos):
        raise WebToolError("不能读取内网地址")
    return url


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title += (" " if self.title else "") + text
        self.parts.append(text)


async def _download(url: str) -> tuple[str, str, str]:
    current = await _check_url(url)
    headers = {"User-Agent": "DwellWebTools/1.0 (+https://github.com/morryzf/dwell-backend)", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.2"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
        for _ in range(4):
            response = await client.get(current, headers=headers)
            if response.is_redirect:
                target = response.headers.get("location")
                if not target:
                    raise WebToolError("网页重定向无效")
                current = await _check_url(urljoin(current, target))
                continue
            response.raise_for_status()
            raw = response.content[:2_000_000]
            return current, response.headers.get("content-type", ""), raw.decode(response.encoding or "utf-8", errors="replace")
    raise WebToolError("网页重定向次数过多")


async def web_fetch(url: str) -> str:
    final_url, content_type, raw = await _download(url)
    if "html" in content_type.lower() or "xhtml" in content_type.lower() or not content_type:
        parser = _TextParser()
        parser.feed(raw)
        text = "\n".join(parser.parts)
        title = parser.title.strip()
    else:
        text, title = raw, ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()[:24_000]
    return json.dumps({"url": final_url, "title": title, "content": text}, ensure_ascii=False)


async def web_search(query: str) -> str:
    query = " ".join(str(query or "").split())[:500]
    if not query:
        raise WebToolError("搜索词不能为空")
    _, _, page = await _download("https://html.duckduckgo.com/html/?q=" + quote_plus(query))
    results = []
    for href, title_html in re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.I | re.S):
        title = re.sub(r"<[^>]+>", "", html.unescape(title_html)).strip()
        target = html.unescape(href)
        parsed = urlparse(target)
        if "duckduckgo.com" in parsed.netloc:
            target = parse_qs(parsed.query).get("uddg", [target])[0]
        if title and target.startswith(("http://", "https://")):
            results.append({"title": title[:300], "url": target})
        if len(results) >= 8:
            break
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)

