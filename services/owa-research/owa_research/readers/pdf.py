"""PDF 文本层与表格提取（规格 6.3、6.4）。

- 文本层：PyMuPDF 按页取文本，保存页码范围；`find_tables` 取有线表格（bbox、单元格）。
- 无边框财务表："文本对齐"策略：从右对齐的数字列推导列分割线，用 accuracy / whitespace 分数做质量门。
- 扫描页判定：抽样至多 10 页，平均每页字符数 < 50、字体映射错误比例 ≥ 0.04、异常字符比例 ≥ 0.03、图片覆盖 ≥ 0.8 任一命中。
- 页图渲染给同一大模型串行视觉读取（在 readers/vision.py），本模块只负责渲染 PNG。
- merge_tables（D20 B1）：表格行按页并入正文、放在该页文字层之前，行前缀 `[表N p.P] `（N 是全局表格索引；低置信行 `[表N p.P ?] `），
  单元格用 ` | ` 分隔。分列排版的表（名字一列、编号一列）文字层里对不上号，只有表格行能让名字与编号同行，find / 读取 / 引文核对都看这一份文本。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from owa_research.core.document import PageSpan, ParsedDocument, Section, TableBlock

_NUM_RE = re.compile(r"^\(?-?\$?[\d,]+(\.\d+)?\)?%?$|^[—–-]$")
_ABNORMAL_RE = re.compile("[�\x00-\x08\x0b\x0c\x0e-\x1f]")
_CODE_RE = re.compile(r"[A-Za-z]{0,3}\d{1,5}[A-Za-z]?")    # 编号型单元格行：E257、F362、C90、2024


@lru_cache(maxsize=1)
def _fitz():
    import pymupdf as fitz  # PyMuPDF，懒加载

    return fitz


@dataclass
class ScanMetrics:
    sampled_pages: int
    avg_chars: float
    font_error_ratio: float
    abnormal_ratio: float
    image_coverage: float
    scanned: bool
    reasons: list[str]


def scan_metrics(pdf_bytes: bytes, *, sample: int = 10) -> ScanMetrics:
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = doc.page_count
    idx = list(range(n)) if n <= sample else [round(i * (n - 1) / (sample - 1)) for i in range(sample)]
    chars: list[int] = []
    font_err = 0
    font_total = 0
    abnormal = 0
    coverage: list[float] = []
    for i in idx:
        page = doc[i]
        text = page.get_text("text")
        chars.append(len(text.strip()))
        abnormal += len(_ABNORMAL_RE.findall(text))
        for f in page.get_fonts(full=True):  # (xref, ext, type, basefont, name, encoding, referenced)
            font_total += 1
            if not f[3] or (f[2] == "Type3" and not f[5]):  # 无 basefont，或无编码的 Type3：字符无法映射
                font_err += 1
        area = float(page.rect.width * page.rect.height) or 1.0
        img_area = 0.0
        for info in page.get_image_info():
            b = info.get("bbox")
            if b:
                img_area += max(0.0, (b[2] - b[0])) * max(0.0, (b[3] - b[1]))
        coverage.append(min(1.0, img_area / area))
    total_chars = sum(chars) or 1
    m = ScanMetrics(
        sampled_pages=len(idx),
        avg_chars=statistics.mean(chars) if chars else 0.0,
        font_error_ratio=(font_err / font_total) if font_total else 0.0,
        abnormal_ratio=abnormal / total_chars,
        image_coverage=statistics.mean(coverage) if coverage else 0.0,
        scanned=False,
        reasons=[],
    )
    if m.avg_chars < 50:
        m.reasons.append("avg_chars<50")
    if m.font_error_ratio >= 0.04:
        m.reasons.append("font_error_ratio>=0.04")
    if m.abnormal_ratio >= 0.03:
        m.reasons.append("abnormal_ratio>=0.03")
    if m.image_coverage >= 0.8 and m.avg_chars < 500:
        m.reasons.append("image_coverage>=0.8")
    m.scanned = bool(m.reasons)
    doc.close()
    return m


def _cell(c: str | None) -> str:
    """单元格压成一行：多行且每行都是编号的格（'F279\\nF280'）用 ' / ' 保留分隔，其它格空白压成一个空格；None 当空串。"""
    lines = [x.strip() for x in (c or "").split("\n") if x.strip()]
    if len(lines) >= 2 and all(_CODE_RE.fullmatch(x) for x in lines):
        return " / ".join(lines)
    return " ".join((c or "").split())


def table_lines(tables: list[TableBlock]) -> str:
    """表格行并入正文用的文本（行级门）：退化表（不到 4 行且有超过 400 字的格）整表不并入；
    alignment / uncertain 表的行、以及有格内换行超过 3 处的行，前缀带 `?`（低置信，保留）。"""
    out: list[str] = []
    for t in tables:
        if len(t.rows) < 4 and max((len(c or "") for r in t.rows for c in r), default=0) > 400:
            continue
        low = t.method == "alignment" or t.uncertain
        for r in t.rows:
            cells = [_cell(c) for c in r]
            if not any(cells):
                continue
            q = low or any((c or "").count("\n") > 3 for c in r)
            out.append(f"[表{t.index} p.{t.page}{' ?' if q else ''}] " + " | ".join(cells))
    return "\n".join(out)


def parse_pdf(pdf_bytes: bytes, *, pages: list[int] | None = None, extract_tables: bool = True, max_chars: int = 50_000_000,
              merge_tables: bool = False) -> ParsedDocument:
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    metrics = scan_metrics(pdf_bytes)
    parts: list[str] = []
    spans: list[PageSpan] = []
    tables: list[TableBlock] = []
    sections: list[Section] = []
    scanned_pages: list[int] = []
    pos = 0
    wanted = set(pages) if pages else None
    for i in range(doc.page_count):
        pno = i + 1
        if wanted and pno not in wanted:
            continue
        page = doc[i]
        text = page.get_text("text").strip()
        if len(text) < 50 or (len(text) < 400 and _image_coverage(page) >= 0.8):  # 纯图页，或整页是图且只剩零星 OCR 残字
            scanned_pages.append(pno)
        header = f"\n\n<!-- page {pno} -->\n"
        page_tables = _page_tables(page, pno, len(tables)) if extract_tables else []
        tables.extend(page_tables)
        rows = table_lines(page_tables) if merge_tables else ""
        chunk = header + (rows + "\n" if rows else "") + text
        spans.append(PageSpan(page=pno, char_start=pos, char_end=pos + len(chunk)))
        parts.append(chunk)
        pos += len(chunk)
        if pos > max_chars:
            parts.append("\n\n[截断：文档超过字符上限]")
            break
    # 目录（大纲）作为章节
    for level, title, pno in doc.get_toc(simple=True) or []:
        span = next((s for s in spans if s.page == pno), None)
        if span:
            sections.append(Section(id=f"s{len(sections)}", title=title, level=level, char_start=span.char_start, char_end=span.char_end, page=pno))
    # 修正章节结束位置到下一章节起点
    for a, b in zip(sections, sections[1:]):
        a.char_end = max(a.char_start, b.char_start)
    if sections and spans:
        sections[-1].char_end = spans[-1].char_end
    if not sections:
        sections = [Section(id=f"s{i}", title=f"第 {s.page} 页", level=1, char_start=s.char_start, char_end=s.char_end, page=s.page) for i, s in enumerate(spans)]
    meta = doc.metadata or {}
    total_pages = doc.page_count
    doc.close()
    return ParsedDocument(
        kind="pdf", title=(meta.get("title") or None), text="".join(parts), sections=sections, tables=tables, pages=spans,
        warnings=([f"疑似扫描件: {', '.join(metrics.reasons)}"] if metrics.scanned else []),
        method="pymupdf", scanned=metrics.scanned, published_at=_pdf_date(meta.get("creationDate")),
        meta={"page_count": len(spans), "total_pages": total_pages, "scan_metrics": metrics.__dict__, "scanned_pages": scanned_pages,
              **({"tables_merged": True} if merge_tables else {})},
    )


def _image_coverage(page: Any) -> float:
    area = float(page.rect.width * page.rect.height) or 1.0
    img = 0.0
    for info in page.get_image_info():
        b = info.get("bbox")
        if b:
            img += max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return min(1.0, img / area)


def _page_tables(page: Any, pno: int, start_index: int) -> list[TableBlock]:
    out: list[TableBlock] = []
    try:
        found = page.find_tables()
        for t in found.tables:
            rows = [[(c or "").strip() for c in r] for r in t.extract()]   # 合并单元格返回 None：当空串
            rows = [r for r in rows if any(r)]
            if len(rows) >= 2:
                out.append(TableBlock(index=start_index + len(out), rows=rows, headers=rows[0], page=pno, bbox=list(t.bbox), method="text-layer"))
    except Exception:  # noqa: BLE001 — find_tables 对个别页会抛错
        pass
    if not out:
        out.extend(alignment_tables(page, pno, start_index))
    return out


def alignment_tables(page: Any, pno: int, start_index: int, *, min_rows: int = 4, min_quality: float = 0.6) -> list[TableBlock]:
    """无边框表：按行聚类词，再按数字词的右边缘 x1 聚类列；行内首个非数字词块为标签列。"""
    words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wordno
    if not words:
        return []
    lines: dict[int, list[tuple]] = {}
    for w in words:
        key = round((w[1] + w[3]) / 2 / 3)  # 3pt 容差
        lines.setdefault(key, []).append(w)
    numeric_rows: list[tuple[float, list[tuple], list[tuple]]] = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda w: w[0])
        nums = [w for w in ws if _NUM_RE.match(w[4])]
        if len(nums) >= 2:
            label = [w for w in ws if w not in nums and w[0] < min(n[0] for n in nums)]
            numeric_rows.append((key * 3, label, nums))
    if len(numeric_rows) < min_rows:
        return []
    # 列边界：数字词右边缘聚类
    edges = sorted(n[2] for _, _, nums in numeric_rows for n in nums)
    cols: list[list[float]] = []
    for e in edges:
        if cols and e - cols[-1][-1] < 12:
            cols[-1].append(e)
        else:
            cols.append([e])
    centers = [statistics.mean(c) for c in cols if len(c) >= max(2, len(numeric_rows) // 3)]
    if len(centers) < 2:
        return []
    rows: list[list[str]] = []
    filled = 0
    header_rows: list[str] = []
    for _, label, nums in numeric_rows:
        row = [" ".join(w[4] for w in label)] + [""] * len(centers)
        hit = 0
        for n in nums:
            j = min(range(len(centers)), key=lambda k: abs(centers[k] - n[2]))
            if abs(centers[j] - n[2]) < 12:
                row[j + 1] = (row[j + 1] + " " + n[4]).strip()
                hit += 1
        if hit == 0:
            # 数字没落进任何列（如日期表头行），不算数据行；出现在首个数据行之前时视为表头
            if not rows:
                header_rows.append(" ".join(w[4] for w in label + nums))
            continue
        filled += hit
        rows.append(row)
    if len(rows) < min_rows:
        return []
    quality = filled / (len(rows) * len(centers))  # accuracy 近似：数字落入列的比例
    whitespace = 1 - quality
    if quality <= min_quality:          # 边界不放行：地图、页眉这类只凑够门槛的"表"（摊位表第 1 页 0.6）
        return []
    # 表头：首个数据行之前最近的两行文字（含被剔除的日期行）
    y_first = min(y for y, _, nums in numeric_rows if any(abs(min(centers, key=lambda c: abs(c - n[2])) - n[2]) < 12 for n in nums))
    text_lines = [" ".join(w[4] for w in sorted(lines[k], key=lambda w: w[0])) for k in sorted(lines) if k * 3 < y_first]
    headers = text_lines[-2:]
    return [TableBlock(index=start_index, rows=rows, headers=[""] + headers[-1:] if headers else [], title=" | ".join(headers) or None, page=pno,
                       method="alignment", quality=round(quality, 3), uncertain=whitespace > 0.25)]


def render_page_png(pdf_bytes: bytes, page: int, *, dpi: int = 110, max_pixels: int = 4_000_000) -> bytes:
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    p = doc[page - 1]
    scale = dpi / 72
    w, h = p.rect.width * scale, p.rect.height * scale
    if w * h > max_pixels:
        scale *= (max_pixels / (w * h)) ** 0.5
    pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


def _pdf_date(v: str | None) -> str | None:
    if not v:
        return None
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", v)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
