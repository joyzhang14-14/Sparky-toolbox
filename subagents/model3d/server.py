"""model3d 子代理（generator，subagents/model3d/subagent.yaml）的程序：用本地 ComfyUI 跑 Pixal3D（bf16 原版），图片生成带 PBR 贴图的 3D 模型（GLB）。
主模型调用 model3d(prompt, image, …inputs)：网关先卸下主模型、按 yaml 的 runtime + model 拉起 ComfyUI，调用后换回主模型；本进程只经 ComfyUI 的 HTTP 接口上传输入图、提交工作流、下载成品（同文件夹的 _comfy.py；成品目录、路径、预览图用平台的 control_center/media.py）。
只给 image 用 workflows/pixal3d.api.json（单图：抠背景、MoGe 估视角）；另给了 left / back / right 用 pixal3d_multiview.api.json（多角度：每张各自抠背景、裁成方图，没给的角度不接）。
两份工作流由 ComfyUI 模板 3d_pixal3d_trellis2_image_to_model / 3d_pixal3d_multi_views 转成 API 格式（形状 1536³、贴图 4096，同模板），末尾加了 4 个角度的渲染拼图（node preview）。
参数与默认值、上下限同 yaml 的 inputs（网关按 inputs 先查，这里的 pydantic 再查一遍）；单次上限是 yaml 的 timeout（网关到点取消这次调用、中断 ComfyUI 里这一单）。prompt 只记进结果，生成只看图。
"""
import random
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import Field

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center import media
import _comfy

WORKFLOWS = Path(__file__).resolve().parent / "workflows"   # 本子代理用的 ComfyUI 工作流（API 格式）

VIEWS = ("front", "left", "back", "right")
SAMPLERS = ("3", "18", "23", "12")                            # 结构、形状、细化、贴图四次采样，种子都用 seed
TIMEOUT_S = media.subagent_timeout("model3d", 15 * 60) + 60   # 网关按单次上限先取消；这里多 1 分钟只防网关没取消

server = MCPServer("model3d", instructions="本地 Pixal3D 图生 3D")


def build(refs: dict[str, str], seed: int, prefix: str) -> dict[str, Any]:
    """refs 是 {角度: LoadImage 用的相对名}，只有 front 就是单图，否则多角度（没给的角度连同它的抠图、裁切节点一起去掉）。"""
    g = _comfy.template(WORKFLOWS / ("pixal3d.api.json" if list(refs) == ["front"] else "pixal3d_multiview.api.json"))
    if list(refs) != ["front"]:
        cond = g["324"]["inputs"]
        for view in VIEWS:
            if view not in refs:
                crop = cond.pop(view)[0]
                del g[g[crop]["inputs"]["masks"][0]], g[crop], g[f"load_{view}"]
    for view, name in refs.items():
        g[f"load_{view}"]["inputs"]["image"] = name
    for k in SAMPLERS:
        g[k]["inputs"]["seed"] = seed
    g["save"]["inputs"]["filename_prefix"] = f"stb/{media.run_id()}/{prefix}"
    g["preview"]["inputs"]["filename_prefix"] = f"stb/{media.run_id()}/{prefix}-preview"
    return g


@server.tool(name="model3d", description="图片生成 3D 模型（本地 Pixal3D）：一张图，或同一物体正面 + 左 / 后 / 右的多角度图，出带贴图的 GLB")
async def model3d(prompt: str, image: str, left: str = "", back: str = "", right: str = "",
                  seed: Annotated[int, Field(ge=-1)] = -1) -> list[TextContent | ImageContent]:
    paths = {v: p for v, p in zip(VIEWS, (image, left, back, right)) if p}
    if "front" not in paths:
        raise media.MediaError("image（正面图）必填")
    t0 = time.monotonic()
    name = media.next_name("model3d")
    staged = await _comfy.stage_inputs(list(paths.values()), name)
    refs = {v: s[0] for v, s in zip(paths, staged)}
    seed = seed if seed >= 0 else random.randrange(2**48)
    outputs = await _comfy.run(build(refs, seed, name), TIMEOUT_S)
    glb = await _comfy.keep(_comfy.output(outputs, "save", "3d"), name)
    grid = await _comfy.keep(_comfy.output(outputs, "preview", "images"), f"{name}-preview")
    with Image.open(grid) as im:
        info = {"file": media.sandbox_path(glb), "preview": media.sandbox_path(grid), "prompt": prompt, "views": list(paths),
                "mb": round(glb.stat().st_size / 2**20, 1), "seed": seed, "seconds": round(time.monotonic() - t0)}
        return media.result(info, im)


if __name__ == "__main__":
    server.run("stdio")
