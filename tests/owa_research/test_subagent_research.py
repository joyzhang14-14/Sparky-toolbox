"""D17 研究会话侧：strict_egress 出站规则（域名白名单、网址里嵌网址）与 extract → answers（引文逐字核对）。"""
from types import SimpleNamespace

import pytest

from owa_research.core.config import Settings
from owa_research.core.document import ParsedDocument
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.loop import SUBAGENT_NOTE, ResearchLoop
from owa_research.core.rerank import Reranker
from owa_research.core.research import _unanswered
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest, ResearchResult
from owa_research.core.session import ResearchSession, nested_url
from owa_research.core.tools_impl import Budgeted

PDF = "https://cdn.qr-code-generator.com/account1/58379853_3.pdf?Expires=1790192990&Signature=abc"


def _session(strict=True, seeds=()):
    s = ResearchSession(rt=None, strict_egress=strict)
    s.allow_urls(seeds)
    return s


@pytest.mark.parametrize("url", [
    f"https://r.jina.ai/{PDF}",
    "https://docs.google.com/viewer?url=https://cdn.qr-code-generator.com/a.pdf",
    "https://viewer.example.com/view?src=https%3A%2F%2Fcdn.qr-code-generator.com%2Fa.pdf",
    "https://web.archive.org/web/2020/https://example.com/",
])
def test_nested_urls_are_detected(url):
    assert nested_url(url)


def test_plain_urls_are_not_nested():
    assert not nested_url(PDF) and not nested_url("https://example.com/a/b?q=hello&page=2")


def test_strict_egress_allows_seed_and_search_hosts_only():
    s = _session(seeds=[PDF])
    s.check_egress(PDF)                                                  # seed 的域名
    s.check_egress("https://cdn.qr-code-generator.com/other.pdf")
    with pytest.raises(ToolError) as e:
        s.check_egress("https://example.org/page")                      # 没出现过的域名
    assert e.value.code == ErrorCode.INVALID_ARGUMENT and "不在本次允许范围" in e.value.info.message
    s.allow_urls(["https://www.example.org/x"])                          # 搜索结果登记过（www. 视为同一站）
    s.check_egress("https://example.org/page")
    assert [f.url for f in s.failures] == ["https://example.org/page"]  # 被拒的记进 access_failures


def test_strict_egress_rejects_nested_even_on_allowed_host():
    s = _session(seeds=[PDF])
    s.allow_urls(["https://r.jina.ai/"])
    with pytest.raises(ToolError) as e:
        s.check_egress(f"https://r.jina.ai/{PDF}")
    assert "嵌着另一个网址" in e.value.info.message and len(s.failures) == 1


def test_egress_rules_off_by_default():
    s = _session(strict=False)
    s.check_egress(f"https://r.jina.ai/{PDF}")                           # Pi / bench / 验收的行为不变
    s.check_egress("https://anything.example/")
    assert s.failures == []


async def test_open_url_blocks_before_fetching_and_learns_redirect_host(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    fetched = []

    async def fake_fetch(url, **kw):
        fetched.append(url)
        return SimpleNamespace(redirect_to="https://d1.cloudfront.net/file.pdf", content=b"", content_type="", final_url=url, fetched_at="now")

    try:
        s = ResearchSession(rt, strict_egress=True)
        s.allow_urls([PDF])
        rt.fetcher.fetch = fake_fetch
        with pytest.raises(ToolError):
            await s.open_url(f"https://r.jina.ai/{PDF}")
        assert fetched == []                                            # 拒绝发生在任何网络请求之前
        with pytest.raises(ToolError) as e:
            await s.open_url(PDF)                                        # 跨主机重定向：不自动跟，但目标域名登记为允许
        assert "跨主机重定向" in e.value.info.message and fetched == [PDF]
        s.check_egress("https://d1.cloudfront.net/file.pdf")
    finally:
        await rt.aclose()


def _loop(tmp_path, request, **kw):
    rt = Runtime.build(Settings(), None, tmp_path)
    s = ResearchSession(rt, strict_egress=bool(request.strict_egress))
    art = rt.evidence.put("document", b"%PDF-fake", url=PDF, content_type="application/pdf", ext="pdf")
    s.docs[art.artifact_id] = ParsedDocument(kind="pdf", title="booths", text="Club booth list\nClaude Builder Club @ UCI   Booth 42\nAI@UCI   Booth 17\n")
    s.register_source(url=PDF, final_url=None, fetched_at="now", artifact_id=art.artifact_id, content_hash=art.content_hash, content_type="application/pdf")
    loop = ResearchLoop(request, s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                        max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1, **kw)
    return rt, loop


async def test_answers_verify_quotes_against_source_text(tmp_path):
    qs = ["Claude Builder Club 的摊位号？", "AI@UCI 的摊位号？", "IEEE 的摊位号？", "ICS 学生会的摊位号？"]
    rt, loop = _loop(tmp_path, ResearchRequest(task="找摊位号", extract=qs, strict_egress=True))
    try:
        obj = {"answers": [
            {"question": qs[0], "answer": "42", "quote": "Claude Builder Club @ UCI Booth 42", "source_id": "s1", "uncertain": False},   # 空白不同也算
            {"question": qs[1], "answer": "17", "quote": "AI@UCI has booth number 17", "source_id": "s1", "uncertain": False},       # 转述：核对不上
            {"question": qs[2], "answer": None, "quote": None, "source_id": None, "uncertain": False},
        ]}                                                                                                                               # 第 4 题模型漏答
        answers, missing = loop.build_answers(obj)
        assert [a.question for a in answers] == qs
        assert answers[0].answer == "42" and answers[0].quote and answers[0].source_id == "s1" and not answers[0].uncertain
        # 转述核对不上；题面没有指标原文用词（只有 AI、UCI 这类缩写），代码不补引文（D27），维持未核实
        assert answers[1].answer == "17" and answers[1].quote is None and answers[1].uncertain
        assert answers[2].answer is None and answers[3].answer is None
        assert any("核对" in m and qs[1] in m for m in missing) and sum(m.startswith("预算内未找到") for m in missing) == 2
        ResearchResult.model_validate({"status": "complete", "summary": "s", "sources": [loop.s.sources["s1"].model_dump()],
                                       "answers": [a.model_dump() for a in answers], "usage": {"wall_seconds": 1}})
    finally:
        await rt.aclose()


async def test_subagent_mode_prompt_and_required_outputs(tmp_path):
    rt, loop = _loop(tmp_path, ResearchRequest(task="t", extract=["问题一"], strict_egress=True))
    try:
        assert loop.system.endswith(SUBAGENT_NOTE) and "answer: 问题一" in loop.required
    finally:
        await rt.aclose()
    rt, plain = _loop(tmp_path / "b", ResearchRequest(task="t"))
    try:
        assert SUBAGENT_NOTE not in plain.system and plain.required == []
    finally:
        await rt.aclose()


async def test_system_note_and_tools_from_request(tmp_path):
    """sparky-toolbox 的 isolated-web-search 子代理传 system_note（接在系统提示后面，空串 = 不加）和 tools（提示里只列这些，
    别的工具调用 TOOL_NOT_ALLOWED）。"""
    from owa_research.core.errors import ErrorCode, ToolError
    from owa_research.core.loop import SYSTEM_PROMPT

    req = ResearchRequest(task="t", extract=["q"], strict_egress=True, system_note="Only answer the task.", tools=["fetch_page", "read_document"])
    rt, loop = _loop(tmp_path, req)
    try:
        assert loop.system == SYSTEM_PROMPT + " Only answer the task."
        tools_part = loop._round_prompt(0.0).split("## Tools")[1]
        assert "- fetch_page:" in tools_part and "- search_web:" not in tools_part
        rt.dispatcher.restrict(req.tools)
        with pytest.raises(ToolError) as e:
            await rt.dispatcher.call("search_web", {"query": "x"})
        assert e.value.info.code == ErrorCode.TOOL_NOT_ALLOWED and "不在允许列表" in e.value.info.message
    finally:
        await rt.aclose()
    rt, loop = _loop(tmp_path / "b", ResearchRequest(task="t", extract=["q"], strict_egress=True, system_note=""))
    try:
        assert loop.system == SYSTEM_PROMPT
    finally:
        await rt.aclose()
    with pytest.raises(ValueError, match="未知工具"):
        ResearchRequest(task="t", tools=["bash"])


def test_unanswered_fills_every_question_and_schema_rules():
    r = _unanswered(ResearchRequest(task="t", extract=["a", "b"]))
    assert [a.question for a in r["answers"]] == ["a", "b"] and all(a.answer is None for a in r["answers"]) and len(r["missing"]) == 2
    with pytest.raises(ValueError):
        ResearchResult.model_validate({"status": "complete", "summary": "s", "answers": [{"question": "q", "answer": "x", "source_id": "s9"}],
                                       "usage": {"wall_seconds": 1}})
    with pytest.raises(ValueError, match="answer 为 null"):
        ResearchResult.model_validate({"status": "complete", "summary": "s", "answers": [{"question": "q", "answer": None, "quote": "x"}],
                                       "usage": {"wall_seconds": 1}})
    with pytest.raises(ValueError):
        ResearchRequest(task="t", extract=["x"] * 21)


async def test_worker_parse_reads_local_files_without_model(tmp_path):
    """source=file 的解析入口：只读本地文件、不调模型；坏文件单独报错，不影响同批其它文件。"""
    from owa_research.worker import PARSE_MAX_BYTES, _parse_file

    (tmp_path / "a.txt").write_text("Booth 42：Claude Builder Club")
    (tmp_path / "big.txt").write_bytes(b"x" * (PARSE_MAX_BYTES + 1))
    ok = await _parse_file(tmp_path / "a.txt")
    assert ok["kind"] == "text" and "Booth 42" in ok["text"] and not ok["truncated"]
    assert "超过" in (await _parse_file(tmp_path / "big.txt"))["error"]["message"]
    assert "error" in await _parse_file(tmp_path / "missing.pdf")


async def test_worker_parse_appends_tables_as_rows(tmp_path):
    """表格按行附在文字后面：PDF 文字层里名字与编号分列排，只看文字层对不上号（2026-09-22 摊位表演练）。"""
    from openpyxl import Workbook

    from owa_research.worker import _parse_file

    wb = Workbook()
    ws = wb.active
    ws.append(["社团", "摊位"])
    ws.append(["ICS Student Council", "F362"])
    ws.append(["IEEE at UCI", "E257"])
    wb.save(tmp_path / "booths.xlsx")
    r = await _parse_file(tmp_path / "booths.xlsx")
    assert r["tables"] >= 1 and "ICS Student Council | F362" in r["text"] and "IEEE at UCI | E257" in r["text"]


def test_quote_must_carry_answer_codes():
    """引文要撑得住答案：答案里 3 位以上的编号 / 数字必须出现在引文里；1–2 位数字（日期）不查，避免换写法误伤。"""
    from owa_research.core.answers import missing_tokens

    assert missing_tokens("F362", "ICS Student Council") == ["f362"]
    assert missing_tokens("F362", "ICS Student Council | F362") == []
    assert missing_tokens("9 月 23 日", "Wednesday, September 23") == []
    assert missing_tokens("$1,234 million", "revenue was 1234 million") == []
    assert missing_tokens("2026 年", "in 2025") == ["2026"]


async def test_answer_with_quote_lacking_the_code_is_uncertain(tmp_path):
    rt, loop = _loop(tmp_path, ResearchRequest(task="t", extract=["AI@UCI 的摊位号？"]))
    try:
        answers, missing = loop.build_answers({"answers": [{"question": "q", "answer": "17", "quote": "AI@UCI", "source_id": "s1", "uncertain": False}]})
        assert answers[0].quote == "AI@UCI" and answers[0].uncertain and "17" in missing[0]     # 引文缺数字；题面没有指标原文用词，代码不补（D27）
        answers, missing = loop.build_answers({"answers": [{"question": "q", "answer": "Booth 042", "quote": "AI@UCI", "source_id": "s1", "uncertain": False}]})
        assert answers[0].uncertain and answers[0].quote == "AI@UCI" and "042" in missing[0]
    finally:
        await rt.aclose()


async def test_question_tracking_only_with_extract_and_switch(tmp_path):
    rt, plain = _loop(tmp_path, ResearchRequest(task="t"))
    try:
        assert not plain.track and "questions" not in plain._step_schema["properties"] and "Question status" not in plain._build_round_prompt(0.1)
    finally:
        await rt.aclose()
    rt = Runtime.build(Settings(), None, tmp_path / "off")
    try:
        s = ResearchSession(rt)
        off = ResearchLoop(ResearchRequest(task="t", extract=["q"]), s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                           max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1, question_tracking=False)
        assert not off.track and "answer: q" in off.required          # 关掉时退回"必须产出"清单（A/B 的对照组）
    finally:
        await rt.aclose()
    from owa_research.core.config import load_settings
    assert load_settings({}).question_tracking is False and load_settings({"OWA_QUESTION_TRACKING": "true"}).question_tracking is True


async def test_quote_from_low_confidence_table_row_is_uncertain(tmp_path):
    """D20 B3：并入正文的表格行里带 ? 前缀的是低置信行（格内换行多、alignment 表），引文落在这里要标未核实。"""
    rt, loop = _loop(tmp_path, ResearchRequest(task="t", extract=["Boxing Club 的摊位？", "AI@UCI 的摊位？"], merge_tables=True))
    try:
        doc = next(iter(loop.s.docs.values()))
        doc.text = "[表4 p.3 ?] Boxing Club Climbing Club | F279 / F280\n[表5 p.3] AI@UCI | F517\n" + doc.text
        doc.meta["tables_merged"] = True
        answers, missing = loop.build_answers({"answers": [
            {"question": "q", "answer": "F279", "quote": "Boxing Club Climbing Club | F279 / F280", "source_id": "s1", "uncertain": False},
            {"question": "q", "answer": "F517", "quote": "AI@UCI | F517", "source_id": "s1", "uncertain": False}]})
        assert answers[0].uncertain and "低置信表格行" in missing[0]
        assert not answers[1].uncertain
    finally:
        await rt.aclose()
