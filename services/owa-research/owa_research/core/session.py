"""研究会话状态：来源登记、解析缓存、证据账本、页面读取的统一入口。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from owa_research.core.document import ParsedDocument
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.runtime import Runtime
from owa_research.core.schema import AccessFailure, Locator, Source
from owa_research.readers.document import parse_document
from owa_research.readers.text_browser import TextBrowser


# 路径或查询串里嵌着另一个网址（含百分号编码）：r.jina.ai/https://…、在线 PDF 查看器 ?url=https://… 这类"代理转读"
NESTED_URL = re.compile(r"(?i)https?(?::|%3a)(?:/|%2f){1,2}")


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def nested_url(url: str) -> bool:
    parts = urlsplit(url)
    rest = f"{parts.path}?{parts.query}"
    return bool(NESTED_URL.search(rest) or NESTED_URL.search(unquote(rest)))


@dataclass
class Evidence:
    """一条证据：来自某来源的原文片段与定位（研究模型可引用）。"""

    evidence_id: str
    source_id: str
    artifact_id: str | None
    quote: str
    locator: Locator
    note: str | None = None


@dataclass
class ResearchSession:
    rt: Runtime
    sources: dict[str, Source] = field(default_factory=dict)  # source_id -> Source
    source_by_key: dict[str, str] = field(default_factory=dict)  # url / artifact_id -> source_id
    docs: dict[str, ParsedDocument] = field(default_factory=dict)  # artifact_id(原始) -> 解析结果
    parsed_ids: dict[str, str] = field(default_factory=dict)  # 原始 artifact_id -> parsed artifact_id
    rendered: dict[tuple, str] = field(default_factory=dict)  # (url, 渲染参数) -> 原始 artifact_id；render_page 不复用静态抓取的结果
    evidence: list[Evidence] = field(default_factory=list)
    _evidence_keys: dict[tuple[str, str], Evidence] = field(default_factory=dict, repr=False)
    failures: list[AccessFailure] = field(default_factory=list)
    browser: TextBrowser = field(default_factory=TextBrowser)
    pages_read: int = 0
    documents_read: int = 0
    ocr_pages: int = 0
    max_vision_pages: int = 8
    # strict_egress（D17，isolated-web-search 用）：只访问 seed_urls 与搜索结果里出现过的域名，拒绝网址里嵌网址
    strict_egress: bool = False
    allowed_hosts: set[str] = field(default_factory=set)
    # merge_tables（D20 B1，带 extract 的请求才开）：PDF 表格行并入正文，find / 读取 / 引文核对看同一份文本
    merge_tables: bool = False
    # 视觉识读会整页替换正文：替换前模型读到的正文留在这里，引文核对也认（D20）
    replaced_texts: dict[str, list[str]] = field(default_factory=dict)
    # 读取类工具（fetch_page / render_page / read_document / read_section）一次给研究模型多少字：研究循环每轮按提示里还剩的空间改，整页放得下就一次给全
    read_chars: int = 16_000
    # 一轮放不下的文档滚动读（core/digest.py）：研究模型的上下文长度（请求的 context_window；没给就不滚动读，照旧给开头并写明从哪继续）、
    # 总结时给模型的研究目标（任务书 + 问题）、总结用掉的输出 token（不算进研究的 token 预算）、读过的文档的总结（artifact_id → 结果）
    context_window: int | None = None
    digest_goal: str = ""
    digest_completion: int = 0
    digests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def replace_doc(self, artifact_id: str, doc: ParsedDocument) -> None:
        if (old := self.docs.get(artifact_id)) is not None and old.text != doc.text:
            self.replaced_texts.setdefault(artifact_id, []).append(old.text)
        self.docs[artifact_id] = doc

    # ---------- 出站 ----------
    def allow_urls(self, urls: Iterable[str]) -> None:
        for u in urls:
            if h := _host(str(u)):
                self.allowed_hosts.add(h)

    def check_egress(self, url: str) -> None:
        """strict_egress 下每次按网址读取前调用；不合规就记进 access_failures 并抛错（研究模型收到的是工具错误）。"""
        if not self.strict_egress:
            return
        if nested_url(url):
            e = ToolError(ErrorCode.INVALID_ARGUMENT, "网址的路径或查询串里嵌着另一个网址，不允许经第三方代读", stage="egress")
        elif _host(url) not in self.allowed_hosts:
            e = ToolError(ErrorCode.INVALID_ARGUMENT, f"域名 {_host(url) or '(空)'} 不在本次允许范围（只允许 seed_urls 与搜索结果里的域名）", stage="egress")
        else:
            return
        self.record_failure(url, e)
        raise e

    # ---------- 来源 ----------
    def register_source(self, *, url: str, final_url: str | None, fetched_at: str, artifact_id: str | None, content_hash: str | None,
                        content_type: str | None, title: str | None = None, published_at: str | None = None) -> Source:
        key = artifact_id or url
        if key in self.source_by_key:
            return self.sources[self.source_by_key[key]]
        sid = f"s{len(self.sources) + 1}"
        src = Source(source_id=sid, url=url, final_url=final_url, title=title, fetched_at=fetched_at, published_at=published_at,
                     artifact_id=artifact_id, content_hash=content_hash, content_type=content_type)
        self.sources[sid] = src
        self.source_by_key[key] = sid
        if url not in self.source_by_key:
            self.source_by_key[url] = sid
        return src

    def source_for_artifact(self, artifact_id: str) -> Source | None:
        sid = self.source_by_key.get(artifact_id)
        return self.sources.get(sid) if sid else None

    def add_evidence(self, source_id: str, artifact_id: str | None, quote: str, locator: Locator, note: str | None = None) -> Evidence:
        """同一来源的同一段引文只记一次；重复抽取返回已有条目（收敛判据依赖"新增证据数"）。"""
        key = (source_id, re.sub(r"\s+", " ", quote.strip().lower())[:400])
        if (existing := self._evidence_keys.get(key)) is not None:
            return existing
        ev = Evidence(evidence_id=f"e{len(self.evidence) + 1}", source_id=source_id, artifact_id=artifact_id, quote=quote[:1500], locator=locator, note=note)
        self.evidence.append(ev)
        self._evidence_keys[key] = ev
        return ev

    def record_failure(self, url: str, e: ToolError) -> None:
        challenge = e.info.message.split(":", 1)[1].strip() if e.code == ErrorCode.NEEDS_HUMAN_VERIFICATION and ":" in e.info.message else None
        self.failures.append(AccessFailure(url=url[:2000], error=e.info, challenge_type=challenge))

    # ---------- 读取 ----------
    async def open_url(self, url: str, *, prefer_browser: bool = False, render_opts: dict[str, Any] | None = None, pages: list[int] | None = None, extract_tables: bool = True, vision_pages: list[int] | None = None) -> tuple[Source, ParsedDocument, str]:
        """抓取 + 存证 + 解析 + 登记来源。返回 (Source, ParsedDocument, 原始 artifact_id)。
        普通抓取 → 挑战/失败或正文过短时改浏览器渲染（HTML）。"""
        rt = self.rt
        self.check_egress(url)
        render_opts = render_opts or {}
        rkey = (url, tuple(sorted(render_opts.items())))
        if prefer_browser and (aid := self.rendered.get(rkey)) in self.docs:
            src = self.source_for_artifact(aid)
            assert src is not None and aid
            return src, self.docs[aid], aid
        existing = self.source_by_key.get(url)
        if not prefer_browser and existing and self.sources[existing].artifact_id in self.docs and not vision_pages:
            aid = self.sources[existing].artifact_id
            assert aid
            return self.sources[existing], self.docs[aid], aid
        content: bytes | None = None
        ctype = ""
        final_url = url
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        render_text: str | None = None
        render_title = None
        err: ToolError | None = None
        if not prefer_browser:
            try:
                with rt.timer.track("download"):
                    r = await rt.fetcher.fetch(url, max_bytes=rt.settings.max_document_bytes)
                if r.redirect_to:
                    if not nested_url(r.redirect_to):
                        self.allow_urls([r.redirect_to])     # 允许的网址重定向过去的域名也允许（下载链接常跳到 CDN）
                    raise ToolError(ErrorCode.FETCH_FAILED, f"跨主机重定向到 {r.redirect_to}，需要显式重新读取该 URL", stage="fetch")
                content, ctype, final_url, fetched_at = r.content, r.content_type, r.final_url, r.fetched_at
            except ToolError as e:
                err = e
        need_render = prefer_browser or (err is not None and err.code in (ErrorCode.FETCH_FAILED, ErrorCode.RATE_LIMITED) and "重定向" not in err.info.message)
        if need_render:
            try:
                render_text, render_title, content, ctype, final_url, fetched_at = await self._render(url, **render_opts)
                err = None
            except ToolError as e:
                err = err or e
        if content is None:
            assert err is not None
            self.record_failure(url, err)
            raise err
        assert content is not None
        art = rt.evidence.put("page" if "html" in ctype.lower() else "document", content, url=url, final_url=final_url, content_type=ctype, fetched_at=fetched_at,
                              ext=_ext_for(ctype, content))
        with rt.timer.track("parse"):
            doc, sniffed = await parse_document(content, content_type=ctype, url=final_url, model=rt.model, pages=pages, extract_tables=extract_tables,
                                                max_vision_pages=self.max_vision_pages, vision_pages=vision_pages, merge_tables=self.merge_tables)
        del content
        if render_text is None and sniffed.kind == "html" and len(doc.text.strip()) < 200:
            try:  # 静态抓取正文过短：可能是 JS 页面，改浏览器渲染
                render_text, render_title, _, _, final_url, fetched_at = await self._render(url)
            except ToolError:
                render_text = None
        if render_text and (len(doc.text.strip()) < 200 or doc.method == "strip-tags"):
            from owa_research.core.document import Section

            doc = ParsedDocument(kind="html", title=render_title or doc.title, text=render_text, sections=[Section(id="s0", title="(渲染文本)", level=0, char_start=0, char_end=len(render_text))],
                                 tables=doc.tables, method="browser-innerText")
        if doc.method == "vision":
            self.ocr_pages += int(doc.meta.get("vision_pages", len(doc.pages)))
        parsed = rt.evidence.put_json("parsed", doc.to_json(), parent=art.artifact_id, meta={"kind": doc.kind, "method": doc.method})
        self.docs[art.artifact_id] = doc
        self.parsed_ids[art.artifact_id] = parsed.artifact_id
        src = self.register_source(url=url, final_url=final_url, fetched_at=fetched_at, artifact_id=art.artifact_id, content_hash=art.content_hash,
                                   content_type=sniffed.mime, title=doc.title or render_title, published_at=doc.published_at)
        if sniffed.kind in ("pdf", "docx", "xlsx", "pptx", "image"):
            self.documents_read += 1
        self.pages_read += 1
        if prefer_browser:
            self.rendered[rkey] = art.artifact_id
        return src, doc, art.artifact_id

    async def _render(self, url: str, **opts: Any) -> tuple[str, str | None, bytes, str, str, str]:
        with self.rt.timer.track("render"):
            rr = await self.rt.browser.render(url, max_chars=self.rt.settings.max_page_chars, **opts)
        content = rr.html_head.encode() if rr.html_head else rr.text.encode()
        return rr.text, rr.title, content, "text/html; charset=utf-8", rr.final_url, rr.fetched_at

    async def ensure_vision(self, artifact_id: str, pages: list[int]) -> list[int]:
        """对文字层缺失/残缺的页（scanned_pages）按需做视觉读取并更新解析结果；返回实际读取的页。受 max_vision_pages 约束。"""
        doc = self.doc_for(artifact_id)
        if doc.kind != "pdf":
            return []
        scanned = set(doc.meta.get("scanned_pages", []))
        done = set(doc.meta.get("vision_done", []))
        todo = [p for p in pages if p in scanned and p not in done][: max(0, self.max_vision_pages - len(done))]
        if not todo:
            return []
        from owa_research.readers.document import parse_document as _pd

        art = self.rt.evidence.get(artifact_id)
        src = self.source_for_artifact(artifact_id)
        with self.rt.timer.track("ocr"):
            new_doc, _ = await _pd(self.rt.evidence.read_bytes(artifact_id), content_type="application/pdf", url=(src.url if src else ""), model=self.rt.model,
                                   max_vision_pages=self.max_vision_pages, vision_pages=sorted(done | set(todo)), merge_tables=self.merge_tables)
        new_doc.meta["vision_done"] = sorted(done | set(todo))
        self.replace_doc(artifact_id, new_doc)
        self.ocr_pages += len(todo)
        if art is not None:
            self.rt.evidence.put_json("parsed", new_doc.to_json(), parent=artifact_id, meta={"kind": new_doc.kind, "method": new_doc.method})
        return todo

    def doc_for(self, artifact_id: str) -> ParsedDocument:
        doc = self.docs.get(artifact_id)
        if doc is None:
            # 可能是上次任务留下的 artifact（evidence_request 二次补取）：从证据存储加载
            art = self.rt.evidence.get(artifact_id)
            if art is None:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"未知 artifact_id: {artifact_id}", stage="evidence")
            parsed = next((c for c in self.rt.evidence.children(artifact_id, "parsed")), None)
            if parsed is None:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"artifact {artifact_id} 没有解析结果", stage="evidence")
            doc = ParsedDocument.from_json(self.rt.evidence.read_json(parsed.artifact_id))
            self.docs[artifact_id] = doc
            if not self.source_for_artifact(artifact_id):
                self.register_source(url=art.url or "", final_url=art.final_url, fetched_at=art.fetched_at, artifact_id=artifact_id,
                                     content_hash=art.content_hash, content_type=art.content_type, title=doc.title, published_at=doc.published_at)
        return doc

    def usage_extra(self) -> dict[str, Any]:
        return {"pages_parsed": self.pages_read, "ocr_pages": self.ocr_pages, "documents": self.documents_read}


def _ext_for(ctype: str, content: bytes) -> str:
    c = ctype.lower()
    if content[:4] == b"%PDF" or "pdf" in c:
        return "pdf"
    if "html" in c:
        return "html"
    if content[:2] == b"PK":
        return "zip"
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    if content[:4] == b"\x89PNG":
        return "png"
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    return "bin"
