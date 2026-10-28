"""video 子代理（generator，subagents/video/subagent.yaml）的程序：用本地 ComfyUI 跑 MiniMax H3，文生视频或图生视频（首帧 + 可选尾帧），带立体声音轨。
主模型调用 video(prompt, …inputs)：网关先卸下主模型、按 yaml 的 runtime + model 拉起 ComfyUI，调用后换回主模型；本进程只经 ComfyUI 的 HTTP 接口上传输入图、提交工作流、下载成品（同文件夹的 _comfy.py；成品目录、路径、预览图用平台的 control_center/media.py）。
工作流是 subagents/video/workflows/minimax_h3.api.json（官方 i2v 图：不接首帧就是文生视频）。首帧会被拉伸到画布尺寸、不保持比例，
所以有首帧时按首帧宽高比、同样的像素预算算宽高（32 的倍数）。
给 references（参考图，最多 9 张）时走 subagents/video/workflows/minimax_h3_r2v.api.json（官方 r2v 图，主模型 ref2va + 它自己的 4 步加速 LoRA）：
参考图只告诉模型人物 / 物品 / 场景长什么样，不当首尾帧；prompt 里用 <Picture i> 按顺序指它们；尺寸照 aspect_ratio 与 megapixels 算。
成品之后的抽帧预览和超过 10 MiB 时的 Discord 压缩版由同文件夹的 _media_av.py 在 ComfyUI 的 python 里做（PyAV）。
参数与默认值、上下限同 yaml 的 inputs（网关按 inputs 先查，这里的 pydantic 再查一遍）；单次上限是 yaml 的 timeout（含抽帧 / 压缩；网关到点取消这次调用、中断 ComfyUI 里这一单）。
环境变量见 _comfy.py。
"""
import functools
import json
import math
import random
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import Field

from control_center.tool_kit import platform_fault
from control_center import media
import _comfy

WORKFLOWS = Path(__file__).resolve().parent / "workflows"   # 本子代理用的 ComfyUI 工作流（API 格式）

TIMEOUT_S = media.subagent_timeout("video", 100 * 60) + 60   # 网关按单次上限先取消；这里多 1 分钟只防网关没取消
STEPS, TURBO_STEPS, R2V_TURBO_STEPS = 20, 8, 4
MAX_REFERENCES = 9
DISCORD_MAX = 10 * 1024 * 1024                  # Discord 私信单个附件上限
SAVE_NODE = "92"
HELPER = Path(__file__).resolve().with_name("_media_av.py")

server = MCPServer("video", instructions="本地 MiniMax H3 文生视频 / 图生视频 / 参考图生视频（带声音）")


def size(ratio: float, megapixels: float) -> tuple[int, int]:
    """和 ComfyUI ResolutionSelector 同一算法：按宽高比和像素预算算宽高，取 32 的倍数。"""
    total = megapixels * 1024 * 1024
    return max(32, round(math.sqrt(total * ratio) / 32) * 32), max(32, round(math.sqrt(total / ratio) / 32) * 32)


def build(prompt: str, refs: list[str], width: int, height: int, duration_s: float, turbo: bool, steps: int, seed: int,
          prefix: str) -> dict[str, Any]:
    """refs 是 [首帧] 或 [首帧, 尾帧] 的 LoadImage 相对名；空就是文生视频。"""
    g = _comfy.template(WORKFLOWS / "minimax_h3.api.json")
    v = g["105:104"]["inputs"]
    del g["114"], v["first_frame"]                      # 模板里的示例首帧
    for key, name in zip(("first_frame", "last_frame"), refs):
        g[key] = {"class_type": "LoadImage", "inputs": {"image": name}}
        v[key] = [key, 0]
    v.update(prompt=prompt, width=width, height=height)
    g["105:111"]["inputs"]["value"] = duration_s        # 秒数，图里按 24 fps 换成 17k+5 帧
    g["105:126"]["inputs"]["value"] = turbo             # turbo：模型走加速 LoRA、步数走 105:125
    g["105:124"]["inputs"]["value"] = g["105:125"]["inputs"]["value"] = steps
    g["105:15"]["inputs"]["noise_seed"] = seed
    g[SAVE_NODE]["inputs"]["filename_prefix"] = f"stb/{media.run_id()}/{prefix}"
    return g


def build_r2v(prompt: str, refs: list[str], width: int, height: int, duration_s: float, turbo: bool, steps: int, seed: int,
              prefix: str) -> dict[str, Any]:
    """refs 是参考图的 LoadImage 相对名（1–9 张），按顺序接到 ref_images.ref_image_0…，prompt 里是 <Picture 1>…。"""
    g = _comfy.template(WORKFLOWS / "minimax_h3_r2v.api.json")
    v = g["136"]["inputs"]
    for i, name in enumerate(refs):
        g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        v[f"ref_images.ref_image_{i}"] = [f"ref{i}", 0]
    v.update(prompt=prompt, width=width, height=height)
    g["132"]["inputs"]["value"] = duration_s
    g["146"]["inputs"]["value"] = turbo                 # turbo：模型走 r2v 的加速 LoRA、步数走 144
    g["143"]["inputs"]["value"] = g["144"]["inputs"]["value"] = steps
    g["129"]["inputs"]["noise_seed"] = seed
    g[SAVE_NODE]["inputs"]["filename_prefix"] = f"stb/{media.run_id()}/{prefix}"
    return g


def _av_run(args: list[str], timeout: float) -> dict[str, Any]:
    """在 ComfyUI 的 python（有 PyAV）里跑 _media_av.py，取 stdout 最后一行 JSON。"""
    try:
        r = subprocess.run([_comfy.comfy_python(), str(HELPER), *args], capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        raise platform_fault(f"视频后处理（{args[0]}）起不来：{_comfy.comfy_python()} 跑不了（{e}）") from None
    except subprocess.TimeoutExpired:
        raise media.MediaError(f"视频后处理（{args[0]}）{timeout:g} 秒没做完，已停止；成品原片已存好（{media.sandbox_path(Path(args[1]))}），可以直接用它，或用 edit-video 自己压缩") from None
    if r.returncode != 0:
        raise media.MediaError(f"视频后处理（{args[0]}）失败（退出码 {r.returncode}）：{r.stderr.strip()[-500:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


async def _av(*args: str, timeout: float) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(functools.partial(_av_run, list(args), timeout))


@server.tool(name="video", description="做带声音的短视频（本地 MiniMax H3，1–15 秒）：文生视频、图生视频（首帧 / 尾帧）或参考图生视频")
async def video(prompt: str, first_frame: str = "", last_frame: str = "", references: list[str] = [],  # noqa: B006 — MCP 参数默认值
                         duration_s: Annotated[float, Field(ge=1, le=15)] = 5,
                         aspect_ratio: _comfy.AspectRatio = "16:9 (Widescreen)",
                         megapixels: Annotated[float, Field(ge=0.2, le=1.0)] = 0.4,
                         turbo: bool = False,
                         steps: Annotated[int, Field(ge=1, le=50)] = STEPS,
                         seed: Annotated[int, Field(ge=-1)] = -1) -> list[TextContent | ImageContent]:
    if not prompt.strip():
        raise media.MediaError("prompt 不能为空")
    if last_frame and not first_frame:
        raise media.MediaError("给 last_frame 时也要给 first_frame")
    if references and (first_frame or last_frame):
        raise media.MediaError("references（参考图）不能和 first_frame / last_frame 一起用：要从某张图开始就用 first_frame，要保持人物 / 场景一致就用 references")
    if len(references) > MAX_REFERENCES:
        raise media.MediaError(f"references 最多 {MAX_REFERENCES} 张")
    if turbo and steps == STEPS:                        # turbo 没另给步数就用 LoRA 的步数（fl2v 8 步、r2v 4 步）
        steps = R2V_TURBO_STEPS if references else TURBO_STEPS
    t0 = time.monotonic()
    name = media.next_name("video")
    refs = await _comfy.stage_inputs(list(references) or [p for p in (first_frame, last_frame) if p], name)
    if refs and not references:
        width, height = size(refs[0][1] / refs[0][2], megapixels)
    else:
        a, b = aspect_ratio.split(" ")[0].split(":")
        width, height = size(int(a) / int(b), megapixels)
    seed = seed if seed >= 0 else random.randrange(2**48)
    g = (build_r2v if references else build)(prompt, [r[0] for r in refs], width, height, duration_s, turbo, steps, seed, name)
    out = await _comfy.run(g, SAVE_NODE, TIMEOUT_S)
    dst = await _comfy.keep(out, name)
    with tempfile.TemporaryDirectory() as tmp:
        sheet = Path(tmp) / "sheet.jpg"
        meta = await _av("sheet", str(dst), str(sheet), timeout=300)
        discord = dst
        if dst.stat().st_size > DISCORD_MAX:            # 私信发不了：另存一份按码率压到 10 MiB 内的
            discord = dst.with_name(f"{name}-discord.mp4")
            await _av("shrink", str(dst), str(discord), str(DISCORD_MAX), timeout=900)
        info = {"file": media.sandbox_path(dst), "discord_file": media.sandbox_path(discord), "duration_s": meta["duration_s"],
                "width": meta["width"], "height": meta["height"], "fps": meta["fps"], "audio": meta["audio"], "seed": seed,
                "steps": steps, "seconds": round(time.monotonic() - t0)}
        with Image.open(sheet) as im:
            return media.result(info, im)


if __name__ == "__main__":
    server.run("stdio")
