"""download 工具：先批后下，以 sparky + Landlock 跑（联网按 tool.yaml）。规则、目录、标注、状态文件见同目录的 _download.py。

- download_request：校验、探测大小（HEAD / HuggingFace API，登录才能下的带浏览器登录档案的 cookie）、查硬盘，然后登记成平台的一次性动态监听
  （经 stbcc-hub 的 monitor op，control_center/monitors.py；程序是同目录的 download_watch.py），Joy 在通用审批卡上同意后平台以 sparky 起这个独立的后台下载进程
  （不占这次运行，崩了按等待重起、从 .part 接着下），下完私信 Joy。当场返回下载 id；同一个来源、同一个目录的重复申请返回原来那个。
- download_install：沙箱里缺程序或 Python 包时申请安装，同样登记成一次性动态监听（程序是同目录的 install_watch.py），Joy 批准后以 sparky 装进沙箱共用的环境
  （control_center/sandbox_env.py：先 conda-forge 再 pip、逐个装），装完私信 Joy。
- download_status：读下载目录里的状态文件（进度、各文件格式与是否不可信；安装的是装上 / 装不上的包）；还在等批 / 被拒 / 被取消看平台的登记。
"""
import json
import os
import shutil
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _download as dl  # noqa: E402
from control_center import hub_client, monitors, sandbox_env  # noqa: E402

TOOL = "download"
WATCHER = "tools/download/download_watch.py"     # 批准后的后台下载程序（仓库相对路径，登记时交给平台）
INSTALLER = "tools/download/install_watch.py"    # 批准后的后台安装程序


class HFRepo(BaseModel):
    repo: str = Field(description="HuggingFace 仓库，如 Qwen/Qwen3-8B")
    revision: str = Field("main", description="分支或 commit（写 commit 最稳）")
    files: str = Field("", description="只下匹配的文件（glob，如 *.safetensors）；不写 = 整个仓库")


server = MCPServer("download", instructions="下载文件 / 模型：Joy 批准后下到统一的下载目录 /home/sparky/workspace/_download")


@server.tool(name="download_request", description="申请下载一个文件或 HuggingFace 仓库，Joy 批准后下载，返回下载 id")
async def download_request(source: str | HFRepo, reason: str, folder: str = "", sha256: str = "", profile: str = "") -> str:
    import hashlib
    import re

    src = source.model_dump() if isinstance(source, HFRepo) else source
    run = os.environ.get("STB_RUN_REF", "")
    try:
        if not str(reason).strip():
            raise dl.DownloadError("reason 写清为什么要下这个（卡上给 Joy 看）")
        if sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise dl.DownloadError("sha256 写 64 位十六进制")
        dest = dl.dest_dir(folder)
        cookies = dl.profile_cookies(profile.strip(), src) if profile.strip() and isinstance(src, str) else None
        files = dl.plan_files(src, cookies)
        if sha256 and len(files) == 1:
            files[0]["sha256"] = sha256.lower()
        for f in files:
            target = (dest / f["path"]).resolve()
            if dest.resolve() not in target.parents:
                raise dl.DownloadError(f"文件路径 {f['path']} 越出了目标目录")
            if target.exists():
                raise dl.DownloadError(f"{dl.sandbox_path(dest / f['path'])} 已经存在，不覆盖（换个 folder，或先确认旧文件是不是就是要的）")
        total, known = dl.total_of(files), sum(f["size"] or 0 for f in files)
        probe = dest
        while not probe.exists():
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if known * dl.FREE_MARGIN > free:
            raise dl.DownloadError(f"硬盘不够：要 {known / 1e9:.1f} GB（再留 {dl.FREE_MARGIN - 1:.0%} 余量），下载目录所在盘剩 {free / 1e9:.1f} GB，没发卡\n"
                                   "改法：HuggingFace 仓库用 files 只下需要的文件（如 *.safetensors 里要的那个）；或用 ask_user 请 Joy 腾出空间后再申请")
    except dl.DownloadError as e:
        raise ToolError(str(e)) from None
    text = dl.source_text(src)
    names = "\n".join(f"- {f['path']}（{dl.gb(f['size'])}）" for f in files[:20]) + (f"\n…共 {len(files)} 个文件" if len(files) > 20 else "")
    key = hashlib.sha256(json.dumps([src, str(dest)], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    first = Path(str(files[0]["path"])).name if files else text
    r, why = await hub_client.call_detail("monitor", run_ref=run, do="create", args={
        "name": f"下载 {text}"[:40], "tool": TOOL, "program": WATCHER, "key": key,
        "title": f"☁️ 下载 {first}" + (f" 等 {len(files)} 个文件" if len(files) > 1 else ""),
        "action": f"下载 {text}（合计 {dl.gb(total)}）到 {dest}",
        "detail": f"{names}\n硬盘剩余 {free / 1e9:.0f} GB\n理由（模型写的）：{str(reason).strip()[:600]}",
        "ctx": {"files": files[:500], "dest": str(dest), "profile": profile.strip() if cookies is not None else "", "source_text": text, "total": total}})
    if not (r or {}).get("ok"):
        raise ToolError(str((r or {}).get("error") or f"stbcc-hub 没响应（{why}），没发卡；可以稍后再试"))
    m = r["monitor"]
    if not dl.load_status(m["id"]):
        dl.save_status({"id": m["id"], "status": "pending", "source_text": text, "dest": str(dest), "total": total, "done_bytes": 0, "files_done": 0})
    return json.dumps({"id": m["id"], "status": "pending" if m["status"] == "pending" else m["status"], "files": len(files), "total_gb": None if total is None else round(total / 1e9, 2),
                       "dest": dl.sandbox_path(dest), "host_dest": str(dest),
                       "note": ("这个下载已经申请过了；" if r.get("existing") else "")
                       + "已请 Joy 批准；用 download_status(id) 查进度；他拒绝了会把意见告诉你"},
                      ensure_ascii=False)


@server.tool(name="download_install", description="沙箱里缺程序或 Python 包时申请安装（先 conda-forge 再 pip），Joy 批准后装进沙箱共用的环境，返回 id")
async def download_install(packages: list[str], reason: str) -> str:
    import hashlib

    try:
        pkgs = sandbox_env.check(packages)
    except ValueError as e:
        raise ToolError(str(e)) from None
    if not str(reason).strip():
        raise ToolError("reason 写清哪个脚本 / 哪一步要用（卡上给 Joy 看）")
    names = "、".join(pkgs)
    for f in sorted((dl.paths.DOWNLOADS / dl.STATUS).glob("*.json")):     # 同样的包已经在装 / 装好了：不再发卡（批准后模型常常没等装完就再申请）
        st = dl.load_status(f.stem) or {}
        if sorted(st.get("packages") or []) == sorted(pkgs) and (st.get("status") == "installing" or (st.get("status") == "done" and not st.get("failed"))):
            note = "已经装好了，直接在 bash 里用" if st["status"] == "done" else "正在装：用 download_status(id) 查到 done 再用，不要重复申请"
            return json.dumps({"id": st["id"], "status": st["status"], "packages": pkgs, "note": note}, ensure_ascii=False)
    r, why = await hub_client.call_detail("monitor", run_ref=os.environ.get("STB_RUN_REF", ""), do="create", args={
        "name": f"安装 {names}"[:40], "tool": TOOL, "program": INSTALLER, "key": hashlib.sha256(json.dumps(sorted(pkgs)).encode()).hexdigest(),
        "title": f"☁️ 安装 {names}",
        "action": f"在沙箱里安装 {names}",
        "detail": f"先从 conda-forge 装，装不上用 pip；装进沙箱共用的环境 {sandbox_env.ENV}，所有任务的沙箱都能用\n理由（模型写的）：{str(reason).strip()[:600]}",
        "ctx": {"packages": pkgs, "source_text": f"安装 {names}"}})
    if not (r or {}).get("ok"):
        raise ToolError(str((r or {}).get("error") or f"stbcc-hub 没响应（{why}），没发卡；可以稍后再试"))
    m = r["monitor"]
    if not dl.load_status(m["id"]):
        dl.save_status({"id": m["id"], "status": "pending", "source_text": f"安装 {names}", "dest": str(sandbox_env.ENV), "packages": pkgs})
    return json.dumps({"id": m["id"], "status": "pending" if m["status"] == "pending" else m["status"], "packages": pkgs,
                       "note": ("这个安装已经申请过了；" if r.get("existing") else "") + "已请 Joy 批准；用 download_status(id) 查进度；装好后直接在 bash 里用"},
                      ensure_ascii=False)


@server.tool(name="download_status", description="查一次下载的进度：pending（等 Joy 批）/ downloading / done（各文件的格式与是否不可信）/ failed / rejected（Joy 的意见）/ cancelled")
async def download_status(id: str) -> str:
    try:
        dl.status_file(id)                              # 校验 id 的格式
        st = dl.load_status(id)
    except dl.DownloadError as e:
        raise ToolError(str(e)) from None
    m = monitors.load(id)
    if st is None or m is None or m.get("tool") != TOOL or m.get("task") != os.environ.get("STB_RUN_REF", "/").split("/", 1)[0]:
        raise ToolError(f"本任务没有下载 {id}（id 是 download_request 返回的 12 位十六进制，照原样传）")
    if st["status"] == "pending" and m["status"] in ("rejected", "cancelled"):
        st = {**st, "status": m["status"], **({"feedback": m["feedback"]} if m.get("feedback") else {})}
    out = {k: st.get(k) for k in ("id", "status", "source_text", "total", "done_bytes", "files_done", "installed", "failed", "error", "feedback") if st.get(k) is not None}
    out["dest"] = dl.sandbox_path(Path(st["dest"]))
    if st.get("meta"):
        out["files"] = [{**x, "path": dl.sandbox_path(Path(x["path"]))} for x in st["meta"]]
    return json.dumps(out, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
