"""一轮放不下的文档滚动读（core/digest.py）：按句子边界切段，每段连同前面的总结交给模型；总结拼起来超过上限就压缩一次再接着读；
读取类工具交回逐段总结，研究循环把整份原文算进核对语料；总结用的 token 不算进研究预算。"""
import re
from pathlib import Path

from owa_research.core import digest as dg
from owa_research.core.config import Settings
from owa_research.core.document import ParsedDocument
from owa_research.core.loop import ResearchLoop
from owa_research.core.model_client import ModelResponse
from owa_research.core.rerank import Reranker
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest
from owa_research.core.session import ResearchSession
from owa_research.core.tools_impl import Budgeted, register_tools

PAGE = "".join(f"Rule {i}: the program must handle case number {i}. " for i in range(6000))   # 约 30 万字


def test_cut_at_sentence():
    t = "First sentence. Second one is long"
    assert t[: dg.cut_at_sentence(t, 0, 25)] == "First sentence."
    assert dg.cut_at_sentence("no boundary here at all", 0, 10) == 10           # 没有句子边界：最大长度处切
    assert dg.cut_at_sentence("短句。后面", 0, 100) == len("短句。后面")             # 到结尾
    assert "短句。后面还有"[: dg.cut_at_sentence("短句。后面还有", 0, 5)] == "短句。"


def _fake_model(rt, monkeypatch, summary=lambda user: "S"):
    prompts = []

    async def chat(messages, **kw):
        user = messages[-1]["content"]
        prompts.append((user, kw["max_tokens"]))
        return ModelResponse(text=summary(user), usage={"completion_tokens": 10}, finish_reason="stop", seconds=0.0)

    monkeypatch.setattr(rt.model, "chat", chat)
    return prompts


def _session(rt, cw):
    s = ResearchSession(rt)
    s.context_window, s.digest_goal = cw, "find the rules"
    return s


async def test_digest_reads_every_part(tmp_path: Path, monkeypatch):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        cw = 40_000
        prompts = _fake_model(rt, monkeypatch, lambda user: "summary of " + re.search(r"characters (\d+)–(\d+) \(untrusted\)", user).group(0))
        s = _session(rt, cw)
        d = await dg.digest(rt, s, ParsedDocument(kind="html", title="p", text=PAGE), "https://x.example/p", 100_000)
        ranges = [tuple(map(int, m.groups())) for m in re.finditer(r"\[第 (\d+)–(\d+) 字\]", d["summary"])]
        assert d["parts"] == len(prompts) > 5 and d["compressions"] == 0 and ranges[0][0] == 0 and ranges[-1][1] == len(PAGE)
        assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))                      # 一段接一段，没有漏的
        assert all(PAGE[b - 1] == "." for _, b in ranges[:-1])                             # 都切在句子边界
        assert all(dg.est_tokens(dg.SYSTEM + u) <= cw - mt for u, mt in prompts)           # 每次提示都放得进上下文
        assert "(this is the first part)" in prompts[0][0] and "summary of characters 0–" in prompts[1][0]   # 后面的段带着前面的总结
        assert s.digest_completion == 10 * len(prompts)
    finally:
        await rt.aclose()


async def test_digest_compresses_when_summaries_grow(tmp_path: Path, monkeypatch):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        monkeypatch.setattr(dg, "SUMMARY_LIMIT", 300)
        prompts = _fake_model(rt, monkeypatch, lambda user: "COMPRESSED" if "Compress these summaries" in user else "x" * 120)
        d = await dg.digest(rt, _session(rt, 40_000), ParsedDocument(kind="html", title="p", text=PAGE), "https://x.example/p", 100_000)
        comp = [u for u, mt in prompts if "Compress these summaries" in u]
        assert d["compressions"] == len(comp) >= 2 and all(mt == dg.COMPRESS_MAX_TOKENS for u, mt in prompts if "Compress" in u)
        assert "COMPRESSED" in d["summary"]
        after = prompts[prompts.index(next(p for p in prompts if "Compress these summaries" in p[0])) + 1][0]
        assert "COMPRESSED" in after                                                         # 压缩之后接着读，带着压缩后的总结
    finally:
        await rt.aclose()


async def test_tools_and_loop_use_digest(tmp_path: Path, monkeypatch):
    rt = Runtime.build(Settings(), None, tmp_path)
    try:
        _fake_model(rt, monkeypatch, lambda user: "「Rule 5999: the program must handle case number 5999.」")
        s = _session(rt, 60_000)
        s.docs["docu_1"] = ParsedDocument(kind="html", title="p", text=PAGE)
        s.register_source(url="https://x.example/p", final_url=None, fetched_at="now", artifact_id="docu_1", content_hash=None, content_type="text/html")
        register_tools(s, Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=5, max_documents=5), goal="q")

        async def opened(url, **kw):
            return s.sources[s.source_by_key["docu_1"]], s.docs["docu_1"], "docu_1"

        monkeypatch.setattr(s, "open_url", opened)
        loop = ResearchLoop(ResearchRequest(task="q", seed_urls=["https://x.example/p"], seeds_only=True, context_window=60_000), s,
                            Reranker(backend=None, model=None), Budgeted(max_searches=1, max_pages=5, max_documents=5),
                            max_model_tokens=1000, deadline=rt.started_at + 10_000, subquery_default=1, read_top_n=1)
        await loop.open_seed_urls()
        assert '"digest": true' in loop.state.last_result and "Rule 5999" in loop.state.last_result
        assert "Rule 3000: the program must handle case number 3000." in loop._last_seen   # 核对语料是整份原文
        _fake_model(rt, monkeypatch, lambda user: "MODEL-WRITTEN SUMMARY")
        s.digests.clear()
        await loop.open_seed_urls()
        assert "MODEL-WRITTEN SUMMARY" in loop.state.last_result and "MODEL-WRITTEN SUMMARY" not in loop._last_seen   # 模型写的总结不算原文
        assert s.digest_completion > 0 and loop._fraction_used() < 0.5                       # 总结的 token 不算预算
        r = await rt.dispatcher.call("fetch_page", {"url": "https://x.example/p", "max_chars": 1000})
        assert "digest" not in r and r["char_end"] == 1000                                   # 指定了长度：照旧给原文
    finally:
        await rt.aclose()
