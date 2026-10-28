"""render_page：渲染参数传到浏览器；不复用同一网址的静态抓取结果，同参数重复渲染复用。"""
from types import SimpleNamespace

from owa_research.core.config import Settings
from owa_research.core.runtime import Runtime
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted, register_tools
from owa_research.core.rerank import Reranker
from owa_research.readers.browser import RenderResult

URL = "https://list.example/top"
STATIC = "<html><body><h1>Top</h1><p>" + "The leaderboard below is filled in by JavaScript after the page loads. " * 4 + "</p></body></html>"
RENDERED = "<html><body><h1>Top</h1><ol>" + "".join(f"<li>skill-{i} {i}00 installs</li>" for i in range(1, 4)) + "</ol></body></html>"
TEXT = "Top\n" + "\n".join(f"skill-{i} {i}00 installs" for i in range(1, 4))


async def test_render_page_passes_options_and_bypasses_static_cache(tmp_path):
    rt = Runtime.build(Settings(), None, tmp_path)
    calls = []

    async def fetch(url, **kw):
        return SimpleNamespace(content=STATIC.encode(), content_type="text/html", final_url=url, fetched_at="now", redirect_to=None)

    async def render(url, **kw):
        calls.append(kw)
        return RenderResult(url=url, final_url=url, status=200, title="Top", text=TEXT, text_length=len(TEXT), html_head=RENDERED, fetched_at="now", elapsed=0.1)

    rt.fetcher.fetch, rt.browser.render = fetch, render
    s = ResearchSession(rt)
    register_tools(s, Reranker(backend=None, model=None), Budgeted(max_searches=4, max_pages=9, max_documents=2), goal="q")
    try:
        static = await rt.dispatcher.call("fetch_page", {"url": URL})
        assert "skill-1" not in static["text"] and not calls
        args = {"url": URL, "action": "read_dom", "wait_for_selector": "ol li", "scroll_to_bottom": True}
        out = await rt.dispatcher.call("render_page", args)
        assert "skill-3 300 installs" in out["text"]
        assert calls[0]["action"] == "read_dom" and calls[0]["wait_for_selector"] == "ol li" and calls[0]["scroll_to_bottom"] is True
        assert (await rt.dispatcher.call("render_page", args))["artifact_id"] == out["artifact_id"] and len(calls) == 1
        await rt.dispatcher.call("render_page", {**args, "scroll_to_bottom": False})
        assert len(calls) == 2
    finally:
        await rt.aclose()
