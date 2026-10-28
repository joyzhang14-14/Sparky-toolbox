"""workflow 任务的步骤交接与 stbcc-hub 的旁路（2026-10-14 Joy 定）。一次 workflow 运行按 steps 一步一步跑，每步由 runner.run_workflow 跑：
- 要资源的步骤（第 3 项 true）由 runner 跑：它是 runner 队列里一个按任务 priority 排队的条目（新运行的第 1 步是新条目，之后的步骤是 resume 这次运行的条目），
  轮到了先切到这一步的模型档位或空出机器；步骤之间自然让出给更高优先级的。
- 不要资源的步骤（false）由 stbcc-hub 跑（dispatch，每 TICK_S 秒）：条目在 data/runtime/steps/，hub 马上起一个运行单元（和 runner 的同名同法 stb-run-<任务>-<运行 id>，
  控制文件 <单元>.json 带 side: true），不进 runner 的队列、不切模型、不打断正在跑的任务；不同任务的旁路步骤同时跑。单元结束由 dispatch 收尾（reap）。
- 一步跑完（status stepping），收尾的那边按下一步要不要资源交接（route）：要 → runner 的队列，不要 → steps/。
- 新触发的运行（runner.enqueue）都先进 steps/：同一任务还有排队的条目或没结束的运行（runner.task_pending）就不开——订阅事件的（queues_overlap）等它结束，别的丢掉；
  否则第 1 步要资源的放进 runner 的队列，不要的马上起。
- 停止标记（stop-<任务>，/stop）：runner 正在跑这个任务的就归 runner；否则旁路在跑的步骤停下（记 stopped），步骤之间等着的运行撤掉待跑的条目、记 stopped。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_center.paths import CONTROL, QUEUE, RUNNER_STATE, RUNS, STEPS, task_file

TICK_S = 2.0


def _read(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def side_units() -> list[dict[str, Any]]:
    """stbcc-hub 起的、还没收尾的旁路步骤：[{unit, task, run_id, step}]（控制文件 <单元>.json 带 side: true）。"""
    out = []
    for f in sorted(CONTROL.glob("*.service.json")):
        c = _read(f)
        if c.get("side"):
            out.append({"unit": f.name.removesuffix(".json"), "task": str((c.get("entry") or {}).get("task")), "run_id": str(c.get("run_id")),
                        "step": c.get("step")})
    return out


def _latest_run(name: str) -> tuple[Path, dict[str, Any]] | None:
    """任务最新的一次运行（运行 id 按时间排）：同一 workflow 任务同时只有一次运行，没结束的一定是最新的。"""
    runs = sorted((RUNS / name).glob("*/run.json"))
    return (runs[-1], _read(runs[-1])) if runs else None


def active(name: str) -> bool:
    """任务有没结束的 workflow 运行：steps/ 里有它下一步的条目、旁路在跑它的步骤、或最新的运行在步骤之间（stepping）。
    steps/ 里新触发、还没开的条目不算（dispatch 按它判同一任务的新触发开不开）。"""
    if any(e.get("task") == name and e.get("resume") for _, e in _queue_entries_in(STEPS)) or any(u["task"] == name for u in side_units()):
        return True
    last = _latest_run(name)
    return last is not None and last[1].get("status") == "stepping"


def _queue_entries_in(folder: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for p in sorted(folder.glob("*.json")):
        e = _read(p)
        if e:
            out.append((p, e))
    return out


def route(task: Any, run_id: str, entry: dict[str, Any]) -> None:
    """一步跑完（stepping）后交接下一步：条目沿用这次运行开跑时的优先级、入队时刻、触发方式，resume 这次运行；下一步要资源的进 runner 的队列，不要的进 steps/。"""
    from control_center.runner import requeue

    n = int(_read(RUNS / task.name / run_id / "run.json").get("step") or 1)
    nxt = {k: entry[k] for k in ("task", "priority", "enqueued_at", "trigger", "catchup_slot") if k in entry}
    nxt["resume"] = run_id
    if n > len(task.step_list):
        _end(task.name, run_id, "failed", f"任务定义改了：运行要接着跑第 {n} 步，现在只有 {len(task.step_list)} 步")
        return
    requeue(nxt, None if task.step_list[n - 1].resource else STEPS)


def _end(name: str, run_id: str, status: str, error: str) -> None:
    """步骤之间（没有进程在跑）就结束的运行：记状态、跑 run_end hook（status 带上）。"""
    from control_center import hooks
    from control_center.paths import WORK
    from control_center.runner import _atomic_write, load_task

    rj = RUNS / name / run_id / "run.json"
    info = _read(rj)
    try:
        hooks.fire("run_end", hooks.run_owners(load_task(task_file(name))), {"task": name, "run_id": run_id, "tool": None, "subagent": None,
                                                                          "function": None, "args": None, "status": status, "error": error}, rj.parent, WORK / name)
    except Exception as e:  # noqa: BLE001 — 任务定义读不了也要把运行记下来
        print(f"workflow {name}/{run_id}：run_end hook 没跑：{type(e).__name__}: {e}", flush=True)
    info.update(status=status, error=error, notified=True, ended=datetime.now(timezone.utc).isoformat())
    _atomic_write(rj, json.dumps(info, ensure_ascii=False, indent=1))


def _start_side(task: Any, entry: dict[str, Any], run_id: str, step: int) -> None:
    """起一个旁路运行单元跑这一步（runner.exec_unit，同 runner 起的单元；控制文件带 side: true，不进 runner.json）。"""
    from control_center import runner

    unit = runner.next_unit(task.name, run_id)
    runner._atomic_write(runner._unit_file(unit), json.dumps({"entry": entry, "run_id": run_id, "load_seconds": 0, "t0": time.time(),
                                                              "side": True, "step": step}, ensure_ascii=False))
    runner._live_file(unit).unlink(missing_ok=True)
    try:
        runner.start_unit(unit, task)
    except Exception:
        runner._unit_file(unit).unlink(missing_ok=True)
        raise


def _reap(notes: list[str], stale: list[Path]) -> None:
    """旁路单元结束了的：跑完一步的交接下一步；没收尾就没了的（status 还是 running）同遗留运行一样标 failed（stale 里的由调用方补发通知）。"""
    from control_center import runner

    for u in side_units():
        if runner.unit_alive(u["unit"]):
            continue
        rj = RUNS / u["task"] / u["run_id"] / "run.json"
        info = _read(rj)
        entry = _read(runner._unit_file(u["unit"])).get("entry") or {}
        try:
            if not info:
                notes.append(f"❌ {u['task']} 第 {u['step']} 步没开跑：运行单元起来就退出了（看 journalctl --user -u {u['unit']}）")
            elif info.get("status") == "running" or (info.get("status") == "stepping" and int(info.get("step") or 0) == u["step"]):
                if runner.fail_stale_run(rj, info, f"第 {u['step']} 步的运行进程没收尾就退出了（被强杀或崩溃，看 journalctl --user -u {u['unit']}）"):
                    stale.append(rj.parent)
            elif info.get("status") == "stepping":
                route(runner.load_task(task_file(u["task"])), u["run_id"], entry)
        except Exception as e:  # noqa: BLE001 — 收尾出错也要清掉控制文件，别反复收
            notes.append(f"❌ {u['task']} 运行 {u['run_id']} 第 {u['step']} 步之后交接失败：{type(e).__name__}: {str(e)[:300]}")
        runner._unit_file(u["unit"]).unlink(missing_ok=True)
        runner._live_file(u["unit"]).unlink(missing_ok=True)


def _stops(notes: list[str]) -> None:
    """停止标记（stop-<任务>）：runner 正在跑这个任务就留给 runner；否则停旁路在跑的步骤、结束步骤之间等着的运行，再取走标记。"""
    from control_center import runner

    current = (_read(RUNNER_STATE).get("current") or {}).get("task")
    for m in sorted(CONTROL.glob("stop-*")):
        name = m.name.removeprefix("stop-")
        if name == current:
            continue
        hit = False
        for u in side_units():
            if u["task"] == name:
                runner.UnitControl(u["unit"], RUNS / name / u["run_id"], 0.0).request_pause("stop")
                hit = True
        last = _latest_run(name)
        if last is not None and last[1].get("status") == "stepping":
            run_id = last[0].parent.name
            for folder in (QUEUE, STEPS):
                for p, e in _queue_entries_in(folder):
                    if e.get("task") == name and e.get("resume") == run_id:
                        p.unlink(missing_ok=True)
            _end(name, run_id, "stopped", "Joy 停下了这次运行（/stop 或 stb stop，在步骤之间）")
            hit = True
        if hit:
            print(f"workflow {name}：按停止标记停下", flush=True)
        m.unlink(missing_ok=True)


def dispatch() -> tuple[list[str], list[Path]]:
    """stbcc-hub 每 TICK_S 秒调一次（在线程里）：收尾结束了的旁路单元、处理停止标记、起 steps/ 里的条目。返回 (要私信 Joy 的话, 要补发失败通知的运行目录)。"""
    from control_center import runner

    notes: list[str] = []
    stale: list[Path] = []
    _reap(notes, stale)
    _stops(notes)
    for p, e in _queue_entries_in(STEPS):
        name = str(e.get("task"))
        try:
            task = runner.load_task(task_file(name))
        except Exception as ex:  # noqa: BLE001
            p.unlink(missing_ok=True)
            notes.append(f"❌ 任务 {name}：定义校验失败，这次（{e.get('trigger')}）没跑、已丢弃：{type(ex).__name__}: {str(ex)[:1000]}")
            continue
        if run_id := e.get("resume"):                     # 运行的下一步（不要资源）：马上起
            step = int(_read(RUNS / name / str(run_id) / "run.json").get("step") or 1)
            p.unlink(missing_ok=True)
            try:
                _start_side(task, e, str(run_id), step)
                print(f"workflow {name}：旁路起第 {step} 步（运行 {run_id}）", flush=True)
            except Exception as ex:  # noqa: BLE001
                _end(name, str(run_id), "failed", f"第 {step} 步起不来：{type(ex).__name__}: {str(ex)[:300]}")
                notes.append(f"❌ {name} · 第 {step} 步起不来：{type(ex).__name__}: {str(ex)[:300]}")
            continue
        p.unlink(missing_ok=True)                         # 新触发的运行：先取走，再看同一任务有没有没结束的
        if runner.task_pending(name):
            if task.queues_overlap:
                runner.requeue(e, STEPS)                  # 订阅事件的：放回去等它结束（入队时刻不变，顺序不乱）
            else:
                print(f"workflow {name}：还有没结束的运行，丢掉这次触发（{e.get('trigger')}）", flush=True)
            continue
        if task.step_list[0].resource:
            runner.requeue(e)                             # 第 1 步要资源：进 runner 的队列，和别的任务按优先级排
            continue
        run_id = runner.new_run_id()
        try:
            _start_side(task, e, run_id, 1)
            print(f"workflow {name}：旁路起第 1 步（新运行 {run_id}）", flush=True)
        except Exception as ex:  # noqa: BLE001
            notes.append(f"❌ {name} 没开跑：第 1 步起不来：{type(ex).__name__}: {str(ex)[:300]}")
    return notes, stale
