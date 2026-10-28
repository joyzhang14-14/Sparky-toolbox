"""定时触发（trigger: schedule）与 cron 解析。stbcc-hub 每分钟（整分后约 1 秒）调一次 tick：按任务 yaml 的 schedule（5 段 cron，本机时区）入队。
schedule 写一条 cron，或几条 cron 的列表（几个时刻的分钟不一样时，如 ["0 7 * * *", "30 18 * * *"]）：列表按并集算，哪条到点都触发。

- 只看已登记（stb task add）、trigger 是 schedule 的任务；yaml 每次现读，改 schedule / enabled 下一分钟就生效。
- 状态文件 data/runtime/schedule.json（STATE_NAME）：{任务: {"slot": 上次处理过的原定时刻, "schedule": 那时的 cron}}。
- 任务最近一个原定时刻（last_occurrence）比记下的新：入队一次（trigger schedule）。中枢停着时错过的只补最近一次（同 systemd timer 的 Persistent=true）。
- 只记下这个时刻、不入队的：yaml 的 enabled 是 false（启用后不补停用期间的）、cron 改过（从新 cron 的下一个时刻起跑）、
  第一次见到这个任务（还没有记录）时：这个时刻早于首次登记、之后已有运行开始过、或它正在跑 / 已在队列里。
原定时刻之后没成功跑过的补跑另有 runner.catchup_due。cron 的日与周取"且"（都写了时两个都要对上）。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from control_center import paths

STATE_NAME = "schedule.json"
SEARCH_DAYS = 1500           # 往前 / 往后最多找这么多天（够覆盖只在 2 月 29 日的 cron）


def _values(v: str, lo: int, hi: int) -> set[int]:
    """cron 单字段 → 取值集合：*、a、a-b，可带 /n，逗号列表；超出 lo-hi 或写法不对抛 ValueError。"""
    out: set[int] = set()
    for p in v.split(","):
        m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", p)
        if not m or (m.group(2) is not None and int(m.group(2)) == 0):
            raise ValueError(f"cron 字段不合法：{p!r}")
        base, step = m.groups()
        if base == "*":
            a, b = lo, hi
        else:
            nums = [int(x) for x in base.split("-")]
            if any(n < lo or n > hi for n in nums):
                raise ValueError(f"cron 字段 {p!r} 超出 {lo}-{hi}")
            a, b = nums[0], nums[-1] if len(nums) > 1 else (hi if step else nums[0])
        out.update(range(a, b + 1, int(step) if step else 1))
    return out


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """5 段 cron（分 时 日 月 周）→ 各段取值集合（周：0 和 7 都是周日，统一成 0）；不合法抛 ValueError。"""
    f = str(expr).split()
    if len(f) != 5:
        raise ValueError(f"cron 表达式要 5 个字段：{expr!r}")
    mi, h, d, mo, w = _values(f[0], 0, 59), _values(f[1], 0, 23), _values(f[2], 1, 31), _values(f[3], 1, 12), _values(f[4], 0, 7)
    return mi, h, d, mo, {x % 7 for x in w}


def exprs(v: Any) -> list[str]:
    """schedule 的值（一条 cron 或几条的列表）→ cron 列表；没写是空列表。"""
    return [] if v is None else [v] if isinstance(v, str) else [str(x) for x in v]


def check(v: Any) -> None:
    """schedule 合法：一条 5 段 cron，或非空的 cron 列表；不合法抛 ValueError。"""
    if not (isinstance(v, str) or (isinstance(v, list) and v and all(isinstance(x, str) for x in v))):
        raise ValueError('写一条 5 段 cron，或几条 cron 的列表（如 ["0 7 * * *", "30 18 * * *"]）')
    for e in exprs(v):
        parse(e)


def text(v: Any) -> str:
    """显示用：几条用「、」连起来。"""
    return "、".join(exprs(v))


def _day_ok(day: date, d: set[int], mo: set[int], w: set[int]) -> bool:
    return day.day in d and day.month in mo and day.isoweekday() % 7 in w


def last_occurrence(expr: str | list[str], now: float) -> float | None:
    """cron 在 now 之前（含 now 这一分钟）最近一次应触发的时刻；找不到返回 None。几条的列表取最近的那条。"""
    if not isinstance(expr, str):
        got = [x for e in exprs(expr) if (x := last_occurrence(e, now)) is not None]
        return max(got) if got else None
    mi, h, d, mo, w = parse(expr)
    t = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    times = sorted(((hh, mm) for hh in h for mm in mi), reverse=True)
    for i in range(SEARCH_DAYS):
        day = t.date() - timedelta(days=i)
        if not _day_ok(day, d, mo, w):
            continue
        for hh, mm in times:
            at = datetime(day.year, day.month, day.day, hh, mm)
            if at <= t:
                return at.timestamp()
    return None


def next_occurrence(expr: str | list[str], now: float) -> float | None:
    """cron 在 now 之后（不含 now 这一分钟）下一次应触发的时刻；找不到返回 None。几条的列表取最早的那条。"""
    if not isinstance(expr, str):
        got = [x for e in exprs(expr) if (x := next_occurrence(e, now)) is not None]
        return min(got) if got else None
    mi, h, d, mo, w = parse(expr)
    t = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    times = sorted((hh, mm) for hh in h for mm in mi)
    for i in range(SEARCH_DAYS):
        day = t.date() + timedelta(days=i)
        if not _day_ok(day, d, mo, w):
            continue
        for hh, mm in times:
            at = datetime(day.year, day.month, day.day, hh, mm)
            if at > t:
                return at.timestamp()
    return None


def _state_path():  # noqa: ANN202
    return paths.RUNTIME / STATE_NAME


def load_state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def next_run(task: Any, now: float) -> float | None:  # noqa: ANN401 — Task
    """任务下一次定时触发的时刻（stb task list、网页、stb doctor 显示）：没有 schedule、停用、没登记的是 None。"""
    from control_center.runner import load_registered

    if not task.schedule or not task.enabled or task.name not in load_registered():
        return None
    return next_occurrence(task.schedule, now)


def tick(now: float) -> list[tuple[str, float]]:
    """处理一遍所有定时任务（规则见模块说明），返回这次入队的 (任务, 原定时刻)。"""
    from control_center.paths import task_files
    from control_center.runner import RUNNER_STATE, _atomic_write, _queue_entries, _started_since, enqueue, held_entries, load_registered
    from control_center.task import load_task

    state, reg = load_state(), load_registered()
    try:
        current = (json.loads(RUNNER_STATE.read_text()).get("current") or {}).get("task")
    except (OSError, ValueError):
        current = None
    busy = {e.get("task") for _, e in [*_queue_entries(), *held_entries()]} | {current}
    fired: list[tuple[str, float]] = []
    due: list[tuple[int, str, float]] = []
    changed = False
    for f in task_files():
        try:
            task = load_task(f)
        except Exception:  # noqa: BLE001 — 定义坏了的任务由 runner 私信 Joy，这里跳过
            continue
        if "schedule" not in task.triggers or task.name not in reg:
            continue
        slot = last_occurrence(task.schedule, now)  # pyright: ignore[reportArgumentType]
        seen = state.get(task.name)
        if slot is None or (seen and seen.get("schedule") == task.schedule and float(seen.get("slot", 0)) >= slot):
            continue
        if not task.enabled:
            fire = False
        elif seen:
            fire = seen.get("schedule") == task.schedule
        else:
            fire = slot >= float(reg[task.name].get("first", now)) and task.name not in busy and not _started_since(task.name, slot)
        state[task.name] = {"slot": slot, "schedule": task.schedule}
        changed = True
        if fire:
            due.append((task.priority, task.name, slot))
    for _prio, name, slot in sorted(due, key=lambda x: -x[0]):   # 同一时刻的按优先级从高到低入队：运行器先看到高的
        enqueue(name, trigger="schedule", enqueued_at=now)
        fired.append((name, slot))
    for name in set(state) - set(reg):                 # 注销了的任务不再留记录
        state.pop(name)
        changed = True
    if changed:
        _atomic_write(_state_path(), json.dumps(state, ensure_ascii=False, indent=1))
    return fired
