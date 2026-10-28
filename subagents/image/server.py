"""image 子代理（generator，subagents/image/subagent.yaml）的程序：用本地 ComfyUI 跑 Qwen-Image 2.1 Uncensored，文生图或改图。
主模型调用 image(prompt, …inputs)：网关先卸下主模型、按 yaml 的 runtime + model 拉起 ComfyUI，调用后换回主模型；本进程只经 ComfyUI 的 HTTP 接口上传输入图、提交工作流、下载成品（同文件夹的 _comfy.py；成品目录、路径、预览图用平台的 control_center/media.py）。
不给输入图用 subagents/image/workflows/qwen_image_t2i.api.json；给 1–10 张用 qwen_image_edit.api.json（第 1 张是要改的图，resolution 1024）。
参数与默认值、上下限同 yaml 的 inputs（网关按 inputs 先查，这里的 pydantic 再查一遍）；单次上限是 yaml 的 timeout（网关到点取消这次调用、中断 ComfyUI 里这一单）。环境变量见 _comfy.py。
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

MAX_IMAGES = 10
TIMEOUT_S = media.subagent_timeout("image", 15 * 60) + 60     # 网关按单次上限先取消；这里多 1 分钟只防网关没取消
SAVE_NODE = "461"

server = MCPServer("image", instructions="本地 Qwen-Image 2.1 文生图 / 改图")


def build(prompt: str, negative_prompt: str, refs: list[str], aspect_ratio: str, megapixels: float, steps: int, seed: int,
          prefix: str) -> dict[str, Any]:
    """按有无输入图选模板并填参数。refs 是 LoadImage 用的相对名，第 1 张接 images.image_1（要改的图）。"""
    if not refs:
        g = _comfy.template(WORKFLOWS / "qwen_image_t2i.api.json")
        enc = g["459:452"]["inputs"]
        g["13"]["inputs"].update(aspect_ratio=aspect_ratio, megapixels=megapixels)
    else:
        g = _comfy.template(WORKFLOWS / "qwen_image_edit.api.json")
        enc = g["459:474"]["inputs"]
        for k in [k for k, n in g.items() if n["class_type"] == "LoadImage"]:   # 模板里的示例图换成本次的输入图
            del g[k]
        for k in [k for k in enc if k.startswith("images.")]:
            del enc[k]
        for i, name in enumerate(refs, 1):
            g[f"image_{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            enc[f"images.image_{i}"] = [f"image_{i}", 0]
    enc.update(prompt=prompt, negative_prompt=negative_prompt)
    g["459:458"]["inputs"].update(seed=seed, steps=steps)
    g[SAVE_NODE]["inputs"]["filename_prefix"] = f"stb/{media.run_id()}/{prefix}"
    return g


@server.tool(name="image", description="画图 / 改图（本地 Qwen-Image 2.1，无审查版）：文生图，或按 1–10 张图改图")
async def image(prompt: str, images: list[str] = [], negative_prompt: str = "",  # noqa: B006 — MCP 参数默认值
                aspect_ratio: _comfy.AspectRatio = "1:1 (Square)",
                megapixels: Annotated[float, Field(ge=0.25, le=4.0)] = 1.0,
                steps: Annotated[int, Field(ge=1, le=50)] = 25,
                seed: Annotated[int, Field(ge=-1)] = -1) -> list[TextContent | ImageContent]:
    if not prompt.strip():
        raise media.MediaError("prompt 不能为空")
    if len(images) > MAX_IMAGES:
        raise media.MediaError(f"images 最多 {MAX_IMAGES} 张")
    t0 = time.monotonic()
    name = media.next_name("image")
    refs = [r[0] for r in await _comfy.stage_inputs(images, name)]
    seed = seed if seed >= 0 else random.randrange(2**48)
    out = await _comfy.run(build(prompt, negative_prompt, refs, aspect_ratio, megapixels, steps, seed, name), SAVE_NODE, TIMEOUT_S)
    dst = await _comfy.keep(out, name)
    with Image.open(dst) as im:
        info = {"file": media.sandbox_path(dst), "width": im.width, "height": im.height, "seed": seed, "steps": steps,
                "seconds": round(time.monotonic() - t0)}
        return media.result(info, im)


if __name__ == "__main__":
    server.run("stdio")
