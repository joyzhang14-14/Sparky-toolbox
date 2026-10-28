"""解析器测试：fixture 由 tests/fixtures/make_fixtures.py 生成。"""
from pathlib import Path

import pytest

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.readers.document import parse_document, sniff
from owa_research.readers.html import parse_html
from owa_research.readers.office import parse_docx, parse_pptx, parse_xlsx
from owa_research.readers.pdf import parse_pdf, scan_metrics

F = Path(__file__).parents[1] / "fixtures"


def test_sniff_by_signature_not_extension():
    assert sniff((F / "report.pdf").read_bytes(), "text/html", "https://x/y.html").kind == "pdf"
    assert sniff((F / "report.docx").read_bytes()).kind == "docx"
    assert sniff((F / "report.xlsx").read_bytes()).kind == "xlsx"
    assert sniff((F / "report.pptx").read_bytes()).kind == "pptx"
    assert sniff((F / "report.doc").read_bytes(), "", "https://x/f.doc").kind == "doc"
    assert sniff((F / "report.xls").read_bytes(), "application/vnd.ms-excel").kind == "xls"
    assert sniff(b"<!doctype html><html>", "").kind == "html"
    assert sniff(b"\x00\x01\x02binary", "application/octet-stream").kind == "unknown"


def test_pdf_text_layer_alignment_table_and_toc():
    d = parse_pdf((F / "report.pdf").read_bytes())
    assert not d.scanned and len(d.pages) == 2 and d.published_at == "2025-02-26"
    assert [s.title for s in d.sections] == ["Consolidated Statements of Income", "Note 1"]
    t = d.tables[0]
    assert t.method == "alignment" and t.page == 1 and t.quality == 1.0 and not t.uncertain
    assert ["Revenue", "130,497", "60,922", "26,974"] in t.rows and ["Net income", "72,880", "29,760", "4,368"] in t.rows
    assert "Jan 26, 2025" in (t.title or "")
    assert d.page_of(d.sections[1].char_start) == 2


def test_scanned_pdf_detected_by_thresholds():
    m = scan_metrics((F / "report_scanned.pdf").read_bytes())
    assert m.scanned and "avg_chars<50" in m.reasons and m.image_coverage >= 0.8


async def test_scanned_pdf_without_model_reports_warning():
    doc, s = await parse_document((F / "report_scanned.pdf").read_bytes(), content_type="application/pdf")
    assert s.kind == "pdf" and doc.scanned and any("视觉模型" in w for w in doc.warnings)


def test_html_sections_tables_and_injection_kept_as_data():
    d = parse_html((F / "page.html").read_text(), url="https://example.com/x")
    assert d.title == "Quarterly update" and d.published_at == "2025-02-26"
    assert [s.title for s in d.sections] == ["Quarterly update", "Segment table"]
    assert d.tables[0].rows == [["Segment", "Q4 FY25"], ["Data Center", "35,580"], ["Gaming", "2,544"]]
    assert "Ignore previous instructions" in d.text  # 只是数据，不是指令


def test_html_rowspan_colspan():
    html = "<html><body><table><tr><th rowspan=2>A</th><th colspan=2>B</th></tr><tr><th>b1</th><th>b2</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table></body></html>"
    d = parse_html(html)
    assert d.tables[0].rows == [["A", "B", ""], ["A", "b1", "b2"], ["1", "2", "3"]]


def test_office_readers():
    x = parse_docx((F / "report.docx").read_bytes())
    assert [s.title for s in x.sections] == ["Fixture Report", "Results"] and x.tables[0].rows[1] == ["Revenue", "130,497", "60,922"]
    x = parse_xlsx((F / "report.xlsx").read_bytes())
    assert x.tables[0].sheet == "Income" and x.tables[0].rows[1][:3] == ["Revenue", "130497", "60922"]
    assert x.tables[0].footnotes == ["公式 D2: =B2/C2-1"] and "未重算" in x.meta["note"]
    x = parse_pptx((F / "report.pptx").read_bytes())
    assert len(x.pages) == 2 and x.tables[0].page == 2 and x.tables[0].rows[1] == ["Revenue", "130,497"]


@pytest.mark.parametrize("name", ["report.doc", "report.xls", "report.ppt"])
async def test_legacy_office_via_libreoffice(name):
    import shutil

    if not shutil.which("soffice"):
        pytest.skip("本机无 LibreOffice")
    doc, s = await parse_document((F / name).read_bytes(), url=f"https://x/{name}")
    assert s.kind in ("docx", "xlsx", "pptx")
    assert any("Revenue" in c for t in doc.tables for r in t.rows for c in r)


async def test_unsupported_format():
    with pytest.raises(ToolError) as ei:
        await parse_document(b"\x00\x01\x02\x03garbage", content_type="application/octet-stream")
    assert ei.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_html_list_page_keeps_items():
    items = "".join(f"<li><h3>owner{i}/repo{i}</h3><p>video tool {i}</p><span>{i}k stars</span></li>" for i in range(1, 6))
    html = f"<html><body><nav><a>Home</a><a>Explore</a></nav><main><h2>5 results</h2><ul>{items}</ul></main><footer>© site</footer></body></html>"
    d = parse_html(html)
    assert d.method == "lxml-blocks"
    assert all(f"owner{i}/repo{i}" in d.text and f"{i}k stars" in d.text for i in range(1, 6))
    assert "Explore" not in d.text and "© site" not in d.text
