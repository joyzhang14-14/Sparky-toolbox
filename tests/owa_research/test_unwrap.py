"""硬换行排版的纯文本（每行到固定宽度就折行）：同一段里被折开的行接回一行，标题、诗、居中的字、列表照旧；不是这种排版、或不是 text/plain 的原样不动。"""
import json
import textwrap

from owa_research.readers.document import parse_document, unwrap_hard_breaks

PARA = ("It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife. "
        "However little known the feelings or views of such a man may be on his first entering a neighbourhood, this truth is so well fixed.")
BOOK = "\n\n".join(["CHAPTER I.", textwrap.fill(PARA, 70), "          THE\n          END", "- first item\n- second item",
                    *(textwrap.fill(PARA.replace("truth", f"truth {i}"), 70) for i in range(12))])


def test_joins_wrapped_paragraphs_keeps_short_lines():
    out = unwrap_hard_breaks(BOOK)
    assert PARA in out.split("\n")                                   # 整段成了一行，长句子能整句引用
    assert "CHAPTER I.\n\n" in out and "          THE\n          END" in out and "- first item\n- second item" in out


def test_leaves_other_layouts_alone():
    prose = "\n".join(["Short line.", "Another one.", "Third."] * 10)    # 不是固定宽度折行
    assert unwrap_hard_breaks(prose) == prose
    assert unwrap_hard_breaks("one\ntwo") == "one\ntwo"


async def test_parse_document_unwraps_text_plain_only():
    doc, _ = await parse_document(BOOK.encode(), content_type="text/plain", url="https://x.example/book.txt")
    assert PARA in doc.text.split("\n")
    data = json.dumps({"lines": [textwrap.fill(PARA, 70)] * 30}, indent=2)
    doc, _ = await parse_document(data.encode(), content_type="application/json", url="https://x.example/a.json")
    assert doc.text == data
