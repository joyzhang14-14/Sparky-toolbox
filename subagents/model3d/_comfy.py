"""model3d 子代理的程序用的 ComfyUI 客户端（从 subagents/image/_comfy.py 复制：去掉装自定义节点，成品按保存节点的输出键取，一次可取几个）；成品目录、路径、预览图用平台的 control_center/media.py。
ComfyUI 由网关在调用前按子代理的档位拉起、调用后换回主模型，这里只管提交工作流和取成品。
和 ComfyUI 只走 HTTP 接口，不读写它的目录：输入图转成 PNG 用 POST /upload/image 传进它的 input/stb/<run_id>/；
成品由工作流照旧写在它自己的 output/stb/<run_id>/（filename_prefix 是 stb/<run_id>/<成品名>），跑完用 GET /view 下载到 media 目录。
ComfyUI 里的这两份副本不在这里清理：子代理 yaml 的 post_call hook（同文件夹的 comfy_cleanup.py，以平台身份跑）在调用结束后删（同一运行还有调用在途就等最后一个），/clear 也补调它。

路径都在调用时按环境变量算（测试指到临时目录）：
- STB_WORK_ROOT：任务的工作目录（沙箱里外同一个路径）；成品放 media/<run_id>/
- STB_RUN_ID：本次运行 id（只含字母数字和 . _ -）
- STB_MODEL：子代理的模型档位（models.yaml 里的整段配置，JSON，网关只给子代理的程序，gateway.profile_env）：ComfyUI 地址取 endpoint
运行信息由网关一律给；设置本身在 models.yaml，不写进 .env
"""
import contextlib
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import anyio
import httpx
from PIL import Image, ImageOps

from control_center.media import IMAGE_EXT, MAX_INPUT_BYTES, MediaError, host_path, media_dir, run_id
from control_center.tool_kit import platform_fault

POLL_S = 2.0


def model() -> dict[str, Any]:
    """网关给的模型档位（STB_MODEL）；没有（工具、手动调用）是空的。"""
    try:
        got = json.loads(os.environ.get("STB_MODEL") or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def comfy_url() -> str:
    return str(model().get("endpoint") or "http://127.0.0.1:8188").rstrip("/")


def template(path: Path) -> dict[str, Any]:
    """工作流（ComfyUI「Save (API)」格式的 json）：放在用它的子代理文件夹的 workflows/ 里，调用方给路径。"""
    return json.loads(Path(path).read_text())


def _png(host: Path, p: str) -> tuple[bytes, int, int]:
    """校验一张输入图（工作目录下、png / jpg / webp / gif、≤ 20 MB），按 EXIF 转正后编码成 PNG。"""
    if host.suffix.lower() not in IMAGE_EXT:
        raise MediaError(f"只收 png / jpg / webp / gif 图片：{p}")
    if not host.is_file():
        raise MediaError(f"文件不存在：{p}")
    if host.stat().st_size > MAX_INPUT_BYTES:
        raise MediaError(f"图片超过 20 MB：{p}（先用 render_png 缩小或换一张）")
    try:
        with Image.open(host) as im:
            im = ImageOps.exif_transpose(im)       # 手机照片按 EXIF 方向转正，宽高比才对
            im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise MediaError(f"读不了图片 {p}：{e}") from None
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), im.width, im.height


async def stage_inputs(paths: list[str], name: str) -> list[tuple[str, int, int]]:
    """校验输入图并转成 PNG，用 POST /upload/image 传进 ComfyUI 的 input/stb/<run_id>/（文件名 <成品名>-<序号>.png）。
    先全部校验完再上传（有一张不合格就一张都不传）。返回 [(LoadImage 用的相对名, 宽, 高)]，顺序同 paths。"""
    pngs = [_png(host_path(p), p) for p in paths]
    if not pngs:
        return []
    sub, out = f"stb/{run_id()}", []
    async with _client() as c:
        for i, (data, w, h) in enumerate(pngs, 1):
            try:
                r = await c.post("/upload/image", files={"image": (f"{name}-{i}.png", data, "image/png")},
                                 data={"type": "input", "subfolder": sub, "overwrite": "true"})
            except httpx.HTTPError as e:
                raise platform_fault(f"上传输入图到 ComfyUI（{comfy_url()}/upload/image）出错：{e!r}") from None
            if r.status_code != 200:
                raise MediaError(f"ComfyUI 拒收输入图 {paths[i - 1]}（HTTP {r.status_code}）：{r.text[:500]}")
            got = r.json()
            out.append((f"{got.get('subfolder') or sub}/{got['name']}", w, h))
    return out


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=comfy_url(), timeout=30)


async def run(graph: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """提交工作流（自带 prompt_id）并轮询 /history 等它跑完；超时、连接出错或调用被取消时撤掉排队、中断正在跑的这一单。
    返回 /history 里这一单的 outputs（用 output 取各保存节点的成品位置，交给 keep 下载）。"""
    pid = str(uuid.uuid4())
    late = f"生成超过 {timeout_s / 60:g} 分钟还没完成，已中断"
    async with _client() as c:
        try:
            (await c.get("/system_stats", timeout=10)).raise_for_status()
        except httpx.HTTPError as e:
            raise platform_fault(f"连不上 ComfyUI（{comfy_url()}）：{e!r}") from None
        try:
            r = await c.post("/prompt", json={"prompt": graph, "prompt_id": pid, "client_id": "stb"})
            if r.status_code != 200:
                raise MediaError(f"ComfyUI 拒绝了工作流：{r.text[:1000]}")
            entry = await _wait(c, pid, timeout_s, late)
        except BaseException as e:
            with anyio.move_on_after(10, shield=True):                   # 被取消时也要发出去
                for path, body in (("/queue", {"delete": [pid]}),         # 还在排队就撤掉
                                   ("/interrupt", {"prompt_id": pid})):   # 正在跑就中断
                    with contextlib.suppress(httpx.HTTPError):
                        await c.post(path, json=body)
            if isinstance(e, httpx.HTTPError):
                raise platform_fault(f"提交到 ComfyUI 出错：{e!r}") from None
            raise
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        raise MediaError(f"ComfyUI 执行出错：{_error_text(status)}")
    return entry.get("outputs") or {}


async def _wait(c: httpx.AsyncClient, pid: str, timeout_s: float, late: str) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            entry = (await c.get(f"/history/{pid}")).json().get(pid)
        except (httpx.HTTPError, ValueError) as e:
            raise platform_fault(f"查询 ComfyUI 进度出错：{e!r}") from None
        status = (entry or {}).get("status") or {}
        if status.get("completed") or status.get("status_str") == "error":
            return entry
        if time.monotonic() >= deadline:
            raise MediaError(late)
        await anyio.sleep(POLL_S)


def _error_text(status: dict[str, Any]) -> str:
    for kind, data in status.get("messages") or []:
        if kind == "execution_error":
            return f"{data.get('node_type', '')}: {data.get('exception_message', '')}".strip()[:1000]
        if kind == "execution_interrupted":
            return "被中断（ComfyUI 收到了 interrupt，可能是有人在 ComfyUI 界面点了取消，或调用被取消）"
    return "ComfyUI 没给出错节点，原始状态：" + json.dumps(status, ensure_ascii=False)[:800]


def output(outputs: dict[str, Any], save_node: str, key: str) -> dict[str, str]:
    """保存节点的成品位置 {filename, subfolder, type}：SaveImage 的在 images 键，Save3DAdvanced 的在 3d 键。"""
    items = (outputs.get(save_node) or {}).get(key) or []
    if not items:
        raise MediaError(f"ComfyUI 跑完了但保存节点 {save_node} 没有产出文件（ComfyUI 返回的 outputs：{json.dumps(outputs, ensure_ascii=False)[:500]}）")
    it = items[0]
    return {"filename": str(it.get("filename", "")), "subfolder": str(it.get("subfolder", "")), "type": str(it.get("type") or "output")}


async def keep(item: dict[str, str], name: str) -> Path:
    """用 GET /view 把 ComfyUI 的成品下载成 media/<run_id>/<成品名>.<扩展名>（ComfyUI output 里那份留着）。"""
    dst = media_dir() / f"{name}{Path(item['filename']).suffix.lower()}"
    part = dst.with_name(dst.name + ".part")
    async with _client() as c:
        try:
            async with c.stream("GET", "/view", params=item) as r:
                if r.status_code != 200:
                    raise MediaError(f"从 ComfyUI 取成品 {item} 失败（HTTP {r.status_code}）：{(await r.aread())[:300].decode(errors='replace')}")
                with part.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise platform_fault(f"从 ComfyUI（{comfy_url()}/view）下载成品 {item} 出错：{e!r}") from None
    part.replace(dst)
    return dst
