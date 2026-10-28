import pytest

from owa_research.core.rerank import Reranker, keyword_overlap


class _FakeBackend:
    """确定性假 embedding：按关键词集合构造向量，便于测试排序/去重/引用匹配逻辑。"""

    model_name = "fake"

    async def embed(self, texts, *, is_query=False):
        vocab = ["nvidia", "revenue", "apple", "tokyo", "python", "2025", "2024"]
        return [[1.0 if w in t.lower() else 0.0 for w in vocab] + [0.01] for t in texts]


class _Broken:
    model_name = "broken"

    async def embed(self, texts, *, is_query=False):
        raise RuntimeError("no model files")


async def test_rank_prefers_matching_passage_and_keywords():
    r = Reranker(backend=_FakeBackend(), model=None)
    out = await r.rank("NVIDIA revenue 2025", [("a", "Tokyo is a city"), ("b", "NVIDIA revenue for 2025 was $130.5B"), ("c", "Apple revenue 2024")], top_k=2)
    assert [x.id for x in out] == ["b", "c"] and out[0].rank == 1 and r.mode == "embedding"


async def test_dedup_and_citation_match():
    r = Reranker(backend=_FakeBackend(), model=None)
    kept = await r.dedup(["NVIDIA revenue 2025", "nvidia 2025 revenue", "Apple revenue 2024"], threshold=0.86)
    assert kept == ["NVIDIA revenue 2025", "Apple revenue 2024"]
    m = await r.match_citations(["NVIDIA revenue 2025 was $130.5B"], [("e1", "Apple revenue 2024"), ("e2", "NVIDIA revenue 2025: $130,497M")], threshold=0.7)
    assert m[0][1] == "e2"


async def test_broken_backend_degrades_to_keyword_mode():
    r = Reranker(backend=_Broken(), model=None)
    out = await r.rank("python asyncio", [("a", "python asyncio semaphore"), ("b", "tokyo")])
    assert out[0].id == "a" and r.backend is None and r.mode == "model-call"


def test_keyword_overlap_exact_numbers():
    assert keyword_overlap("revenue 130,497", "Revenue was 130,497 million") == 1.0
    assert keyword_overlap("revenue 130,497", "Revenue was 60,922 million") == 0.5
