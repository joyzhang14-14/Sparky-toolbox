"""edit-video 工具：剪视频。主模型写一份剪辑单（哪几段素材、每段从几秒到几秒、变速、画面尺寸、配乐、字幕、淡入淡出），
本工具请 stbcc-docker（control_center/docker_client.py）起一次性 stb-video 容器（services/stb-video）用 ffmpeg 执行：断网、只读根、无能力，
按原路径只读挂任务工作目录、只写本次运行的 media 目录；内存、CPU、进程数、/tmp 上限按 tool.yaml 的 docker。
另有 edit-video_info：看素材（时长、尺寸、帧率、有无音轨 + 抽帧拼图）。不换模型，不要凭证。
成品存工作目录 media/<运行 id>/edit-N.mp4（超过 10 MiB 另出 edit-N-discord.mp4 压缩版给 reply 用），结果带 4 帧拼图给主模型看。
环境变量：STB_WORK_ROOT、STB_RUN_ID、STB_RUN_REF（网关一律给；容器属于这次运行，运行结束就停）。
"""
import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, Field

from control_center.tool_kit import DISK_DOC, REASON_DOC, platform_fault, tool_timeout
from control_center.media import host_path, media_dir, MediaError, next_name, sandbox_path, work_root
from control_center import docker_client

IMAGE = "stb-video:latest"           # 内存、CPU、进程数、/tmp 上限在 tool.yaml 的 docker
INFO_TIMEOUT_S, EDIT_TIMEOUT_S = 120, 20 * 60
DISCORD_MAX = 10 * 1024 * 1024
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".gif")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")
PIC_EXT = (".png", ".jpg", ".jpeg", ".webp")

server = MCPServer("edit-video", instructions="ffmpeg 剪视频：看素材、按剪辑单出成片")


class Clip(BaseModel):
    file: str = Field(description="素材的绝对路径（工作目录下）：视频（mp4 / mov / mkv / webm…）或图片（png / jpg / webp，定格 duration 秒）")
    start: Annotated[float, Field(ge=0)] = Field(0, description="视频从第几秒开始用")
    end: float | None = Field(None, description="视频用到第几秒（不写 = 到结尾）")
    speed: Annotated[float, Field(ge=0.25, le=4)] = Field(1, description="变速倍数，声音同步变速")
    duration: Annotated[float, Field(gt=0, le=600)] = Field(3, description="只对图片：定格几秒")
    volume: Annotated[float, Field(ge=0, le=4)] = Field(1, description="这一段原声音量倍数")


class Subtitle(BaseModel):
    start: Annotated[float, Field(ge=0)] = Field(description="在成片里第几秒出现")
    end: Annotated[float, Field(gt=0)] = Field(description="第几秒消失")
    text: str = Field(max_length=200, description="字幕文字，可以用换行分两行")


class Music(BaseModel):
    file: str = Field(description="配乐的绝对路径（工作目录下；mp3 / wav / m4a… 或带音轨的视频），不够长会循环")
    volume: Annotated[float, Field(ge=0, le=4)] = Field(0.5, description="配乐音量倍数")
    start: Annotated[float, Field(ge=0)] = Field(0, description="从配乐的第几秒开始用")


class Plan(BaseModel):
    clips: list[Clip] = Field(min_length=1, max_length=50, description="按顺序拼接的片段")
    width: Annotated[int, Field(ge=64, le=3840)] | None = Field(None, description="成片宽（像素），不写 = 第一段视频的宽")
    height: Annotated[int, Field(ge=64, le=3840)] | None = Field(None, description="成片高，不写 = 第一段视频的高（竖屏 1080×1920）")
    fit: Literal["contain", "cover"] = Field("contain", description="比例不同时：contain 完整显示、补黑边；cover 铺满、裁掉多出的部分")
    fps: Annotated[int, Field(ge=1, le=60)] = 30
    mute: bool = Field(False, description="去掉所有片段的原声")
    music: Music | None = None
    subtitles: list[Subtitle] = Field([], max_length=500, description="烧进画面的字幕（思源黑体、白字黑边、底部居中）")
    subtitle_size: Annotated[int, Field(ge=12, le=200)] | None = Field(None, description="字幕字号（像素），不写 = 画面短边 / 18（1080 宽竖屏约 60）")
    fade: Annotated[float, Field(ge=0, le=5)] = Field(0, description="开头结尾淡入淡出几秒（画面和声音一起）")


def _src(path: str, exts: tuple[str, ...]) -> str:
    host = host_path(path)
    if host.suffix.lower() not in exts:
        raise MediaError(f"不支持的文件类型（只收 {' / '.join(e[1:] for e in exts)}）：{path}")
    if not host.is_file():
        raise MediaError(f"文件不存在：{path}")
    return sandbox_path(host)


def _docker(job: dict[str, Any], media: Path, timeout: int, disk: str = "", reason: str = "") -> dict[str, Any]:
    mounts = [{"host": str(work_root()), "container": str(work_root()), "mode": "ro"},   # 按原路径挂：容器里外同一个路径
              {"host": str(media), "container": sandbox_path(media), "mode": "rw"}]
    try:
        r = docker_client.run(IMAGE, tool="edit-video", run=os.environ.get("STB_RUN_REF"), mounts=mounts, input=json.dumps(job), text=True, timeout=timeout,
                              disk=disk, reason=reason)
    except subprocess.TimeoutExpired:
        raise tool_timeout(f"剪辑 {timeout // 60} 分钟没完成，已停止（素材太长或太多）") from None
    except OSError as e:
        raise platform_fault(f"剪辑容器没起来：{e}") from None
    if r.returncode == 137:
        raise platform_fault("剪辑容器被系统强制终止（退出码 137，通常是内存不足）")
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    if r.returncode != 0 or not lines:
        err = r.stderr.strip()
        if r.returncode == 1 and any(k in err for k in ("ffmpeg 出错", "读不了", "不对", "没有画面")):   # video.py 自己报的错（SystemExit）
            raise MediaError(err[-600:])
        raise platform_fault(f"剪辑容器没有正常结束（退出码 {r.returncode}）：{err[-300:]}")
    return json.loads(lines[-1])


def _sheet(path: Path) -> ImageContent:
    data = path.read_bytes()
    path.unlink(missing_ok=True)
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg")


@server.tool(name="edit-video_info", description="看一个视频素材：时长、宽高、帧率、有没有声音和一张抽帧拼图；剪之前先看")
def edit_video_info(file: str, count: Annotated[int, Field(ge=1, le=9)] = 4, start: Annotated[float, Field(ge=0)] = 0,
                    end: float | None = None) -> list[TextContent | ImageContent]:
    src = _src(file, VIDEO_EXT)
    media = media_dir()
    sheet = media / f"{next_name('info')}-sheet.jpg"
    r = _docker({"mode": "info", "file": src, "sheet": sandbox_path(sheet), "count": count, "start": start, "end": end},
                media, INFO_TIMEOUT_S)
    return [TextContent(type="text", text=json.dumps({"file": src, **r}, ensure_ascii=False)), _sheet(sheet)]


@server.tool(name="edit-video_run", description=(
    "按剪辑单剪出一个 mp4：拼接、截取、变速、调音量、配乐、烧字幕、淡入淡出；生成新视频用子代理 video"))
def edit_video_run(plan: Plan, disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> list[TextContent | ImageContent]:
    p = plan.model_dump()
    for c in p["clips"]:
        c["file"] = _src(c["file"], VIDEO_EXT + PIC_EXT)
    if p["music"]:
        p["music"]["file"] = _src(p["music"]["file"], AUDIO_EXT + VIDEO_EXT)
    for s in p["subtitles"]:
        if s["end"] <= s["start"]:
            raise MediaError(f"字幕的 end 要大于 start：{s['text'][:20]}")
    media = media_dir()
    name = next_name("edit")
    out, sheet, small = media / f"{name}.mp4", media / f"{name}-sheet.jpg", media / f"{name}-discord.mp4"
    r = _docker({"mode": "edit", "plan": p, "out": sandbox_path(out), "sheet": sandbox_path(sheet),
                 "discord_out": sandbox_path(small), "max_bytes": DISCORD_MAX}, media, EDIT_TIMEOUT_S, disk, reason)
    if not out.is_file():
        raise platform_fault("剪辑容器没写出成片")
    info = {"file": sandbox_path(out), **{k: r.get(k) for k in ("duration_s", "width", "height", "fps", "audio", "bytes", "frame_times")}}
    if small.is_file():
        info["discord_file"] = sandbox_path(small)
    elif r.get("bytes", 0) > DISCORD_MAX:
        info["note"] = "成片超过 10 MiB，压缩版也没压到 10 MiB 以下：剪短一些或降低分辨率再试"
    return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False)), _sheet(sheet)]


if __name__ == "__main__":
    server.run("stdio")
