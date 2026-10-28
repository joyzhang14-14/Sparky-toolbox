"""平台给产出文件的工具 / 子代理程序的接口（`from control_center import media`）：本次运行的 media 目录与成品名、模型写的路径 → 宿主路径、
给主模型看的预览图、带预览图的结果、一次性 GPU 容器（run_gpu）与内存不足的说明、子代理的单次上限。和具体后端（ComfyUI 等）无关。

路径都在调用时按环境变量算（测试指到临时目录）：
- STB_WORK_ROOT：任务的工作目录（沙箱里外同一个路径）；成品放 media/<run_id>/
- STB_RUN_ID：本次运行 id（只含字母数字和 . _ -）
运行信息由网关一律给。
"""
import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from PIL import Image

from control_center import docker_client
from control_center.tool_kit import platform_fault

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_INPUT_BYTES = 20 * 1024 * 1024
PREVIEW_EDGE = 1024
MEDIA_KEEP_DAYS = 90                                    # media/<运行 id>/ 超过这么多天没动过就在下次生成时删掉
_reserved: set[Path] = set()                            # 本进程已分出去的成品名：同一次运行里并发的调用不会撞名


class MediaError(ToolError):
    """参数、额度或生成失败：说明原样回给主 agent（MCP 只透传 ToolError 的文字）。"""


def subagent_timeout(name: str, default: int) -> int:
    """子代理 name 的单次上限（subagents/<名>/subagent.yaml 的 timeout，秒）：网关按它先取消调用，程序自己的兜底时限按它定。
    写 none（不限时）或读不到用 default。"""
    import yaml

    from control_center.subagents import def_path
    from control_center.task import TaskError, parse_duration

    try:
        return parse_duration((yaml.safe_load(def_path(name).read_text()) or {})["timeout"])
    except (OSError, KeyError, TypeError, yaml.YAMLError, TaskError):
        return default


def run_id() -> str:
    rid = os.environ.get("STB_RUN_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", rid):
        raise MediaError("STB_RUN_ID 缺失或含路径字符")
    return rid


def work_root() -> Path:
    if not os.environ.get("STB_WORK_ROOT"):
        raise MediaError("STB_WORK_ROOT 未设置")
    return Path(os.environ["STB_WORK_ROOT"]).resolve()


def host_path(path: str) -> Path:
    """模型写的路径（沙箱里外同一个路径）→ realpath：要写绝对路径，realpath 后必须在 STB_WORK_ROOT 里（.. 和符号链接都逃不出去）。"""
    root = work_root()
    if not isinstance(path, str) or not path.startswith("/"):
        raise MediaError(f"路径要写工作目录下文件的绝对路径（如 {root}/in/图.png）：{path}")
    host = Path(path).resolve()
    if root not in host.parents:
        raise MediaError(f"要写工作目录 {root} 下的文件（别处的先拷进工作目录）：{path}")
    return host


def sandbox_path(host: Path) -> str:
    """给模型看的路径：沙箱里外同一个路径，就是 realpath。"""
    return str(host.resolve())


def media_dir() -> Path:
    d = work_root() / "media" / run_id()
    d.mkdir(parents=True, exist_ok=True)
    if work_root() not in d.resolve().parents:
        raise MediaError("media 目录越界")
    prune_media(d)
    return d


def prune_media(current: Path) -> None:
    """删掉 media/ 下超过 MEDIA_KEEP_DAYS 天没动过的运行目录（只看直接子目录，跳过符号链接和本次运行的目录）。"""
    cutoff = time.time() - MEDIA_KEEP_DAYS * 86400
    for p in current.parent.iterdir():
        with contextlib.suppress(OSError):
            if p != current and p.is_dir() and not p.is_symlink() and p.stat().st_mtime < cutoff:
                shutil.rmtree(p)


def next_name(kind: str) -> str:
    """本次运行里还没用过的成品名 image-1、image-2…（成品文件名不带扩展名；也可当后端的输出前缀、输入目录名）。"""
    d, n = media_dir(), 1
    while d / f"{kind}-{n}" in _reserved or any(d.glob(f"{kind}-{n}.*")):
        n += 1
    _reserved.add(d / f"{kind}-{n}")
    return f"{kind}-{n}"


def result(info: dict[str, Any], image: Image.Image) -> list[TextContent | ImageContent]:
    """工具结果：JSON 文字 + 一张 JPEG 预览图（长边 ≤ 1024），网关把图转给主模型看。"""
    im = image.convert("RGB")
    im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False)),
            ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mime_type="image/jpeg")]


def preview(path: Path) -> ImageContent:
    """给主模型看的预览图：长边 ≤ PREVIEW_EDGE 的 JPEG；透明处垫灰色。"""
    with Image.open(path) as im:
        im = im.convert("RGBA")
        im = Image.alpha_composite(Image.new("RGBA", im.size, (128, 128, 128, 255)), im).convert("RGB")
        im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")  # pyright: ignore[reportCallIssue]


GPU_OOM = ("CUDA_ERROR_OUT_OF_MEMORY", "CUDA out of memory", "OutOfMemoryError")
GPU_IMAGE = "stb-docker-base"                                   # services/stb-docker-base：只有系统和 python3.12，venv、模型、脚本都从宿主挂进去
GPU_ENV = {"TORCH_DISABLE_NATIVE_JIT": "1"}                      # 镜像没有 C 编译器：不让 torch 在 import 时注册要现场编译的 Triton 内核（工具的 env 可覆盖）


def avail_gb() -> float:
    """宿主当前可用内存（/proc/meminfo 的 MemAvailable，GB）：GB10 统一内存，显存也从这里出。"""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def low_memory(what: str, avail: float, need: float) -> str:
    """开跑前可用内存不够、或跑的时候显存不足的说明：给出现在可用多少、这个工具要多少，由主模型决定稍后重试还是换更小的输入。"""
    return (f"{what}：显存不足（当前宿主可用约 {avail:.1f} GB，本工具大约需要 {need:g} GB；主模型和别的任务也在用内存）。"
            "可以稍后重试，或换更小的输入")


def run_gpu(tool: str, cmd: list[str], read: list[Path], write: list[Path], *, timeout: float, need_gb: float,
            env: dict[str, str] | None = None, cwd: Path | None = None, disk: str = "", reason: str = "") -> "subprocess.CompletedProcess[str]":
    """跑一次性 GPU 脚本（如 cutout、transcribe）：请 stbcc-docker 起 stb-docker-base 容器（--gpus all、断网、只读根，资源按 tool.yaml 的 docker），
    read 里的路径（venv、模型、脚本、输入）以宿主上的同一路径只读挂进去、write 的可写，cmd 原样在容器里跑（路径不用换）；环境默认带 GPU_ENV，工具给的 env 叠在上面。
    跑的时候显存不足（stderr 里有 CUDA OOM）不自动重试，报平台故障并写明当前可用与需要的内存（need_gb），由主模型决定。
    服务不在、被拒抛 OSError，到时限抛 subprocess.TimeoutExpired；disk / reason（磁盘额度申请）原样交给 stbcc-docker。"""
    env = {**GPU_ENV, **(env or {})}
    mounts = [{"host": str(p), "container": str(p), "mode": "ro"} for p in read] + [{"host": str(p), "container": str(p), "mode": "rw"} for p in write]
    r = docker_client.run(GPU_IMAGE, cmd, tool=tool, run=os.environ.get("STB_RUN_REF"), mounts=mounts, gpu=True, env=env,
                          workdir=str(cwd) if cwd else None, text=True, timeout=timeout, disk=disk, reason=reason)
    if r.returncode != 0 and any(k in (r.stderr or "") for k in GPU_OOM):
        raise platform_fault(low_memory(tool, avail_gb(), need_gb) + f"。原始报错：{(r.stderr.strip().splitlines() or [''])[-1][:300]}")
    return r
