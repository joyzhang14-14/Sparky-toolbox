"""研究会话内部工具的参数契约（规格第 5 节）。

这里只定义每个工具的参数 schema 与说明；handler 由 readers/providers 在后续步骤注册。
研究模型只能传 URL 或受控 artifact ID，不允许自选本地路径、命令、模块路径、代理配置或任意 JavaScript。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from owa_research.core import image_engines

MAX_QUERY_CHARS = 400
MAX_URL_CHARS = 2000
MAX_GOAL_CHARS = 2000
MAX_PASSAGES = 200
MAX_PASSAGE_CHARS = 4000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchWebParams(Strict):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    categories: list[str] | None = Field(default=None, max_length=10)
    engines: list[str] | None = Field(default=None, max_length=20)
    language: str | None = Field(default=None, max_length=16)
    pageno: int = Field(default=1, ge=1, le=10, description="SearXNG 没有 limit，要更多结果用 pageno 翻页")
    time_range: str | None = Field(default=None, pattern=r"^(day|month|year)$")
    min_score: float | None = Field(default=None, ge=0)
    max_results: int = Field(default=20, ge=1, le=50, description="客户端截断")


class FetchPageParams(Strict):
    url: str = Field(max_length=MAX_URL_CHARS)
    start_index: int = Field(default=0, ge=0, description="分段读取起点；截断处附'从第 N 字符继续'提示")
    max_chars: int | None = Field(default=None, ge=500, description="缺省给整页（这一轮放得下就一次给全；放不下在截断处写明从哪继续）")


class ReadOutlineParams(Strict):
    """只返回标题/章节/表格清单，不返回正文。url 与 artifact_id 二选一。"""

    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    artifact_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _one_of(self) -> "ReadOutlineParams":
        if bool(self.url) == bool(self.artifact_id):
            raise ValueError("url 与 artifact_id 必须且只能给一个")
        return self


class ReadSectionParams(Strict):
    artifact_id: str = Field(max_length=128)
    section_id: str | None = Field(default=None, max_length=128, description="read_outline 给出的章节 ID")
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=500, description="缺省读到章节 / 范围结尾（这一轮放得下的话）")

    @model_validator(mode="after")
    def _one_of(self) -> "ReadSectionParams":
        if self.section_id is None and self.char_start is None:
            raise ValueError("section_id 或 char_start/char_end 至少给一个")
        return self


class BrowseAction(StrEnum):
    OPEN = "open"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    GOTO = "goto"
    FIND = "find"
    FIND_NEXT = "find_next"


class BrowseDocumentParams(Strict):
    """分页文本浏览器：8K 字符一屏，报"第 X 页共 Y 页"。"""

    artifact_id: str = Field(max_length=128)
    action: BrowseAction = BrowseAction.OPEN
    page: int | None = Field(default=None, ge=1, description="goto 时目标屏号")
    query: str | None = Field(default=None, max_length=200, description="find 时的查找词")


class RenderAction(StrEnum):
    NAVIGATE = "navigate"
    READ_DOM = "read_dom"
    SCROLL = "scroll"


class RenderPageParams(Strict):
    """JS 页面渲染、读取 DOM；只读导航和滚动，不接受任意脚本。"""

    url: str = Field(max_length=MAX_URL_CHARS)
    action: RenderAction = RenderAction.NAVIGATE
    wait_for_selector: str | None = Field(default=None, max_length=200)
    scroll_to_bottom: bool = False
    max_chars: int | None = Field(default=None, ge=500, description="缺省给整页（这一轮放得下就一次给全）")


class ReadDocumentParams(Strict):
    """安全下载后按 MIME/文件签名解析 PDF/Office/图片。url 与 artifact_id 二选一。"""

    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    artifact_id: str | None = Field(default=None, max_length=128)
    pages: list[int] | None = Field(default=None, max_length=100, description="只解析这些页（1 起）")
    extract_tables: bool = True
    vision_pages: list[int] | None = Field(default=None, max_length=20, description="强制用视觉读取这些页的页图（文字层缺失/OCR 错乱时用；串行、每页一次模型调用）")

    @model_validator(mode="after")
    def _one_of(self) -> "ReadDocumentParams":
        if bool(self.url) == bool(self.artifact_id):
            raise ValueError("url 与 artifact_id 必须且只能给一个")
        return self


class ExtractFromParams(Strict):
    """目标条件抽取：对一段内容按当前研究目标返回 {理由, 证据片段, 摘要}。"""

    artifact_id: str = Field(max_length=128)
    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    section_id: str | None = Field(default=None, max_length=128)
    page: int | None = Field(default=None, ge=1)


class ReadEvidenceParams(Strict):
    artifact_id: str = Field(max_length=128)
    pages: list[int] | None = Field(default=None, max_length=100)
    sections: list[str] | None = Field(default=None, max_length=100)
    tables: list[int] | None = Field(default=None, max_length=100)
    cells: list[str] | None = Field(default=None, max_length=100)


class Passage(Strict):
    id: str = Field(max_length=128)
    text: str = Field(max_length=MAX_PASSAGE_CHARS)


class RankPassagesParams(Strict):
    question: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    passages: list[Passage] = Field(min_length=1, max_length=MAX_PASSAGES)
    top_k: int = Field(default=10, ge=1, le=MAX_PASSAGES)


class SecAction(StrEnum):
    FILINGS = "filings"
    CONCEPT = "concept"


class SecFilingsParams(Strict):
    """SEC EDGAR 专用查询：按 ticker/CIK 列申报文件（原文 URL）或取 XBRL 财务概念值。"""

    company: str = Field(min_length=1, max_length=20, description="ticker（如 NVDA）或 CIK 数字")
    action: SecAction = SecAction.FILINGS
    forms: list[str] | None = Field(default=None, max_length=10, description="filings：表单类型过滤，如 ['10-K','10-Q']")
    concept: str | None = Field(default=None, max_length=80, description="concept：revenue/net_income/operating_income/gross_profit/rnd/total_assets/eps_diluted/cash 或 us-gaap 标签")
    form: str | None = Field(default="10-K", max_length=10, description="concept：只取该表单的值")
    fiscal_years: list[int] | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _concept_required(self) -> "SecFilingsParams":
        if self.action == SecAction.CONCEPT and not self.concept:
            raise ValueError("action=concept 需要 concept")
        return self


class SearchImagesParams(Strict):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    pageno: int = Field(default=1, ge=1, le=5)
    max_results: int = Field(default=20, ge=1, le=40)


class PageImagesParams(Strict):
    """已读网页上的图：artifact_id 是 fetch_page / render_page 返回的那个。"""

    artifact_id: str = Field(max_length=128)
    max_results: int = Field(default=20, ge=1, le=40)


class ViewImagesParams(Strict):
    ids: list[str] = Field(min_length=1, max_length=8, description="search_images / page_images / reverse_image_search 给的候选 id")
    need: str = Field(min_length=1, max_length=MAX_GOAL_CHARS, description="要什么样的图（内容、构图、风格、清晰度）")


class InspectImageParams(Strict):
    id: str = Field(pattern=r"^u[1-9]$", description="调用方给的图（u1、u2…）")
    question: str = Field(min_length=1, max_length=MAX_GOAL_CHARS, description="要从图里看出什么（人物是谁、物品、地点、图里的文字…）")


def _image_engines() -> tuple[dict[str, image_engines.Engine], str | None]:
    """以图搜图服务插件（subagents/isolated-web-search/image_engines/）：参数的可选值和工具说明从这里生成；插件不合约定时说明里写原因。"""
    try:
        return image_engines.scan(), None
    except image_engines.EngineError as e:
        return {}, str(e)


IMAGE_ENGINES, IMAGE_ENGINES_ERROR = _image_engines()


class ReverseImageSearchParams(Strict):
    id: str = Field(pattern=r"^u[1-9]$", description="调用方给的图（u1、u2…）")
    engine: str | None = Field(default=None, pattern=image_engines.NAME_RE.pattern,
                               description=f"服务：{'|'.join(IMAGE_ENGINES) or '（没有）'}，不写用第一个可用的；各自擅长什么见工具说明")


def _reverse_description() -> str:
    if not IMAGE_ENGINES:
        return f"以图搜图：现在没有可用的服务（{(IMAGE_ENGINES_ERROR or '插件文件夹是空的')[:200]}）；用 inspect_image 看图再按内容搜"
    return ("以图搜图：找调用方的图的出处，返回含这张图的网站（sites：链接、标题，有的带相似度、画师）和识别标签（tags）。engine 选服务（"
            + "；".join(f"engine={n}：{e.description}" for n, e in IMAGE_ENGINES.items())
            + "；不写 engine 用排在前面、这次可用的那个）。按各服务擅长的图挑，一个没结果再换另一个；都搜不到就用 inspect_image 看图再按内容搜")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    params: type[BaseModel]
    description: str


TOOL_SPECS: dict[str, ToolSpec] = {
    s.name: s
    for s in [
        ToolSpec("search_web", SearchWebParams, "SearXNG 搜索，返回按 score 排序的片段（标题、摘要、URL、引擎、日期、分数）"),
        ToolSpec("fetch_page", FetchPageParams, "普通 HTTP 读取 HTML/JSON/XML/text，缺省一次给整页；一轮放不下的系统分段读完、交回逐段总结（要某处原文用 read_section）"),
        ToolSpec("read_outline", ReadOutlineParams, "只返回页面或文档的标题/章节/表格清单，不返回正文"),
        ToolSpec("read_section", ReadSectionParams, "按 read_outline 的章节 ID 或字符范围读取内容"),
        ToolSpec("browse_document", BrowseDocumentParams, "分页文本浏览器：翻页、跳页、页内查找"),
        ToolSpec("render_page", RenderPageParams, "浏览器渲染 JS 页面并读取 DOM 文本；只读导航和滚动"),
        ToolSpec("read_document", ReadDocumentParams, "下载并解析 PDF/Office/图片/网页，返回大纲和正文（缺省给全文；一轮放不下的系统分段读完、交回逐段总结）"),
        ToolSpec("extract_from", ExtractFromParams, "对指定内容按研究目标做目标条件抽取"),
        ToolSpec("read_evidence", ReadEvidenceParams, "从证据存储按 artifact ID、页码、单元格范围补取内容；tables=[全局表格索引] 取整张表的全部行"),
        ToolSpec("rank_passages", RankPassagesParams, "按问题相关度给片段排序，返回分数与定位"),
        ToolSpec("sec_filings", SecFilingsParams, "SEC EDGAR 专用查询：列公司申报文件（含原文 URL）或取 XBRL 财务概念值（美股公司）"),
        ToolSpec("search_images", SearchImagesParams, "图片搜索：返回候选图（id、图片地址、所在网页、标题、分辨率），用 view_images 看"),
        ToolSpec("page_images", PageImagesParams, "列出已读网页上的大图（og:image、正文图片）作为候选，用 view_images 看"),
        ToolSpec("view_images", ViewImagesParams, "下载并查看候选图（一次最多 8 张），按 need 给每张打分描述；过了规则的图（相关、图里没有大段或可疑文字）自动进结果的 images"),
        ToolSpec("inspect_image", InspectImageParams, "看调用方给的图（u1…），回答关于图的问题：画的是谁 / 什么、有什么特征、图里写了什么，用来决定怎么搜"),
        ToolSpec("reverse_image_search", ReverseImageSearchParams, _reverse_description()),
    ]
}

# 图片动作：只在请求要图（images > 0）或给了图（input_images）时注册并出现在研究模型的工具清单里
IMAGE_TOOLS: frozenset[str] = frozenset({"search_images", "page_images", "view_images", "inspect_image", "reverse_image_search"})

# 研究模型可用工具的 allowlist；与 TOOL_SPECS 的 key 一致，单独列出是为了在 isolation 层做显式检查
RESEARCH_TOOL_ALLOWLIST: frozenset[str] = frozenset(TOOL_SPECS)


def compact_params(spec: ToolSpec) -> str:
    """给研究模型看的紧凑参数说明：name: type (required) — description。"""
    schema = spec.params.model_json_schema()
    required = set(schema.get("required", []))
    parts = []
    for name, prop in schema.get("properties", {}).items():
        typ = prop.get("type") or ("/".join(str(a.get("type", "?")) for a in prop.get("anyOf", [])) if prop.get("anyOf") else ("enum" if "$ref" in prop or "allOf" in prop else "any"))
        if "enum" in prop:
            typ = "|".join(map(str, prop["enum"]))
        if "$ref" in prop:
            enum = schema.get("$defs", {}).get(prop["$ref"].rsplit("/", 1)[-1], {}).get("enum")
            if enum:
                typ = "|".join(map(str, enum))
        for ref in prop.get("allOf", []):
            if "$ref" in ref:
                enum = schema.get("$defs", {}).get(ref["$ref"].rsplit("/", 1)[-1], {}).get("enum")
                if enum:
                    typ = "|".join(map(str, enum))
        desc = prop.get("description", "")
        parts.append(f"{name}: {typ}{' (required)' if name in required else ''}{' — ' + desc[:80] if desc else ''}")
    return "; ".join(parts)


def export_tool_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in TOOL_SPECS.values():
        path = out_dir / f"tool_{spec.name}.json"
        schema = spec.params.model_json_schema()
        schema["description"] = spec.description
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in export_tool_schemas(Path(sys.argv[1] if len(sys.argv) > 1 else "services/owa-research/owa_research/schemas")):
        print(p)
