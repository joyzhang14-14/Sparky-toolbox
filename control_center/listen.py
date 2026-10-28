"""监听：定义者任务文件夹里的 listen.py（任务 yaml 写 listen: <监听名>），由 stbcc-hub 中枢运行。监听属于任务：听什么、什么时候触发、这次的用户消息是什么，都由 listen.py 决定；
平台负责运行它、按结果入队，并按任务配置加系统层面的东西（附件说明、memory 清单里的记忆）；用户消息里带不带以前的对话由 listen.py 自己拼。

- 有名字、可复用：每个监听名只在一个任务（定义者）的文件夹里有 listen.py；别的任务写同一个 listen: <名> 就是引用，自己不放 listen.py。
  check 每 LISTEN_EVERY_DEFAULT 秒只调一次，fire 时所有引用它的任务（含定义者，启用的）用同一个 prompt 各自入队，
  每个任务各按下面的规则跳过（已在队列 / 在跑 / 在等 Joy / 连续失败暂停）；全都要跳过时这次不调 check（不推进已读位置）。
  事件交给每个订阅它的任务（渠道发布的消息也一样，各订阅者的 on_event 自己决定接不接，见 hub.Hub.publish），调的是它引用的 listen.py。
  监听程序的 network 按定义者的 task.yaml；状态目录跟着监听名（data/listen/<监听名>/），定义迁到别的任务也不变。
- listen.py 可以写三个函数（普通函数；check / on_event 返回 {"fire": bool, "prompt": str}；prompt 可省略或为空——这次只靠任务的 system_prompt 跑）：
  · check(ctx)：定时检查。检查循环（Listener.tick）每 LISTEN_EVERY_DEFAULT 秒（30 秒）调一次。
  · on_event(event, ctx)：事件推送。任务 yaml 的 listen_events 写订阅的来源名；中枢收到这个来源的事件就立刻调（publish）。
    event = {"source": 来源名, "ts": 时刻, …来源给的字段}；渠道发布的（来源 message，Joy 发来的消息）另有 channel（来源渠道）、text、msg_id（渠道的消息 id）、origin，
    带附件时有 images、docs（图片 / 其余附件的文件名）；一次性监听回调（来源是登记它的工具名，只交给登记它的任务）给 name、prompt。
    on_event 还可以返回（parse_result）：
    · reply：回给发布方的一句话（渠道显示给 Joy）；
    · steer：写了就表示这条可以插进本任务正在跑的对话，插进去时用这段文字（prompt 里拼了以前的对话时只写这条消息本身，免得重复）；不写 = 不插话，排队开下一次运行；
    · actions：请平台对本任务自己做的通用动作（run_actions）：[{"do": "stop"}] 停本任务正在跑的运行、撤本任务还没开跑的、和这个事件同来源的条目；
      [{"do": "clear_runs", "since": 时刻}] 删本任务 since 之后的运行记录（只留精简的 run.json 与 gateway.jsonl）、卡、工作目录与收件箱里的文件，补调工具 / 子代理的 post_call hook。
    命令（/stop、/new、私信开关…）、接不接、对话记录都由任务自己认、自己做（如 tasks/dm-assistant/listen.py），平台不认任何命令名。
  · watch(ctx, emit)：常驻监听。程序一直跑，自己决定什么时候触发：调 emit({"fire": bool, "prompt": str})（同 check 的返回值）。
    有 watch 的监听，检查循环不再按间隔调 check；规则见下面的「常驻监听」。
- ctx（平台给的只读信息，每次现算；常驻监听是起程序时算一次）：task（定时检查 / 常驻监听是定义者，事件是那个任务）、
  tasks（定时检查 / 常驻监听：这次 fire 会入队的任务；事件：这个事件交给的全部任务，各自决定接不接）、listen（监听名）、
  why（interval / event / watch：这次为什么调）、now、
  state_dir（这个监听的状态目录 data/listen/<监听名>/state，记已读位置等；可写）、last_run（ctx.task 最近一次运行的 id / status / started / channel，没有为 null）、
  model（ctx.task 这次会用的模型档位：models.yaml 里那一项，加 name；含 context_window，任务按它定拼进上下文的对话有多少；没有对应档位为 null）。
- 运行身份：以 sparky 跑（launch.sparky_command，宿主是 control_center/listen_host.py），文件看 sparky 的文件权限，state_dir 由平台现建现开；
  network 按任务 yaml（不写 = 禁 TCP）。不起模型、不占 runner；单次限时 CALL_TIMEOUT_S。每次调用起一个进程（实测几十毫秒）。
  listen.py 的输出与报错记在 data/listen/<监听名>/listen.stderr.log（超过 LOG_MAX 轮转成 .1）。
- 检查循环：任务停用（enabled: false）不检查；队列里已有、正在跑（含加载模型）也不检查（不推进已读位置，跑完再查）；fire 时再看一遍，已有就不重复入队。
  最近一次运行在等 Joy（waiting：等回答问题卡；partial：等点「续跑」）也不检查：Joy 处理了或有了更新的运行再查。
  运行连续 RUN_FAIL_PAUSE 次 failed / timeout：暂停这个任务的检查，发一条系统通知；有一次运行不再失败、或任务定义（task.yaml / listen.py）在最近一次失败之后改过，就恢复。
- 检查失败（起不来、报错、超时、返回格式不对，或 fire 了但入队失败）：记日志（stbcc-hub 的 journal）、不入队；连续 FAIL_NOTIFY 次失败发一条系统通知，恢复前不再发。
- 事件不受上面「已在队列 / 在等 Joy / 连续失败暂停」的限制（照常排队、带 steer 的插入对话）；同一来源的事件按到达顺序逐条交给订阅者；on_event 失败时把原因交回发布者（渠道显示给 Joy）。
- 通用动作（run_actions）：只对发请求的任务自己执行；/stop 的停止标记由运行器在下一个边界取走（runner._stop_requested），撤掉的条目交回中枢标成已停。
- 常驻监听（watch）：中枢的检查循环监督它（Listener._supervise，每 TICK_S 秒对一次账；listen.py 的定义每 WATCH_SCAN_S 秒重读一次）。
  · 身份、目录同 check：sparky（定义者的 network），宿主 listen_host.py，state_dir、ctx、listen.stderr.log（同样轮转）。
  · 协议：宿主的 stdin 一行请求（fn = watch）之后一直开着，中枢关掉它 = 请程序退出；程序每 emit 一次，宿主往 stdout 写一行 {"emit": {…}}；
    watch 返回或出错时最后一行 {"ok": …}（同 check），然后退出。
  · 起：至少一个引用它的任务 启用；不满足了就停（关 stdin，等 WATCH_STOP_S 秒，再 SIGTERM，再 SIGKILL）。
    定义者没了（任务删了、改名、定义无效）也停；定义者的 task.yaml 或 listen.py 改了就停掉、马上按新定义重起。
  · 程序退出了（崩溃、报错、watch 自己返回）就重起：等 WATCH_BACKOFF_MIN_S 秒，连着退出每次翻倍，最多 WATCH_BACKOFF_MAX_S 秒；
    上次连续跑满 WATCH_STABLE_S 秒才退出的，回到最短。崩溃、报错与格式不对的 emit 算失败（同检查失败：记日志，连续 FAIL_NOTIFY 次发一条通知，
    恢复前不再发；正常的 emit 算恢复）；watch 自己正常返回只记日志、照样按等待重起。
  · emit fire：按当时引用它的任务（启用）各入队一次，跳过规则同 check fire（已在队列 / 在跑、在等 Joy、连续失败暂停），每个跳过都记日志。
  · 登记来的常驻监听：别的平台模块可以交给 Listener 一份 extra（现算的 Watch 列表），按同一套规则起、停、重起，如一次性动态监听
    （control_center/monitors.watchers：程序是登记它的工具给的，属于登记它的任务）。它们的 emit 由登记方处理（Watch.on_emit），实例名不是监听名（如 monitor.<id>）。
  · 在跑的常驻监听写在 data/runtime/listening/<实例名>.json（{key, task, tasks, pid, started}；起时写、停时删，中枢启动时清空）：
    stbcc-docker 据此认「属于这个监听」的容器（control_center/docker_service.py），监听停了容器跟着停。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from control_center import launch, runner
from control_center.paths import APPROVALS, CONTROL, HOME, INBOX, LISTEN, QUEUE, REPO, RUNNER_STATE, RUNS, WORK, listening_dir, task_file
from control_center.task import LISTEN_EVERY_DEFAULT, Task, TaskError, listen_users, load_task

TICK_S = 1                 # 循环多久看一次哪些任务到点了
CALL_TIMEOUT_S = 60        # 一次调用（起进程 + check / on_event）的上限
FAIL_NOTIFY = 3            # 连续失败几次给 Joy 发通知
RUN_FAIL_PAUSE = 3         # 运行连续失败几次暂停检查
LOG_MAX = 1 << 20
HOST = REPO / "control_center" / "listen_host.py"
WATCH_SCAN_S = 5           # 常驻监听：多久重读一次 listen.py 的任务定义（该起 / 该停 / 定义改了要重起）
WATCH_BACKOFF_MIN_S = 5    # 常驻监听退出后等多久重起；连着退出每次翻倍
WATCH_BACKOFF_MAX_S = 300  # 最多等这么久
WATCH_STABLE_S = 600       # 连续跑满这么久才退出：等待回到 WATCH_BACKOFF_MIN_S
WATCH_STOP_S = 10          # 停常驻监听：关 stdin 后等它自己退出多久，再 SIGTERM（再等这么久，然后 SIGKILL）
WATCH_LINE_MAX = 4 << 20   # 常驻监听一行输出（一次 emit）最多多大
ACTIONS = ("stop", "clear_runs")   # on_event 能请平台做的通用动作（run_actions）
RUN_KEEP = {"task", "run_id", "status", "trigger", "started", "first_started", "ended", "seconds", "elapsed_seconds", "runtime", "model", "priority",
            "segments", "load_seconds", "model_switch_seconds", "subagent_seconds", "paused_seconds", "preempt_count", "calls", "rejections",
            "agent_messages", "exit_code", "tokens", "tokens_all", "memory_peak", "notified", "cleared"}   # clear_runs 后 run.json 留的键
LOG_KEEP = {"ts", "ev", "name", "tools", "seconds", "ok", "code", "timeout", "refunded", "quota", "subagent_seconds"}   # clear_runs 后 gateway.jsonl 每行留的键


class CheckError(Exception):
    pass


def listen_dir(name: str) -> Path:
    """监听用的目录 data/listen/<监听名>/：state/（state_dir）、listen.stderr.log。跟着监听定义，不是每个引用者一份。"""
    return LISTEN / name


def state_dir(name: str) -> Path:
    return listen_dir(name) / "state"


def last_run(name: str) -> dict[str, Any] | None:
    """最近一次运行的摘要（ctx 的 last_run）。"""
    for f in sorted((RUNS / name).glob("*/run.json"), reverse=True):
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        return {"id": f.parent.name, "status": info.get("status"), "started": info.get("first_started") or info.get("started"),
                "channel": info.get("channel")}
    return None


def model_of(task: Task) -> dict[str, Any] | None:
    """ctx 的 model：任务这次会用的档位（models.yaml 里那一项加 name）；读不了或没有对应档位为 None。"""
    from control_center.models import load_models, profiles

    try:
        models = load_models()
        name = task.profile_name(models)
    except Exception:  # noqa: BLE001 — 档位表读不了、任务的 runtime + model 对不上：ctx 照常给，model 为 null
        return None
    return {"name": name, **profiles(models)[name]}


def make_ctx(task: Task, why: str, tasks: list[str] | None = None) -> dict[str, Any]:
    return {"task": task.name, "tasks": tasks or [task.name], "listen": task.listen, "why": why, "now": time.time(),
            "state_dir": str(state_dir(str(task.listen))), "last_run": last_run(task.name), "model": model_of(task)}


def rel_file(task: Task) -> str:
    """监听程序在仓库里的位置（日志、通知里用）：tasks/<定义者>/listen.py。"""
    return f"tasks/{task.listen_owner or task.name}/listen.py"


def command(task: Task) -> tuple[list[str], dict[str, str]]:
    """起监听程序的命令与环境：sparky（同任务的 command，launch.command_argv 那一套），宿主 listen_host.py。
    network 按定义者的 task.yaml（引用者用定义者的）。"""
    f = task.listen_file
    if f is None or not f.is_file():
        raise CheckError(f"监听程序不在：{f or rel_file(task)}")
    owner = task if task.defines_listen else load_task(f.parent / "task.yaml", refresh=False)
    return host_command(str(task.listen), task.name, f.parent, owner.network)


def host_command(key: str, task: str, code: Path, network: Any) -> tuple[list[str], dict[str, str]]:
    """以 sparky 起宿主 listen_host.py 的命令与环境（check / on_event 一次调用、常驻监听都用）：程序在 code 目录，
    这个实例的 state_dir 现建（整个 data/listen/ 对 sparky 读写，paths.sparky_access），别的文件看 sparky 的文件权限；
    network：true 才放行 TCP，[端口…] 只放行连本机这些端口，别的禁 TCP。key 是实例名（data/listen/<key>/），task 进环境 STB_LISTEN_TASK。"""
    sd = state_dir(key)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()), "TMPDIR": "/tmp", "STB_LISTEN_TASK": task,
           **{k: os.environ[k] for k in ("STB_HOME", "STB_CONFIG") if k in os.environ}}     # 数据目录与设置跟着平台走（listen.py 里按它找 data/…）
    if os.environ.get("STB_TEST_LAUNCH_PLAIN"):    # 测试钩子（tests/stb_platform/conftest.py）：临时目录 sparky 进不去，以当前用户起
        sd.mkdir(parents=True, exist_ok=True)
        return [sys.executable, str(HOST)], env
    sd.mkdir(parents=True, exist_ok=True)
    cfg: dict[str, Any] = {**launch.net_cfg(network), "code": str(code), "server": str(HOST)}
    return launch.sparky_command(sys.executable, cfg, env, f"listen:{key}"), env


def _errlog(task: str) -> Any:
    f = listen_dir(task) / "listen.stderr.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists() and f.stat().st_size > LOG_MAX:
        f.replace(f.with_name(f.name + ".1"))
    return f.open("ab")


def parse_result(fn: str, data: Any) -> dict[str, Any]:
    """check / on_event 的返回值 → {"fire": bool, "prompt": str}；prompt 可省略、为 null 或空（这次只靠 system_prompt）。
    on_event 还可以写 reply（回给发布方的一句话）、steer（插进正在跑的对话时用的文字）、loads_since（上下文起点，加载过的接着有效）、
    actions（通用动作，见模块说明）；写了才带上。格式不对抛 CheckError。"""
    if not isinstance(data, dict) or not isinstance(data.get("fire"), bool):
        raise CheckError(f'{fn} 要返回 {{"fire": true/false, "prompt": "…"}}，实际是：{json.dumps(data, ensure_ascii=False)[:200]}')
    prompt = data.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise CheckError(f"{fn} 返回的 prompt 要是字符串（或不写），实际是 {type(prompt).__name__}")
    out = {"fire": data["fire"], "prompt": prompt or ""}
    for key in ("reply", "steer"):
        if data.get(key) is not None:
            if not isinstance(data[key], str):
                raise CheckError(f"{fn} 返回的 {key} 要是字符串（或不写），实际是 {type(data[key]).__name__}")
            out[key] = data[key]
    if data.get("loads_since") is not None:            # 私信任务：上下文起点（时刻，秒）；这之后本任务各次运行加载过的工具与 skill 下一次运行接着有效（手册 §4.10 第 5 步）
        if isinstance(data["loads_since"], bool) or not isinstance(data["loads_since"], (int, float)):
            raise CheckError(f"{fn} 返回的 loads_since 要是时刻（秒，数字），实际是 {data['loads_since']!r}")
        out["loads_since"] = float(data["loads_since"])
    if data.get("actions") is not None:
        acts = data["actions"]
        if not isinstance(acts, list) or not all(isinstance(a, dict) and a.get("do") in ACTIONS for a in acts):
            raise CheckError(f'{fn} 返回的 actions 要是 [{{"do": …}}] 列表，do 只能是 {"、".join(ACTIONS)}，实际是：{json.dumps(acts, ensure_ascii=False)[:200]}')
        if bad := [a for a in acts if a["do"] == "clear_runs" and (isinstance(a.get("since"), bool) or not isinstance(a.get("since"), (int, float)))]:
            raise CheckError(f"{fn} 返回的 clear_runs 要写 since（时刻，秒）：{json.dumps(bad, ensure_ascii=False)[:200]}")
        out["actions"] = acts
    return out


# ---------- 通用动作（on_event 的 actions，只对发请求的任务自己做） ----------
def run_actions(task: str, actions: list[dict[str, Any]], source: str) -> tuple[list[str], list[dict[str, Any]]]:
    """按顺序执行 actions（source：请求它的事件的来源名）。返回 (接在 reply 后面的执行结果, 撤掉的队列条目)。"""
    notes: list[str] = []
    dropped: list[dict[str, Any]] = []
    for a in actions:
        if a["do"] == "stop":
            got = drop_queued(task, source)
            dropped += got
            if _running(task):
                CONTROL.mkdir(parents=True, exist_ok=True)
                (CONTROL / f"stop-{task}").write_text(str(time.time()))   # 运行器在下一个边界取走（runner._stop_requested）
            if got:
                notes.append(f"（另取消了 {len(got)} 条排队的消息）")
        elif a["do"] == "clear_runs":
            if _running(task):
                notes.append(f"（{task} 正在跑，运行记录没清）")
                continue
            n = clear_runs(task, float(a["since"]))
            clear_files(task, float(a["since"]))
            notes.append(f"（另清了 {n} 次运行：只留调用记录，卡片和生成的文件删了）")
    return notes, dropped


def _running(task: str) -> bool:
    """任务在跑：runner 正在跑它，或它有没结束的 workflow 运行（旁路在跑的步骤、步骤之间，workflow.active）。"""
    from control_center import workflow

    try:
        cur = json.loads(RUNNER_STATE.read_text()).get("current") or {}
    except (OSError, ValueError):
        cur = {}
    return cur.get("task") == task or workflow.active(task)


def drop_queued(task: str, source: str) -> list[dict[str, Any]]:
    """stop：撤掉队列里这个任务还没开跑的、来源是 source 的事件条目（如消息来的 /stop 不撤一次性监听回调的；不含被抢占后等恢复的、定时与手动的、监听检查的）。
    返回撤掉的条目。"""
    out = []
    for p in sorted(QUEUE.glob("*.json")):
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if e.get("task") == task and e.get("trigger") == "listen" and (e.get("source") or {}).get("event") == source and not e.get("resume"):
            p.unlink(missing_ok=True)
            out.append(e)
    return out


def _tool_owners(task: str) -> list[Any]:
    """任务 yaml 现在的 tools 里有 hooks 的工具、subagents 里有 hooks 的子代理（hooks.Owner）；yaml 读不了就是空。"""
    import yaml

    from control_center import hooks
    try:
        raw = yaml.safe_load(task_file(task).read_text()) or {}
        items = raw.get("tools") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return []
    subs = raw.get("subagents") if isinstance(raw.get("subagents"), list) else []
    names = dict.fromkeys(i if isinstance(i, str) else next(iter(i)) for i in items if isinstance(i, (str, dict)) and i)
    callees = dict.fromkeys(i if isinstance(i, str) else next(iter(i)) for i in subs if isinstance(i, (str, dict)) and i)  # pyright: ignore[reportOptionalIterable]
    return [o for o in [*(hooks.tool_owner(n) for n in names), *(hooks.subagent_owner(n) for n in callees)] if o is not None]


def clear_runs(task: str, since: float) -> int:
    """clear_runs：这段时间动过的运行，运行目录只留精简的 run.json 与 gateway.jsonl；还在等恢复的记 stopped、撤出队列；它们的卡片记录删掉；
    任务现在用的工具与子代理的 post_call hook 按运行 id 补调一次（只调不带 match 的，收掉 ComfyUI 里的副本这类东西）。返回清了几次运行。"""
    from control_center import hooks

    runs = []
    for f in sorted((RUNS / task).glob("*/run.json")):
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if f.stat().st_mtime >= since and not info.get("cleared"):
            runs.append((f.parent, info))
    ids = {f"{task}/{d.name}" for d, _ in runs}
    for p in sorted(QUEUE.glob("*.json")):
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if f"{e.get('task')}/{e.get('resume')}" in ids:
            p.unlink(missing_ok=True)
    for d, info in runs:
        for child in d.iterdir():
            if child.name in ("run.json", "gateway.jsonl"):
                continue
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink(missing_ok=True)
        log = d / "gateway.jsonl"
        if log.exists():
            lines = []
            for line in log.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                lines.append(json.dumps({k: v for k, v in ev.items() if k in LOG_KEEP}, ensure_ascii=False))
            log.write_text("\n".join(lines) + ("\n" if lines else ""))
        if info.get("status") in ("waiting", "paused", "partial"):
            info["status"] = "stopped"
        kept = {k: v for k, v in info.items() if k in RUN_KEEP}
        kept["cleared"] = time.time()
        (d / "run.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1))
        for owner in _tool_owners(task):
            sub = owner.kind == "subagent"
            hooks.fire("post_call", [owner], {"task": task, "run_id": d.name, "tool": None if sub else owner.name, "subagent": owner.name if sub else None,
                                              "function": None, "args": None,
                                              "ok": False, "result": "运行被清掉", "inflight": 0, "cleared": True})
    for f in APPROVALS.glob("*.json"):
        try:
            p = (json.loads(f.read_text()).get("payload") or {})
        except (OSError, ValueError):
            continue
        if p.get("run") in ids or f"{p.get('task')}/{p.get('run_id')}" in ids:
            f.unlink(missing_ok=True)
    return len(runs)


def clear_files(task: str, since: float) -> None:
    """clear_runs：工作目录与收件箱里这段时间写的文件；一级目录（in/、media/ 等）留着。"""
    for root in (WORK / task, INBOX / task):
        if not root.exists():
            continue
        for f in sorted(root.rglob("*"), reverse=True):
            rel = f.relative_to(root).parts
            if f.is_symlink():
                continue
            if f.is_file() and f.stat().st_mtime >= since:
                f.unlink(missing_ok=True)
            elif f.is_dir() and len(rel) >= 2 and not any(f.iterdir()) and f.stat().st_mtime >= since:
                f.rmdir()


async def call(task: Task, fn: str, why: str, event: dict[str, Any] | None = None, tasks: list[str] | None = None) -> dict[str, Any]:
    """起一次监听程序、调 fn（check / on_event）、拿回结果。tasks：ctx 的 tasks（check：这次 fire 会入队的任务；事件：交给的全部任务；不给 = 只有 task）。
    超时抛 TimeoutError，别的问题抛 CheckError / 原异常。"""
    ctx = await asyncio.to_thread(make_ctx, task, why, tasks)
    cmd, env = await asyncio.to_thread(command, task)        # 里面读任务文件、建目录：不堵 stbcc-hub 的事件循环
    args = [ctx] if fn == "check" else [event, ctx]
    req = json.dumps({"repo": str(REPO), "file": str(task.listen_file), "fn": fn, "args": args}, ensure_ascii=False) + "\n"
    log = _errlog(str(task.listen))
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(req.encode()), CALL_TIMEOUT_S)
        except TimeoutError:
            proc.kill()                                       # 只按这个子进程的 PID
            await proc.wait()
            raise
    finally:
        log.close()
    lines = out.decode(errors="replace").strip().splitlines()
    try:
        res = json.loads(lines[-1]) if lines else None
    except ValueError:
        res = None
    if not isinstance(res, dict):
        raise CheckError(f"监听程序没有交回结果（退出码 {proc.returncode}）；看 data/listen/{task.listen}/listen.stderr.log")
    if not res.get("ok"):
        raise CheckError(f"{fn} 报错：{str(res.get('error'))[:500]}")
    return parse_result(fn, res.get("result"))


def failure_why(task: Task, e: BaseException, fn: str = "check") -> str:
    """失败原因文字：超时写明上限与排查方向，别的写「类型: 原文」。"""
    if isinstance(e, TimeoutError):
        return (f"超时：{CALL_TIMEOUT_S} 秒内没完成（起监听程序 + 调 {fn}）；看 data/listen/{task.listen}/listen.stderr.log")
    return f"{type(e).__name__}: {str(e)[:500]}"


def hold_reason(name: str, listen_file: Path | None = None) -> tuple[str, str] | None:
    """按运行记录看这个任务现在该不该检查：(种类, 原因)，种类 waiting（最近一次运行在等 Joy）/ failing（运行连续失败，暂停检查）；可以检查返回 None。"""
    runs = []
    for f in sorted((RUNS / name).glob("*/run.json"), reverse=True)[:RUN_FAIL_PAUSE]:   # 运行 id 是时间，倒序 = 新的在前
        try:
            runs.append((f.parent.name, json.loads(f.read_text())))
        except (OSError, ValueError):
            continue
    if not runs:
        return None
    rid, last = runs[0]
    if last.get("status") in ("waiting", "partial"):
        return "waiting", f"最近一次运行 {rid} 是 {last['status']}，等 Joy 处理"
    streak = 0
    for _, info in runs:
        if info.get("status") not in ("failed", "timeout"):
            break
        streak += 1
    if streak < RUN_FAIL_PAUSE:
        return None
    tf = task_file(name)
    try:
        fixed = max(p.stat().st_mtime for p in (tf, tf.parent / "listen.py", *([listen_file] if listen_file else [])) if p.exists()) > float(last.get("first_started") or 0)
    except (OSError, ValueError):
        fixed = False
    if fixed:
        return None
    return "failing", f"最近 {streak} 次运行都失败了（最近一次 {rid}：{str(last.get('error') or last.get('status'))[:200]}）"


@dataclass
class Watch:
    """一个该在跑的常驻监听（Listener 按它起、停、重起）。key：实例名（data/listen/<key>/，listen.py 的是监听名，登记来的如 monitor.<id>）；
    task：属于哪个任务（listen.py 的是定义者，登记来的是登记它的任务）；tasks：fire 时入队的任务，也是 stbcc-docker 认的「这个监听所属的任务」；
    file：有 watch(ctx, emit) 的程序；sig：定义的指纹（变了就停掉重起）；label：日志、通知里的称呼；
    command()：起它的命令与环境（host_command，在线程里调：里面给 sparkle 组开可写目录）；ctx：给 ctx 多加的字段；
    on_emit(data)：处理一次 emit（data 是程序给的原样映射）；不给 = 按 check fire 的规则入队（Listener._emit_listen）。"""
    key: str
    task: Task
    tasks: list[str]
    file: Path
    sig: str
    label: str
    command: Callable[[], tuple[list[str], dict[str, str]]]
    ctx: dict[str, Any] = field(default_factory=dict)
    on_emit: Callable[[dict[str, Any]], Awaitable[Any]] | None = None


@dataclass
class _Live:
    """在跑（或正在起 / 正在停）的常驻监听。"""
    w: Watch
    started: float
    proc: Any = None
    stopping: str | None = None           # 为什么停（主动停的不算退出、不按等待重起）
    job: asyncio.Task[Any] | None = None


def _sig(*files: Path | None) -> str:
    """定义的指纹：这些文件的路径、修改时间、大小。"""
    out = []
    for f in files:
        try:
            st = f.stat() if f else None
        except OSError:
            st = None
        out.append(f"{f}:{st.st_mtime_ns}:{st.st_size}" if st else f"{f}:-")
    return "|".join(out)


def clear_live() -> None:
    """中枢启动时：上一个实例留下的「在跑的常驻监听」记录都作废（它的子进程随它一起停了）。"""
    d = listening_dir()
    for f in d.glob("*.json") if d.is_dir() else ():
        f.unlink(missing_ok=True)


class Listener:
    """检查循环的状态（按监听名）：每个监听下次检查的时刻、连续失败次数、在途的检查，以及常驻监听（watch）的监督。notify(text) 发系统通知（中枢的 send）；
    extra()：别的平台模块登记来的常驻监听（现算的 Watch 列表，每 TICK_S 秒调一次），如一次性动态监听。"""

    def __init__(self, notify: Callable[[str], Awaitable[Any]], extra: Callable[[], list[Watch]] | None = None) -> None:
        self.notify = notify
        self.extra = extra
        self.due: dict[str, float] = {}
        self.fails: dict[str, int] = {}
        self.busy: set[str] = set()
        self._errors: dict[str, str] = {}      # 任务定义无效时的错误：变了才记日志
        self.held: dict[str, str] = {}         # 因运行记录暂不检查的任务 → 原因：变了才记日志 / 通知
        self._bg: set[asyncio.Task[Any]] = set()
        self.wanted: dict[str, Watch] = {}     # 该在跑的常驻监听（listen.py 的每 WATCH_SCAN_S 秒更新，登记来的每次 tick 更新）
        self.live: dict[str, _Live] = {}       # 在跑的常驻监听
        self.backoff: dict[str, float] = {}    # 实例名 → 上次退出后等了多久（秒）
        self.next_start: dict[str, float] = {}   # 实例名 → 最早什么时候（重）起
        self._extra_keys: set[str] = set()     # wanted 里哪些是 extra 登记来的

    def tick(self, now: float | None = None) -> list[str]:
        """到点、定义者的 listen.py 有 check、至少一个引用它的任务可以入队（启用、没在队列里、不在等 Joy / 失败暂停）的监听各起一次检查
        （后台跑，同一监听不叠）；返回这次起了检查的监听名。listen.py 有 watch 的不按间隔调 check：更新该不该常驻（_want_watch），再对一次账（_supervise）。"""
        now = time.time() if now is None else now
        groups = listen_users()
        for gone in set(self.due) - set(groups):
            self.due.pop(gone, None)
            self.fails.pop(gone, None)
            self.wanted.pop(gone, None)
        started = []
        for lname, names in groups.items():
            if lname in self.busy or now < self.due.get(lname, 0):
                continue
            tasks = [t for n in names if (t := self._load(n, now)) is not None]
            owner = next((t for t in tasks if t.defines_listen), None)
            if owner is not None and owner.listen_has("watch"):
                self.due[lname] = now + WATCH_SCAN_S
                self._want_watch(owner, tasks)
                continue
            self.wanted.pop(lname, None)
            self.due[lname] = now + LISTEN_EVERY_DEFAULT
            if owner is None or not owner.listen_has("check"):
                continue
            ready = [t for t in tasks if t.enabled and not runner.task_pending(t.name)]
            ready = [t for t in ready if not self._hold(t)]
            if not ready:
                continue
            self.busy.add(lname)
            t = asyncio.create_task(self._one(owner, ready))
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            started.append(lname)
        self._supervise(now)
        return started

    def _load(self, name: str, now: float) -> Task | None:
        """载入任务定义；无效时同一原因只记一次日志、通知一次，返回 None（这个任务不检查、不收事件）。"""
        try:
            task = load_task(task_file(name))
        except TaskError as e:
            if self._errors.get(name) != str(e):
                self._errors[name] = str(e)
                print(f"监听 {name}：任务定义无效，不检查：{str(e)[:200]}", flush=True)
                self._spawn(self._notify(f"❌ 任务 {name}：定义校验失败：{str(e)[:1500]}\n它的监听暂停（不检查、不收事件）；修好 tasks/{name}/ 后自动恢复"))
            return None
        self._errors.pop(name, None)
        return task

    def _hold(self, task: Task) -> bool:
        """运行记录说先别查（hold_reason）：原因变了才记日志；进入「连续失败暂停」时通知一次。恢复了记一条日志。"""
        name = task.name
        h = hold_reason(name, task.listen_file)
        if h is None:
            if self.held.pop(name, None) is not None:
                print(f"监听 {name}：恢复检查", flush=True)
            return False
        kind, why = h
        if self.held.get(name) != why:
            self.held[name] = why
            print(f"监听 {name}：暂不检查：{why}", flush=True)
            if kind == "failing":
                self._spawn(self._notify(f"⚠️ 任务 {name} {why}，已暂停它的监听检查（不再检查、不再入队）。"
                                         f"修好任务定义（改了 task.yaml 或 listen.py 就恢复）或手动跑成功一次后自动恢复"))
        return True

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _notify(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception as e:  # noqa: BLE001 — 通知发不出去不影响检查循环
            print(f"监听：通知发送失败：{type(e).__name__}: {str(e)[:200]}", flush=True)

    async def _one(self, owner: Task, ready: list[Task]) -> None:
        try:
            await self.check_once(owner, ready)
        finally:
            self.busy.discard(str(owner.listen))

    async def check_once(self, task: Task, targets: list[Task] | None = None) -> bool:
        """检查一次（task 是定义者或引用者，调的都是定义的 listen.py）；fire 就把 targets（不给 = 只有 task）里没在队列里的各入队一次，
        用同一个 prompt。返回是否有任务入队了。"""
        targets = targets or [task]
        t0 = time.monotonic()
        try:
            data = await call(task, "check", "interval", tasks=[t.name for t in targets])
        except Exception as e:  # noqa: BLE001 — 起不来、报错、超时、格式不对都算检查失败
            await self._failed(task, f"{failure_why(task, e)}（{time.monotonic() - t0:.1f} 秒）")
            return False
        took = f"{time.monotonic() - t0:.1f} 秒"
        if not data["fire"]:
            print(f"监听 {task.listen}：check 不触发（{took}）", flush=True)
            self._ok(str(task.listen))
            return False
        return await self._enqueue(str(task.listen), rel_file(task), "check", targets, data["prompt"], f"（{took}）")

    async def _enqueue(self, key: str, rel: str, fn: str, targets: list[Task], prompt: str, note: str = "") -> bool:
        """fire 之后：targets 里没在队列里 / 没在跑的各入队一次（trigger=listen，同一个 prompt）。入队失败算一次失败。返回是否有任务入队了。"""
        done, errors = [], []
        for t in targets:
            if runner.task_pending(t.name):
                print(f"监听 {key}：{fn} 说要跑，但任务 {t.name} 已在队列里或在跑，不重复入队", flush=True)
                continue
            try:
                runner.enqueue(t.name, trigger="listen", prompt=prompt)
                done.append(t.name)
            except Exception as e:  # noqa: BLE001 — 入队失败同检查失败：计数、连续失败通知
                errors.append(f"{t.name}：{type(e).__name__}: {str(e)[:300]}")
        if errors:
            await self._failed_key(key, rel, f"{fn} 说要跑，但入队失败：{'；'.join(errors)}")
        else:
            self._ok(key)
        if done:
            print(f"监听 {key}：{fn} 触发，已入队 {'、'.join(done)}{note}", flush=True)
        return bool(done)

    def _ok(self, key: str) -> None:
        if self.fails.pop(key, 0) >= FAIL_NOTIFY:
            print(f"监听 {key}：监听程序恢复正常", flush=True)

    async def _failed(self, task: Task, why: str) -> None:
        await self._failed_key(str(task.listen), rel_file(task), why)

    async def _failed_key(self, key: str, rel: str, why: str, what: str = "") -> None:
        """一次失败：计数、记日志；连续 FAIL_NOTIFY 次通知一次（恢复前不再发）。rel：程序的位置；what：通知里怎么称呼它（默认「监听 <key> 的监听程序（rel）」）。"""
        n = self.fails[key] = self.fails.get(key, 0) + 1
        print(f"监听 {key}：第 {n} 次失败，不入队：{why}", flush=True)
        if n == FAIL_NOTIFY:
            await self._notify(f"⚠️ {what or f'监听 {key} 的监听程序（{rel}）'}连续 {n} 次失败，这期间引用它的任务都不会入队（最近一次：{why}）。"
                               f"恢复前不再提醒；日志见 journalctl --user -u stbcc-hub 与 data/listen/{key}/listen.stderr.log")

    # ---------- 常驻监听（watch） ----------
    def _want_watch(self, owner: Task, tasks: list[Task]) -> None:
        """listen.py 有 watch 的监听：至少一个引用它的任务 启用就该常驻，否则不该。"""
        name = str(owner.listen)
        users = [t.name for t in tasks if t.enabled]
        if not users:
            self.wanted.pop(name, None)
            return
        f = owner.listen_file
        assert f is not None
        self.wanted[name] = Watch(key=name, task=owner, tasks=users, file=f, sig=_sig(f, task_file(owner.name)),
                                  label=f"监听 {name} 的常驻监听程序（{rel_file(owner)}）", command=lambda: command(owner))

    def _supervise(self, now: float) -> None:
        """对一次账：不该跑的（没人要了、定义者没了）和定义改了的停掉（改了的停完马上按新定义起）；该跑、没在跑、过了等待时刻的起。"""
        if self.extra is not None:
            try:
                extra = {w.key: w for w in self.extra()}
            except Exception as e:  # noqa: BLE001 — 登记方出错不拖垮监听循环：这次照旧
                print(f"常驻监听：登记来的监听读不了：{type(e).__name__}: {str(e)[:200]}", flush=True)
                extra = {k: self.wanted[k] for k in self._extra_keys if k in self.wanted}
            for k in self._extra_keys - set(extra):
                self.wanted.pop(k, None)
            self._extra_keys = set(extra)
            self.wanted.update(extra)
        for key, lv in list(self.live.items()):
            w = self.wanted.get(key)
            if lv.stopping:
                continue
            if w is None:
                self._spawn(self._stop(lv, "不再需要"))
            elif w.sig != lv.w.sig:
                self.next_start[key] = 0
                self.backoff.pop(key, None)
                self._spawn(self._stop(lv, "定义改了"))
        for key in [k for k in self.backoff if k not in self.wanted and k not in self.live]:
            self.backoff.pop(key, None)
            self.next_start.pop(key, None)
            self.fails.pop(key, None)
        for key, w in self.wanted.items():
            if key not in self.live and now >= self.next_start.get(key, 0):
                lv = self.live[key] = _Live(w, time.monotonic())
                lv.job = asyncio.create_task(self._run(lv))
                self._bg.add(lv.job)
                lv.job.add_done_callback(self._bg.discard)

    async def _stop(self, lv: _Live, why: str) -> None:
        """停一个常驻监听：关 stdin（请它退出），等 WATCH_STOP_S 秒；还在就 SIGTERM（sudo 转给程序），再等，最后 SIGKILL。"""
        lv.stopping = why
        print(f"监听 {lv.w.key}：停常驻监听（{why}）", flush=True)
        proc = lv.proc
        if proc is None:                       # 还在起（准备命令）：_run 起好进程后看到 stopping 会自己停
            return
        for step in ("close", "term", "kill"):
            if proc.returncode is not None:
                return
            try:
                if step == "close" and proc.stdin is not None:
                    proc.stdin.close()
                elif step == "term":
                    proc.terminate()
                elif step == "kill":
                    proc.kill()                # 只按这个子进程的 PID
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), WATCH_STOP_S)
            except TimeoutError:
                continue

    async def _run(self, lv: _Live) -> None:
        """起一个常驻监听、读它的输出直到退出：每行 {"emit": …} 交给 _emit；退出后（不是主动停的）按等待重起，崩溃 / 报错算一次失败。"""
        w, key = lv.w, lv.w.key
        why, normal = "", False
        log = None
        live = listening_dir() / f"{key}.json"
        try:
            cmd, env = await asyncio.to_thread(w.command)                # 里面读文件、建目录：不堵事件循环
            ctx = {**await asyncio.to_thread(make_ctx, w.task, "watch", w.tasks), "listen": key, "state_dir": str(state_dir(key)), **w.ctx}
            if lv.stopping:
                return
            req = json.dumps({"repo": str(REPO), "file": str(w.file), "fn": "watch", "args": [ctx]}, ensure_ascii=False) + "\n"
            log = _errlog(key)
            proc = lv.proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                                  stderr=log, limit=WATCH_LINE_MAX)
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text(json.dumps({"key": key, "task": w.task.name, "tasks": w.tasks, "pid": proc.pid, "started": time.time()}, ensure_ascii=False))
            print(f"监听 {key}：常驻监听已起（{w.file}）", flush=True)
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(req.encode())
            await proc.stdin.drain()
            if lv.stopping:
                await self._stop(lv, lv.stopping)
            async for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and "emit" in msg:
                    await self._emit(w, msg["emit"])
                elif isinstance(msg, dict) and "ok" in msg:
                    normal = bool(msg["ok"])
                    why = "" if normal else f"watch 报错：{str(msg.get('error'))[:500]}"
            rc = await proc.wait()
            if not normal and not why:
                why = f"监听程序退出了（退出码 {rc}）"
        except Exception as e:  # noqa: BLE001 — 起不来、读输出出错：同崩溃
            why = f"{type(e).__name__}: {str(e)[:500]}"
            if lv.proc is not None and lv.proc.returncode is None:
                lv.proc.kill()
        finally:
            if log is not None:
                log.close()
            live.unlink(missing_ok=True)
            if self.live.get(key) is lv:
                self.live.pop(key)
        if lv.stopping:
            print(f"监听 {key}：常驻监听已停（{lv.stopping}）", flush=True)
            return
        up = time.monotonic() - lv.started
        wait = WATCH_BACKOFF_MIN_S if up >= WATCH_STABLE_S or key not in self.backoff else min(self.backoff[key] * 2, WATCH_BACKOFF_MAX_S)
        self.backoff[key] = wait
        self.next_start[key] = time.time() + wait
        if normal:
            print(f"监听 {key}：watch 自己返回了（跑了 {up:.0f} 秒），{wait:.0f} 秒后重起", flush=True)
        else:
            await self._failed_key(key, str(w.file), f"{why}（跑了 {up:.0f} 秒；{wait:.0f} 秒后重起）", w.label)

    async def _emit(self, w: Watch, data: Any) -> None:
        """一次 emit：登记来的交给它的 on_emit；listen.py 的按 check fire 的规则入队（_emit_listen）。处理出错算一次失败。"""
        try:
            if w.on_emit is not None:
                await w.on_emit(data if isinstance(data, dict) else {"fire": data})
                self._ok(w.key)
            else:
                await self._emit_listen(w, data)
        except Exception as e:  # noqa: BLE001
            await self._failed_key(w.key, str(w.file), f"处理 emit 出错：{type(e).__name__}: {str(e)[:300]}", w.label)

    async def _emit_listen(self, w: Watch, data: Any) -> None:
        """listen.py 的 emit：格式同 check 的返回值（不对算一次失败）；fire 时按当时引用它的任务（启用）各入队一次，
        跳过规则同 check fire：在等 Joy / 连续失败暂停（_hold）、已在队列 / 在跑（_enqueue），都记日志。"""
        try:
            res = parse_result("watch 的 emit", data)
        except CheckError as e:
            await self._failed_key(w.key, str(w.file), str(e), w.label)
            return
        if not res["fire"]:
            print(f"监听 {w.key}：watch 说不触发", flush=True)
            self._ok(w.key)
            return
        targets = []
        for n in listen_users().get(w.key, []):
            t = self._load(n, time.time())
            if t is None or not t.enabled:
                continue
            if self._hold(t):
                print(f"监听 {w.key}：watch 说要跑，但任务 {t.name} 暂不入队：{self.held.get(t.name)}", flush=True)
                continue
            targets.append(t)
        if targets:
            await self._enqueue(w.key, str(w.file), "watch", targets, res["prompt"])
        else:
            self._ok(w.key)
