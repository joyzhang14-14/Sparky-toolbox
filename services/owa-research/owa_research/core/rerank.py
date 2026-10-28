"""rank_passages 与向量工具（规格 5、9）。

- OWA_RERANK_ENABLED=true：本地 embedding 模型（fastembed / ONNX，CPU）做余弦相似度排序；也供子查询去重与事后引用匹配。
- OWA_RERANK_ENABLED=false：rank_passages 退化为一次模型调用对截到 200 字符的片段返回排序索引；引用匹配退化为字面匹配；接口不变。
- 重排是关键词搜索之上的补充，精确数字、代码、文件编号仍以关键词命中为准（关键词加权在 hybrid_score 里）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from owa_research.core.model_client import ModelClient

_QUERY_PREFIX = {"BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ", "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: "}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}|\d[\d,\.]*\d|\d")


@dataclass
class Ranked:
    id: str
    score: float
    rank: int


class EmbeddingBackend:
    """fastembed 懒加载单例；模型文件由 FASTEMBED_CACHE_PATH 指定（镜像内预置）。"""

    def __init__(self, model_name: str, *, threads: int = 4) -> None:
        self.model_name = model_name
        self._threads = threads
        self._model: Any = None
        self.load_seconds: float | None = None
        self.embed_calls = 0
        self.embed_seconds = 0.0

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            t0 = time.monotonic()
            self._model = TextEmbedding(model_name=self.model_name, threads=self._threads)
            self.load_seconds = time.monotonic() - t0
        return self._model

    def embed_sync(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        model = self._load()
        prefix = _QUERY_PREFIX.get(self.model_name, "") if is_query else ""
        t0 = time.monotonic()
        vecs = [list(map(float, v)) for v in model.embed([prefix + t for t in texts], batch_size=16)]
        self.embed_calls += 1
        self.embed_seconds += time.monotonic() - t0
        return vecs

    async def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_sync, texts, is_query=is_query)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


def keyword_overlap(question: str, passage: str) -> float:
    """精确 token（数字、编号、专名）命中比例，用来保底关键词命中。"""
    q = {t.lower() for t in _TOKEN_RE.findall(question)}
    if not q:
        return 0.0
    p = {t.lower() for t in _TOKEN_RE.findall(passage)}
    return len(q & p) / len(q)


class Reranker:
    def __init__(self, *, backend: EmbeddingBackend | None, model: ModelClient | None) -> None:
        self.backend = backend
        self.model = model

    @property
    def mode(self) -> str:
        return "embedding" if self.backend else "model-call"

    async def _embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]] | None:
        """backend 不可用（模型文件缺失、内存不足）时降级为无 backend，接口不变。"""
        if self.backend is None:
            return None
        try:
            return await self.backend.embed(texts, is_query=is_query)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("owa.rerank").warning("embedding backend 不可用，降级为模型调用/字面匹配: %s", type(e).__name__)
            self.backend = None
            return None

    async def rank(self, question: str, passages: list[tuple[str, str]], *, top_k: int = 10, keyword_weight: float = 0.3) -> list[Ranked]:
        if not passages:
            return []
        qv_list = await self._embed([question], is_query=True)
        pvs = await self._embed([t for _, t in passages]) if qv_list is not None else None
        if qv_list is not None and pvs is not None:
            qv = qv_list[0]
            scored = []
            for (pid, text), pv in zip(passages, pvs):
                s = (1 - keyword_weight) * cosine(qv, pv) + keyword_weight * keyword_overlap(question, text)
                scored.append((pid, s))
        elif self.model is not None:
            scored = await self._rank_by_model(question, passages)
        else:
            scored = [(pid, keyword_overlap(question, t)) for pid, t in passages]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [Ranked(id=pid, score=round(s, 4), rank=i + 1) for i, (pid, s) in enumerate(scored[:top_k])]

    async def _rank_by_model(self, question: str, passages: list[tuple[str, str]]) -> list[tuple[str, float]]:
        assert self.model is not None
        items = "\n".join(f"[{i}] {t[:200].replace(chr(10), ' ')}" for i, (_, t) in enumerate(passages))
        schema = {"title": "ranking", "type": "object", "properties": {"order": {"type": "array", "items": {"type": "integer"}}}, "required": ["order"]}
        obj, _ = await self.model.chat_json(
            [{"role": "user", "content": f"Question: {question}\n\nRank these snippets by relevance to the question, most relevant first. Return JSON {{\"order\": [indices]}} using every index exactly once.\n\n{items}"}],
            json_schema=schema, max_tokens=1024, temperature=0.0, thinking=False,
        )
        order = [i for i in obj.get("order", []) if isinstance(i, int) and 0 <= i < len(passages)]
        seen = set(order)
        order += [i for i in range(len(passages)) if i not in seen]
        n = len(passages)
        return [(passages[i][0], (n - pos) / n) for pos, i in enumerate(order)]

    async def dedup(self, texts: list[str], *, threshold: float = 0.86) -> list[str]:
        """子查询去重：embedding 相似度超阈值的丢弃（无 backend 时按小写字面去重）。"""
        if not self.backend:
            out, seen = [], set()
            for t in texts:
                k = re.sub(r"\s+", " ", t.strip().lower())
                if k not in seen:
                    seen.add(k)
                    out.append(t)
            return out
        vecs = await self._embed(texts)
        if vecs is None:
            return await self.dedup(texts, threshold=threshold)
        kept: list[int] = []
        for i, v in enumerate(vecs):
            if all(cosine(v, vecs[j]) < threshold for j in kept):
                kept.append(i)
        return [texts[i] for i in kept]

    async def match_citations(self, sentences: list[str], sources: list[tuple[str, str]], *, threshold: float = 0.7) -> list[tuple[int, str | None, float]]:
        """把报告句子匹配回证据片段：返回 (句子序号, 片段 id 或 None, 分数)。无 backend 时用关键词重叠（阈值 0.5）。"""
        if not sentences or not sources:
            return [(i, None, 0.0) for i in range(len(sentences))]
        sv = await self._embed(sentences)
        pv = await self._embed([t for _, t in sources]) if sv is not None else None
        if sv is not None and pv is not None:
            out = []
            for i, v in enumerate(sv):
                best = max(range(len(sources)), key=lambda j: cosine(v, pv[j]))
                score = cosine(v, pv[best])
                out.append((i, sources[best][0] if score >= threshold else None, round(score, 4)))
            return out
        out = []
        for i, s in enumerate(sentences):
            best = max(range(len(sources)), key=lambda j: keyword_overlap(s, sources[j][1]))
            score = keyword_overlap(s, sources[best][1])
            out.append((i, sources[best][0] if score >= 0.5 else None, round(score, 4)))
        return out


@lru_cache(maxsize=4)
def get_backend(model_name: str, threads: int = 4) -> EmbeddingBackend:
    return EmbeddingBackend(model_name, threads=threads)
