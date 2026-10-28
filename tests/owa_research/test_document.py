from owa_research.core.document import ParsedDocument, PageSpan, sections_from_markdown, tables_from_markdown
from owa_research.readers.text_browser import TextBrowser


def test_sections_and_tables_from_markdown():
    md = "intro\n\n# A\ntext a\n\n## B\n| h1 | h2 |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n# C\nend"
    secs = sections_from_markdown(md)
    assert [s.title for s in secs] == ["(前言)", "A", "B", "C"] and [s.id for s in secs] == ["s0", "s1", "s2", "s3"]
    assert md[secs[2].char_start:secs[2].char_end].startswith("## B")
    tables = tables_from_markdown(md)
    assert len(tables) == 1 and tables[0].rows == [["h1", "h2"], ["1", "2"], ["3", "4"]]


def test_text_browser_paging_and_find():
    text = "".join(f"line {i} " for i in range(3000)) + " NEEDLE tail"
    doc = ParsedDocument(kind="text", title=None, text=text, pages=[PageSpan(1, 0, 10000), PageSpan(2, 10000, len(text))])
    tb = TextBrowser(screen_chars=8000)
    v = tb.view("a", doc, "open")
    assert v["screen"] == 1 and v["total_screens"] == 4 and v["document_page"] == 1
    v = tb.view("a", doc, "page_down")
    assert v["screen"] == 2 and v["document_page"] == 1  # 字符 8000 仍在第 1 页
    v = tb.view("a", doc, "page_down")
    assert v["screen"] == 3 and v["document_page"] == 2
    v = tb.view("a", doc, "find", query="needle")
    assert v["screen"] == 4 and "命中 1/1" in v["note"] and "NEEDLE" in v["text"]
    v = tb.view("a", doc, "goto", page=99)
    assert v["screen"] == 4
    assert "未找到" in tb.view("a", doc, "find", query="zzz")["note"]
