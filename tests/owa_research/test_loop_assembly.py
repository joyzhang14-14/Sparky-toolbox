"""结果组装：模型给的超长字段不能让整次研究崩溃。"""
import asyncio
from pathlib import Path

from owa_research.core.config import Settings
from owa_research.core.document import ParsedDocument, TableBlock
from owa_research.core.loop import ResearchLoop
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted


async def test_long_model_strings_are_clipped(tmp_path: Path):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        art = rt.evidence.put("document", b"%PDF-fake", url="https://x/a.pdf", content_type="application/pdf", ext="pdf")
        s.docs[art.artifact_id] = ParsedDocument(kind="pdf", title="t", text="x", tables=[TableBlock(index=0, rows=[["a", "1"], ["b", "2"]], headers=["a", "1"], page=3)])
        s.register_source(url="https://x/a.pdf", final_url=None, fetched_at="now", artifact_id=art.artifact_id, content_hash=art.content_hash, content_type="application/pdf")
        loop = ResearchLoop(ResearchRequest(task="q"), s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                            max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)
        obj = {"status": "complete", "summary": "s", "claims": [{"text": "c", "kind": "source_statement", "source_ids": ["s1"], "uncertain": False}],
               "tables": [{"artifact_id": art.artifact_id, "table_index": 0, "period": "fiscal year " * 30, "currency": "United States dollars (USD)", "title": "T" * 900}],
               "disagreements": [], "missing_information": []}
        claims, tables, *_ = await loop.build_result_parts(obj)
        assert len(tables) == 1 and len(tables[0].period) == 100 and len(tables[0].currency) == 16 and tables[0].locator.page == 3
        assert claims[0].source_ids == ["s1"]
    finally:
        await rt.aclose()


def test_norm_args_treats_company_spellings_as_same_call():
    from owa_research.core.loop import _norm_args

    a = _norm_args({"company": "Apple Inc.", "action": "concept", "concept": "revenue"})
    b = _norm_args({"action": "concept", "company": "apple", "concept": "Revenue"})
    c = _norm_args({"company": "NVDA", "action": "concept", "concept": "revenue"})
    assert a == b and a != c


async def test_budget_counts_completion_only_and_clamps_prompt(tmp_path):
    """预算只按输出 token 计；单轮提示超上限时截断而不是撑爆上下文。"""
    from owa_research.core.config import Settings
    from owa_research.core.loop import MAX_PROMPT_CHARS, ResearchLoop
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        loop = ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                            Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=10_000,
                            deadline=rt.started_at + 10_000, subquery_default=1, read_top_n=1)
        rt.model.usage.prompt = 5_000_000   # prompt 再大也不该吃掉预算
        rt.model.usage.completion = 2_000
        assert abs(loop._fraction_used() - 0.2) < 0.05
        rt.model.usage.completion = 9_000
        assert loop._fraction_used() >= 0.85

        loop.state.last_result = "x" * (MAX_PROMPT_CHARS + 50_000)
        prompt = loop._round_prompt(0.5)
        assert len(prompt) <= MAX_PROMPT_CHARS and "截断" in prompt
    finally:
        await rt.aclose()


async def test_convergence_and_dead_target(tmp_path):
    """连续无新增证据即收敛收尾；确定性失败的 URL 不再重试。"""
    from owa_research.core.config import Settings
    from owa_research.core.loop import MIN_ROUNDS_BEFORE_CONVERGE, STAGNANT_LIMIT, ResearchLoop, _target_of
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import Locator, ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        loop = ResearchLoop(ResearchRequest(task="q"), s, Reranker(backend=None, model=None),
                            Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=99_999,
                            deadline=rt.started_at + 9999, subquery_default=1, read_top_n=1)
        loop.state.rounds = MIN_ROUNDS_BEFORE_CONVERGE
        for i in range(STAGNANT_LIMIT - 1):
            assert loop._check_converged() is False, i
        assert loop._check_converged() is True and loop.state.converged

        # 弱信号（只是往报告里补数字）算半步停滞：不会假收敛，也不会永不收敛
        loop.state.converged = False
        loop.state.stagnant = 0.0
        loop.state.last_progress = (0, 0, 0, 0)
        for i in range(STAGNANT_LIMIT * 2 - 1):
            loop.state.report += f"\nfact {i}: value {100 + i} <source id=\"s1\">"
            assert loop._check_converged() is False, f"弱信号第 {i} 轮不应收敛"
        loop.state.report += '\nfact final: value 999 <source id="s1">'
        assert loop._check_converged() is True, "弱信号累积到上限仍应收敛"

        # 强信号（新证据）直接清零停滞计数
        loop.state.converged = False
        loop.state.stagnant = 0.0
        s.register_source(url="https://a.com", final_url=None, fetched_at="now", artifact_id="a1", content_hash=None, content_type="text/html")
        s.add_evidence("s1", "a1", "new quote", Locator())
        assert loop._check_converged() is False and loop.state.stagnant == 0.0

        # 同一来源同一引文不重复计入
        before = len(s.evidence)
        s.add_evidence("s1", "a1", "  NEW   Quote ", Locator())
        assert len(s.evidence) == before

        assert _target_of({"url": "https://X.com/a", "max_chars": 5}) == "url:https://x.com/a"
        assert _target_of({"artifact_id": "p1"}) == "artifact_id:p1"
        assert _target_of({"query": "x"}) is None
    finally:
        await rt.aclose()


async def test_incremental_report_append_revise_and_bad_diff(tmp_path):
    """增量模式：服务端拼接报告；订正生效；old 找不到时忽略而不是打断研究。"""
    from owa_research.core.config import Settings
    from owa_research.core.loop import ResearchLoop
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        def make(incremental: bool) -> ResearchLoop:
            return ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                                Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                                deadline=rt.started_at + 999, subquery_default=1, read_top_n=1, incremental_report=incremental)

        loop = make(True)
        loop._apply_report({"report_append": "Revenue was 130,497 million <source id=\"s1\">"})
        loop._apply_report({"report_append": "R&D was 12,914 million <source id=\"s1\">"})
        assert loop.state.report.splitlines() == ['Revenue was 130,497 million <source id="s1">', 'R&D was 12,914 million <source id="s1">']

        loop._apply_report({"report_append": "", "report_revise": [{"old": "130,497", "new": "130,497 (FY2025)"}]})
        assert "130,497 (FY2025)" in loop.state.report and len(loop.state.report.splitlines()) == 2

        before = loop.state.report
        loop._apply_report({"report_append": "", "report_revise": [{"old": "NOT IN REPORT", "new": "x"}]})
        assert loop.state.report == before  # 坏 diff 被忽略

        loop._apply_report({"report": "full rewrite"})  # 仍接受全文
        assert loop.state.report == "full rewrite"

        full = make(False)
        full._apply_report({"report": "v1"})
        full._apply_report({"report": "v2"})
        assert full.state.report == "v2"
    finally:
        await rt.aclose()


def test_report_dedup_is_mechanical_and_lossless(tmp_path):
    """报告去重：逐字重复与被包含的条目合并；不调用模型，数字与引用一个不丢。"""
    import asyncio

    from owa_research.core.config import Settings
    from owa_research.core.loop import ResearchLoop, _numbers, _source_ids
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted

    async def run() -> None:
        rt = Runtime.build(Settings(), None, tmp_path)
        try:
            loop = ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                                Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                                deadline=rt.started_at + 999, subquery_default=1, read_top_n=1)
            calls = []
            loop.rt.model.chat_json = lambda *a, **k: calls.append(1)  # type: ignore[method-assign]
            report = "\n".join([
                'Revenue was 130,497 million USD for FY2025 <source id="s1">',
                'revenue was 130,497 million usd for fy2025 <source id="s1">',          # 逐字重复（大小写不同）
                'R&D was 12,914 million <source id="s2">',
                'R&D was 12,914 million <source id="s2"> and grew 18% year over year',   # 更完整，应替换上一条
                'Saturn has 285 moons per IAU and 293 per NASA <source id="s3">',
            ])
            loop.state.report = report
            loop.state.rounds = 5
            loop._compact_report()

            assert not calls, "去重不应调用模型"
            lines = loop.state.report.splitlines()
            assert len(lines) == 3, lines
            assert _numbers(report) <= _numbers(loop.state.report), "数字不得丢失"
            assert _source_ids(report) == _source_ids(loop.state.report), "来源引用不得丢失"
            assert "grew 18%" in loop.state.report
        finally:
            await rt.aclose()

    asyncio.run(run())


async def test_partial_result_stays_structured(tmp_path):
    """模型在 partial 收尾时不给 claims 时，从报告里带引用的句子机械拆回，保证结果仍可结构化消费。"""
    from owa_research.core.config import Settings
    from owa_research.core.loop import ResearchLoop
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.register_source(url="https://a.com", final_url=None, fetched_at="now", artifact_id="a1", content_hash=None, content_type="text/html")
        loop = ResearchLoop(ResearchRequest(task="q"), s, Reranker(backend=None, model=None),
                            Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                            deadline=rt.started_at + 999, subquery_default=1, read_top_n=1)
        loop.state.report = ('Saturn has 293 confirmed moons as of August 2026 <source id="s1">\n'
                             'short\n'
                             'IAU lists 285 as of March 2026 <source id="s1">\n'
                             'A line with no citation at all, should be skipped\n')
        claims, *_ = await loop.build_result_parts({"status": "partial", "summary": "blob", "claims": [], "tables": [],
                                                    "disagreements": [], "missing_information": []})
        assert len(claims) == 2 and all(c.source_ids == ["s1"] for c in claims)
        assert "<source" not in claims[0].text and "293" in claims[0].text
    finally:
        await rt.aclose()


def test_actions_normalisation_single_and_parallel():
    """并行模式读 actions 数组并截到上限；非并行模式仍读单个 tool/args；两种都容忍缺失 args。"""
    from owa_research.core.loop import MAX_PARALLEL_ACTIONS, _actions_of

    multi = {"actions": [{"tool": "fetch_page", "args": {"url": "https://a"}},
                         {"tool": "search_web", "args": {"query": "x"}},
                         {"tool": "fetch_page", "args": {"url": "https://b"}},
                         {"tool": "fetch_page", "args": {"url": "https://c"}}]}
    assert len(_actions_of(multi, True)) == MAX_PARALLEL_ACTIONS
    assert _actions_of(multi, False) == []                        # 并行关闭时忽略 actions
    single = {"tool": "search_web", "args": {"query": "x"}}
    assert _actions_of(single, False) == [("search_web", {"query": "x"})]
    assert _actions_of(single, True) == [("search_web", {"query": "x"})]   # 并行模式也接受单动作
    assert _actions_of({"tool": "search_web"}, False) == [("search_web", {})]
    assert _actions_of({"action": "finish"}, True) == []



async def test_evidence_extract_switch_hides_tool_and_changes_flow(tmp_path):
    """evidence_extract=False：提示里不列 extract_from、流程说明改为直接写报告；请求字段可覆盖部署默认（None）。"""
    assert ResearchRequest(task="q").evidence_extract is None
    assert ResearchRequest(task="q", evidence_extract=False).evidence_extract is False
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        def make(flag: bool) -> ResearchLoop:
            return ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                                Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                                deadline=rt.started_at + 999, subquery_default=1, read_top_n=1, merged_extract=False, evidence_extract=flag)
        on, off = make(True)._build_round_prompt(0.1), make(False)._build_round_prompt(0.1)
        assert "- extract_from:" in on and "extract_from(artifact_id, goal)" in on
        assert "extract_from" not in off.split("## Tools")[1] and "into report_append citing" in off
    finally:
        await rt.aclose()


async def test_rounds_stop_early_to_reserve_time_for_finalize(tmp_path):
    """剩余时间不够"再跑一轮 + 收尾"时不再开新轮：forced_partial 且 trace 记 final_reserve，不发模型调用。"""
    import json
    import time as _t
    from owa_research.core.loop import FINAL_RESERVE_S

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        loop = ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                            Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                            deadline=_t.monotonic() + FINAL_RESERVE_S + 10, subquery_default=1, read_top_n=1)  # 剩 70 s，上一轮默认 45 s
        await loop.run_rounds()
        assert loop.state.rounds == 0 and loop.state.forced_partial and loop.state.finish_reason == "budget"
        stops = [json.loads(l) for l in (tmp_path / "trace.jsonl").read_text().splitlines() if '"budget_stop"' in l]
        assert stops and stops[-1]["reason"] == "final_reserve"
    finally:
        await rt.aclose()


def test_source_tiers_and_thinking_mode_mapping():
    """域名分级规则与思维链三态映射是纯函数，直接验。"""
    from owa_research.core.config import load_settings as settings_from_env
    from owa_research.core.loop import _thinking_mode
    from owa_research.core.tiers import tier_label, tier_of

    assert tier_of("https://www.sec.gov/Archives/x.htm") == 1 and tier_of("https://www.stat.go.jp/data/") == 1 and tier_of("https://arxiv.org/abs/1") == 1
    assert tier_of("https://en.wikipedia.org/wiki/Saturn") == 2 and tier_of("https://github.com/a/b") == 2
    assert tier_of("https://www.reddit.com/r/x") == 4 and tier_of("https://x.com/u/status/1") == 4 and tier_of("https://medium.com/@u/p") == 4
    assert tier_of("https://example-blog.io/post") == 3 and tier_label("https://example-blog.io/post") == "other"
    assert _thinking_mode(True) == "on" and _thinking_mode(False) == "off" and _thinking_mode("first") == "first" and _thinking_mode("TRUE") == "on"
    st = settings_from_env({"OWA_PLANNING_THINKING": "first", "OWA_SUBQUERY_MODE": "auto", "OWA_SOURCE_TIERS": "1"})
    assert st.planning_thinking == "first" and st.subquery_mode == "auto" and st.source_tiers is True
    assert settings_from_env({"OWA_PLANNING_THINKING": "false"}).planning_thinking == "off"


async def test_first_round_thinking_and_tier_prompt(tmp_path):
    """first 模式：子查询规划与第 1 轮开思维链，第 2 轮起关；source_tiers 开时提示里候选/来源带 tier 标签与说明。"""
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        def make(**kw) -> ResearchLoop:
            return ResearchLoop(ResearchRequest(task="q"), ResearchSession(rt), Reranker(backend=None, model=None),
                                Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=9999,
                                deadline=rt.started_at + 999, subquery_default=1, read_top_n=1, **kw)
        lp = make(planning_thinking="first")
        assert lp._think() is True and lp._think(1) is True and lp._think(2) is False
        assert make(planning_thinking="on")._think(5) is True and make(planning_thinking=False)._think(1) is False
        lp = make(source_tiers=True)
        lp.state.candidates = [{"title": "t", "url": "https://www.reddit.com/r/x", "snippet": "s", "score": 1, "rerank": 0.5}]
        pr = lp._build_round_prompt(0.1)
        assert "tier=low" in pr and "Source tiers:" in pr
        assert "tier=" not in make(source_tiers=False)._build_round_prompt(0.1)
    finally:
        await rt.aclose()


async def test_repeated_find_is_blocked_but_paging_is_not(tmp_path):
    """2026-09-22 摊位表演练：研究模型 5 轮原样重复 browse_document(find "Claude")。
    同一个词在同一份文档里查第二次就拦下并提示换词/转下一个；翻页与 find_next 参数不变、结果会变，照常执行。"""
    import json
    from types import SimpleNamespace

    from owa_research.core.config import Settings
    from owa_research.core.document import ParsedDocument
    from owa_research.core.loop import ResearchLoop
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted, register_tools

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.docs["docu_1"] = ParsedDocument(kind="pdf", title="booths", text=("ICS Student Council | F362\n" * 800))
        s.register_source(url="https://x/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
        budget = Budgeted(max_searches=1, max_pages=5, max_documents=1)
        register_tools(s, Reranker(backend=None, model=None), budget, goal="q")
        find = {"artifact_id": "docu_1", "action": "find", "query": "Claude"}
        steps = [find, find, find, {"artifact_id": "docu_1", "action": "page_down"}, {"artifact_id": "docu_1", "action": "page_down"}, None]

        async def chat_json(messages, **kw):
            args = steps.pop(0)
            obj = {"report_append": "", "action": "finish", "reason": "done"} if args is None else \
                {"report_append": "", "action": "tool", "tool": "browse_document", "args": args, "reason": "找"}
            return obj, SimpleNamespace(seconds=0.1, usage={}, text=json.dumps(obj))

        rt.model.chat_json = chat_json
        loop = ResearchLoop(ResearchRequest(task="q"), s, Reranker(backend=None, model=None), budget, max_model_tokens=99_999,
                            deadline=rt.started_at + 9999, subquery_default=1, read_top_n=1)
        seen = []
        orig = loop._check_converged

        def spy():
            seen.append(loop.state.last_result)
            return orig()

        loop._check_converged = spy
        await loop.run_rounds()
        trace = [json.loads(l) for l in (rt.evidence.root / "trace.jsonl").read_text().splitlines()]
        skipped = [e["round"] for e in trace if e.get("event") == "action_skipped" and e.get("reason") == "repeat"]
        assert skipped == [2, 3]                                   # 第 2、3 次 find 被拦
        assert "查不到" in seen[1] and "未找到" in seen[0]
        assert all("已用相同参数" not in r for r in seen[3:5])     # 两次 page_down 都真执行
        assert "第 2 屏" in seen[3] and "第 3 屏" in seen[4]
    finally:
        await rt.aclose()


async def test_trace_records_raw_output_and_coverage(tmp_path):
    """D20 X1：每轮落原始输出、思维链开关、上一步工具结果开头与哈希、prompt token；收尾落原始 answers 与覆盖范围。"""
    import json
    from types import SimpleNamespace

    from owa_research.core.config import Settings
    from owa_research.core.document import PageSpan, ParsedDocument
    from owa_research.core.loop import ResearchLoop
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import Budgeted, register_tools

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.docs["docu_1"] = ParsedDocument(kind="pdf", title="b", text="ICS Student Council | F362\n", pages=[PageSpan(page=1, char_start=0, char_end=27)],
                                          meta={"total_pages": 3, "scanned_pages": [3]})
        s.register_source(url="https://x/b.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
        budget = Budgeted(max_searches=1, max_pages=5, max_documents=1)
        register_tools(s, Reranker(backend=None, model=None), budget, goal="q")
        steps = [{"report_append": "", "action": "tool", "tool": "browse_document", "args": {"artifact_id": "docu_1", "action": "find", "query": "ICS"}, "reason": "找"},
                 {"report_append": "", "action": "finish", "reason": "done"},
                 {"status": "complete", "summary": "s", "claims": [], "tables": [], "disagreements": [], "missing_information": [],
                  "answers": [{"question": "q", "answer": "F362", "quote": "ICS Student Council | F362", "source_id": "s1", "uncertain": False}]}]

        async def chat_json(messages, **kw):
            obj = steps.pop(0)
            return obj, SimpleNamespace(seconds=0.1, usage={"prompt_tokens": 123}, text=json.dumps(obj))

        rt.model.chat_json = chat_json
        loop = ResearchLoop(ResearchRequest(task="q", extract=["ICS 的摊位？"]), s, Reranker(backend=None, model=None), budget, max_model_tokens=99_999,
                            deadline=rt.started_at + 9999, subquery_default=1, read_top_n=1, planning_thinking="first")
        loop.state.last_result = "种子读取结果"
        await loop.run_rounds()
        await loop.finalize()
        ev = [json.loads(l) for l in (rt.evidence.root / "trace.jsonl").read_text().splitlines()]
        r1 = next(e for e in ev if e["event"] == "round")
        assert json.loads(r1["raw_json"])["tool"] == "browse_document" and r1["thinking"] is True and r1["prompt_tokens"] == 123
        assert r1["tool_result_head"] == "种子读取结果" and len(r1["tool_result_sha1"]) == 12
        r2 = [e for e in ev if e["event"] == "round"][1]
        assert "F362" in r2["tool_result_head"] and r2["thinking"] is False
        cov = next(e for e in ev if e["event"] == "coverage")["docs"][0]
        assert cov["pages_parsed"] == 1 and cov["total_pages"] == 3 and cov["unread_scanned_pages"] == [3] and not cov["complete"]
        assert next(e for e in ev if e["event"] == "final")["answers_raw"][0]["answer"] == "F362"
    finally:
        await rt.aclose()


async def test_subquery_failure_is_traced(tmp_path, monkeypatch):
    import json

    from owa_research.core import research
    from owa_research.core.config import Settings
    from owa_research.core.errors import ErrorCode, ToolError
    from owa_research.core.runtime import Runtime
    from owa_research.core.schema import ResearchRequest

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        async def boom(self):
            raise ToolError(ErrorCode.EXTRACTION_FAILED, "输出在 max_tokens 处被截断")

        async def stop(self):
            raise ToolError(ErrorCode.CANCELLED, "停")

        async def ok(*a, **k):
            return None

        monkeypatch.setattr(research.ResearchLoop, "plan_subqueries", boom)
        monkeypatch.setattr(research.ResearchLoop, "initial_search", stop)
        monkeypatch.setattr(rt.model, "verify", ok)
        await research.run_research(ResearchRequest(task="q"), rt)
        ev = [json.loads(l) for l in (rt.evidence.root / "trace.jsonl").read_text().splitlines()]
        assert any(e["event"] == "subqueries_failed" and e["code"] == "EXTRACTION_FAILED" for e in ev)
    finally:
        await rt.aclose()
