"""RSS / Atom 订阅源 → 条目清单（标题、发布时间、文章链接、摘要）。

研究模型读的是渲染好的清单（每条一个「### 标题」块），不是原始 XML；条目另存在 ParsedDocument.meta["entries"]，
答案的引文落在某条标题上时，收尾由代码把这一条作为 Answer.item 附上（文章页被反爬拦下的站也能用订阅源里的标题、日期和链接）。
XML 用 lxml 解析：不展开实体、不联网、不收超大树。
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from owa_research.core.document import ParsedDocument, sections_from_markdown

MAX_ENTRIES = 100
SUMMARY_CHARS = 300
_ROOT_RE = re.compile(rb"<(rss|feed|rdf:RDF)[\s>]")
_TAG_RE = re.compile(r"<[^>]+>")


def looks_like_feed(data: bytes) -> bool:
    return bool(_ROOT_RE.search(data[:4096]))


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: Any, *names: str) -> Any:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: Any) -> str:
    if el is None:
        return ""
    s = "".join(el.itertext())
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()      # 摘要常是转义过的 HTML：去标签再反转义


def _date(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _link(item: Any) -> str:
    for c in item:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        if href and c.get("rel", "alternate") == "alternate":
            return href.strip()
        if not href and (t := _text(c)):
            return t
    guid = _child(item, "guid")
    if guid is not None and guid.get("isPermaLink", "true") != "false" and _text(guid).startswith("http"):
        return _text(guid)
    return ""


def parse_feed(data: bytes) -> ParsedDocument | None:
    """是订阅源就解析成 ParsedDocument（kind="feed"），不是或解析不了返回 None。"""
    if not looks_like_feed(data):
        return None
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None or _local(root.tag) not in ("rss", "feed", "RDF"):
        return None
    channel = _child(root, "channel")
    feed_title = _text(_child(channel if channel is not None else root, "title")) or None
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    entries: list[dict[str, Any]] = []
    for it in items[:MAX_ENTRIES]:
        title = _text(_child(it, "title"))
        url = _link(it)
        if not title or not url.startswith(("http://", "https://")):
            continue
        published = _date(_text(_child(it, "pubDate", "published", "updated", "date")))
        summary = _text(_child(it, "description", "summary", "content"))[:SUMMARY_CHARS]
        entries.append({"title": title, "url": url, "published_at": published, "summary": summary})
    if not entries:
        return None
    blocks = [f"# {feed_title}" if feed_title else "# (订阅源)"]
    for e in entries:
        blocks.append(f"### {e['title']}\n发布：{e['published_at'] or '未知'}\n链接：{e['url']}" + (f"\n{e['summary']}" if e["summary"] else ""))
    text = "\n\n".join(blocks)
    return ParsedDocument(kind="feed", title=feed_title, text=text, sections=sections_from_markdown(text), method="feed", meta={"entries": entries})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def match_entry(entries: list[dict[str, Any]], answer: str | None, quote: str | None) -> dict[str, Any] | None:
    """答案引用的那一条：答案或引文里写了它的文章链接，或引文就是（或包含）它的标题。对不上返回 None。"""
    hay = f"{answer or ''} {quote or ''}"
    for e in entries:
        if e["url"] in hay:
            return e
    q = _norm(quote or "")
    if len(same := [e for e in entries if _norm(e["title"]) == q]) == 1:     # 引文就是某一条的标题（短标题如版本号也认，但要唯一）
        return same[0]
    if len(q) < 12:
        return None
    hits = [e for e in entries if (t := _norm(e["title"])) and (t in q or (q in t and len(q) >= 0.6 * len(t)))]
    return max(hits, key=lambda e: len(e["title"])) if hits else None
