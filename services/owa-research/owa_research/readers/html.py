"""HTML 正文与表格提取（规格 6.2）。

- 正文：lxml 按 DOM 块级结构转 Markdown（段落、标题、列表、表格原样保留，列表页、榜单、搜索结果不会被当成杂物删掉）；
  trafilatura 只读元数据（标题、日期、作者）。
- 表格：始终从 DOM 结构化提取（处理 rowspan/colspan），过度简化的 Markdown 会丢失复杂表格。
内存卫生：调用方在抓取后先释放 HTTP 原始缓冲区，再调用这里；解析器懒加载。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from owa_research.core.document import ParsedDocument, TableBlock, sections_from_markdown

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "td", "th", "dd", "dt"}
_SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "button", "svg", "iframe", "template"}


@lru_cache(maxsize=1)
def _trafilatura():
    import trafilatura

    return trafilatura


@lru_cache(maxsize=1)
def _lxml_html():
    import lxml.html

    return lxml.html


def parse_html(html: str, *, url: str | None = None, max_chars: int = 50_000_000) -> ParsedDocument:
    tf = _trafilatura()
    meta = _metadata(tf, html, url)
    tree = _parse_tree(html)
    tables = _dom_tables(tree) if tree is not None else []
    text = _dom_to_markdown(tree) if tree is not None else ""
    method = "lxml-blocks"
    if not text.strip():
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I))
        text = re.sub(r"[ \t]+", " ", text).strip()
        method = "strip-tags"
    warnings = []
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[截断：正文超过 {max_chars} 字符]"
        warnings.append(f"正文截断到 {max_chars} 字符")
    return ParsedDocument(
        kind="html", title=meta.get("title") or _title_from_html(html), text=text,
        sections=sections_from_markdown(text), tables=tables, warnings=warnings, method=method,
        published_at=meta.get("date"), meta={"author": meta.get("author"), "sitename": meta.get("sitename")},
    )


def _parse_tree(html: str):
    try:
        tree = _lxml_html().fromstring(html)
    except Exception:  # noqa: BLE001
        return None
    for bad in tree.xpath("//script|//style|//noscript|//template"):
        bad.getparent().remove(bad)
    return tree


def _dom_tables(tree: Any) -> list[TableBlock]:
    tables: list[TableBlock] = []
    for t in tree.xpath("//table"):
        if t.xpath("./ancestor::table"):
            continue  # 嵌套表格只取最外层
        grid: list[list[str]] = []
        spans: dict[tuple[int, int], str] = {}
        header_rows = 0
        for r_i, tr in enumerate(t.xpath(".//tr")):
            row: list[str] = []
            c = 0
            cells = [x for x in tr if x.tag in ("td", "th")]
            if cells and all(x.tag == "th" for x in cells) and r_i == header_rows:
                header_rows += 1
            for cell in cells:
                while (r_i, c) in spans:
                    row.append(spans.pop((r_i, c)))
                    c += 1
                txt = re.sub(r"\s+", " ", cell.text_content()).strip()
                rs, cs = _int(cell.get("rowspan"), 1), _int(cell.get("colspan"), 1)
                for k in range(cs):
                    row.append(txt if k == 0 else "")
                    for dr in range(1, rs):
                        spans[(r_i + dr, c + k)] = txt if k == 0 else ""
                c += cs
            while (r_i, c) in spans:
                row.append(spans.pop((r_i, c)))
                c += 1
            if any(row):
                grid.append(row)
        if len(grid) >= 2 and max(len(r) for r in grid) >= 2:
            caption = t.xpath("string(./caption)").strip() or None
            tables.append(TableBlock(index=len(tables), rows=grid, headers=grid[0], title=caption, method="html", header_rows=max(1, header_rows)))
    return tables


def _dom_to_markdown(tree: Any) -> str:
    out: list[str] = []

    def walk(el: Any) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in _SKIP_TAGS:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append("\n" + "#" * int(tag[1]) + " " + _clean(el.text_content()) + "\n")
            return
        if tag == "table":
            rows = [[_clean(c.text_content()) for c in tr if c.tag in ("td", "th")] for tr in el.xpath(".//tr")]
            rows = [r for r in rows if any(r)]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append("\n| " + " | ".join(rows[0]) + " |\n|" + " --- |" * width + "\n" + "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]) + "\n")
            return
        if tag == "li":
            out.append("- " + _clean(el.text_content()) + "\n")
            return
        if tag in ("p", "pre", "blockquote", "dd", "dt"):
            t = _clean(el.text_content())
            if t:
                out.append(t + "\n\n")
            return
        if el.text and el.text.strip() and tag not in _BLOCK_TAGS:
            out.append(_clean(el.text) + " ")
        elif el.text and el.text.strip():
            out.append(_clean(el.text) + "\n")
        for child in el:
            walk(child)
            if child.tail and child.tail.strip():
                out.append(_clean(child.tail) + ("\n" if tag in _BLOCK_TAGS else " "))

    body = tree.xpath("//body")
    walk(body[0] if body else tree)
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _int(v: str | None, default: int) -> int:
    try:
        return max(1, min(int(v), 50)) if v else default
    except ValueError:
        return default


def _metadata(tf: Any, html: str, url: str | None) -> dict[str, Any]:
    try:
        m = tf.extract_metadata(html, default_url=url)
        if m is None:
            return {}
        return {"title": m.title, "date": m.date, "author": m.author, "sitename": m.sitename}
    except Exception:  # noqa: BLE001
        return {}


def _title_from_html(html: str) -> str | None:
    m = _TITLE_RE.search(html[:100_000])
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
