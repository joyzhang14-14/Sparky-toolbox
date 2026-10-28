"""photocraft 引擎子进程的客户端（tools/photocraft/ 文件夹内自足，别的文件夹不共享）。

第一次用到时起一个 `photocraft-cli mcp`（stdio JSON-RPC，一行一条消息）子进程，握在模块级变量里：
工具进程从 load_tool 起活到这次运行结束，所以同一次运行里跨工具调用文档与改动都在；
工具进程退出（atexit / SIGTERM / SIGHUP）时把子进程一起收掉，跨运行不保留、不留盘上会话状态。

- 子进程内存上限 RLIMIT_AS 8 GB（preexec_fn 里 setrlimit，Joy 定的）：超了本次调用失败并交回原因，不拖垮宿主。
- 读根 = 本次运行的工作目录（STB_WORK_ROOT）、写根 = 本次运行的 media 目录：起子进程时用
  --automation-read-root / --automation-write-root 授予，server.py 在调用前按同一规则先校验，引擎再兜一道。
- 只放行计划里的 12 个 headless 引擎工具（WHITELIST）；ui_*、control_call、doc_render_preview、doc_export、
  doc_export/import_data_sets、jobs_list/jobs_cancel 都不开（到这里也挡住）。
- 子进程崩了 / 被外部杀掉：读线程看到 EOF 就察觉；下一次调用自动重启并重试这次，结果里明说
  「会话已重建、之前打开的文档没了」（重启后文档是空的，不静默装作文档还在）。
"""
import atexit
import base64
import itertools
import json
import os
import queue
import resource
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from control_center import media
from control_center.tool_kit import platform_fault

RLIMIT_AS_BYTES = 8 << 30                 # 引擎子进程单个进程的虚拟内存上限（Joy 2026-10-20 定）
RPC_TIMEOUT_S = 1800.0                    # 一条引擎调用的上限：长滤镜（Content-Aware Fill 等）留足时间，卡死不至于永久占着
INIT_TIMEOUT_S = 60.0                     # 握手（initialize）的上限

# 计划只开这 12 个（photocraft MCP 的 headless 工具）；其余 13 个（ui_*、control_call、doc_render_preview、doc_export*、jobs_*）不开
WHITELIST = frozenset({
    "doc_open", "doc_new", "doc_place", "session_list", "doc_select", "doc_close",
    "command_list", "command_run", "command_batch", "doc_inspect", "render_preview", "doc_save",
})

# 正式位置是手册规定的项目目录（和 ComfyUI、llama.cpp 放一起）；备用的那份是还没挪过去时的原始下载位置
PRIMARY_BIN = "/home/joyzhang14/apps/photocraft/photocraft-current/bin/photocraft-cli"
FALLBACK_BIN = "/home/sparky/apps/photocraft-0.10.0-linux-aarch64/bin/photocraft-cli"
BIN_CANDIDATES = [PRIMARY_BIN, FALLBACK_BIN]

REBUILT_NOTE = ("引擎子进程上次调用时没了（崩溃、被杀或超过 8 GB 内存上限），会话已重建："
                "之前打开的文档和没保存的改动都没了，请先 doc_open 重新打开或 doc_new 新建，再继续")


class EngineGone(Exception):
    """引擎子进程死了（EOF / 管道断了 / 握手时就没起来）；带上 stderr 尾巴方便交回原因。"""

    def __init__(self, tail: str = "") -> None:
        super().__init__(tail)
        self.tail = tail


class EngineTimeout(Exception):
    """一条引擎调用超过 RPC_TIMEOUT_S 没回音：杀子进程，调用方按平台故障交回。"""


def engine_binary() -> Path:
    """photocraft-cli 的位置：先手册目录的 photocraft-current 软链，后备用下载位置。都没有 → 平台故障。"""
    for cand in BIN_CANDIDATES:
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    raise platform_fault(
        f"photocraft 没装好：{PRIMARY_BIN} 不在（备用位置 {FALLBACK_BIN} 也不在）。"
        "要 Joy 把官方 aarch64 包（photocraft-0.10.0-linux-aarch64）装到 /home/joyzhang14/apps/photocraft/；这次先用别的办法修图")


def _child_limits() -> None:
    """引擎子进程 exec 前：RLIMIT_AS 8 GB（内存超限由内核拒绝分配，进程自己失败或死掉，不拖垮宿主）。"""
    resource.setrlimit(resource.RLIMIT_AS, (RLIMIT_AS_BYTES, RLIMIT_AS_BYTES))


class _Engine:
    """一个活的 photocraft-cli mcp 子进程 + 行式 JSON-RPC。同一时刻只跑一条调用（模块级锁串行化），pending 表仍加锁保险。"""

    def __init__(self, read_root: str, write_root: str) -> None:
        argv = [str(engine_binary()), "mcp", "--automation-read-root", read_root, "--automation-write-root", write_root]
        self._id = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._dead = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=60)
        self._log = None
        run_dir = os.environ.get("STB_TOOL_RUN_DIR") or os.environ.get("STB_RUN_DIR", "")   # 排查记录写本次运行的目录，不进返回值
        if run_dir:
            try:
                Path(run_dir).mkdir(parents=True, exist_ok=True)
                self._log = open(Path(run_dir) / "photocraft-engine.log", "ab", buffering=0)
            except OSError:
                self._log = None
        try:
            self._p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, preexec_fn=_child_limits)
        except OSError as e:
            raise platform_fault(f"photocraft 引擎起不来（{argv[0]}：{e}）；这次先用别的办法修图") from None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self._call("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "stb-photocraft-tool", "version": "1.0"}}, timeout=INIT_TIMEOUT_S)
            self._notify("notifications/initialized")
            self.server_info = self._last_info
        except EngineGone:
            self.close()
            raise platform_fault("photocraft 引擎握手失败（子进程没起来或立刻退出）：" + self.stderr_tail()) from None
        except ToolError as e:
            self.close()
            raise platform_fault(f"photocraft 引擎握手被拒：{e}") from None
        except EngineTimeout as e:
            self.close()
            raise platform_fault(f"photocraft 引擎{e}，没握手成功") from None

    # ---------- 传输 ----------

    def _read_stdout(self) -> None:
        for raw in self._p.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            with self._lock:
                q = self._pending.pop(msg.get("id"), None)
            if q is not None:                       # 通知（没有对应 id）直接忽略
                q.put(msg)
        self._dead.set()
        with self._lock:
            pend = list(self._pending.values())
            self._pending.clear()
        for q in pend:
            q.put(None)                             # None = EOF，等答案的调用据此判引擎死了

    def _read_stderr(self) -> None:
        for raw in self._p.stderr:
            line = raw.decode(errors="replace").rstrip()
            self._stderr_tail.append(line)
            if self._log:
                try:
                    self._log.write(raw)
                except OSError:
                    pass

    def stderr_tail(self, n: int = 12) -> str:
        return "\n".join(list(self._stderr_tail)[-n:]) or "（stderr 没有输出）"

    def alive(self) -> bool:
        return not self._dead.is_set() and self._p.poll() is None

    def _notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n").encode())
            self._p.stdin.flush()
        except OSError:
            raise EngineGone(self.stderr_tail()) from None

    def _call(self, method: str, params: dict, timeout: float) -> dict:
        i = next(self._id)
        q: queue.Queue = queue.Queue()
        with self._lock:
            if self._dead.is_set():
                raise EngineGone(self.stderr_tail())
            self._pending[i] = q
        try:
            self._p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n").encode())
            self._p.stdin.flush()
        except OSError:
            with self._lock:
                self._pending.pop(i, None)
            raise EngineGone(self.stderr_tail()) from None
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise EngineTimeout(f"photocraft 引擎 {int(timeout)} 秒没回音（方法 {method}）") from None
        finally:
            with self._lock:
                self._pending.pop(i, None)
        if msg is None:
            rc = self._p.poll()
            raise EngineGone(f"引擎子进程退出了（退出码 {rc}）；stderr 末尾：{self.stderr_tail()}")
        if "error" in msg:                          # JSON-RPC 层报错（协议级），原文交回
            raise ToolError(f"photocraft 引擎 JSON-RPC 错误：{json.dumps(msg['error'], ensure_ascii=False)}")
        if method == "initialize":
            self._last_info = (msg.get("result") or {}).get("serverInfo") or {}
        return msg.get("result") or {}

    def tools_call(self, name: str, arguments: dict) -> dict:
        """调一次引擎工具，返回 MCP 的 result（{content:[…], isError}）；isError 抛 ToolError（原文，不吞）。"""
        res = self._call("tools/call", {"name": name, "arguments": arguments}, timeout=RPC_TIMEOUT_S)
        if res.get("isError"):
            text = "\n".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
            raise ToolError(text or "photocraft 引擎调用失败（没有文字说明）")
        return res

    def close(self) -> None:
        self._dead.set()
        if self._p.poll() is None:
            try:
                self._p.terminate()
                self._p.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._p.kill()
                    self._p.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


_engine: _Engine | None = None
_state_lock = threading.Lock()


def _roots() -> tuple[str, str]:
    """(读根, 写根) = 本次运行的工作目录、本次运行的 media 目录（media 目录顺手建出来，引擎要求根存在）。"""
    read_root = media.work_root()
    if not read_root.is_dir():
        raise platform_fault(f"本次运行的工作目录不存在：{read_root}")
    return str(read_root), str(media.media_dir())


def _ensure() -> _Engine:
    global _engine
    if _engine is not None and _engine.alive():
        return _engine
    if _engine is not None:
        _engine.close()
    _engine = _Engine(*_roots())
    return _engine


def tools_call(name: str, arguments: dict[str, Any]) -> tuple[dict, bool]:
    """调一次白名单内的引擎工具：返回 (MCP result, 这次是否重建过会话)。

    - 子进程死了（EOF）：重启引擎并重试这一次；第二次还死 → 平台故障。成功时 rebuilt=True，server.py 把它写进返回。
    - 引擎 isError / JSON-RPC 错误：ToolError 原文交回，不重试（可能是改图参数错，重启没用）。
    - 超过 RPC_TIMEOUT_S：杀子进程按平台故障交回（已经重启，会话是空的）。
    """
    if name not in WHITELIST:
        raise ToolError(f"引擎工具 {name} 没开给本工具（只开计划里的 12 个 headless 工具）")
    with _state_lock:
        rebuilt = False
        try:
            res = _ensure().tools_call(name, arguments)
        except EngineTimeout as e:
            global _engine
            _engine.close()
            _engine = _Engine(*_roots())
            raise platform_fault(f"{e}，已杀掉引擎子进程；会话已重建（之前打开的文档没了），可以先 session_list 确认再重试") from None
        except EngineGone as first:
            _engine = None
            try:
                _ensure()
            except Exception:
                raise platform_fault(f"photocraft 引擎子进程死了且重启失败。上一次的情况：{first.tail}") from None
            rebuilt = True
            try:
                res = _engine.tools_call(name, arguments)
            except EngineGone as second:
                _engine.close()
                raise platform_fault(f"photocraft 引擎重启后立刻又死了（上次情况：{first.tail}）：{second.tail}") from None
            except ToolError as e:      # 重建后这次依赖的状态没了（如引擎为这条命令撑爆内存而死）：把死因带上，别只留「没有文档」这种误导错
                raise ToolError(f"{REBUILT_NOTE}。上次情况：{first.tail}。重启后的报错：{e}") from None
        return res, rebuilt


def decode_image(res: dict) -> bytes | None:
    """tools/call 的 result 里第一个 image 块的 base64（render_preview 用）；没有就 None。"""
    for b in res.get("content") or []:
        if b.get("type") == "image" and b.get("data"):
            try:
                return base64.b64decode(b["data"])
            except (ValueError, TypeError):
                return None
    return None


def text_payload(res: dict) -> Any:
    """result 的 text 块：是 JSON 就按 photocraft 自己的 JSON 原样返回，不是就回原文（不做字段翻译）。"""
    text = "\n".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def shutdown() -> None:
    """工具进程退出时收掉引擎子进程（跨运行不保留）。"""
    global _engine
    with _state_lock:
        if _engine is not None:
            _engine.close()
            _engine = None


atexit.register(shutdown)


def _on_term(sig: int, _frame: Any) -> None:
    shutdown()
    signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)


for _sig in (signal.SIGTERM, signal.SIGHUP):          # 网关停工具进程发 SIGTERM：默认处理不跑 atexit，这里先收子进程再自杀
    if signal.getsignal(_sig) is signal.SIG_DFL:
        signal.signal(_sig, _on_term)
