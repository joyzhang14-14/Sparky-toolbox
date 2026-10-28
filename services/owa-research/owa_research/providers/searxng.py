"""SearXNG 搜索客户端（规格 5、8）。

- 使用官方 /search JSON 契约：q、categories、engines、language、pageno、time_range、safesearch；没有 limit，客户端截断。
- 来源部分失败放进 warnings；合法零结果与上游全失败区分。
- 上游 score 可为空，不伪造分数。
- URL 启发式排序、每主机最多 2 条、去 utm 规范化，都不用模型。
- 查询按引擎清洗：Wikipedia 等对 site: 一类操作符返回 400。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from owa_research.core.errors import ErrorCode, ToolError

_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref|ref_src|igshid|yclid|_ga)$", re.I)
_OPERATOR_RE = re.compile(r"\b(site|inurl|intitle|filetype|allintitle|allinurl):\S+", re.I)
# 这些引擎不接受 Google 式操作符
_NO_OPERATOR_ENGINES = {"wikipedia", "crossref", "openalex", "arxiv", "github", "yandex", "seznam"}
_ENGINES_ACCEPT_OPERATORS = {"brave", "duckduckgo", "startpage"}
_MIN_INTERVAL_SECONDS = 2.0


@dataclass
class SearchHit:
    title: str
    url: str
    canonical_url: str
    snippet: str
    engines: list[str]
    score: float | None
    published_date: str | None
    category: str | None
    kind: str = "result"  # result / infobox


@dataclass
class SearchResponse:
    query: str
    hits: list[SearchHit]
    warnings: list[str] = field(default_factory=list)
    unresponsive_engines: list[tuple[str, str]] = field(default_factory=list)
    engines_used: list[str] = field(default_factory=list)
    all_failed: bool = False
    seconds: float = 0.0


def canonicalize_for_dedup(url: str) -> str:
    """去重键：去 fragment、去已知追踪参数、小写主机、去尾部 /。不用于实际抓取。"""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def clean_query_for_engine(query: str, engine: str) -> str:
    if engine in _NO_OPERATOR_ENGINES:
        return re.sub(r"\s+", " ", _OPERATOR_RE.sub("", query)).strip() or query
    return query


def url_heuristic_bonus(url: str) -> float:
    """不用模型的 URL 启发式：偏好 https、短路径、非聚合/社交站；负分给明显噪音。"""
    parts = urlsplit(url)
    bonus = 0.0
    host = parts.netloc.lower()
    if parts.scheme == "https":
        bonus += 0.1
    depth = parts.path.count("/")
    bonus -= 0.02 * max(0, depth - 3)
    if any(h in host for h in ("pinterest.", "facebook.", "instagram.", "tiktok.", "quora.")):
        bonus -= 0.5
    if host.endswith((".gov", ".edu")) or ".gov." in host or ".edu." in host:
        bonus += 0.2
    if re.search(r"\.(pdf|xlsx?|docx?|pptx?)$", parts.path, re.I):
        bonus += 0.1
    return bonus


class SearxngClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30, user_agent: str = "open-web-access") -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=5),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._last_call = 0.0
        self._gap = asyncio.Lock()
        self.calls = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> bool:
        try:
            r = await self._client.get("/healthz")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def search(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        engines: list[str] | None = None,
        language: str | None = "en",
        pageno: int = 1,
        time_range: str | None = None,
        safesearch: int = 0,
        max_results: int = 20,
        min_score: float | None = None,
        per_host_limit: int = 2,
    ) -> SearchResponse:
        params: dict[str, Any] = {"q": query, "format": "json", "pageno": pageno, "safesearch": safesearch}
        if categories:
            params["categories"] = ",".join(categories)
        if engines:
            params["engines"] = ",".join(engines)
            if all(e in _NO_OPERATOR_ENGINES for e in engines):
                params["q"] = clean_query_for_engine(query, engines[0])
        if language:
            params["language"] = language
        if time_range:
            params["time_range"] = time_range

        async with self._gap:  # 同一研究内搜索请求间隔约 2 秒
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        t0 = time.monotonic()
        try:
            r = await self._client.get("/search", params=params)
        except httpx.HTTPError as e:
            raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SearXNG 不可达: {type(e).__name__}", retryable=True, stage="search") from None
        self.calls += 1
        if r.status_code == 429:
            raise ToolError(ErrorCode.RATE_LIMITED, "SearXNG 限流（检查 server.limiter）", retryable=True, stage="search")
        if r.status_code == 403:
            raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, "SearXNG 返回 403（JSON format 未在 search.formats 启用？）", stage="search")
        if r.status_code != 200:
            raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SearXNG 返回 {r.status_code}", retryable=r.status_code >= 500, stage="search")
        data = r.json()
        resp = _parse(query, data)
        resp.seconds = time.monotonic() - t0
        resp.hits = _rank_and_dedup(resp.hits, min_score=min_score, per_host_limit=per_host_limit)[:max_results]
        return resp

    async def search_images(self, query: str, *, pageno: int = 1, max_results: int = 20) -> list[dict[str, Any]]:
        """图片搜索（categories=images）：返回 [{img_src, url（图所在网页）, title, resolution, engine}]，按引擎给的顺序去重。"""
        async with self._gap:
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        try:
            r = await self._client.get("/search", params={"q": query, "format": "json", "pageno": pageno, "categories": "images", "safesearch": 0})
        except httpx.HTTPError as e:
            raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SearXNG 不可达: {type(e).__name__}", retryable=True, stage="search") from None
        self.calls += 1
        if r.status_code != 200:
            raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SearXNG 返回 {r.status_code}", retryable=r.status_code >= 500, stage="search")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for x in r.json().get("results", []):
            src = str(x.get("img_src") or "")
            if not src.startswith(("http://", "https://")) or src in seen:
                continue
            seen.add(src)
            page = str(x.get("url") or "")
            out.append({"img_src": src, "url": page if page.startswith(("http://", "https://")) else None, "title": str(x.get("title") or "")[:200],
                        "resolution": str(x.get("resolution") or "")[:20] or None, "engine": x.get("engine")})
            if len(out) >= max_results:
                break
        return out


def _parse(query: str, data: dict[str, Any]) -> SearchResponse:
    hits: list[SearchHit] = []
    for r in data.get("results", []):
        url = r.get("url")
        if not url or not url.startswith(("http://", "https://")):
            continue
        hits.append(
            SearchHit(
                title=(r.get("title") or "").strip(),
                url=url,
                canonical_url=canonicalize_for_dedup(url),
                snippet=(r.get("content") or "").strip(),
                engines=list(r.get("engines") or []),
                score=_score(r.get("score")),
                published_date=r.get("publishedDate") or None,
                category=r.get("category"),
            )
        )
    for box in data.get("infoboxes", []):
        url = box.get("id") or next((u.get("url") for u in box.get("urls", []) if u.get("url")), None)
        if not url:
            continue
        hits.append(
            SearchHit(
                title=box.get("infobox") or "",
                url=url,
                canonical_url=canonicalize_for_dedup(url),
                snippet=(box.get("content") or "").strip(),
                engines=[box.get("engine")] if box.get("engine") else [],
                score=None,
                published_date=None,
                category=None,
                kind="infobox",
            )
        )
    unresponsive = [(e[0], e[1]) for e in data.get("unresponsive_engines", []) if isinstance(e, (list, tuple)) and len(e) >= 2]
    warnings = [f"引擎 {name} 未响应: {reason}" for name, reason in unresponsive]
    engines_used = sorted({e for h in hits for e in h.engines})
    all_failed = not hits and bool(unresponsive) and not engines_used
    return SearchResponse(
        query=query, hits=hits, warnings=warnings, unresponsive_engines=unresponsive,
        engines_used=engines_used, all_failed=all_failed,
    )


def _score(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rank_and_dedup(hits: list[SearchHit], *, min_score: float | None, per_host_limit: int) -> list[SearchHit]:
    if min_score is not None:
        hits = [h for h in hits if h.score is not None and h.score >= min_score]
    # 同一规范化 URL 合并（保留引擎并集、较高分数）
    merged: dict[str, SearchHit] = {}
    for h in hits:
        cur = merged.get(h.canonical_url)
        if cur is None:
            merged[h.canonical_url] = h
            continue
        cur.engines = sorted(set(cur.engines) | set(h.engines))
        if (h.score or 0) > (cur.score or 0):
            cur.score = h.score
        if len(h.snippet) > len(cur.snippet):
            cur.snippet = h.snippet
    ranked = sorted(merged.values(), key=lambda h: ((h.score or 0.0) + url_heuristic_bonus(h.url), len(h.engines)), reverse=True)
    per_host: dict[str, int] = {}
    out: list[SearchHit] = []
    for h in ranked:
        host = urlsplit(h.canonical_url).netloc
        if per_host.get(host, 0) >= per_host_limit:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        out.append(h)
    return out
