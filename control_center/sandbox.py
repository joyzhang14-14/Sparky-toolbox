"""agent 的沙箱（规格第 7 节、7.1 第二层）：平台统一一种，任务 yaml 不写。
一次性容器（每段运行 docker run --rm 一个，跑完即删）：镜像 IMAGE（pi + uv + git，无浏览器）、只读根文件系统、--cap-drop ALL、--init（PID 1 收掉孤儿进程的僵尸）、以 sparky 跑（paths.sparky_user，带 sparkle 组）、内存上限 MEMORY，
按原路径挂：sparky 的家目录（读写；builder 的开发区 dev/、预演区 stage/ 只读）、Joy 的家目录（只读）；沙箱里外同一个路径，
启动时的工作目录是任务的工作目录 <workspace>/<任务>/（跨运行保留）；另挂 /out（agent 自己的日志）。所有任务一样，不按工具判断。
网络只连网关：内部桥 NET_TOOLS_ONLY（--internal，出不了外网），模型与工具都经网关；要联网的活交给工具 / 子代理。
工具都不在沙箱里，在宿主上以 sparky 跑（如 shell：命令在 Sparky 本机以 sparky 用户执行，每条先经 Joy 审批）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from control_center import sandbox_env
from control_center.paths import DOWNLOADS, REPO, host_tz, share, shared_gid, sparky_home, sparky_user
from control_center.prompts import START_PROMPT, now_line, system_prompt
from control_center.task import Task

NET_TOOLS_ONLY = "stb-net"      # docker network create --internal stb-net
IMAGE = "stb-agent:latest"      # 沙箱镜像：pi + uv + git，无浏览器
IMAGE_PATH = "/home/agent/.local/bin:/home/agent/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"   # 镜像自带的 PATH（services/stb-agent 的基础镜像）
IMAGE_UID = "1001"              # 镜像里的用户 agent 的 uid（同宿主 sparky，services/stb-agent/Dockerfile）
MEMORY = "4g"                   # 沙箱容器的内存上限
# pi 自带的 compaction：模型报上下文超长时先压缩（旧内容写成摘要、最近约 2 万 token 原样留着）再重试一次，扩展 stb-gateway.ts 等重试结束再收尾
# （Joy 2026-10-01：满了自动压缩、继续执行，像 Claude Code）；单次 -p 运行中途不做阈值压缩，只在收尾与续跑时按阈值压缩。每次运行都是新会话，不跨运行续用。
PI_SETTINGS = {"compaction": {"enabled": True}}
EXT_DIR = REPO / "services" / "owa-research" / "owa_research" / "adapters" / "pi"
READ_ONLY_IN_HOME = ("dev", "stage")   # sparky 家目录里只读挂的：builder 的开发区、预演区
# pi 自带的工具不关：扩展 stb-gateway.ts 按网关 /tools 只开任务列了的（平台工具 workspace 的函数就是 pi 自带的）
PROMPT_FILE = "prompt.txt"   # 日志目录里：这次给 pi 的用户消息，经 stdin 交给 pi（-p 不带消息时 pi 读管道）；不放命令行：单个参数上限 128 KB，私信带上以前加载过的工具与 skill 会超
PI_ARGS = ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", "--offline"]


def ensure_network() -> None:
    if subprocess.run(["docker", "network", "inspect", NET_TOOLS_ONLY], capture_output=True).returncode != 0:
        subprocess.run(["docker", "network", "create", "--internal", NET_TOOLS_ONLY], check=True, capture_output=True)


def bridge_ip(network: str) -> str:
    out = subprocess.run(["docker", "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        raise RuntimeError(f"拿不到 docker 网络 {network} 的网关 IP")
    return out


def network_for() -> tuple[list[str], str]:
    """返回 (docker 网络参数, 网关应绑定的宿主 IP)：沙箱只在内部桥 NET_TOOLS_ONLY 上，网关绑这座桥在宿主一侧的 IP。"""
    ensure_network()
    return ["--network", NET_TOOLS_ONLY], bridge_ip(NET_TOOLS_ONLY)


def pi_settings_file(run_dir: Path) -> Path:
    p = run_dir / "pi-settings.json"
    p.write_text(json.dumps(PI_SETTINGS))
    return p


def docker_command(task: Task, *, name: str, run_dir: Path, work_dir: Path, models_json: Path, gateway_url: str,
                   token: str, model: str, net_args: list[str], prompt: str | None = None,
                   session_dir: Path | None = None, session_file: Path | None = None,
                   attachments: list[str] | None = None) -> list[str]:
    """主 agent 的沙箱：日志在运行目录的 agent/，系统提示由任务生成，用户消息末尾接现在的本机时间（每次起 pi 重算）；每次运行一个新会话，同一次运行续跑时 --session 接着原文件。"""
    return pi_command(name=name, out_dir=run_dir / "agent", settings=pi_settings_file(run_dir), work_dir=work_dir, models_json=models_json,
                      gateway_url=gateway_url, token=token, model=model, net_args=net_args, system=system_prompt(task),
                      prompt=f"{prompt or START_PROMPT}\n\n{now_line()}", session_dir=session_dir, session_file=session_file, attachments=attachments,
                      effort=task.effort)


def pi_command(*, name: str, out_dir: Path, settings: Path, work_dir: Path, models_json: Path, gateway_url: str, token: str, model: str,
               net_args: list[str], system: str, prompt: str, session_dir: Path | None = None, session_file: Path | None = None,
               attachments: list[str] | None = None, effort: str | None = None) -> list[str]:
    """起一个跑 pi 的沙箱容器的 docker 命令。主 agent 与 agent 子代理（gateway._run_agent_pi）是同一种沙箱，只是日志目录 out_dir（挂到 /out）、
    系统提示、令牌（网关按令牌认出是谁、给哪些工具）与模型不同。这次给 pi 的完整系统提示存进 out_dir/system_prompt.txt（续跑时覆盖成那一刻的版本）；
    用户消息存进 out_dir/prompt.txt（PROMPT_FILE），run_container / run_pi 把它接到容器的 stdin。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    share(out_dir, 7)                                      # 容器以 sparky 跑：日志目录组可写
    (out_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (out_dir / PROMPT_FILE).write_text(prompt, encoding="utf-8")
    # 会话文件：会话目录挂到 /session（读写；没给就写在 /out/session）；接着原会话时 --session 给原文件
    session_args = ["--session-dir", "/out/session"]
    session_mount: list[str] = []
    if session_dir is not None:
        session_dir.mkdir(parents=True, exist_ok=True)     # 先建好：让 docker 自动建的话属主是 root，容器里以 sparky 跑的 pi 写不进去
        share(session_dir, 7)
        session_mount = ["-v", f"{session_dir}:/session:rw"]
        session_args = ["--session-dir", "/session"] + (["--session", f"/session/{session_file.name}"] if session_file else [])
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    user, gid = sparky_user(), shared_gid()
    ident = [*(["--user", user] if user else []), *(["--group-add", str(gid)] if gid is not None else [])]   # 以 sparky 跑；没有 sparky（测试容器）用镜像里的用户
    cuid, cgid = (user or f"{IMAGE_UID}:{IMAGE_UID}").split(":")
    mounts = workspace_mounts(work_dir)
    return [
        "docker", "run", "--rm", "-i", "--name", name, *net_args,
        "--init",                                   # PID 1 是 docker 的小 init：收掉孤儿进程退出后留下的僵尸、把停止信号转给 pi
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *ident,
        "--memory", MEMORY, "--cpus", "4", "--pids-limit", "512",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m", "--tmpfs", f"/home/agent/.pi/agent:rw,size=64m,uid={cuid},gid={cgid}",
        "-v", f"{models_json}:/home/agent/.pi/agent/models.json:ro",
        "-v", f"{settings}:/home/agent/.pi/agent/settings.json:ro",
        "-v", f"{EXT_DIR / 'toollog.ts'}:/opt/stb/ext/toollog.ts:ro", "-v", f"{EXT_DIR / 'stb-gateway.ts'}:/opt/stb/ext/stb-gateway.ts:ro",
        *mounts, *session_mount, "-v", f"{out_dir}:/out:rw", "-w", str(work_dir),
        "-e", f"STB_GATEWAY_URL={gateway_url}", "-e", f"STB_GATEWAY_TOKEN={token}", "-e", "HARNESS_LOG=/out/toollog.jsonl", "-e", "PI_OFFLINE=1",
        "-e", f"PATH={sandbox_env.ENV / 'bin'}:{IMAGE_PATH}",   # 沙箱共用的安装环境放最前（download_install 装的程序与包，control_center/sandbox_env.py）
        "-e", f"TZ={host_tz()}",   # 容器里的"现在"按本机时区（D22 J6），和系统提示里注入的本机时间、研究请求的日期一致
        IMAGE, *PI_ARGS, "--model", model, *(["--thinking", effort] if effort else []), *session_args,   # effort：推理档位（models.json 同时打开 supportsReasoningEffort）
        "-e", "/opt/stb/ext/toollog.ts", "-e", "/opt/stb/ext/stb-gateway.ts", "--mode", "json",
        "--system-prompt", system, "-p", *(f"@{a}" for a in attachments or []),
    ]


def workspace_mounts(work_dir: Path) -> list[str]:
    """沙箱的挂载（按原路径，沙箱里外同一个路径）：sparky 的家目录读写（READ_ONLY_IN_HOME 只读）、Joy 的家目录只读；
    工作目录、下载目录不在 sparky 的家目录下（测试、改过 workspace）时另挂读写。"""
    home, joy = sparky_home(), Path.home()
    out = ["-v", f"{home}:{home}:rw"]
    out += [x for d in READ_ONLY_IN_HOME if (home / d).is_dir() for x in ("-v", f"{home / d}:{home / d}:ro")]
    out += ["-v", f"{joy}:{joy}:ro"]
    for d in (work_dir, DOWNLOADS):
        if home not in d.resolve().parents:
            out += ["-v", f"{d}:{d}:rw"]
    return out


async def _wait(proc: Any, timeout_seconds: int, extra: Callable[[], float] | None) -> int:
    """等进程结束；extra() 是运行中追加的秒数（D19 换档延长超时），每 5 秒重算一次截止时刻。到点抛 TimeoutError。
    timeout_seconds 可以是负数（恢复的运行已超出 base 档，靠 extra 补回来）。"""
    deadline = time.monotonic() + timeout_seconds
    while True:
        left = deadline + (extra() if extra else 0) - time.monotonic()
        if left <= 0:
            raise asyncio.TimeoutError
        try:
            return await asyncio.wait_for(proc.wait(), min(left, 5) if extra else left)
        except asyncio.TimeoutError:
            continue


STREAM_LIMIT = 64 * 1024 * 1024        # pi 的一行事件最长多大（整条消息 + 工具结果）


async def _pump(stream: asyncio.StreamReader, path: Path) -> None:
    """pi 的 json 事件流 → pi-output.jsonl：流式增量 message_update（每个都带整条消息，占大头）不存，
    只存 message_end、工具事件等；丢掉增量时照样刷新文件修改时间（停滞判定看它，runner.last_activity）。"""
    touched = 0.0
    with open(path, "wb") as out:
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                line = await stream.read(STREAM_LIMIT)       # 超长的一行：原样照写
            if not line:
                return
            if b'"message_update"' in line[:200]:
                try:
                    skip = json.loads(line).get("type") == "message_update"
                except (json.JSONDecodeError, AttributeError):
                    skip = False
                if skip:
                    if time.time() - touched >= 1:
                        os.utime(path)
                        touched = time.time()
                    continue
            out.write(line)
            out.flush()


def kill_group(pid: int, sig: int, user: str | None = None) -> None:
    """给进程组发信号（按组号，不按名字）。user：组里的命令以这个用户跑（sudo -u）：平台用户直接发只发得到 sudo 本身，
    再以那个用户发一次（sudo -n -u <user> kill -<sig> -- -<组号>），SIGKILL 也杀得到。"""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    if user:
        subprocess.run(["sudo", "-n", "-u", user, "kill", f"-{int(sig)}", "--", f"-{pid}"], capture_output=True, timeout=30)


def group_alive(pid: int, user: str | None = None) -> bool:
    """进程组里还有没有进程（user 同 kill_group：以那个用户问）。"""
    if user:
        return subprocess.run(["sudo", "-n", "-u", user, "kill", "-0", "--", f"-{pid}"], capture_output=True, timeout=30).returncode == 0
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def run_container(cmd: list[str], name: str, timeout_seconds: int, stdout: Path, stderr: Path,
                        extra: Callable[[], float] | None = None, stdin: Path | None = None) -> tuple[int | None, bool]:
    """跑到结束或超时。超时：SIGTERM，10 秒后 kill。返回 (退出码, 是否超时)。stdin：接到进程标准输入的文件（pi 的用户消息），没给就是空。"""
    with open(stderr, "ab") as err, (open(stdin, "rb") if stdin else contextlib.nullcontext(asyncio.subprocess.DEVNULL)) as inp:
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=inp, stdout=asyncio.subprocess.PIPE, stderr=err,
                                                    limit=STREAM_LIMIT)
        assert proc.stdout                                         # stdout=PIPE，一定有
        pump = asyncio.create_task(_pump(proc.stdout, stdout))
        try:
            rc = await _wait(proc, timeout_seconds, extra)
            return rc, False
        except asyncio.TimeoutError:
            subprocess.run(["docker", "kill", "--signal", "TERM", name], capture_output=True)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                subprocess.run(["docker", "kill", name], capture_output=True)
                await proc.wait()
            return proc.returncode, True
        finally:
            await _drain(pump)


async def run_pi(cmd: list[str], name: str, stdout: Path, stderr: Path, stdin: Path | None = None) -> int | None:
    """agent 子代理的 pi 容器跑到 pi 自己退出（单次上限、卡住判定由网关在外面管：到点取消这个协程）；返回退出码。
    被取消时先停掉容器（docker kill）再把取消抛出去，不留孤儿容器。stdin 同 run_container。"""
    with open(stderr, "ab") as err, (open(stdin, "rb") if stdin else contextlib.nullcontext(asyncio.subprocess.DEVNULL)) as inp:
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=inp, stdout=asyncio.subprocess.PIPE, stderr=err,
                                                    limit=STREAM_LIMIT)
        assert proc.stdout                                         # stdout=PIPE，一定有
        pump = asyncio.create_task(_pump(proc.stdout, stdout))
        try:
            return await proc.wait()
        except asyncio.CancelledError:
            await asyncio.to_thread(subprocess.run, ["docker", "kill", name], capture_output=True)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
            raise
        finally:
            await _drain(pump)


async def _drain(pump: "asyncio.Task[None]") -> None:
    """进程结束后把管道里剩下的事件写完（最多等 10 秒：孙进程还占着管道时不卡住运行器）。"""
    try:
        await asyncio.wait_for(asyncio.shield(pump), 10)
    except asyncio.TimeoutError:
        pump.cancel()


def last_error(pi_output: Path) -> str | None:
    """最后一条 assistant 消息是模型报错（stopReason: error）就返回报错文字：pi 照样正常退出，不能当成完成。
    上下文超长时 pi 先压缩再重试（stb-gateway.ts 把其间的事件照写进事件流）：重试成功则最后一条是重试后的回复；
    压缩没成功或重试后仍超长，报错之后跟一条带 errorMessage 的 compaction_end，返回的文字写明「上下文超长、压缩后仍失败」。"""
    err = None
    if not pi_output.exists():
        return err
    for line in pi_output.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            m = ev["message"]
            err = (str(m.get("errorMessage") or f"stopReason=error 但没给 errorMessage，原始 message_end 前 300 字：{line[:300]}")
                   if m.get("stopReason") == "error" else None)
        elif ev.get("type") == "compaction_end" and ev.get("errorMessage") and err:
            err = f"上下文超长、压缩后仍失败（{ev['errorMessage']}）；模型的报错：{err}"
    return err


def last_empty(pi_output: Path) -> bool:
    """最后一条 assistant 消息既没有可见文字、也没有工具调用（只有 thinking 或完全空），且不是模型报错：
    模型想好了下一步却没调工具，pi 把「不调工具」当成结束。没有 assistant 消息时返回 False。"""
    last = None
    if not pi_output.exists():
        return False
    for line in pi_output.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            last = ev["message"]
    if last is None or last.get("stopReason") == "error":
        return False
    return not any(c.get("type") == "toolCall" or (c.get("type") == "text" and str(c.get("text") or "").strip()) for c in last.get("content") or [])


def final_text(pi_output: Path) -> tuple[str, dict[str, int], bool]:
    """从 pi 的 json 事件流取最后一条 assistant 文本、token 用量、是否正常结束。
    用量里 input 是全部输入（pi 的 input 不含前缀缓存命中与写入的部分，这里加回来），cached 是其中命中前缀缓存的
    （pi 的 cacheRead；vLLM 开了 --enable-prompt-tokens-details 才有）。"""
    text, usage, ended = "", {"input": 0, "output": 0, "cached": 0}, False
    if not pi_output.exists():
        return text, usage, ended
    for line in pi_output.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            m = ev["message"]
            t = "\n".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text").strip()
            if t:
                text = t
            u = m.get("usage") or {}
            cached = int(u.get("cacheRead") or 0)
            usage["input"] += int(u.get("input") or 0) + cached + int(u.get("cacheWrite") or 0)
            usage["output"] += int(u.get("output") or 0)
            usage["cached"] += cached
        elif ev.get("type") == "agent_end":
            ended = True
    return text, usage, ended
