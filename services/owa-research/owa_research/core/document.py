"""解析后文档的统一模型：正文 + 章节 + 表格 + 页范围。所有 reader 输出这个结构，存入证据存储的 parsed artifact。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Section:
    id: str
    title: str
    level: int
    char_start: int
    char_end: int
    page: int | None = None


@dataclass
class PageSpan:
    page: int
    char_start: int
    char_end: int


@dataclass
class TableBlock:
    index: int
    rows: list[list[str]]  # 原始单元格文本，含表头行
    headers: list[str] = field(default_factory=list)
    title: str | None = None
    page: int | None = None
    bbox: list[float] | None = None
    method: str = "text-layer"  # text-layer / alignment / table-model / vision / html / office
    header_rows: int = 1
    footnotes: list[str] = field(default_factory=list)
    quality: float | None = None  # camelot 式 accuracy 分数，None 表示未评估
    uncertain: bool = False
    sheet: str | None = None
    cell_origin: str | None = None  # 表格左上角单元格地址（XLSX）


@dataclass
class ParsedDocument:
    kind: str  # html / pdf / docx / xlsx / pptx / image / text / feed
    title: str | None
    text: str
    sections: list[Section] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    pages: list[PageSpan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    method: str = ""
    scanned: bool = False
    published_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ParsedDocument":
        return cls(
            kind=d["kind"], title=d.get("title"), text=d.get("text", ""),
            sections=[Section(**s) for s in d.get("sections", [])],
            tables=[TableBlock(**t) for t in d.get("tables", [])],
            pages=[PageSpan(**p) for p in d.get("pages", [])],
            warnings=d.get("warnings", []), method=d.get("method", ""), scanned=d.get("scanned", False),
            published_at=d.get("published_at"), meta=d.get("meta", {}),
        )

    def page_of(self, char_index: int) -> int | None:
        for p in self.pages:
            if p.char_start <= char_index < p.char_end:
                return p.page
        return None

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def outline(self, max_items: int = 200) -> dict[str, Any]:
        """只给标题/章节/表格清单，不给正文。"""
        return {
            "title": self.title,
            "kind": self.kind,
            "chars": len(self.text),
            "pages": len(self.pages) or None,
            "scanned": self.scanned,
            "scanned_pages": self.meta.get("scanned_pages", [])[:50],
            "sections": [{"id": s.id, "title": s.title[:120], "level": s.level, "chars": s.char_end - s.char_start, "page": s.page} for s in self.sections[:max_items]],
            "tables": [{"index": t.index, "title": (t.title or "")[:120], "rows": len(t.rows), "cols": max((len(r) for r in t.rows), default=0), "page": t.page, "sheet": t.sheet, "method": t.method} for t in self.tables[:max_items]],
            "warnings": self.warnings[:20],
        }


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def sections_from_markdown(text: str) -> list[Section]:
    """按 Markdown 标题切章节；正文开头无标题的部分作为 s0。"""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[Section] = []
    if not matches:
        return [Section(id="s0", title="(全文)", level=0, char_start=0, char_end=len(text))] if text.strip() else []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        sections.append(Section(id="s0", title="(前言)", level=0, char_start=0, char_end=matches[0].start()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(Section(id=f"s{len(sections) + (0 if sections and sections[0].id == 's0' else 1)}", title=m.group(2).strip(), level=len(m.group(1)), char_start=m.start(), char_end=end))
    # 保证 id 唯一且顺序
    for i, s in enumerate(sections):
        s.id = f"s{i}"
    return sections


_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_LINE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def tables_from_markdown(text: str, *, method: str = "html") -> list[TableBlock]:
    """把 Markdown 管道表格解析成 TableBlock（用于 trafilatura 输出）。"""
    tables: list[TableBlock] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if _TABLE_LINE.match(lines[i]):
            block = []
            while i < len(lines) and _TABLE_LINE.match(lines[i]):
                block.append(lines[i])
                i += 1
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in block if not _SEP_LINE.match(ln)]
            if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                tables.append(TableBlock(index=len(tables), rows=rows, headers=rows[0], method=method))
        else:
            i += 1
    return tables
