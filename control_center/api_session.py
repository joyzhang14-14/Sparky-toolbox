"""算力 API 的会话（任务 api-<名> 唯一的一步：任务文件夹里的 hold.py 只调 hold()）与门口、会话共用的文件（门口在 control_center/api_door.py）。

- 会话：workflow 的一步「要资源 + 模型档位」，开跑时模型已由平台加载好。以 sparky 跑、不联网，只读写下面几个文件：
  · 就绪标记 $STB_RUN_DIR/session.json（运行目录 steps/ 下）：{started（这次会话第一段开始的时刻，续跑不变）, ready, pid}；ready 为 true 时门口才转发。
  · 门口写的 data/hub/api/live/<名>.json：{last_request（最后一次请求的时刻）, inflight（在答的路数）}。
  · 用量账本 data/hub/api/usage/<日期>.jsonl：门口每条请求记一行（时刻、API、方法、路径、状态码、用时、token；不记内容）。
- IDLE_S 秒没有请求 → 退出码 3（没事可做：complete，不发消息）。SIGTERM（被抢占、stb pause、/stop）→ 同样先停接新请求；
  最近 IDLE_S 秒有请求的以 75 退出（之后自动续），没有的以 3 退出。停接新请求后等在答的答完（最多 DRAIN_S 秒）再退出。
  退出前把这次会话的汇总（起止、请求数、token）打进步骤日志：控制台的运行页显示。
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from control_center.paths import HUB

IDLE_S = 300            # 会话闲多久结束（同问题卡就地等的 5 分钟）
DRAIN_S = 120           # 停接新请求后，等在答的请求答完最多多久
POLL_S = 2
SESSION_FILE = "session.json"
NOTHING_EXIT, PAUSE_EXIT = 3, 75   # 同 runner.NOTHING_EXIT_CODE、PAUSE_EXIT_CODE（这里不 import runner：会话进程只要这几个文件）


def api_dir() -> Path:
    """data/hub/api/：<名>.json（端口、对外方式、密钥的 sha256）、live/、usage/。"""
    return HUB / "api"


def live_file(name: str) -> Path:
    return api_dir() / "live" / f"{name}.json"


def usage_file(day: str) -> Path:
    return api_dir() / "usage" / f"{day}.jsonl"


def read_json(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def write_json(p: Path, data: dict[str, Any]) -> None:
    """原子写（临时文件 + 改名）：读的一方不会读到写了一半的。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, p)


def usage_rows(name: str, since: float, until: float | None = None) -> list[dict[str, Any]]:
    """账本里 API name 在 since 之后（给了 until 就到 until 为止）的请求：按本地日期一天一个文件，从 since 那天读到今天。"""
    out, day, last = [], date.fromtimestamp(since), date.fromtimestamp(until or time.time())
    while day <= last:
        try:
            lines = usage_file(day.isoformat()).read_text().splitlines()
        except OSError:
            lines = []
        for l in lines:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("api") == name and since <= float(r.get("ts") or 0) <= (until or float("inf")):
                out.append(r)
        day += timedelta(days=1)
    return out


def _wan(n: int) -> str:
    return f"{n / 10000:.1f} 万" if n >= 10000 else str(n)


def summary(name: str, started: float, now: float | None = None) -> str:
    """一次会话的汇总（打进步骤日志）：起止、请求数、输入 / 输出 token。"""
    rows = usage_rows(name, started)
    now = now or time.time()
    t = lambda x: time.strftime("%m-%d %H:%M", time.localtime(x))   # noqa: E731
    ok = [r for r in rows if int(r.get("status") or 0) < 400]
    tin, tout, tc = (sum(int(r.get(k) or 0) for r in ok) for k in ("input", "output", "cached"))
    return (f"会话 {t(started)} – {t(now)}（{(now - started) / 60:.0f} 分钟）：{len(rows)} 次请求（成功 {len(ok)}），"
            f"输入 {_wan(tin)} / 输出 {_wan(tout)} token" + (f"（输入里缓存命中 {_wan(tc)}）" if tc else ""))


def hold() -> None:
    """会话（hold.py 调）：写就绪 → 等到 IDLE_S 秒没有请求或收到 SIGTERM → 停接新请求、等在答的答完 → 打汇总 → 以 3 或 75 退出。"""
    name, sdir = os.environ["STB_TASK"], Path(os.environ["STB_RUN_DIR"])
    stop: list[int] = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    sess = sdir / SESSION_FILE
    started = float(read_json(sess).get("started") or time.time())
    seg = time.time()
    write_json(sess, {"started": started, "ready": True, "pid": os.getpid()})
    print(f"会话就绪：门口开始转发请求（{IDLE_S // 60} 分钟没有请求就结束）", flush=True)
    last = 0.0
    while not stop:
        last = float(read_json(live_file(name)).get("last_request") or 0)
        if time.time() - max(seg, last) > IDLE_S:
            break
        time.sleep(POLL_S)
    write_json(sess, {"started": started, "ready": False, "pid": os.getpid()})
    t0 = time.time()
    while read_json(live_file(name)).get("inflight") and time.time() - t0 < DRAIN_S:
        time.sleep(0.5)
    last = float(read_json(live_file(name)).get("last_request") or 0)
    if stop and time.time() - last <= IDLE_S:
        print(f"被打断：最近 {IDLE_S // 60} 分钟有请求，轮到时接着开会话", flush=True)
        sys.exit(PAUSE_EXIT)
    print(summary(name, started), flush=True)
    sys.exit(NOTHING_EXIT)
