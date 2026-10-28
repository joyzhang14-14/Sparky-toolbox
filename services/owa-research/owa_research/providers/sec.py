"""SEC EDGAR adapter（规格 7）：按 CIK/ticker、表单、期间查 submissions、XBRL companyconcept，并给出原始申报文件 URL。

公开接口无需 API key；聚合字段不替代完整原文件，原文件由 read_document 读取。
所有请求走 HttpFetcher（出站策略、限流），SEC 要求 UA 带联系方式（Settings.user_agent 已带）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.readers.http import HttpFetcher

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{concept}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"

# 常用财务概念的 us-gaap 标签候选（公司可能用不同标签，按顺序尝试）
CONCEPT_ALIASES: dict[str, list[str]] = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "total_assets": ["Assets"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
}


class SecAdapter:
    name = "sec-edgar"

    def __init__(self, fetcher: HttpFetcher) -> None:
        self._f = fetcher
        self._tickers: dict[str, int] | None = None
        self._titles: list[tuple[str, int]] = []

    async def _json(self, url: str) -> Any:
        r = await self._f.fetch(url)  # 速率由每域名限流控制
        try:
            return json.loads(r.content)
        except json.JSONDecodeError:
            raise ToolError(ErrorCode.EXTRACTION_FAILED, "SEC 返回非 JSON", stage="sec") from None

    async def resolve_cik(self, ticker_or_cik: str) -> int:
        """接受 CIK 数字、ticker，或公司名（按 SEC 公司列表标题匹配：完全相等 → 前缀 → 包含，取 SEC 列表顺序最靠前者）。"""
        t = ticker_or_cik.strip().upper()
        if t.isdigit():
            return int(t)
        if self._tickers is None:
            data = await self._json(TICKERS_URL)
            self._tickers = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
            self._titles = [(v["title"].upper(), int(v["cik_str"])) for v in data.values()]
        if t in self._tickers:
            return self._tickers[t]
        name = re.sub(r"[.,]|\b(INC|CORP|CORPORATION|CO|LTD|PLC|HOLDINGS?)\b", "", t).strip()
        for match in (lambda title: title == t, lambda title: title.startswith(name + " ") or title == name, lambda title: name in title):
            hit = next((cik for title, cik in self._titles if match(title)), None)
            if hit is not None:
                return hit
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"未知 ticker/公司名: {ticker_or_cik}（用 ticker 如 NVDA、AAPL，或 CIK）", stage="sec")

    async def filings(self, ticker_or_cik: str, *, forms: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
        cik = await self.resolve_cik(ticker_or_cik)
        sub = await self._json(SUBMISSIONS_URL.format(cik=cik))
        recent = sub.get("filings", {}).get("recent", {})
        wanted = {f.upper() for f in forms} if forms else None
        out = []
        for form, date_, acc, doc, desc, period in zip(recent.get("form", []), recent.get("filingDate", []), recent.get("accessionNumber", []),
                                                       recent.get("primaryDocument", []), recent.get("primaryDocDescription", []), recent.get("reportDate", [])):
            if wanted and form.upper() not in wanted:
                continue
            out.append({"form": form, "filing_date": date_, "report_period": period or None, "accession": acc,
                        "primary_document_url": ARCHIVE_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), doc=doc), "description": desc or None})
            if len(out) >= limit:
                break
        return {"cik": cik, "name": sub.get("name"), "fiscal_year_end": sub.get("fiscalYearEnd"), "sic": sub.get("sicDescription"), "filings": out,
                "note": "primary_document_url 是原始申报文件（多为 HTML），用 read_document/fetch_page 读取原文"}

    async def concept(self, ticker_or_cik: str, concept: str, *, form: str | None = "10-K", fiscal_years: list[int] | None = None, limit: int = 12) -> dict[str, Any]:
        """合并全部候选标签的事实：公司会换标签（如 Apple 2019 起把 Revenues 换成 RevenueFromContractWithCustomer…），
        只试第一个有数据的标签会拿到过期年份。"""
        cik = await self.resolve_cik(ticker_or_cik)
        tags = CONCEPT_ALIASES.get(concept.lower(), [concept])
        facts: list[dict[str, Any]] = []
        used: list[str] = []
        entity = label = None
        last_err: ToolError | None = None
        for tag in tags:
            try:
                data = await self._json(CONCEPT_URL.format(cik=cik, taxonomy="us-gaap", concept=tag))
            except ToolError as e:
                last_err = e
                continue
            entity = entity or data.get("entityName")
            label = label or data.get("label")
            found = 0
            for unit, rows in (data.get("units") or {}).items():
                for r in rows:
                    if form and (r.get("form") or "").upper() != form.upper():
                        continue
                    if fiscal_years and r.get("fy") not in fiscal_years:
                        continue
                    facts.append({"fy": r.get("fy"), "fp": r.get("fp"), "start": r.get("start"), "end": r.get("end"), "value": r.get("val"), "unit": unit,
                                  "form": r.get("form"), "filed": r.get("filed"), "accession": r.get("accn"), "frame": r.get("frame"), "tag": tag})
                    found += 1
            if found:
                used.append(tag)
        if not facts:
            raise last_err or ToolError(ErrorCode.EXTRACTION_FAILED, f"概念 {concept} 在 {form or '全部表单'} 中无数据（可换 form 或直接读原文件）", stage="sec")
        # 去重（同期间同值可能出现在多份申报）并按期末倒序
        seen: set[tuple] = set()
        deduped = []
        for f in sorted(facts, key=lambda x: (x["end"] or "", x["filed"] or ""), reverse=True):
            key = (f["start"], f["end"], f["value"], f["unit"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(f)
        return {"cik": cik, "entity": entity, "concept": concept, "tags_used": used, "label": label, "facts": deduped[:limit],
                "note": "facts 已合并多个 us-gaap 标签（见 tag 字段）并按期末倒序；同一期间可能有修订版本，以 accession 对应的原文件为准，XBRL 聚合值不替代原文"}
