"""OWA_* 环境变量配置（规格第 9 节）。这些是待测默认值，不是性能保证。"""
from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_base_url: str = "http://host.docker.internal:8000/v1"
    model_id: str = "qwen"
    model_api_key: str = "local"
    model_timeout_seconds: float = 600
    model_concurrency: int = 1
    searxng_base_url: str = "http://searxng:8080"
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-small-en-v1.5"
    table_model: str = ""
    parallel_actions: bool = False  # 一轮允许多个互不依赖的工具调用并行执行
    evidence_extract: bool = True  # 部署默认：是否提供 extract_from（逐页目标抽取→证据账本→逐字引文定位）。请求里 evidence_extract 可覆盖；关掉更快、定位退化为段落级
    merged_extract: bool = False  # 读取类工具带 extract_goal 时读完直接抽取。实测模型从不使用该字段，默认关（docs/merged-extract-ab.md）
    planning_thinking: str = "first"  # 规划类调用思维链：on / off / first（子查询规划与第 1 轮开，后续关）/ event（first + 放下、门禁、拦截的下一轮开，D20 A3-e）。A/B：first 耗时 −63%、8/8 持平、来源 −30%（分析文档 13.2）；穷尽来源的任务设 on
    subquery_mode: str = "auto"  # auto：先用一次不带思维链的短调用判断是否单跳事实题，是则直接用一条查询（12 次 0 误判，分析文档 13.1）；always：总是拆子查询
    source_tiers: bool = False  # 来源可信度分级（services/owa-research/owa_research/core/tiers.py）：候选排序加权 + 提示里标注 tier + 收尾偏向高可信来源
    incremental_report: bool = True  # 轮次只输出报告增量，服务端拼接（关掉则每轮重输全文）
    question_tracking: bool = False  # 请求带 extract 时逐题跟踪（D20 A 层）：状态由代码从工具参数与结果推断（已答要在原文里核过、卡住先放下、
    # 收尾前门禁与扫描），模型必填的 questions 只作线索。默认关，E2 A/B 后再定（旧版 A/B 见 docs/question-tracking-ab.md）
    fixture_urls: bool = False  # 只给 bench：https://fixtures.invalid/<文件名> 读 launcher 只读挂载的 /fixtures（D20 实验样本，生产请求不带）
    subquery_default: int = 4
    read_top_n_default: int = 6
    browser_concurrency: int = 1
    parse_concurrency: int = 1
    http_concurrency: int = 2
    research_timeout_seconds: int = 900
    output_char_budget: int = 24000
    artifact_ttl_seconds: int = 3600
    artifact_dir: str = "/artifacts"
    # 可信配置给任务 budget 的上限（规格 4：budget 由可信配置限制上限）
    max_searches_cap: int = 30
    max_pages_cap: int = 40
    max_documents_cap: int = 10
    max_seconds_cap: int = 3600
    max_output_chars_cap: int = 60000
    max_model_tokens_cap: int = 200_000  # 输出（completion）token 上限；约 1.8 小时解码，实际由 max_seconds_cap 主导
    # 抓取边界（规格 9）
    http_timeout_seconds: float = 60
    max_download_bytes: int = 10 * 1024 * 1024
    max_document_bytes: int = 50 * 1024 * 1024
    max_page_chars: int = 50_000_000
    page_cache_seconds: int = 900
    page_cache_bytes: int = 50 * 1024 * 1024
    max_url_length: int = 2000
    max_redirects: int = 10
    user_agent: str = Field(
        default="open-web-access-research-bot/0.1 (automated research tool)"   # 带联系邮箱的完整 UA 在仓库根目录 .env 的 OWA_USER_AGENT（SEC 要求带联系方式）
    )


_ENV_MAP = {
    "OWA_MODEL_BASE_URL": "model_base_url",
    "OWA_MODEL_ID": "model_id",
    "OWA_MODEL_API_KEY": "model_api_key",
    "OWA_MODEL_TIMEOUT_SECONDS": "model_timeout_seconds",
    "OWA_MODEL_CONCURRENCY": "model_concurrency",
    "SEARXNG_BASE_URL": "searxng_base_url",
    "OWA_RERANK_ENABLED": "rerank_enabled",
    "OWA_RERANK_MODEL": "rerank_model",
    "OWA_TABLE_MODEL": "table_model",
    "OWA_PARALLEL_ACTIONS": "parallel_actions",
    "OWA_EVIDENCE_EXTRACT": "evidence_extract",
    "OWA_MERGED_EXTRACT": "merged_extract",
    "OWA_PLANNING_THINKING": "planning_thinking",
    "OWA_SUBQUERY_MODE": "subquery_mode",
    "OWA_SOURCE_TIERS": "source_tiers",
    "OWA_INCREMENTAL_REPORT": "incremental_report",
    "OWA_QUESTION_TRACKING": "question_tracking",
    "OWA_FIXTURE_URLS": "fixture_urls",
    "OWA_SUBQUERY_DEFAULT": "subquery_default",
    "OWA_READ_TOP_N_DEFAULT": "read_top_n_default",
    "OWA_BROWSER_CONCURRENCY": "browser_concurrency",
    "OWA_PARSE_CONCURRENCY": "parse_concurrency",
    "OWA_HTTP_CONCURRENCY": "http_concurrency",
    "OWA_RESEARCH_TIMEOUT_SECONDS": "research_timeout_seconds",
    "OWA_OUTPUT_CHAR_BUDGET": "output_char_budget",
    "OWA_ARTIFACT_TTL_SECONDS": "artifact_ttl_seconds",
    "OWA_ARTIFACT_DIR": "artifact_dir",
    "OWA_HTTP_TIMEOUT_SECONDS": "http_timeout_seconds",
    "OWA_MAX_DOWNLOAD_BYTES": "max_download_bytes",
    "OWA_MAX_DOCUMENT_BYTES": "max_document_bytes",
    "OWA_MAX_PAGE_CHARS": "max_page_chars",
    "OWA_USER_AGENT": "user_agent",
}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    values: dict[str, object] = {}
    for key, field in _ENV_MAP.items():
        if key in env:
            raw = env[key]
            if field == "planning_thinking":
                values[field] = raw.lower() if raw.lower() in ("first", "event") else ("on" if raw.lower() in ("1", "true", "yes", "on") else "off")
            elif field == "subquery_mode":
                values[field] = "auto" if raw.lower() == "auto" else "always"
            else:
                values[field] = raw.lower() in ("1", "true", "yes", "on") if field in ("rerank_enabled", "incremental_report", "merged_extract", "parallel_actions", "evidence_extract", "source_tiers", "question_tracking", "fixture_urls") else raw
    return Settings(**values)
