"""read_document 的分发器（规格 6.1）：按 MIME 和文件签名（不只看后缀）路由到 HTML/PDF/Office/图片解析；旧格式先转换；扫描页走视觉。"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import urlsplit

from owa_research.core.document import ParsedDocument
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.model_client import ModelClient
from owa_research.readers import office, pdf, vision
from owa_research.readers.html import parse_html

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_UNZIPPED_BYTES = 300 * 1024 * 1024
MAX_ZIP_RATIO = 200
_IMAGE_MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif", b"RIFF": "image/webp"}


@dataclass(frozen=True)
class Sniffed:
    kind: str  # html / pdf / docx / xlsx / pptx / doc / xls / ppt / image / eml / text / unknown
    mime: str


def sniff(data: bytes, content_type: str = "", url: str = "") -> Sniffed:
    head = data[:2048]
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = urlsplit(url).path.rsplit(".", 1)[-1].lower() if "." in urlsplit(url).path else ""
    if head.startswith(b"%PDF"):
        return Sniffed("pdf", "application/pdf")
    for magic, mime in _IMAGE_MAGIC.items():
        if head.startswith(magic):
            return Sniffed("image", mime)
    if head.startswith(b"PK\x03\x04"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = set(zf.namelist())
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_UNZIPPED_BYTES or (len(data) > 0 and total / len(data) > MAX_ZIP_RATIO):
                raise ToolError(ErrorCode.EXTRACTION_FAILED, f"压缩包解压尺寸 {total} 字节超过上限或压缩比异常", stage="parse")
        except zipfile.BadZipFile:
            names = set()
        if any(n.startswith("word/") for n in names):
            return Sniffed("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if any(n.startswith("xl/") for n in names):
            return Sniffed("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if any(n.startswith("ppt/") for n in names):
            return Sniffed("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        return Sniffed("unknown", "application/zip")
    if head.startswith(_OLE_MAGIC):
        if ext in ("doc", "xls", "ppt"):
            return Sniffed(ext, f"application/vnd.ms-{ {'doc': 'word', 'xls': 'excel', 'ppt': 'powerpoint'}[ext] }")
        if "word" in ct or "msword" in ct:
            return Sniffed("doc", "application/msword")
        if "excel" in ct:
            return Sniffed("xls", "application/vnd.ms-excel")
        if "powerpoint" in ct:
            return Sniffed("ppt", "application/vnd.ms-powerpoint")
        return Sniffed("doc", "application/msword")  # OLE 默认按 doc 试
    if head.startswith(b"{\\rtf"):
        return Sniffed("rtf", "application/rtf")
    if ext == "eml" or ct == "message/rfc822":
        return Sniffed("eml", "message/rfc822")
    text_head = head.decode("utf-8", errors="ignore").lower()
    if "<html" in text_head or "<!doctype html" in text_head or "html" in ct:
        return Sniffed("html", "text/html")
    if ct.startswith("text/") or ct in ("application/json", "application/xml") or re.fullmatch(r"[\x09\x0a\x0d\x20-\x7e-￿]*", text_head):
        return Sniffed("text", ct or "text/plain")
    return Sniffed("unknown", ct or "application/octet-stream")


BULLET = re.compile(r"\s{2,}|[-*•·]\s|\d+[.)]\s|[A-Za-z][.)]\s")


def unwrap_hard_breaks(text: str) -> str:
    """硬换行排版的纯文本（每行到固定宽度就折行，如 Project Gutenberg 的 .txt）：把同一段里被折开的行接回一行，空行仍是分段。
    先判断是不是这种排版：非空行里九成不超过 100 字、且至少一半的行长在折行宽度（第 90 百分位的行长）的 75% 以上；不是就原样返回。
    接行：这一行长到折行宽度的 60% 以上（是被折下来的），下一行不空、不以缩进或列表符号开头；短行（标题、诗、居中的字）原样保留。"""
    lines = text.split("\n")
    lens = sorted(len(ln.rstrip()) for ln in lines if ln.strip())
    if len(lens) < 20:
        return text
    width = lens[int(len(lens) * 0.9) - 1]
    if not 40 <= width <= 100 or sum(n >= 0.75 * width for n in lens) < len(lens) / 2:
        return text
    out = [lines[0]]
    for ln in lines[1:]:
        prev = out[-1]
        if prev.strip() and ln.strip() and len(prev.rstrip()) >= 0.6 * width and not BULLET.match(ln):
            out[-1] = prev.rstrip() + " " + ln.strip()
        else:
            out.append(ln)
    return "\n".join(out)


async def parse_document(
    data: bytes,
    *,
    content_type: str = "",
    url: str = "",
    model: ModelClient | None = None,
    pages: list[int] | None = None,
    extract_tables: bool = True,
    max_vision_pages: int = 8,
    render_dpi: int = 110,
    vision_pages: list[int] | None = None,
    merge_tables: bool = False,
) -> tuple[ParsedDocument, Sniffed]:
    from owa_research.readers.feed import parse_feed

    if (feed := parse_feed(data)) is not None:                  # RSS / Atom：解析成条目清单（readers/feed.py）
        return feed, Sniffed("feed", "application/rss+xml")
    s = sniff(data, content_type, url)
    if s.kind == "html":
        return parse_html(data.decode("utf-8", errors="replace"), url=url), s
    if s.kind == "eml":
        return _parse_eml(data), s
    if s.kind == "text":
        text = data.decode("utf-8", errors="replace")
        if s.mime == "text/plain":                        # JSON / XML / CSV / 代码的换行有意义，不接
            text = unwrap_hard_breaks(text)
        from owa_research.core.document import Section

        return ParsedDocument(kind="text", title=None, text=text, sections=[Section(id="s0", title="(全文)", level=0, char_start=0, char_end=len(text))], method="text"), s
    if s.kind in ("doc", "xls", "ppt", "rtf"):
        data, target = await office.convert_legacy(data, s.kind)
        s = Sniffed(target, s.mime)
    if s.kind == "docx":
        return office.parse_docx(data), s
    if s.kind == "xlsx":
        return office.parse_xlsx(data), s
    if s.kind == "pptx":
        return office.parse_pptx(data), s
    if s.kind == "pdf":
        doc = pdf.parse_pdf(data, pages=pages, extract_tables=extract_tables, merge_tables=merge_tables)
        # 整体有文字层但个别页是纯图（OCR 缺失）或调用方显式要求看页图：只对这些页走视觉，其余沿用文字层
        # 自动视觉只对调用方点名的 pages 里的纯图页生效；其余纯图页只在 outline 的 scanned_pages 里报告，由研究模型决定
        auto = set(doc.meta.get("scanned_pages", [])) & set(pages or [])
        partial = sorted(set(vision_pages or []) | auto)
        if not doc.scanned and partial and model is not None:
            partial = partial[:max_vision_pages]
            results = []
            for pno in partial:
                png = pdf.render_page_png(data, pno, dpi=render_dpi)
                results.append((pno, await vision.read_page_image(model, png, page=pno)))
            doc = _merge_vision_pages(doc, results, merge_tables=merge_tables)
            doc.meta["vision_pages"] = len(results)
        if doc.scanned:
            if model is None:
                doc.warnings.append("扫描件但未提供视觉模型，只返回空文字层")
                return doc, s
            page_nos = pages or [p.page for p in doc.pages]
            if len(page_nos) > max_vision_pages:
                doc.warnings.append(f"扫描页 {len(page_nos)} 页超过本次视觉预算 {max_vision_pages}，只读前 {max_vision_pages} 页")
                page_nos = page_nos[:max_vision_pages]
            results = []
            for pno in page_nos:  # 串行，不并发占模型
                png = pdf.render_page_png(data, pno, dpi=render_dpi)
                results.append((pno, await vision.read_page_image(model, png, page=pno)))
            vdoc = vision.assemble_pages("pdf", results, title=doc.title, merge_tables=merge_tables)
            vdoc.warnings = doc.warnings + vdoc.warnings
            vdoc.meta = {**doc.meta, **vdoc.meta, "vision_pages": len(results)}
            vdoc.published_at = doc.published_at
            return vdoc, s
        return doc, s
    if s.kind == "image":
        if model is None:
            raise ToolError(ErrorCode.MODEL_CAPABILITY_UNVERIFIED, "读取图片需要视觉模型", stage="ocr")
        result = await vision.read_page_image(model, data, page=1, mime=s.mime)
        return vision.assemble_pages("image", [(1, result)], merge_tables=merge_tables), s
    raise ToolError(ErrorCode.UNSUPPORTED_FORMAT, f"不支持的格式: {s.mime}", stage="parse")


def _parse_eml(data: bytes) -> ParsedDocument:
    """邮件（.eml）：头部（发件人、收件人、抄送、日期、主题）+ 正文（有纯文本用纯文本，只有 HTML 的转成文字）+ 邮件里附件的名单（附件内容不读）。"""
    from email import policy
    from email.parser import BytesParser

    from owa_research.core.document import Section

    msg = BytesParser(policy=policy.default).parsebytes(data)
    head = [f"{k}: {msg[k]}" for k in ("From", "To", "Cc", "Date", "Subject") if msg[k]]
    body, text = msg.get_body(preferencelist=("plain", "html")), ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, ValueError):            # 声明的字符集不认识：按 UTF-8 解码，失败的字节用替代字符
            text = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if body.get_content_type() == "text/html":
            text = parse_html(text).text
    names = [p.get_filename() or p.get_content_type() for p in msg.iter_attachments()]
    full = "\n".join(head) + "\n\n" + text.strip() + ("\n\n（邮件里的附件，内容没读：" + "、".join(names) + "）" if names else "")
    return ParsedDocument(kind="eml", title=str(msg["Subject"] or "") or None, text=full,
                          sections=[Section(id="s0", title="(全文)", level=0, char_start=0, char_end=len(full))], method="email")


def _merge_vision_pages(doc: ParsedDocument, results: list[tuple[int, dict]], *, merge_tables: bool = False) -> ParsedDocument:
    """把视觉读取的页文本/表格并入文字层文档：替换该页文本，追加该页表格（merge_tables 时表格行按全局索引放在该页文字之前）。"""
    vdoc = vision.assemble_pages(doc.kind, results)
    for i, t in enumerate(vdoc.tables):
        t.index = len(doc.tables) + i
    by_page = {span.page: vdoc.text[span.char_start:span.char_end] for span in vdoc.pages}
    if merge_tables:
        for pno, chunk in by_page.items():
            hdr = f"\n\n<!-- page {pno} (vision) -->\n"
            if (lines := pdf.table_lines([t for t in vdoc.tables if t.page == pno])) and chunk.startswith(hdr):
                by_page[pno] = hdr + lines + "\n" + chunk[len(hdr):]
    parts: list[str] = []
    new_pages = []
    pos = 0
    shift: dict[int, int] = {}
    for span in doc.pages:
        chunk = by_page.get(span.page, doc.text[span.char_start:span.char_end])
        shift[span.page] = pos - span.char_start
        new_pages.append(type(span)(page=span.page, char_start=pos, char_end=pos + len(chunk)))
        parts.append(chunk)
        pos += len(chunk)
    for sec in doc.sections:
        delta = shift.get(sec.page or 0, 0)
        sec.char_start += delta
        sec.char_end = min(pos, sec.char_end + delta)
    doc.text = "".join(parts)
    doc.pages = new_pages
    doc.tables.extend(vdoc.tables)
    doc.warnings += vdoc.warnings + [f"第 {p} 页由视觉读取（文字层缺失或按要求）" for p, _ in results]
    doc.method = doc.method + "+vision"
    return doc
