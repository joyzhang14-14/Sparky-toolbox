"""读页面整页给研究模型：网页转文字不截到 10 万字；读取类工具按这一轮放得下的字数给（放得下就整页，放不下写明从哪继续）；
给了 context_window 时每轮留给工具结果的空间按它算，先读的已知来源整页进第一轮；快搜（search_only）只搜一次。"""
import json
from pathlib import Path

from owa_research.core import research
from owa_research.core.config import Settings
from owa_research.core.document import ParsedDocument
from owa_research.core.loop import OUTPUT_RESERVE_TOKENS, TOOL_RESULT_LIMIT, ResearchLoop, est_tokens
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest, ResearchStatus
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted, register_tools
from owa_research.readers.html import parse_html

PAGE = "".join(f"Rule {i}: the program must handle case number {i}.\n" for i in range(4000))   # 约 19 万字
CW = 262_144


def _loop(rt, s, req):
    return ResearchLoop(req, s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=5, max_documents=5),
                        max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)


def _page_session(rt):
    s = ResearchSession(rt)
    s.docs["docu_1"] = ParsedDocument(kind="html", title="p", text=PAGE)
    s.register_source(url="https://x.example/p", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="text/html")
    register_tools(s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=5, max_documents=5), goal="q")
    return s


def test_html_keeps_long_page():
    html = "<html><body>" + "".join(f"<p>Paragraph {i} " + "word " * 30 + "</p>" for i in range(1500)) + "<h2>Deliverables</h2><p>Submit project1.py</p></body></html>"
    doc = parse_html(html)
    assert len(doc.text) > 200_000 and doc.text.rstrip().endswith("Submit project1.py") and "截断" not in doc.text


async def test_read_tools_give_what_fits(tmp_path: Path):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = _page_session(rt)
        r = await rt.dispatcher.call("read_document", {"artifact_id": "docu_1"})
        assert r["total_chars"] == len(PAGE) and r["char_end"] < len(PAGE) and f"char_start={r['char_end']}" in r["text_note"]   # 缺省 1.6 万字：给开头、写明从哪继续
        s.read_chars = 300_000
        r = await rt.dispatcher.call("read_document", {"artifact_id": "docu_1"})
        assert r["text"] == PAGE and "text_note" not in r
        assert (await rt.dispatcher.call("read_section", {"artifact_id": "docu_1", "char_start": 1000}))["char_end"] == len(PAGE)
        assert (await rt.dispatcher.call("read_section", {"artifact_id": "docu_1", "char_start": 0, "max_chars": 800}))["char_end"] == 800
    finally:
        await rt.aclose()


async def test_round_room_follows_context_window(tmp_path: Path):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        assert _loop(rt, ResearchSession(rt), ResearchRequest(task="q"))._result_room() == TOOL_RESULT_LIMIT   # 没给 context_window：照旧
        big = _loop(rt, ResearchSession(rt), ResearchRequest(task="q", context_window=CW))
        room = big._result_room()
        assert len(PAGE) < room < CW
        big.state.last_result = PAGE                     # 放得下的整页：原样进提示
        prompt = big._round_prompt(0.1)
        assert PAGE in prompt and est_tokens(prompt) <= CW - OUTPUT_RESERVE_TOKENS
        big.state.last_result = "字" * 300_000           # 超出上下文：截断，估算的 token 不超
        prompt = big._round_prompt(0.1)
        assert "截断" in prompt and est_tokens(prompt) <= CW - OUTPUT_RESERVE_TOKENS
    finally:
        await rt.aclose()


async def test_seed_page_read_in_full(tmp_path: Path, monkeypatch):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = _page_session(rt)

        async def opened(url, **kw):
            return s.sources[s.source_by_key["docu_1"]], s.docs["docu_1"], "docu_1"

        monkeypatch.setattr(s, "open_url", opened)
        loop = _loop(rt, s, ResearchRequest(task="q", seed_urls=["https://x.example/p"], seeds_only=True, context_window=CW))
        await loop.open_seed_urls()
        assert "Rule 3999: the program must handle case number 3999." in loop.state.last_result and "截断" not in loop.state.last_result
    finally:
        await rt.aclose()


async def test_quick_search(tmp_path: Path, monkeypatch):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        calls = []

        async def call(name, args):
            calls.append((name, args))
            return {"results": [{"title": f"t{i}", "url": f"https://x.example/{i}", "snippet": "s" * 400, "date": None, "engines": ["e"]} for i in range(40)]}

        monkeypatch.setattr(rt.dispatcher, "call", call)
        r = await research.run_research(ResearchRequest(task="ics h32 project 1", search_only=True), rt)
        rows = json.loads(r.summary)["results"]
        assert r.status == ResearchStatus.COMPLETE and calls == [("search_web", {"query": "ics h32 project 1", "max_results": 20})]
        assert 0 < len(rows) <= 40 and rows[0] == {"title": "t0", "url": "https://x.example/0", "snippet": "s" * 400, "date": None}
    finally:
        await rt.aclose()
