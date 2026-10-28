"""研究编排入口：预算、超时、取消，装配会话/工具/重排，跑研究循环，组装结果。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from dataclasses import dataclass

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.loop import ResearchLoop
from owa_research.core.rerank import EmbeddingBackend, Reranker, get_backend
from owa_research.core.runtime import Runtime
from owa_research.core.schema import (
    Budget, CacheStats, Claim, Disagreement, ResearchRequest, ResearchResult, ResearchStatus, Source, Table, TokenUsage, Usage,
)
from owa_research.core.schema import MAX_SUMMARY_CHARS, Answer, Claim, ClaimKind
from owa_research.core.tools import MAX_QUERY_CHARS
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted, register_tools
from owa_research.core.images import register_image_tools


@dataclass(frozen=True)
class EffectiveBudget:
    max_searches: int
    max_pages: int
    max_documents: int
    max_model_tokens: int
    max_seconds: int
    max_output_chars: int

    @classmethod
    def resolve(cls, budget: Budget | None, rt: Runtime) -> "EffectiveBudget":
        s = rt.settings
        b = budget or Budget()

        def pick(value: int | None, default: int, cap: int) -> int:
            return min(value if value is not None else default, cap)

        return cls(
            max_searches=pick(b.max_searches, 8, s.max_searches_cap),
            max_pages=pick(b.max_pages, s.read_top_n_default * 2, s.max_pages_cap),
            max_documents=pick(b.max_documents, 4, s.max_documents_cap),
            max_model_tokens=pick(b.max_model_tokens, 30_000, s.max_model_tokens_cap),  # 默认 3 万输出 token ≈ 16 分钟解码
            max_seconds=pick(b.max_seconds, s.research_timeout_seconds, s.max_seconds_cap),
            max_output_chars=pick(b.max_output_chars, s.output_char_budget, s.max_output_chars_cap),
        )


def make_reranker(rt: Runtime) -> Reranker:
    backend: EmbeddingBackend | None = None
    if rt.settings.rerank_enabled and rt.settings.rerank_model:
        backend = get_backend(rt.settings.rerank_model)
    return Reranker(backend=backend, model=rt.model)


async def run_research(request: ResearchRequest, rt: Runtime) -> ResearchResult:
    budget = EffectiveBudget.resolve(request.budget, rt)
    session = ResearchSession(rt, strict_egress=bool(request.strict_egress), merge_tables=request.merge_tables and bool(request.extract))
    session.allow_urls(str(u) for u in request.seed_urls or [])
    counters = Budgeted(max_searches=budget.max_searches, max_pages=budget.max_pages, max_documents=budget.max_documents)
    reranker = make_reranker(rt)
    session.context_window = request.context_window
    session.digest_goal = request.task + ("\nQuestions:\n" + "\n".join(f"- {q}" for q in request.extract) if request.extract else "")
    register_tools(session, reranker, counters, goal=request.task)
    register_image_tools(session, request, counters)   # 要图 / 给了图时才注册图片动作（core/images.py）
    if request.probe_image_engine:
        return await _probe_image_engine(request, rt, session, budget)
    if request.search_only:
        return await _quick_search(request, rt, session, budget)
    if request.tools is not None:
        rt.dispatcher.restrict(request.tools)
    loop = ResearchLoop(request, session, reranker, counters, max_model_tokens=budget.max_model_tokens,
                        deadline=rt.started_at + budget.max_seconds, subquery_default=rt.settings.subquery_default, read_top_n=rt.settings.read_top_n_default,
                        incremental_report=rt.settings.incremental_report, planning_thinking=rt.settings.planning_thinking, merged_extract=rt.settings.merged_extract, parallel_actions=rt.settings.parallel_actions,
                        evidence_extract=request.evidence_extract if request.evidence_extract is not None else rt.settings.evidence_extract,
                        subquery_mode=rt.settings.subquery_mode, source_tiers=rt.settings.source_tiers,
                        question_tracking=request.question_tracking if request.question_tracking is not None else rt.settings.question_tracking)
    try:
        return await asyncio.wait_for(_research(request, rt, session, loop, counters, budget), timeout=budget.max_seconds)
    except ToolError as e:
        status = ResearchStatus.CANCELLED if e.code == ErrorCode.CANCELLED else ResearchStatus.BLOCKED
        return _finish(rt, session, status, f"研究未完成：{e.info.code}: {e.info.message}\n\n{(loop.state.report or '')[:8000]}",
                       exhausted=counters.exhausted, max_chars=budget.max_output_chars, **_unanswered(request))
    except asyncio.TimeoutError:
        # 总超时打断了收尾：报告本身就是账本，机械拆回 claims，保证 partial 结果仍可结构化消费
        (rt.evidence.root / "report.md").write_text(loop.state.report or "")
        claims = [Claim(text=c["text"], kind=ClaimKind.SOURCE_STATEMENT, source_ids=c["source_ids"]) for c in loop._claims_from_report()]
        return _finish(rt, session, ResearchStatus.PARTIAL, (loop.state.report or "")[:8000] + f"\n\n[研究在 {budget.max_seconds} 秒预算内未完成]",
                       claims=claims, exhausted=counters.exhausted + ["max_seconds"], max_chars=budget.max_output_chars, **_unanswered(request))
    except asyncio.CancelledError:
        # 外部取消（SIGTERM，如 sparky-toolbox 的暂停）：演进报告就是检查点，拆回 claims 作为 cancelled 结果交回，证据目录保留
        (rt.evidence.root / "report.md").write_text(loop.state.report or "")
        claims = [Claim(text=c["text"], kind=ClaimKind.SOURCE_STATEMENT, source_ids=c["source_ids"]) for c in loop._claims_from_report()]
        return _finish(rt, session, ResearchStatus.CANCELLED, (loop.state.report or "")[:8000] + "\n\n[研究被取消，以上为取消前的阶段性报告]",
                       claims=claims, exhausted=counters.exhausted, max_chars=budget.max_output_chars, **_unanswered(request))


async def _research(request: ResearchRequest, rt: Runtime, session: ResearchSession, loop: ResearchLoop, counters: Budgeted, budget: EffectiveBudget) -> ResearchResult:
    with rt.timer.track("model_verify"):
        await rt.model.verify()
    # evidence_request：二次补取，不重新搜索
    if request.evidence_request:
        for er in request.evidence_request:
            try:
                session.doc_for(er.artifact_id)
                res = await rt.dispatcher.call("read_evidence", {"artifact_id": er.artifact_id, "pages": er.pages, "sections": er.sections, "tables": er.tables, "cells": er.cells})
                loop.state.last_result = (loop.state.last_result + "\n" if loop.state.last_result != "(none)" else "") + f"[read_evidence {er.artifact_id}]\n" + str(res)[:12000]
            except ToolError as e:
                loop.state.last_result += f"\n[read_evidence {er.artifact_id} 失败] {e.info.code}: {e.info.message}"
        loop.state.report = "(evidence_request 补取模式：优先使用上面的证据)"
    elif request.seeds_only and request.seed_urls:
        loop.trace.log("seeds_only", n=len(request.seed_urls))
        await loop.open_seed_urls(limit=8)
    else:
        try:
            await loop.plan_subqueries()
        except ToolError as e:
            if e.code == ErrorCode.CANCELLED:
                raise
            loop.trace.log("subqueries_failed", code=e.info.code, message=e.info.message[:200])   # 规划失败就用任务原文搜，但要留痕
            loop.state.subqueries = []
        await loop.initial_search()
        await loop.open_seed_urls()
    await loop.run_rounds()
    obj = await loop.finalize()
    claims, tables, disagreements, missing, status, summary = await loop.build_result_parts(obj)
    answers, unanswered = loop.build_answers(obj)
    loop.align_claims(claims, answers)
    if loop.state.forced_partial:  # 预算打断才降级；收敛收尾由模型自报 complete/partial
        status = ResearchStatus.PARTIAL
    return _finish(rt, session, status, summary, claims=claims, tables=tables, disagreements=disagreements, missing=(missing + unanswered)[:50], answers=answers,
                   exhausted=counters.exhausted + (["model_tokens_or_time"] if loop.state.forced_partial else []),
                   max_chars=budget.max_output_chars, converged=loop.state.converged)


QUICK_RESULTS = 20


async def _quick_search(request: ResearchRequest, rt: Runtime, session: ResearchSession, budget: EffectiveBudget) -> ResearchResult:
    """快搜（search_only）：不起研究循环、不调模型，用 task 调一次 search_web。summary 是 JSON {results: [{title, url, snippet, date}]}，
    放不进 summary 上限时从后往前去掉结果；搜索失败照常交 blocked 和原因。"""
    try:
        r = await asyncio.wait_for(rt.dispatcher.call("search_web", {"query": request.task[:MAX_QUERY_CHARS], "max_results": QUICK_RESULTS}), timeout=budget.max_seconds)
    except ToolError as e:
        return _finish(rt, session, ResearchStatus.BLOCKED, f"快搜没有结果：{e.info.code}: {e.info.message}", exhausted=[], max_chars=budget.max_output_chars)
    except asyncio.TimeoutError:
        return _finish(rt, session, ResearchStatus.BLOCKED, f"快搜超过 {budget.max_seconds} 秒", exhausted=["max_seconds"], max_chars=budget.max_output_chars)
    rows = [{"title": h.get("title"), "url": h.get("url"), "snippet": h.get("snippet"), "date": h.get("date")} for h in r.get("results") or []]
    while rows and len(json.dumps({"results": rows}, ensure_ascii=False)) > MAX_SUMMARY_CHARS - 200:
        rows.pop()
    return _finish(rt, session, ResearchStatus.COMPLETE, json.dumps({"results": rows}, ensure_ascii=False), exhausted=[], max_chars=budget.max_output_chars)


async def _probe_image_engine(request: ResearchRequest, rt: Runtime, session: ResearchSession, budget: EffectiveBudget) -> ResearchResult:
    """试跑一个以图搜图服务（probe_image_engine，builder 部署插件时经 launcher 调）：不起研究循环、不调模型，用 u1 调一次 reverse_image_search。
    summary 是 JSON：engines（镜像里加载到的服务）、result / error、requested（插件请求过的网址，宿主核对只连了声明的）。"""
    st = session.images
    out: dict[str, Any] = {"engines": sorted(st.engines), "load_error": st.engines_error, "engine": request.probe_image_engine, "result": None, "error": None}
    try:
        out["result"] = await asyncio.wait_for(rt.dispatcher.call("reverse_image_search", {"id": "u1", "engine": request.probe_image_engine}), timeout=budget.max_seconds)
    except ToolError as e:
        out["error"] = e.info.model_dump(mode="json")
    except asyncio.TimeoutError:
        out["error"] = {"code": ErrorCode.BUDGET_EXCEEDED, "message": f"超过 {budget.max_seconds} 秒"}
    out["requested"] = st.requested
    return _finish(rt, session, ResearchStatus.COMPLETE, json.dumps(out, ensure_ascii=False, default=str), exhausted=[], max_chars=budget.max_output_chars)


def _unanswered(request: ResearchRequest) -> dict[str, Any]:
    """研究没走到收尾（出错、超时、取消）：extract 的每个问题仍给一条，answer 为 null。"""
    qs = list(request.extract or [])
    return {"answers": [Answer(question=q) for q in qs], "missing": [f"未作答（研究未走到收尾）：{q}"[:500] for q in qs]}


def failed_result(rt: Runtime, reason: str) -> ResearchResult:
    return _finish(rt, ResearchSession(rt), ResearchStatus.BLOCKED, f"研究未完成：内部错误 {reason}", exhausted=[], max_chars=rt.settings.output_char_budget)


def cancelled_result(rt: Runtime) -> ResearchResult:
    return _finish(rt, ResearchSession(rt), ResearchStatus.CANCELLED, "研究已取消（收到终止信号）", exhausted=[], max_chars=rt.settings.output_char_budget)


def _finish(rt: Runtime, session: ResearchSession, status: ResearchStatus, summary: str, *, claims: list[Claim] | None = None, tables: list[Table] | None = None,
            disagreements: list[Disagreement] | None = None, missing: list[str] | None = None, exhausted: list[str], max_chars: int, converged: bool = False,
            answers: list[Answer] | None = None) -> ResearchResult:
    m = rt.model.usage
    rt.timer.add("model_total", m.seconds)
    if rt.browser.start_seconds is not None:
        rt.timer.add("browser_start", rt.browser.start_seconds)
    usage = Usage(
        wall_seconds=round(time.monotonic() - rt.started_at, 3),
        stage_seconds={k: round(v, 3) for k, v in rt.timer.seconds.items()},
        tokens=TokenUsage(prompt=m.prompt, completion=m.completion, cached_prompt=m.cached_prompt),
        model_calls=m.calls, search_calls=rt.search.calls,
        pages_fetched=rt.fetcher.pages_fetched + rt.browser.renders, pages_parsed=session.pages_read, ocr_pages=session.ocr_pages,
        cache=CacheStats(page_hits=rt.fetcher.cache.hits, page_misses=rt.fetcher.cache.misses, prefix_cache_hit_tokens=None),
        budget_exhausted=sorted(set(exhausted)) + (["converged"] if converged else []),
    )
    result = ResearchResult(status=status, summary=summary[:12000], claims=claims or [], tables=tables or [], sources=list(session.sources.values()),
                            disagreements=disagreements or [], missing_information=missing or [], access_failures=session.failures[:100], answers=answers or [],
                            images=session.images.final() if getattr(session, "images", None) else [], usage=usage)
    return _shrink(result, max_chars)


def _shrink(result: ResearchResult, max_chars: int) -> ResearchResult:
    """输出长度上限：先逐步裁表格行，再去掉表格（claims 仍带定位），再裁 claims，最后才裁 summary。"""
    def size() -> int:
        return len(result.model_dump_json())

    if size() <= max_chars:
        return result
    trimmed = []
    rows = 60
    while size() > max_chars and rows >= 5 and result.tables:
        for t in result.tables:
            t.rows = t.rows[:rows]
        rows //= 2
        trimmed.append(f"表格行裁到 {rows * 2}")
    if size() > max_chars and result.tables:
        result.tables = []
        trimmed.append("表格已移除")
    if size() > max_chars and len(result.claims) > 20:
        result.claims = result.claims[:20]
        trimmed.append("claims 裁到 20 条")
    if size() > max_chars:
        keep = max(500, max_chars // 3)
        if len(result.summary) > keep:
            result.summary = result.summary[:keep] + "…[截断]"
            trimmed.append("summary 截断")
    if trimmed:
        result.missing_information.append("结果因输出字符上限被裁剪（" + "；".join(dict.fromkeys(trimmed)) + "）；用 evidence_request 按 artifact_id 补取")
    return result
