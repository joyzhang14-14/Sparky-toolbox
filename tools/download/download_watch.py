"""download 工具批准后的后台下载程序：登记成平台的一次性动态监听（control_center/monitors.py 起、监督它：以 sparky 跑、联网按 download 的 tool.yaml、
崩了按等待重起），独立于发起它的那次运行。规则、状态文件见同目录的 _download.py。

watch(ctx, emit)：ctx["watch"] = 登记时给的 {id, files, dest, profile, source_text, total}。逐个文件下到 dest（已下完的跳过，没下完的从 .part 接着下）、
写 <文件>.meta.json，进度每 PROGRESS_S 秒写一次状态文件；下完或失败都 emit done（注销，不再重起）并私信 Joy 一句结果。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _download as dl  # noqa: E402

PROGRESS_S = 5


def watch(ctx: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    m = ctx["watch"]
    base = Path(m["dest"])
    st = dl.load_status(m["id"]) or {"id": m["id"], "source_text": m["source_text"], "dest": str(base), "total": m["total"]}
    st.update(status="downloading", started=st.get("started") or time.time(), files_done=0, meta=[], done_bytes=0)
    dl.save_status(st)
    last = [time.time()]

    def progress(n: int) -> None:
        st["done_bytes"] += n
        if st.get("total") is not None and st["done_bytes"] > st["total"]:
            st["total"] = None                            # 探到的大小不准：不显示超过 100% 的进度，下完以实际字节为准
        if time.time() - last[0] > PROGRESS_S:
            last[0] = time.time()
            dl.save_status(st)

    try:
        base.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(base).free
        if (sum(f["size"] or 0 for f in m["files"]) - sum((base / f["path"]).stat().st_size for f in m["files"] if (base / f["path"]).exists())) * dl.FREE_MARGIN > free:
            raise dl.DownloadError(f"硬盘不够了：下载目录所在盘只剩 {free / 1e9:.1f} GB。改法：重新申请时用 files 只下需要的文件，或请 Joy 腾出空间后再申请")
        for f in m["files"]:
            target = base / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():                       # 已下完的（重起之前）跳过；没下完的 fetch 从 .part 接着下
                cookies = dl.profile_cookies(m["profile"], f["url"]) if m.get("profile") else None
                dl.fetch(f["url"], target, f.get("sha256"), f.get("git_sha1"), dl._cookie_header(cookies), progress)
            else:
                st["done_bytes"] += target.stat().st_size
            meta = target.with_name(target.name + ".meta.json")
            st["meta"].append({"path": str(target), **(dl.json.loads(meta.read_text()) if meta.exists() else dl.write_meta(target, f["url"]))})
            st["files_done"] += 1
            dl.save_status(st)
        st["status"], st["total"] = "done", st["done_bytes"]
    except (dl.DownloadError, OSError) as e:
        st["status"], st["error"] = "failed", str(e)[:1000]
    st["ended"] = time.time()
    dl.save_status(st)
    files = "、".join(Path(x["path"]).name + ("（不可信内容）" if x.get("untrusted") else "") for x in st["meta"])
    emit({"done": True, "notify": f"✅ 下载完成：{st['source_text']} → {dl.sandbox_path(base)}：{files}" if st["status"] == "done"
          else f"❌ 下载失败：{st['source_text']} → {dl.sandbox_path(base)}：{st.get('error')}"})
