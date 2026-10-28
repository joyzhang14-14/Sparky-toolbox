import pytest

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.tools import RESEARCH_TOOL_ALLOWLIST, RankPassagesParams, SearchWebParams
from owa_research.isolation.allowlist import ToolDispatcher, to_error_info


@pytest.fixture
def dispatcher():
    d = ToolDispatcher()

    async def search(params: SearchWebParams):
        return {"echo": params.query}

    d.register("search_web", search)
    return d


@pytest.mark.parametrize("name", ["bash", "read", "write", "exec", "python", "search_web2", ""])
async def test_unknown_tools_are_rejected(dispatcher, name):
    with pytest.raises(ToolError) as ei:
        await dispatcher.call(name, {"cmd": "ls"})
    assert ei.value.code == ErrorCode.TOOL_NOT_ALLOWED


async def test_allowed_but_unregistered_tool_rejected(dispatcher):
    with pytest.raises(ToolError) as ei:
        await dispatcher.call("render_page", {"url": "https://a.com"})
    assert ei.value.code == ErrorCode.TOOL_NOT_ALLOWED


async def test_invalid_args_rejected_without_trace(dispatcher):
    with pytest.raises(ToolError) as ei:
        await dispatcher.call("search_web", {"query": "q", "shell": "rm"})
    info = to_error_info(ei.value)
    assert info.code == ErrorCode.INVALID_ARGUMENT
    assert "Traceback" not in info.message


async def test_valid_call(dispatcher):
    assert await dispatcher.call("search_web", {"query": "hello"}) == {"echo": "hello"}


def test_register_outside_allowlist_fails():
    with pytest.raises(ValueError):
        ToolDispatcher().register("bash", None)  # type: ignore[arg-type]


def test_allowlist_matches_spec_section_5():
    assert RESEARCH_TOOL_ALLOWLIST == {
        "search_web", "fetch_page", "read_outline", "read_section", "browse_document",
        "render_page", "read_document", "extract_from", "read_evidence", "rank_passages", "sec_filings",
        "search_images", "page_images", "view_images", "inspect_image", "reverse_image_search",   # 图片动作（2026-09-29）
    }


def test_rank_passages_limits():
    with pytest.raises(Exception):
        RankPassagesParams(question="q", passages=[])


async def test_invalid_args_error_lists_expected_params(dispatcher):
    with pytest.raises(ToolError) as ei:
        await dispatcher.call("search_web", {"q": "x"})
    assert "期望参数" in ei.value.info.message and "query: string (required)" in ei.value.info.message


def test_budget_separates_search_and_lookup():
    from owa_research.core.tools_impl import Budgeted

    b = Budgeted(max_searches=2, max_pages=5, max_documents=2)
    b.take_search(); b.take_search()
    with pytest.raises(ToolError) as ei:
        b.take_search()
    assert ei.value.code == ErrorCode.BUDGET_EXCEEDED and "不能再调用该工具" in ei.value.info.message
    for _ in range(b.max_lookups):  # SEC 查询不受网页搜索预算影响
        b.take_lookup()
    with pytest.raises(ToolError):
        b.take_lookup()
    assert "search_web" in b.exhausted_note and "sec_filings" in b.exhausted_note
