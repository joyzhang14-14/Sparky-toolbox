"""video 子代理的程序用的 ComfyUI 客户端（成品目录、路径、预览图用平台的 control_center/media.py）；装自定义节点（install_nodes）给同文件夹的 install.py 用。
ComfyUI 由网关在调用前按子代理的档位拉起、调用后换回主模型，这里只管提交工作流和取成品。
和 ComfyUI 只走 HTTP 接口，不读写它的目录：输入图转成 PNG 用 POST /upload/image 传进它的 input/stb/<run_id>/；
成品由工作流照旧写在它自己的 output/stb/<run_id>/（filename_prefix 是 stb/<run_id>/<成品名>），跑完用 GET /view 下载到 media 目录。
ComfyUI 里的这两份副本不在这里清理：子代理 yaml 的 post_call hook（同文件夹的 comfy_cleanup.py，以平台身份跑）在调用结束后删（同一运行还有调用在途就等最后一个），/clear 也补调它。

路径都在调用时按环境变量算（测试指到临时目录）：
- STB_WORK_ROOT：任务的工作目录（沙箱里外同一个路径）；成品放 media/<run_id>/
- STB_RUN_ID：本次运行 id（只含字母数字和 . _ -）
- STB_MODEL：子代理的模型档位（models.yaml 里的整段配置，JSON，网关只给子代理的程序，gateway.profile_env）：ComfyUI 地址取 endpoint，
  安装目录取 home（没给默认 ~/apps/ComfyUI：工具 cutout 不拿档位，用默认），带 PyAV 的 python 是 <home>/venv/bin/python（ComfyUI 自己的 venv，视频抽帧 / 重编码用）
运行信息由网关一律给；设置本身在 models.yaml，不写进 .env
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from PIL import Image, ImageOps

from control_center.media import IMAGE_EXT, MAX_INPUT_BYTES, MediaError, avail_gb, host_path, media_dir, run_id
from control_center.tool_kit import platform_fault

POLL_S = 2.0
# ComfyUI ResolutionSelector 的宽高比选项
AspectRatio = Literal["1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)", "3:4 (Portrait Standard)", "4:3 (Standard)",
                      "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)"]


def model() -> dict[str, Any]:
    """网关给的模型档位（STB_MODEL）；没有（工具、手动调用）是空的。"""
    try:
        got = json.loads(os.environ.get("STB_MODEL") or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def comfy_url() -> str:
    return str(model().get("endpoint") or "http://127.0.0.1:8188").rstrip("/")


def comfy_home() -> Path:
    return Path(str(model().get("home") or "~/apps/ComfyUI")).expanduser().resolve()


def comfy_python() -> str:
    return str(comfy_home() / "venv" / "bin" / "python")


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


async def run(graph: dict[str, Any], save_node: str, timeout_s: float) -> dict[str, str]:
    """提交工作流（自带 prompt_id）并轮询 /history 等它跑完；超时、连接出错或调用被取消时撤掉排队、中断正在跑的这一单。
    返回成品在 ComfyUI 里的位置 {filename, subfolder, type}（交给 keep 下载）。"""
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
    return _output(entry, save_node)


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


def _output(entry: dict[str, Any], save_node: str) -> dict[str, str]:
    items = ((entry.get("outputs") or {}).get(save_node) or {}).get("images") or []
    if not items:
        raise MediaError(f"ComfyUI 跑完了但保存节点 {save_node} 没有产出文件（ComfyUI 返回的 outputs：{json.dumps(entry.get('outputs'), ensure_ascii=False)[:500]}）")
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


# ---- 装自定义节点（子代理文件夹里的 install.py 用，builder 部署时合入之前跑）----
NODES_FILE = "comfy_nodes.yaml"   # 子代理文件夹里要装的自定义节点清单：[{repo: https://…, ref: commit 或 tag, classes: [节点类名…]}]
START_MIN_GB = 8                  # ComfyUI 原本没在跑时，为试跑起它之前要求的宿主 MemAvailable（GiB；空载的 ComfyUI 不加载模型）
CMD_TIMEOUT_S = 300
START_TIMEOUT_S = 600             # 同 control_center/models.py：起命令之后等就绪的上限


class InstallError(RuntimeError):
    """没装成（已撤回）：说明原样给 builder。"""


def _sh(argv: list[str], what: str, timeout: float = CMD_TIMEOUT_S) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InstallError(f"{what}：{type(e).__name__}: {e}") from None
    if r.returncode != 0:
        raise InstallError(f"{what}：退出码 {r.returncode}\n{(r.stdout + r.stderr).decode(errors='replace')[-2000:]}")
    return r.stdout.decode(errors="replace")


def _get(path: str, timeout: float = 10) -> Any:
    r = httpx.get(comfy_url() + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _up() -> bool:
    try:
        return httpx.get(comfy_url(), timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


def _cmd(which: str) -> None:
    """跑档位的 start / stop 命令（shlex 拆开、~ 展开，不经 shell；同模型切换）。"""
    import shlex

    cmd = str(model().get(which) or "")
    if not cmd:
        raise InstallError(f"ComfyUI 档位没有 {which} 命令")
    argv = shlex.split(cmd)
    argv[0] = os.path.expanduser(argv[0])
    _sh(argv, f"ComfyUI {which}（{cmd}）")


def _start() -> None:
    _cmd("start")
    deadline = time.time() + START_TIMEOUT_S
    while not _up():
        if time.time() >= deadline:
            raise InstallError(f"ComfyUI 起了 {START_TIMEOUT_S} 秒还没就绪（{comfy_url()}）")
        time.sleep(3)


def _freeze(py: str) -> dict[str, str]:
    return {line.split("==", 1)[0].lower(): line for line in _sh([py, "-m", "pip", "freeze"], "pip freeze").splitlines() if "==" in line and not line.startswith("#")}


def _node_dir(repo: str) -> str:
    return re.sub(r"\.git$", "", str(repo).rstrip("/").rsplit("/", 1)[-1])


def install_nodes(folder: Path) -> str:
    """folder/comfy_nodes.yaml 列的、ComfyUI 里还没有的自定义节点：克隆并切到 ref → 有 requirements.txt 就先备份 pip freeze、装进 ComfyUI 的 venv
    → 按档位的 stop / start 重启 ComfyUI（原本没在跑：起来试完再停）→ 试跑：/object_info 里有每个 classes。
    ComfyUI 正在生成、或原本没在跑而可用内存不到 START_MIN_GB 就不动。任何一步不过就撤回（删新节点目录、按备份恢复依赖、恢复 ComfyUI 原来的状态）再抛 InstallError。"""
    import yaml
    os.umask(0o002)   # ~/apps 归 sparkle 组（Joy 与 sparky 共用）：新装的节点与依赖组可写

    f = Path(folder) / NODES_FILE
    nodes = (yaml.safe_load(f.read_text()) or []) if f.is_file() else []
    home, py = comfy_home(), comfy_python()
    todo = [n for n in nodes if not (home / "custom_nodes" / _node_dir(n["repo"])).exists()]
    if not todo:
        return "没有要装的 ComfyUI 节点"
    st: dict[str, Any] = {"nodes": [], "freeze": None, "was_up": _up(), "restarted": False}
    try:
        if st["was_up"]:
            q = _get("/queue")
            if (q or {}).get("queue_running") or (q or {}).get("queue_pending"):
                raise InstallError("ComfyUI 正在生成（/queue 里有任务），没装、没重启；等它空了再交")
        elif (avail := avail_gb()) < START_MIN_GB:
            raise InstallError(f"ComfyUI 原本没在跑，宿主可用内存只有 {avail:.1f} GiB（为试跑起它要至少 {START_MIN_GB} GiB）")
        for n in todo:
            dest = home / "custom_nodes" / _node_dir(n["repo"])
            st["nodes"].append(dest)
            _sh(["git", "clone", "-q", str(n["repo"]), str(dest)], f"克隆节点 {n['repo']}", timeout=900)
            _sh(["git", "-C", str(dest), "checkout", "-q", str(n["ref"])], f"切到节点版本 {n['ref']}")
            if (dest / "requirements.txt").is_file():
                if st["freeze"] is None:
                    st["freeze"] = _freeze(py)
                _sh([py, "-m", "pip", "install", "-q", "-r", str(dest / "requirements.txt")], f"装节点 {dest.name} 的依赖", timeout=1800)
        st["restarted"] = True
        if st["was_up"]:
            _cmd("stop")
        _start()
        info = _get("/object_info", timeout=60)
        if missing := [c for n in todo for c in n.get("classes") or [] if c not in info]:
            raise InstallError(f"ComfyUI 重启后 /object_info 里没有节点 {missing}（节点导入失败，或类名写错）")
        if not st["was_up"]:
            _cmd("stop")                                   # 原本没在跑：试完停掉，用的时候由模型切换起
    except (InstallError, httpx.HTTPError, OSError, ValueError, KeyError) as e:
        raise InstallError(f"ComfyUI 节点没装成，已撤回（{_rollback(st)}）：{e}") from None
    return "ComfyUI 节点装好并试跑通过：" + "、".join(d.name for d in st["nodes"])


def _rollback(st: dict[str, Any]) -> str:
    done, bad = [], []
    for d in st["nodes"]:
        shutil.rmtree(d, ignore_errors=True)
        done.append(f"删了节点 {d.name}")
    if st["freeze"] is not None:
        py = comfy_python()
        try:
            now = _freeze(py)
            if added := sorted(set(now) - set(st["freeze"])):
                _sh([py, "-m", "pip", "uninstall", "-y", "-q", *added], "卸掉新加的包", timeout=900)
            if changed := [st["freeze"][k] for k in st["freeze"] if k in now and now[k] != st["freeze"][k]]:
                _sh([py, "-m", "pip", "install", "-q", "--no-deps", *changed], "装回原来的版本", timeout=1800)
            done.append("依赖已按备份恢复")
        except InstallError as e:
            bad.append(f"恢复依赖出错：{e}（要 Joy 手动处理）")
    if st["restarted"]:
        try:
            if _up():
                _cmd("stop")
            if st["was_up"]:
                _start()
            done.append("ComfyUI 已恢复" + ("在跑" if st["was_up"] else "不在跑"))
        except InstallError as e:
            bad.append(f"恢复 ComfyUI 出错：{e}（要 Joy 手动处理）")
    return "；".join(done + bad) or "没装任何东西"
