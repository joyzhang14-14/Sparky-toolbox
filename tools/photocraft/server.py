"""photocraft 工具：把 Joy 指定的 photocraft（纯 Rust 的 Photoshop 重实现，官方 aarch64 包）接成修图工具。

走 photocraft 自带的 headless MCP 引擎（_engine.py 起 `photocraft-cli mcp` 子进程，stdio JSON-RPC）：
- 子进程第一次用到才起、握在工具进程里：同一次运行里跨工具调用文档打开着、改动留着；工具进程退出随它一起退，跨运行不保留。
- 子进程 RLIMIT_AS 8 GB（超限本次调用失败并交回原因，不拖垮宿主）；不联网、不起容器、不要 GPU。
- 读根 = 本次运行的工作目录、写根 = 本次运行的 media 目录（起子进程时授予，这里在调用前先校验，报错写清可用的根）。
- 参数与返回沿用 photocraft 自己的 JSON（command_list 带每条命令的参数文档），不做字段翻译；服务端 error 原文交回。
分工（Joy 2026-10-20 定）：photocraft 管调色 / 滤镜 / 裁剪缩放 / 合成 / 图层 / 文字 / 形状 / PSD·PCRAFT；
精细抠图（头发、胡须）仍用 cutout；生成新内容仍用 image 子代理。三者都保留、不替换。
"""
import io
import json
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from PIL import Image

from control_center import media
import _engine

server = MCPServer("photocraft", instructions="photocraft 修图：doc_open/doc_new → command_list 查命令 → command_run/command_batch 改 → doc_inspect/render_preview 检查 → doc_save 保存")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _call(name: str, arguments: dict[str, Any]) -> str:
    """调一次引擎工具并交回 photocraft 自己的 JSON；会话重建过时在返回里明说（不静默装作文档还在）。"""
    res, rebuilt = _engine.tools_call(name, arguments)
    payload = _engine.text_payload(res)
    if rebuilt:
        if isinstance(payload, dict):
            payload = {**payload, "session_rebuilt": _engine.REBUILT_NOTE}
        else:
            payload = {"session_rebuilt": _engine.REBUILT_NOTE, "result": payload}
    return _json(payload)


def _read_path(path: str) -> Path:
    """要读的文件：必须在本次运行的工作目录（读根）里；media.host_path 的报错会写清这个根。"""
    p = media.host_path(path)
    if not p.is_file():
        raise ToolError(f"文件不存在：{path}（读根是本次运行的工作目录 {media.work_root()}，只收它下面文件的绝对路径）")
    return p


def _write_path(path: str) -> Path:
    """要写的文件：必须在本次运行的 media 目录（写根）里；报错写清两个根。"""
    md = media.media_dir()
    try:
        p = media.host_path(path)
    except media.MediaError:
        raise ToolError(f"保存只能写进本次运行的 media 目录 {md}（它在工作目录 {media.work_root()} 下）：你给的 {path} 不在里面") from None
    if md not in p.parents:
        raise ToolError(f"保存只能写进本次运行的 media 目录 {md}（你给的 {path} 不在里面；要读的文件放工作目录 {media.work_root()} 下，输出写 {md}/文件名）")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@server.tool(name="photocraft_doc_open", description=(
    "打开图片 / 文档成为当前文档（png / jpg / webp / tif / psd / psb / pcraft 等）；path = 本次运行工作目录内文件的绝对路径。"
    "返回 photocraft 的 JSON：index、尺寸、导入告警"))
def photocraft_doc_open(path: str) -> str:
    return _call("doc_open", {"path": str(_read_path(path))})


@server.tool(name="photocraft_doc_new", description=(
    "新建文档（不写默认 1920x1080 RGB 8 位白底）。width/height 像素；mode 取 rgb|gray|cmyk|lab；depth 取 8|16|32；"
    "fill 底色：white | black | transparent | #rrggbb。返回 photocraft 的 JSON"))
def photocraft_doc_new(width: int = 0, height: int = 0, mode: str = "", depth: int = 0,
                       fill: str = "") -> str:
    a: dict[str, Any] = {}
    if width:
        a["width"] = width
    if height:
        a["height"] = height
    if mode:
        a["mode"] = mode
    if depth:
        a["depth"] = depth
    if fill:
        a["background"] = fill          # 引擎的底色参数叫 background
    return _call("doc_new", a)


@server.tool(name="photocraft_doc_place", description=(
    "把文件作为 Smart Object 图层放进当前文档（Place Embedded）；path = 本次运行工作目录内文件的绝对路径。返回 photocraft 的 JSON"))
def photocraft_doc_place(path: str) -> str:
    return _call("doc_place", {"path": str(_read_path(path))})


@server.tool(name="photocraft_session_list", description=(
    "列出当前打开的文档（index、名字、尺寸、是否已改）与当前 index——跨工具调用确认会话还在、文档还开着就用它。返回 photocraft 的 JSON"))
def photocraft_session_list() -> str:
    return _call("session_list", {})


@server.tool(name="photocraft_doc_select", description="切换当前文档（index 来自 photocraft_session_list）。返回 photocraft 的 JSON")
def photocraft_doc_select(index: int) -> str:
    return _call("doc_select", {"index": index})


@server.tool(name="photocraft_doc_close", description="关掉文档（不保存）；index 不写关当前文档。返回 photocraft 的 JSON")
def photocraft_doc_close(index: int = -1) -> str:
    return _call("doc_close", {} if index < 0 else {"index": index})


@server.tool(name="photocraft_command_list", description=(
    "检索 photocraft 引擎命令注册表（实测 918 条）：filter 按 id / label / 关键词过滤（如 levels、brightness、crop），不写列全部。"
    "返回 JSON 数组 [{id,label,menu:[路径],params:参数文档,…}]——改图前先用它查准确命令 id 和参数文档再跑"))
def photocraft_command_list(filter: str = "") -> str:
    return _call("command_list", {} if not filter else {"filter": filter})


@server.tool(name="photocraft_command_run", description=(
    "按 id 跑一条引擎命令（一条 history step），params 是命令的参数 JSON（写法先 photocraft_command_list 查）。"
    "返回命令自己的 JSON；引擎报错原文交回。常用：image.autoTone、image.adjustments.brightnessContrast、image.imageSize（缩放写法见 guide）"))
def photocraft_command_run(id: str, params: dict[str, Any] | None = None) -> str:
    return _call("command_run", {"id": id, "params": params})


@server.tool(name="photocraft_command_batch", description=(
    "一次按序跑多条命令（少几趟往返）：steps = [[命令id], [命令id, 参数], …]，任何一步报错会在返回里写明第几步。"
    "返回 photocraft 的 JSON（completed、failed、results）"))
def photocraft_command_batch(steps: list[Any]) -> str:
    out: list[dict[str, Any]] = []
    for s in steps:
        if isinstance(s, dict):
            if not s.get("id"):
                raise ToolError(f"steps 每项要 [命令id] 或 [命令id, 参数]（字典要带 id）：{s!r}")
            d: dict[str, Any] = {"id": str(s["id"])}
            if "params" in s:
                d["params"] = s["params"]
            out.append(d)
        elif isinstance(s, (list, tuple)) and 1 <= len(s) <= 2:
            d = {"id": str(s[0])}
            if len(s) == 2:
                d["params"] = s[1]
            out.append(d)
        else:
            raise ToolError(f"steps 每项要 [命令id] 或 [命令id, 参数]：{s!r}")
    if not out:
        raise ToolError("steps 是空的")
    return _call("command_batch", {"steps": out})


@server.tool(name="photocraft_doc_inspect", description=(
    "当前文档状态 JSON：图层树（自上而下）、history、选区、当前图层——确认改动进没进、图层结构长什么样用它"))
def photocraft_doc_inspect() -> str:
    return _call("doc_inspect", {})


@server.tool(name="photocraft_render_preview", description=(
    "渲染扁平化 PNG 预览，存进本次运行的 media/ 并附预览图给你看（不落盘看不到效果就用它）。"
    "max_side 预览最长边（不写默认 1024，最大 2048，0 = 按上限全尺寸）；width/height 只在想限制大致大小时写（取两者较大值当 max_side）"))
def photocraft_render_preview(width: int = 0, height: int = 0, max_side: int = 0) -> list[TextContent | ImageContent]:
    side = max_side or max(width, height)
    res, rebuilt = _engine.tools_call("render_preview", {} if not side else {"max_side": int(side)})
    png = _engine.decode_image(res)
    if not png:
        raise ToolError(f"render_preview 没返回图片：{_json(_engine.text_payload(res))}")
    try:
        im = Image.open(io.BytesIO(png))
        im.load()
    except Exception as e:
        raise ToolError(f"render_preview 返回的不是能读的 PNG：{e}") from None
    name = media.next_name("preview")
    dest = media.media_dir() / f"{name}.png"
    dest.write_bytes(png)
    info: dict[str, Any] = {"file": media.sandbox_path(dest), "width": im.width, "height": im.height,
                            "max_side": int(side) if side else 1024}
    if rebuilt:
        info["session_rebuilt"] = _engine.REBUILT_NOTE
    return [TextContent(type="text", text=_json(info)), media.preview(dest)]


@server.tool(name="photocraft_doc_save", description=(
    "保存 / 导出当前文档：path 必须在本次运行的 media 目录内（绝对路径）；.pcraft 是无损原生格式（可增量保存），"
    "其他扩展名（psd / png / jpg / tif / webp 等）按格式导出；format 可显式指定格式（如 \"png\"），不写按扩展名。返回 photocraft 的 JSON（path、warnings）"))
def photocraft_doc_save(path: str, format: str = "") -> str:
    a: dict[str, Any] = {"path": str(_write_path(path))}
    if format:
        a["format"] = format
    return _call("doc_save", a)


if __name__ == "__main__":
    server.run("stdio")
