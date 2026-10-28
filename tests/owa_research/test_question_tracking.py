"""D20 A 层：逐题状态由代码推断（归属、轮内核验、放下、门禁、确认不在）。样本是事故里的摊位表（表格行并入正文）。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from owa_research.core.config import Settings, load_settings
from owa_research.core.loop import STAGNANT_LIMIT, ResearchLoop, step_schema
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted, register_tools
from owa_research.readers import pdf

BOOTHS = Path(__file__).parents[1] / "fixtures" / "real" / "booths.pdf"
QS = ["Claude Builder Club @ UCI 的摊位号是多少？", "IEEE（Institute of Electrical and Electronics Engineers at UCI）的摊位号是多少？",
      "ICS Student Council 的摊位号是多少？"]
ICS_ROW = "[表1 p.2]  |  | ICS Student Council | F362"
MERGED = pdf.parse_pdf(BOOTHS.read_bytes(), merge_tables=True) if BOOTHS.exists() else None   # 没有样本时（样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布）用到它的测试跳过
PLAIN = pdf.parse_pdf(BOOTHS.read_bytes()) if BOOTHS.exists() else None


def _booths() -> bytes:
    if not BOOTHS.exists():
        pytest.skip("样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布")
    return BOOTHS.read_bytes()


@pytest.fixture
async def loop(tmp_path):
    _booths()                       # 没有样本就跳过
    rt = Runtime.build(Settings(), None, tmp_path)
    s = ResearchSession(rt, merge_tables=True)
    s.docs["docu_1"] = MERGED
    s.register_source(url="https://fixtures.invalid/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
    budget = Budgeted(max_searches=4, max_pages=5, max_documents=2)
    register_tools(s, Reranker(backend=None, model=None), budget, goal="q")
    lp = ResearchLoop(ResearchRequest(task="按社团名查摊位号", extract=QS, merge_tables=True), s, Reranker(backend=None, model=None), budget,
                      max_model_tokens=99_999, deadline=rt.started_at + 9999, subquery_default=1, read_top_n=1, planning_thinking="event", question_tracking=True)
    yield lp
    await rt.aclose()


def _find(q, cur, answers=(None, None, None), status=("open",) * 3, action="tool"):
    return {"reason": "找", "current": cur, "questions": [{"status": s, "answer": a} for s, a in zip(status, answers)], "report_append": "",
            "action": action, **({"tool": "browse_document", "args": {"artifact_id": "docu_1", "action": "find", "query": q}} if action == "tool" else {})}


def _script(lp, steps):
    rounds = []

    async def chat_json(messages, **kw):
        obj = steps.pop(0)
        rounds.append({"thinking": kw.get("thinking"), "prompt": messages[-1]["content"]})
        return obj, SimpleNamespace(seconds=0.1, usage={}, text=json.dumps(obj))

    lp.rt.model.chat_json = chat_json
    return rounds


def test_schema_puts_state_first_and_requires_it():
    sc = step_schema(True, True, False, questions=3)
    assert list(sc["properties"])[:4] == ["reason", "current", "questions", "report_append"]
    assert sc["required"][:3] == ["reason", "current", "questions"] and sc["properties"]["questions"]["minItems"] == 3
    assert "reason" == list(step_schema(True)["properties"])[-1] and "questions" not in step_schema(True)["properties"]
    assert load_settings({"OWA_PLANNING_THINKING": "event"}).planning_thinking == "event"


def test_key_terms_drop_shared_words(loop):
    assert [q["terms"] for q in loop.state.questions] == [["claude", "builder", "club"], ["ieee", "institute", "electrical", "electronics", "engineers"],
                                                          ["ics", "student", "council"]]


async def test_parking_goes_by_args_and_names_next_question(loop):        # (a)(h)
    rounds = _script(loop, [_find("Claude", 2), _find("Claude Builder", 2), _find("E257", 2, action="finish"), _find("x", 1, action="finish"),
                            _find("x", 1, action="finish")])
    loop.state.last_result = "(seed)"
    await loop.run_rounds()
    q1, q2 = loop.state.questions[:2]
    assert q1["tried"] == ["Claude", "Claude Builder"] and q1["queries"] == 2 and q2["queries"] == 0        # current 写 2 也算在第 1 题
    assert "第 1 题先放下" in rounds[2]["prompt"] and "转去第 2 题" in rounds[2]["prompt"]
    assert rounds[2]["thinking"] is True and rounds[1]["thinking"] is False                              # event：放下后的下一轮开思维链


async def test_answered_needs_a_source_line(loop):                         # (b)(c)(d)(e)
    rec = {}
    loop._last_seen, loop._last_tools = "Institute of Electrical and Electronics\nEngineers at UCI - IEEE\nF342\nF349", ["browse_document"]
    loop._apply_questions({"questions": [{"status": "open", "answer": None}, {"status": "answered", "answer": "F349"},
                                         {"status": "answered", "answer": "E257"}]}, rec)
    assert [q["status"] for q in loop.state.questions] == ["open", "open", "open"] and rec["answered_rejected"] == [2, 3]
    loop._last_seen, loop._last_tools = "foo\n" + ICS_ROW, ["search_web"]
    loop._apply_questions({"questions": [{"status": "open", "answer": None}] * 2 + [{"status": "answered", "answer": "F362"}]}, {})
    assert loop.state.questions[2]["status"] == "open"                     # 上一步只是搜索：不核验
    loop._last_tools = ["browse_document"]
    loop._apply_questions({"questions": [{"status": "open", "answer": None}] * 2 + [{"status": "answered", "answer": "F362"}]}, rec)
    assert loop.state.questions[2]["status"] == "answered" and loop.state.questions[2]["support"] == ICS_ROW
    loop._apply_questions({"questions": [{"status": "open", "answer": None}] * 3}, {})
    assert loop.state.questions[2]["status"] == "answered"                 # 报 open 不会把已答打回


def test_attribution_prefers_args_and_records_mismatch(loop):            # (g)
    rec = {}
    assert loop._attribute("browse_document", {"query": "IEEE"}, 1, rec) == 1 and rec["current_mismatch"]
    assert loop._attribute("browse_document", {"query": "booth list"}, 3, rec) == 2 and rec["attribution"][-1]["by"] == "current"


async def test_repeat_blocked_rounds_do_not_count_as_stagnant(loop):     # (f)
    _script(loop, [_find("Claude", 1)] * 3 + [_find("x", 1, action="finish")] * 3)
    loop.state.last_result = "(seed)"
    stag = []
    orig = loop._check_converged
    loop._check_converged = lambda: stag.append(loop.state.stagnant) or orig()
    await loop.run_rounds()
    assert stag[1:3] == [stag[0], stag[0]] and loop.state.questions[0]["rounds_spent"] == 3


async def test_finish_gate_fires_once_for_unqueried_question(loop):
    rounds = _script(loop, [_find("ICS Student Council", 3), _find("x", 3, action="finish"), _find("x", 3, action="finish"),
                            _find("x", 3, action="finish"), _find("x", 3, action="finish")])
    loop.state.last_result = "(seed)"
    await loop.run_rounds()
    gated = [r for r in rounds if "尚未查过" in r["prompt"]]
    assert len(gated) == 1 and "第 1 题尚未查过" in gated[0]["prompt"] and "第 2 题尚未查过" in gated[0]["prompt"]
    assert any("候选行" in r["prompt"] and "E257" in r["prompt"] for r in rounds)     # 扫描重开：IEEE 行在已读文本里
    assert len(rounds) == 4


async def test_converged_exit_is_gated_too(loop):
    loop.state.rounds, loop.state.stagnant = 10, STAGNANT_LIMIT
    rec = {}
    assert loop._gate("converged", rec) and rec["gate"] == "converged" and not loop.state.converged
    for q in loop.state.questions:
        q["queries"] = 1
    loop.state.questions[1].update(status="answered")
    loop.state.questions[2].update(status="answered")
    assert loop._gate("converged", {}) is False                            # Claude 题扫描零命中：不再打回


async def test_absent_needs_coverage_tries_and_empty_scan(loop):
    q = loop.state.questions
    q[0]["tried"] = ["Claude"]
    q[1]["tried"] = ["IEEE booth"]
    loop._final_states()
    assert q[0]["status"] == "absent" and q[1]["status"] == "not_found"     # IEEE 行扫描得到：不能说不在
    answers, missing = loop.build_answers({"answers": [{"question": QS[0], "answer": "A12", "quote": None, "source_id": None, "uncertain": False}] + [{}] * 2})
    assert answers[0].answer is None and missing[0].startswith("确认不在已读来源中") and "换过 1 种查法：Claude" in missing[0]
    assert missing[1].startswith("预算内未找到")
    loop.s.docs["docu_1"] = pdf.parse_pdf(_booths(), pages=[1, 2], merge_tables=True)   # 没读全
    q[0]["status"] = "open"
    loop._final_states()
    assert q[0]["status"] == "not_found"


async def test_chinese_only_question_is_never_absent(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        lp = ResearchLoop(ResearchRequest(task="t", extract=["2024 年诺贝尔物理学奖得主是谁？", "2024 年诺贝尔化学奖得主是谁？"]), ResearchSession(rt),
                          Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=1000,
                          deadline=rt.started_at + 100, subquery_default=1, read_top_n=1, question_tracking=True)
        lp.state.questions[0]["tried"] = ["诺贝尔"]
        lp._final_states()
        assert [q["terms"] for q in lp.state.questions] == [[], []] and lp.state.questions[0]["status"] == "not_found"
    finally:
        await rt.aclose()


async def test_stops_when_every_question_is_answered_or_parked_twice(loop):
    """E2（2026-09-23）：Claude 题放下后模型仍一直查它、被拦的重复又不算停滞，一次跑了 15 轮。所有题已答、或放下后再给过一轮仍没进展就收敛。"""
    q = loop.state.questions
    q[1].update(status="answered")
    q[2].update(status="answered")
    q[0].update(status="parked", unlocked=False)
    loop.state.last_blocked = True
    assert loop._check_converged() is False
    q[0].update(unlocked=True)
    assert loop._check_converged() is True and loop.state.converged


async def test_quote_from_text_replaced_by_vision_still_counts(loop):
    """E4（2026-09-23）：模型对 3 页都要了视觉识读，整页正文被替换，它之前读到的表格行引文因此核对不上。替换前的正文也认。"""
    from owa_research.core.document import ParsedDocument

    old_row = "[表2 p.2] Bhagat Puran Singh Health Initiative at UCI | E205 | Institute of Electrical and Electronics Engineers at UCI - IEEE | E257"
    loop.s.replace_doc("docu_1", ParsedDocument(kind="pdf", title=None, text="<!-- page 2 (vision) -->\nsomething else"))
    answers, _ = loop.build_answers({"answers": [{}, {"question": QS[1], "quote": old_row.split("] ", 1)[1], "answer": "E257", "source_id": "s1",
                                                      "kind": "quote", "uncertain": False}, {}]})
    assert answers[1].quote and not answers[1].uncertain


def test_request_field_overrides_deployment_default():
    """逐题跟踪可按单次请求开关（主 agent 经 isolated-web-search 的 track_questions 决定）；不传用部署默认。"""
    assert ResearchRequest(task="t", extract=["q"]).question_tracking is None
    assert ResearchRequest(task="t", extract=["q"], question_tracking=True).question_tracking is True


async def test_unlocated_extract_quotes_are_not_source_text(loop):
    """验收第 1 条：摘句模型编出的句子（原文里找不到、char_start 为空）不能在轮内核过，也不能当引文核过；只写在摘要里同样不行。"""
    from owa_research.core.schema import Locator

    fake = "Institute of Electrical and Electronics Engineers at UCI - IEEE: F349"
    loop.s.add_evidence("s1", "docu_1", fake, Locator(exact_quote=fake, char_start=None))
    loop._last_seen, loop._last_tools = "", ["extract_from"]
    rec = {}
    loop._apply_questions({"questions": [{"status": "open", "answer": None}, {"status": "answered", "answer": "F349"}, {"status": "open", "answer": None}]}, rec)
    assert loop.state.questions[1]["status"] == "open" and rec["answered_rejected"] == [2]
    answers, _ = loop.build_answers({"answers": [{}, {"question": QS[1], "quote": fake, "answer": "F349", "source_id": "s1", "kind": "quote", "uncertain": False}, {}]})
    assert answers[1].uncertain and answers[1].quote is None
    results = {"extract_from": {"artifact_id": "docu_1", "summary": fake, "evidence": [fake]},
               "browse_document": {"artifact_id": "docu_1", "text": "ICS Student Council | F362", "extraction": {"summary": fake}}}

    async def fake_call(tool, args):
        return results[tool]
    loop.rt.dispatcher.call = fake_call
    loop._round_seen = []
    await loop._run_action("extract_from", {"artifact_id": "docu_1", "goal": "g"}, {}, ("extract_from", "k"), 10_000)
    assert loop._round_seen == []
    await loop._run_action("browse_document", {"artifact_id": "docu_1", "action": "page_down"}, {}, ("browse_document", "k"), 10_000)
    assert "F362" in loop._round_seen[0] and "F349" not in loop._round_seen[0]


async def test_extract_quote_must_match_whole_sentence(loop, monkeypatch):
    """复验 R1：摘句开头 80 字照抄、末格编号改掉，不算在原文里定位到；轮内不判已答，引文也核不过。"""
    _booths()                       # 没有样本就跳过
    import owa_research.core.tools_impl as ti

    row = next(line for line in MERGED.text.splitlines() if line.endswith("IEEE | E257"))
    fake = row[:-4] + "F349"
    assert len(row) > 80 and fake[:80] == row[:80]

    async def goal_extract(model, text, goal, **kw):
        return {"relevant": True, "rational": "", "summary": "", "evidence": [fake, row]}
    monkeypatch.setattr(ti, "goal_extract", goal_extract)
    out = await loop.rt.dispatcher.call("extract_from", {"artifact_id": "docu_1", "goal": "IEEE 摊位号"})
    bad, good = (next(e for e in loop.s.evidence if e.evidence_id == i) for i in out["evidence_ids"])
    assert bad.locator.char_start is None and good.locator.char_start is not None
    assert MERGED.text[good.locator.char_start:good.locator.char_end] == row
    loop._last_seen, loop._last_tools = "", ["extract_from"]
    rec = {}
    loop._apply_questions({"questions": [{"status": "open", "answer": None}, {"status": "answered", "answer": "F349"}, {"status": "open", "answer": None}]}, rec)
    assert loop.state.questions[1]["status"] == "open" and rec["answered_rejected"] == [2]
    answers, _ = loop.build_answers({"answers": [{}, {"question": QS[1], "quote": fake, "answer": "F349", "source_id": "s1", "kind": "quote", "uncertain": False}, {}]})
    assert answers[1].uncertain


async def test_keywordless_question_may_answer_by_inference(tmp_path):
    """验收第 5 条：诺奖化学奖这类题面没有可查词的题，系统没核到时收尾允许按推断作答（标未核实），不再逼成 null。"""
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        qs = ["2024 年诺贝尔物理学奖得主是谁？", "2024 年诺贝尔化学奖得主是谁？"]
        lp = ResearchLoop(ResearchRequest(task="t", extract=qs), ResearchSession(rt), Reranker(backend=None, model=None),
                          Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=1000, deadline=rt.started_at + 100,
                          subquery_default=1, read_top_n=1, question_tracking=True)
        lp._final_states()
        block = lp._final_block()
        assert "otherwise null" not in block and "kind=inference" in block
        answers, missing = lp.build_answers({"answers": [{}, {"question": qs[1], "quote": None, "answer": "David Baker（1/2）、Demis Hassabis 和 John Jumper（各 1/4）",
                                                              "source_id": None, "kind": "inference", "uncertain": True}]})
        assert answers[1].answer and answers[1].uncertain and missing[-1].startswith("推断答案")
    finally:
        await rt.aclose()


async def test_hit_in_unlock_round_is_seen_next_round(tmp_path):
    """验收第 6 条：放下后解锁的那一轮 find 命中了答案行，下一轮模型报的答案要能被核过，不能当轮就判卡住收敛。"""
    _booths()                       # 没有样本就跳过
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt, merge_tables=True)
        s.docs["docu_1"] = MERGED
        s.register_source(url="https://fixtures.invalid/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
        budget = Budgeted(max_searches=1, max_pages=5, max_documents=1)
        register_tools(s, Reranker(backend=None, model=None), budget, goal="q")
        q = "学生会的摊位号是多少？"
        lp = ResearchLoop(ResearchRequest(task="t", extract=[q], merge_tables=True), s, Reranker(backend=None, model=None), budget,
                          max_model_tokens=99_999, deadline=rt.started_at + 9999, subquery_default=1, read_top_n=1, question_tracking=True)
        one = lambda query, status="open", ans=None, action="tool": {  # noqa: E731
            "reason": "找", "current": 1, "questions": [{"status": status, "answer": ans}], "report_append": "", "action": action,
            **({"tool": "browse_document", "args": {"artifact_id": "docu_1", "action": "find", "query": query}} if action == "tool" else {})}
        _script(lp, [one("学生会"), one("学生组织"), one("ICS Student Council"), one("x", "answered", "F362", "finish"), one("x", action="finish")])
        lp.state.last_result = "(seed)"
        await lp.run_rounds()
        assert lp.state.questions[0]["terms"] == [] and lp.state.questions[0]["unlocked"]
        assert lp.state.questions[0]["status"] == "answered" and "F362" in lp.state.questions[0]["support"]
    finally:
        await rt.aclose()
