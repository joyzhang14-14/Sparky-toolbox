"""D20 C 层：收尾先抄原文（预填候选行）、词法核对 v2 + 同行配对、kind 三选一、claims 对齐。"""
from pathlib import Path

import pytest

from owa_research.core.answers import unsupported_terms
from owa_research.core.config import Settings
from owa_research.core.loop import ANSWERS_SCHEMA, ResearchLoop
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import Claim, ClaimKind, ResearchRequest
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted
from owa_research.readers import pdf

IEEE_ROW = "[表2 p.2] Bhagat Puran Singh Health Initiative at UCI | E205 | Institute of Electrical and Electronics Engineers at UCI - IEEE | E257"
TEXT_LAYER = "Institute of Electrical and Electronics\nEngineers at UCI - IEEE\nF342\nF349"
QS = ["Claude Builder Club @ UCI 的摊位号是多少？", "IEEE（Institute of Electrical and Electronics Engineers at UCI）的摊位号是多少？",
      "ICS Student Council 的摊位号是多少？", "2024 年诺贝尔物理学奖得主是谁？"]


def _booths() -> bytes:
    """真实样本 booths.pdf（两栏的社团摊位表）；没有这份文件时跳过用到它的测试（样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布）。"""
    f = Path(__file__).parents[1] / "fixtures" / "real" / "booths.pdf"
    if not f.exists():
        pytest.skip("样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布")
    return f.read_bytes()


@pytest.mark.parametrize("answer,quote,question,kind,expect", [
    ("David Baker", "The Nobel Prize in Chemistry 2024", "2024 年诺贝尔化学奖得主是谁？", "quote", "Baker"),
    ("F362", "F362", "ICS Student Council 的摊位号？", "quote", "主体:"),
    ("F362", "[表2 p.2]  |  | ICS Student Council | F362", "ICS Student Council 的摊位号？", "quote", None),
    ("E205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("E257", IEEE_ROW, "IEEE 的摊位号？", "quote", None),
    ("F349", TEXT_LAYER, "IEEE 的摊位号？", "quote", "配对:F349"),
    ("Hopfield 和 Hinton", "…John J. Hopfield and Geoffrey E. Hinton", "", "quote", None),
    ("9 月 23 日", "Wednesday, September 23", "", "quote", None),
    ("总计 59", "…42 … 17…", "", "quote", "59"),
    ("2026 年 9 月 21 日", "会议定在 9/21 19:00", "", "quote", "2026"),
    ("总计 59", "…42 … 17…", "", "inference", None),
    # 验收第 3 条：编号不分大小写、可带空格或连字符、1 位数字、中文紧贴；纯数字只作为编号一部分出现时也配对
    ("205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:205"),
    ("e205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("E 205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("E-205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("摊位号为E205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    # 复验：答案一侧的小写编号（带分隔、紧贴中文、跟在英文词后）也认；普通英文的 "at 7pm" 不当编号
    ("e 205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("e-205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("摊位号为e205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("booth e205", IEEE_ROW, "IEEE 的摊位号？", "quote", "配对:E205"),
    ("e 257", IEEE_ROW, "IEEE 的摊位号？", "quote", None),
    ("at 7pm", "IEEE general meeting at 7pm", "IEEE 几点开会？", "quote", None),
    ("begin at 7PM ET", "IEEE general meeting will begin at 7PM ET", "IEEE 几点开会？", "quote", None),
    # 验收第 4 条（D21）：邮件里常见的日期、钟点、金额写法
    ("19:00", "starts at 7pm.", "", "quote", None),
    ("09/23", "September 23.", "", "quote", None),
    ("10 月 1 日", "by 10/01/2026.", "", "quote", None),
    ("$1,200.00", "$1,200.", "", "quote", None),
    ("Wednesday, September 23", "The session is Wed, Sept 23.", "", "quote", None),
    ("20:00", "starts at 7pm.", "", "quote", "20"),
    # 复验 R6：时间段只在末尾写 p.m.，起点也按下午算
    ("14:00–17:00", "Opening reception: Oct. 10, 2–5 p.m.", "", "quote", None),
    ("10 月 10 日 14:00", "Opening reception: Oct. 10, 2–5 p.m.", "", "quote", None),
    ("11:00", "Lunch talk 11–1 p.m.", "", "quote", None),
    ("15:00", "Opening reception: Oct. 10, 2–5 p.m.", "", "quote", "15"),
])
def test_unsupported_terms(answer, quote, question, kind, expect):
    got = unsupported_terms(answer, quote, question, kind)
    assert (got == []) if expect is None else any(expect in g for g in got), got


def test_answers_schema_is_quote_first():
    props = list(ANSWERS_SCHEMA["items"]["properties"])
    assert props.index("quote") < props.index("answer") and ANSWERS_SCHEMA["items"]["properties"]["kind"]["enum"] == ["quote", "inference", "absent"]


@pytest.fixture
async def loop(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    s = ResearchSession(rt, merge_tables=True)
    s.docs["docu_1"] = pdf.parse_pdf(_booths(), merge_tables=True)
    s.register_source(url="https://fixtures.invalid/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
    lp = ResearchLoop(ResearchRequest(task="t", extract=QS, merge_tables=True), s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                      max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)
    yield lp
    await rt.aclose()


async def test_prefill_picks_rows_with_codes_and_skips_chinese_only(loop):
    pre = loop._prefill()
    assert pre[0] == [] and pre[3] == []                                   # Claude 不在表里；诺奖题没有可查的关键词
    assert pre[1][0] == ("s1", IEEE_ROW) and pre[2][0][1] == "[表1 p.2]  |  | ICS Student Council | F362"
    assert all(len(p) <= 3 for p in pre)


async def test_build_answers_kinds_and_alignment(loop):
    answers, missing = loop.build_answers({"answers": [
        {"question": QS[0], "quote": None, "answer": None, "source_id": None, "kind": "absent", "uncertain": False},
        {"question": QS[1], "quote": "s1: " + IEEE_ROW, "answer": "E257", "source_id": "s1", "kind": "quote", "uncertain": False},   # 抄进了来源标签
        {"question": QS[2], "quote": "ICS Student Council | F362", "answer": "E205", "source_id": "s1", "kind": "quote", "uncertain": False},
        {"question": QS[3], "quote": None, "answer": "Hopfield", "source_id": None, "kind": "quote", "uncertain": False}]})
    assert answers[0].answer is None and missing[0].startswith("预算内未找到")
    assert answers[1].quote == IEEE_ROW and not answers[1].uncertain
    assert answers[2].uncertain and any("E205" in m for m in missing)
    assert answers[3].uncertain and answers[3].quote is None and any(m.startswith("模型未给引文") for m in missing)
    # 推断、或引文核不上：答案含编号时代码回原文找到 IEEE 那一行补上（D26），标已核实；模型的转述不当引文
    answers, missing = loop.build_answers({"answers": [{}, {"question": QS[1], "quote": IEEE_ROW, "answer": "E257", "source_id": "s1", "kind": "inference", "uncertain": False}]})
    assert not answers[1].uncertain and answers[1].quote.endswith("IEEE | E257") and not any(QS[1] in m for m in missing)
    answers, missing = loop.build_answers({"answers": [{}, {"question": QS[1], "quote": "IEEE booth E257", "answer": "E257", "source_id": "s1", "kind": "quote", "uncertain": False}]})
    assert answers[1].quote.endswith("IEEE | E257") and not answers[1].uncertain


def test_claims_follow_verified_answers():
    from owa_research.core.schema import Answer

    claims = [Claim(text="IEEE 的摊位是 E257", kind=ClaimKind.SOURCE_STATEMENT, source_ids=["s1"], uncertain=True),
              Claim(text="ICS 在 F362", kind=ClaimKind.SOURCE_STATEMENT, source_ids=["s1"], uncertain=True)]
    ResearchLoop.align_claims(claims, [Answer(question="q", answer="E257", quote="x", source_id="s1", uncertain=False),
                                       Answer(question="q", answer="F362", quote="x", source_id="s1", uncertain=True)])
    assert [c.uncertain for c in claims] == [False, True]


def test_single_digit_codes_pair_by_cell():
    from owa_research.core.answers import codes, key_terms

    qs = ["GoodSoil at UCI 的摊位号？", "Chabad at UCI 的摊位号？"]
    row = "[表0 p.1] Chabad at UCI | A3 | GoodSoil at UCI | A21"
    assert any("配对:A3" in x for x in unsupported_terms("A3", row, qs[0], "quote", key_terms(qs[0], qs)))
    assert unsupported_terms("A21", row, qs[0], "quote", key_terms(qs[0], qs)) == []
    assert codes("at 7") == [] and codes("Room B2 at 7pm") == ["B2"] and codes("E-205") == ["E205"]


@pytest.mark.parametrize("together", [True, False])
def test_generic_words_do_not_pair(together):
    """验收第 2 条（D21）：题目关键词过半才算命中，已读文档里到处都有的词（uci）不算主体。
    Claude Builder Club 不在表里：单独问和多题同问时，没有一个（编号, 行）能核过；IEEE 单独问时 E205 报配对错误。"""
    from owa_research.core.answers import codes, drop_common, key_terms

    doc = pdf.parse_pdf(_booths(), merge_tables=True)
    qs = QS if together else [QS[0]]
    terms = drop_common(key_terms(QS[0], qs), [doc.text])
    assert "uci" not in terms
    lines = [l for l in doc.text.splitlines() if l.strip()]
    assert [(c, l) for l in lines for c in codes(l) if unsupported_terms(c, l, QS[0], "quote", terms) == []] == []
    ieee = drop_common(key_terms(QS[1], [QS[1]]), [doc.text])
    assert any("配对:E205" in x for x in unsupported_terms("E205", IEEE_ROW, QS[1], "quote", ieee))
    assert unsupported_terms("E257", IEEE_ROW, QS[1], "quote", ieee) == []


def test_subject_found_in_the_whole_mail_block():
    """验收第 4 条：社团名只在 Subject 行时，主体检查看整封邮件段也能通过；只看引文仍然报缺主体。"""
    q = "ICS Student Council 的 info session 是哪天？"
    quote = "The info session is on Wednesday, September 23."
    block = "From: ICS <ics@uci.edu>\nSubject: ICS Student Council info session\n\n" + quote
    assert unsupported_terms("9 月 23 日", quote, q, context=block) == []
    assert any(x.startswith("主体:") for x in unsupported_terms("9 月 23 日", quote, q))


def test_quote_spliced_from_two_lines():
    """复验：答案带编号、引文在原文里跨了两行（两行拼成一句）→ 报跨行；不带编号的邮件硬折行引文照样能过。"""
    chicanos = "[表2 p.2] Chicanos/Latinos for Community Medicine | E206 | Institute of Transportation Engineers | E258"
    source = f"{IEEE_ROW}\n{chicanos}\n"
    spliced = IEEE_ROW + " " + chicanos
    assert "跨行" in unsupported_terms("E257", spliced, QS[1], source=source)
    assert "跨行" not in unsupported_terms("E257", IEEE_ROW, QS[1], source=source)
    mail = "The info session is on Wednesday,\nSeptember 23 at 7pm in DBH 6011.\n"
    assert unsupported_terms("9 月 23 日 19:00", "The info session is on Wednesday, September 23 at 7pm", "", source=mail) == []


CHICANOS_ROW = "[表2 p.2] Chicanos/Latinos for Community Medicine | E206 | Institute of Transportation Engineers | E258"
CHICANOS_Q = "Chicanos/Latinos for Community Medicine 的摊位号是多少？"


def test_bare_number_answer_spliced_from_two_lines():
    """D22 复验第 1 条：纯数字答案（'257'）的引文由 IEEE 行和 Chicanos 行拼成 → 同样报跨行。"""
    source = f"{IEEE_ROW}\n{CHICANOS_ROW}\n"
    assert "跨行" in unsupported_terms("257", IEEE_ROW + " " + CHICANOS_ROW, CHICANOS_Q, source=source)
    assert "跨行" not in unsupported_terms("206", CHICANOS_ROW, CHICANOS_Q, source=source)


async def test_build_answers_flags_bare_number_spliced_quote(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt, merge_tables=True)
        s.docs["docu_1"] = pdf.parse_pdf(_booths(), merge_tables=True)
        s.register_source(url="https://fixtures.invalid/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None,
                          content_type="application/pdf")
        assert f"{IEEE_ROW}\n{CHICANOS_ROW}" in s.docs["docu_1"].text
        lp = ResearchLoop(ResearchRequest(task="t", extract=[CHICANOS_Q], merge_tables=True), s, Reranker(backend=None, model=None),
                          Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=1000, deadline=rt.started_at + 100,
                          subquery_default=1, read_top_n=1)
        answers, missing = lp.build_answers({"answers": [{"question": CHICANOS_Q, "quote": IEEE_ROW + " " + CHICANOS_ROW, "answer": "257",
                                                          "source_id": "s1", "kind": "quote", "uncertain": False}]})
        assert answers[0].uncertain and any("跨行" in m for m in missing), missing
    finally:
        await rt.aclose()


def test_drop_common_keeps_the_rarest_term_when_all_are_common():
    """复验：题目关键词全是高频词（"Acts2fellowship at UCI" 只抽出 uci）时，留下行频最低的词，同行配对不静默关掉。"""
    from owa_research.core.answers import drop_common, key_terms

    doc = pdf.parse_pdf(_booths(), merge_tables=True)
    q = "Acts2fellowship at UCI 的摊位号是多少？"
    terms = drop_common(key_terms(q), [doc.text])
    assert terms == ["uci"]
    row = next(l for l in doc.text.splitlines() if "| A1 |" in l)
    assert any("配对:A1" in x for x in unsupported_terms("A1", row, q, "quote", terms))


def test_spaced_code_in_question_counts_as_subject():
    """复验：题面的编号带空格（AI 2025、CS 161）时，原文同样写法算命中题目主体。"""
    assert unsupported_terms("东京", "AI 2025 will be held in Tokyo.", "AI 2025 大会在哪个城市举办？") == []
    assert unsupported_terms("10 月 20 日", "CS161 midterm: October 20", "CS 161 的期中考试在哪天？") == []
    assert any(x.startswith("主体") for x in unsupported_terms("东京", "The summit will be held in Tokyo.", "AI 2025 大会在哪个城市举办？"))


def test_answer_rules_say_when_to_use_quote():
    """D25：研究收尾提示写明引文直接出现答案（逐字或只差格式）就标 quote，inference 只用于计算或跨行拼合。"""
    from owa_research.core.loop import ANSWER_RULES

    assert "directly contains the answer" in ANSWER_RULES and "differing only in format" in ANSWER_RULES
    assert "inference only when the answer must be computed or combined from more than one line" in ANSWER_RULES
    assert "one form only" in ANSWER_RULES and "no added unit conversion, no rounding" in ANSWER_RULES    # 双写法的答案找不到一行同时含两个数
    assert "the most precise one" in ANSWER_RULES and "25.8" not in ANSWER_RULES                   # 示例不用 bench 的参考答案


# ---------- D26：代码按答案里的数字回原文找引文 ----------
NV_QS = ["NVIDIA FY2025 数据中心（Data Center）收入是多少？", "NVIDIA FY2025 全年总收入（Revenue）是多少？",     # D27：题面写明指标并附原文用词
         "NVIDIA FY2025 全年营业利润（Operating income）是多少？", "NVIDIA FY2025 财年截止日（Year Ended）是哪天？"]
NV_TEXT = "\n".join([   # D25 小实测里 NVIDIA 新闻稿解析文本的几行（tests/fixtures/d25-one-fact-0924-1221/nvidia/parsed/）
    "NVIDIA (NASDAQ: NVDA) today reported revenue for the fourth quarter ended January 26, 2025, of $39.3 billion, up 12% from the previous quarter.",
    "| *($ in millions, except earnings* *per share)* | **FY25** | **FY24** | **Y/Y** | ",
    "| Revenue | $130,497 | $60,922 | Up 114% | ",
    "| Operating income | $81,453 | $32,972 | Up 147% | ",
    "NVIDIA achieved progress since its previous earnings announcement in these areas:", "", "**Data Center** ", "",
    "- Fourth-quarter revenue was a record $35.6 billion, up 16% from the previous quarter. Full-year revenue rose 142% to a record $115.2 billion.",
    "| Revenue |  |  | $ | 39,331 |  |  | $ | 22,103 |  |  | $ | 130,497 |  |  | $ | 60,922 |  | "])


def _cq(answer, i):
    from owa_research.core.answers import code_quotes, key_terms
    return next(code_quotes(answer, NV_QS[i], NV_QS, [("s1", NV_TEXT)], key_terms(NV_QS[i], NV_QS)), None)


def test_code_quote_finds_the_line_with_the_numbers():
    assert _cq("$130,497 million", 1) == ("s1", "| Revenue | $130,497 | $60,922 | Up 114% |")
    assert _cq("$81,453 million", 2)[1].startswith("| Operating income | $81,453")
    assert _cq("January 26, 2025", 3) is None                            # 原文只有 "fourth quarter ended January 26, 2025"，不挨 "year ended"（D27）
    assert _cq("$115.2 billion", 0)[1].endswith("$115.2 billion.")          # 行里没有 data center，靠上方 2 行内的 "**Data Center**" 小节标题


def test_code_quote_gate_and_known_limit():
    assert _cq("130,497", 2) is None                                      # 营业利润题答成总收入：候选行不含 operating / income，不补
    assert _cq("39,331", 1)[1].startswith("| Revenue |  |  | $ | 39,331")  # 已知限制（同 J1）：总收入题答成同一指标的季度数，行标签是 Revenue，拦不住
    assert _cq("Jensen Huang", 1) is None                                 # 答案不含数字或日期：不触发


def test_code_quote_rejects_adversarial_booth_answers():
    """摊位表对抗样本走代码补引文也核不过：IEEE 答 E205（同行另一社团）；Claude Builder Club（不在表里）答表里任何编号。"""
    from owa_research.core.answers import code_quotes, codes, drop_common, key_terms

    doc = pdf.parse_pdf(_booths(), merge_tables=True)
    src = [("s1", doc.text)]
    ieee = drop_common(key_terms(QS[1], QS), [doc.text])
    assert next(code_quotes("E205", QS[1], QS, src, ieee), None) is None
    assert next(code_quotes("E257", QS[1], QS, src, ieee))[1].endswith("IEEE | E257")
    claude = drop_common(key_terms(QS[0], QS), [doc.text])
    assert [c for c in dict.fromkeys(codes(doc.text)) if next(code_quotes(c, QS[0], QS, src, claude), None)] == []


async def test_absent_with_an_answer_is_kept_and_code_quoted(tmp_path):
    """D26 第二项：模型标 kind=absent 却给了答案（D25 小实测 NVIDIA 后 3 题）→ 答案保留，按推断处理，代码补上原文行后标已核实；
    逐题跟踪由系统判定"确认不在"的题维持现状。"""
    from owa_research.core.document import ParsedDocument

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.docs["docu_1"] = ParsedDocument(kind="html", title=None, text=NV_TEXT)
        s.register_source(url="https://nvidianews.nvidia.com/x", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None,
                          content_type="text/html")
        lp = ResearchLoop(ResearchRequest(task="t", extract=NV_QS), s, Reranker(backend=None, model=None),
                          Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=1000, deadline=rt.started_at + 100,
                          subquery_default=1, read_top_n=1)
        answers, missing = lp.build_answers({"answers": [{}, {"question": NV_QS[1], "quote": "", "answer": "$130,497 million", "source_id": None,
                                                              "kind": "absent", "uncertain": False}]})
        assert answers[1].answer == "$130,497 million" and answers[1].quote == "| Revenue | $130,497 | $60,922 | Up 114% |" and not answers[1].uncertain
        assert not any(NV_QS[1] in m for m in missing)
    finally:
        await rt.aclose()


def test_date_year_is_not_borrowed_from_another_column():
    """D26 复验第 1 步：AMD 表头行（Markdown 加粗、年份和逗号粘连）能解析出年份；答案的年份不能借同一行别的日期列。"""
    from owa_research.core.answers import dates

    row = "|  | **December 28,****2024** |  | **December 30,****2023** |"
    assert dates(row) == {(2024, 12, 28), (2023, 12, 30)}
    assert unsupported_terms("December 28, 2023", row) == ["2023"]            # 不补
    assert unsupported_terms("December 28, 2024", row) == []                  # 补得上
    assert unsupported_terms("2026 年 9 月 21 日", "会议定在 9/21，另见 2026 年报") == ["2026"]


def test_code_quote_skips_metadata_lines():
    """D26 复验第 2 步：邮件头（含正文里的转发头、回复引用头）、引用行、附件页脚、worker 插入的表格标题不当补引文的候选。"""
    from owa_research.core.answers import code_quotes

    q = "Floor Meeting 是哪天？"
    fwd = ("From: UCI Housing <h@uci.edu>\nDate: Wed, Sep 23, 2026\nSubject: Fwd: Floor Meeting\n\n---------- Forwarded message ---------\n"
           "From: Floor Meeting Bot <bot@uci.edu>\nDate: Mon, Sep 22, 2026 at 9:15 AM\nSubject: Floor Meeting\n\nSee you there.\n")
    assert next(code_quotes("9/22", q, [q], [("m1", fwd)], []), None) is None
    reply = "Thanks!\n\nOn Mon, Sep 22, 2026 at 9:15 AM Floor Meeting Bot <bot@uci.edu> wrote:\n> Floor Meeting moved to Sep 22.\n"
    assert next(code_quotes("9/22", q, [q], [("m1", reply)], []), None) is None
    page = "Page count of the handbook\nPage 12 of 40\n"
    assert next(code_quotes("40", "Page count 是多少？", [], [("f1", page)], []), None) is None
    tbl = "# 解析出的表格（每行一条，单元格用 | 分隔）\n## 表格 3（第 15 页）\nClub | Booth\n"
    assert next(code_quotes("15", "表格在第几页？", [], [("f1", tbl)], []), None) is None


@pytest.mark.parametrize("text", ["未找到", "N/A", "Not found in the sources", "未披露，只知道截至 2025 年",
                                  # D28：前缀后不要求词边界、词表补全、absent 时任何位置出现都算、年份不算数
                                  "没找到 2025 年的数据", "未找到相关数据（2025）", "未找到2025年全年数据", "无公开数据（截至 2025 年）",
                                  "没有披露 2025 年数据", "暂无 2025 年全年数据", "未提及，只有 2025 年一季度指引", "not available in 2025 filing",
                                  "2025 年的数据未公布", "Revenue for 2025 is not disclosed"])
async def test_absent_placeholder_answer_is_dropped(tmp_path, text):
    """D26 复验第 3 步：模型标 absent、答案是"没找到"一类占位文字（哪怕带年份）→ 照旧丢弃，不当答案。"""
    from owa_research.core.answers import absent_but_answered
    from owa_research.core.document import ParsedDocument

    assert not absent_but_answered(text) and absent_but_answered("$130,497 million")
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.docs["docu_1"] = ParsedDocument(kind="html", title=None, text=NV_TEXT)
        s.register_source(url="https://nvidianews.nvidia.com/x", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None,
                          content_type="text/html")
        lp = ResearchLoop(ResearchRequest(task="t", extract=NV_QS), s, Reranker(backend=None, model=None),
                          Budgeted(max_searches=1, max_pages=1, max_documents=1), max_model_tokens=1000, deadline=rt.started_at + 100,
                          subquery_default=1, read_top_n=1)
        answers, missing = lp.build_answers({"answers": [{}, {"question": NV_QS[1], "quote": "", "answer": text, "source_id": None, "kind": "absent",
                                                              "uncertain": False}]})
        assert answers[1].answer is None and any(m.startswith("预算内未找到") and NV_QS[1] in m for m in missing)
    finally:
        await rt.aclose()


AMD_TEXT = "\n".join([   # D25 小实测里 AMD 新闻稿解析文本的几行（tests/fixtures/d25-one-fact-0924-1221/amd/parsed/）
    "For the full year 2024, AMD reported record revenue of $25.8 billion, gross margin of 49%, operating income of $1.9 billion, net income of $1.6 billion.",
    "| Gross profit ($M) | $3,882 | $2,911 | Up 33% | $3,419 | Up 14% | ",
    "|  | \xa0**2024** | \xa0**2023** | **Y/Y** | \xa0**2024** | \xa0**2023** | **Y/Y** | ",
    "| Revenue ($M) | $25,785 | $22,680 | Up 14% | $25,785 | $22,680 | Up 14% | ",
    "| Operating income ($M) | $1,900 | $401 | Up 374% | $6,138 | $4,854 | Up 26% | "])


def _loop_on(tmp_path, text, qs):
    from owa_research.core.document import ParsedDocument

    rt = Runtime.build(Settings(), None, tmp_path)
    s = ResearchSession(rt)
    s.docs["docu_1"] = ParsedDocument(kind="html", title=None, text=text)
    s.register_source(url="https://example.com/x", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="text/html")
    lp = ResearchLoop(ResearchRequest(task="t", extract=qs), s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                      max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)
    origins = []
    lp.trace.log = lambda ev, **kw: origins.extend(kw.get("quote_origin") or []) if ev == "answers_origin" else None
    return rt, lp, origins


async def test_inference_keeps_its_own_verified_quote(tmp_path):
    """D26 复验第 4 步：kind=inference 但模型抄的原文逐字核得上、也撑得住答案 → 保留它（quote_origin=model），不换成代码找的行。"""
    q = "AMD FY2024 全年总收入（Revenue）同比增长多少？"
    row = "| Revenue ($M) | $25,785 | $22,680 | Up 14% | $25,785 | $22,680 | Up 14% |"
    qs = [q, "AMD FY2024 全年营业利润（Operating income）是多少？"]           # 同问几题时公司名、财年是共有词，不当题目主体
    rt, lp, origins = _loop_on(tmp_path, AMD_TEXT, qs)
    try:
        answers, _ = lp.build_answers({"answers": [{"question": q, "quote": row, "answer": "14%", "source_id": "s1", "kind": "inference", "uncertain": False}, {}]})
        assert answers[0].quote == row and not answers[0].uncertain and origins[0] == "model"
    finally:
        await rt.aclose()


# ---------- D27：题目带指标名，数字紧挨指标（用 D25 小实测存下的真实原文） ----------
D25_RUN = Path(__file__).parents[1] / "fixtures" / "d25-one-fact-0924-1221"
AMD_QS = ["AMD FY2024 数据中心（Data Center）收入是多少？", "AMD FY2024 全年总收入（Revenue）是多少？",
          "AMD FY2024 全年营业利润（Operating income）是多少？", "AMD FY2024 财年截止日（Year Ended）是哪天？"]


def _real(case, answer, q, qs):
    import json

    from owa_research.core.answers import code_quotes, key_terms
    text = json.loads(next((D25_RUN / case / "parsed").glob("pars_*.json")).read_text())["text"]
    got = next(code_quotes(answer, q, qs, [("s1", text)], key_terms(q, qs)), None)
    return got and got[1]


def test_d27_number_must_sit_next_to_the_metric():
    assert _real("nvidia", "$72,880 million", NV_QS[2], NV_QS) is None                           # 净利润当营业利润
    assert _real("nvidia", "$81,453 million", NV_QS[2], NV_QS).startswith("| Operating income | $81,453")
    assert _real("amd", "$7.1 billion", AMD_QS[0], AMD_QS) is None                                 # Client 分部当数据中心
    assert "Data Center segment revenue was a record $12.6 billion" in _real("amd", "$12.6 billion", AMD_QS[0], AMD_QS)
    assert _real("nvidia", "$115.2 billion", NV_QS[1], NV_QS) is None                              # 数据中心收入当总收入
    assert _real("amd", "$1.6 billion", AMD_QS[2], AMD_QS) is None                                 # 净利润当营业利润（同一句里 operating income 后第一个数是 1.9）
    assert _real("amd", "$1.9 billion", AMD_QS[2], AMD_QS).startswith("For the full year 2024")
    assert _real("nvidia", "$130,497 million", "全年总收入是多少？", ["全年总收入是多少？"]) is None      # 题面没有指标短语：不补
    assert _real("amd", "1.9%", AMD_QS[2], AMD_QS) is None                                         # 单位冲突：原文是 $1.9 billion


def test_d27_metric_phrases():
    from owa_research.core.answers import metric_phrases

    assert [metric_phrases(q, AMD_QS) for q in AMD_QS] == [["data center"], ["revenue"], ["operating income"], ["year ended"]]
    assert metric_phrases("NVIDIA FY2025 全年总收入是多少？") == []                                  # 公司名、带数字的 FY2025 都不算
    assert metric_phrases("Total fiscal year end date？") == []                                     # 全是泛词


async def test_model_unsure_stays_unverified_even_with_code_quote(tmp_path):
    """D27 第 7 步：模型自己标 uncertain=True → 代码照样补引文（参考），但仍未核实，missing 写明；kind=absent 却给了答案的不算"不确定"。"""
    from owa_research.core.document import ParsedDocument  # noqa: F401

    rt, lp, origins = _loop_on(tmp_path, NV_TEXT, NV_QS)
    try:
        answers, missing = lp.build_answers({"answers": [{}, {"question": NV_QS[1], "quote": None, "answer": "$130,497 million", "source_id": None,
                                                              "kind": "quote", "uncertain": True}, {}, {}]})
        assert answers[1].quote == "| Revenue | $130,497 | $60,922 | Up 114% |" and answers[1].uncertain and origins[1] == "code"
        assert any(m.startswith("模型自己标了不确定") for m in missing)
        answers, missing = lp.build_answers({"answers": [{}, {"question": NV_QS[1], "quote": None, "answer": "$130,497 million", "source_id": None,
                                                              "kind": "absent", "uncertain": True}, {}, {}]})
        assert answers[1].quote and not answers[1].uncertain
    finally:
        await rt.aclose()


def test_d28_phrases_skip_company_names_and_bind_company():
    """D28 第三步：括号英文才是指标、公司名不当指标；共有的指标短语不删；公司名要出现在文档开头或候选行附近，一家公司的数核不到另一家的题上。"""
    import json

    from owa_research.core.answers import code_quotes, key_terms, metric_phrases

    nv = json.loads(next((D25_RUN / "nvidia" / "parsed").glob("pars_*.json")).read_text())["text"]
    amd = json.loads(next((D25_RUN / "amd" / "parsed").glob("pars_*.json")).read_text())["text"]

    def cq(answer, q, qs, sources):
        got = next(code_quotes(answer, q, qs, sources, key_terms(q, qs)), None)
        return got and got[1]

    split = ["全年总收入（Revenue）是多少", "全年总收入（Revenue）同比增长多少"]                      # 工具说明里的拆法：共有短语不能删光
    assert [metric_phrases(q, split) for q in split] == [["revenue"], ["revenue"]]
    assert cq("$25,785 million", split[0], split, [("s2", amd)]).startswith("| Revenue ($M) | $25,785")
    assert cq("14%", split[1], split, [("s2", amd)]).startswith("| Revenue ($M) | $25,785")
    both = ["NVIDIA FY2025 全年总收入（Revenue）是多少？", "AMD FY2024 全年总收入（Revenue）是多少？"]
    assert cq("$130,497 million", both[1], both, [("s1", nv), ("s2", amd)]) is None              # NVIDIA 的数核不到 AMD 题上
    assert cq("$130,497 million", both[0], both, [("s1", nv), ("s2", amd)]).startswith("| Revenue | $130,497")
    q = "Nvidia FY2025 全年营业利润（Operating income）是多少？"
    assert metric_phrases(q) == ["operating income"] and cq("$500 billion", q, [q], [("s1", nv)]) is None
    en = "What was NVIDIA's total revenue for fiscal 2025?"
    assert metric_phrases(en) == ["revenue"] and cq("$130,497 million", en, [en], [("s1", nv)]).startswith("| Revenue | $130,497")
    assert metric_phrases("Beta Beta Beta (Tri Beta) - Biology Honor Society 的摊位号是多少？")[0] == "beta beta beta"   # 名字里自带的括号不当注释


async def test_inference_answer_inside_verified_quote_counts_as_quote(tmp_path):
    """模型标了推断，但答案整个就在逐字核对过的原文引文里：按原文核，不标 uncertain；答案不在引文里的推断照旧标 uncertain。"""
    from owa_research.core.document import ParsedDocument

    first = "It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife."
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt)
        s.docs["docu_1"] = ParsedDocument(kind="text", title=None, text="Chapter I.\n\n" + first + "\n\nHowever little known the feelings.\n\nCHAPTER LXI.\n\nHappy for all her maternal feelings.")
        s.register_source(url="https://x.example/pp.txt", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="text/plain")
        qs = ["小说第一句话是什么（It is a truth universally acknowledged）？", "全书最后一章是第几章（CHAPTER）？"]
        lp = ResearchLoop(ResearchRequest(task="t", extract=qs), s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                          max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)
        answers, missing = lp.build_answers({"answers": [
            {"question": qs[0], "quote": first, "answer": first, "source_id": "s1", "kind": "inference", "uncertain": False},
            {"question": qs[1], "quote": "Happy for all her maternal feelings.", "answer": "CHAPTER LXI", "source_id": "s1", "kind": "inference", "uncertain": False}]})
        assert not answers[0].uncertain and answers[0].quote == first and not any(qs[0] in m for m in missing)
        assert answers[1].uncertain and any(qs[1] in m for m in missing)
    finally:
        await rt.aclose()
