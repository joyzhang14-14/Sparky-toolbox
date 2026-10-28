"""read-email 的已读位置（平台不管已读位置，全在本工具这里和它的 run_end hook 里）。每个邮箱一份，各任务共用。

- 正本：data/tool_state/read-email/<邮箱>.json {"last_seen": epoch}（<邮箱> 是邮箱的键，即 mailboxes/ 里的文件名，如 uci、163）。
  工具运行时只读它当起点：同一次运行里起点不变，重取拿到同一批（失败可重试）。
- 读过 = read-email_new 的调用成功（邮件交出去）：每次成功交信，把这批每封的时间和这次交出的是第几封写进本次运行的工具目录
  {run}/read-email/<邮箱>.json（环境变量 STB_TOOL_RUN_DIR；write_position），不碰正本：
  {"last_seen": 这批最新一封的时间, "ts": [这批每封的时间，按编号从旧到新], "served": [[a, b, 第 b 封的时间], …]}（served：每次成功交出的是第 a–b 封；
  这批没有新邮件也写一份 ts 为空的）。调用失败（报错）不写 served，在 {run}/read-email/_failed.json 记一条（call_failed，带邮箱）。
- 写回：run_end hook（run_end），运行 complete 才写：每个邮箱从第 1 封起连续交出去的前 k 封算读过（advance）——
  全交出去推进到最后一封的时间；没全交出去推进到「第 k+1 封的时间 − 1 秒」（第 k+1 封起下次重读，同一秒到的几封宁可重读也不漏）；k = 0 不动。
  只往前推；只写本工具的状态目录；文件名由 hook 定；不跟符号链接。
  某个邮箱有失败的调用、且它这批邮件没有全部交出去（或失败后一直没取成）→ 照上面推进交出去的部分，stdout 输出 {"status": "failed", "error": "<邮箱>没读完，…"}，
  平台把运行改判 failed（原因只写没读完的邮箱）、照常发 ❌ 通知；失败后重试取成、整批都交出去了，不改判。
- 身份：read-email 是 is_editable: false，hook 以平台身份跑（control_center/hooks.py）；状态目录在 data/tool_state/ 下（整个对 sparky 读写）。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

FAILED = "_failed.json"                       # 失败记录的文件名（不会和位置文件撞：位置文件名不以 _ 开头）
NAME = re.compile(r"[A-Za-z0-9@.+-][A-Za-z0-9@._+-]*\.json")   # 位置文件名：邮箱 + .json


def tool_state(tool: str) -> Path:
    """工具的状态目录 data/tool_state/<工具名>/（control_center/paths.py：按 STB_HOME）。"""
    from control_center.paths import TOOL_STATE

    return TOOL_STATE / tool


def last_seen(path: Path) -> int:
    try:
        return int(json.loads(path.read_text()).get("last_seen", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def positions(cdir: Path) -> list[Path]:
    """{run}/read-email/ 里工具写的新位置文件（不含失败记录、临时文件、符号链接）。"""
    try:
        return sorted(f for f in cdir.iterdir() if NAME.fullmatch(f.name) and f.is_file() and not f.is_symlink())
    except OSError:
        return []


def _json(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def write_position(path: Path, ts: list[int], first: int, last: int) -> None:
    """取信的函数成功交信时调：ts 是这批每封的时间（按编号；没有新邮件是空的），这次交出的是第 first–last 封。
    同一次运行里再取：ts 的开头和上次记的一样（编号没变）就接着记 served，不一样就从头记。"""
    old = _json(path)
    prev = old.get("ts")
    served = list(old.get("served") or []) if isinstance(prev, list) and ts[:len(prev)] == prev else []
    if 1 <= first <= last <= len(ts):
        served.append([first, last, ts[last - 1]])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps({"last_seen": ts[-1] if ts else 0, "ts": ts, "served": served}))
    os.replace(tmp, path)


def call_failed(cdir: Path, mailbox: str, error: str) -> None:
    """read-email_new 这次调用失败（报错）：在 {run}/read-email/_failed.json 记一条（带邮箱）（加锁追加：同一运行里几次调用可能同时结束）。"""
    cdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cdir / FAILED, os.O_RDWR | os.O_CREAT, 0o664)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            got = json.loads(f.read() or "{}")
        except ValueError:
            got = {}
        items = [x for x in (got.get("failures") if isinstance(got, dict) else None) or [] if isinstance(x, dict)]
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"failures": [*items, {"time": int(time.time()), "mailbox": mailbox, "error": str(error)[:300]}]}, ensure_ascii=False))


def failures(cdir: Path) -> list[dict[str, Any]]:
    return [x for x in _json(cdir / FAILED).get("failures") or [] if isinstance(x, dict)]


def covered(ts: list[Any], served: list[Any]) -> int:
    """从第 1 封起连续被交出去的封数 k（served 里的 [a, b, 第 b 封的时间]，时间对不上的不认）。"""
    n, got = len(ts), set()
    for x in served:
        if isinstance(x, list) and len(x) == 3 and all(type(v) is int for v in x) and 1 <= x[0] <= x[1] <= n and ts[x[1] - 1] == x[2]:
            got.update(range(x[0], x[1] + 1))
    k = 0
    while k + 1 in got:
        k += 1
    return k


def advance(ts: list[Any], served: list[Any]) -> int:
    """新的已读位置：前 k 封交出去了（covered）→ 全交出去是最后一封的时间，否则第 k+1 封的时间 − 1 秒；k = 0 返回 0。"""
    k = covered(ts, served)
    return 0 if k == 0 else int(ts[-1]) if k == len(ts) else int(ts[k]) - 1


def run_end(data: dict[str, Any], tool: str, names: dict[str, str]) -> dict[str, Any] | None:
    """run_end：返回要输出到 stdout 的改判（{"status": "failed", "error": …}）或 None。names：邮箱 → 给人看的名字（邮箱文件的 NAME）。"""
    run_dir = data.get("run_dir")
    if data.get("status") != "complete" or not run_dir:
        return None
    cdir = Path(str(run_dir)) / tool
    state = tool_state(tool).resolve()
    done = set()                                      # 这批全交出去了的邮箱
    for f in positions(cdir):
        got = _json(f)
        ts, served = got.get("ts"), got.get("served") or []
        if not (isinstance(ts, list) and all(type(t) is int for t in ts)):
            continue
        if covered(ts, served) == len(ts):
            done.add(f.name[:-len(".json")])
        new = advance(ts, served)
        target = (state / f.name).resolve()
        if new <= 0 or target.parent != state or new <= last_seen(target):
            continue                                  # 只往前推、只写本工具状态目录里的文件
        state.mkdir(parents=True, exist_ok=True)
        tmp = state / f".{f.name}.tmp"
        tmp.write_text(json.dumps({"last_seen": new}))
        os.replace(tmp, target)
        print(f"{tool}：已读位置 {f.name} 前进到 {new}", file=sys.stderr)
    unread = sorted({str(x.get("mailbox") or "") for x in failures(cdir)} - done)
    if unread:
        print(f"{tool}：这次运行有取信失败的调用，已读位置只推进到交出去的部分", file=sys.stderr)
        return {"status": "failed", "error": f"{'、'.join(names.get(m, m) for m in unread)}没读完，已读位置只推进到读成的部分，下次接着读"}
    return None


def main_run_end(tool: str, names: dict[str, str]) -> int:
    """run_end hook 入口（同文件夹的 cursor_hook.py 调它）：stdin 是平台给的输入。"""
    out = run_end(json.loads(sys.stdin.read() or "{}"), tool, names)
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return 0
