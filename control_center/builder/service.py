"""stbcc-builder：builder 的开发区与部署（开发区里写文件跑命令、试跑、要凭证、部署、写 models.yaml 档位）都在这个常驻服务里做、在这里检查。
以 joyzhang14 运行（`stb builder --install` 装成 systemd 用户服务）；builder-dev 工具（tools/builder-dev，只给子代理 builder 用）只是客户端：以 sparky + Landlock 运行，只经本机 socket 发请求。
提案的审批与建开发区在子代理 builder 的 pre_call hook 里（subagents/builder/proposal_hook.py，以平台身份跑），不经这里。

- 本机接口 ~/sparky-toolbox/data/sockets/builder.sock：660、归 sparkle 组（客户端以 sparky 运行）；一行 JSON 请求、一行 JSON 回复。
  请求 {"op": …, "run": "<任务>/<运行 id>", "args": {…}}；回复 {"ok": true, "result": …} 或 {"ok": false, "error": 原因}。
- 谁能调：对端 uid 只能是平台用户或 sparky（SO_PEERCRED）；run 必须是正在跑的那次运行（运行目录在、任务用得到 TOOL：任务 yaml 的 tools 里有它，
  或任务 subagents 里的 agent 子代理的 tools 里有它）。
- op（与 builder-dev 工具的函数一一对应，逻辑都在 control_center/builder）：guide、status、request_credential、try_tool、try_subagent、check、write_file、dev_run、verify、submit；
  另有 end_trials（子代理 builder 的 post_call hook 经 request 发）：关掉这次运行里 try_tool 留着的试跑工具进程。verify、submit 之前也先关，运行不在跑的每 PRUNE_S 秒查一次关掉。
  try_subagent 只装预演副本（builder.stage_subagent），真跑由调用它的那次运行的网关接着做（gateway._try_subagent）。
  submit 同步做到合入，合入之后的事由 systemd-run 起的 worker 做（也以 joyzhang14 跑）。
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import socket
import struct
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from control_center import builder
from control_center.paths import REPO, RUNS, SOCKETS, TOOL_USER, ensure_dirs, share_socket, task_file
from control_center.registry import PLATFORM_FAULT

CLIENT_USERS = (TOOL_USER,)               # 除平台用户自己以外，能连这个 socket 的用户（builder-dev 工具的客户端以 sparky 跑）
TOOL = "builder-dev"                     # 能调这个服务的工具：运行用得到它才收请求（check_run）
LIMIT = 2 ** 24                          # 一行请求的上限：builder_write_file 一次最多 1 MB 文字
PRUNE_S = 30                             # 多久查一次：运行已经不在跑的，关掉它试跑时留着的工具进程

class Denied(Exception):
    pass


def socket_path() -> Path:
    return SOCKETS / "builder.sock"


def allowed_uids() -> set[int]:
    out = {os.getuid()}
    for u in CLIENT_USERS:
        try:
            out.add(pwd.getpwnam(u).pw_uid)
        except KeyError:
            pass
    return out


def _peer_uid(sock: socket.socket | None) -> int | None:
    if sock is None:
        return None
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    except OSError:
        return None
    return uid


def _share(path: Path) -> None:
    """socket 归 sparkle 组、660（connect 要写权限；CLIENT_USERS 都在组里）；测试替换它。"""
    share_socket(path)


def check_run(run: Any) -> str:
    """run 必须是 <任务>/<运行 id>：运行目录在、run.json 是 running（同 stbcc-docker）、任务用得到 TOOL（tools 里，或 subagents 里 agent 子代理的 tools 里）。返回 run。"""
    tool = TOOL
    if not isinstance(run, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}", run):
        raise Denied(f"请求要带 run（<任务>/<运行 id>，{TOOL} 工具的 STB_RUN_REF）")
    task, rid = run.split("/", 1)
    if not (RUNS / task / rid).is_dir():
        raise Denied(f"{PLATFORM_FAULT}运行 {run} 不存在（{RUNS / task / rid} 没有）：请求带的 STB_RUN_REF 对不上运行目录")
    try:
        status = json.loads((RUNS / task / rid / "run.json").read_text()).get("status")
    except (OSError, ValueError, AttributeError):
        status = None
    if status != "running":
        raise Denied(f"{PLATFORM_FAULT}运行 {run} 不在跑（run.json 的 status 是 {status!r}，不是 running）：运行已结束或在暂停，这次请求不再处理")
    from control_center.docker_service import _tools_of

    try:
        yaml.safe_load(task_file(task).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise Denied(f"任务 {task} 的 yaml 读不了（{task_file(task)}）：{type(e).__name__}: {str(e)[:300]}") from None
    if tool not in _tools_of(task_file(task)):
        raise Denied(f"任务 {task} 用不到 {tool}（tools 里没有，subagents 里的子代理也没列它），不能用 {tool} 的功能")
    return run


def status() -> dict[str, Any]:
    from control_center import env

    p = builder.active_plan()
    keys = sorted({k for t in ((p or {}).get("plan") or {}).get("tools") or [] for k in builder.tool_credentials(REPO / "tools" / t["name"])})   # 合入后的工具代码里读的键
    creds = {k: "已填" if env.get(k) else "还没有" for k in keys}
    return {"plan": p, "credentials": creds}


def _a(args: dict[str, Any], key: str, default: Any = ...) -> Any:
    if key in args:
        return args[key]
    if default is ...:
        raise builder.BuilderError(f"缺参数 {key}")
    return default


def _ops() -> dict[str, Callable[[dict[str, Any], str], Awaitable[Any]]]:
    """op → 处理函数（参数、run）。同步的长活（开发区命令、部署检查）放线程里，不挡别的请求。"""
    return {
        "guide": lambda a, run: asyncio.to_thread(builder.guide_text, str(_a(a, "topic"))),
        "status": lambda a, run: asyncio.to_thread(status),
        "request_credential": lambda a, run: builder.request_credential(str(_a(a, "key")), str(_a(a, "note", "")), run),
        "try_tool": lambda a, run: builder.try_tool(str(_a(a, "tool")), str(_a(a, "function", "")), _a(a, "args", None) or {}, _a(a, "steps", None),
                                                    bool(_a(a, "restart", False)), run),
        "try_subagent": lambda a, run: asyncio.to_thread(builder.stage_subagent, str(_a(a, "name"))),
        "check": lambda a, run: builder.check(str(_a(a, "tool"))),
        "write_file": lambda a, run: asyncio.to_thread(builder.write_file, str(_a(a, "path")), str(_a(a, "content"))),
        "dev_run": lambda a, run: asyncio.to_thread(builder.dev_run, _a(a, "argv"), str(_a(a, "cwd", "")), int(_a(a, "timeout", 600))),
        "verify": lambda a, run: _after_trials(run, builder.verify),
        "submit": lambda a, run: _after_trials(run, builder.submit),
        "end_trials": lambda a, run: builder.end_trials(run),
    }


async def _after_trials(run: str, fn: Callable[[], Any]) -> Any:
    """部署检查、合入前先关掉 try_tool 留着的试跑进程（它们也要装预演目录、起同一个工具），再在线程里做。"""
    await builder.end_trials(run)
    return await asyncio.to_thread(fn)


OPS = tuple(_ops())


async def handle(req: dict[str, Any], uid: int | None) -> dict[str, Any]:
    """一个请求：先查调用方（uid、run），再交给 control_center/builder；BuilderError 原文回给 agent，意外异常标平台故障。"""
    try:
        if uid not in allowed_uids():
            raise Denied("调用方不是平台用户或 sparky")
        op = req.get("op")
        fn = _ops().get(op) if isinstance(op, str) else None
        if fn is None:
            raise Denied(f"不认识的 op {op!r}（可用：{', '.join(OPS)}）")
        args = req.get("args") or {}
        if not isinstance(args, dict):
            raise Denied("args 要是 JSON 对象")
        run = check_run(req.get("run"))
        return {"ok": True, "result": await fn(args, run)}
    except (Denied, builder.BuilderError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — 只回类型与消息，traceback 在服务日志
        import traceback

        traceback.print_exc()
        return {"ok": False, "error": f"{PLATFORM_FAULT}stbcc-builder 出错：{type(e).__name__}: {str(e)[:300]}"}


def _live(run: str) -> bool:
    try:
        check_run(run)
    except Denied:
        return False
    return True


async def _prune() -> None:
    """每 PRUNE_S 秒：运行已经不在跑的（完成、失败、让出、暂停、被停），关掉它试跑时留着的工具进程（这一轮 builder 调用没走到 post_call hook 时的兜底）。"""
    while True:
        await asyncio.sleep(PRUNE_S)
        try:
            await builder.prune_trials(_live)
        except Exception:  # noqa: BLE001 — 只记服务日志，下一轮再查
            import traceback

            traceback.print_exc()


def request(op: str, run: str, args: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
    """以平台身份给本服务发一个请求（子代理 builder 的 post_call hook 用），返回服务的回复；连不上回 {"ok": false, "error": …}。"""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(socket_path()))
            s.sendall(json.dumps({"op": op, "run": run, "args": args or {}}, ensure_ascii=False).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n") and (chunk := s.recv(65536)):
                buf += chunk
        return json.loads(buf or b"{}")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"连不上 stbcc-builder（{socket_path()}）：{type(e).__name__}: {e}"}


async def serve() -> None:
    ensure_dirs()
    path = socket_path()
    path.unlink(missing_ok=True)

    async def conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(r.readline(), 30)
            resp = await handle(json.loads(line or b"{}"), _peer_uid(w.get_extra_info("socket")))
        except (asyncio.LimitOverrunError, ValueError) as e:     # 一行超过 LIMIT（readline 报 ValueError）或不是 JSON
            big = isinstance(e, asyncio.LimitOverrunError) or "limit" in str(e)
            resp = {"ok": False, "error": f"请求格式不对：{type(e).__name__}: {str(e)[:300]}" + (
                f"\n请求超过单次上限 {LIMIT // 2 ** 20} MB：builder_write_file 一次最多写 1 MB 文字，大文件分几次写（先写一段，再分段追加 / 拆成几个文件）" if big else "")}
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"{PLATFORM_FAULT}stbcc-builder 处理请求出错：{type(e).__name__}: {str(e)[:300]}"}
        try:
            w.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            await w.drain()
            w.close()
        except OSError:
            pass

    srv = await asyncio.start_unix_server(conn, str(path), limit=LIMIT)
    os.chmod(path, 0o600)
    _share(path)
    print(f"stbcc-builder 在线：{path}", flush=True)

    prune = asyncio.create_task(_prune())
    async with srv:
        try:
            await srv.serve_forever()
        finally:
            prune.cancel()


def main() -> None:
    from control_center import scheme_check

    scheme_check.require("stbcc-builder")            # 字段表锁对不上就不启动（私信 Joy）
    asyncio.run(serve())
