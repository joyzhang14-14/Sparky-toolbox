"""SEC adapter：用真实 EDGAR 接口做有界 live 测试（网络不可用时跳过）。"""
import httpx
import pytest

from owa_research.core.config import Settings
from owa_research.isolation.egress import EgressPolicy
from owa_research.providers.sec import SecAdapter
from owa_research.readers.http import HttpFetcher


@pytest.fixture
async def sec():
    f = HttpFetcher(Settings(), EgressPolicy())
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.head("https://www.sec.gov/")
    except httpx.HTTPError:
        pytest.skip("SEC 不可达")
    yield SecAdapter(f)
    await f.aclose()


async def test_filings_and_concept(sec):
    assert await sec.resolve_cik("NVIDIA CORPORATION") == 1045810 and await sec.resolve_cik("Apple Inc.") == 320193
    r = await sec.filings("NVDA", forms=["10-K"], limit=2)
    assert r["cik"] == 1045810 and r["filings"] and r["filings"][0]["form"] == "10-K"
    assert r["filings"][0]["primary_document_url"].startswith("https://www.sec.gov/Archives/edgar/data/1045810/")
    c = await sec.concept("NVDA", "revenue", form="10-K", fiscal_years=[2025])
    vals = {f["value"] for f in c["facts"] if f["end"] == "2025-01-26"}
    assert 130_497_000_000 in vals and c["facts"][0]["unit"] == "USD"


async def test_concept_merges_tags_across_label_changes(sec):
    """Apple 2019 起把 Revenues 换成 RevenueFromContractWithCustomer…：必须合并标签才能拿到近年数字。"""
    c = await sec.concept("AAPL", "revenue", form="10-K")
    years = {f["end"] for f in c["facts"]}
    assert "2024-09-28" in years and len(c["tags_used"]) >= 1
    assert any(f["value"] == 391_035_000_000 for f in c["facts"])
