"""stbcc-docker：起容器的启动器。工具（以 sparky 跑）和平台服务（如 stbcc-builder）都不直接调 docker，只经本机 socket 请它代起；
以 joyzhang14 运行（`stb docker --install` 装成 systemd 用户服务），只有它调 docker。前提（Joy）：模型不是恶意的，会手滑——这里防误操作、管资源，
不按攻击者设计。

- 本机接口 ~/sparky-toolbox/data/sockets/stbcc-docker.sock：660、归 sparkle 组（组里的 sparky 能连）。帧格式：1 字节类型 + 4 字节长度（大端）+ 内容。
  客户端 → 服务：q 请求 JSON（第一帧）、i 标准输入的一段、c 标准输入结束；服务 → 客户端：h 开头 JSON（{ok, container} 或 {ok: false, error}）、
  o 标准输出的一段、e 标准错误的一段、x 结束 JSON（{exit_code, stopped, disk_over}）。客户端断开 = 不要了：容器马上停。
  磁盘额度引起的拒绝（在等卡、被拒、disk 写法不对）h 里另带 disk: true，客户端交给主模型（调用有误）。
  客户端库在 control_center/docker_client.py。
- 谁能调（SO_PEERCRED）：平台用户（本服务自己的 uid）与 sparky。sparky 必须以某个工具的名义（tool）、带所属者：所属运行（run = <任务>/<运行 id>），
  或所属常驻监听（listen = 实例名，监听程序 ctx 的 listen；如一次性动态监听的 monitor.<id>），两个只写一个；
  平台用户不带 tool 时是平台自己的容器（builder 的开发容器），不看 tool.yaml，只按上限。
- 只给工具起受限的一次性容器（资源 ≤ tool.yaml / DOCKER_MAX），不给 workflow 的步骤起它要连的长驻服务：那种服务登记成 models.yaml 的服务档位，
  由运行器以平台身份起停（control_center/models.py）。
- op：run（起一个容器、把输入输出流转回去）、build（工具目录的 Dockerfile → 镜像 stb-<工具名>，只给平台用户）、list（在跑的容器）。
- 校验（validate）：
  · 工具的容器：工具在 registry 里、写了 docker；运行目录在、run.json 是 running、这次运行的任务 tools 里有它
    （属于常驻监听的：stbcc-hub 记着它在跑——data/runtime/listening/<实例名>.json 在、记下的进程还在——这个监听所属的任务（记录里的 tasks）之一的 tools 里有它）；镜像在 docker.images 里；
    内存 / CPU / 进程数 / 内存盘只能 ≤ tool.yaml 的值（不写就用它）；gpu 只给 docker.gpu: true 的；联网（不是 none）只给 network: true 的工具；
    挂载只许 sparky 在宿主上本来就能碰的路径（符号链接解析后，以 sparky 身份 test：ro 要能读、rw 要能读写，目录还要能进）——
    工具的容器不管谁调用一律以 sparky 跑（container_user），挂载不能越过 sparky 本来的文件权限（如挂进它进不去的目录下的子目录）。
  · builder 试跑的工具的容器（run = builder-trial/<计划>，builder.run_trial 给工具的 STB_RUN_REF）：工具还没上线、不在 registry 里，
    不看运行目录，查 stbcc-builder 试跑期间写的登记 data/hub/builder-trial/<计划>/run/<工具>/trial.json（trial_live：running、没过 until、归平台用户；
    data/hub 对 sparky 只读，sparky 伪造不了）；声明用登记里计划批过的 docker 与联网（不看预演目录的 tool.yaml），其余规则同上；不能申请超额磁盘。
  · 平台的容器：资源 ≤ registry.DOCKER_MAX（不写用 DOCKER_DEFAULTS），容器用户默认 sparky；只有部署时建依赖环境（builder._build_venv，平台拥有、sparky 只读）写平台用户。
  · 都一样：--rm、根只读（--read-only）、--cap-drop ALL、no-new-privileges、--memory-swap 同 --memory；/tmp 等内存盘总大小算进 tmp 上限；
    不许 --privileged、--cap-add、--pid=host、--network=host / container:…（请求里根本没有这些字段，网络名另查）；
    挂载不许是根目录、/home、家目录本身、/root，路径里不许有 docker.sock、.env。
- 生命周期：容器属于一次运行（run），运行结束（run.json 不再是 running：完成、失败、/stop、暂停；试跑的：登记不再是 running 或过了 until）就停掉它（每 POLL_S 秒查一次）；
  属于常驻监听的，监听停了（记录没了或记下的进程不在了）就停掉它（同样每 POLL_S 秒查一次）；客户端断开也停。时长不设上限：tool.yaml 的 docker.timeout 是预计时长，只在 list 里显示、判卡住（overdue）。
  服务重启时，上一个实例留下的容器（标签 stb.stbcc-docker = 本 socket）一律停掉（它们的客户端已经不在了）。
- 磁盘：containerd overlayfs + ext4 不支持 --storage-opt size；根只读、内存盘有上限，落盘只能写 rw 挂载的目录。额度 = tool.yaml 的 docker.disk
  （默认 20g，最多 20g；平台的容器按 DOCKER_MAX）：容器运行期间每 DISK_POLL_S 秒（目录大、扫得慢就按扫描耗时拉长间隔）算这些目录比开跑时多了多少，
  超了就停容器（stopped: disk_over，结束帧 disk_over: {written, limit}）。请求带 disk 超过额度时要带 reason：经 stbcc-hub 发通用审批卡
  （acquire_permission.PERMISSION_KIND，key 是 docker-disk:<额度>；卡面由中枢按 payload 生成）并当场拒绝（disk: true），网关就地等卡（同其他审批卡：等不到就让出运行）；Joy 批准后同一运行、同一工具、
  同样的 disk 再请求一次就按申请的额度起（用掉这张卡）；拒绝的话下一次同样的请求返回 Joy 的意见。平台的容器不能超过 DOCKER_MAX；
  属于常驻监听的容器不能申请超额（审批卡挂在运行上，监听没有运行）。
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import secrets
import signal
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from control_center import registry
from control_center.paths import HUB, REPO, RUNS, SOCKETS, TOOL_USER, ensure_dirs, listening_dir, share_socket, shared_gid, task_file

CLIENT_USERS = (TOOL_USER,)               # 除平台用户自己以外，能连这个 socket 的用户
LABEL = "stb.stbcc-docker"               # 本服务起的容器都带这个标签（值 = socket 路径），重启时据此找上一个实例留下的
POLL_S = 5                               # 多久查一次所属运行结束了没有
RECONCILE_S = 60                         # 多久按标签对一次账：停掉没人管的容器
DISK_POLL_S = 5                          # 容器运行期间多久算一次 rw 挂载目录新写了多少（扫描慢时按耗时 × DISK_SCAN_RATIO 拉长）
DISK_SCAN_RATIO = 20                     # 扫描时间最多占间隔的 1/20
MAX_FRAME = 2 ** 20                      # 一帧最多 1 MiB（请求、标准输入的一段）
BUILD_TIMEOUT_S = 3600
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?")
NET_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}")
LISTEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,60}")      # 常驻监听的实例名（监听名，或登记来的如 monitor.<id>）
TRIAL_TASK = "builder-trial"                               # builder 试跑的运行 ref：builder-trial/<计划>（builder.run_trial）
TOOL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")           # 工具名（同 builder 计划的 NAME_RE）：试跑登记的路径里要用
GUIDE = '规范：builder_guide("docker-tool")'   # 与 tool.yaml 写法有关的拒绝都指向它


class Denied(Exception):
    pass


class DiskDenied(Denied):
    """磁盘额度写法不对、超额没写 reason：拒绝帧带 disk: true，交给主模型。"""


def socket_path() -> Path:
    return SOCKETS / "stbcc-docker.sock"


def _uid(name: str) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _gid(uid: int) -> int:
    try:
        return pwd.getpwuid(uid).pw_gid
    except KeyError:
        return uid


def container_user() -> str | None:
    """容器的运行身份 "uid:gid"：sparky（CLIENT_USERS 第一个）；没有这个用户时为 None。"""
    u = _uid(CLIENT_USERS[0])
    return None if u is None else f"{u}:{_gid(u)}"


def allowed_uids() -> set[int]:
    return {os.getuid(), *(u for u in map(_uid, CLIENT_USERS) if u is not None)}


def peer_uid(sock: socket.socket | None) -> int | None:
    if sock is None:
        return None
    try:
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
    except OSError:
        return None


# ---------- 校验 ----------
@dataclass
class Plan:
    argv: list[str]
    name: str
    run: str | None
    tool: str | None
    image: str
    expected: float | None                   # 预计时长（秒），只用来显示、判卡住
    rw: list[str] = field(default_factory=list)   # rw 挂载的宿主目录：运行期间查新写了多少
    disk: int = 20 * 2 ** 30                 # 磁盘额度（字节）：rw 目录新写超过就停容器
    ask: dict[str, str] | None = None         # 申请超过额度：{disk, default, reason}，要 Joy 批卡
    listen: str | None = None                 # 属于哪个常驻监听（实例名）；属于运行的是 None


def _image(s: str) -> str:
    """镜像名统一带 tag（没写就是 :latest），比较用。"""
    return s if ":" in s.rsplit("/", 1)[-1] else s + ":latest"


def _size(v: Any, what: str) -> float:
    try:
        return registry._size(v)
    except ValueError:
        raise Denied(f"{what} 是 {v!r}：写成数字加 k / m / g，如 512m、2g") from None


def run_dir(run: str) -> Path:
    task, rid = run.split("/", 1)
    return RUNS / task / rid


def run_running(run: str, tool: Any = None) -> bool:
    """运行在跑：run.json 是 running；builder 试跑的（builder-trial/<计划>）看这个工具的试跑登记（trial_live）。"""
    if run.split("/", 1)[0] == TRIAL_TASK:
        return trial_live(run, tool) is not None
    try:
        return json.loads((run_dir(run) / "run.json").read_text()).get("status") == "running"
    except (OSError, ValueError, AttributeError):
        return False


def trial_file(run: str, tool: str) -> Path:
    """builder 试跑的登记（试跑的运行目录里，同正式运行的 run.json）：data/hub/builder-trial/<计划>/run/<工具>/trial.json。"""
    return HUB / TRIAL_TASK / run.split("/", 1)[1] / "run" / tool / "trial.json"


def trial_live(run: str, tool: Any) -> dict[str, Any] | None:
    """在跑的 builder 试跑登记 {status, tool, docker, network, until}（stbcc-builder 在 run_trial 期间写，结束改成 ended）：docker、network 是计划里 Joy 批过的声明。
    文件要归平台用户（data/hub 对 sparky 只读，sparky 伪造不了）；没有、读不了、不是 running、过了 until（builder 中途崩了也不会一直算在跑）、工具对不上返回 None。"""
    if run.split("/", 1)[0] != TRIAL_TASK or not isinstance(tool, str) or not TOOL_RE.fullmatch(tool):
        return None
    f = trial_file(run, tool)
    try:
        if f.lstat().st_uid != os.getuid():
            return None
        info = json.loads(f.read_text())
        live = info.get("status") == "running" and info.get("tool") == tool and float(info.get("until") or 0) > time.time()
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return info if live else None


def _trial_spec(tool: str, trial: dict[str, Any]) -> Any:
    """试跑中的工具：名字、容器声明（计划的 docker，按 tool.yaml 的规则补默认值、查上限）与联网（计划的 network），代替 registry 里的 ToolSpec。"""
    from types import SimpleNamespace

    if not trial.get("docker"):
        raise Denied(f"计划里的工具 {tool} 没写 docker，试跑不能起容器\n改法：要起容器得在计划的 tools 里给它写 docker 声明（Joy 批过才算）；{GUIDE}")
    try:
        docker = registry._docker(Path(f"计划里的工具 {tool}"), trial["docker"])
    except ValueError as e:
        raise Denied(str(e)) from None
    return SimpleNamespace(name=tool, docker=docker, network=trial.get("network"))


def listen_live(key: str) -> dict[str, Any] | None:
    """在跑的常驻监听的记录（stbcc-hub 写的 data/runtime/listening/<实例名>.json：{key, task, tasks, pid, started}）；
    没有、读不了、记下的进程已经不在了返回 None。"""
    try:
        info = json.loads((listening_dir() / f"{key}.json").read_text())
        pid = int(info.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return info if pid > 0 and Path(f"/proc/{pid}").exists() else None


def _run_tools(run: str) -> dict[str, dict[str, Any]]:
    """这次运行的任务（运行目录里的 task.yaml 快照）用到的工具 → 它的选项（含它的 agent 子代理的工具）。"""
    return _tools_of(run_dir(run) / "task.yaml")


def _tools_of(f: Path) -> dict[str, dict[str, Any]]:
    """任务 yaml（运行的快照，或 tasks/ 里的定义）用到的工具 → 它的选项；subagents 里列的 agent 子代理（文件夹写法）的工具也算（选项为空）：
    子代理的工具（如 isolated-reader 的 attachment 起解析容器）以那个工具的名义请容器；列了 load-tool 的，能加载的都算。"""
    from control_center import subagents

    try:
        raw = yaml.safe_load(f.read_text()) or {}
        items = raw.get("tools") or []
        subs = raw.get("subagents") or []
    except Exception:  # noqa: BLE001 — 读不了、写坏了：当它没有工具
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in items if isinstance(items, list) else []:
        if isinstance(i, str):
            out[i] = {}
        elif isinstance(i, dict) and len(i) == 1:
            k, v = next(iter(i.items()))
            out[str(k)] = dict(v) if isinstance(v, dict) else {}
    for i in subs if isinstance(subs, list) else []:
        name = i if isinstance(i, str) else next(iter(i), None) if isinstance(i, dict) and len(i) == 1 else None
        try:
            tools = subagents.load_def(str(name)).tools if name and subagents.is_folder(str(name)) else ()
        except subagents.SubagentError:
            continue
        for t in tools:
            out.setdefault(t, {})
    if registry.LOADER in out:              # 列了 load-tool：能加载的工具与能加载的 agent 子代理的工具都算本任务用到的（加载了才用得上，手册 §4.10 第 5 步）
        for n, it in registry.searchable().items():
            if it["kind"] == "tool":
                out.setdefault(n, {})
            else:
                try:
                    for t in subagents.load_def(n).tools:
                        out.setdefault(t, {})
                except subagents.SubagentError:
                    continue
    return out


def _forbidden_host(p: Path) -> str | None:
    homes = {Path("/home"), Path("/root"), Path.home(), *(Path(pw.pw_dir) for n in CLIENT_USERS if (pw := _pw(n)))}
    if p == Path("/") or p in homes:
        return "根目录、/home、家目录本身"
    if any(x in ("docker.sock", ".env") for x in p.parts) or p.name.startswith(".env"):
        return "docker.sock、.env"
    return None


def _pw(name: str) -> Any:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _container_path(s: Any, what: str) -> str:
    if not isinstance(s, str) or not s.startswith("/") or ".." in Path(s).parts or os.path.normpath(s) == "/" or ":" in s or "," in s:
        raise Denied(f"{what} 要写容器里的绝对路径（不能是 /、不能有 .. : ,）：{s!r}")
    return os.path.normpath(s)


def _caller_can(uid: int, p: Path, mode: str) -> bool:
    """uid（容器的身份，即 sparky）在宿主上能不能读（ro）/ 读写（rw）路径 p（目录还要能进）：是平台用户自己就直接 test，别人以他的身份 test（sudo -n -u）。测试替换它。"""
    flags = ["-r", *(["-w"] if mode == "rw" else []), *(["-x"] if p.is_dir() else [])]
    expr = [x for f in flags for x in ("-a", f, str(p))][1:]
    argv = ["test", *expr] if uid == os.getuid() else ["sudo", "-n", "-u", pwd.getpwuid(uid).pw_name, "--", "test", *expr]
    return subprocess.run(argv, capture_output=True, timeout=30).returncode == 0


def validate(req: dict[str, Any], uid: int | None) -> Plan:
    """一个 run 请求 → 要执行的 docker run 命令行；不合规则抛 Denied（原因写给工具 / 模型看）。"""
    if uid not in allowed_uids():
        raise Denied(f"调用方（uid {uid}）不是平台用户或 sparky")
    platform = uid == os.getuid() and not req.get("tool")
    run, listen = req.get("run"), req.get("listen")
    if run is not None and (not isinstance(run, str) or not RUN_RE.fullmatch(run)):
        raise Denied("run 要写 <任务>/<运行 id>（工具的 STB_RUN_REF）")
    if listen is not None and (not isinstance(listen, str) or not LISTEN_RE.fullmatch(listen)):
        raise Denied("listen 要写常驻监听的实例名（监听程序 ctx 的 listen）")
    if run and listen:
        raise Denied("run 与 listen 只写一个：容器属于一次运行，或属于一个常驻监听")
    if not platform and not run and not listen:
        raise Denied("工具的容器要带所属运行 run（<任务>/<运行 id>，工具环境里的 STB_RUN_REF），或所属常驻监听 listen（监听程序 ctx 的 listen）")
    trial = trial_live(run, req.get("tool")) if run else None
    if run and run.split("/", 1)[0] == TRIAL_TASK and trial is None:
        raise Denied(f"试跑 {run} 不在跑（没有工具 {req.get('tool')!r} 在跑的试跑登记，或试跑已经结束）：容器随试跑结束")
    if run and trial is None and not run_running(run):
        raise Denied(f"运行 {run} 不在跑（运行目录不在，或 run.json 不是 running）：容器随运行结束")
    live = listen_live(listen) if listen else None
    if listen and live is None:
        raise Denied(f"常驻监听 {listen} 不在跑（stbcc-hub 没有它在跑的记录，或记下的进程已经不在了）：容器随监听结束")
    image = req.get("image")
    if not isinstance(image, str) or not NAME_RE.fullmatch(image):
        raise Denied(f"image 要写镜像名，如 stb-render：{image!r}")
    spec = None
    if platform:
        decl: dict[str, Any] = dict(registry.DOCKER_DEFAULTS)
        limits: dict[str, Any] = dict(registry.DOCKER_MAX)
        gpu_ok, net_ok = True, True
    else:
        tool = req.get("tool")
        if trial is not None:                    # builder 试跑：工具还没上线，声明只认计划里批过的（不看预演目录的 tool.yaml）
            spec = _trial_spec(tool, trial)  # pyright: ignore[reportArgumentType]
        else:
            registry.refresh()
            spec = registry.TOOLS.get(tool) if isinstance(tool, str) else None
        if spec is None:
            raise Denied(f"没有工具 {tool!r}")
        if not spec.docker:
            raise Denied(f"{spec.name} 的 tool.yaml 没写 docker，不能起容器\n改法：在 tool.yaml 加 docker 段（images、memory、cpus、pids、tmp…），重新部署后再起；{GUIDE}")
        if live is not None:                     # 属于常驻监听：工具要在这个监听所属的任务之一的 tools 里（按任务现在的定义）
            owners = [str(t) for t in live.get("tasks") or [live.get("task")] if t]
            tools = next((tl for t in owners if spec.name in (tl := _tools_of(task_file(t)))), {})
            if spec.name not in tools:
                raise Denied(f"常驻监听 {listen} 所属的任务（{'、'.join(owners) or '无'}）都没有用工具 {spec.name}（任务 yaml 的 tools 里没有它），不能以它的名义起容器\n"
                             f"改法：请 Joy 把 {spec.name} 加进任务的 tools；别再以它的名义请求")
        else:
            tools = {spec.name: {}} if trial is not None else _run_tools(run)   # pyright: ignore[reportArgumentType]  # 试跑的登记本来就只属于这个工具
        if spec.name not in tools:
            raise Denied(f"运行 {run} 的任务没有用工具 {spec.name}（任务 yaml 的 tools 里没有它，subagents 里的子代理也没列它），不能以它的名义起容器\n"
                         f"改法：请 Joy 把 {spec.name} 加进任务的 tools（或任务调用的子代理的 tools，下次运行生效）；这次运行别再以它的名义请求")
        if _image(image) not in {_image(x) for x in spec.docker["images"]}:
            raise Denied(f"{spec.name} 的 tool.yaml 的 docker.images 里没有 {image}（有：{', '.join(spec.docker['images'])}）\n"
                         f"改法：image 改成上面列出的之一；确实要用新镜像就加进 tool.yaml 的 docker.images 再部署；{GUIDE}")
        decl = spec.docker
        limits = {k: spec.docker[k] for k in ("memory", "cpus", "pids", "tmp")}
        gpu_ok, net_ok = bool(spec.docker.get("gpu")), spec.network is True
    # 磁盘额度：不写用声明（平台按上限）；超过声明的要写 reason、发卡等 Joy 批（平台的不能超）
    base = str((decl if not platform else limits).get("disk") or registry.DOCKER_DEFAULTS["disk"])
    disk, ask = _size(base, "disk"), None
    if req.get("disk") is not None:
        want = req["disk"]
        try:
            disk = registry._size(want) if isinstance(want, str) else -1
        except ValueError:
            disk = -1
        if disk <= 0:
            raise DiskDenied(f"disk 写成数字加 k / m / g，如 40g：{want!r}")
        if disk > _size(base, "disk"):
            reason = req.get("reason")
            if platform:
                raise Denied(f"disk 是 {want}，最多 {base}")
            if listen:
                raise DiskDenied(f"disk {want} 超过默认额度 {base}：属于常驻监听的容器不能申请超额（审批卡挂在运行上）；不写 disk 用默认额度")
            if trial is not None:
                raise DiskDenied(f"disk {want} 超过默认额度 {base}：builder 试跑的容器不能申请超额；不写 disk 用默认额度")
            if not isinstance(reason, str) or not reason.strip():
                raise DiskDenied(f"disk {want} 超过默认额度 {base}：要同时写 reason（为什么要这么多空间），会发审批卡给 Joy")
            ask = {"disk": want, "default": base, "reason": reason.strip()[:1000]}
    # 资源：不写用声明（平台用默认值），写了只能 ≤ 上限
    res: dict[str, Any] = {}
    for k in ("memory", "tmp"):
        v = req.get(k) if req.get(k) is not None else decl[k]
        if _size(v, k) > _size(limits[k], k):
            raise Denied(f"{k} 是 {v}，最多 {limits[k]}" + ("" if platform else f"（{spec.name} 的 tool.yaml 写的）\n改法：{k} 写不超过 {limits[k]} 的值或不写（用 tool.yaml 的值）；确实要更多就改 tool.yaml 的 docker.{k} 再部署；{GUIDE}"))  # pyright: ignore[reportOptionalMemberAccess]
        res[k] = str(v)
    for k in ("cpus", "pids"):
        v = req.get(k) if req.get(k) is not None else decl[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= limits[k] or (k == "pids" and not isinstance(v, int)):
            raise Denied(f"{k} 是 {v!r}，要是大于 0、最多 {limits[k]} 的数" + ("" if platform else f"（{spec.name} 的 tool.yaml 写的）\n改法：{k} 写 0 到 {limits[k]} 之间的数{'（整数）' if k == 'pids' else ''}或不写（用 tool.yaml 的值）；确实要更多就改 tool.yaml 的 docker.{k} 再部署；{GUIDE}"))  # pyright: ignore[reportOptionalMemberAccess]
        res[k] = v
    gpu = req.get("gpu", False)
    if not isinstance(gpu, bool) or (gpu and not gpu_ok):
        raise Denied(f"gpu 是 {gpu!r}：gpu 只能是 true / false，且只给 tool.yaml 写了 docker.gpu: true 的工具"
                     + ("" if platform else f"（{spec.name} 没写）") + f"\n改法：去掉 gpu（或写 false）；确实要 GPU 就在 tool.yaml 的 docker 里写 gpu: true 再部署；{GUIDE}")  # pyright: ignore[reportOptionalMemberAccess]
    network = req.get("network", "none")
    if not isinstance(network, str) or not NET_RE.fullmatch(network) or network in ("host", "default") or network.startswith("container"):
        raise Denied(f"network 只能是 none 或一个 docker 网络名（不许 host、container:…）：{network!r}")
    if network != "none" and not net_ok:
        raise Denied(f"{spec.name if spec else ''} 不联网（tool.yaml 的 network 不是 true），容器只能 network: none（请求的是 {network}）\n"
                     f"改法：去掉 network（默认 none）；确实要联网就在 tool.yaml 写 network: true 再部署；{GUIDE}")
    # 内存盘：总大小算进 tmp 上限
    tmpfs = req.get("tmpfs")
    tmpfs = [{"path": "/tmp", "size": res["tmp"]}] if tmpfs is None else tmpfs
    if not isinstance(tmpfs, list) or not all(isinstance(t, dict) for t in tmpfs):
        raise Denied("tmpfs 要写 [{path, size, exec?}]")
    total = 0.0
    # 挂载与用户
    user = req.get("user")
    if platform:
        ok_users = {f"{u}:{_gid(u)}" for u in allowed_uids()}
        user = user or container_user() or f"{os.getuid()}:{os.getgid()}"
        if user not in ok_users:
            raise Denied(f"user 只能是 {', '.join(sorted(ok_users))}")
    else:
        if user is not None:
            raise Denied("工具的容器一律以 sparky 跑，不能指定 user")
        user = container_user() or f"{uid}:{_gid(uid)}"
    cuid, cgid = user.split(":")
    name = "stbd-" + re.sub(r"[^a-z0-9-]", "-", (run or (f"listen-{listen}" if listen else "platform")).lower())[:60].strip("-") + "-" + secrets.token_hex(3)
    argv = ["docker", "run", "--rm", "-i", "--name", name, "--label", f"{LABEL}={socket_path()}", "--network", network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", user, "--memory", res["memory"], "--memory-swap", res["memory"],
            "--cpus", str(res["cpus"]), "--pids-limit", str(res["pids"])]
    if (gid := shared_gid()) is not None:           # --user 只带主组：补上 sparkle 组，宿主上共享给组的文件容器里照样能读写
        argv += ["--group-add", str(gid)]
    seen: set[str] = set()
    for t in tmpfs:
        if set(t) - {"path", "size", "exec"}:
            raise Denied(f"tmpfs 只能写 path、size、exec：{sorted(set(t) - {'path', 'size', 'exec'})}")
        path = _container_path(t.get("path"), "tmpfs.path")
        total += _size(t.get("size"), "tmpfs.size")
        seen.add(path)
        argv += ["--tmpfs", f"{path}:rw,nosuid,{'exec' if t.get('exec') else 'noexec'},size={t.get('size')}"
                 + ("" if path == "/tmp" else f",uid={cuid},gid={cgid}")]
    if total > _size(res["tmp"], "tmp"):
        raise Denied(f"内存盘合计超过 tmp 上限 {res['tmp']}")
    rw: list[str] = []
    for m in req.get("mounts") or []:
        if not isinstance(m, dict) or set(m) - {"host", "container", "mode"} or m.get("mode", "ro") not in ("ro", "rw"):
            raise Denied("mounts 每项写 {host, container, mode: ro|rw}")
        host = m.get("host")
        if not isinstance(host, str) or not host.startswith("/") or ".." in Path(host).parts:
            raise Denied(f"mounts 的 host 要写宿主上的绝对路径（不能有 ..）：{host!r}")
        real = Path(host).resolve()
        if not real.exists():
            raise Denied(f"挂载的宿主路径不存在：{host}")
        for p in (Path(os.path.normpath(host)), real):
            if why := _forbidden_host(p):
                raise Denied(f"不能挂 {why}：{host}")
        mode = m.get("mode", "ro")
        if spec is not None and not _caller_can(int(user.split(":")[0]), real, mode):
            raise Denied(f"sparky 在宿主上{'不能读写' if mode == 'rw' else '读不到'} {host}，不能挂（容器以 sparky 跑，挂载不能越过它本来的文件权限）\n"
                         f"改法：只挂 sparky 本来就能{'写' if mode == 'rw' else '读'}的目录（如本次运行的 media 目录、工作目录、data/tool_state/<工具名>/）；"
                         f"确实要用别的目录请 Joy 给 sparky 开权限；{GUIDE}")
        cpath = _container_path(m.get("container"), "mounts.container")
        if cpath in seen:
            raise Denied(f"容器路径 {cpath} 挂了两次")
        seen.add(cpath)
        argv += ["-v", f"{real}:{cpath}:{mode}"]
        if mode == "rw":
            rw.append(str(real))
    if gpu:
        argv += ["--gpus", "all"]
    if req.get("init"):
        argv.append("--init")
    if (wd := req.get("workdir")) is not None:
        argv += ["-w", _container_path(wd, "workdir")]
    env = req.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and ENV_RE.fullmatch(k) and isinstance(v, str) and "\0" not in v for k, v in env.items()):
        raise Denied("env 要写 {变量名: 字符串}")
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    cmd = req.get("argv") or []
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
        raise Denied("argv 要是字符串列表")
    argv += [image, *cmd]
    expected = None
    if spec is not None and spec.docker.get("timeout"):  # pyright: ignore[reportOptionalMemberAccess]
        from control_center.task import parse_duration

        expected = float(parse_duration(spec.docker["timeout"]))  # pyright: ignore[reportOptionalSubscript]
    return Plan(argv=argv, name=name, run=run, tool=spec.name if spec else None, image=image, expected=expected, rw=rw, disk=int(disk), ask=ask, listen=listen)


def dir_bytes(paths: list[str]) -> int:
    """这些目录里文件的合计大小（os.scandir 递归，不跟符号链接；读不了的跳过）。"""
    total, stack = 0, list(paths)
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _disk_card(run: str, tool: str, key: str) -> dict[str, Any] | None:
    """这次运行里同一工具、同一申请（key）、还没用掉的磁盘额度审批卡（通用审批卡，最新的一张）。"""
    from control_center import acquire_permission, cards

    found = []
    for p in cards.APPROVALS.glob("*.json"):
        c = cards.load(p.stem) or {}
        pl = c.get("payload") or {}
        if c.get("kind") == acquire_permission.PERMISSION_KIND and not pl.get("used") and (pl.get("run"), pl.get("tool"), pl.get("key")) == (run, tool, key):
            found.append(c)
    return max(found, key=lambda c: c["created"]) if found else None


# ---------- 帧 ----------
async def read_frame(r: asyncio.StreamReader) -> tuple[str, bytes] | None:
    """读一帧；连接断了返回 None。"""
    try:
        head = await r.readexactly(5)
        n = struct.unpack(">I", head[1:])[0]
        if n > MAX_FRAME:
            return None
        return head[:1].decode(), await r.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None


def frame(kind: str, data: bytes) -> bytes:
    return kind.encode() + struct.pack(">I", len(data)) + data


# ---------- 服务 ----------
@dataclass
class Live:
    plan: Plan
    started: float
    proc: Any = None
    stopped: str | None = None
    disk_over: dict[str, int] = field(default_factory=dict)   # 超了磁盘额度：{written, limit}


class Service:
    def __init__(self) -> None:
        self.live: dict[str, Live] = {}

    async def _docker(self, *argv: str, timeout: float = 60) -> int:
        p = await asyncio.create_subprocess_exec("docker", *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.wait_for(p.wait(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return -1

    async def stop(self, name: str, why: str) -> None:
        """按名字停本服务起的一个容器（--rm：停了就删）。"""
        if (lv := self.live.get(name)) is not None and lv.stopped is None:
            lv.stopped = why
            print(f"停容器 {name}（{why}）", flush=True)
            await self._docker("kill", name)

    async def _labeled(self) -> list[str]:
        """带本 socket 标签的容器名（docker ps -a）。"""
        p = await asyncio.create_subprocess_exec("docker", "ps", "-a", "--filter", f"label={LABEL}={socket_path()}", "--format", "{{.Names}}",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        return out.decode().split()

    async def sweep_orphans(self) -> None:
        """带本 socket 标签、却不在 self.live 里的容器没人管了，停掉：上一个实例留下的（启动时），
        或停的时候容器还没建好（docker kill 落空、只杀了 docker 客户端）、之后才建好的（watch_runs 每 RECONCILE_S 秒对一次账）。"""
        for name in await self._labeled():
            if name in self.live:
                continue
            print(f"停没人管的容器 {name}", flush=True)
            await self._docker("kill", name)
            await self._docker("rm", "-f", name)

    async def watch_runs(self) -> None:
        """所属运行结束了、所属常驻监听停了就停容器；定期对账没人管的容器。出错只记日志，循环不停（同 stbcc-hub 的 sweep_loop）。"""
        last = time.monotonic()                 # 启动时 serve 刚对过一次
        while True:
            try:
                for name, lv in list(self.live.items()):
                    if lv.plan.run and lv.stopped is None and not run_running(lv.plan.run, lv.plan.tool):
                        await self.stop(name, "run_ended")
                    elif lv.plan.listen and lv.stopped is None and listen_live(lv.plan.listen) is None:
                        await self.stop(name, "listen_ended")
                if time.monotonic() - last >= RECONCILE_S:
                    last = time.monotonic()
                    await self.sweep_orphans()
            except Exception as e:  # noqa: BLE001
                print(f"watch_runs 出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            await asyncio.sleep(POLL_S)

    def listing(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"container": n, "run": lv.plan.run, "listen": lv.plan.listen, "tool": lv.plan.tool or "platform", "image": lv.plan.image, "seconds": round(now - lv.started),
                 "expected": lv.plan.expected, "overdue": bool(lv.plan.expected and now - lv.started > lv.plan.expected), "stopping": lv.stopped}
                for n, lv in self.live.items()]

    async def conn(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        uid = peer_uid(w.get_extra_info("socket"))
        lock = asyncio.Lock()

        async def send(kind: str, data: bytes) -> None:
            async with lock:
                w.write(frame(kind, data))
                await w.drain()

        try:
            first = await asyncio.wait_for(read_frame(r), 30)
            req = json.loads(first[1]) if first and first[0] == "q" else None
            if not isinstance(req, dict):
                await send("h", json.dumps({"ok": False, "error": "第一帧要是请求（q）"}).encode())
                return
            op = req.get("op")
            if op == "run":
                await self.op_run(req, uid, r, send)
            elif op == "build":
                await self.op_build(req, uid, send)
            elif op == "list" and uid in allowed_uids():
                await send("x", json.dumps({"ok": True, "containers": self.listing()}, ensure_ascii=False).encode())
            else:
                await send("h", json.dumps({"ok": False, "error": f"不认识的 op {op!r}（run / build / list）"}).encode())
        except (ConnectionError, asyncio.TimeoutError):
            pass                                 # 客户端断开 / 30 秒没发请求：没人收回复
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            try:
                await send("h", json.dumps({"ok": False, "error": f"{registry.PLATFORM_FAULT}stbcc-docker 出错：{type(e).__name__}: {str(e)[:300]}"}).encode())
            except (ConnectionError, OSError):
                pass
        finally:
            try:
                w.close()
            except OSError:
                pass

    async def op_run(self, req: dict[str, Any], uid: int | None, r: asyncio.StreamReader, send: Any) -> None:
        try:
            plan = validate(req, uid)
            if plan.ask is not None:
                await self.disk_approval(plan)
        except Denied as e:
            await send("h", json.dumps({"ok": False, "error": str(e), "disk": isinstance(e, DiskDenied)}, ensure_ascii=False).encode())
            return
        base = await asyncio.to_thread(dir_bytes, plan.rw) if plan.rw else 0
        proc = await asyncio.create_subprocess_exec(*plan.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdin and proc.stdout and proc.stderr           # 三个都是 PIPE，一定有
        stdin = proc.stdin
        lv = self.live[plan.name] = Live(plan, time.time(), proc)
        print(f"起容器 {plan.name}：{plan.tool or 'platform'} {plan.image} " + (f"listen={plan.listen}" if plan.listen else f"run={plan.run}"), flush=True)
        try:
            await send("h", json.dumps({"ok": True, "container": plan.name}).encode())

            async def pump_out(stream: asyncio.StreamReader, kind: str) -> None:
                while chunk := await stream.read(65536):
                    try:
                        await send(kind, chunk)
                    except (ConnectionError, OSError):
                        pass                     # 客户端走了：继续读完，免得容器写满管道卡住；停容器由 pump_in 管

            async def pump_in() -> None:
                stdin_open = True
                while True:
                    f = await read_frame(r)
                    if f is None:                # 客户端断开（超时、被取消、进程没了）：不要了
                        await self.stop(plan.name, "client_gone")
                        return
                    if f[0] == "i" and stdin_open:
                        stdin.write(f[1])
                        try:
                            await stdin.drain()
                        except (ConnectionError, OSError):
                            stdin_open = False
                    elif f[0] == "c" and stdin_open:
                        stdin.close()
                        stdin_open = False

            outs = [asyncio.create_task(pump_out(proc.stdout, "o")), asyncio.create_task(pump_out(proc.stderr, "e"))]
            inp = asyncio.create_task(pump_in())
            disk = asyncio.create_task(self.watch_disk(lv, base)) if plan.rw else None
            rc = await proc.wait()
            await asyncio.gather(*outs)
            inp.cancel()
            if disk is not None:
                disk.cancel()
            try:
                await send("x", json.dumps({"exit_code": rc, "stopped": lv.stopped, "disk_over": lv.disk_over}, ensure_ascii=False).encode())
            except (ConnectionError, OSError):
                pass
        finally:
            if proc.returncode is None:
                await self.stop(plan.name, "service_error")
                proc.kill()
            self.live.pop(plan.name, None)
            print(f"容器 {plan.name} 结束：退出码 {proc.returncode}" + (f"（{lv.stopped}）" if lv.stopped else ""), flush=True)

    async def watch_disk(self, lv: Live, base: int) -> None:
        """容器运行期间：rw 挂载目录比开跑时（base）多写了多少，超过额度就停容器（disk_over）。扫描在线程里做，扫得慢就隔久一点。"""
        wait = DISK_POLL_S
        while lv.stopped is None:
            await asyncio.sleep(wait)
            t0 = time.monotonic()
            written = await asyncio.to_thread(dir_bytes, lv.plan.rw) - base
            wait = max(DISK_POLL_S, (time.monotonic() - t0) * DISK_SCAN_RATIO)
            if written > lv.plan.disk:
                lv.disk_over = {"written": written, "limit": lv.plan.disk}
                print(f"容器 {lv.plan.name} 的可写目录新写 {written / 2 ** 30:.1f} GB，超过额度 {lv.plan.disk / 2 ** 30:g} GB", flush=True)
                await self.stop(lv.plan.name, "disk_over")
                return

    async def disk_approval(self, plan: Plan) -> None:
        """申请超过额度（plan.ask）：Joy 批过同样的申请就用掉那张卡放行；拒绝过就返回他的意见；还在等就说在等；都没有就发卡。放行以外都抛 DiskDenied。"""
        from control_center import acquire_permission, cards, hub_client

        assert plan.ask is not None and plan.run and plan.tool
        want = plan.ask["disk"]
        key = f"docker-disk:{want}"                        # 通用审批卡认「同一件事」的指纹：同一运行、同一工具、同样的额度
        card = await asyncio.to_thread(_disk_card, plan.run, plan.tool, key)
        if card is not None and card.get("resolved"):
            card["payload"]["used"] = time.time()
            cards.save(card)
            if card["resolved"].get("action") == "approve":
                return
            raise DiskDenied(f"磁盘额度 {want} 的申请没批：{card['resolved'].get('result') or '（Joy 没写原因）'}\n"
                             f"改法：不带 disk 按默认额度 {plan.ask['default']} 跑；或把活分批做、每批少写一些；或减少写到可写挂载目录的量（中间文件写 /tmp 内存盘、用完就删）；别再用同样的 disk 重复申请")
        if card is not None:
            raise DiskDenied(f"磁盘额度 {want} 的审批卡已经发过、Joy 还没处理；批了之后用同样的 disk 再调用")
        r, why = await hub_client.call_detail("card", kind=acquire_permission.PERMISSION_KIND, title="", body="", buttons=[],
                                              payload={"run": plan.run, "tool": plan.tool, "key": key,
                                                       "action": f"申请容器磁盘额度 {want}（默认 {plan.ask['default']}），即容器运行期间可写目录最多新写这么多",
                                                       "detail": f"理由（模型写的）：{plan.ask['reason']}\n运行：{plan.run}"})
        if not (r or {}).get("id"):
            raise DiskDenied(f"磁盘额度 {want} 的审批卡没发出去（stbcc-hub {why or '回复里没有卡片 id：' + json.dumps(r, ensure_ascii=False)[:300]}），容器没起；"
                             f"可以稍后再试，或不带 disk 用默认额度 {plan.ask['default']}")
        raise DiskDenied(f"申请磁盘额度 {want}（默认 {plan.ask['default']}）已发审批卡给 Joy，容器没起；Joy 批准后用同样的 disk 再调用一次")

    async def op_build(self, req: dict[str, Any], uid: int | None, send: Any) -> None:
        """工具目录的 Dockerfile → 镜像 stb-<工具名>（要写在它的 docker.images 里）。只给平台用户（builder 部署、Joy 手动）。"""
        try:
            if uid != os.getuid():
                raise Denied("只有平台用户能构建镜像")
            registry.refresh()
            spec = registry.TOOLS.get(req.get("tool")) if isinstance(req.get("tool"), str) else None  # pyright: ignore[reportArgumentType]
            if spec is None or not spec.docker:
                raise Denied(f"没有写了 docker 的工具 {req.get('tool')!r}")
            tag = f"stb-{spec.name}:latest"
            if tag not in {_image(x) for x in spec.docker["images"]}:
                raise Denied(f"{spec.name} 的 docker.images 里要有 stb-{spec.name}（现在是：{', '.join(spec.docker['images'])}）\n改法：在 tool.yaml 的 docker.images 加 stb-{spec.name}；{GUIDE}")
            d = spec.folder
            if not (d / "Dockerfile").is_file():
                raise Denied(f"{d} 里没有 Dockerfile，构建不了 stb-{spec.name}\n改法：在工具目录写 Dockerfile；只用现成镜像（docker.images 里别的名字）就不用构建；{GUIDE}")
        except Denied as e:
            await send("h", json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode())
            return
        await send("h", json.dumps({"ok": True, "image": tag}).encode())
        proc = await asyncio.create_subprocess_exec("docker", "build", "-t", tag, "-f", str(d / "Dockerfile"), str(d),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout                                         # stdout=PIPE，一定有
        out = proc.stdout
        try:
            async def pump() -> None:
                while chunk := await out.read(65536):
                    await send("o", chunk)
            await asyncio.wait_for(pump(), BUILD_TIMEOUT_S)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            rc = await proc.wait()
        await send("x", json.dumps({"exit_code": rc}).encode())


def _share(path: Path) -> None:
    """socket 归 sparkle 组、660（connect 要写权限；CLIENT_USERS 都在组里）；测试替换它。"""
    share_socket(path)


async def serve() -> None:
    ensure_dirs()
    path = socket_path()
    path.unlink(missing_ok=True)
    svc = Service()
    await svc.sweep_orphans()
    srv = await asyncio.start_unix_server(svc.conn, str(path), limit=MAX_FRAME + 16)
    os.chmod(path, 0o600)
    _share(path)
    watcher = asyncio.create_task(svc.watch_runs())
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)   # systemctl stop / restart：先停掉在跑的容器再退
    print(f"stbcc-docker 在线：{path}", flush=True)
    async with srv:
        await stop.wait()
    watcher.cancel()
    for name in list(svc.live):
        await svc.stop(name, "service_stopped")


def main() -> None:
    from control_center import scheme_check

    scheme_check.require("stbcc-docker")             # 字段表锁对不上就不启动（私信 Joy）
    asyncio.run(serve())
