"""hooks（2026-10-05 Joy 定，照 Claude Code 的 hooks）：tool.yaml、子代理 yaml（subagents/<名>/subagent.yaml）、任务 yaml 都可以写 hooks，
只能写四个事件（EVENTS），写别的键就拒。
- 主人：工具 / 子代理的 pre_call、post_call 管调用它自己；任务的 pre_call、post_call 管本任务的运行里的每次调用（同 Claude Code 的 PreToolUse / PostToolUse
  + matcher）：主模型调工具、调子代理，子代理调它的工具，平台代发的调用（如运行结束时代发最终回答的 reply）都算；match 写函数名或工具名（对上其一就跑）。
  一次调用两边都有 hook 时先跑任务的、再跑工具（子代理）自己的：工具自己的 pre_call 拿到的是任务的 hook 改写之后、真要执行的参数，它的检查和拦截最后说了算。

- pre_call：每次调用这个工具（子代理：主模型调用它本身，function 是子代理名）之前。退出码 2 = 这次不执行，stderr 原文交给模型；
  stdout 输出 {"args": {...}} = 改写这次调用的参数（同 Claude Code PreToolUse 的 updatedInput；如 send-email 换收件人）。
- post_call：每次调用之后（成功、失败都调；用法规范先返回、被 pre_call 拦下的不算调用）。不能拦。
- run_start：这次运行开始（工具 / 子代理：用到它的运行开始时；暂停、挂起后的续跑不算新开始）。退出码 2 = 这次运行不开跑（记失败、私信 Joy）。
- run_end：这次运行的一段结束（任何结束方式都调：完成、失败、超时、暂停、挂起等回答、/stop、被 run_start 拦下；在发结束通知之前；
  运行进程没收尾就没了的，运行器标失败时补调，status 是 failed）。不能拦；
  stdout 输出 {"status": "failed", "error": "原因"} = 把本来 complete 的这一段改判 failed（同 pre_call 的 {"args": …}，是 hook 的输出格式），
  之后照常走失败通知；只能从 complete 改成 failed，别的状态不变；几个 hook 都输出时原因用「；」合并。stdout 不是这个格式的不理会。
写法：hooks: {事件: [{command: <本文件夹里的文件>, match: [函数名…]（可选，只给 pre_call / post_call；不写 = 全部函数；任务的还可以写工具名）, timeout: 60s（可选，默认 60s）}]}。
「本文件夹」：工具是 tool.yaml 所在的文件夹，子代理是 subagents/<名>/，任务是 tasks/<名>/。
.py 用平台 venv 的 python 跑，.sh 用 bash，别的直接执行（要有执行权限）。
输入：一段 JSON 从 stdin 交给它：{event, task, run_id, run_dir, work_dir, tool, subagent, function, args}；run_dir 是本次运行目录的宿主路径（主模型看不到它；
没有运行目录时为 null），平台在里面放每个工具的本次运行目录 <工具名>/（STB_TOOL_RUN_DIR）、子代理调用结果 subagent_results.jsonl（gateway.SUBAGENT_RESULTS）等；
work_dir 是本次运行的工作目录（没有为 null）；
post_call 另带 result、ok（这次调用成功；报错、超时都是 false）、inflight（同一次运行里这个工具还在途的别的调用数：并发调用共用的东西等最后一个结束再收）；
run_end 另带 status、error（这一段的失败原因，没有为空串；hook 跑之前 run.json 已存过一次，读得到这一段的 final_text）；
run_start / run_end 的 function、args 为 null，tool / subagent 是这个 hook 的主人（任务自己的 hook 两个都是 null）；
pre_call / post_call 的 tool、subagent、function 是这次调用的（任务的 hook 也一样）。
退出码：0 正常；2 = 拦（只有 pre_call、run_start 有效，别的事件当出错）；其他 = 出错：记日志、私信 Joy 一次（同一运行同一个 hook 只私信一次），不影响运行；超时同出错。
身份：is_editable: false 的工具 / 子代理（Joy 维护的）以平台身份跑，能做 sparky 做不了的事（如改平台身份才能写的文件）；
  别的工具 / 子代理和所有任务的 hook 以 sparky 跑（control_center/launch.py 同一个启动器）：文件看 sparky 的文件权限，
  联网同主人的 network（子代理没有 network：不联网）。
每次执行记进运行目录的 hooks.jsonl（事件、主人、文件、退出码、秒数、stderr 末尾），不含输入输出原文。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENTS = ("pre_call", "post_call", "run_start", "run_end")
CALL_EVENTS = ("pre_call", "post_call")       # match 只给这两个
BLOCKING = ("pre_call", "run_start")          # 退出码 2 能拦的
BLOCK_RC = 2
DEFAULT_TIMEOUT = 60
HOOK_KEYS = ("command", "match", "timeout")
_notified: set[tuple[str, str, str]] = set()  # (运行 id, 主人, 文件)：出错只私信 Joy 一次


@dataclass(frozen=True)
class Hook:
    command: str                     # 宿主上的绝对路径（解析时已核对在主人的文件夹里、是文件）
    match: tuple[str, ...] = ()      # 只对这些函数；空 = 全部
    timeout: int = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Owner:
    """hook 的主人：工具、子代理或任务。editable 决定身份（见模块说明）；network 是 sparky 身份时的联网限制（Landlock）。"""
    kind: str                        # tool / subagent / task
    name: str
    hooks: dict[str, tuple[Hook, ...]]
    editable: bool
    network: Any = None


def parse(where: str, raw: Any, folder: Path | None) -> dict[str, tuple[Hook, ...]]:
    """yaml 里的 hooks 段 → {事件: (Hook…)}；没写是 {}。folder：hook 文件所在的文件夹（None = 这个主人不能写 hooks，原因写在 where 里）。
    不合格抛 ValueError（中文说明哪一项不对）。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}：hooks 要写成映射 {{事件: [{{command, match, timeout}}]}}，事件只能是 {', '.join(EVENTS)}")
    if bad := sorted(set(raw) - set(EVENTS)):
        raise ValueError(f"{where}：hooks 里的 {bad} 不是能写的事件（只能是 {', '.join(EVENTS)}）")
    if raw and folder is None:
        raise ValueError(f"{where}：hooks 的 command 是本文件夹里的文件，这里没有文件夹")
    out: dict[str, tuple[Hook, ...]] = {}
    for ev, items in raw.items():
        if not isinstance(items, list) or not items:
            raise ValueError(f"{where}：hooks.{ev} 要写成列表，每项 {{command: <本文件夹里的文件>, match: [函数名]（可选）, timeout: 60s（可选）}}")
        hooks = []
        for i, h in enumerate(items, 1):
            at = f"{where}：hooks.{ev} 第 {i} 项"
            if not isinstance(h, dict) or "command" not in h or (extra := set(h) - set(HOOK_KEYS)):
                raise ValueError(f"{at} 要写成 {{command, match?, timeout?}}" + (f"（多了 {sorted(extra)}）" if isinstance(h, dict) and "command" in h else ""))  # pyright: ignore[reportPossiblyUnboundVariable]
            cmd = h["command"]
            assert folder is not None
            if not isinstance(cmd, str) or "/" in cmd or cmd in ("", ".", "..") or not (folder / cmd).is_file():
                raise ValueError(f"{at}：command 要是本文件夹（{folder}）里的文件名，现在写的是 {cmd!r}")
            match = h.get("match")
            if match is not None:
                if ev not in CALL_EVENTS:
                    raise ValueError(f"{at}：match 只给 pre_call / post_call（按函数名过滤），{ev} 不写")
                if not isinstance(match, list) or not match or not all(isinstance(x, str) and x for x in match):
                    raise ValueError(f"{at}：match 要写函数名列表，如 [send-email_send_digest]")
            timeout = _seconds(h.get("timeout", DEFAULT_TIMEOUT))
            if timeout is None:
                raise ValueError(f"{at}：timeout 写成 60s、5m 这种时长（大于 0）")
            hooks.append(Hook(command=str((folder / cmd).resolve()), match=tuple(match or ()), timeout=timeout))
        out[ev] = tuple(hooks)
    return out


def _seconds(v: Any) -> int | None:
    """时长（同任务的 20m、90s、1h30m，或正整数秒）→ 秒；不合格 None。不用 task.parse_duration：registry 导入时就解析 tool.yaml，会循环 import。"""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if v > 0 else None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", str(v).strip())
    n = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0) if m else 0
    return n or None


# ---- 主人 ----
def tool_owner(name: str) -> Owner | None:
    from control_center.registry import TOOLS

    spec = TOOLS.get(name)
    if spec is None or not spec.hooks:
        return None
    return Owner("tool", name, spec.hooks, spec.is_editable, spec.network)


def subagent_owner(name: str) -> Owner | None:
    """子代理的 hooks（subagents/<名>/subagent.yaml）。"""
    from control_center import subagents

    try:
        a = subagents.load_def(name)
    except subagents.SubagentError:
        return None
    return Owner("subagent", name, a.hooks, a.is_editable) if a.hooks else None


def task_owner(task: Any) -> Owner | None:
    """任务的 hooks：一律以 sparky 跑。"""
    if not task.hook_map:
        return None
    return Owner("task", task.name, task.hook_map, True, task.network)


def run_owners(task: Any, loaded: Any = ()) -> list[Owner]:
    """一次运行的主人：任务自己，再是它用到的每个工具和子代理（任务 yaml 的 subagents，去重），以及 agent 子代理自己的工具（任务没列的）；
    loaded 是本次运行经 load-tool 加载过的工具与子代理名（run.json 的 loaded），一样算。"""
    from control_center import subagents
    from control_center.registry import TOOLS

    extra = [str(n) for n in loaded or ()]
    tools = [t.name for t in task.tools] + [n for n in extra if n in TOOLS]
    subs = dict.fromkeys([*(u.name for u in getattr(task, "subagents", [])), *(n for n in extra if n not in TOOLS)])
    for s in subs:
        if s and subagents.is_folder(s):
            try:
                tools += [t for t in subagents.load_def(s).tools if t not in tools]
            except subagents.SubagentError:
                pass
    out = [task_owner(task), *(tool_owner(t) for t in tools), *(subagent_owner(s) for s in subs if s)]
    return [o for o in out if o is not None]


# ---- 执行 ----
@dataclass
class Outcome:
    blocked: str | None = None       # 被拦下：stderr 原文（没写就是一句说明）
    args: dict[str, Any] | None = None   # pre_call 改写后的参数
    failed: str | None = None        # run_end 要求改判 failed 的原因（几个 hook 的用「；」合并）


def _argv(command: str) -> list[str]:
    if command.endswith(".py"):
        return [sys.executable, command]
    if command.endswith(".sh"):
        return ["bash", command]
    return [command]


def _command(owner: Owner, hook: Hook, run_dir: Path | None, work_dir: Path | None, run_id: str) -> tuple[list[str], dict[str, str]]:
    """以谁的身份跑：editable 的主人 sparky（launch.sparky_command，联网按主人的 network），否则平台身份直接跑。环境只有几个基本变量（加平台的 registry.PLATFORM_ENV 与 STB_TEST_ 开头的测试开关）。"""
    from control_center import launch
    from control_center.registry import PLATFORM_ENV, TEST_ENV_PREFIX

    from control_center.paths import child_pythonpath

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()), "LANG": "C.UTF-8", "TMPDIR": "/tmp",   # 数据目录、设置与 .env 跟着平台走（hook 里 import control_center 按它找 data/…）；测试开关透传
           "PYTHONPATH": child_pythonpath(),                                  # hook 里 import 的 control_center 是本仓库的（同 launch.tool_command）
           **{k: v for k, v in os.environ.items() if k in PLATFORM_ENV or k.startswith(TEST_ENV_PREFIX)}}
    argv = _argv(hook.command)
    if not owner.editable or os.environ.get("STB_TEST_LAUNCH_PLAIN"):   # 测试钩子同 launch.tool_command：临时目录里 sparky 进不去
        return argv, env
    cfg = {**launch.net_cfg(owner.network), "exec": shlex.join(argv)}
    return launch.sparky_command(sys.executable, cfg, env, f"hook:{owner.kind}:{owner.name}"), env


def _log(run_dir: Path | None, rec: dict[str, Any]) -> None:
    if run_dir is None:
        return
    try:
        with open(run_dir / "hooks.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _notify(run_id: str, owner: Owner, hook: Hook, why: str) -> None:
    """hook 出错：同一运行同一个 hook 只私信 Joy 一次（中枢不在就算了）。"""
    key = (run_id, f"{owner.kind}:{owner.name}", hook.command)
    if key in _notified:
        return
    _notified.add(key)
    from control_center import hub_client

    text = f"⚠ {owner.kind} {owner.name} 的 hook {Path(hook.command).name} 出错（运行 {run_id}）：{why[:500]}；运行照常继续"
    t = threading.Thread(target=lambda: asyncio.run(hub_client.call("send", text=text)), daemon=True)   # 另起线程：调用方可能在事件循环里（/clear）
    t.start()
    t.join(20)


def fire(event: str, owners: list[Owner], payload: dict[str, Any], run_dir: Path | None = None, work_dir: Path | None = None) -> Outcome:
    """按顺序执行这些主人在 event 上的 hook（pre_call / post_call 只跑 match 对得上的：工具 / 子代理的按 payload["function"]，任务的按 function 或 tool）。同步，调用方在线程里跑。
    pre_call 改写的参数接着交给下一个 hook；有一个拦下（退出码 2）就停，返回 blocked。"""
    out = Outcome()
    run_id = str(payload.get("run_id") or "")
    data = dict(payload, event=event)
    data.setdefault("run_dir", str(run_dir) if run_dir is not None else None)
    data.setdefault("work_dir", str(work_dir) if work_dir is not None else None)
    for owner in owners:
        if event not in CALL_EVENTS:                       # 运行事件：tool / subagent 写这个主人自己（任务的 hook 两个都是 null）
            data.update(tool=owner.name if owner.kind == "tool" else None, subagent=owner.name if owner.kind == "subagent" else None)
        for hook in owner.hooks.get(event, ()):
            if event in CALL_EVENTS and hook.match and not {data.get("function"), *([data.get("tool")] if owner.kind == "task" else [])} & set(hook.match):
                continue
            rec: dict[str, Any] = {"event": event, "owner": f"{owner.kind}:{owner.name}", "command": Path(hook.command).name}
            t0 = time.monotonic()
            try:
                argv, env = _command(owner, hook, run_dir, work_dir, run_id)
                p = subprocess.run(argv, input=json.dumps(data, ensure_ascii=False, default=str).encode(), capture_output=True,
                                   timeout=hook.timeout, env=env, cwd=str(Path(hook.command).parent))
                rc, stdout, stderr = p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace").strip()
            except subprocess.TimeoutExpired:
                rc, stdout, stderr = None, "", f"超时（{hook.timeout} 秒）"
            except Exception as e:  # noqa: BLE001 — 起不来（文件没执行权限、launch 展开路径失败）也只算这个 hook 出错
                rc, stdout, stderr = None, "", f"起不来：{type(e).__name__}: {e}"
            rec.update(rc=rc, seconds=round(time.monotonic() - t0, 3), stderr=stderr[-300:])
            if rc == BLOCK_RC and event in BLOCKING:
                out.blocked = stderr or f"{owner.kind} {owner.name} 的 hook {Path(hook.command).name} 拦下了（没写原因）"
                _log(run_dir, {**rec, "blocked": True})
                return out
            if rc != 0:
                why = stderr or f"退出码 {rc}"
                _log(run_dir, {**rec, "error": True})
                _notify(run_id, owner, hook, why if rc != BLOCK_RC else f"{event} 不能拦（退出码 2 当出错）：{why}")
                continue
            if event == "pre_call" and stdout.strip():
                try:
                    got = json.loads(stdout)
                    args = got.get("args") if isinstance(got, dict) else None
                    if args is not None and not isinstance(args, dict):
                        raise ValueError("args 要是对象")
                except ValueError as e:
                    _log(run_dir, {**rec, "error": True})
                    _notify(run_id, owner, hook, f"stdout 不是 {{\"args\": {{...}}}}：{e}")
                    continue
                if args is not None:
                    data["args"] = out.args = args
                    rec["args_rewritten"] = True
            if event == "run_end" and (why := _fail_request(stdout)) is not None:
                out.failed = f"{out.failed}；{why}" if out.failed else why
                rec["status_failed"] = True
            _log(run_dir, rec)
    return out


def _fail_request(stdout: str) -> str | None:
    """run_end 的 stdout {"status": "failed", "error": "原因"} → 原因；不是这个格式（含空、普通日志文字）返回 None。"""
    try:
        got = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        return None
    if not isinstance(got, dict) or got.get("status") != "failed":
        return None
    return str(got.get("error") or "run_end hook 判这次运行失败（没写原因）").strip()[:1000]


async def afire(event: str, owners: list[Owner], payload: dict[str, Any], run_dir: Path | None = None, work_dir: Path | None = None) -> Outcome:
    """fire 的异步版（在线程里跑，不卡网关与运行器的事件循环）。没有要跑的 hook 时直接返回。"""
    if not any(owner.hooks.get(event) for owner in owners):
        return Outcome()
    return await asyncio.to_thread(fire, event, owners, payload, run_dir, work_dir)
