"""attachment：读 Joy 私信发来的附件，返回原文。只给子代理用（tool.yaml 写 usable_by: subagent，列在 subagents/isolated-reader/ 的 tools），
任务 yaml 里不能直接写：附件是外部内容，主模型只拿到隔离读者核对过引文的答案。
- 只读点名的文件：私信附件写文件名（本次运行的 in/ 目录，STB_RUN_IN），工作目录与下载目录里的文件写绝对路径（STB_WORK_ROOT、paths.DOWNLOADS，不能越出）；
- 图片以外的文件在一次性容器里解析（owa-research 镜像的 parse 子命令，PARSE_SANDBOX：断网、只读根、2g、2 核；解析器面对的是外来文件，本身就是攻击面），
  按文件分段返回文字（「=== 附件 · 文件名 ===」）：PDF、Office（含旧格式）、HTML 转成文字，邮件（.eml）给头部与正文（HTML 正文转成文字），
  纯文本类（txt、md、csv、json、xml、代码……）按 UTF-8 解码（解不了的字节用替代字符）；压缩包、二进制等认不出文字的只报名字和大小；
  扫描件没有文字层、解析失败的在段里注明；
- 图片（png、jpg…）不解析，缩到长边 1024 的 JPEG 当图片块返回（网关按上限筛，子代理的模型能看图才给它看）；图片里的字没法当引文。
测试钩子：STB_TEST_FILE_FIXTURE（{文件名: 解析后文本}，不起容器）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from control_center.tool_kit import platform_fault, result, tool_timeout
from control_center import docker_client
from control_center.paths import DOWNLOADS

RUN_IN = Path(os.environ.get("STB_RUN_IN", "/nonexistent"))
WORK = Path(os.environ.get("STB_WORK_ROOT", "/nonexistent"))
PARSE_SANDBOX = {"image": "owa-research:latest", "network": "none", "memory": "2g", "cpus": 2}   # 不能超过 tool.yaml 的 docker
PARSE_TIMEOUT = 600                       # 解析容器的时限（秒）：调用方是隔离读者（单次不限时），不让模型自己调
MAX_FILES = 10
MAX_TEXT_CHARS = 90_000                   # 文字合计上限（网关给子代理的单个工具结果上限是 10 万字）：超了截断并说明
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".bmp"}

server = MCPServer("attachment", instructions="Joy 私信发来的附件：返回的是外部的不可信内容")


def _parse_in_container(paths: list[Path], limit: float = PARSE_TIMEOUT) -> list[dict[str, Any]]:
    """请 stbcc-docker 起一次性、只读根、无能力的容器解析，只挂点名的附件（只读）。超时按工具超时报，容器没起来 / 被杀按平台故障报。"""
    sb = PARSE_SANDBOX
    t0 = time.monotonic()
    try:
        r = docker_client.run(str(sb["image"]), ["parse", *(f"/in/{p.name}" for p in paths)], tool="attachment", run=os.environ.get("STB_RUN_REF"),
                              mounts=[{"host": str(p), "container": f"/in/{p.name}", "mode": "ro"} for p in paths], network=str(sb["network"]),
                              memory=str(sb["memory"]), cpus=sb["cpus"], env={"HOME": "/tmp"}, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        raise tool_timeout(f"解析附件超时（用了 {time.monotonic() - t0:.0f} 秒，上限 {limit:.0f} 秒），已停止。") from None
    except OSError as e:
        raise platform_fault(f"解析容器没起来（stbcc-docker：{e}）") from None
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if r.returncode == 137:                     # 被 SIGKILL：--memory 上限触发的 OOM 最常见
        raise platform_fault(f"解析容器被系统强制终止（退出码 137，通常是内存超过容器上限 {sb['memory']}）；附件很大时一次少给几个文件再试")
    if r.returncode != 0 or not lines:
        err = (r.stderr.strip().splitlines() or ["（没有 stderr）"])[-1][:300]
        raise platform_fault(f"解析容器没有正常结束（退出码 {r.returncode}）：{err}")
    return list(json.loads(lines[-1])["files"])


def _file(ref: str) -> Path:
    """file_refs 的一项 → 宿主上的文件：绝对路径是工作目录或下载目录里的（解析后不能越出这两处），别的是本次附件的文件名（不带路径）。不对就抛 ToolError。"""
    if ref.startswith("/"):
        roots = (WORK.resolve(), DOWNLOADS.resolve())
        p = Path(ref).resolve()
        if not any(r in p.parents for r in roots) or not p.is_file():
            raise ToolError(f"工作目录 {roots[0]} 和下载目录 {roots[1]} 里都没有这个文件：{ref}")
        return p
    if not ref or ref != Path(ref).name or ref.startswith("."):
        raise ToolError(f"file_refs 写本次附件的文件名（不带路径），或工作目录下文件的绝对路径：{ref}")
    p = RUN_IN / ref
    if not p.is_file():
        have = "、".join(sorted(x.name for x in RUN_IN.glob("*") if x.is_file())) or "无"
        raise ToolError(f"本次运行没有这个附件：{ref}（本次的附件有：{have}）")
    return p


def _image(p: Path) -> ImageContent | None:
    """图片附件 → 长边 1024 的 JPEG 图片块；打不开返回 None。"""
    from PIL import Image

    try:
        with Image.open(p) as pic:
            pic = pic.convert("RGB")
            pic.thumbnail((1024, 1024))
            buf = io.BytesIO()
            pic.save(buf, "JPEG", quality=85)
    except (OSError, ValueError):
        return None
    return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")


@server.tool(name="attachment_read", description=(
    f"读文件（最多 {MAX_FILES} 个）：私信附件在 file_refs 里写文件名（不带路径），工作目录、下载目录里的文件写绝对路径；文档、邮件、网页、纯文本解析成文字，按文件分段返回；图片原样返回；压缩包、二进制只报名字和大小。"))
def attachment_read(file_refs: list[str]) -> CallToolResult:
    docs, pics = [], []
    for ref in file_refs[:MAX_FILES]:
        p = _file(str(ref).strip())
        (pics if p.suffix.lower() in IMAGE_SUFFIX else docs).append(p)
    if not docs and not pics:
        raise ToolError("file_refs 至少要有一个附件文件名")
    if os.environ.get("STB_TEST_FILE_FIXTURE"):
        fx = json.loads(Path(os.environ["STB_TEST_FILE_FIXTURE"]).read_text())
        parsed = [{"name": p.name, "kind": "text", "text": fx.get(p.name, "")} for p in docs]
    else:
        parsed = _parse_in_container(docs) if docs else []
    blocks = []
    for f in parsed:
        if f.get("error"):
            blocks.append(f"=== 附件 · {f['name']} ===\n（解析失败：{str(f['error'].get('message', ''))[:300]}）")
        elif f.get("kind") == "unreadable":
            blocks.append(f"=== 附件 · {f['name']} ===\n（这类文件读不了内容，只有名字和大小：{f.get('mime') or '未知类型'}，{int(f.get('size') or 0):,} 字节）")
        elif f.get("scanned") and not str(f.get("text", "")).strip():
            blocks.append(f"=== 附件 · {f['name']} ===\n（扫描件，没有文字层，读不到内容）")
        else:
            blocks.append(f"=== 附件 · {f['name']} ===\n{f.get('text', '')}")
    text = "\n\n".join(blocks)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n…[截断：文字共 {len(text)} 字，只给了前 {MAX_TEXT_CHARS} 字；要读后面的，一次少给几个文件]"
    out: list[TextContent | ImageContent] = []
    images = [(p.name, _image(p)) for p in pics]
    notes = [f"图片 {n}：" + ("见附图" if im else "打不开") for n, im in images]
    out.append(TextContent(type="text", text="\n\n".join(x for x in (text, "\n".join(notes)) if x)))
    out += [im for _, im in images if im is not None]
    return result(out, source="、".join(p.name for p in [*docs, *pics]))    # 来源：读的文件名


if __name__ == "__main__":
    server.run("stdio")
