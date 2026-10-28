"""以图搜图服务插件（2026-10-06）：subagents/isolated-web-search/image_engines/<服务名>.py，一个服务一个文件，文件名以 _ 开头的跳过。
研究镜像构建时整个文件夹拷进镜像（/opt/owa/subagents/isolated-web-search/image_engines），reverse_image_search 运行时扫描它。

插件约定（例子见那个文件夹里的 saucenao.py、yandex.py）：
- NAME：服务名，等于文件名去掉 .py（小写字母、数字、下划线，字母开头）
- DESCRIPTION：擅长什么图、限制（原样给研究模型看，用来挑服务）
- KEY：要的凭证键名 STB_<NAME 大写>_…（如 STB_SAUCENAO_API_KEY），不要就 None；宿主按它读 .env，只把这个值交给这个插件
- URLS：要连的网址（https）；研究容器只为它放行这些，http 只能请求它们（同协议、同主机、路径以它开头）
- reverse_image_search 不写 engine 时用「第一个可用的」：按文件名排序，第一个不要 key 或 key 已给的服务
- async def search(image, key, http)：image 有 id（u1…）、data（JPEG 字节）、mime；key 是 KEY 的值（KEY 是 None 时为 None）；
  http 是 EngineHttp（post_form / get / url）。返回 {"sites": [{"url", "title", "similarity"?, …}], "tags": [...], "note"?, "total_sites"?}；
  出错 raise ToolError(ErrorCode.…, 说明, stage="search")（from owa_research.core.errors import ErrorCode, ToolError）。
这里读约定用 ast（不执行插件代码：宿主进程只读不跑）；真正 import 插件只在研究容器里调用 search 时（load_search）。
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

DIR = Path(__file__).resolve().parents[4] / "subagents" / "isolated-web-search" / "image_engines"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
FIELDS = ("NAME", "DESCRIPTION", "KEY", "URLS")
SEARCH_ARGS = ["image", "key", "http"]
MAX_SITES = 20
MAX_TAGS = 20


class EngineError(ValueError):
    """插件不符合约定（缺项、类型不对…）：消息写明文件和哪一项。"""


@dataclass(frozen=True)
class Engine:
    name: str
    description: str
    key: str | None
    urls: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class EngineImage:
    id: str
    data: bytes
    mime: str


def key_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"^STB_{re.escape(name.upper())}_[A-Z0-9_]{{1,60}}$")


def parse_engine(name: str, text: str, path: Path | None = None) -> Engine:
    """一个插件文件的约定（ast 读顶层赋值的字面量和 search 的签名，不执行代码）；不合约定抛 EngineError。"""
    where = str(path or f"{name}.py")
    if not NAME_RE.fullmatch(name):
        raise EngineError(f"{where}：文件名 {name}.py 不行（服务名只用小写字母、数字、下划线，字母开头）")
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise EngineError(f"{where}：语法错（第 {e.lineno} 行）：{e.msg}") from None
    vals: dict[str, Any] = {}
    search = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else []
        for t in targets:
            if isinstance(t, ast.Name) and t.id in FIELDS:
                try:
                    vals[t.id] = ast.literal_eval(node.value)
                except ValueError:
                    raise EngineError(f"{where}：{t.id} 要直接写字面量（第 {node.lineno} 行）") from None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "search":
            search = node
    for f in ("NAME", "DESCRIPTION", "KEY", "URLS"):
        if f not in vals:
            raise EngineError(f"{where}：缺 {f}（约定：NAME、DESCRIPTION、KEY、URLS、async def search(image, key, http)）")
    if vals["NAME"] != name:
        raise EngineError(f"{where}：NAME 是 {vals['NAME']!r}，要和文件名一致：{name!r}")
    if not isinstance(vals["DESCRIPTION"], str) or not vals["DESCRIPTION"].strip() or len(vals["DESCRIPTION"]) > 600:
        raise EngineError(f"{where}：DESCRIPTION 要是 1–600 字的说明（擅长什么图、限制）")
    key = vals["KEY"]
    if key is not None and not (isinstance(key, str) and key_pattern(name).fullmatch(key)):
        raise EngineError(f"{where}：KEY 是 {key!r}，要写 None 或 STB_{name.upper()}_… 这样的键名（如 STB_{name.upper()}_API_KEY）")
    urls = vals["URLS"]
    if not isinstance(urls, (list, tuple)) or not urls or len(urls) > 10 or not all(isinstance(u, str) and _https(u) for u in urls):
        raise EngineError(f"{where}：URLS 要是 1–10 个 https:// 网址的列表")
    if not isinstance(search, ast.AsyncFunctionDef):
        raise EngineError(f"{where}：缺 async def search(image, key, http)" + ("（写成了普通 def，要 async def）" if search is not None else ""))
    a = search.args
    if [x.arg for x in a.posonlyargs + a.args] != SEARCH_ARGS or a.vararg or a.kwarg or a.kwonlyargs:
        raise EngineError(f"{where}：search 的参数要正好是 (image, key, http)")
    return Engine(name=name, description=vals["DESCRIPTION"].strip(), key=key, urls=tuple(urls), path=path or Path(f"{name}.py"))


def _https(u: str) -> bool:
    p = urlsplit(u)
    return p.scheme == "https" and bool(p.hostname) and not p.username and not p.password


def plugin_files(folder: Path = DIR) -> list[Path]:
    return sorted(f for f in folder.glob("*.py") if not f.name.startswith("_")) if folder.is_dir() else []


def scan(folder: Path = DIR) -> dict[str, Engine]:
    """文件夹里的插件（按文件名排序，_ 开头的跳过）；有一个不合约定就抛 EngineError。"""
    return {f.stem: parse_engine(f.stem, f.read_text(), f) for f in plugin_files(folder)}


def default_name(engines: dict[str, Engine], keys: dict[str, str]) -> str | None:
    """不写 engine 时用哪个：第一个可用的（按文件名排序，不要 key 或 key 已给的）；都不可用就文件名排第一的（调用时报没有 key）。"""
    return next((n for n, e in engines.items() if not e.key or keys.get(n)), next(iter(engines), None))


def url_allowed(url: str, urls: tuple[str, ...]) -> bool:
    """url 在声明的网址里：同协议、同主机（含端口）、路径以声明的路径开头。"""
    p = urlsplit(url)
    for u in urls:
        q = urlsplit(u)
        if p.scheme == q.scheme and p.netloc.lower() == q.netloc.lower() and p.path.startswith(q.path):
            return True
    return False


def load_search(engine: Engine) -> Callable[[EngineImage, str | None, "EngineHttp"], Awaitable[dict[str, Any]]]:
    """import 插件（只在研究容器里调用时），返回它的 search。"""
    spec = importlib.util.spec_from_file_location(f"owa_image_engine_{engine.name}", engine.path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "search", None)
    if not inspect.iscoroutinefunction(fn):
        raise EngineError(f"{engine.path}：search 不是 async 函数")
    return fn


class EngineHttp:
    """插件能用的网络：只能请求插件 URLS 声明的网址，都经研究引擎的 HttpFetcher（出站策略、代理、大小上限）。请求过的网址记在 requested。"""

    def __init__(self, fetcher: Any, engine: Engine) -> None:
        self._fetcher, self._engine = fetcher, engine
        self.requested: list[str] = []

    def _check(self, url: str) -> None:
        from owa_research.core.errors import ErrorCode, ToolError

        if not url_allowed(url, self._engine.urls):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"以图搜图服务 {self._engine.name} 请求了没声明的网址（只能连 {list(self._engine.urls)}）", stage="search")
        self.requested.append(url[:500])

    async def post_form(self, url: str, params: dict[str, Any] | None = None, files: dict[str, tuple[str, bytes, str]] | None = None) -> Any:
        """表单上传（POST multipart），返回 httpx.Response（status_code、json()、content、text）；不跟随重定向。"""
        self._check(url)
        return await self._fetcher.post_form(url, params=params or {}, files=files or {})

    async def get(self, url: str) -> Any:
        """GET，返回带 content（字节）、status 的结果；同站重定向会跟。"""
        self._check(url)
        return await self._fetcher.fetch(url, use_cache=False)

    @staticmethod
    def url(base: str, params: dict[str, Any]) -> str:
        """拼带查询参数的网址（编码同 httpx）。"""
        import httpx

        return str(httpx.URL(base, params=params))


def normalize(name: str, out: Any) -> dict[str, Any]:
    """插件返回值 → 统一格式（截断过长的字段）；格式不对抛 ToolError。"""
    from owa_research.core.errors import ErrorCode, ToolError

    def bad(why: str) -> ToolError:
        return ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"以图搜图服务 {name} 返回的格式不对（{why}），是插件的问题；换别的服务或按内容搜", stage="search")

    if not isinstance(out, dict) or not isinstance(out.get("sites"), list) or not isinstance(out.get("tags"), list):
        raise bad("要是 {sites: [...], tags: [...]}")
    if extra := set(out) - {"sites", "tags", "note", "total_sites"}:
        raise bad(f"多了 {sorted(extra)}")
    sites = []
    for s in out["sites"][:MAX_SITES]:
        if not isinstance(s, dict) or not isinstance(s.get("url"), str) or not isinstance(s.get("title"), str):
            raise bad("sites 的每项要有 url、title（字符串）")
        if s["url"] and not s["url"].startswith(("http://", "https://")):
            continue
        site: dict[str, Any] = {}
        for k, v in s.items():
            if isinstance(v, str):
                site[k] = v[:2000] if k == "url" else v[:500]
            elif isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
                site[k] = v
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                site[k] = [x[:2000] for x in v[:10]]
            else:
                raise bad(f"sites 里 {k} 的值类型不认识")
        sites.append(site)
    if not all(isinstance(t, str) for t in out["tags"]):
        raise bad("tags 要是字符串列表")
    res: dict[str, Any] = {"sites": sites, "tags": [t[:60] for t in out["tags"][:MAX_TAGS]]}
    if isinstance(out.get("total_sites"), int) and not isinstance(out["total_sites"], bool):
        res["total_sites"] = out["total_sites"]
    if isinstance(out.get("note"), str) and out["note"].strip():
        res["note"] = out["note"][:300]
    return res


def site_urls(res: dict[str, Any]) -> list[str]:
    """结果里出现的网址（url 与 urls 列表）：研究模型接着用 fetch_page 读出处，strict_egress 下要放行。"""
    out = []
    for s in res["sites"]:
        for u in [s.get("url"), *(s.get("urls") or [])]:
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                out.append(u)
    return out


# ---------- builder 用的静态检查（插件代码只碰声明的网址、只拿自己的 key） ----------
SAFE_IMPORTS = {"__future__", "json", "re", "html", "base64", "math", "typing", "dataclasses", "datetime", "urllib.parse", "string",
                "itertools", "functools", "collections", "unicodedata", "owa_research.core.errors"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr", "breakpoint", "input"}


def check_source(name: str, text: str) -> list[str]:
    """插件代码的问题（空列表 = 没问题）：先查约定，再查只 import 纯计算的标准库与 owa_research.core.errors、不调 open / eval / getattr 之类、
    不碰下划线开头的属性（绕过 http 的限制）、不读环境变量。网络只能用传进来的 http（只连 URLS），key 只用传进来的。"""
    try:
        parse_engine(name, text)
    except EngineError as e:
        return [str(e)]
    probs = []
    for node in ast.walk(ast.parse(text)):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            probs += [f"第 {line} 行 import {a.name}" for a in node.names if a.name not in SAFE_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in SAFE_IMPORTS:
                probs.append(f"第 {line} 行 from {'.' * node.level}{node.module or ''} import …")
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            probs.append(f"第 {line} 行用了 {node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in ("environ", "getenv")):
            probs.append(f"第 {line} 行访问属性 .{node.attr}")
    return [f"{name}.py：{p}（插件只能 import {'、'.join(sorted(SAFE_IMPORTS))}；联网只用参数 http，凭证只用参数 key，不读文件和环境变量）" for p in probs]
