"""stbcc-runner：常驻的串行执行器（规格第 9、9.1、10 节）。

队列 = ~/sparky-toolbox/data/runtime/queue/*.json（`stb run --now`、stbcc-hub（定时触发、监听、事件）写入）。每 2 秒扫一次，严格按 (priority 大者先, 入队时间) 取一个（不看模型有没有加载），
持全局模型租约串行跑：建运行目录 → 起网关 → 起沙箱 → 等结束/超时 → 投递 → 销毁。

抢占（9.1）：运行中若队列里出现更高优先级的条目且当前任务可抢占（agent 任务一律可以；workflow 任务看 resumable，默认可以），向网关发"暂停"；沙箱里的 pi 在下一个
模型/工具调用边界以退出码 75 退出，运行标 paused、沙箱销毁、写检查点（会话文件 + 网关状态 + 工作目录），并把
"恢复"条目按原优先级、原入队时间放回队列。宽限 preempt_grace（agent 任务 PREEMPT_GRACE_AGENT_S）内边界不到则强停，恢复时从上一检查点重来。
时间账：timeout 只算主模型干活的时间（运行片段的墙钟减去子代理调用与就地等问题卡 / 审批卡的时间，子代理调用里的模型切换也在其中）；一次运行的总时长没有别的上限。
主模型时间（timeout）到点不直接判失败（D34）：同一会话再给一轮收尾（WRAPUP_S，调工具一律被拒），运行记为 partial，
通知带「续跑」按钮；续跑 = 同一运行目录与会话再给一份 timeout。停滞照旧硬停。
停滞（agent 任务，平台固定规则）：STALL_S 秒既没有模型输出、也没有在途的工具调用（含子代理调用）才算卡住，强停判失败；工具执行期间不算（长调用由工具自己的超时兜底）；workflow 任务不判。
ask-user（D35）：问题卡发出后网关先就地等 Joy 回答（acquire_permission.WAIT_S，等待时间不算主模型时间）；没等到就让出，运行记 waiting
（不占运行器、不自动恢复），Joy 之后回答才按任务优先级入队续跑，答案作为下一条用户消息送进同一会话。
抢占警告：同一次运行被抢占超过 PREEMPT_WARN_AFTER 次、且任务优先级 ≥ PREEMPT_WARN_MIN_PRIORITY 才私信提醒（低优先级任务本来就是让路的）。
workflow 任务（run_workflow，步骤之间的交接见 control_center/workflow.py）：runner 只跑要资源的步骤，每步是一个按任务 priority 排队的条目（步骤之间自然让出）。
步骤中间被抢占（resumable 默认 true）没有网关边界：立刻给这一步的进程组发 SIGTERM，它把进度存到持久路径后以退出码 75 退出；
宽限 PREEMPT_GRACE_COMMAND_S 到期还没退就 SIGKILL（存完早退出就早结束，不用等满）。被信号杀掉（SIGKILL 或没处理的 SIGTERM）也算暂停，但 run.json 记下这次没确认存下进度；
请求暂停后以其他非 0 退出码退出算失败；没请求暂停却以 75 退出也算失败。恢复 = 在同一个运行目录重跑这一步，环境变量 STB_RESUME=1。
运行期间同一任务再入队的条目：订阅了事件（listen_events 不空，Task.queues_overlap）的保留排队；其余在运行结束后丢弃。
运行结束一律通知 Joy（deliver）：agent 任务成功时自己发过消息就不补发、没发过就发最终回答，失败 / 部分完成发错误摘要（部分完成附续跑卡）；
workflow 任务成功发步骤写进 reply.txt 的文字（没写就发「✅ 完成」），以退出码 3 结束的（没事可做）不发，失败发错误摘要。
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_center import cards, acquire_permission, builder, hooks, hub_client, memory_docs, reply, sandbox, systemd, todo
from control_center.gateway import WRAPUP_NOTE, Gateway, GatewayError
from control_center.models import PI_PROVIDER, START_TIMEOUT_S, _container_running, call_name, exclusive, is_up, load_models, loading, pi_models_json, profiles, resident, running
from control_center.paths import CONTROL, HOME, INBOX, QUEUE, REGISTERED, REPO, RUNNER_STATE, RUNS, STEPS, TASKS, WORK, ensure_dirs, run_work_dir, task_file, task_files  # noqa: F401 — TASKS 留给测试写任务文件
from control_center.schedule import last_occurrence
from control_center.prompts import START_PROMPT
from control_center.task import Task, load_task

POLL_S = 2.0
PAUSE_EXIT_CODE = 75
SIGNAL_EXITS = {-signal.SIGKILL, -signal.SIGTERM, 128 + signal.SIGKILL, 128 + signal.SIGTERM}   # 命令被信号杀掉（负数：进程本身；128+n：shell 报的子进程）
PREEMPT_WARN_AFTER = 5
PREEMPT_WARN_MIN_PRIORITY = 5   # 低于它的任务（如 tenbagger-research 的 3）本来就是给别的任务让路的，被抢占多少次都不提醒
RESUME_PROMPT = "（系统消息）你上一次运行在此处被暂停；上面的对话与工具结果都已保留，已经发出去的消息不要重发。现在从这里继续完成原任务。"
WRAPUP_S = 300              # 收尾轮（D34）的上限：主模型时间用完后同一会话再给一轮，只能写回答、不能调工具
WRAPUP_PROMPT = f"（系统消息）{WRAPUP_NOTE}。"
STALL_S = 300               # 停滞判定（agent 任务）：这么久既没有模型输出、也没有在途的工具调用就判卡住（tick 里判；workflow 任务不判）
# 请求暂停（抢占 / stb pause / /stop）后等检查点的上限（平台常量，任务 yaml 不写；早停早结束，到点强停），见 preempt_grace：
PREEMPT_GRACE_AGENT_S = 180     # agent 任务：等到下一个模型 / 工具调用边界（pi 在边界退出），等不到就强停，恢复时从上一检查点重来
PREEMPT_GRACE_COMMAND_S = 1200  # workflow 的步骤：等它自己存完进度退出（训练存检查点要几分钟），到了还没退就 SIGKILL
NOTHING_EXIT_CODE = 3           # workflow 的步骤以它退出：没事可做，运行到此结束（complete），不发消息
STEPS_DIR = "steps"            # workflow：运行目录里给步骤的可写目录（步骤的 STB_RUN_DIR；运行目录本身对 sparky 只读）
REPLY_FILE = "reply.txt"        # workflow：步骤写进 steps/ 里这个文件的文字，运行 complete 时原样发给 Joy
UNLIMITED_S = 10 * 365 * 86400   # timeout: none 时给沙箱的截止秒数：实际等于不限
CONTINUE_PROMPT = ("（系统消息）Joy 点了「续跑」，主模型时间重新给了一份（{timeout}）。上面的对话与工具结果都已保留，已经发出去的消息不要重发；"
                   "从上次标明没做完的地方接着做完原任务。")
CONTINUE_KIND = "continue"  # 「续跑」按钮所在的卡
DONE_NOTE = "✅ {task} 完成"     # 运行成功、agent 没发过消息、最终回答也是空的：通知只说完成了
EMPTY_PROMPT = "（系统消息）你上一条回复没有任何文字，也没有调用工具，运行就停下了。请接着把事情做完；做完了就直接写出回答。"
EMPTY_NOTE = "⚠ {task} 无回答\n运行目录: {dir}"   # 补过一次「继续」，最后一条回复还是没文字也没调工具：不说完成


def _atomic_write(p: Path, text: str) -> None:
    """断电安全地写状态文件：写同目录临时文件 → fsync → os.replace 原子替换 → fsync 目录。
    断电时读到的要么是旧内容、要么是新内容，不会是半截（半截的队列文件会被 _queue_entries 当坏条目删掉，任务就丢了）。"""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")    # 带 pid：stbcc-hub 与 runner 可能同时入队
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    fd = os.open(p.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------- 队列 ----------
# 队列条目与 run.json 的 trigger：schedule（stbcc-hub 定时触发）/ listen（监听 fire、事件、一次性监听回调）/ manual（手动）/ catchup（补跑）；
# 旧记录里的 timer 读的时候当 schedule（file、dream 只在旧记录里）
def norm_trigger(trigger: str | None, default: str = "manual") -> str:
    return "schedule" if trigger == "timer" else str(trigger or default)


def enqueue(task_name: str, trigger: str = "schedule", prompt: str | None = None, resume: str | None = None,
            priority: int | None = None, enqueued_at: float | None = None, ref: dict[str, str] | None = None,
            attachments: list[str] | None = None, catchup_slot: float | None = None, files: list[str] | None = None, cont: bool = False,
            answer: bool = False, channel: str | None = None, source: dict[str, Any] | None = None, watch: str | None = None,
            steer: str | None = None, loads_since: float | None = None) -> Path:
    ensure_dirs()
    task = load_task(task_file(task_name))
    entry: dict[str, Any] = {"task": task.name, "priority": priority if priority is not None else task.priority,
                             "enqueued_at": enqueued_at if enqueued_at is not None else time.time(), "trigger": norm_trigger(trigger)}
    if prompt is not None:
        entry["prompt"] = prompt          # 这次的用户消息：由触发方（监听、事件的 listen.py）给出
    if resume is not None:
        entry["resume"] = resume          # 从检查点恢复的运行 id
    if cont:
        entry["continue"] = True          # D34 续跑：resume 指的是 partial 的运行，再给一份 timeout
    if answer:
        entry["answer"] = True            # D35：resume 指的是 waiting 的运行，Joy 答了问题卡
    if ref is not None:
        entry["ref"] = ref                # 消息标记：渠道给的那条消息（渠道按它显示排队 / 进行中 / 结果，如 Discord 的表情）
    if loads_since is not None:
        entry["loads_since"] = loads_since   # 私信任务的上下文起点：这之后本任务各次运行加载过的工具与 skill 这次接着有效（carry_loads）
    if steer is not None:
        entry["steer"] = steer            # 这条可以插进本任务正在跑的对话，插进去时用的文字（listen.py 给的，不重复带它拼进 prompt 的上下文；gateway.take_steers）
    if attachments:
        entry["attachments"] = attachments  # 沙箱视角的图片路径，随 prompt 附给模型
    if catchup_slot is not None:
        entry["catchup_slot"] = catchup_slot  # 补跑：原定触发时刻
    if channel:
        entry["channel"] = channel          # 来源渠道（渠道消息从哪个渠道来，如 discord、web）：回复只回这个渠道；不写（定时、监听、别的事件）= 发给开着的各渠道
    if source:
        entry["source"] = source            # 来源：事件的 {event: 来源名}，stb chat send 的另带 origin: cli
    if watch:
        entry["watch"] = watch              # 一次性动态监听触发的运行（control_center/monitors.py）：状态栏写"<任务>·<监听名>"
    if files:
        entry["files"] = files              # 图片以外的附件：音视频是工作目录 in/ 下的路径，其余是文件名（在 ~/sparky-toolbox/data/hub/inbox/<task>/，运行开始时拷进该次运行的 in/）
    if task.workflow and resume is None:    # 新触发的 workflow 运行先交 stbcc-hub（workflow.dispatch）：同一任务没有别的运行在跑才开，第 1 步要资源的再放进这个队列
        return requeue(entry, STEPS)
    return requeue(entry)


def requeue(entry: dict[str, Any], folder: Path | None = None) -> Path:
    """条目写进队列（folder 不给 = runner 的队列；STEPS = stbcc-hub 马上跑的 workflow 步骤）；也用于取出过、没开跑的条目原样放回（trigger、优先级、入队时刻、resume 等都不变）。"""
    ensure_dirs()
    folder = QUEUE if folder is None else folder
    ms = int(time.time() * 1000)
    p = folder / f"{ms}-{entry['task']}.json"
    while p.exists():                     # 同一毫秒同名任务（比如恢复条目 + 新触发）不能互相覆盖
        ms += 1
        p = folder / f"{ms}-{entry['task']}.json"
    _atomic_write(p, json.dumps(entry))
    return p


def _queue_entries() -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for p in sorted(QUEUE.glob("*.json")):
        try:
            e = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            p.unlink(missing_ok=True)
            continue
        out.append((p, e))
    return out


def pick() -> tuple[Path, dict[str, Any]] | None:
    entries = _queue_entries()
    if not entries:
        return None
    return min(entries, key=lambda e: (-int(e[1].get("priority", 5)), float(e[1].get("enqueued_at", 0))))


def _write_state(current: dict[str, Any] | None) -> None:
    _atomic_write(RUNNER_STATE, json.dumps({"pid": os.getpid(), "updated": time.time(), "current": current}, ensure_ascii=False))


MEMINFO = Path("/proc/meminfo")


class MemPeak:
    """运行期间的内存峰值（规格第 12 节）：每 POLL_S 秒读一次 /proc/meminfo，记已用内存（MemTotal − MemAvailable）与已用 swap（SwapTotal − SwapFree）
    的最大值（GiB，一位小数）。run_task 起一个后台任务跑 watch，这一段结束时 result() 再读一次、交出峰值（agent 任务和 workflow 的步骤一样）。"""

    def __init__(self) -> None:
        self.peak: dict[str, float | None] = {"mem_used_gb": None, "swap_used_gb": None}

    def sample(self) -> None:
        try:
            kb = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in MEMINFO.read_text().splitlines() if ":" in line)}
            used = {"mem_used_gb": kb["MemTotal"] - kb["MemAvailable"], "swap_used_gb": kb["SwapTotal"] - kb["SwapFree"]}
        except (OSError, ValueError, KeyError, IndexError):
            return
        for k, v in used.items():
            gb = round(v / 1024 ** 2, 1)
            self.peak[k] = gb if self.peak[k] is None else max(self.peak[k], gb)  # pyright: ignore[reportArgumentType]

    async def watch(self) -> None:
        while True:
            self.sample()
            await asyncio.sleep(POLL_S)

    def result(self) -> dict[str, float | None]:
        self.sample()
        return dict(self.peak)


def files_note(files: list[str]) -> str:
    """图片以外的附件说明，内容都是不可信的：音视频（中枢放进工作目录的 in/，给路径）提示用能处理音视频的工具；其余任何类型（只给文件名）
    主 agent 只能交给能读附件的隔离子代理读（D17）。平台不写是哪个子代理 / 工具，主模型按说明挑。"""
    docs = [f for f in files if "/" not in f]
    media = [f for f in files if "/" in f]
    notes = []
    if docs:
        refs = json.dumps(docs, ensure_ascii=False)
        notes.append(f"（Joy 随这条消息发来了附件：{'、'.join(docs)}。你看不到内容；要读就调用能读附件的子代理，"
                     f"prompt 里写附件文件名 {refs} 和要回答的问题）")
    if media:
        notes.append(f"（Joy 随这条消息发来了音视频文件：{'、'.join(media)}。要用里面的内容就调用能处理音视频的工具，写这个路径）")
    return "\n".join(notes)


def build_prompt(task: Task, prompt: str | None, files: list[str] | None = None) -> str:
    """这次运行的用户消息 = 条目带来的内容（listen.py 给的 prompt：私信任务自己拼好的最近对话 + 这条私信、dreaming 的上次整理之后的私信对话…）
    + [附件说明] + [任务 yaml 的 memory 清单：记忆块，memory_docs.render]。每次运行都是新会话，不沿用上一轮的。
    条目没带内容（prompt 为空）时用开始语（这次只靠 system_prompt）。任务的 system_prompt 走系统提示（sandbox）。"""
    parts = [prompt if prompt else START_PROMPT]
    if files:
        parts.append(files_note(files))
    if task.memory:
        parts.append(memory_docs.render(task.memory))
    if task.tool(todo.TOOL) is not None:         # 列了 todo：上次留下没清掉的待办
        parts.append(todo.start_note(task.name))
    return "\n\n".join(x for x in parts if x)


def stage_files(task: Task, run_dir: Path, files: list[str]) -> list[str]:
    """把要隔离读的附件从 ~/sparky-toolbox/data/hub/inbox/<task>/ 拷进该次运行的 in/（沙箱不挂这个目录，只有读附件的工具读，它只给隔离子代理用）。
    只认纯文件名；找不到的跳过。工作目录 in/ 下的路径（中枢已拷进工作目录的音视频）不拷，在就留着。返回实际可用的文件名 / 路径。"""
    staged = []
    for name in files:
        if "/" in name:
            p = Path(name)
            if p.parent == WORK / task.name / "in" and p.is_file():
                staged.append(name)
            continue
        src = INBOX / task.name / name
        if name != Path(name).name or not src.is_file():
            continue
        (run_dir / "in").mkdir(exist_ok=True)
        shutil.copy(src, run_dir / "in" / name)
        staged.append(name)
    return staged


def task_pending(name: str) -> bool:
    """队列里有这个任务的条目（含被抢占后等恢复的、已取出正在加载模型的 .json.loading、加载失败留着等 Joy 的 .json.held），或它正在跑；
    workflow 任务另算 stbcc-hub 那边的步骤条目与没结束的运行（在跑旁路步骤、步骤之间等下一步，workflow.active）。"""
    from control_center import workflow

    if any(e.get("task") == name for _, e in _queue_entries()):
        return True
    if workflow.active(name):
        return True
    for p in [*QUEUE.glob("*.json" + LOADING_SUFFIX), *QUEUE.glob("*.json" + HELD_SUFFIX)]:
        try:
            if json.loads(p.read_text()).get("task") == name:
                return True
        except (OSError, ValueError, AttributeError):
            continue
    try:
        cur = json.loads(RUNNER_STATE.read_text()).get("current") or {}
    except (OSError, json.JSONDecodeError):
        cur = {}
    return cur.get("task") == name


def _session_files(sdir: Path) -> list[Path]:
    """会话目录（<运行目录>/agent/session）里的 pi 会话文件，按修改时间排序。"""
    return sorted(sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def _latest_session(sdir: Path) -> Path | None:
    files = _session_files(sdir)
    return files[-1] if files else None


def _session_used(sdir: Path | None, session_file: Path | None, before: dict[Path, float]) -> Path | None:
    """本次运行真正用过的会话文件：续用的那个被这次改过，或新会话由这次新建。pi 没写文件就失败时返回 None，
    不能误把上一段正常对话当成本次的（D16）。"""
    if sdir is None or not sdir.exists():
        return None
    touched = [f for f in _session_files(sdir) if f not in before or f.stat().st_mtime > before[f]]
    if session_file is not None:
        return session_file if session_file in touched else None
    return touched[-1] if touched else None


# ---------- 运行 ----------
class Run:
    def __init__(self, task: Task, trigger: str, resume: str | None = None, cont: bool = False, answer: bool = False,
                 load_seconds: float = 0.0, run_id: str | None = None) -> None:
        self.task, self.trigger = task, norm_trigger(trigger)
        self.final_text = ""
        self.reply = ""                   # D35：续跑时送进会话的答案
        self.answered: list[dict[str, Any]] = []   # 续跑时送进会话的各轮（acquire_permission.resume_rounds）
        self.started = time.time()
        self.mem = MemPeak()              # 这一段的内存峰值（run_task 期间自己采样）
        if resume:
            self.run_id = resume
            self.dir = RUNS / task.name / resume
            self.info = json.loads((self.dir / "run.json").read_text())
            self.info["trigger"] = norm_trigger(self.info.get("trigger"), self.trigger)
            want = "partial" if cont else "waiting" if answer else "paused"
            self.rerun = self.info.get("status") == "paused"     # workflow：重跑被暂停的那一步（STB_RESUME=1）；stepping 是接着跑下一步
            if self.info.get("status") != want and not (task.workflow and self.info.get("status") == "stepping"):
                raise RuntimeError(f"运行 {resume} 状态是 {self.info.get('status')}，不是 {want}")
            if answer:  # D35：挂起等回答的时间（到 Joy 作答为止）记下
                self.answered = acquire_permission.resume_rounds(self.dir)
                self.reply = acquire_permission.reply_prompt(self.answered)
                since = float(self.info.pop("waiting_since", 0) or 0)
                if since:
                    first = min(float(r.get("answered_at") or self.started) for r in self.answered)
                    self.info["waiting_seconds"] = round(float(self.info.get("waiting_seconds", 0)) + max(0.0, first - since), 1)
                for k in ("waiting_reason", "ask_card", "ask_round"):
                    self.info.pop(k, None)
            if cont:   # D34 续跑：之前用掉的主模型时间记为 credit，这一段重新给满一份 timeout
                self.info["timeout_credit"] = float(self.info.get("elapsed_seconds", 0)) - float(self.info.get("subagent_seconds", 0))
                self.info["continued"] = int(self.info.get("continued", 0)) + 1
            self.info["status"] = "running"
            segs = self.info.setdefault("segments", [])
            segs.append({"started": self.started})
            # 暂停总时长 = 状态为 paused 的段结束到下一段开始的间隔之和（按段现算，旧记录也对）
            self.info["paused_seconds"] = round(sum(max(0.0, float(b["started"]) - float(a["ended"])) for a, b in zip(segs, segs[1:])
                                                    if a.get("status") == "paused" and a.get("ended") is not None), 1)
        else:
            self.rerun = False
            self.run_id = run_id or new_run_id()          # 运行单元：运行器起单元前已定好运行 id（单元名里要用）
            self.dir = RUNS / task.name / self.run_id
            self.dir.mkdir(parents=True, exist_ok=False)
            self.info = {"task": task.name, "run_id": self.run_id, "status": "running", "trigger": self.trigger,
                         "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(), "first_started": self.started,
                         "runtime": task.runtime, "model": task.model, "priority": task.priority, **({"step": 1} if task.workflow else {}),
                         "elapsed_seconds": 0.0, "preempt_count": 0, "paused_seconds": 0.0, "segments": [{"started": self.started}]}
        if load_seconds:
            self.info["load_seconds"] = round(float(self.info.get("load_seconds", 0)) + load_seconds, 1)   # 开跑前加载模型的时间（只作记录，不算主模型时间）
        self.save()

    def save(self) -> None:
        _atomic_write(self.dir / "run.json", json.dumps(self.info, ensure_ascii=False, indent=1))

    @property
    def remaining_seconds(self) -> int:
        """timeout 还剩的秒数（续跑的加上 credit），可以是负数：沙箱按它加上子代理时间（extra）定截止，恢复的运行不会多给一截时间。
        timeout: none（不限）给 UNLIMITED_S。"""
        if self.task.timeout_seconds is None:
            return UNLIMITED_S
        return int(self.task.timeout_seconds + float(self.info.get("timeout_credit", 0)) - float(self.info.get("elapsed_seconds", 0)))


class RunControl:
    """运行器主循环与 run_task 之间的把手：请求暂停、宽限强停。"""

    def __init__(self) -> None:
        self.gateway: Gateway | None = None
        self.container: str | None = None
        self.proc: Any = None                      # workflow：这一步的进程（暂停 / 强停按它的进程组发信号）
        self.pause_requested_at: float | None = None
        self.pause_reason: str | None = None       # preempt | manual | stop（/stop）
        self.killed = False
        self.stalled_seconds: float | None = None  # 停滞判定触发：记录停了多久
        self.stop_reason: str | None = None        # 运行器自己要退出（SIGTERM，D18）：按失败收尾
        self.run_dir: Path | None = None
        self.run_started: float = 0.0
        self.command = False                       # workflow 的步骤：暂停直接发 SIGTERM，没有网关边界
        self.as_user: str | None = None            # 步骤以这个用户跑（sparky）：发信号、查进程组要以它的身份
        self.owns_state = True                     # 运行单元里为 False：runner.json 只由运行器写

    def request_pause(self, reason: str) -> None:
        if self.pause_requested_at is None:
            self.pause_requested_at = time.time()
            self.pause_reason = reason
            if self.gateway:
                self.gateway.pause_requested = True
            if self.command and self.proc is not None:     # 步骤收到 SIGTERM 存进度、以 75 退出；进程还没起来时由 run_workflow 起来后补发
                sandbox.kill_group(self.proc.pid, 15, self.as_user)

    def last_activity(self) -> float:
        """最近一次有动静的时刻：模型端点有字节流过、工具调用开始/结束（网关记录），以及 pi 事件流与 toollog 的修改时间，取最近。"""
        t = max(self.run_started, self.gateway.last_activity if self.gateway else 0)
        if self.run_dir:
            for f in (self.run_dir / "agent" / "pi-output.jsonl", self.run_dir / "agent" / "toollog.jsonl"):
                try:
                    t = max(t, f.stat().st_mtime)
                except OSError:
                    pass
        return t

    def stall_stop(self, seconds: float) -> None:
        self.stalled_seconds = seconds
        self.force_stop()

    def force_stop(self) -> None:
        """先 SIGTERM（pi 会正常收尾），10 秒不退就 KILL（规格第 9 节）。"""
        if self.killed:
            return
        self.killed = True
        if self.command and self.pause_requested_at is not None and self.proc is not None:   # 暂停宽限已到：SIGTERM 早发过了，直接 KILL
            sandbox.kill_group(self.proc.pid, 9, self.as_user)
        elif self.proc is not None:
            sandbox.kill_group(self.proc.pid, 15, self.as_user)
            asyncio.get_event_loop().call_later(10, sandbox.kill_group, self.proc.pid, 9, self.as_user)
        elif self.container:
            subprocess.run(["docker", "kill", "--signal", "TERM", self.container], capture_output=True)
            name = self.container
            asyncio.get_event_loop().call_later(10, lambda: subprocess.Popen(["docker", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def log_tail(p: Path, n: int = 600) -> str:
    """日志末尾 n 字（接在报错后面，换行开头）：读不了、空的也说明。"""
    try:
        t = p.read_text(errors="replace")[-n:].strip()
    except OSError as e:
        return f"\n（{p.name} 读不了：{type(e).__name__}: {e}）"
    return f"\n{p.name} 末尾：\n{t}" if t else f"\n（{p.name} 是空的）"


def earlier_loads(task: str, since: float, current: Path) -> dict[str, str]:
    """本任务开跑时刻在 since 之后的各次运行（current 这次除外）加载过的工具与 skill → 加载结果原文（运行目录的 loads.json，按开跑先后，后加载的为准）。"""
    from control_center.gateway import LOADS_FILE

    out: dict[str, str] = {}
    for d in sorted((RUNS / task).iterdir()) if (RUNS / task).is_dir() else []:
        if d == current or not (d / LOADS_FILE).is_file():
            continue
        try:
            started = datetime.fromisoformat(json.loads((d / "run.json").read_text())["started"]).timestamp()
            texts = json.loads((d / LOADS_FILE).read_text()).get("texts") or {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if started < since:
            continue
        for k, v in texts.items():
            out.pop(k, None)
            out[str(k)] = str(v)
    return out


def save_checkpoint(run: Run, gw: Gateway, session_file: Path | None) -> None:
    ck = run.dir / "checkpoint"
    ck.mkdir(exist_ok=True)
    _atomic_write(ck / "gateway.json", json.dumps({"agent_messages": gw.agent_messages, "calls": gw.calls, "rejections": gw.rejections,
                                                   "loaded": list(gw.loaded)}, ensure_ascii=False, indent=1))
    _atomic_write(ck / "session", str(session_file) if session_file else "")


def load_checkpoint(run: Run, gw: Gateway) -> Path | None:
    ck = run.dir / "checkpoint"
    if not (ck / "gateway.json").exists():
        return None
    st = json.loads((ck / "gateway.json").read_text())
    gw.agent_messages, gw.calls, gw.rejections = int(st.get("agent_messages", 0)), int(st.get("calls", 0)), int(st.get("rejections", 0))
    gw.restore = [str(n) for n in st.get("loaded") or []]   # 上一段经 load-tool 加载过的：运行器随后 restore_loaded 重新加载
    s = (ck / "session").read_text().strip()
    return Path(s) if s else None


async def run_task(task: Task, trigger: str = "manual", prompt: str | None = None, resume: str | None = None,
                   ctl: RunControl | None = None, ref: dict[str, str] | None = None, attachments: list[str] | None = None,
                   files: list[str] | None = None, cont: bool = False, answer: bool = False, load_seconds: float = 0.0,
                   channel: str | None = None, source: dict[str, Any] | None = None, run_id: str | None = None, steer: str | None = None,
                   loads_since: float | None = None) -> Run:
    """一次运行（一段）：新开的先跑 run_start hook（任务、用到的工具与子代理的，control_center/hooks.py；退出码 2 = 不开跑，记失败、私信 Joy），
    再跑 workflow 的这一步或 agent；这一段结束（任何结束方式：完成、失败、超时、暂停、挂起等回答、/stop、运行器要停、被 run_start 拦下）、发结束通知之前跑 run_end hook
    （run_end），输入带 status；hook 输出 {"status": "failed", "error": …} 时把 complete 改判 failed，照常发失败通知。
    运行进程没收尾就没了（被硬杀、崩溃、断电）的，由运行器标失败时补跑 run_end（fail_stale_run）。"""
    ctl = ctl or RunControl()
    run = Run(task, trigger, resume, cont, answer, load_seconds, run_id)
    sampler = asyncio.create_task(run.mem.watch())        # 这一段的内存峰值：运行期间每 POLL_S 秒自己采样
    try:
        return await _run_task(task, run, ctl, trigger, prompt, resume, ref, attachments, files, cont, answer, channel, source, steer, loads_since)
    finally:
        sampler.cancel()


async def _run_task(task: Task, run: Run, ctl: RunControl, trigger: str, prompt: str | None, resume: str | None, ref: dict[str, str] | None,
                    attachments: list[str] | None, files: list[str] | None, cont: bool, answer: bool, channel: str | None,
                    source: dict[str, Any] | None, steer: str | None = None, loads_since: float | None = None) -> Run:
    if not resume:
        shutil.copy(task_file(task.name), run.dir / "task.yaml")
    owners = hooks.run_owners(task)
    work = WORK / task.name
    base = {"task": task.name, "run_id": run.run_id, "tool": None, "subagent": None, "function": None, "args": None}

    async def run_end(status: str, error: str) -> tuple[str, str]:
        """这一段的状态定了、通知之前：先存一次 run.json（hook 读得到这一段的 final_text 等），跑 run_end hook（输入带 status、error）；
        complete 且有 hook 要求改判，就改成 failed（原因是 hook 写的）。"""
        run.save()
        ends = hooks.run_owners(task, run.info.get("loaded")) if run.info.get("loaded") else owners   # 加载过的工具 / 子代理的 run_end 也跑
        got = await hooks.afire("run_end", ends, {**base, "status": status, "error": error}, run.dir, work)
        if status == "complete" and got.failed:
            return "failed", got.failed
        return status, error

    if not resume and (blocked := (await hooks.afire("run_start", owners, base, run.dir, work)).blocked) is not None:
        await run_end("failed", "")                       # 被拦下也算这一段结束：别的 hook 的 run_start 做过的事照样收尾
        return await _hook_blocked(task, run, ctl, blocked)
    if task.workflow:
        if not resume and prompt:
            from control_center import launch

            launch.grant(run.dir / STEPS_DIR)
            (run.dir / STEPS_DIR / "prompt.txt").write_text(prompt)    # 触发方（listen.py、事件）给的这次的内容：步骤自己读 $STB_RUN_DIR/prompt.txt
        return await run_workflow(task, run, ctl, run_end=run_end)
    return await _run_agent(task, run, ctl, trigger, prompt, resume, ref, attachments, files, cont, answer, channel, source, run_end, steer, loads_since)


async def _hook_blocked(task: Task, run: Run, ctl: RunControl, why: str) -> Run:
    """run_start hook 拦下：这次运行不开跑，记 failed、私信 Joy（不起沙箱、网关、步骤）。"""
    error = f"run_start hook 拦下了，没开跑：{why[:1000]}"
    ended_at = time.time()
    run.info["segments"][-1].update(ended=ended_at, seconds=round(ended_at - run.started, 1), status="failed")
    run.info.update(status="failed", error=error, ended=datetime.fromtimestamp(ended_at, timezone.utc).isoformat(), seconds=0.0)
    try:
        await hub_client.call("send", task=task.name, text=f"❌ {task.name} failed: {hub_client.escape(error)}\n运行目录: {run.dir}")
        run.info["notified"] = True
    except Exception as e:  # noqa: BLE001
        run.info["deliver_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    run.save()
    return run


async def _run_agent(task: Task, run: Run, ctl: RunControl, trigger: str, prompt: str | None, resume: str | None, ref: dict[str, str] | None,
                     attachments: list[str] | None, files: list[str] | None, cont: bool, answer: bool, channel: str | None,
                     source: dict[str, Any] | None, run_end: Any = None, steer: str | None = None, loads_since: float | None = None) -> Run:
    """agent 任务的一段：沙箱 + 网关 + pi，收尾、投递（run_task 调；run_end：状态定了、投递之前跑 run_end hook，可能把 complete 改判 failed）。"""
    if not resume:
        if prompt is not None:
            run.info["prompt"] = prompt[:4000]
        if steer is not None:
            run.info["steer"] = steer[:4000]      # 这条消息本身（listen.py 给的 steer；prompt 里还拼了以前的对话）：网页聊天、stb chat 显示用户说了什么用它
        if ref:
            run.info["ref"] = ref
        if attachments:
            run.info["attachments"] = attachments
        if channel:
            run.info["channel"] = channel
        if source:
            run.info["source"] = source
        files = stage_files(task, run.dir, files or [])
        if files:
            run.info["files"] = files
    ref = ref or run.info.get("ref")
    attachments = attachments or run.info.get("attachments") or []
    work_dir = WORK / task.name                                # 工作目录（沙箱启动时所在的目录，里外同一个路径）：平台统一 <workspace>/<任务>，跨运行保留
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "out").mkdir(exist_ok=True)
    models = load_models()
    profile = profiles(models)[task.profile_name(models)]
    net_args, bind_ip = sandbox.network_for()
    name = f"stb-{task.name}-{run.run_id}"
    gw = Gateway(task, run.dir, work_dir, profile, bind_ip)
    gw.channel = run.info.get("channel")                                                  # 来源渠道（恢复的运行沿用开跑时的）；没有 = 不是渠道消息触发的
    gw.ref = ref                                                                          # 消息标记：有它才报运行中的状态（activity）
    gw.elapsed_before = float(run.info.get("elapsed_seconds", 0)) - float(run.info.get("timeout_credit", 0))   # 续跑：上一份额度用掉的不算

    async def _switch(m: str, on_loading: Any) -> str | None:
        return await ensure_model(m, load_models(), on_loading)

    gw.switch_model = _switch                                                             # D32：子代理换档位，和任务开跑用同一个 ensure_model
    gw.on_fatal = ctl.force_stop                                                          # 切换失败：停沙箱，任务判失败
    gw.switch_seconds = float(run.info.get("model_switch_seconds", 0))                   # 恢复的运行：之前各段的加载时间也不计入超时
    gw.sub_seconds = float(run.info.get("subagent_seconds", 0))                         # 恢复的运行：之前各段不算主模型时间的部分（子代理调用、就地等卡）
    ctl.gateway = gw
    ctl.run_dir, ctl.run_started = run.dir, run.started
    if ctl.owns_state:
        _write_state({"task": task.name, "run_id": run.run_id, "started": run.started, "priority": task.priority, "preemptible": task.preemptible})
    progress = await hub_client.call("progress_start", task=task.name, started=run.started, ref=ref)
    gw.progress_id = (progress or {}).get("id")
    status, error, timed_out, rc = "failed", "", False, None
    usage = {"input": 0, "output": 0, "cached": 0}     # 主模型（pi）这一段的 token 用量：每条回答的 usage 加起来（cached：命中前缀缓存的输入）
    session_file: Path | None = None
    session_dir: Path | None = None
    session_before: dict[Path, float] = {}
    try:
        await gw.start()
        if not (model_id := call_name(profile)):               # 调用名：端点 /models 里 root 等于档位 model_path 的那一条的 id
            raise RuntimeError(f"模型端点 {profile.get('endpoint')} 的 /models 里没有 {profile.get('model_path')}（root 对不上，或端点不答）")
        models_json = run.dir / "models.json"
        models_json.write_text(json.dumps(pi_models_json(profile, gw.model_url, model_id, task.effort)))
        ctl.container = name
        if resume:
            session_file = load_checkpoint(run, gw)
            await gw.restore_loaded()
            session_dir = session_file.parent if session_file else (run.dir / "agent" / "session")
            if proceed := [r for r in run.answered if (r.get("executed") or {}).get("proceed")]:   # pre_call hook 在 Joy 同意时放行了（如 builder 建好开发区）：先替模型调用这一次
                done = {}
                for r in proceed:
                    try:
                        got = await gw.call(r["call"]["function"], r["call"]["asked"], origin="resume")
                    except GatewayError as e:
                        got = e.message
                    done[r["card"]] = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
                run.reply = acquire_permission.reply_prompt(run.answered, done)
            run_prompt = CONTINUE_PROMPT.format(timeout=task.timeout) if cont else run.reply or RESUME_PROMPT
            attachments = []                     # 恢复时图片已在会话里
        else:
            session_dir, session_file = run.dir / "agent" / "session", None      # 每次运行一个新会话（私信任务的上文由它的 listen.py 拼进 prompt）
            run_prompt = build_prompt(task, prompt, files=files)
            if loads_since is not None and (carried := await gw.carry(await asyncio.to_thread(earlier_loads, task.name, loads_since, run.dir))):
                run_prompt = f"{carried}\n\n{run_prompt}"     # 私信任务：上下文起点之后加载过的接着有效，放在用户消息最前面
        run.info["session"] = session_file.name if session_file else None
        run.save()
        session_before = {f: f.stat().st_mtime for f in _session_files(session_dir)} if session_dir.exists() else {}
        gw.session_dir, gw.session_file, gw.session_before = session_dir, session_file, session_before   # 网关按会话认哪些工具的用法规范已给过
        pi_out = run.dir / "agent" / "pi-output.jsonl"

        async def launch(prompt_: str, sfile: Path | None, atts: list[str], seconds: int, extra: Any, cname: str) -> tuple[int | None, bool]:
            ctl.container = cname
            cmd = sandbox.docker_command(task, name=cname, run_dir=run.dir, work_dir=work_dir, models_json=models_json,
                                         gateway_url=gw.url, token=gw.token, model=f"{PI_PROVIDER}/{model_id}", net_args=net_args,
                                         prompt=prompt_, session_dir=session_dir, session_file=sfile,
                                         attachments=atts)
            return await sandbox.run_container(cmd, cname, seconds, pi_out, run.dir / "agent.stderr.log", extra=extra,
                                               stdin=run.dir / "agent" / sandbox.PROMPT_FILE)

        rc, timed_out = await launch(run_prompt, session_file, attachments, run.remaining_seconds, gw.extra_seconds, name)  # pyright: ignore[reportArgumentType]
        text, usage, ended = sandbox.final_text(pi_out)
        if (rc == 0 and ended and not timed_out and not gw.fatal and not ctl.stop_reason and ctl.pause_requested_at is None
                and gw.ask_yielded is None and gw.agent_messages == 0 and not run.info.get("nudged") and sandbox.last_empty(pi_out)):
            # 最后一条回复只有 thinking（没文字、没调工具），pi 当成结束了：同一会话接一条「继续」，一次运行最多一次
            run.info["nudged"] = True
            pi_out.rename(pi_out.with_name("pi-output.empty.jsonl"))       # 停下的那一段事件流留着看
            sfile = _session_used(session_dir, session_file, session_before)
            rc, timed_out = await launch(EMPTY_PROMPT if sfile is not None else f"{run_prompt}\n\n{EMPTY_PROMPT}", sfile or session_file,
                                         [] if sfile is not None else attachments, int(run.remaining_seconds - (time.time() - run.started)),  # pyright: ignore[reportArgumentType]
                                         gw.extra_seconds, f"{name}-nudge")
            t2, u2, ended = sandbox.final_text(pi_out)
            text, usage = t2 or text, {k: usage[k] + u2[k] for k in usage}
        if (task.tool(todo.TOOL) is not None and rc == 0 and ended and not timed_out and not gw.fatal and not ctl.stop_reason
                and ctl.pause_requested_at is None and gw.ask_yielded is None and not run.info.get("todo_nudged")
                and (nudge := todo.nudge_prompt(task.name))):
            # 列了 todo 的任务正常结束时还有没清掉的待办：同一会话补一轮提醒，一次运行最多一次（仍没清掉的附在结束通知里）
            run.info["todo_nudged"] = True
            pi_out.rename(pi_out.with_name("pi-output.todo.jsonl"))         # 提醒前那一段事件流留着看
            sfile = _session_used(session_dir, session_file, session_before)
            rc, timed_out = await launch(nudge if sfile is not None else f"{run_prompt}\n\n{nudge}", sfile or session_file,
                                         [] if sfile is not None else attachments, int(run.remaining_seconds - (time.time() - run.started)),  # pyright: ignore[reportArgumentType]
                                         gw.extra_seconds, f"{name}-todo")
            t2, u2, ended = sandbox.final_text(pi_out)
            text, usage = t2 or text, {k: usage[k] + u2[k] for k in usage}
        wrapped = timed_out and not gw.fatal and not ctl.stop_reason
        if wrapped:          # D34：主模型时间用完不直接判失败：同一会话再给一轮收尾（网关拒绝一切工具调用），运行记为 partial
            if pi_out.exists():
                pi_out.rename(pi_out.with_name("pi-output.main.jsonl"))    # 主段的事件流留着看
            gw.wrapup, gw.last_activity, ctl.run_started = True, time.time(), time.time()
            # pi 在第一轮回答写完之前不落会话文件：主段一轮都没写完就到点时，收尾轮要把原任务一起带上，不然模型不知道在收什么尾
            sfile = _session_used(session_dir, session_file, session_before)
            wrap_prompt = WRAPUP_PROMPT if sfile is not None else f"{run_prompt}\n\n{WRAPUP_PROMPT}"
            rc, _ = await launch(wrap_prompt, sfile or session_file, [] if sfile is not None else attachments, WRAPUP_S,  # pyright: ignore[reportArgumentType]
                                 lambda: 0, f"{name}-wrapup")
            t2, u2, _ = sandbox.final_text(pi_out)
            text, usage = t2 or text, {k: usage[k] + u2[k] for k in usage}
        prev = run.info.get("tokens") or {}
        run.info.update(exit_code=rc, tokens={k: int(prev.get(k, 0)) + usage[k] for k in usage}, final_text=text[:4000])
        run.final_text = text                # 完整回答：收尾通知用它；run.json 里只存展示用的前 4000 字
        if gw.fatal:                              # D32：子代理切换模型失败 = 任务失败
            status, error = "failed", gw.fatal
        elif ctl.stop_reason:
            status, error = "failed", ctl.stop_reason + ("（请再发一次）" if from_channel(run.info) else f"；要重跑：stb run {task.name}")
        elif wrapped:
            status, error = "partial", f"主模型时间 {task.timeout} 用完，已按现有材料收尾"
        elif timed_out:
            status, error = "timeout", f"主模型时间 {task.timeout} 用完，没能收尾，沙箱已销毁"
        elif ctl.stalled_seconds is not None:
            status, error = "failed", f"停滞 {int(ctl.stalled_seconds)} 秒没有任何模型输出、也没有在途的工具调用，判为卡住，沙箱已销毁"
        elif ctl.pause_reason == "stop" and (rc == PAUSE_EXIT_CODE or ctl.killed or (rc == 0 and not ended)):   # /stop：停下，不续跑
            status, error = "stopped", "Joy 停下了这次运行（/stop 或 stb stop）"
        elif gw.ask_yielded is not None and rc in (PAUSE_EXIT_CODE, 0):     # D35：问题没等到回答，已让出：挂起等 Joy
            status = "waiting"
        elif rc == 0 and ended and (model_err := sandbox.last_error(pi_out)):   # 模型报错（如上下文超长、压缩后重试也失败）：pi 正常退出也算失败
            status, error = "failed", f"模型报错：{model_err[:300]}"
        elif rc == 0 and ended:
            status = "complete"
        elif ctl.pause_requested_at is not None and (rc == PAUSE_EXIT_CODE or ctl.killed):
            status = "paused"
        else:
            status, error = "failed", f"agent 退出码 {rc}" + ("" if ended else "，未正常结束") + log_tail(run.dir / "agent.stderr.log")
    except GatewayError as e:
        status, error = "failed", f"网关启动失败 {e.code}: {e.message}"
    except Exception as e:  # noqa: BLE001 — 任何意外都要落盘并投递
        status, error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
    used = gw.meter.total()                            # 子代理那边的模型调用（网关按每次调用返回的 usage 记，tokmeter.py）
    prev_all = run.info.get("tokens_all") or {}     # 早先的运行没有 cached：按 0 接着加
    run.info["tokens_all"] = {k: int(prev_all.get(k, 0)) + usage[k] + used[k] for k in used}
    gw.abort()          # agent 已退出：在途的子代理工具调用没人收结果，取消掉、放开网关锁，下面的通知不用等生成跑完
    if gw.injected:     # D36：运行中认领的插话条目：进了会话的记下，没进的放回队列
        settle_injected(run, gw, _latest_session(session_dir) if session_dir and session_dir.exists() else None)
    if gw.loaded:
        run.info["loaded"] = list(gw.loaded)            # 本次运行经 load-tool 加载过的（run_end hook 的主人、私信任务跨私信接着用，手册 §4.10 第 5 步）
    if run_end is not None:
        status, error = await run_end(status, error)
    ended_at = time.time()
    seg = run.info["segments"][-1]
    seg.update(ended=ended_at, seconds=round(ended_at - run.started, 1), status=status, memory_peak=run.mem.result())
    run.info["elapsed_seconds"] = round(float(run.info.get("elapsed_seconds", 0)) + (ended_at - run.started), 1)
    if gw.switch_seconds:
        run.info["model_switch_seconds"] = round(gw.switch_seconds, 1)
    if gw.sub_seconds:
        run.info["subagent_seconds"] = round(gw.sub_seconds, 1)
    if status in ("paused", "partial", "waiting"):   # 续跑（D34）、答后续跑（D35）同恢复：从检查点（会话文件 + 网关状态）接着
        session_now = _latest_session(session_dir) if session_dir and session_dir.exists() else session_file
        save_checkpoint(run, gw, session_now)
    if status == "paused":
        run.info["preempt_count"] = int(run.info.get("preempt_count", 0)) + (1 if ctl.pause_reason == "preempt" else 0)
        run.info["pause_reason"] = ctl.pause_reason
        run.info["forced"] = ctl.killed
        run.info["paused_at"] = ended_at
        error = f"被{'抢占' if ctl.pause_reason == 'preempt' else '手动'}暂停" + ("（宽限到期强停，恢复自上一检查点）" if ctl.killed else "（在工具边界暂停）")
    if status == "waiting":
        y = gw.ask_yielded or {}
        run.info.update(waiting_reason="waiting_for_answer", waiting_since=ended_at, ask_card=y.get("card"), ask_round=y.get("round"))
        error = (f"等 Joy 处理审批卡「{y.get('title')}」（卡片 {y.get('card')}）；不自动恢复" if y.get("title") else
                 f"等 Joy 回答第 {y.get('round')} 轮问题（{y.get('questions')} 题，卡片 {y.get('card')}）；不自动恢复")
    elif ids := acquire_permission.obsolete_pending(run.dir, f"运行已结束（{status}），这张卡作废"):
        for cid in ids:
            await hub_client.call("card_refresh", id=cid)
    for cid in acquire_permission.expire_password_cards(f"{task.name}/{run.run_id}"):   # 发卡的调用都结束了：还没填的密码卡作废
        await hub_client.call("card_refresh", id=cid)
    out_src = work_dir / "out"                     # 产出留在工作目录（平台外），运行记录只记 out/ 里的文件名（网页运行详情显示）
    run.info["out_files"] = sorted(str(f.relative_to(out_src)) for f in out_src.rglob("*") if f.is_file()) if out_src.is_dir() else []
    run.info.update(status=status, error=error, calls=gw.calls, rejections=gw.rejections, agent_messages=gw.agent_messages)
    # 投递（规格第 12 节三种通知：成功 / 失败 / 被拒）；暂停、挂起等回答不投递
    if status == "stopped":                          # /stop：Joy 自己叫停的，不投递
        run.info["notified"] = True
    elif status not in ("paused", "waiting"):
        try:
            await deliver(task, gw, run, status, error)
            run.info["notified"] = True            # 启动时清理遗留运行靠它判断要不要补发（D18）
        except Exception as e:  # noqa: BLE001
            run.info["deliver_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    if gw.bugs and status not in ("paused", "waiting"):    # 工具崩了但 agent 绕过去收尾了：结果照发，另外单独提醒，不被淹没
        run.info["tool_bugs"] = gw.bugs
        await hub_client.call("send", text=f"⚠ {task.name}：这次运行里 {'、'.join(f'{n}（{gw.bug_notes[n]}）' if gw.bug_notes.get(n) else n for n in gw.bugs)} "
                                                f"出了意外异常（程序错误），结果可能不全；traceback 见 {run.dir}/gateway.stderr.log")
    await gw.stop()
    if progress and progress.get("id"):
        await hub_client.call("progress_end", id=progress["id"], status=status)
    for e in run.info.get("injected") or []:          # 插进来的消息跟着这次运行的结果换标记
        if e.get("delivered") and e.get("ref"):
            await hub_client.call("progress_end", id="", status=status, ref=e["ref"])
    run.info.update(ended=datetime.fromtimestamp(ended_at, timezone.utc).isoformat(), seconds=run.info["elapsed_seconds"],
                    memory_peak={k: max((s.get("memory_peak") or {}).get(k) or 0 for s in run.info["segments"]) or None for k in ("mem_used_gb", "swap_used_gb")})
    run.save()
    if ctl.owns_state:
        _write_state(None)
    return run


def _user_texts(sfile: Path) -> str:
    """会话文件里全部用户消息的文字。"""
    out = []
    for line in sfile.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message") or {}
        if e.get("type") == "message" and m.get("role") == "user":
            c = m.get("content")
            out.append(c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict)))
    return "\n".join(out)


def settle_injected(run: Run, gw: Gateway, sfile: Path | None) -> None:
    """D36：网关认领的插话条目进没进会话（pi 的 steer 在这一轮工具跑完、下一次模型调用前才送）：进了记 delivered；
    没进（pi 在那之前就停了：暂停、收尾、问题卡让出、被杀）就把队列条目原样放回，照常开下一次运行——不丢、不重复。"""
    seen = _user_texts(sfile) if sfile is not None and sfile.exists() else ""
    rec = run.info.setdefault("injected", [])
    for e in gw.injected:
        ok = bool(e.get("delivered_text")) and str(e["delivered_text"]) in seen
        if not ok:
            src = run.dir / "injected" / str(e.get("queue_file"))
            e.pop("delivered_text", None)
            e.pop("queue_file", None)
            _atomic_write(QUEUE / src.name, json.dumps(e, ensure_ascii=False))
            src.unlink(missing_ok=True)
        rec.append({"queue_file": src.name if not ok else e.get("queue_file"), "ref": e.get("ref"), "delivered": ok,  # pyright: ignore[reportPossiblyUnboundVariable]
                    "chars": len(str(e.get("prompt") or ""))})
    gw.injected = []


def preempt_grace(task: Task) -> int:
    """这个任务被要求暂停后等检查点的上限（秒）：workflow 任务 PREEMPT_GRACE_COMMAND_S，agent 任务 PREEMPT_GRACE_AGENT_S。"""
    return PREEMPT_GRACE_COMMAND_S if task.workflow else PREEMPT_GRACE_AGENT_S


async def _wait_group_gone(pgid: int, ctl: RunControl, grace: float) -> None:
    """步骤的主进程已退出，组里的子进程可能还在收尾（如它起的训练进程还在存检查点）。
    等整组退完再返回，免得下一个任务在它还占着 GPU 时开跑；宽限（外加 10 秒）到期还在就 KILL。"""
    t0 = ctl.pause_requested_at or time.time()
    while True:
        if not await asyncio.to_thread(sandbox.group_alive, pgid, ctl.as_user):
            return
        if time.time() - t0 > grace + 10:
            sandbox.kill_group(pgid, 9, ctl.as_user)
            ctl.killed = True
        await asyncio.sleep(0.2)


async def run_workflow(task: Task, run: Run, ctl: RunControl, run_end: Any = None) -> Run:
    """workflow 任务的一段：跑 run.json 里 step 指的那一步（任务文件夹里的 py），不起沙箱、网关、agent。
    一律以 sparky 跑（launch.command_argv，文件看 sparky 的文件权限，联网按任务 yaml 的 network），工作目录是任务文件夹；用任务的依赖环境（有 requirements.txt）或平台的 python；
    py 里能 import control_center、用 credential("KEY") 读凭证。环境：STB_RUN_DIR（运行目录里给步骤的可写目录 steps/）、STB_TASK、STB_STEP、STB_REPLY_FILE，重跑被暂停的那一步加 STB_RESUME=1，
    写了模型的步骤加 STB_MODEL（档位配置，同 generator 子代理）。输出追加进运行目录的 step-<编号>.log。
    退出码：0 → 还有下一步就记 stepping（下一步由调度方交接：runner._settle / workflow.reap → workflow.route），最后一步 → complete；
    3（NOTHING_EXIT_CODE）→ complete、不发消息（请求过暂停也算）；请求过暂停后 75 或被信号杀掉 → paused（/stop 的 → stopped）；其他 → failed。
    状态定了（stepping 除外：步骤之间不算这一段结束）、通知之前跑 run_end hook。complete 发 reply.txt 的文字（没写发「✅ 完成」），失败发错误摘要。"""
    from control_center import launch
    from control_center.gateway import profile_env
    from control_center.registry import PLATFORM_ENV, REQUIREMENTS_FILE, TEST_ENV_PREFIX, venv_dir

    n = int(run.info.get("step") or 1)
    if n > len(task.step_list):                   # 运行到一半任务定义改短了
        run.info["step"] = len(task.step_list)
        status, error = await run_end("failed", f"任务定义改了：运行要接着跑第 {n} 步，现在只有 {len(task.step_list)} 步") if run_end else ("failed", "")
        run.info.update(status=status, error=error, notified=True, ended=datetime.now(timezone.utc).isoformat())
        run.info["segments"][-1].update(ended=time.time(), seconds=0.0, status=status)
        run.save()
        await hub_client.call("send", task=task.name, text=f"❌ {task.name} {status}: {hub_client.escape(error)}\n运行目录: {run.dir}")
        return run
    step = task.step_list[n - 1]
    ctl.command = True
    ctl.run_dir, ctl.run_started = run.dir, run.started
    if ctl.owns_state and step.resource:
        _write_state({"task": task.name, "run_id": run.run_id, "started": run.started, "priority": task.priority, "preemptible": task.preemptible})
    progress = await hub_client.call("progress_start", task=task.name, started=run.started)
    log = run.dir / f"step-{n}.log"
    sdir = run.dir / STEPS_DIR                    # 步骤可写的目录（步骤之间交接、reply.txt）
    launch.grant(sdir)
    status, error, rc = "failed", "", None
    own = venv_dir(task.name, "tasks") if task._dir is not None and (task._dir / REQUIREMENTS_FILE).is_file() else None   # 任务文件夹有 requirements.txt：用它的依赖环境
    python = str(own / "bin" / "python") if own else sys.executable
    envs = {"PATH": ":".join([*([str(own / "bin")] if own else []), str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
            "LANG": "C.UTF-8", "TMPDIR": "/tmp", "STB_RUN_DIR": str(sdir), "STB_TASK": task.name, "STB_STEP": str(n),
            "STB_REPLY_FILE": str(sdir / REPLY_FILE), **({"STB_RESUME": "1"} if run.rerun else {}),
            **{k: v for k, v in os.environ.items() if k in PLATFORM_ENV or k.startswith(TEST_ENV_PREFIX)}}
    try:
        if own is not None and not Path(python).exists():
            raise RuntimeError(f"任务文件夹有 {REQUIREMENTS_FILE}，它的依赖环境 {own} 还没建（builder 部署时建）")
        if step.model is not None:
            models = load_models()
            prof = str(task.step_profile(models, n))
            envs.update(profile_env(models[prof], prof))
        script = str(task._dir / step.file)  # pyright: ignore[reportOptionalOperand]
        with open(log, "ab") as out:
            out.write(f"===== 第 {n} 步 {step.file}{'（重跑）' if run.rerun else ''} {datetime.now().astimezone().isoformat(timespec='seconds')} =====\n".encode())
            out.flush()
            plain = os.environ.get("STB_TEST_LAUNCH_PLAIN")   # 测试钩子同 launch.tool_command：临时目录里 sparky 进不去，照旧以当前用户跑
            ctl.as_user = None if plain else launch.SPARKY
            command = f"exec {shlex.quote(python)} {shlex.quote(script)}"
            argv = ["bash", "-c", command] if plain else launch.command_argv(task, command, envs)
            proc = await asyncio.create_subprocess_exec(*argv, cwd=str(task._dir), stdout=out, stderr=asyncio.subprocess.STDOUT,
                                                        env={**envs, "HOME": str(Path.home())}, start_new_session=True)
            ctl.proc = proc
            if ctl.pause_requested_at is not None:          # 起进程前就被要求暂停：补发 SIGTERM
                sandbox.kill_group(proc.pid, 15, ctl.as_user)
            try:
                rc = await asyncio.wait_for(proc.wait(), max(1, run.remaining_seconds))
            except asyncio.TimeoutError:
                ctl.force_stop()
                rc = await proc.wait()
                status, error = "timeout", f"超过 timeout {task.timeout}（各步骤在跑的时间合计），第 {n} 步已终止"
            if ctl.pause_requested_at is not None or ctl.killed:
                await _wait_group_gone(proc.pid, ctl, PREEMPT_GRACE_COMMAND_S)
        run.info["exit_code"] = rc
        if status != "timeout":
            if ctl.stop_reason:
                status, error = "failed", ctl.stop_reason + f"；要重跑：stb run {task.name}"
            elif ctl.pause_requested_at is not None and ctl.pause_reason == "stop":
                status, error = "stopped", "Joy 停下了这次运行（/stop 或 stb stop）"
            elif rc == 0:
                status = "stepping" if n < len(task.step_list) else "complete"
            elif rc == NOTHING_EXIT_CODE:                   # 没事可做（请求暂停时也一样：如算力 API 的会话被抢占时已经闲着，不用续）
                status, run.info["nothing_to_do"] = "complete", n
            elif ctl.pause_requested_at is not None and rc == PAUSE_EXIT_CODE:    # 存好进度
                status = "paused"
                run.info.pop("checkpoint_note", None)
            elif ctl.pause_requested_at is not None and (ctl.killed or rc in SIGNAL_EXITS):
                status = "paused"                           # 被信号杀掉：没确认存下进度，恢复时这一步重跑
                run.info["checkpoint_note"] = "本次暂停时进程被信号杀掉，没有确认存下进度；恢复时这一步重跑"
            elif rc == PAUSE_EXIT_CODE and ctl.pause_requested_at is None:
                status, error = "failed", f"第 {n} 步自行以 75 退出，但平台没有请求暂停" + log_tail(log)
            else:
                status, error = "failed", f"第 {n} 步（{step.file}）退出码 {rc}" + log_tail(log)
    except Exception as e:  # noqa: BLE001 — 任何意外都要落盘并投递
        status, error = "failed", f"第 {n} 步：{type(e).__name__}: {str(e)[:300]}"
    if status == "stepping":
        run.info["step"] = n + 1
    elif run_end is not None:                               # 状态定了、通知之前：run_end hook（可能把 complete 改判 failed）
        status, error = await run_end(status, error)
    ended_at = time.time()
    run.info["segments"][-1].update(ended=ended_at, seconds=round(ended_at - run.started, 1), status=status, step=n,
                                    memory_peak=run.mem.result())
    run.info["elapsed_seconds"] = round(float(run.info.get("elapsed_seconds", 0)) + (ended_at - run.started), 1)
    if status == "paused":
        run.info["preempt_count"] = int(run.info.get("preempt_count", 0)) + (1 if ctl.pause_reason == "preempt" else 0)
        run.info.update(pause_reason=ctl.pause_reason, forced=ctl.killed, paused_at=ended_at)
        error = (f"第 {n} 步被{'抢占' if ctl.pause_reason == 'preempt' else '手动'}暂停，退出码 {rc}"
                 + ("（进程被信号杀掉，没确认存下进度，恢复时这一步重跑）" if "checkpoint_note" in run.info else ""))
    run.info.update(status=status, error=error, final_text=f"第 {n} 步{'完成' if status in ('complete', 'stepping') else '未完成'}，日志 {log}")
    if status in ("complete", "failed", "timeout") and not run.info.get("nothing_to_do"):   # 运行结束：成功发 reply.txt 的文字，失败发摘要（暂停、步骤之间、/stop、没事可做不发）
        try:
            body = (sdir / REPLY_FILE).read_text().strip() if (sdir / REPLY_FILE).is_file() else ""
            text = ((body or f"✅ {task.name} 完成，用时 {run.info['elapsed_seconds'] / 60:.0f} 分钟") if status == "complete"
                    else f"❌ {task.name} · 第 {n} 步 {status}: {hub_client.escape(error)}\n运行目录: {run.dir}")
            run.info["delivered"] = sent_entries(await hub_client.call("send", task=task.name, text=text))
        except Exception as e:  # noqa: BLE001
            run.info["deliver_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    if status not in ("paused", "stepping"):
        run.info["notified"] = True
    if progress and progress.get("id"):
        await hub_client.call("progress_end", id=progress["id"], status="complete" if status == "stepping" else status)
    run.info.update(ended=datetime.fromtimestamp(ended_at, timezone.utc).isoformat(), seconds=run.info["elapsed_seconds"],
                    memory_peak=run.info["segments"][-1].get("memory_peak"))
    run.save()
    if ctl.owns_state and step.resource:
        _write_state(None)
    return run


def from_channel(info: dict[str, Any]) -> bool:
    """由渠道消息触发的运行：run.json 有来源渠道或消息标记。最终回答不加前缀，中断了请 Joy 再发一次。"""
    return bool(info.get("channel") or info.get("ref"))


def notice_label(task: Task, info: dict[str, Any]) -> str:
    """通知前缀「✅ / 🟡 <它>」写谁：别的事件、一次性监听回调触发的运行写来源名（如 download），定时、手动、监听检查（和渠道消息）写任务名。"""
    event = (info.get("source") or {}).get("event")
    return str(event) if event and not from_channel(info) else task.name


def sent_entries(r: dict[str, Any] | None) -> list[str]:
    """stbcc-hub 的 send 回复里实际收到的入口（run.json 的 delivered）；中枢不在、一个入口都没收到是空列表。"""
    return [str(n) for n in (r or {}).get("entries") or []]


async def reply_entries(info: dict[str, Any]) -> list[str]:
    """经 reply 工具发的通知收到的入口：工具只交回结果文字，按中枢 reply 的规则算（来源渠道开着就是它，否则是开着的各入口）。"""
    got = sent_entries(await hub_client.call("status"))
    return [info["channel"]] if info.get("channel") in got else got


async def deliver(task: Task, gw: Gateway, run: Run, status: str, error: str) -> None:
    """运行结束一律通知 Joy（平台规则，任务 yaml 不写）：任务勾了 reply（reply.TOOL）走它（网关审计）；没勾就经 stbcc-hub 私信 owner（系统通知）。
    成功：agent 这次自己发过消息（gw.agent_messages > 0）就不补发，没发过就发最终回答：渠道消息触发的原样发，别的加「✅ <notice_label>」
    （最终回答也是空的就发 DONE_NOTE「✅ <notice_label> 完成」；补过一次「继续」还是空的发 EMPTY_NOTE「⚠ <notice_label> 无回答」）；
    失败 / 超时发错误摘要；部分完成发收尾回答 + 续跑卡（deliver_partial）。"""
    ok = status == "complete"
    if status == "partial":
        run.info["delivered"] = await deliver_partial(task, gw, run)
        return
    body = getattr(run, "final_text", "") or str(run.info.get("final_text") or "")   # 发全文，reply 按 2000 字分块
    label = notice_label(task, run.info)
    done = (f"✅ {label}\n{body}" if body.strip() else EMPTY_NOTE.format(task=label, dir=run.dir) if run.info.get("nudged")
            else DONE_NOTE.format(task=label))
    todos = todo.end_note(task.name) if ok and task.tool(todo.TOOL) is not None else ""     # 列了 todo：还没清掉的待办附在后面
    if task.tool(reply.TOOL) is None:        # 没有 reply：agent 发不了消息，成功时一律经 stbcc-hub 发最终回答
        note = f"\n⚠ 有 {gw.rejections} 次白名单外的工具/参数请求被拒（看 {run.dir}/gateway.jsonl）" if gw.rejections else ""
        run.info["delivered"] = sent_entries(await hub_client.call("send", task=task.name, text=(done + (f"\n\n{todos}" if todos else "") if ok
                                                             else f"❌ {task.name} {status}: {hub_client.escape(error)}\n运行目录: {run.dir}") + note))
        return
    note = f"\n⚠ 有 {gw.rejections} 次白名单外的工具/参数请求被拒（看 {run.dir}/gateway.jsonl，可能要改任务定义或 prompt）" if gw.rejections else ""
    if ok:
        if gw.agent_messages == 0:        # agent 这次没自己发过消息才补发最终回答（发过就不再补发）
            final = body if from_channel(run.info) and body.strip() else done
            await gw.call(reply.TOOL, {"text": final + (f"\n\n{todos}" if todos else "")}, origin="system")
        elif todos:                       # 发过消息：待办单独发一条
            await gw.call(reply.TOOL, {"text": todos}, origin="system")
        if note:                          # 被拒提示是系统通知：经 stbcc-hub 发给各入口，不分渠道，不进第一层记忆（D16）
            await hub_client.call("send", text=note.strip())
    else:
        # 错误摘要可能含 agent 或工具输出：转义后再贴（11.7）
        await gw.call(reply.TOOL, {"text": f"❌ {task.name} {status}: {hub_client.escape(error)}\n运行目录: {run.dir}{note}"}, origin="system")
    run.info["delivered"] = await reply_entries(run.info)


async def deliver_partial(task: Task, gw: Gateway, run: Run) -> list[str]:
    """D34：主模型时间用完、收尾过的运行：先发收尾的回答（渠道消息触发的原样发，别的加「🟡 <notice_label>（部分完成）」），
    再发带「续跑」按钮的卡（只认 owner 点，stbcc-hub 处理，不经模型）。返回收到回答的入口（run.json 的 delivered）。"""
    body = getattr(run, "final_text", "") or str(run.info.get("final_text") or "（agent 没有输出）")
    text = body if from_channel(run.info) else f"🟡 {notice_label(task, run.info)}（部分完成）\n{body}"
    if task.tool(reply.TOOL) is not None:
        await gw.call(reply.TOOL, {"text": text}, origin="system")
        got = await reply_entries(run.info)
    else:
        got = sent_entries(await hub_client.call("send", task=task.name, text=text))
    lines = [f"主模型时间 {task.timeout} 用完，已按现有材料收尾，没做完的见上一条。",
             f"点「续跑」接着做：同一会话和工作目录，再给 {task.timeout} 主模型时间。",
             f"运行目录: {run.dir}"]
    await hub_client.call("card", kind=CONTINUE_KIND, title=f"🟡 {task.name} 部分完成", body="\n".join(lines), ttl_seconds=None,
                              buttons=[{"id": "continue", "label": "续跑", "style": "primary"}], payload={"task": task.name, "run_id": run.run_id})
    return got


def _handle_continue_card(card: dict[str, Any], _action: str, _value: Any) -> str:
    """「续跑」按钮（在 stbcc-hub 进程里跑）：运行还是 partial 就入队续跑；不自动续跑，只有 Joy 点了才入队。"""
    p = card.get("payload") or {}
    name, run_id = str(p.get("task", "")), str(p.get("run_id", ""))
    try:
        info = json.loads((RUNS / name / run_id / "run.json").read_text())
        task = load_task(task_file(name))
    except Exception:  # noqa: BLE001
        return "找不到这次运行或任务定义，没法续跑"
    if info.get("status") != "partial":
        return f"这次运行现在是 {info.get('status')}，不能续跑"
    trigger = norm_trigger(info.get("trigger"))
    enqueue(name, trigger="schedule" if trigger == "catchup" else trigger, resume=run_id, cont=True)
    return f"已入队续跑：同一会话，再给 {task.timeout} 主模型时间"


cards.register(CONTINUE_KIND, _handle_continue_card)


def _handle_ask_card(card: dict[str, Any], action: str, value: Any) -> str:
    """问题卡的「提交」/「不回答」（D35，在 stbcc-hub 进程里跑）：记下答案。网关还在就地等 → 它自己取走当调用结果；
    已让出 → 按任务优先级入队续跑（答案作为下一条用户消息）。运行已结束就不送。"""
    p = card.get("payload") or {}
    name, run_id = str(p.get("task", "")), str(p.get("run_id", ""))
    d = RUNS / name / run_id
    try:
        info = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        return "找不到这次运行，回答没有送出"
    if info.get("status") not in ("running", "waiting"):
        return f"这次运行已经结束（{info.get('status')}），回答没有送出"
    r = acquire_permission.answer(d, card["id"], action, value)
    if r is None:
        return "这组问题已经作废，回答没有送出"
    what = "不回答" if action == "decline" else "回答"
    if not r.get("yielded"):
        return f"已收到{what}，{name} 接着做"
    trigger = norm_trigger(info.get("trigger"))
    enqueue(name, trigger="schedule" if trigger == "catchup" else trigger, resume=run_id, answer=True)
    return f"已收到{what}，{name} 已入队接着做"


cards.register(acquire_permission.KIND, _handle_ask_card)


_ANSWERED_LOCK = threading.Lock()     # 几张卡的执行同时做完（stbcc-hub 的几个后台线程）：查队列与入队一次只一个


def _enqueue_answered(name: str, run_id: str, info: dict[str, Any]) -> None:
    """挂起的运行有了要送达的结果：按任务优先级入队续跑（队列里已有它的续跑条目就不重复入队：几张卡都在那次续跑里送达）。"""
    with _ANSWERED_LOCK:
        if any(e.get("task") == name and e.get("resume") == run_id for _p, e in _queue_entries()):
            return
        trigger = norm_trigger(info.get("trigger"))
        enqueue(name, trigger="schedule" if trigger == "catchup" else trigger, resume=run_id, answer=True)


def _execute_approved(d: Path, r: dict[str, Any]) -> None:
    """让出后 Joy 同意了记着调用的卡：当场替那次调用执行一次（gateway.execute_approved），结果存进这一轮，运行还挂着就入队续跑。在 stbcc-hub 的后台线程里跑
    （工具执行时要经 hub.sock 用掉这张卡，中枢的事件循环不能被占着）。"""
    from control_center.gateway import execute_approved

    name, run_id = d.parent.name, d.name
    if not acquire_permission.claim_execution(d, r["card"]):    # 已在执行或执行过（启动补执行与卡片处理撞上）：不重复执行
        return
    c, cid = r["call"], secrets.token_hex(6)

    def audit(rec: dict[str, Any]) -> None:
        """这次执行记进运行的 gateway.jsonl（origin approved，开始与结束各一条，按 call_id 对上）：主模型的 toollog 里没有，网页按它显示。
        pre_call hook 发的卡（builder 提案）不记：放行后续跑时由运行器调用，网关照常记。"""
        if c["kind"] != "hook":
            with open(d / "gateway.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.time(), **rec, "origin": "approved", "tool": c["function"], "call_id": cid, "card": r["card"]},
                                   ensure_ascii=False) + "\n")

    async def go() -> None:
        t0 = time.monotonic()
        audit({"ev": "call_start", "args": Gateway._audit_args(c["args"])})
        try:
            out = await execute_approved(f"{name}/{run_id}", r)
        except Exception as e:  # noqa: BLE001 — 起不来、断了都如实交给模型
            out = {"ok": False, "text": f"执行失败：{type(e).__name__}: {str(e)[:300]}"}
        audit({"ev": "call", "ok": bool(out.get("ok", True)), "seconds": round(time.monotonic() - t0, 3), "result": str(out.get("text") or "")[:3000]})
        acquire_permission.set_executed(d, r["card"], out)
        if acquire_permission.note_executed(r["card"], out):
            await hub_client.call("card_refresh", id=r["card"])

    asyncio.run(go())
    _enqueue_if_waiting(d)


def _enqueue_if_waiting(d: Path) -> None:
    """平台执行完（或记成不确定）：运行还挂着就入队续跑。"""
    name, run_id = d.parent.name, d.name
    try:
        info = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        return
    if info.get("status") == "waiting":
        _enqueue_answered(name, run_id, info)


def recover_approved() -> int:
    """stbcc-hub 启动时：挂起的运行里 Joy 已同意、平台还没执行完的调用补上。没开始执行的现在执行（后台线程，执行完入队续跑）；
    执行到一半中枢就退出了的不再执行（不知道做没做成），结果记成「不确定」、卡上写明、入队续跑。卡已处理、这一轮还没记上的（处理器跑到一半退出）先记上。
    返回补了几个。"""
    n = 0
    for rj in sorted(RUNS.glob("*/*/run.json")):
        try:
            if json.loads(rj.read_text()).get("status") != "waiting":
                continue
        except (OSError, ValueError):
            continue
        d = rj.parent
        todo, cut = acquire_permission.unfinished(d)        # 先取：下面补记的轮次由 _on_run_card 自己起执行，不在这两张单子里
        if (d / "ask.json").exists():
            for r in json.loads((d / "ask.json").read_text()).get("rounds", []):
                c = cards.load(str(r.get("card") or "")) if r.get("status") == "pending" else None
                if c and c.get("resolved") and c["kind"] != acquire_permission.KIND:
                    _on_run_card(c, c["resolved"]["action"], c["resolved"].get("value"), str(c["resolved"].get("result", "")))
                    n += 1
        for r in cut:
            acquire_permission.set_executed(d, r["card"], acquire_permission.INTERRUPTED)
            if acquire_permission.note_executed(r["card"], acquire_permission.INTERRUPTED):
                asyncio.run(hub_client.call("card_refresh", id=r["card"]))
            _enqueue_if_waiting(d)
            n += 1
        for r in todo:
            threading.Thread(target=_execute_approved, args=(d, r), daemon=True).start()
            n += 1
    if n:
        print(f"启动时补上 {n} 个已同意、还没执行完的调用", flush=True)
    return n


def _on_run_card(card: dict[str, Any], action: str, _value: Any, result: str) -> None:
    """C3：运行中的工具发的审批卡被 Joy 处理了（在 stbcc-hub 进程里跑）：记成这一轮的答案；网关已经让出（运行 waiting）就按任务优先级
    入队续跑，处理结果作为下一条用户消息送进同一会话。网关还在就地等的，它自己取走。问题卡走 _handle_ask_card，不在这里。
    记着调用的卡（acquire_permission.record_card 的 call）让出后同意了：先在后台替那次调用执行（_execute_approved），执行完再入队。"""
    ref = str((card.get("payload") or {}).get("run") or "")
    if card.get("kind") == acquire_permission.KIND or "/" not in ref:
        return
    name, run_id = ref.split("/", 1)
    d = RUNS / name / run_id
    if not (d / "ask.json").exists():
        return
    r = acquire_permission.answer(d, card["id"], "submit", {"action": action, "result": result})
    try:
        info = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        return
    if r is not None and r.get("yielded") and info.get("status") in ("running", "waiting"):
        if acquire_permission.approved_call(r):
            threading.Thread(target=_execute_approved, args=(d, r), daemon=True).start()
            return
        _enqueue_answered(name, run_id, info)


cards.ON_RESOLVED.append(_on_run_card)


# ---------- 模型切换（规格第 8 节、D32）----------
CMD_TIMEOUT_S = 300         # 启停命令的上限（如 vllm-eugr 单元的 TimeoutStartSec=180）：卡住的命令不能让主循环一直停着
UNLOAD_WAIT_S = 180         # 卸载后等容器消失、MemAvailable 回到 min_avail_gb 的上限；等不到判失败、不加载
UNLOAD_POLL_S = 5
START_POLL_S = 5            # 起命令之后等健康的轮询间隔
DOCKER_TIMEOUT_S = 30       # 单次 docker inspect / ps 的上限：docker 守护进程卡住时不让加载一直等


def _run_cmd(cmd: str) -> tuple[int, str]:
    """跑启停命令，返回 (退出码, 说明)；说明是退出码与输出末尾（stderr 优先），给启停失败的报错用。
    命令写错（引号不配对、空命令）、找不到或没权限执行、超时都返回 -1（说明写原因），按启停失败处理。"""
    import shlex

    try:
        argv = shlex.split(cmd)
        argv[0] = os.path.expanduser(argv[0])
    except (ValueError, IndexError) as e:
        return -1, f"命令写错（{type(e).__name__}: {e}）：{cmd!r}"
    try:
        p = subprocess.run(argv, capture_output=True, timeout=CMD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return -1, f"{CMD_TIMEOUT_S} 秒没结束：{cmd}"
    except OSError as e:
        return -1, f"执行不了（{type(e).__name__}: {e}）：{cmd}"
    tail = (p.stderr or p.stdout or b"").decode(errors="replace").strip()[-500:]
    return p.returncode, f"退出码 {p.returncode}" + (f"，输出末尾：{tail}" if tail else "，没有输出")


def _container_gone(name: str) -> bool:
    try:
        return subprocess.run(["docker", "inspect", name], capture_output=True, timeout=DOCKER_TIMEOUT_S).returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return False                                   # 查不到就当还在：等到 UNLOAD_WAIT_S 判失败


def _mem_avail_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 ** 2
    return 0.0


def _unregistered_containers(models: dict[str, Any]) -> list[str]:
    """在跑的容器里不属于在跑的已登记档位的：容器名没登记，或容器名对上、模型不对（D32）。"""
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ["（docker ps 没有响应）"]
    profs = profiles(models)
    known = {str(profs[n].get("container")) for n in running(models)}
    return [n for n in r.stdout.split() if n not in known]


HOLDER_MIN_GB = 2           # 内存闸 / 加载失败时列出的宿主进程：常驻内存至少这么多
HOLDER_TOP = 5


def _memory_holders() -> str:
    """占内存最多的宿主进程（常驻内存 ≥ HOLDER_MIN_GB，最多 HOLDER_TOP 个；容器里的进程也在内），写成"pid 名字 GiB"给 Joy 看是谁占着。"""
    try:
        r = subprocess.run(["ps", "-eo", "pid=,rss=,comm=", "--sort=-rss"], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return "（ps 没有响应）"
    out = []
    for line in r.stdout.splitlines()[:HOLDER_TOP]:
        pid, rss, comm = (line.split(None, 2) + ["", "", ""])[:3]
        if rss.isdigit() and int(rss) / 1024 ** 2 >= HOLDER_MIN_GB:
            out.append(f"pid {pid} {comm.strip()} {int(rss) / 1024 ** 2:.0f} GiB")
    return "、".join(out) or "无"


def _switchable(models: dict[str, Any]) -> list[str]:
    """换档位时要停的在跑档位：running 去掉常驻接口（不起不停）。"""
    profs = profiles(models)
    return [n for n in running(models) if not resident(profs[n])]


FREE_MACHINE = "空出机器"   # workflow 要资源、没写模型的步骤：不起模型，停掉所有已登记档位、等内存够了（_settings 的 min_avail_gb）再跑


async def ensure_model(task_model: str | None, models: dict[str, Any], on_loading: Callable[[], Awaitable[None]] | None = None,
                       retry: bool = False) -> str | None:
    """_ensure_model 加总时长上限：等加载中的档位认出来 START_TIMEOUT_S + 卸载等待上限 UNLOAD_WAIT_S + 起命令上限 CMD_TIMEOUT_S + 等就绪上限 START_TIMEOUT_S。
    超过判失败（返回原因）。retry（运行器开跑前用）：失败后再清一次场（在跑的已登记档位全停，含加载到一半的目标自己），重新加载一次；还不行返回两次的原因。"""
    limit = UNLOAD_WAIT_S + CMD_TIMEOUT_S + 2 * START_TIMEOUT_S

    async def once(on: Callable[[], Awaitable[None]] | None, fresh: bool) -> str | None:
        try:
            return await asyncio.wait_for(_ensure_model(task_model, models, on, fresh), limit)
        except asyncio.TimeoutError:
            return f"加载 {task_model or FREE_MACHINE} 超过总时长上限 {limit:.0f} 秒"

    why = await once(on_loading, False)
    if why is None or not retry:
        return why
    print(f"加载 {task_model or FREE_MACHINE} 失败：{why}；清场后再试一次", flush=True)
    again = await once(None, True)                    # 状态栏、私信的 ⏳ 第一次已经换过
    if again is None:
        return None
    return f"{again}（清场后重试一次，原因相同）" if again == why else f"清场后重试：{again}；第一次：{why}"


async def _identify(models: dict[str, Any], on_loading: Callable[[], Awaitable[None]]) -> str | None:
    """登记了容器的档位在加载中（容器在跑、端点还不答 /models，models.loading）：认不出是哪个档位，先等它能答 /models 再认（最多 START_TIMEOUT_S），
    等的期间算加载（on_loading：状态栏、私信的 ⏳）。等不到返回原因。"""
    deadline = time.time() + START_TIMEOUT_S
    while pending := await asyncio.to_thread(loading, models):
        if time.time() >= deadline:
            return f"容器 {'、'.join(pending)} 在跑，但端点 {START_TIMEOUT_S} 秒内没答 /models，认不出在跑哪个模型，不切换；请先手动停掉"
        await on_loading()
        await asyncio.sleep(START_POLL_S)
    return None


async def _stop_profiles(names: list[str], profs: dict[str, Any], deadline: float, what: str) -> str | None:
    """依次停掉这些档位（stop 命令），登记了容器的等它消失（到 deadline 为止）；和前面停过的档位同一个容器的跳过（随前一个一起停掉了）。失败返回原因。"""
    done: set[str] = set()
    for n in names:
        cont = profs[n].get("container")
        if cont and str(cont) in done:
            continue
        if cont:
            done.add(str(cont))
        print(f"{what}：停 {n}", flush=True)
        rc, out = await asyncio.to_thread(_run_cmd, str(profs[n]["stop"]))
        if rc != 0:
            return f"停 {n} 失败（{out}）"
        while cont and not await asyncio.to_thread(_container_gone, str(cont)):
            if time.time() >= deadline:
                return f"停了 {n}，但容器 {cont} {UNLOAD_WAIT_S} 秒内没消失"
            await asyncio.sleep(UNLOAD_POLL_S)
    return None


async def _ensure_model(task_model: str | None, models: dict[str, Any], on_loading: Callable[[], Awaitable[None]] | None = None,
                        fresh: bool = False) -> str | None:
    """让 task_model 的档位就绪（D32）。已就绪返回 None；否则调 on_loading（只调一次），
    先等加载中、认不出是哪个档位的容器能答 /models（_identify：如开机自启正在加载的模型；等到的就是目标就直接用，不打断它的加载），
    再停掉在跑的全部其他已登记档位、等它们的容器消失，再等 MemAvailable ≥ min_avail_gb（档位自己的，没写用 _settings 的；共 UNLOAD_WAIT_S），之后才 start、等健康。
    未登记的容器与进程不停；它占着内存让内存闸过不了、或占着目标档位的容器名（容器在跑、端点答的模型不是任何登记档位的）就判失败（原因里列出占着的容器与进程）。
    fresh（失败后的重试）时不等加载中的：和别的档位一样先停掉，过内存闸后重新 start。
    task_model 是 None（workflow 要资源、没写模型的步骤）：同独占档位，只空出机器（内存闸用 _settings 的 min_avail_gb）。
    失败返回原因。在 Run 建立之前完成，加载时间不计入任务超时。"""
    profs = profiles(models)
    shown = False

    async def loading_once() -> None:
        nonlocal shown
        if on_loading is not None and not shown:
            shown = True
            await on_loading()
    if task_model is None:
        return await _ensure_exclusive(FREE_MACHINE, {}, models, loading_once, fresh)
    prof = profs.get(task_model)
    if prof is None:
        return f"模型档位 {task_model} 未登记"
    if exclusive(prof):
        return await _ensure_exclusive(task_model, prof, models, loading_once, fresh)
    if await asyncio.to_thread(is_up, prof):
        return None
    settings = models.get("_settings") or {}
    if resident(prof):
        return f"常驻接口 {task_model}（没写 start / stop，平台不起它）的 /models 没应答，或里面没有 {prof.get('model_path')}"
    if not prof.get("start"):
        return f"模型 {task_model} 没有 start 命令"
    if not fresh and (why := await _identify(models, loading_once)):
        return why
    if not fresh and await asyncio.to_thread(is_up, prof):    # 等到的就是它（加载完了）
        return None
    live = await asyncio.to_thread(_switchable, models)
    cont = str(prof.get("container") or "")
    if cont and cont not in {str(profs[n].get("container")) for n in live} and await asyncio.to_thread(_container_running, cont):
        return (f"容器 {cont} 在跑的不是 {task_model} 的模型（未登记的模型），不停它、不加载 {task_model}；"
                f"要用 {task_model} 请先手动停掉 {cont}")
    for n in live:
        if not profs[n].get("stop"):
            return f"要停掉 {n} 才能起 {task_model}，但它没有 stop 命令"
    await loading_once()
    deadline = time.time() + UNLOAD_WAIT_S
    if why := await _stop_profiles(live, profs, deadline, "模型切换"):
        return f"{why}，不加载 {task_model}"
    need = float(prof.get("min_avail_gb", settings.get("min_avail_gb", 0)))   # 档位自己写了（实际占用 + 余量）就按它，没写用全局
    while (avail := await asyncio.to_thread(_mem_avail_gb)) < need:
        if not live or time.time() >= deadline:
            held = "、".join(await asyncio.to_thread(_unregistered_containers, models)) or "无"
            return (f"可用内存 {avail:.0f} GiB < 加载前要求的 {need:.0f} GiB（min_avail_gb），不加载 {task_model}；"
                    f"未登记的容器与进程不会被自动停（在跑的未登记容器：{held}；未登记的宿主进程也算，"
                    f"占内存最多的进程：{await asyncio.to_thread(_memory_holders)}），请先手动停掉")
        await asyncio.sleep(UNLOAD_POLL_S)
    print(f"模型切换：起 {task_model}", flush=True)
    rc, out = await asyncio.to_thread(_run_cmd, str(prof["start"]))
    if rc != 0:
        return f"起 {task_model} 失败（{out}）"
    deadline = time.time() + START_TIMEOUT_S
    seen, missing = False, 0
    while time.time() < deadline:
        if await asyncio.to_thread(is_up, prof):
            return None
        if cont:                                       # 容器露过面又连续两次不在：加载中途退出了（底座的清理会删容器），不再等满 START_TIMEOUT_S
            if await asyncio.to_thread(_container_running, cont):
                seen, missing = True, 0
            elif seen:
                missing += 1
                if missing >= 2:
                    return f"{task_model} 加载中容器 {cont} 退出了，不再等"
        await asyncio.sleep(START_POLL_S)
    return f"{task_model} 在 {START_TIMEOUT_S} 秒内未就绪"


async def _ensure_exclusive(task_model: str, prof: dict[str, Any], models: dict[str, Any],
                            on_loading: Callable[[], Awaitable[None]], fresh: bool = False) -> str | None:
    """独占档位（没有 endpoint，如训练）：没有服务可起，只要求机器空出来——先等加载中、认不出是哪个档位的容器能答 /models（fresh 时不等），
    再停掉在跑的全部已登记档位、等容器消失、MemAvailable ≥ 档位的 min_avail_gb（没写用 _settings 的）。跑完不恢复，下一个要模型的任务开跑时自己加载。"""
    settings = models.get("_settings") or {}
    need = float(prof.get("min_avail_gb", settings.get("min_avail_gb", 0)))
    if not fresh and (why := await _identify(models, on_loading)):
        return why
    live = await asyncio.to_thread(_switchable, models)
    if not live and await asyncio.to_thread(_mem_avail_gb) >= need:
        return None
    profs = profiles(models)
    for n in live:
        if not profs[n].get("stop"):
            return f"要停掉 {n} 才能独占，但它没有 stop 命令"
    await on_loading()
    deadline = time.time() + UNLOAD_WAIT_S
    if why := await _stop_profiles(live, profs, deadline, f"独占 {task_model}"):
        return why
    while (avail := await asyncio.to_thread(_mem_avail_gb)) < need:
        if time.time() >= deadline:
            held = "、".join(await asyncio.to_thread(_unregistered_containers, models)) or "无"
            return (f"可用内存 {avail:.0f} GiB < {task_model} 要求的 {need:.0f} GiB；未登记的容器与进程不会被自动停"
                    f"（在跑的未登记容器：{held}；未登记的宿主进程也算，占内存最多的进程：{await asyncio.to_thread(_memory_holders)}），请先手动停掉")
        await asyncio.sleep(UNLOAD_POLL_S)
    return None


def _started_since(task_name: str, since: float) -> bool:
    """since 之后开始过的运行（任何状态）：这个时刻它已经跑过了。"""
    for f in (RUNS / task_name).glob("*/run.json"):
        try:
            if float(json.loads(f.read_text()).get("first_started") or 0) >= since:
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------- 补跑（Joy 2026-09-22）----------
# 定时任务每个原定时刻之后必须成功跑过一次；没有（模型没起来被丢、断电、运行失败）就补跑一次，补跑也失败就私信 Joy。
CATCHUP_CHECK_S = 300
PRESENCE_REFRESH_S = 60    # 状态栏文字没变也每分钟重发一次：聊天入口（如 stbcc-discord）重启上线可能把状态栏重置成"空闲"
PRESENCE_MIN_GAP_S = 5     # 运行中状态文字一变就发，但两次之间至少隔这么久（Discord 限流约 20 秒 5 次）
CATCHUP_GRACE_S = 120      # 原定时刻过后先等 2 分钟：中枢入队、runner 取走都需要时间，别和正常那次撞上


def _catchup_state_path() -> Path:
    return RUNNER_STATE.parent / "catchup.json"        # 每个任务已经补跑过的原定时刻；调用时现算，测试会换掉 RUNNER_STATE


def _catchup_state() -> dict[str, float]:
    try:
        return json.loads(_catchup_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def mark_catchup(task_name: str, slot: float) -> None:
    st = _catchup_state()
    st[task_name] = slot
    _atomic_write(_catchup_state_path(), json.dumps(st))


def _completed_since(task_name: str, since: float) -> bool:
    for f in (RUNS / task_name).glob("*/run.json"):
        try:
            info = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        started = info.get("first_started")
        if started is None and info.get("started"):
            started = datetime.fromisoformat(info["started"]).timestamp()
        if info.get("status") in ("complete", "partial", "waiting") and started is not None and float(started) >= since:   # partial / waiting 由 Joy 决定，不补跑
            return True
    return False


def load_registered() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(REGISTERED.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def mark_registered(task_name: str, first: float | None = None) -> None:
    """登记表只记首次登记时刻（重新 stb task add 不改，否则老任务会被当成刚登记的、漏跑补不上）；启停看任务 yaml 的 enabled。"""
    reg = load_registered()
    reg.setdefault(task_name, {"first": first if first is not None else time.time()})
    _atomic_write(REGISTERED, json.dumps(reg, ensure_ascii=False, indent=1))


def catchup_due(now: float, running: str | None) -> list[tuple[str, float]]:
    """该补跑的 (任务名, 原定时刻)。原定时刻取任务 schedule 在 now 之前最近的一次；跳过：刚到点不满 2 分钟、
    这个时刻已经补跑过、正在跑或已在队列里（含加载失败留着等 Joy 的）、之后已有成功运行、没登记或 yaml 的 enabled 是 false、原定时刻早于首次登记（刚加的任务不补之前的）。
    模型没起来也照常补跑：开跑时自动切换（D32）。"""
    state = _catchup_state()
    reg = load_registered()
    queued = {e.get("task") for _, e in [*_queue_entries(), *held_entries()]}
    due = []
    for f in task_files():
        try:
            task = load_task(f)
        except Exception:  # noqa: BLE001
            continue
        if "schedule" not in task.triggers:     # 只补跑 trigger: schedule 的任务
            continue
        slot = last_occurrence(task.schedule, now)  # pyright: ignore[reportArgumentType]
        r = reg.get(task.name)
        if r is None or not task.enabled:
            continue
        if slot is None or now - slot < CATCHUP_GRACE_S or slot < float(r.get("first", now)):
            continue
        if float(state.get(task.name, 0)) >= slot or task.name == running or task.name in queued or task_pending(task.name):
            continue
        if not _completed_since(task.name, slot):
            due.append((task.name, slot))
    return due


def _slot_text(entry: dict[str, Any]) -> str:
    slot = entry.get("catchup_slot")
    return time.strftime("%m-%d %H:%M", time.localtime(float(slot))) if slot else "未知时刻"


# ---------- 主循环 ----------
class Current:
    def __init__(self, task: Task, entry: dict[str, Any], ctl: RunControl, fut: asyncio.Task[Run]) -> None:
        self.task, self.entry, self.ctl, self.fut = task, entry, ctl, fut
        self.t0 = time.time()


def _would_be_preempted(entry: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    """队列里有比 entry 优先级更高的条目、而 entry 的任务可被抢占（开跑就会被 tick 抢占）：返回那个更高的条目。"""
    best = pick()
    if best is None or int(best[1].get("priority", 5)) <= int(entry.get("priority", 5)):
        return None
    try:
        preemptible = load_task(task_file(entry["task"])).preemptible
    except Exception:  # noqa: BLE001 — 坏条目交给 _start 去报错并丢弃
        return None
    return best if preemptible else None


def _manual_pause_requested(task_name: str) -> bool:
    p = CONTROL / f"pause-{task_name}"
    if p.exists():
        p.unlink(missing_ok=True)
        return True
    return False


def _stop_requested(cur: Current) -> bool:
    """停止标记（任务的 listen.py 经 actions 请平台停本任务，listen.run_actions 写）：标记写在这次运行开始之后才算，早于它的（上一次运行结束前没来得及取走）作废。"""
    p = CONTROL / f"stop-{cur.task.name}"
    if not p.exists():
        return False
    try:
        ts = float(p.read_text())
    except (OSError, ValueError):
        ts = 0.0
    p.unlink(missing_ok=True)
    return ts >= cur.t0


MODEL_CHECK_S = 30


def model_available() -> bool:
    """模型算在线：任务开跑时按需加载（D32），models.yaml 读得了就算在线；读不了时状态栏"已离线"、不补跑。"""
    try:
        load_models()
    except Exception:  # noqa: BLE001
        return False
    return True


def presence_text(current: Current | None, model_ok: bool = True) -> str:
    """状态栏："正在：A（优先级）；插队：E（优先级）；打断：B（优先级），C（优先级）；续跑：F（优先级）；排队：D（优先级）"。
    插队 = 还没开始跑、优先级比正在跑的高、而正在跑的能被抢占（在等它到边界停下，比如优先级 10 的消息打断定时任务）；
    打断 = 队列里等着恢复的被抢占 / 暂停的条目；续跑 = 答完卡片（answer）或主模型时间用完再给一份（continue）排队接着跑的条目；排队 = 其余还没开始跑的条目（同级或更低优先级，比如连发的两条消息）。都按优先级从高到低。
    models.yaml 读不了时显示"已离线"（Joy 2026-09-21）。"""
    from control_center import monitors, workflow

    watching = monitors.watching_names()
    tail = ("；监听中：" + "、".join(watching)) if watching else ""      # 一次性动态监听是 stbcc-hub 监督的常驻监听，和运行器上的任务一起显示
    if side := workflow.side_units():                                    # stbcc-hub 在跑的 workflow 步骤（不占运行器）
        tail = "；旁路：" + "、".join(f"{u['task']}·第 {u['step']} 步" for u in side) + tail
    if not model_ok:
        return "已离线"
    if current is None:
        return ("空闲" + tail)[:128]
    if loading := getattr(current.ctl.gateway, "loading", None):
        return f"加载模型：{current.task.name}（{loading}）"[:128]   # D32：子代理换模型期间，写明是哪个任务
    p0 = current.entry.get("priority", current.task.priority)
    label = lambda e, name: f"{name}·{e['watch']}" if e.get("watch") else name  # noqa: E731 — 监听触发的运行带上监听名
    text = f"正在：{label(current.entry, current.task.name)}（{p0}）"
    entries = sorted((e for _, e in _queue_entries()), key=lambda e: (-int(e.get("priority", 5)), float(e.get("enqueued_at", 0))))
    fmt = lambda es: "，".join(f"{label(e, e['task'])}（{e.get('priority', 5)}）" for e in es)  # noqa: E731
    cur_p = int(current.entry.get("priority", current.task.priority))
    jumps = lambda e: current.task.preemptible and int(e.get("priority", 5)) > cur_p  # noqa: E731
    paused = [e for e in entries if e.get("resume") and not (e.get("answer") or e.get("continue"))]
    resumed = [e for e in entries if e.get("resume") and (e.get("answer") or e.get("continue"))]
    jumping = [e for e in entries if not e.get("resume") and jumps(e)]
    queued = [e for e in entries if not e.get("resume") and not jumps(e)]
    if jumping:
        text += "；插队：" + fmt(jumping)
    if paused:
        text += "；打断：" + fmt(paused)
    if resumed:
        text += "；续跑：" + fmt(resumed)
    if queued:
        text += "；排队：" + fmt(queued)
    return (text + tail)[:128]


_DEF_ERRORS: dict[str, str] = {}   # 任务 → 上次私信过的定义校验错误（同一原因不重复私信）
STOP_REASON = "runner stopped：运行器收到停止信号（stbcc-runner 被停止或重启、关机），这次运行中断了"
STOP_WAIT_S = 15            # SIGTERM 后等当前运行收尾的上限；systemd 单元 TimeoutStopSec=20
NOTICE_RETRY_S = 30
LOADING_SUFFIX = ".loading"   # 认领标记：条目从队列取出到运行真正开始（含加载模型）期间改名成 <条目>.json.loading；运行器这期间被停就会留下，下次启动放回队列
HELD_SUFFIX = ".held"         # 开跑前加载模型清场重试后仍失败：条目改名成 <条目>.json.held 留着等 Joy（不取、不自动重试），卡上「重试」或 stb resume 放回
HELD_KIND = "load-held"       # 加载失败、条目留着的卡（「重试」按钮）


def recover_stale_runs() -> list[Path]:
    """启动时清理遗留（D18）：status 还是 running 的运行是上一个运行器没来得及收尾的（断电、SIGKILL）。
    杀掉可能还在跑的容器，run.json 标 failed。返回还没发出失败通知的运行目录（由 notify_stale 发）。"""
    pending = []
    for rj in sorted(RUNS.glob("*/*/run.json")):
        try:
            info = json.loads(rj.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if info.get("status") != "running":
            continue
        name, run_id = str(info.get("task") or rj.parent.parent.name), rj.parent.name
        if unit_alive(unit_name(name, run_id, len(info.get("segments") or []))):     # 运行单元（最新一段的）还在跑（运行器重启不打断运行）：不是遗留，由接管它的运行器等它结束
            continue
        if fail_stale_run(rj, info, f"{STOP_REASON}（上一个运行器没来得及收尾：断电或被强杀）"):
            pending.append(rj.parent)
    return pending


def fail_stale_run(rj: Path, info: dict[str, Any], why: str) -> bool:
    """status 还是 running、运行进程却已不在的运行：杀掉可能还在跑的容器，run.json 标 failed。返回是否还要补发失败通知。"""
    name, run_id = str(info.get("task") or rj.parent.parent.name), rj.parent.name
    subprocess.run(["docker", "kill", f"stb-{name}-{run_id}"], capture_output=True)
    stale_run_end(rj.parent, name, run_id)     # 运行进程没收尾：这一段的 run_end hook 在这里补跑（如取消交给别的服务、不在本单元 cgroup 里的研究容器）
    acquire_permission.obsolete_pending(rj.parent, "运行器重启时这次运行没收尾，这组问题作废")   # 挂起（waiting）的运行不在这里：它们不是 running
    info.update(status="failed", error=why + ("（请再发一次）" if from_channel(info) else ""),
                ended=datetime.now(timezone.utc).isoformat())
    try:
        load_task(task_file(name))
    except Exception:  # noqa: BLE001 — 任务已删掉也要把运行标失败（不再补发通知）
        info["notified"] = True
    _atomic_write(rj, json.dumps(info, ensure_ascii=False, indent=1))
    print(f"遗留运行 {name}/{run_id} 标为 failed", flush=True)
    return not info.get("notified")


def stale_run_end(run_dir: Path, name: str, run_id: str) -> None:
    """运行进程没收尾就没了（被硬杀、崩溃、断电）的这一段：照样跑 run_end hook（status failed），任何结束方式都触发 run_end。
    hook 的主人按任务现在的定义算（任务已删、定义读不了就跳过）；hook 自己出错只记日志、私信 Joy（hooks.fire）。"""
    try:
        task = load_task(task_file(name))
    except Exception:  # noqa: BLE001
        return
    base = {"task": name, "run_id": run_id, "tool": None, "subagent": None, "function": None, "args": None, "status": "failed",
            "error": "运行进程没收尾就没了"}
    try:
        loaded = json.loads((run_dir / "run.json").read_text()).get("loaded") or []
    except (OSError, ValueError, AttributeError):
        loaded = []
    hooks.fire("run_end", hooks.run_owners(task, loaded), base, run_dir, WORK / name)


def restore_claimed(keep: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """运行器启动时：上一个运行器取出（认领）了、还没开跑就被停的条目（<条目>.json.loading）原样放回队列（优先级、入队时刻、resume 等都不变）。
    keep：本运行器接管的运行单元的条目（_adopt）——和它同一任务、同一入队时刻、同一 resume 的标记是已经开跑了的，只删标记不放回。
    加载失败留着等 Joy 的 .json.held 不动。返回放回的条目。"""
    back = []
    for m in sorted(QUEUE.glob("*.json" + LOADING_SUFFIX)):
        try:
            entry = json.loads(m.read_text())
        except (OSError, ValueError):
            continue                                       # 读不了：留着给人看，不猜
        if keep and all(entry.get(k) == keep.get(k) for k in ("task", "enqueued_at", "resume")):
            m.unlink(missing_ok=True)
            continue
        requeue(entry)
        m.unlink(missing_ok=True)
        back.append(entry)
        print(f"放回队列：{entry.get('task')}（上一个运行器取出后没开跑就停了）", flush=True)
    return back


def held_entries() -> list[tuple[Path, dict[str, Any]]]:
    """加载失败、留着等 Joy 的条目（<条目>.json.held）。"""
    out = []
    for p in sorted(QUEUE.glob("*.json" + HELD_SUFFIX)):
        try:
            out.append((p, {k: v for k, v in json.loads(p.read_text()).items() if k != "held_reason"}))
        except (OSError, ValueError, AttributeError):
            continue
    return out


async def hold_entry(marker: Path, entry: dict[str, Any], task: Task, prof: str | None, why: str, models: dict[str, Any]) -> Path:
    """开跑前加载模型清场重试后仍失败：条目不丢、不判运行失败——改名成 .json.held 留着（暂停中的运行仍是 paused），
    私信一张卡：原因、占内存最多的进程、日志在哪，「重试」把条目原样放回队列（_handle_held_card）。返回留着的文件。"""
    held = marker.with_name(marker.name.removesuffix(LOADING_SUFFIX) + HELD_SUFFIX)
    _atomic_write(held, json.dumps({**entry, "held_reason": why}, ensure_ascii=False))   # held_reason：原因原文（stb run --wait 打印它），放回队列时去掉
    marker.unlink(missing_ok=True)
    start = (profiles(models).get(prof) or {}).get("start") if prof else None
    what = f"补跑（原定 {_slot_text(entry)} 那次）" if entry.get("trigger") == "catchup" else ""
    lines = [f"原因：{why}",
             "已清场（停掉在跑的已登记档位）重试过一次。这次排队的条目留着，不自动重试"
             + (f"；暂停中的运行 {entry['resume']} 仍是 paused" if entry.get("resume") else "") + "。",
             f"占内存最多的进程：{await asyncio.to_thread(_memory_holders)}",
             "日志：运行器 journalctl --user -u stbcc-runner" + (f"；档位的起命令 {start}（模型自己的日志看它写到哪）" if start else ""),
             "处理好之后点「重试」放回队列" + (f"，或 stb resume {task.name}" if entry.get("resume") else "") + "。"]
    await hub_client.call("card", kind=HELD_KIND, title=f"⏸ {task.name} {what}没开跑：{prof or FREE_MACHINE} 失败", body="\n".join(lines), ttl_seconds=None,
                          buttons=[{"id": "retry", "label": "重试", "style": "primary"}], payload={"task": task.name, "held": held.name})
    return held


def _handle_held_card(card: dict[str, Any], _action: str, _value: Any) -> str:
    """加载失败卡的「重试」（在 stbcc-hub 进程里跑）：留着的条目原样放回队列（优先级、入队时刻、resume 都不变）；
    已经放回过（重试过、stb resume 过）就不再放。恢复条目指的运行已不在等恢复的状态（被别的途径恢复了）就丢掉留着的条目。"""
    p = card.get("payload") or {}
    held = QUEUE / Path(str(p.get("held", ""))).name
    if not held.name.endswith(HELD_SUFFIX) or not held.exists():
        return "这条已经不在等了（重试过或已恢复）"
    try:
        entry = json.loads(held.read_text())
        entry.pop("held_reason", None)
    except (OSError, ValueError, AttributeError):
        return "留着的条目读不了，没法重试"
    if run_id := entry.get("resume"):
        want = "partial" if entry.get("continue") else "waiting" if entry.get("answer") else "paused"
        status = _read(RUNS / str(entry.get("task")) / str(run_id) / "run.json").get("status")
        if status != want and status != "stepping":
            held.unlink(missing_ok=True)
            return f"运行 {run_id} 现在是 {status}，不用重试了"
    requeue(entry)
    held.unlink(missing_ok=True)
    return f"已放回队列，{entry.get('task')} 等运行器重新加载模型"


cards.register(HELD_KIND, _handle_held_card)


async def notify_stale(run_dir: Path) -> bool:
    """给遗留运行补发失败通知（经 stbcc-hub；它还没起来就返回 False，下次再试）。发成功才记 notified，不重复发。"""
    rj = run_dir / "run.json"
    info = json.loads(rj.read_text())
    if info.get("notified"):
        return True
    if ref := info.get("ref"):                                   # 渠道上那条消息卡着的进行中标记换成失败
        await hub_client.call("progress_end", id="", status="failed", ref=ref)
    r = await hub_client.call("send", task=str(info.get('task')), text=f"❌ {info.get('task')} failed: {hub_client.escape(str(info.get('error', '')))}\n运行目录: {run_dir}")
    if not (r and r.get("ok")):
        return False
    info["notified"] = True
    _atomic_write(rj, json.dumps(info, ensure_ascii=False, indent=1))
    return True


# ---------- 运行单元：每次运行一个 systemd 临时单元 ----------
# 运行器只调度；每段运行由 `systemd-run --user --collect` 起一个临时单元 stb-run-<任务>-<运行 id>（第 2 段起加 -s<段号>），单元里的 `stb run-unit` 进程跑 run_task
# （网关、沙箱、投递都在它下面），进程退出单元就消失。运行器重启不打断运行：runner.json 的 current 记着单元名，新运行器接管、等它结束再收尾出队。
# 两边经文件通信：data/runtime/control/<单元>.json 由运行器写（开跑参数 + 暂停 / 强停 / 让出请求），<单元>.live.json 由运行进程写（网关状态：
# 加载模型、收尾轮、就地等回答、在途调用、最近动静）。抢占、手动暂停、/stop 写好请求后 `systemctl stop` 单元：运行进程收到 SIGTERM 按请求
# 在边界暂停 / 停下（没写请求的 SIGTERM，如关机，按失败收尾）；宽限到期、停滞由运行器写强停请求，运行进程自己杀沙箱后照常收尾。
UNIT_POLL_S = 1.0
UNIT_STOP_SLACK_S = 300     # 单元的 TimeoutStopSec = preempt_grace(任务) + 这么多：运行进程卡死不收尾时 systemd 杀掉整个单元（含子进程）
UNIT_ALIVE = {"active", "activating", "deactivating", "reloading", "refreshing"}
REAP_CHECK_S = 60           # 多久看一次已结束运行的单元里有没有遗留进程
CGROUP_ROOT = Path("/sys/fs/cgroup")


def new_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S")


def unit_name(task_name: str, run_id: str, seg: int = 1) -> str:
    """运行单元名 stb-run-<任务名>-<运行 id>，同一次运行第 2 段起加 -s<段号>（上一段的单元里可能还留着后台进程、单元没消失，同名起不来）；
    systemd 单元名不认的字符换成 _。"""
    return re.sub(r"[^A-Za-z0-9:_.\-]", "_", f"stb-run-{task_name}-{run_id}" + (f"-s{seg}" if seg > 1 else "")) + ".service"


def next_unit(task_name: str, run_id: str) -> str:
    """这次运行下一段的单元名：段号 = run.json 已有的段数 + 1。"""
    return unit_name(task_name, run_id, len(_read(RUNS / task_name / run_id / "run.json").get("segments") or []) + 1)


def reap_leftovers() -> None:
    """已结束的运行还留在它各段单元里的进程（Sparky 起的后台进程，如 nohup … &）按 PID 杀掉。它们多是 sparky 的进程，用户级 systemd
    杀不了（Operation not permitted），单元因此一直不消失。运行没结束（暂停、等回答、步骤之间）不杀：续跑的下一段可能还要用。"""
    r = systemd.systemctl("list-units", "--all", "--plain", "--no-legend", "stb-run-*", check=False)
    for line in r.stdout.splitlines():
        f = line.split(None, 4)
        if len(f) < 5 or f[2] in UNIT_ALIVE or not f[4].startswith("stb run "):
            continue
        name = f[4].removeprefix("stb run ")
        m = re.fullmatch(re.escape(unit_name(name, "")[:-len(".service")]) + r"(.+?)(?:-s\d+)?\.service", f[0])
        if not m or _read(RUNS / name / m.group(1) / "run.json").get("status") in ("running", "paused", "waiting", "stepping"):
            continue
        cg = systemd.systemctl("show", f[0], "-p", "ControlGroup", "--value", check=False).stdout.strip()
        try:
            pids = (CGROUP_ROOT / cg.lstrip("/") / "cgroup.procs").read_text().split() if cg else []
        except OSError:
            continue
        for pid in pids:
            try:
                uid = os.stat(f"/proc/{pid}").st_uid
            except OSError:
                continue
            print(f"清理 {f[0]} 的遗留进程 {pid}", flush=True)
            if uid == os.getuid():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                subprocess.run(["sudo", "-n", "-u", pwd.getpwuid(uid).pw_name, "kill", "-KILL", "--", pid], capture_output=True, timeout=30)


def _unit_file(unit: str) -> Path:
    return CONTROL / f"{unit}.json"


def _live_file(unit: str) -> Path:
    return CONTROL / f"{unit}.live.json"


def _read(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def unit_alive(unit: str) -> bool:
    r = systemd.systemctl("show", unit, "-p", "ActiveState", "--value", check=False)
    if r.returncode != 0:
        return True                      # 查不到状态当还在：宁可多等，不提前收尾
    return r.stdout.strip() in UNIT_ALIVE


def start_unit(unit: str, task: Task) -> None:
    """systemd-run 起运行单元（和 stbcc-runner 一样是用户单元）。KillMode=mixed：stop 只给运行进程发 SIGTERM，由它按请求收尾。"""
    env = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith("STB_") or k == "PATH"]
    argv = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--description=stb run {task.name}",
            "-p", "KillMode=mixed", "-p", f"TimeoutStopSec={preempt_grace(task) + UNIT_STOP_SLACK_S}", "-p", "UMask=0022",   # 平台建的文件组只读（共享可写的目录靠组默认可写）
            "-p", f"WorkingDirectory={REPO}", *env, sys.executable, "-m", "control_center.cli", "run-unit", unit]   # -m + 工作目录 REPO：和运行器跑同一份代码
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"起运行单元 {unit} 失败：{r.stderr.strip()[:300]}")


def stop_unit(unit: str) -> None:
    systemd.systemctl("stop", "--no-block", unit, check=False)


class _LiveGateway:
    """运行器这边看到的网关：属性读运行进程写的 <单元>.live.json；设 ask_yield 写成让出请求。"""

    def __init__(self, ctl: "UnitControl") -> None:
        self._ctl = ctl

    def _get(self, k: str, default: Any = None) -> Any:
        return _read(_live_file(self._ctl.unit)).get(k, default)

    loading = property(lambda self: self._get("loading"))
    wrapup = property(lambda self: self._get("wrapup", False))
    ask_waiting = property(lambda self: self._get("ask_waiting", False))
    inflight = property(lambda self: self._get("inflight", 0))
    last_activity = property(lambda self: float(self._get("last_activity") or 0))

    @property
    def ask_yield(self) -> bool:
        return bool(_read(_unit_file(self._ctl.unit)).get("ask_yield"))

    @ask_yield.setter
    def ask_yield(self, v: bool) -> None:
        if v:
            self._ctl.update(ask_yield=time.time())


class UnitControl(RunControl):
    """运行器这边的把手（接口同 RunControl）：请求写进 <单元>.json，暂停 / 停下 / 强停都再 systemctl stop 单元。状态也记在那个文件里，换了运行器照样接上。"""

    def __init__(self, unit: str, run_dir: Path, started: float) -> None:
        super().__init__()
        self.unit, self.run_dir, self.run_started = unit, run_dir, started
        self.gateway = _LiveGateway(self)  # type: ignore[assignment]
        c = _read(_unit_file(unit))
        self.pause_requested_at, self.pause_reason = c.get("pause_requested_at"), c.get("pause")
        self.killed, self.stalled_seconds = bool(c.get("force")), c.get("stall")

    def update(self, **kw: Any) -> None:
        _atomic_write(_unit_file(self.unit), json.dumps({**_read(_unit_file(self.unit)), **kw}, ensure_ascii=False))

    def request_pause(self, reason: str) -> None:
        if self.pause_requested_at is None:
            self.pause_requested_at, self.pause_reason = time.time(), reason
            self.update(pause=reason, pause_requested_at=self.pause_requested_at)
            stop_unit(self.unit)

    def force_stop(self) -> None:
        if self.killed:
            return
        self.killed = True
        self.update(force=True, stall=self.stalled_seconds)
        stop_unit(self.unit)


class UnitRun:
    """运行单元结束后从 run.json 读回的结果（_finish 用到的几项同 Run）。"""

    def __init__(self, task_name: str, run_id: str, info: dict[str, Any]) -> None:
        self.run_id, self.dir, self.info = run_id, RUNS / task_name / run_id, info


class UnitNotStarted(RuntimeError):
    """运行单元没开跑就退出（run.json 没有这次的新一段）。"""


def _unit_log_tail(unit: str, n: int = 5) -> str:
    r = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(n), "-o", "cat", "--no-pager"], capture_output=True, text=True)
    return " / ".join(x.strip() for x in r.stdout.splitlines() if x.strip())[-500:]


async def wait_unit(unit: str, task_name: str, run_id: str, t0: float) -> UnitRun:
    """等运行单元结束，读回 run.json。运行进程没收尾就没了（被硬杀、崩溃）：同遗留运行一样标 failed 并补发通知。"""
    while await asyncio.to_thread(unit_alive, unit):
        await asyncio.sleep(UNIT_POLL_S)
    rj = RUNS / task_name / run_id / "run.json"
    info = _read(rj)
    segs = info.get("segments") or []
    if not segs or float(segs[-1].get("started") or 0) < t0 - 1:
        tail = await asyncio.to_thread(_unit_log_tail, unit)
        raise UnitNotStarted(f"运行单元 {unit} 没开跑就退出了：{tail or '单元日志为空'}（看 journalctl --user -u {unit}）")
    if info.get("status") == "running":
        if await asyncio.to_thread(fail_stale_run, rj, info, f"运行进程没收尾就退出了（被强杀或崩溃，看 journalctl --user -u {unit}）"):
            await notify_stale(rj.parent)
        info = _read(rj)
    return UnitRun(task_name, run_id, info)


async def exec_unit(unit: str) -> None:
    """运行单元的主进程（`stb run-unit <单元>`）：照 <单元>.json 跑一次 run_task；每 0.5 秒取运行器的请求、写网关状态。"""
    spec = _read(_unit_file(unit))
    entry = spec["entry"]
    models = load_models()
    task = load_task(task_file(entry["task"]), models)
    ctl = RunControl()
    ctl.owns_state = False
    yielded = 0.0

    def apply(sigterm: bool = False) -> None:
        nonlocal yielded
        c = _read(_unit_file(unit))
        if c.get("pause") and ctl.pause_requested_at is None:
            ctl.request_pause(str(c["pause"]))
        if c.get("force") and not ctl.killed:
            if c.get("stall") is not None:
                ctl.stall_stop(float(c["stall"]))
            else:
                ctl.force_stop()
        if float(c.get("ask_yield") or 0) > yielded and ctl.gateway is not None:
            yielded = float(c["ask_yield"])
            ctl.gateway.ask_yield = True
        if sigterm and not c.get("pause") and not c.get("force") and not ctl.stop_reason:   # 不是运行器要停的（关机、手动 systemctl stop）：按失败收尾
            ctl.stop_reason = STOP_REASON
            ctl.force_stop()

    async def watch() -> None:
        last: dict[str, Any] | None = None
        while True:
            apply()
            if (g := ctl.gateway) is not None:
                live = {"loading": g.loading, "wrapup": g.wrapup, "ask_waiting": g.ask_waiting, "inflight": g.inflight,
                        "last_activity": round(g.last_activity)}
                if live != last:
                    _atomic_write(_live_file(unit), json.dumps(live, ensure_ascii=False))
                    last = live
            await asyncio.sleep(0.5)

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: apply(sigterm=True))
    w = asyncio.create_task(watch())
    try:
        await run_task(task, norm_trigger(entry.get("trigger"), "schedule"), prompt=entry.get("prompt"), resume=entry.get("resume"), ctl=ctl,
                       ref=entry.get("ref"), attachments=entry.get("attachments"), files=entry.get("files"),
                       cont=bool(entry.get("continue")), answer=bool(entry.get("answer")), load_seconds=float(spec.get("load_seconds") or 0),
                       channel=entry.get("channel"), source=entry.get("source"), run_id=str(spec["run_id"]), steer=entry.get("steer"), loads_since=entry.get("loads_since"))
    finally:
        w.cancel()


class Runner:
    def __init__(self, units: bool = False) -> None:
        self.units = units                 # True（stbcc-runner）：每次运行起一个 systemd 临时单元；False（测试、run_once）：在本进程里跑
        self.current: Current | None = None
        self._presence_shown: str | None = None
        self._presence_at = 0.0
        self.model_ok = True
        self._model_checked = 0.0
        self._catchup_checked = 0.0
        self._reap_checked = 0.0

    async def _pick(self) -> tuple[Path, dict[str, Any]] | None:
        """严格按顺序取下一个（pick：优先级大的先，同优先级按入队时间；不看模型有没有加载）。"""
        return pick()

    async def _check_model(self) -> None:
        if time.time() - self._model_checked >= MODEL_CHECK_S:
            self._model_checked = time.time()
            ok = await asyncio.to_thread(model_available)
            if ok != self.model_ok:
                print(f"models.yaml {'读得了' if ok else '读不了'}，状态栏 {'恢复' if ok else '改为已离线'}", flush=True)
            self.model_ok = ok

    async def _catchup(self) -> None:
        """每 5 分钟看一次有没有该补跑的定时任务；模型不通时不看（补跑也跑不成，白白用掉唯一一次机会）。"""
        if time.time() - self._catchup_checked < CATCHUP_CHECK_S:
            return
        await self._check_model()
        if not self.model_ok:
            return
        self._catchup_checked = time.time()
        running = self.current.task.name if self.current else None
        for name, slot in await asyncio.to_thread(catchup_due, time.time(), running):
            mark_catchup(name, slot)
            enqueue(name, trigger="catchup", catchup_slot=slot)
            print(f"补跑：{name}（原定 {time.strftime('%m-%d %H:%M', time.localtime(slot))} 之后没有成功运行）", flush=True)

    async def _presence(self) -> None:
        await self._check_model()
        text = presence_text(self.current, self.model_ok)
        if text != self._presence_shown or time.time() - self._presence_at >= PRESENCE_REFRESH_S:
            self._presence_shown, self._presence_at = text, time.time()
            await hub_client.call("presence", text=text)

    async def _start(self, path: Path, entry: dict[str, Any]) -> None:
        marker = path.with_name(path.name + LOADING_SUFFIX)   # 取出条目 = 认领：运行开始前一直留着这个标记（运行器被停时下次启动据此放回队列，restore_claimed）
        try:
            path.replace(marker)
        except OSError:
            pass
        models = load_models()
        try:
            task = load_task(task_file(entry['task']), models)
        except Exception as e:  # noqa: BLE001
            print(f"跳过 {entry.get('task')}：{e}", flush=True)
            marker.unlink(missing_ok=True)
            if ref := entry.get("ref"):                  # 渠道上那条消息的排队标记换成失败
                await hub_client.call("progress_end", id="", status="failed", ref=ref)
            name, why = str(entry.get("task")), f"{type(e).__name__}: {str(e)[:1500]}"
            if _DEF_ERRORS.get(name) != why:              # 同一原因只私信一次（定时 / 监听反复触发时不刷屏）
                _DEF_ERRORS[name] = why
                await hub_client.call("send", task=name, text=f"❌ 任务 {name}：定义校验失败：{why}\n"
                                                                  f"排队的这次（{entry.get('trigger')}）没跑、已丢弃；修好 tasks/{name}/task.yaml 后重新触发（stb run {name}）")
            return
        _DEF_ERRORS.pop(task.name, None)
        lid = ""
        if task.workflow:                                 # workflow：这次要跑的那一步的档位（没写模型 = None：只空出机器）
            n = int(_read(RUNS / task.name / str(entry["resume"]) / "run.json").get("step") or 1) if entry.get("resume") else 1
            prof = task.step_profile(models, n) if n <= len(task.step_list) else None    # 定义改短了：run_workflow 判失败
        else:
            prof = task.profile_name(models)

        async def on_loading() -> None:                  # D32：确认要切换时调：状态栏"加载模型：X"，消息标记换成加载中；加载期间别的条目排队
            nonlocal lid
            self._presence_shown = f"加载模型：{task.name}（{prof or FREE_MACHINE}）"[:128]
            await hub_client.call("presence", text=self._presence_shown)
            if entry.get("ref"):
                lid = str(((await hub_client.call("loading", ref=entry["ref"])) or {}).get("id") or "")

        t_load = time.time()
        why = await ensure_model(prof, models, on_loading, retry=True)   # 失败会清场再试一次
        load_seconds = time.time() - t_load
        if why:                                           # 重试后仍失败（D32，不回退默认档位）：⏳ 换成 ❌；条目留着等 Joy，私信带「重试」的卡
            held = await hold_entry(marker, entry, task, prof, why, models)
            print(f"{task.name} 没开跑，条目留着等 Joy（{held.name}）：{why}", flush=True)
            if lid:
                await hub_client.call("progress_end", id=lid, status="failed")
            elif ref := entry.get("ref"):                # 加载还没开始就失败：排队标记换成失败（渠道没有这条的进度记录，按 ref 找那条消息）
                await hub_client.call("progress_end", id="", status="failed", ref=ref)
            return
        if (higher := _would_be_preempted(entry)) is not None:     # 加载期间来了更高优先级的：这条放回队列（先放回再删认领标记），下一轮先跑高的
            requeue(entry)
            marker.unlink(missing_ok=True)
            print(f"{task.name} 开跑前发现更高优先级的 {higher[1]['task']}（{higher[1].get('priority')}），放回队列先跑它", flush=True)
            return
        print(f"开始 {task.name}（priority {entry.get('priority', task.priority)}，{entry.get('trigger')}{'，恢复 ' + entry['resume'] if entry.get('resume') else ''}）", flush=True)
        if self.units:
            self.current = self._start_unit(task, entry, load_seconds)   # runner.json 记下运行单元：之后运行器重启由 _adopt 接管
            marker.unlink(missing_ok=True)                               # 运行开始了才删认领标记（之后被停由 _adopt / recover_stale_runs 接手）
            return
        ctl = RunControl()
        fut = asyncio.create_task(run_task(task, norm_trigger(entry.get("trigger"), "schedule"), prompt=entry.get("prompt"), resume=entry.get("resume"), ctl=ctl,
                                           ref=entry.get("ref"), attachments=entry.get("attachments"), files=entry.get("files"),
                                           cont=bool(entry.get("continue")), answer=bool(entry.get("answer")), load_seconds=load_seconds,
                                           channel=entry.get("channel"), source=entry.get("source"), steer=entry.get("steer"), loads_since=entry.get("loads_since")))
        self.current = Current(task, entry, ctl, fut)
        marker.unlink(missing_ok=True)

    def _start_unit(self, task: Task, entry: dict[str, Any], load_seconds: float) -> Current:
        """起运行单元：开跑参数写进 <单元>.json，runner.json 的 current 记下单元名（重启后据此接管）。"""
        run_id = str(entry["resume"]) if entry.get("resume") else new_run_id()
        unit, t0 = next_unit(task.name, run_id), time.time()
        _atomic_write(_unit_file(unit), json.dumps({"entry": entry, "run_id": run_id, "load_seconds": load_seconds, "t0": t0}, ensure_ascii=False))
        _live_file(unit).unlink(missing_ok=True)
        _write_state({"task": task.name, "run_id": run_id, "started": t0, "priority": task.priority, "preemptible": task.preemptible, "unit": unit})
        return self._attach(task, entry, unit, run_id, t0, start=True)

    def _attach(self, task: Task, entry: dict[str, Any], unit: str, run_id: str, t0: float, start: bool) -> Current:
        async def go() -> UnitRun:
            if start:
                await asyncio.to_thread(start_unit, unit, task)
            return await wait_unit(unit, task.name, run_id, t0)

        cur = Current(task, entry, UnitControl(unit, RUNS / task.name / run_id, t0), asyncio.create_task(go()))  # type: ignore[arg-type]
        cur.t0 = t0
        return cur

    def _adopt(self) -> Current | None:
        """新运行器启动：runner.json 记着的运行单元交给本运行器接着管（还在跑就等它结束，已结束就马上收尾）。"""
        cur = _read(RUNNER_STATE).get("current") or {}
        spec = _read(_unit_file(str(cur["unit"]))) if cur.get("unit") else {}
        if not spec:
            return None
        try:
            task = load_task(task_file(spec["entry"]["task"]), load_models())
        except Exception as e:  # noqa: BLE001
            print(f"接管运行单元 {cur['unit']} 失败：{e}", flush=True)
            return None
        print(f"接管运行单元 {cur['unit']}（{task.name} {spec['run_id']}）", flush=True)
        _write_state(cur)
        return self._attach(task, spec["entry"], str(cur["unit"]), str(spec["run_id"]), float(spec["t0"]), start=False)

    async def _finish(self) -> None:
        cur = self.current
        try:
            await self._settle()
        finally:
            if cur is not None and isinstance(cur.ctl, UnitControl):     # 运行单元收完尾：清掉当前运行与两边的通信文件
                _write_state(None)
                _unit_file(cur.ctl.unit).unlink(missing_ok=True)
                _live_file(cur.ctl.unit).unlink(missing_ok=True)

    async def _settle(self) -> None:
        cur = self.current
        assert cur is not None
        self.current = None
        try:
            run = cur.fut.result()
        except Exception as e:  # noqa: BLE001
            _write_state(None)
            if isinstance(e, UnitNotStarted) and cur.ctl.pause_reason in ("preempt", "manual"):
                # 起单元后马上被抢占 / 暂停、运行进程还没开跑：算暂停，条目原样放回队列（恢复中的运行 run.json 没动过，仍从原检查点恢复）
                requeue(cur.entry)
                print(f"{cur.task.name} 还没开跑就被{'抢占' if cur.ctl.pause_reason == 'preempt' else '暂停'}，记为 paused，条目放回队列", flush=True)
                return
            print(f"运行异常 {cur.task.name}：{type(e).__name__}: {e}", flush=True)
            if isinstance(cur.ctl, UnitControl):       # 单元没起来或没开跑就退出：私信 ❌，别让这条触发没有下文
                await hub_client.call("send", task=cur.task.name, text=f"❌ {cur.task.name} failed: {hub_client.escape(str(e))}")
            if cur.entry.get("trigger") == "catchup":
                await hub_client.call("send", text=f"⚠ {cur.task.name} 原定 {_slot_text(cur.entry)} 那次没跑成，补跑也出错了：{type(e).__name__}: {e}")
            return
        print(f"结束 {cur.task.name} {run.info['status']} {run.info.get('seconds')}s → {run.dir}", flush=True)
        if run.info["status"] == "stepping":              # workflow 跑完一步：下一步交给队列（要资源）或 stbcc-hub（不要资源）
            from control_center import workflow

            workflow.route(cur.task, run.run_id, cur.entry)
            return
        if cur.entry.get("trigger") == "catchup" and run.info["status"] not in ("paused", "waiting"):
            if run.info["status"] in ("complete", "partial"):
                await hub_client.call("send", text=f"🔁 以上是 {cur.task.name} 的补跑（原定 {_slot_text(cur.entry)} 那次没跑成）")
            else:
                await hub_client.call("send", text=f"⚠ {cur.task.name} 原定 {_slot_text(cur.entry)} 那次没跑成，补跑也失败了（{run.info['status']}），"
                                                       f"不再重试。运行目录：{run.dir}")
        if run.info["status"] == "paused":
            if cur.ctl.pause_reason == "preempt":
                enqueue(cur.task.name, trigger=norm_trigger(cur.entry.get("trigger"), "schedule"), resume=run.run_id,
                        priority=int(cur.entry.get("priority", cur.task.priority)), enqueued_at=float(cur.entry.get("enqueued_at", cur.t0)),
                        catchup_slot=cur.entry.get("catchup_slot"))
                n = int(run.info.get("preempt_count", 0))
                if n > PREEMPT_WARN_AFTER and int(cur.entry.get("priority", cur.task.priority)) >= PREEMPT_WARN_MIN_PRIORITY:
                    await hub_client.call("send", text=f"⚠ {cur.task.name} 已被抢占 {n} 次，累计暂停 {int(run.info.get('paused_seconds', 0))} 秒；考虑调整它的优先级或时间表（运行 {run.run_id}）")
            return
        if cur.task.name.startswith(builder.ONEOFF_PREFIX) and run.info["status"] in builder.ONEOFF_DONE:
            builder.remove_oneoff(cur.task.name)          # 一次性任务跑完就注销（同 stb task rm），运行记录留着
        if not cur.task.queues_overlap:                   # 没订阅事件的任务：运行期间又入队的丢掉（跑完这次就是最新的）
            for p, e in _queue_entries():
                if e.get("task") == cur.task.name and not e.get("resume") and cur.t0 <= float(e.get("enqueued_at", 0)) <= time.time():
                    p.unlink(missing_ok=True)
                    print(f"没订阅事件：丢弃运行期间入队的 {cur.task.name}", flush=True)

    async def tick(self) -> bool:
        """一次调度决策。返回是否有事发生（测试用）。"""
        await self._catchup()
        if self.units and time.time() - self._reap_checked >= REAP_CHECK_S:
            self._reap_checked = time.time()
            await asyncio.to_thread(reap_leftovers)
        cur = self.current
        if cur is None:
            picked = await self._pick()
            if picked is None:
                await self._presence()
                return False
            await self._start(*picked)
            await self._presence()
            return True
        if cur.fut.done():
            await self._finish()
            picked = await self._pick()     # 紧接着起下一个，状态栏不闪"空闲"
            if picked is not None:
                await self._start(*picked)
            await self._presence()
            return True
        loading = getattr(cur.ctl.gateway, "loading", None) is not None     # D32：子代理换模型的加载期间别的任务排队：不抢占、不强停
        changed = presence_text(cur, self.model_ok) != self._presence_shown and time.time() - self._presence_at >= PRESENCE_MIN_GAP_S
        if (loading or changed or str(self._presence_shown).startswith("加载模型")
                or time.time() - self._presence_at >= PRESENCE_REFRESH_S):
            await self._presence()             # D32 加载期间"加载模型：X"、加载完恢复；文字没变只按 PRESENCE_REFRESH_S 重发
        best = pick()
        cur_priority = int(cur.entry.get("priority", cur.task.priority))
        wrapping = getattr(cur.ctl.gateway, "wrapup", False) is True     # D34 收尾轮只有几分钟：不抢占、不手动暂停，收完再说
        if best is not None and int(best[1].get("priority", 5)) > cur_priority and getattr(cur.ctl.gateway, "ask_waiting", False) is True:
            cur.ctl.gateway.ask_yield = True             # pyright: ignore[reportOptionalMemberAccess]  # D35：就地等回答时来了更高优先级的：马上让出
        if cur.ctl.pause_requested_at is None:
            if best is not None and int(best[1].get("priority", 5)) > cur_priority and cur.task.preemptible and not wrapping and not loading:
                print(f"抢占：{best[1]['task']}（{best[1]['priority']}）> {cur.task.name}（{cur_priority}），请求在下一个边界暂停", flush=True)
                cur.ctl.request_pause("preempt")
            elif _stop_requested(cur):
                cur.ctl.request_pause("stop")
            elif not wrapping and _manual_pause_requested(cur.task.name):
                cur.ctl.request_pause("manual")
        elif time.time() - cur.ctl.pause_requested_at > (grace := preempt_grace(cur.task)) and not cur.ctl.killed and not loading:
            print(f"等检查点 {grace} 秒到期，强停 {cur.task.name}", flush=True)
            cur.ctl.force_stop()
        # 停滞判定（agent 任务；workflow 的步骤没有 token 流，不判）：没有在途工具调用（含子代理调用）、且 STALL_S 秒没有任何模型输出与工具调用 → 杀掉判失败
        if not cur.task.workflow and not cur.ctl.killed and cur.ctl.run_dir is not None and not (cur.ctl.gateway and cur.ctl.gateway.inflight):
            idle = time.time() - cur.ctl.last_activity()
            if idle > STALL_S:
                print(f"停滞 {int(idle)} s > {STALL_S} s，强停 {cur.task.name}", flush=True)
                cur.ctl.stall_stop(idle)
        return False

    async def shutdown(self) -> None:
        """收到 SIGTERM（D18）：正在跑的运行按失败收尾（杀容器 → run.json failed → ❌ 私信），最多等 STOP_WAIT_S。
        没跑完的由下次启动的 recover_stale_runs 接手。队列里的条目是文件，重启后照常取；取出后还没开跑（含加载模型期间）被停的条目留着认领标记 .loading，
        下次启动放回队列（restore_claimed）。"""
        cur = self.current
        if cur is not None and isinstance(cur.ctl, UnitControl):     # 运行单元不随运行器停：runner.json 留着，下次启动接管
            print(f"收到 SIGTERM：{cur.task.name} 的运行单元 {cur.ctl.unit} 照常跑，下次启动接管", flush=True)
            cur.fut.cancel()
            return
        if cur is not None and not cur.fut.done():
            print(f"收到 SIGTERM，收尾 {cur.task.name}", flush=True)
            cur.ctl.stop_reason = STOP_REASON
            cur.ctl.force_stop()
            try:
                await asyncio.wait_for(asyncio.shield(cur.fut), STOP_WAIT_S)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                print(f"{cur.task.name} 没在 {STOP_WAIT_S} 秒内收尾，交给下次启动清理", flush=True)
        _write_state(None)

    async def serve(self) -> None:
        ensure_dirs()
        self.current = self._adopt() if self.units else None
        if self.current is None:
            _write_state(None)
        print(f"stbcc-runner 启动，队列 {QUEUE}", flush=True)
        restore_claimed(self.current.entry if self.current is not None else None)    # 取出了还没开跑的条目放回队列
        pending = recover_stale_runs()                                                      # 遗留运行
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        last_notice = 0.0
        while not stop.is_set():
            if pending and time.time() - last_notice >= NOTICE_RETRY_S:
                last_notice = time.time()
                pending = [d for d in pending if not await notify_stale(d)]
            if not await self.tick():
                try:
                    await asyncio.wait_for(stop.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
        await self.shutdown()


async def run_once() -> bool:
    """跑完队列里优先级最高的一条（不抢占）。没有可跑的返回 False。测试与 --wait 用。"""
    r = Runner()
    if not await r.tick():
        return False
    while r.current is not None:
        await asyncio.sleep(0.02)
        await r.tick()
    return True


async def serve() -> None:
    await Runner(units=True).serve()
