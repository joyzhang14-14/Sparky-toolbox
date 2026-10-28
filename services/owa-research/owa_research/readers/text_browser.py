"""分页文本浏览器（规格 5 browse_document）：8K 字符一屏，"第 X 页共 Y 页"，翻页、跳页、页内查找。状态按 artifact 保存在研究会话内存中。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from owa_research.core.document import ParsedDocument

SCREEN_CHARS = 8000


def find_all(text: str, query: str, limit: int = 500) -> list[int]:
    """不分大小写、空白容错的查找：查询里的空白可匹配原文任意空白（含换行），PDF 里被换行拆开的名字也能命中。"""
    words = query.split()
    if not words:
        return []
    pat = re.compile(r"\s+".join(re.escape(w) for w in words), re.I)
    return [m.start() for _, m in zip(range(limit), pat.finditer(text))]


@dataclass
class _State:
    screen: int = 0
    query: str | None = None
    hits: list[int] = field(default_factory=list)
    hit_pos: int = -1


class TextBrowser:
    def __init__(self, screen_chars: int = SCREEN_CHARS) -> None:
        self._screen = screen_chars
        self._states: dict[str, _State] = {}

    def _screens(self, doc: ParsedDocument) -> int:
        return max(1, (len(doc.text) + self._screen - 1) // self._screen)

    def view(self, artifact_id: str, doc: ParsedDocument, action: str, *, page: int | None = None, query: str | None = None) -> dict:
        st = self._states.setdefault(artifact_id, _State())
        total = self._screens(doc)
        note = ""
        if action == "open":
            st.screen = 0
        elif action == "page_down":
            st.screen = min(total - 1, st.screen + 1)
        elif action == "page_up":
            st.screen = max(0, st.screen - 1)
        elif action == "goto":
            if page is None:
                raise ValueError("goto 需要 page")
            st.screen = min(total - 1, max(0, page - 1))
        elif action in ("find", "find_next"):
            if action == "find":
                if not query:
                    raise ValueError("find 需要 query")
                st.query = query
                st.hits = find_all(doc.text, query)
                st.hit_pos = -1
            if not st.hits:
                note = f"未找到 {st.query!r}"
            else:
                st.hit_pos = (st.hit_pos + 1) % len(st.hits)
                st.screen = st.hits[st.hit_pos] // self._screen
                note = f"命中 {st.hit_pos + 1}/{len(st.hits)}，位于第 {st.screen + 1} 屏"
        else:
            raise ValueError(f"未知动作 {action}")
        start = st.screen * self._screen
        end = min(len(doc.text), start + self._screen)
        doc_page = doc.page_of(start)
        header = f"[第 {st.screen + 1} 屏 / 共 {total} 屏；字符 {start}-{end}" + (f"；文档页 {doc_page}" if doc_page else "") + "]"
        if note:
            header += f" {note}"
        return {"screen": st.screen + 1, "total_screens": total, "char_start": start, "char_end": end, "document_page": doc_page, "note": note, "text": header + "\n" + doc.text[start:end]}
