"""stbcc-docker 的客户端（工具和平台服务用它起容器，不直接调 docker；服务与校验规则见 control_center/docker_service.py）。

run() 的用法同 subprocess.run(input=…, capture_output=True, timeout=…)：返回 CompletedProcess（returncode 是容器的退出码，137 = 被杀，多半内存不足），
另带 .info（{stopped: 被服务停掉的原因或 None, disk_over: {written, limit}（字节，超了磁盘额度才有）}）。到时限抛 subprocess.TimeoutExpired（连接一断，服务就停掉容器）；
服务不在、被拒抛 DockerError（OSError 的子类：工具按 OSError 接住、报"容器没起来"即可）。
磁盘额度（disk / reason）：工具的容器可写目录新写超过额度被停掉、申请超额被拒或在等 Joy 批卡时抛 ValueError（说明原样交给主模型，调用有误）；
平台的容器（不带 tool）超了只写在 .info 里。
工具调用时要带 tool（工具名）与 run（STB_RUN_REF，网关给每个工具的运行信息里就有）；常驻监听程序起的容器不带 run，带 listen（监听程序 ctx 的 listen，
session 的关键字参数照传）；平台服务（以平台用户跑）不带 tool。
session()（asyncio）是交互式的用法：容器开着，边写标准输入边读标准输出（如 browser 工具的浏览器驱动：一行命令、一行回复）；关掉会话 = 请服务停掉容器。
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from control_center.paths import SOCKETS

CHUNK = 2 ** 20 - 64
MAX_LINE = 2 ** 21                           # session 的读缓冲：服务一帧最多 64 KiB 标准输出


class DockerError(OSError):
    pass


def socket_path() -> Path:
    return SOCKETS / "stbcc-docker.sock"


def _frame(kind: str, data: bytes) -> bytes:
    return kind.encode() + struct.pack(">I", len(data)) + data


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("stbcc-docker 断开了连接")
        buf += chunk
    return buf


def _recv_frame(s: socket.socket) -> tuple[str, bytes]:
    head = _recv_exact(s, 5)
    return head[:1].decode(), _recv_exact(s, struct.unpack(">I", head[1:])[0])


def _connect(req: dict[str, Any], timeout: float | None) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(10)
        s.connect(str(socket_path()))
        s.sendall(_frame("q", json.dumps(req, ensure_ascii=False).encode()))
        s.settimeout(timeout)
        kind, data = _recv_frame(s)
    except (OSError, ConnectionError) as e:
        s.close()
        raise DockerError(f"连不上 stbcc-docker（{socket_path()}）：{type(e).__name__}: {e}") from None
    head = json.loads(data) if kind in ("h", "x") else {}
    if not head.get("ok"):
        s.close()
        if head.get("disk"):                         # 磁盘额度：发了卡在等 Joy、被拒、写法不对——交给主模型（调用有误）
            raise ValueError(str(head.get("error") or "磁盘额度申请没通过"))
        raise DockerError(f"stbcc-docker 拒绝了：{head.get('error') or '没有回复'}")
    return s


def _collect(s: socket.socket, args: list[str], timeout: float | None, deadline: float | None) -> tuple[bytes, bytes, dict[str, Any]]:
    out, err = [], []
    while True:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(args, timeout or 0)
            s.settimeout(left)
        try:
            kind, data = _recv_frame(s)
        except socket.timeout:
            raise subprocess.TimeoutExpired(args, timeout or 0) from None
        except (OSError, ConnectionError) as e:
            raise DockerError(f"stbcc-docker 中途断开：{e}") from None
        if kind == "o":
            out.append(data)
        elif kind == "e":
            err.append(data)
        elif kind == "x":
            return b"".join(out), b"".join(err), json.loads(data)


def run(image: str, argv: list[str] | tuple[str, ...] = (), *, tool: str | None = None, run: str | None = None,
        mounts: list[dict[str, str]] | tuple[dict[str, str], ...] = (), network: str = "none", memory: str | None = None,
        cpus: float | None = None, pids: int | None = None, tmp: str | None = None, tmpfs: list[dict[str, Any]] | None = None,
        gpu: bool = False, env: dict[str, str] | None = None, workdir: str | None = None, user: str | None = None, init: bool = False,
        input: bytes | str | None = None, timeout: float | None = None, text: bool = False, disk: str | None = None,
        reason: str | None = None) -> subprocess.CompletedProcess[Any]:
    """请 stbcc-docker 起一个一次性容器跑 argv，标准输入给 input，收齐标准输出 / 错误。mounts 每项 {host, container, mode: ro|rw}；
    tmpfs 每项 {path, size, exec?}（不写就是 /tmp，大小 tmp）；资源不写就用 tool.yaml 的（平台用默认值）。
    disk：这次容器的磁盘额度（如 40g，不写 = tool.yaml 的 docker.disk）；超过 tool.yaml 的要写 reason，stbcc-docker 发卡等 Joy 批。"""
    req: dict[str, Any] = {"op": "run", "image": image, "argv": list(argv), "tool": tool, "run": run, "mounts": list(mounts), "network": network,
                           "memory": memory, "cpus": cpus, "pids": pids, "tmp": tmp, "tmpfs": tmpfs, "gpu": gpu, "env": env or {},
                           "workdir": workdir, "user": user, "init": init, "disk": disk or None, "reason": reason or None}
    req = {k: v for k, v in req.items() if v is not None}
    args = [image, *argv]
    deadline = time.monotonic() + timeout if timeout is not None else None
    s = _connect(req, timeout)
    data = input.encode() if isinstance(input, str) else (input or b"")

    def feed() -> None:
        try:
            for i in range(0, len(data), CHUNK):
                s.sendall(_frame("i", data[i:i + CHUNK]))
            s.sendall(_frame("c", b""))
        except OSError:
            pass

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        out, err, end = _collect(s, args, timeout, deadline)
    finally:
        s.close()                                    # 到时限 / 出错时关连接 = 请服务停掉容器
    cp: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess(args, int(end.get("exit_code", -1)),
                                                                       out.decode(errors="replace") if text else out,
                                                                       err.decode(errors="replace") if text else err)
    cp.info = {"stopped": end.get("stopped"), "disk_over": end.get("disk_over") or {}}   # type: ignore[attr-defined]
    if tool and end.get("stopped") == "disk_over":
        over = end.get("disk_over") or {}
        raise ValueError(f"容器的可写目录写了 {over.get('written', 0) / 2 ** 30:.1f} GB，上限 {over.get('limit', 0) / 2 ** 30:g} GB，已停止。"
                         "如果确实需要更多空间，调用时用 disk 参数申请更大的额度（如 \"40g\"）并写清 reason，会发审批卡给 Joy")
    return cp


class Session:
    """session() 起的交互式容器：write 写标准输入，stdout（asyncio.StreamReader）读标准输出，err 留标准错误的最后 err_keep 字节；
    wait() 等容器结束、返回退出码（连接断了没收到结束帧是 None）；close() 关连接 = 请服务停掉容器。"""

    def __init__(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, err_keep: int) -> None:
        self._r, self._w, self._keep = r, w, err_keep
        self.stdout = asyncio.StreamReader(limit=32 * 1024 * 1024)
        self.err = bytearray()
        self.end: dict[str, Any] = {}
        self._pump = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                head = await self._r.readexactly(5)
                kind, data = head[:1].decode(), await self._r.readexactly(struct.unpack(">I", head[1:])[0])
                if kind == "o":
                    self.stdout.feed_data(data)
                elif kind == "e":
                    self.err = (self.err + data)[-self._keep:]
                elif kind == "x":
                    self.end = json.loads(data)
                    return
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.stdout.feed_eof()

    async def write(self, data: bytes) -> None:
        for i in range(0, len(data), CHUNK):
            self._w.write(_frame("i", data[i:i + CHUNK]))
        await self._w.drain()

    async def wait(self) -> int | None:
        await asyncio.shield(self._pump)
        return int(self.end["exit_code"]) if "exit_code" in self.end else None

    def close(self) -> None:
        self._w.close()


async def session(image: str, argv: list[str] | tuple[str, ...] = (), *, tool: str | None = None, run: str | None = None,
                  network: str = "none", env: dict[str, str] | None = None, err_keep: int = 8192, **res: Any) -> Session:
    """请 stbcc-docker 起一个交互式容器（参数同 run：res 里写 memory、cpus、tmp、mounts…，不写用 tool.yaml 的）。被拒抛 DockerError / ValueError（同 run）。"""
    req = {k: v for k, v in {"op": "run", "image": image, "argv": list(argv), "tool": tool, "run": run, "network": network, "env": env or {}, **res}.items()
           if v is not None}
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path()), limit=MAX_LINE), 10)
        w.write(_frame("q", json.dumps(req, ensure_ascii=False).encode()))
        await w.drain()
        head = await asyncio.wait_for(r.readexactly(5), 60)
        data = await r.readexactly(struct.unpack(">I", head[1:])[0])
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
        raise DockerError(f"连不上 stbcc-docker（{socket_path()}）：{type(e).__name__}: {e}") from None
    got = json.loads(data) if head[:1] in (b"h", b"x") else {}
    if not got.get("ok"):
        w.close()
        if got.get("disk"):
            raise ValueError(str(got.get("error") or "磁盘额度申请没通过"))
        raise DockerError(f"stbcc-docker 拒绝了：{got.get('error') or '没有回复'}")
    return Session(r, w, err_keep)


def build(tool: str, timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    """工具目录的 Dockerfile → 镜像 stb-<工具名>（只给平台用户）。"""
    s = _connect({"op": "build", "tool": tool}, timeout)
    try:
        out, err, end = _collect(s, ["build", tool], timeout, time.monotonic() + timeout if timeout is not None else None)
    finally:
        s.close()
    return subprocess.CompletedProcess(["build", tool], int(end.get("exit_code", -1)), out, err)


def containers() -> list[dict[str, Any]]:
    """在跑的容器（stb docker 列表用）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(10)
        s.connect(str(socket_path()))
        s.sendall(_frame("q", b'{"op": "list"}'))
        kind, data = _recv_frame(s)
    except (OSError, ConnectionError) as e:
        raise DockerError(f"连不上 stbcc-docker（{socket_path()}）：{e}") from None
    finally:
        s.close()
    return list(json.loads(data).get("containers") or [])
