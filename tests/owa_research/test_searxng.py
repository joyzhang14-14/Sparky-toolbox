import httpx
import pytest

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.providers.searxng import SearxngClient, canonicalize_for_dedup, clean_query_for_engine, _parse, _rank_and_dedup, SearchHit


def test_canonicalize_strips_tracking_but_keeps_signature_like_params():
    a = canonicalize_for_dedup("https://WWW.Example.com/a/?utm_source=x&sig=abc#frag")
    b = canonicalize_for_dedup("https://example.com/a?sig=abc")
    assert a == b == "https://example.com/a?sig=abc"


def test_clean_query_removes_operators_for_wikipedia_only():
    assert clean_query_for_engine("site:sec.gov nvidia 10-K", "wikipedia") == "nvidia 10-K"
    assert clean_query_for_engine("site:sec.gov nvidia 10-K", "brave") == "site:sec.gov nvidia 10-K"


def test_parse_distinguishes_zero_results_from_all_failed():
    empty = _parse("q", {"results": [], "unresponsive_engines": []})
    assert not empty.all_failed and empty.hits == []
    failed = _parse("q", {"results": [], "unresponsive_engines": [["brave", "timeout"], ["ddg", "CAPTCHA"]]})
    assert failed.all_failed and len(failed.warnings) == 2


def test_parse_keeps_null_score_and_infobox():
    r = _parse("Tokyo", {
        "results": [{"url": "https://a.com/x", "title": "A", "content": "c", "engines": ["brave"], "score": None}],
        "infoboxes": [{"infobox": "Tokyo", "id": "https://en.wikipedia.org/wiki/Tokyo", "content": "capital", "engine": "wikipedia"}],
    })
    assert r.hits[0].score is None and r.hits[1].kind == "infobox"


def test_rank_per_host_limit_and_merge():
    hits = [SearchHit(title="", url=f"https://h.com/p{i}", canonical_url=f"https://h.com/p{i}", snippet="", engines=["e"], score=1.0, published_date=None, category=None) for i in range(4)]
    hits.append(SearchHit(title="", url="https://h.com/p0?utm_source=z", canonical_url="https://h.com/p0", snippet="longer snippet", engines=["f"], score=2.0, published_date=None, category=None))
    out = _rank_and_dedup(hits, min_score=None, per_host_limit=2)
    assert len(out) == 2 and out[0].engines == ["e", "f"] and out[0].score == 2.0


async def test_403_is_explained():
    c = SearxngClient("http://sx.test")
    c._client = httpx.AsyncClient(base_url="http://sx.test", transport=httpx.MockTransport(lambda req: httpx.Response(403)))
    with pytest.raises(ToolError) as ei:
        await c.search("q")
    assert ei.value.code == ErrorCode.SEARCH_UNAVAILABLE and "formats" in ei.value.info.message
