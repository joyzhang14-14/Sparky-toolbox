"""D20 B 层：PDF 表格行并入正文（merge_tables）、行级门、find 空白容错、read_document 表格视图。
样本 tests/fixtures/real/booths.pdf 是 2026-09-22 事故里的社团摊位表：名字一列、编号一列分开排，文字层里对不上号。"""
from pathlib import Path

import pytest

from owa_research.core.document import ParsedDocument, TableBlock
from owa_research.readers import pdf
from owa_research.readers.text_browser import TextBrowser, find_all

BOOTHS = Path(__file__).parents[1] / "fixtures" / "real" / "booths.pdf"


def _booths() -> bytes:
    """没有这份样本时跳过用到它的测试（样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布）。"""
    if not BOOTHS.exists():
        pytest.skip("样本 booths.pdf 是 UCI 的社团摊位表，不随公开版本发布")
    return BOOTHS.read_bytes()


@pytest.fixture(scope="module")
def merged():
    return pdf.parse_pdf(_booths(), merge_tables=True)


def _line_at(text: str, pos: int) -> str:
    return text[text.rfind("\n", 0, pos) + 1: text.find("\n", pos)]


def test_cell_keeps_code_lists_and_flattens_names():
    assert pdf._cell("F279\nF280") == "F279 / F280"
    assert pdf._cell("Boxing Club\nClimbing Club") == "Boxing Club Climbing Club"
    assert pdf._cell("Institute of Electrical and Electronics\nEngineers at UCI - IEEE") == "Institute of Electrical and Electronics Engineers at UCI - IEEE"
    assert pdf._cell(None) == ""


def test_merge_off_keeps_text_layer_only():
    plain = pdf.parse_pdf(_booths())
    assert "[表" not in plain.text and "tables_merged" not in plain.meta
    assert plain.meta["total_pages"] == 3


def test_table_rows_are_on_their_page_before_the_text_layer(merged):
    p2 = next(p for p in merged.pages if p.page == 2)
    pos = merged.text.index("Institute of Electrical and Electronics Engineers at UCI - IEEE | E257")
    assert merged.page_of(pos) == 2 and pos - p2.char_start < 12_000       # read_evidence(pages=[2]) 的 12,000 字之内
    assert merged.text[p2.char_start:].lstrip("\n").startswith("<!-- page 2 -->\n[表")
    assert "[表1 p.2]  |  | ICS Student Council | F362" in merged.text


@pytest.mark.parametrize("query,code", [("IEEE", "E257"), ("Electronics Engineers", "E257"), ("E257", "E257"), ("ICS Student Council", "F362")])
def test_find_first_hit_is_the_table_row(merged, query, code):
    hits = find_all(merged.text, query)
    assert hits and code in _line_at(merged.text, hits[0])


def test_absent_club_has_no_hit(merged):
    assert find_all(merged.text, "Claude") == []


def test_gate(merged):
    assert all(t.method != "alignment" for t in merged.tables)            # 页 1 地图（quality 正好 0.6）不再算表
    ieee = _line_at(merged.text, merged.text.index("E257"))
    assert ieee.startswith("[表2 p.2] ")                                   # 格内换行 1 处：普通前缀
    low = [l for l in merged.text.splitlines() if l.startswith("[表") and l.split("]")[0].endswith("?")]
    assert low                                                             # 格内换行超过 3 处的行保留但标低置信
    big = TableBlock(index=0, rows=[["x" * 500, "a"], ["b", "c"]], page=1)
    assert pdf.table_lines([big]) == ""                                    # 退化表整表不并入
    al = TableBlock(index=3, rows=[["a", "1"]] * 4, page=5, method="alignment")
    assert pdf.table_lines([al]).startswith("[表3 p.5 ?] a | 1")


def test_find_tolerates_whitespace():
    doc = ParsedDocument(kind="pdf", title=None, text="Institute of Electrical and Electronics\nEngineers at UCI")
    v = TextBrowser().view("a", doc, "find", query="electronics   engineers")
    assert "命中 1/1" in v["note"]


async def test_read_document_table_view(tmp_path, merged):
    from owa_research.core.config import Settings
    from owa_research.core.rerank import Reranker
    from owa_research.core.runtime import Runtime
    from owa_research.core.session import ResearchSession
    from owa_research.core.tools_impl import RESULT_LIMIT, Budgeted, register_tools

    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        s = ResearchSession(rt, merge_tables=True)
        s.docs["docu_1"] = merged
        s.register_source(url="https://x/booths.pdf", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/pdf")
        register_tools(s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=5, max_documents=1), goal="q")
        r = await rt.dispatcher.call("read_document", {"artifact_id": "docu_1"})
        assert "rows" not in r["tables"][0] and r["tables"][0]["nrows"] > 0 and "find" in r["note"]
        import json
        assert len(json.dumps(r, ensure_ascii=False)) < RESULT_LIMIT
        s.docs["docu_2"] = ParsedDocument(kind="pdf", title=None, text="x", meta={"tables_merged": True},
                                          tables=[TableBlock(index=i, rows=[["a"]], page=1, title="t" * 200) for i in range(40)],
                                          sections=[type(merged.sections[0])(id=f"s{i}", title="很长的章节标题" * 20, level=1, char_start=0, char_end=1) for i in range(200)])
        s.register_source(url="https://x/big.pdf", final_url=None, fetched_at="now", artifact_id="docu_2", content_hash=None, content_type="application/pdf")
        big = await rt.dispatcher.call("read_document", {"artifact_id": "docu_2"})
        assert big["error"] == "RESULT_TOO_LARGE" and "browse_document" in big["hint"]
    finally:
        await rt.aclose()
