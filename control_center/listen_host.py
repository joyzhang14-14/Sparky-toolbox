"""监听程序的宿主：以 sparky 跑（control_center/listen.py 起它），载入任务文件夹里的 listen.py（或登记来的常驻监听程序），调 check / on_event / watch，把结果写回。

- stdin 一行 JSON：{"repo": 仓库根, "file": 程序的路径, "fn": "check" / "on_event" / "watch", "args": [位置参数…]}。
- stdout 最后一行 JSON：{"ok": true, "result": 函数的返回值} 或 {"ok": false, "error": "…"}。listen.py 里的 print 转到 stderr（记进监听日志），不会弄乱结果。
- watch(ctx, emit)：常驻。args 只给 ctx，宿主补上 emit；每调一次 emit(data)，stdout 写一行 {"emit": data}（data 要能转成 JSON，否则 emit 抛 TypeError 给程序）。
  读完请求后 stdin 一直开着：中枢关掉它（读到 EOF）= 请程序退出，宿主立刻结束进程。watch 返回或出错时照样写最后一行 {"ok": …}。
- 任务文件夹（程序所在目录）在 sys.path 最前（能 import 同文件夹里的模块），其次是仓库根（能 import control_center）。
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import threading
import traceback


def main() -> None:
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    sys.stdout = sys.stderr                      # listen.py 的 print 进日志，不进结果
    lock = threading.Lock()

    def write(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with lock:
            out.write(line + "\n")
            out.flush()

    def emit(data: object) -> None:
        try:
            write({"emit": data})
        except (TypeError, ValueError) as e:
            raise TypeError(f"emit 的内容不能转成 JSON：{e}") from None

    try:
        req = json.loads(sys.stdin.readline())
        sys.path[:0] = [os.path.dirname(req["file"]), req["repo"]]
        ns = runpy.run_path(req["file"], run_name="stb_listen")
        args = req["args"]
        if req["fn"] == "watch":                 # 常驻：stdin 读到 EOF（中枢关掉它）就结束整个进程
            threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
            args = [*args, emit]
        res = {"ok": True, "result": ns[req["fn"]](*args)}
    except BaseException as e:  # noqa: BLE001 — 任何错误都作为结果交回平台（平台记失败、节流通知）
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:1000]}
    try:
        write(res)
    except (TypeError, ValueError) as e:
        write({"ok": False, "error": f"返回值不能转成 JSON：{e}"})


if __name__ == "__main__":
    main()
