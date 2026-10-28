"""RSS / Atom 订阅源（readers/feed.py）：解析成条目清单给研究模型读；答案的引文落在某一条上时附上这一条（Answer.item）。"""
import pytest

from owa_research.core.config import Settings
from owa_research.core.loop import ResearchLoop
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted
from owa_research.readers.document import parse_document
from owa_research.readers.feed import match_entry, parse_feed

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Markets</title>
<item><title>Fed holds rates steady &amp; signals patience</title><link>https://paywall.example/a/1</link>
<pubDate>Mon, 19 Oct 2026 21:37:00 -0400</pubDate><description><![CDATA[<p>The central bank <b>kept</b> rates unchanged.</p>]]></description></item>
<item><title>v0.40.7</title><link>https://paywall.example/a/2</link></item>
<item><title>No link here</title></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes</title>
<entry><title>Model X released</title><link rel="alternate" href="https://lab.example/x"/><link rel="enclosure" href="https://lab.example/x.zip"/>
<updated>2026-10-18T00:28:57Z</updated><content type="html">&lt;p&gt;Faster &amp;amp; smaller.&lt;/p&gt;</content></entry>
</feed>"""


def test_parse_rss_and_atom():
    d = parse_feed(RSS)
    assert d.kind == "feed" and d.title == "Markets"
    assert d.meta["entries"] == [
        {"title": "Fed holds rates steady & signals patience", "url": "https://paywall.example/a/1", "published_at": "2026-10-19T21:37:00-04:00",
         "summary": "The central bank kept rates unchanged."},
        {"title": "v0.40.7", "url": "https://paywall.example/a/2", "published_at": None, "summary": ""}]          # 没有链接的条目不收
    assert "### Fed holds rates steady & signals patience\n发布：2026-10-19T21:37:00-04:00\n链接：https://paywall.example/a/1" in d.text
    a = parse_feed(ATOM).meta["entries"]
    assert a == [{"title": "Model X released", "url": "https://lab.example/x", "published_at": "2026-10-18T00:28:57+00:00", "summary": "Faster & smaller."}]


async def test_parse_document_routes_feeds_and_ignores_the_rest():
    doc, sniffed = await parse_document(RSS, content_type="text/html", url="https://paywall.example/rss")   # 订阅源按内容认，不看 content type
    assert doc.kind == "feed" and sniffed.kind == "feed"
    assert parse_feed(b"<!doctype html><html><body><rss>no</rss></body></html>") is None
    assert parse_feed(b"plain text") is None and parse_feed(b"<rss><channel><title>empty</title></channel></rss>") is None


def test_entities_are_not_expanded(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    xxe = (f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file://{secret}">]>'
           '<rss><channel><item><title>t &x; t</title><link>https://a.example/1</link></item></channel></rss>').encode()
    d = parse_feed(xxe)
    assert d is None or "TOPSECRET" not in d.text


def test_match_entry():
    e = parse_feed(RSS).meta["entries"]
    assert match_entry(e, "美联储按兵不动", "Fed holds rates steady & signals patience") is e[0]
    assert match_entry(e, "见 https://paywall.example/a/2", "whatever") is e[1]                  # 答案里写了文章链接
    assert match_entry(e, None, "v0.40.7") is e[1]                                                  # 短标题要和引文完全一样
    assert match_entry(e, None, "v0.40") is None and match_entry(e, None, "central bank") is None


@pytest.fixture
async def loop(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    s = ResearchSession(rt)
    s.docs["docu_1"] = parse_feed(RSS)
    s.register_source(url="https://paywall.example/rss", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="application/rss+xml")
    lp = ResearchLoop(ResearchRequest(task="t", extract=["第 1 重要的新闻的标题是什么？", "第 2 重要的新闻的标题是什么？"]), s,
                      Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=1, max_documents=1),
                      max_model_tokens=1000, deadline=rt.started_at + 100, subquery_default=1, read_top_n=1)
    yield lp
    await rt.aclose()


async def test_answers_from_a_feed_carry_the_item(loop):
    answers, _ = loop.build_answers({"answers": [
        {"quote": "Fed holds rates steady & signals patience", "answer": "Fed holds rates steady & signals patience", "source_id": "s1", "kind": "quote"},
        {"quote": "The central bank kept rates unchanged.", "answer": "The central bank kept rates unchanged.", "source_id": "s1", "kind": "quote"}]})
    assert answers[0].item.model_dump() == {"url": "https://paywall.example/a/1", "title": "Fed holds rates steady & signals patience",
                                            "published_at": "2026-10-19T21:37:00-04:00"}
    assert answers[0].quote and not answers[0].uncertain
    assert answers[1].item is None                                                                  # 引文不在标题上：不附条目
