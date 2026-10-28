"""download_install 批准后的后台安装程序：登记成平台的一次性动态监听（control_center/monitors.py 起、监督它：以 sparky 跑、联网按 download 的 tool.yaml），
独立于发起它的那次运行。watch(ctx, emit)：ctx["watch"] = 登记时给的 {id, packages, source_text}；装进沙箱共用的环境（control_center/sandbox_env.install），
状态文件写 installing → done（installed / failed 列出装上与装不上的）或 failed（环境本身建不起来）；完了 emit done（注销）并私信 Joy 一句结果。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _download as dl  # noqa: E402
from control_center import sandbox_env  # noqa: E402


def watch(ctx: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    m = ctx["watch"]
    st = dl.load_status(m["id"]) or {"id": m["id"], "source_text": m["source_text"], "dest": str(sandbox_env.ENV), "packages": m["packages"]}
    st.update(status="installing", started=time.time())
    dl.save_status(st)
    try:
        st["installed"], st["failed"] = sandbox_env.install(m["packages"])
        st["status"] = "done"
    except (RuntimeError, OSError) as e:
        st["status"], st["error"] = "failed", str(e)[:1000]
    st["ended"] = time.time()
    dl.save_status(st)
    if st["status"] == "done":
        text = f"✅ 安装完成：{'、'.join(st['installed']) or '无'}" + (f"；没装上：{'、'.join(st['failed'])}" if st["failed"] else "")
    else:
        text = f"❌ 安装失败：{st['source_text']}：{st['error']}"
    emit({"done": True, "notify": text})
