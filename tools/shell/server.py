"""shell 工具：在 Sparky 本机以 Linux 用户 sparky 执行命令。每条命令先在 sandlock 里试跑（trial：写入只落在写时复制副本上、跑完丢弃，
不联网，不能给外面的进程发信号，连真实存在的本机服务（Unix socket）、用 GPU 算东西都会被拦下并记下，内存与进程数有上限，最多 TRY_S 秒）：
试跑没有任何副作用 = 只读，不发卡，在只读环境里真跑一次（execute(readonly=True)：
landlock.restrict_readonly，看得到真实的进程和端口），输出就是工具结果；否则给 Joy 发审批卡，卡上列试跑看到的效果，批准才跑。
试跑在新起的子进程里跑（trial_isolated）：平台启动器把本进程设成不可 dump（PR_SET_DUMPABLE 0，护住环境里的凭证），sandlock 在这样的进程里起不来；
exec 出来的子进程又可 dump，只带 ENV 这几个环境变量。沙箱没跑起来、超时没跑完、子进程出错，都按有副作用发卡，卡上写原因。
- 执行：本进程由平台以 sparky 用户起（control_center/launch.py，同别的工具），命令直接 timeout … env -i -C <cwd> <最小环境> bash -c <命令>。完整 bash 语法（管道、重定向、变量都行）；
  环境变量只给 ENV 这几个（PATH、HOME=/home/sparky、LANG…），不带任何凭证；sparky 用户对 joyzhang14 的家目录只读、凭证目录挡住，.env 只读（只有平台的 env.update 写）。
  stdout 与 stderr 合在一起，截断到 MAX_OUT 字符（留头留尾），前面带退出码；超过单条上限整组进程被杀，退出码 124。
- 审批：平台的通用审批卡（tool_kit.ask_permission：卡面文字是命令的说明、试跑效果和命令全文），就地等 tool_kit.APPROVAL_WAIT_S（运行要暂停就提前停）；批准 → 执行，输出就是工具结果；
  「拒绝，你应该…」→ 不执行，把 Joy 写的话交回模型；没处理 → 不执行，网关随后让出运行（waiting）。Joy 之后批准，平台以同样的参数
  替这次调用再调用一次、执行（tool_kit.ask_permission；本进程不写 data/hub/approvals），续跑时结果交给模型。等卡的时间不算命令上限，也不算主模型时间（网关扣掉）。
工作目录写死 /home/sparky；单条命令的时限是函数参数 timeout（秒数或 20m、1h30m、90s，默认 10m，不设上限；等审批的时间不算）。
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from mcp.server.mcpserver import MCPServer

from control_center import landlock, tool_kit
from control_center.task import parse_duration

CWD = "/home/sparky"
TIMEOUT = parse_duration("10m")                                 # 单条命令默认的时限：调用时的 timeout 不写就用它
MAX_DESC = 80
RUN_AS = "sparky"
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/home/sparky", "USER": RUN_AS,
       "LOGNAME": RUN_AS, "SHELL": "/bin/bash", "LANG": "C.UTF-8"}
MAX_OUT = 30_000
SHOWN = 700                                      # 卡上命令最多显示多少字（通用审批卡的细节上限是 acquire_permission.MAX_DETAIL，前面还有试跑效果）
TRY_S = 120                                      # 试跑最多跑多久：到点杀掉，按没跑完发卡
SIGNAL_CALLS = ("kill", "tkill", "tgkill", "pidfd_send_signal")
SHOW_ITEMS = 6                                   # 卡上每类效果最多列几条
DNS = "udp://127.0.0.53:53"                      # 试跑放行的只有本机 DNS（systemd-resolved）：解析出地址，才看得到它想连哪里
PSEUDO = ("dev/", "proc/", "sys/")              # 这些下面的「改动」是设备与内核接口，不是文件（/dev/shm 除外，那是真的存储）
MAX_MEM, MAX_PROCS = "4G", 512                   # 试跑与只读执行的内存、进程数上限：主模型在线时整机常只剩十几 GB，吃内存的命令要 Joy 批
MAX_MEM_BYTES = 4 << 30                          # 同 MAX_MEM：只读执行里单个进程的 RLIMIT_DATA
LIMIT_HIT = re.compile(r"\bKilled\b|fork: .*(Resource temporarily unavailable|retry)|[Cc]annot fork")   # 试跑碰到上限时 bash 报的话
GPU_COMPUTE = "/dev/nvidia-uvm"                  # CUDA 算东西要打开它（nvidia-smi 也会打开，拿不到照样能查状态）：试跑里打开算副作用，只读执行里不给写

server = MCPServer("shell", instructions="在 Sparky 上以 sparky 用户执行命令：只读的直接执行，有副作用的先经 Joy 审批")


def argv(command: str, limit: int, env: dict[str, str] | None = None) -> list[str]:
    """本进程已是 sparky：timeout 管单条上限（超时杀整个进程组）；env -i 清空环境，只给 ENV（env 再加几项）。"""
    return ["timeout", "-k", "10", str(limit),
            "env", "-i", "-C", CWD, *(f"{k}={v}" for k, v in {**ENV, **(env or {})}.items()), "bash", "-c", command]


def devices() -> list[str]:
    """/dev 下的字符设备（只读环境里照常能开，如 /dev/null、GPU 的 /dev/nvidia*；能不能用看文件权限）。"""
    out = []
    for root, dirs, files in os.walk("/dev"):
        dirs[:] = [d for d in dirs if d not in ("shm", "mqueue")]
        for f in files:
            p = os.path.join(root, f)
            try:
                if stat.S_ISCHR(os.stat(p).st_mode):
                    out.append(p)
            except OSError:
                pass
    return out


def execute(command: str, limit: int | None = None, readonly: bool = False) -> tuple[int, str]:
    """readonly：只读环境（landlock.restrict_readonly：只能写设备（GPU_COMPUTE 除外，CUDA 跑不起来）和这次的一次性 TMPDIR，
    禁 TCP，不能给外面的进程发信号；单个进程内存 MAX_MEM_BYTES、新起的进程 MAX_PROCS 个，见 _readonly_child）。"""
    limit = limit or TIMEOUT
    t0 = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="stb-shell-ro-") if readonly else None
    try:
        if tmp:
            writable = [tmp, *(d for d in devices() if not os.path.realpath(d).startswith(GPU_COMPUTE))]   # /dev/char/ 下的软链也指着它
            nproc = MAX_PROCS + _procs_of(os.getuid())
            p = subprocess.Popen(argv(command, limit, {"TMPDIR": tmp}), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 preexec_fn=lambda: _readonly_child(writable, nproc))
        else:
            p = subprocess.Popen(argv(command, limit), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = p.communicate(timeout=limit + 30)        # timeout 那边到点就杀；这里只兜底它本身卡住
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return 124, f"超时（{limit} 秒），已终止；命令本身就慢可以给更大的 timeout"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    text = out.decode(errors="replace")
    if p.returncode in (124, 137) and time.monotonic() - t0 >= limit - 1:
        text += ("\n" if text else "") + f"…[超时（{limit} 秒），已终止；命令本身就慢可以给更大的 timeout，或拆成几条短命令]"
    return p.returncode, text


def _procs_of(uid: int) -> int:
    """这个用户现在有几个进程（RLIMIT_NPROC 按用户算，上限 = 现有的 + MAX_PROCS）。"""
    n = 0
    for d in os.listdir("/proc"):
        try:
            n += d.isdigit() and os.stat(f"/proc/{d}").st_uid == uid
        except OSError:
            pass
    return n


def _readonly_child(writable: list[str], nproc: int) -> None:
    """只读执行的子进程在 exec 前：单进程内存（RLIMIT_DATA）与进程数（RLIMIT_NPROC）上限——试跑没认出超上限时的兜底，再上 Landlock。"""
    resource.setrlimit(resource.RLIMIT_DATA, (MAX_MEM_BYTES, MAX_MEM_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
    landlock.restrict_readonly(writable)


def limit_of(timeout: int | str) -> int:
    """调用时给的 timeout（秒数或 90s / 5m 这种写法；0 或空 = 默认 TIMEOUT）→ 实际秒数，不设上限。"""
    if timeout in (0, "", "0", None):
        return TIMEOUT
    try:
        sec = int(timeout) if str(timeout).strip().isdigit() else parse_duration(timeout)
    except Exception:  # noqa: BLE001
        raise ValueError(f"timeout 写秒数或 90s、5m、1h 这种写法：{timeout}") from None
    return max(1, int(sec))


def clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    head, tail = MAX_OUT * 2 // 3, MAX_OUT // 3
    return (f"{text[:head]}\n…[截断：共 {len(text)} 字符，中间省略；要看全部就把输出重定向到 /home/sparky 下的文件（> 文件 2>&1），"
            f"再用 sed -n / head / tail / grep 分段看]\n{text[-tail:]}")


class Trial:
    """试跑看到的：done 跑完了没有（没到 TRY_S）、rc 退出码、changes 文件改动（sandlock Change，不含试跑自己的 TMPDIR）、
    net 想连的地址、unix 想连的本机服务（Unix socket 路径）、signals 想给外面的进程发的信号（信号名, PID, 命令行）、gpu 想用 GPU 算东西、limit 碰到了内存或进程数上限、
    progs 跑了的程序、error 试跑本身没跑起来的原因。"""

    def __init__(self) -> None:
        self.done, self.rc, self.error = False, 0, ""
        self.gpu = self.limit = False
        self.changes: list = []
        self.net: list[str] = []
        self.unix: list[str] = []
        self.signals: list[tuple[str, str, str]] = []
        self.progs: list[str] = []
        self.listen: set[str] = set()                 # net 里想监听（bind）的

    @property
    def readonly(self) -> bool:
        return (self.done and not self.error and not self.changes and not self.net and not self.unix and not self.signals and not self.gpu
                and not self.limit)


def _confinement(pid: int) -> tuple[str, ...] | None:
    """进程的 NoNewPrivs、Seccomp、Seccomp_filters（试跑里的进程都带 sandlock 的过滤器，外面的一般没有）；进程不在了是 None。"""
    try:
        st = dict(line.split(":", 1) for line in open(f"/proc/{pid}/status") if line.startswith(("NoNewPrivs", "Seccomp")))
    except OSError:
        return None
    return tuple(st.get(k, "").strip() for k in ("NoNewPrivs", "Seccomp", "Seccomp_filters"))


def _outside(pid: int, caller: int) -> bool:
    """信号目标是不是试跑以外的进程：和发信号的进程带着同样的 sandlock 过滤器就是里面的；目标不在了（信号也发不出去）不算。"""
    target = _confinement(pid)
    return target is not None and target != _confinement(caller)


def _real_change(c) -> bool:
    """试跑副本里的这处改动真跑时会不会发生：设备与内核接口下的不算；sparky 对它所在目录没有写权限的不算（副本不看文件权限）。"""
    if c.path.startswith(PSEUDO) and not c.path.startswith("dev/shm/"):
        return False
    d = os.path.dirname("/" + c.path)
    while not os.path.exists(d):
        d = os.path.dirname(d)
    return os.access(d, os.W_OK)


def _cmdline(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()[:100]
    except OSError:
        return "?"


def _unix_target(pid: int) -> str | None:
    """停在 connect(fd, addr, len) 上的进程要连的 Unix socket 路径（抽象 socket 写成 @名字）；不是 Unix socket 或读不到是 None。
    sandlock 的事件里没有这个地址：从 /proc/<pid>/syscall 取参数，再从它的内存读 sockaddr_un（试跑的进程是本进程的后代，读得到）。"""
    try:
        f = open(f"/proc/{pid}/syscall").read().split()
        addr, ln = int(f[2], 16), int(f[3], 16)
        with open(f"/proc/{pid}/mem", "rb") as m:
            m.seek(addr)
            raw = m.read(min(ln, 110))
    except (OSError, ValueError, IndexError):
        return None
    if len(raw) < 3 or struct.unpack("=H", raw[:2])[0] != 1:             # AF_UNIX
        return None
    if raw[2] == 0:
        return "@" + raw[3:].split(b"\0")[0].decode(errors="replace")
    return raw[2:].split(b"\0")[0].decode(errors="replace")


def _comm(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return ""


def _sig_name(n: int) -> str:
    try:
        return "0（探测在不在）" if n == 0 else signal.Signals(n).name
    except ValueError:
        return str(n)


def trial(command: str) -> Trial:
    """在 sandlock 里试跑一次：写入落在写时复制副本上、跑完丢弃（COW 根是 /）；不联网（想连的记下来）；发信号的系统调用都记下，
    给外面进程的由 Landlock 挡住；只放行本机 DNS；连 Unix socket 按路径看（副本模式下 sandlock 不拦、事件里也没有地址，_unix_target 自己读）：
    连真实存在的本机服务（平台的 hub、stbcc-docker、builder 等）记下并拒绝：sandlock 先替它连上再报事件，对方会收到一个空连接，但命令拿到的是 EACCES，一个字节也发不过去
    （平台的三个服务收到空请求都直接回错、什么也不做）；不存在的（ls 查用户名先试的 nscd）照常失败、不算；
    打开 GPU_COMPUTE 记下并拦下；
    内存、进程数上限 MAX_MEM、MAX_PROCS（sandlock 超内存只悄悄杀掉那个进程，不报；按 bash 报的 Killed、退出码 137、fork 失败记 limit）；TMPDIR 指向这次的一次性目录（缓存之类不算改动）；
    最多 TRY_S 秒，结束时它起的进程全杀。"""
    from sandlock import BranchAction, Sandbox
    from sandlock.handler import Handler, NotifAction

    t, lock = Trial(), threading.Lock()
    tmp = tempfile.mkdtemp(prefix="stb-shell-try-")

    def on_event(e, ctx):
        if e.syscall == "connect" and not e.host and (p := _unix_target(e.pid)) and (p.startswith("@") or os.path.exists(p)):
            with lock:
                if p not in t.unix:
                    t.unix.append(p)
            return errno.EACCES
        with lock:
            if e.syscall == "execve" and e.argv:
                p = " ".join(e.argv)[:100]
                if p not in t.progs:
                    t.progs.append(p)
            elif getattr(e, "category", "") == "network" and e.denied and e.host and e.port:      # 本机 socket 文件（没有地址）、端口 0 的不算联网
                where = f"{e.host}:{e.port}"
                if e.syscall == "bind":
                    t.listen.add(where)
                if where not in t.net:
                    t.net.append(where)
        return 0

    class Sig(Handler):
        def __init__(self, name: str) -> None:
            self.name = name

        def handle(self, ctx):
            a = [ctypes.c_int32(x & 0xFFFFFFFF).value for x in ctx.args[:3]]
            pid, sig = (a[0], a[2]) if self.name == "tgkill" else (None, a[1]) if self.name == "pidfd_send_signal" else (a[0], a[1])
            if pid is None or pid == -1 or (pid != 0 and _outside(abs(pid), ctx.pid)):
                with lock:
                    t.signals.append((_sig_name(sig), "全部进程" if pid == -1 else "?" if pid is None else str(pid),
                                      "" if pid is None or pid == -1 else _cmdline(abs(pid))))
            return NotifAction.continue_()

    class Gpu(Handler):
        def handle(self, ctx):
            p = ctx.read_path(1)                     # openat(dirfd, pathname, …)：只用来记下，挡靠返回 EACCES
            if p and p.startswith(GPU_COMPUTE):
                t.gpu = t.gpu or _comm(ctx.pid) != "nvidia-smi"   # nvidia-smi 也会打开它，拿不到照样能查状态，不算用 GPU 算东西
                return NotifAction.errno(errno.EACCES)
            return NotifAction.continue_()

    try:
        sb = Sandbox(workdir="/", fs_writable=["/"], fs_readable=["/"], cwd=CWD, clean_env=True, env={**ENV, "TMPDIR": tmp},
                     net_allow=[DNS], max_memory=MAX_MEM, max_processes=MAX_PROCS,
                     on_exit=BranchAction.ABORT, on_error=BranchAction.ABORT, policy_fn=on_event)
        t0 = time.monotonic()
        r = sb.run_with_handlers(["timeout", "-k", "3", str(TRY_S), "bash", "-c", command],
                                 [*((n, Sig(n)) for n in SIGNAL_CALLS), ("openat", Gpu())])
        t.done, t.rc = time.monotonic() - t0 < TRY_S - 0.5, r.exit_code
        if r.error:                                   # 沙箱本身没跑起来（命令一条都没执行）：不能当成没有副作用
            t.error = f"沙箱没跑起来：{r.error}"
        t.limit = t.rc in (137, -9) or bool(LIMIT_HIT.search(r.stderr.decode(errors="replace")))
        skip = tmp.lstrip("/")
        t.changes = [c for c in r.changes if c.path != skip and not c.path.startswith(skip + "/") and _real_change(c)]
        t.progs = [p for p in t.progs if not p.startswith("timeout -k 3 ") and p != f"bash -c {command}"[:100]]
    except Exception as e:  # noqa: BLE001 — 试跑起不来就按有副作用发卡，卡上写原因
        t.error = f"{type(e).__name__}: {e}"[:200]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return t


def trial_isolated(command: str) -> Trial:
    """在新起的子进程（本文件 --trial，命令从 stdin 进）里试跑，结果按 JSON 交回；子进程出错、超时记成 error。"""
    env = {**ENV, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}      # 仓库根：子进程要 import control_center
    r = None
    try:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--trial"], input=command.encode(), capture_output=True,
                           env=env, timeout=TRY_S + 30)
        d = json.loads(r.stdout.decode().strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001 — 子进程起不来、超时、输出不对，都按试跑没跑起来发卡
        tail = r.stderr.decode(errors="replace").strip()[-200:] if r else ""
        t = Trial()
        t.error = f"试跑进程出错：{type(e).__name__}: {e}"[:200] + (f"；{tail}" if tail else "")
        return t
    t = Trial()
    for k, v in d.items():
        setattr(t, k, v)
    t.changes = [SimpleNamespace(path=c, kind=k, after=SimpleNamespace(size=n) if n is not None else None) for c, k, n in d["changes"]]
    t.signals, t.listen = [tuple(x) for x in d["signals"]], set(d["listen"])
    return t


def _dump(t: Trial) -> str:
    d = dict(vars(t))
    d["changes"] = [[c.path, c.kind, c.after.size if c.after else None] for c in t.changes]
    d["listen"] = sorted(t.listen)
    return json.dumps(d, ensure_ascii=False)


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MB"


def effects_text(t: Trial) -> str:
    """试跑效果写成卡上的几行：文件改动按目录归并，联网、发信号、跑了的程序各列前 SHOW_ITEMS 条。"""
    if t.error:
        return f"试跑没跑起来（{t.error}），看不到它会做什么"
    lines = ["试跑（写入只落在副本上、跑完丢弃；不联网）：" + (f"跑完，退出码 {t.rc}" if t.done else f"跑了 {TRY_S} 秒没跑完，之后的效果看不到")]
    if t.changes:
        n = {k: sum(1 for c in t.changes if c.kind == k) for k in "AMD"}
        lines.append(f"会改 {len(t.changes)} 处文件（新增 {n['A']}、修改 {n['M']}、删除 {n['D']}）：")
        groups: dict[str, list] = {}
        for c in t.changes:
            groups.setdefault(os.path.dirname("/" + c.path), []).append(c)
        word = {"A": "新增", "M": "修改", "D": "删除"}
        for d, cs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:SHOW_ITEMS]:
            size = _size(sum((c.after.size if c.after else 0) for c in cs))
            if len(cs) == 1:
                lines.append(f"· {word[cs[0].kind]} /{cs[0].path}（{size}）")
            else:
                lines.append(f"· {d}/ 下 {len(cs)} 个（" + "、".join(f"{word[k]} {m}" for k in "AMD" if (m := sum(1 for c in cs if c.kind == k))) + f"，{size}）")
        if len(groups) > SHOW_ITEMS:
            lines.append(f"· 另有 {len(groups) - SHOW_ITEMS} 个目录")
    if t.net:
        lines.append("想联网（试跑时拦下了，批准后会真的连）：" + "、".join(w + ("（监听）" if w in t.listen else "") for w in t.net[:SHOW_ITEMS])
                     + ("…" if len(t.net) > SHOW_ITEMS else ""))
    if t.unix:
        lines.append("想连本机服务（Unix socket，试跑时拦下了，批准后会真的连）：" + "、".join(t.unix[:SHOW_ITEMS]) + ("…" if len(t.unix) > SHOW_ITEMS else ""))
    if t.gpu:
        lines.append("想用 GPU 算东西（试跑时拦下了，批准后会真的用 GPU、占内存）")
    if t.limit:
        lines.append(f"碰到了试跑的上限（内存 {MAX_MEM}、进程 {MAX_PROCS} 个），被停下（批准后执行不设这些上限）")
    for name, pid, cmd in t.signals[:SHOW_ITEMS]:
        lines.append(f"想给外面的进程发信号：{name} → {pid}" + (f"（{cmd}）" if cmd else ""))
    if t.progs:
        lines.append("跑了的程序：" + "；".join(t.progs[:SHOW_ITEMS]) + ("…" if len(t.progs) > SHOW_ITEMS else ""))
    return "\n".join(lines)


def card_text(command: str, description: str, effects: str = "") -> tuple[str, str, str]:
    """一条命令 → 通用审批卡的 (key, action, detail)：key 认「同一条命令」（cwd + 命令全文）；detail 先列试跑效果，再列命令（只显示前 SHOWN 字）。"""
    shown = command if len(command) <= SHOWN else command[:SHOWN] + "\n…（太长，已截断）"
    detail = (f"{effects}\n" if effects else "") + f"在 {CWD} 以 sparky 用户执行：\n```\n{shown.replace('```', '`​``')}\n```"
    return hashlib.sha256(f"{CWD}\0{command}".encode()).hexdigest(), f"执行命令：{description}", detail


async def ask_approval(command: str, description: str = "", effects: str = "") -> bool | str | None:
    """发通用审批卡并就地等 Joy 点（tool_kit.ask_permission）；批准 True，没处理 None（卡还在等），
    「拒绝，你应该…」返回 Joy 写的话（str，没写是 ""），卡发不出去 False。
    同一次运行里 Joy 之前已经批准过同一条命令（同 cwd）的卡（挂起后续跑）：经中枢 card_use 用掉，直接放行一次，不再发卡。"""
    key, action, detail = card_text(command, description, effects)
    got, value = await tool_kit.ask_permission("shell", action, key, detail)
    return True if got == "approve" else None if got == "pending" else False if got == "failed" else str(value or "")


@server.tool(description=(
    "在 Sparky 本机以 sparky 身份执行一条 bash 命令（能联网、能写 Joy 家目录里开给 sparky 的地方、能看本机进程和 GPU）。"
    "有副作用的要 Joy 批准。读文件、列目录、看大小这类沙箱里能做的，用 read / ls / grep / bash（沙箱断网；Joy 的家目录在沙箱里只读可见）"))
async def shell(command: str, description: str, timeout: int | str = 0) -> str:
    if not command.strip():
        raise ValueError("command 不能为空")
    description = " ".join(description.split())
    if not description:
        raise ValueError("description 不能为空：用一句话说清这条命令在做什么")
    if len(description) > MAX_DESC:
        raise ValueError(f"description 不超过 {MAX_DESC} 字：{description[:40]}…")
    limit = limit_of(timeout)
    effects = ""
    if not (tool_kit.CALL.get() or {}).get("stb_approved"):       # Joy 已同意、平台替这次调用重跑：不再试跑
        t = await asyncio.to_thread(trial_isolated, command)
        if t.readonly:
            rc, text = await asyncio.to_thread(execute, command, limit, True)
            return f"exit {rc}（只读，没发卡）\n{clip(text)}"
        effects = effects_text(t)
    ok = await ask_approval(command, description, effects)
    if ok is True:
        rc, text = await asyncio.to_thread(execute, command, limit)
        return f"exit {rc}\n{clip(text)}"
    if ok is None:
        return "Joy 还没处理这条命令的审批卡（命令没执行）"
    if ok is False:
        return "审批卡没发出去（stbcc-hub 没有响应），命令没执行；可以稍后再试"
    return f"Joy 拒绝了，他说：「{ok}」，照他说的改" if ok else "Joy 拒绝了这条命令（没写原因），命令没执行"


if __name__ == "__main__":
    if sys.argv[1:] == ["--trial"]:
        print(_dump(trial(sys.stdin.read())))
    else:
        server.run("stdio")
