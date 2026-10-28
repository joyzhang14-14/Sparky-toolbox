"""render 工具：把主模型写在工作目录里的 HTML 用无头 Chromium 截成 PNG 或导出 PDF（海报、卡片、幻灯片、裁剪 / 拼图 / 加字）。
每次调用请 stbcc-docker（control_center/docker_client.py）起一个一次性 stb-render 容器（services/stb-render）：断网、只读根、无能力，
按原路径只读挂任务工作目录、只写本次运行的 media 目录；内存、CPU、进程数、/tmp 上限按 tool.yaml 的 docker。字体全部烧在镜像里。
成品存工作目录 media/<运行 id>/render-N.png|pdf，结果带预览图给主模型检查（PDF 最多 4 页预览）。
时限默认 TIMEOUT_S（Office 转换 OFFICE_TIMEOUT_S），主模型可以按调用给 timeout（不设上限）。不需要换模型，也不需要凭证。环境变量：STB_WORK_ROOT、STB_RUN_ID、STB_RUN_REF（网关一律给；容器属于这次运行，运行结束就停）。
"""
import json
import os
import time
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import Field

from control_center.tool_kit import DISK_DOC, REASON_DOC, TIMEOUT_DOC, call_timeout, platform_fault, timeout_note, tool_timeout
from control_center.media import host_path, media_dir, MediaError, next_name, preview, sandbox_path, work_root
from control_center import docker_client

IMAGE = "stb-render:latest"          # 内存、CPU、进程数、/tmp 上限在 tool.yaml 的 docker
TIMEOUT_S = 120
OFFICE_TIMEOUT_S = 200          # LibreOffice 冷启动 + 转换
OFFICE_EXT = (".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".odp", ".odt", ".ods")
MAX_EDGE = 8000                       # 画布边长上限（像素，乘 scale 之前）
MAX_PREVIEWS = 4                      # 网关每次最多转给主模型 4 张图
CATEGORIES = ("SANS_SERIF", "SERIF", "DISPLAY", "HANDWRITING", "MONOSPACE")

server = MCPServer("render", instructions="把工作目录里的 HTML 渲染成 PNG / PDF")


def _html(path: str) -> Path:
    host = host_path(path)
    if host.suffix.lower() not in (".html", ".htm"):
        raise MediaError(f"html 要写 .html 文件的路径：{path}")
    if not host.is_file():
        raise MediaError(f"没有这个文件：{path}")
    return host


def _docker(job: dict[str, Any], media: Path | None = None, timeout: float = TIMEOUT_S, given: bool = False, disk: str = "",
            reason: str = "") -> dict[str, Any]:
    """请 stbcc-docker 起一次性渲染容器跑 job，返回容器 stdout 最后一行的 JSON。given：timeout 是主模型这次给的（超时说明里这么写）。"""
    mounts = [{"host": str(work_root()), "container": str(work_root()), "mode": "ro"}]   # 按原路径挂：容器里外同一个路径
    if media is not None:
        mounts.append({"host": str(media), "container": sandbox_path(media), "mode": "rw"})
    t0 = time.monotonic()
    try:
        r = docker_client.run(IMAGE, tool="render", run=os.environ.get("STB_RUN_REF"), mounts=mounts, input=json.dumps(job), text=True, timeout=timeout,
                              disk=disk, reason=reason)
    except subprocess.TimeoutExpired:
        raise tool_timeout(timeout_note("渲染", time.monotonic() - t0, timeout, given) + "；也可能是页面里有死循环脚本，先检查 HTML") from None
    except OSError as e:
        raise platform_fault(f"渲染容器没起来（stbcc-docker：{e}）") from None
    if r.returncode == 137:
        raise platform_fault("渲染容器被系统强制终止（退出码 137，通常是内存超过容器上限）；页面很大（超长整页、超大画布、大量高清图）时缩小 width / height / scale 或拆成几页再试")
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    if r.returncode != 0 or not lines:
        err = (r.stderr.strip().splitlines() or ["（没有 stderr）"])[-1][:300]
        if "net::ERR_FILE_NOT_FOUND" in r.stderr or "Timeout" in r.stderr:
            raise MediaError(f"页面打不开：{err}")
        raise platform_fault(f"渲染容器没有正常结束（退出码 {r.returncode}）：{err}")
    return json.loads(lines[-1])


def _text(info: dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(info, ensure_ascii=False))


@server.tool(name="render_png", description=(
    "把工作目录里的 HTML 文件截成 PNG：海报、卡片、封面、拼图、给图加字 / 边框 / 换底色、裁剪缩放"))
def render_png(html: str, width: Annotated[int, Field(ge=16, le=MAX_EDGE)] = 1080, height: Annotated[int, Field(ge=16, le=MAX_EDGE)] = 1350,
               scale: Annotated[float, Field(ge=0.5, le=3)] = 1, transparent: bool = False, full_page: bool = False,
               timeout: Annotated[int | str, Field(description=TIMEOUT_DOC.format(default="120 秒"))] = 0, disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> list[TextContent | ImageContent]:
    limit = call_timeout(timeout, TIMEOUT_S)
    src = _html(html)
    media = media_dir()
    name = next_name("render")
    dst = media / f"{name}.png"
    r = _docker({"mode": "png", "html": sandbox_path(src), "out": sandbox_path(dst), "width": width, "height": height,
                 "scale": scale, "transparent": transparent, "full_page": full_page}, media, limit, bool(timeout), disk, reason)
    if not dst.is_file():
        raise platform_fault(f"渲染容器正常退出但没写出 PNG（容器输出：{json.dumps(r, ensure_ascii=False)[:300]}）")
    with Image.open(dst) as im:
        size = im.size
    info = {"file": sandbox_path(dst), "width": size[0], "height": size[1], "problems": r.get("problems", [])}
    return [_text(info), preview(dst)]


@server.tool(name="render_pdf", description="把工作目录里的 HTML 文件导出成 PDF：幻灯片（PDF 版）、文档、多页小册子")
def render_pdf(html: str, width: Annotated[int, Field(ge=16, le=MAX_EDGE)] = 1280, height: Annotated[int, Field(ge=16, le=MAX_EDGE)] = 720,
               preview_pages: list[int] = [1, 2, 3, 4], timeout: Annotated[int | str, Field(description=TIMEOUT_DOC.format(default="120 秒"))] = 0, disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> list[TextContent | ImageContent]:
    limit = call_timeout(timeout, TIMEOUT_S)
    src = _html(html)
    media = media_dir()
    name = next_name("render")
    dst, pv = media / f"{name}.pdf", media / f"{name}-preview"
    pv.mkdir(exist_ok=True)
    try:
        r = _docker({"mode": "pdf", "html": sandbox_path(src), "out": sandbox_path(dst), "width": width, "height": height,
                     "previews": sorted(set(preview_pages))[:MAX_PREVIEWS], "preview_dir": sandbox_path(pv)}, media, limit, bool(timeout), disk, reason)
        if not dst.is_file():
            raise platform_fault(f"渲染容器正常退出但没写出 PDF（容器输出：{json.dumps(r, ensure_ascii=False)[:300]}）")
        shots = [preview(pv / Path(p["file"]).name) for p in r.get("previews", []) if (pv / Path(p["file"]).name).is_file()]
    finally:
        shutil.rmtree(pv, ignore_errors=True)
    info = {"file": sandbox_path(dst), "pages": r.get("pages"), "previewed": [p["page"] for p in r.get("previews", [])],
            "problems": r.get("problems", [])}
    return [_text(info), *shots]


@server.tool(name="render_office", description=(
    "把工作目录里的 Office 文件（pptx / docx / xlsx 等）转成 PDF 并给几页预览：做完 pptx-deck 之类的文件用它检查排版"))
def render_office(file: str, preview_pages: list[int] = [1, 2, 3, 4], timeout: Annotated[int | str, Field(description=TIMEOUT_DOC.format(default="200 秒"))] = 0,
                  disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> list[TextContent | ImageContent]:
    limit = call_timeout(timeout, OFFICE_TIMEOUT_S)
    src = host_path(file)
    if src.suffix.lower() not in OFFICE_EXT:
        raise MediaError(f"只收 {' / '.join(e[1:] for e in OFFICE_EXT)} 文件：{file}")
    if not src.is_file():
        raise MediaError(f"没有这个文件：{file}")
    media = media_dir()
    name = next_name("render")
    dst, pv = media / f"{name}.pdf", media / f"{name}-preview"
    pv.mkdir(exist_ok=True)
    try:
        r = _docker({"mode": "office", "file": sandbox_path(src), "out": sandbox_path(dst),
                     "previews": sorted(set(preview_pages))[:MAX_PREVIEWS], "preview_dir": sandbox_path(pv)}, media, limit, bool(timeout), disk, reason)
        if not dst.is_file():
            raise platform_fault(f"LibreOffice 正常退出但没转出 PDF（容器输出：{json.dumps(r, ensure_ascii=False)[:300]}）；文件可能损坏或加了密码：确认文件完整（比如重新生成一次）再试")
        shots = [preview(pv / Path(p["file"]).name) for p in r.get("previews", []) if (pv / Path(p["file"]).name).is_file()]
    finally:
        shutil.rmtree(pv, ignore_errors=True)
    info = {"file": sandbox_path(dst), "source": sandbox_path(src), "pages": r.get("pages"),
            "previewed": [p["page"] for p in r.get("previews", [])]}
    return [_text(info), *shots]


@server.tool(name="render_fonts", description="查渲染容器里装了哪些字体（按类别、中文、名字查）")
def render_fonts(category: str = "", zh: bool = False, query: str = "") -> str:
    if category and category not in CATEGORIES:
        raise MediaError(f"category 只能是 {' / '.join(CATEGORIES)} 或空")
    rows = _docker({"mode": "fonts"})["fonts"]
    rows = [f for f in rows if (not category or f["category"] == category) and (not zh or f["zh"]) and query.lower() in f["family"].lower()]
    return json.dumps({"total": len(rows), "fonts": rows[:200]}, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
