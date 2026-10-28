"""cutout 工具：抠图。用 ComfyUI 自带的 BiRefNet（models/background_removal/birefnet.safetensors）把主体从背景里分出来，
输出透明底 PNG 和蒙版（白 = 保留）。每次调用请 stbcc-docker 起一个一次性 stb-docker-base 容器（ComfyUI venv 的 python 跑 _cutout_run.py，
media.run_gpu），跑完就退、释放显存；
不起 ComfyUI 服务、不换模型，和主模型同时跑（约 1–3.5 GB）。开跑前查可用内存，不够（或跑的时候显存不足）就报平台故障，写明可用与需要的内存，由主模型决定稍后重试还是换输入。
时限默认 TIMEOUT_S，主模型可以按调用给 timeout（不设上限）。
成品存工作目录 media/<运行 id>/cutout-N.png 与 cutout-N-mask.png，结果带一张预览图（透明处垫灰色）。
环境变量：STB_WORK_ROOT、STB_RUN_ID、STB_RUN_REF（网关一律给；容器属于这次运行，运行结束就停）；ComfyUI 目录和 python 用 _comfy.py 的默认（~/apps/ComfyUI、它的 venv）。
"""
import json
import subprocess
import time
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from pydantic import Field

from control_center.tool_kit import DISK_DOC, REASON_DOC, TIMEOUT_DOC, call_timeout, platform_fault, timeout_note, tool_timeout
from control_center import media
import _comfy

HELPER = Path(__file__).resolve().with_name("_cutout_run.py")
MODEL = "models/background_removal/birefnet.safetensors"
TIMEOUT_S = 180
MIN_AVAIL_GB = 6                     # 开跑前宿主可用内存下限（模型 + 推理约 1–3.5 GB，留余量给主模型）

server = MCPServer("cutout", instructions="BiRefNet 抠图：透明底 PNG + 蒙版")


def _avail_gb() -> float:
    return media.avail_gb()


@server.tool(name="cutout_run", description=(
    "抠图：把图片里的主体（人、动物、商品、物体）从背景里分出来，输出透明底 PNG 和蒙版；"
    "换背景、做贴纸 / 海报素材时用"))
def cutout_run(image: str, trim: bool = True, timeout: Annotated[int | str, Field(description=TIMEOUT_DOC.format(default="180 秒"))] = 0, disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> list[TextContent | ImageContent]:
    limit = call_timeout(timeout, TIMEOUT_S)
    src = media.host_path(image)
    if src.suffix.lower() not in media.IMAGE_EXT:
        raise media.MediaError(f"只收 png / jpg / webp / gif 图片：{image}")
    if not src.is_file():
        raise media.MediaError(f"文件不存在：{image}")
    if src.stat().st_size > media.MAX_INPUT_BYTES:
        raise media.MediaError(f"图片超过 20 MB：{image}（先用 render_png 缩小或换一张）")
    home = _comfy.comfy_home()
    if not (home / MODEL).is_file():
        raise platform_fault(f"抠图模型没装：{home / MODEL}（要 Joy 装好；这次先用别的办法，或用问题卡告诉 Joy）")
    avail = _avail_gb()
    if avail < MIN_AVAIL_GB:
        raise platform_fault(media.low_memory("抠图没开跑", avail, MIN_AVAIL_GB))
    name = media.next_name("cutout")
    d = media.media_dir()
    rgba, mask = d / f"{name}.png", d / f"{name}-mask.png"
    t0 = time.monotonic()
    try:
        r = media.run_gpu("cutout", [_comfy.comfy_python(), str(HELPER), str(home), str(home / MODEL), str(src), str(rgba), str(mask), "1" if trim else "0"],
                           read=[media.work_root(), home, HELPER], write=[d], timeout=limit, need_gb=MIN_AVAIL_GB, cwd=home, disk=disk, reason=reason)
    except subprocess.TimeoutExpired:
        raise tool_timeout(timeout_note("抠图", time.monotonic() - t0, limit, bool(timeout))) from None
    except OSError as e:
        raise platform_fault(f"抠图容器没起来（stbcc-docker：{e}）") from None
    lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    if r.returncode != 0 or not lines or not rgba.is_file():
        err = (r.stderr.strip().splitlines() or ["（没有 stderr）"])[-1][:300]
        if "cannot identify image file" in r.stderr or "UnidentifiedImageError" in r.stderr:
            raise media.MediaError(f"读不了图片 {image}：{err}")
        raise platform_fault(f"抠图进程没有正常结束（退出码 {r.returncode}）：{err}")
    out = json.loads(lines[-1])
    info = {"file": media.sandbox_path(rgba), "mask": media.sandbox_path(mask), "width": out["width"], "height": out["height"],
            "source_size": out.get("source_size"), "box": out.get("box"), "foreground": out["foreground"], "seconds": round(time.monotonic() - t0, 1)}
    return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False)), media.preview(rgba)]


if __name__ == "__main__":
    server.run("stdio")
