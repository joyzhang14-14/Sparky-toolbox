"""目录布局（规格 D1、第 12 节；2026-10-04 Joy 定新布局）：仓库 ~/sparky-toolbox/ 下 data/ 与 logs/ 不进 git（.gitignore）。
  config/  平台设置（进 git）：stbcc.yaml（端口、渠道、工作区）、models.yaml（模型档位）
  data/    运行中产生的东西，分六块：
    runtime/    平台运转要用、不清理：runner.json、queue/、steps/、control/、registered.json、schedule.json、catchup.json、listening/（在跑的常驻监听，stbcc-hub 写）、
                venvs/tools/<工具>/、venvs/tasks/<任务>/（有 requirements.txt 的工具 / 任务的依赖环境）、tool_defs/（子代理用的工具的函数清单缓存）
    sockets/    各服务的本机接口：hub.sock、builder.sock、stbcc-docker.sock；channels/<名>.sock 是渠道报到
    hub/        中枢的记录：approvals/、cards/<kind>/（卡片附件）、monitors/、builder/（计划、部署记录）、builder-trial/、inbox/、web/
    messages/   私信与任务消息的共享数据（MESSAGES）：对话库、接管状态、命令表，由任务（listen.py、hook）与渠道读写，平台不读不写
    tool_state/ 工具跨运行的状态：<工具名>/（工具自己的状态目录、已读位置）、memory/（记忆写锁、备份）、browser/（登录档案与域名清单）、owa/（令牌与研究证据）
    listen/     监听的状态：<监听名>/
    共享靠 sparkle 组（SHARED_GROUP，Joy 与 sparky 都在组里）：按文件夹（sparky_access）runtime/、hub/、sockets/ 整个组只读；messages/、listen/、tool_state/ 整个组读写
  logs/    运行记录 runs/ 与各类日志
  tasks/   任务定义，只有这一份：runner 直接读，改了即生效（stb task add 只登记，不拷贝）。
           每个任务一个文件夹 tasks/<名>/：定义是里面的 task.yaml，代码、listen.py、hook 脚本放在旁边（task_files / task_file）
任务工作区在仓库外：stbcc.yaml 的 workspace（默认 <工具用户的家目录>/workspace，Sparky 上是 /home/sparky/workspace），任务的工作目录是 <workspace>/<任务>（跨运行保留）。
STB_HOME 指向 data/、STB_CONFIG 指向 config/；tasks/、logs/ 按 STB_HOME 的上一级推算，测试把它们换成临时目录时跟着走。"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("STB_HOME", REPO / "data"))
CONFIG = Path(os.environ.get("STB_CONFIG", REPO / "config"))   # 平台设置（进 git）：stbcc.yaml、models.yaml
STBCC_FILE = CONFIG / "stbcc.yaml"
MODELS_FILE = CONFIG / "models.yaml"


def _platform_home() -> Path:
    """平台用户（仓库目录的属主，同 env.py 对 .env 属主的要求）的家目录：models/、apps/ 在这里；工具以工具用户跑，不能用 Path.home()。
    属主不在 passwd 里（builder 的测试容器）时是仓库的上一层。"""
    import pwd

    try:
        return Path(pwd.getpwuid(REPO.stat().st_uid).pw_dir)
    except KeyError:
        return REPO.parent


PLATFORM_HOME = _platform_home()


def tailscale_ip(forced: str = "tailscale") -> str:
    """只绑 Tailscale 网卡的地址：不在 tailnet 里的人连端口都摸不到。没有 Tailscale 就退回本机回环。forced：stbcc.yaml 写了具体 IP 就用它。"""
    if forced != "tailscale":
        return forced
    import subprocess
    import sys

    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        if ip and ip[0].startswith("100."):
            return ip[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    print("警告：没有 Tailscale 地址，只绑 127.0.0.1", file=sys.stderr)
    return "127.0.0.1"


def settings() -> dict:
    """config/stbcc.yaml 的内容（调用时才读，测试换了 STBCC_FILE 也跟着走）；没有这个文件是 {}（各项用默认值），写坏了抛 ValueError。"""
    import yaml

    try:
        raw = yaml.safe_load(STBCC_FILE.read_text())
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise ValueError(f"{STBCC_FILE} 不是合法的 YAML：{e}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{STBCC_FILE} 顶层要是映射")
    return raw


def _setting(key: str, default: str) -> str:
    """stbcc.yaml 顶层的一项字符串设置；没写、或文件写坏了用 default。"""
    try:
        v = settings().get(key)
    except ValueError:
        v = None
    return str(v) if v else default


TOOL_USER = _setting("tool_user", "sparky")          # 工具、监听、任务命令以它的身份跑（sudo -u）；uid、gid、家目录都按名字现查（sparky_uid 等）
SHARED_GROUP = _setting("shared_group", "sparkle")   # 平台用户与工具用户共用的组（2026-10-13 Joy 定）：两人都要碰的东西一律归它，不按用户开 ACL


def tool_home() -> Path:
    """工具用户的家目录；没有这个用户（builder 的测试容器）时按 /home/<用户名> 算。"""
    import pwd

    try:
        return Path(pwd.getpwnam(TOOL_USER).pw_dir)
    except KeyError:
        return Path("/home") / TOOL_USER


WORKSPACE_DEFAULT = tool_home() / "workspace"   # 没写 workspace 时的任务工作区：工具用户的家目录下的 workspace/


def _workspace() -> Path:
    try:
        ws = settings().get("workspace")
    except ValueError:
        ws = None
    return Path(str(ws)).expanduser() if ws else WORKSPACE_DEFAULT


TASKS = HOME.parent / "tasks"   # 任务定义（仓库 tasks/）
LOGS = HOME.parent / "logs"
RUNS = LOGS / "runs"            # runs/<task>/<ts>/
WORK = _workspace()           # 任务工作区（stbcc.yaml 的 workspace）：任务的工作目录 = <workspace>/<任务>（沙箱里外同一个路径）
DOWNLOADS = WORK / "_download"  # 统一的下载目录（Joy 2026-10-07 定）：所有任务共用，沙箱里外同一个路径；平台只管建，下什么、怎么下归工具。
                                # 名字以下划线开头：任务名不能以下划线开头，和任务工作目录 <workspace>/<任务> 永远不撞
RUNTIME = HOME / "runtime"    # 平台运转要用的状态（不清理）
SOCKETS = HOME / "sockets"    # 各服务的本机接口
HUB = HOME / "hub"            # 中枢的记录
TOOL_STATE = HOME / "tool_state"   # 工具跨运行的状态：工具自己的状态目录是 <TOOL_STATE>/<工具名>（launch.tool_state）
LISTEN = HOME / "listen"      # 监听的状态：<监听名>/
QUEUE = RUNTIME / "queue"     # 入队文件（stbcc-hub 的定时触发、监听、私信，stb run --now）
STEPS = RUNTIME / "steps"     # workflow 任务不要资源的步骤（和新触发的 workflow 运行）：stbcc-hub 取走马上跑，不进 runner 的队列（control_center/workflow.py）
CONTROL = RUNTIME / "control"   # 控制文件：runner ↔ 运行单元的 <单元>.json、stb pause 与停止（stop-<任务>）的标记、disk-low
RUNNER_STATE = RUNTIME / "runner.json"
REGISTERED = RUNTIME / "registered.json"   # stb task add 登记过的任务：{名字: {first: 首次登记时刻}}
CHANNEL_SOCKETS = SOCKETS / "channels"     # 渠道报到：<名字>.sock
MESSAGES = HOME / "messages"  # 共享数据（见模块说明）：平台只建目录（sparky 读写，sparky_access），里面的文件归任务与渠道
APPROVALS = HUB / "approvals"   # 审批卡 / 问题卡状态（11.7 P3）
INBOX = HUB / "inbox"         # 私信里要隔离读的附件（不挂进沙箱）；运行开始时拷到该次运行的 in/，只有读附件的工具读（如 attachment，只给隔离子代理用，D17）
# 私信附件（渠道不按类型丢，单个 ≤ 10 MB）按类型三分：图片（channels.IMAGE_EXT）随提示给主模型看；音视频（MEDIA_EXT）拷进任务工作目录的 in/（），
# 主模型拿到路径交能处理音视频的工具；其余任何类型（文档、邮件、网页、代码、压缩包、未知类型……）都进 INBOX（同上，主模型只拿到文件名，交能读附件的隔离子代理读；runner.files_note）
MEDIA_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
MEMORY_STATE = TOOL_STATE / "memory"   # 写记忆文档的锁 .lock 与改动前的备份 backups/（记忆文档本身在 Obsidian，memory 工具管）
OWA_HOME = TOOL_STATE / "owa"          # owa-launcher 令牌与研究证据


def listening_dir() -> Path:
    """在跑的常驻监听：<实例名>.json（stbcc-hub 的监听循环起时写、停时删，stbcc-docker 按它认「属于这个监听」的容器，control_center/listen.py）。调用时才算。"""
    return RUNTIME / "listening"


TASK_FILE = "task.yaml"         # 任务文件夹 tasks/<名>/ 里的定义


def task_name_of(p: Path) -> str:
    """定义文件 tasks/<名>/task.yaml → 任务名（文件夹名）。"""
    return p.parent.name


def task_files() -> list[Path]:
    """全部任务定义 tasks/*/task.yaml（下划线开头的文件夹不算），按任务名排序。调用时才扫，测试换了 TASKS 也跟着走。"""
    return sorted(p for p in TASKS.glob(f"*/{TASK_FILE}") if not p.parent.name.startswith("_"))


def task_file(name: str) -> Path:
    """任务名 → 定义文件 tasks/<名>/task.yaml（不存在也返回这个路径，调用方 .exists() 判断是否有这个任务）。"""
    return TASKS / name / TASK_FILE


def run_work_dir(run: Path) -> Path:
    """一次运行（运行目录 RUNS/<任务>/<运行 id>）的工作目录（沙箱启动时所在的目录）：平台统一是 <workspace>/<任务>（跨运行保留，与 runner 一致）。"""
    return WORK / run.parent.name


def ensure_dirs() -> None:
    """建平台目录，按 sparky_access 共享给 sparkle 组（share）。ensure_dirs 各入口常调：已经对的只读一次 stat 和扩展属性。"""
    access = sparky_access()
    for d in (HOME, TASKS, RUNS, WORK, QUEUE, STEPS, CONTROL, APPROVALS, INBOX, *access):
        d.mkdir(parents=True, exist_ok=True, mode=0o750)
    for d, perm in access.items():
        share(d, perm)


def sparky_access() -> dict[Path, int]:
    """sparkle 组按文件夹的权限（2026-10-10 Joy 定，文件夹里面不再细分）：平台的 runtime/、hub/、sockets/ 整个只读（5 = r-x；要做事连 socket 请服务代办，
    socket 文件本身组可读写，见 share_socket）；sparky 的 messages/、listen/、tool_state/ 整个读写（7 = rwx）；
    数据目录本身与运行记录 runs/ 只读（运行里工具自己的子目录由 launch.prepare 现开读写）。调用时才算，测试换了路径也跟着走。"""
    return {HOME: 5, RUNS: 5, RUNTIME: 5, HUB: 5, SOCKETS: 5, MESSAGES: 7, LISTEN: 7, TOOL_STATE: 7}


def child_pythonpath() -> str:
    """平台起的子进程（工具、子代理程序）的 PYTHONPATH：本仓库根在前（import 的 control_center 是本仓库的），再接上本进程已有的
    （builder 的测试容器里 venv 的可编辑安装指向宿主路径、容器里没有，靠容器给的 PYTHONPATH 补上）。"""
    return os.pathsep.join([str(REPO), *(p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p)])


def sparky_uid() -> int | None:
    """sparky 用户的 uid；没有这个用户（builder 的测试容器）时为 None，给 sparky 开权限的地方就只建目录。"""
    import pwd

    try:
        return pwd.getpwnam(TOOL_USER).pw_uid
    except KeyError:
        return None


def sparky_home() -> Path:
    """sparky 的家目录：沙箱按原路径整个挂进去（可读写，dev/、stage/ 只读，control_center/sandbox.py）；没有 sparky（builder 的测试容器）时是工作区的上一层。"""
    import pwd

    try:
        return Path(pwd.getpwnam(TOOL_USER).pw_dir)
    except KeyError:
        return WORK.parent


def sparky_user() -> str | None:
    """容器的运行身份 "uid:gid"：平台起的容器一律以 sparky 跑；没有这个用户（builder 的测试容器）时为 None。"""
    import pwd

    try:
        p = pwd.getpwnam(TOOL_USER)
    except KeyError:
        return None
    return f"{p.pw_uid}:{p.pw_gid}"


def shared_gid() -> int | None:
    """sparkle 组的 gid；没有这个组（builder 的测试容器）时为 None，共享的地方就只建目录。"""
    import grp

    try:
        return grp.getgrnam(SHARED_GROUP).gr_gid
    except KeyError:
        return None


def group_default(d: Path) -> int | None:
    """d 的默认 ACL 里组（group::）那一条的权限位（r=4 w=2 x=1）；没有默认 ACL 时为 None。
    直接读扩展属性（ensure_dirs 各入口常调，不每次起 getfacl）：4 字节版本号之后每条 8 字节（标记、权限、id），标记 4 = 属组。"""
    import struct

    try:
        raw = os.getxattr(d, "system.posix_acl_default")
    except (OSError, AttributeError):
        return None
    return next((perm for tag, perm, _ in struct.iter_unpack("<HHI", raw[4:]) if tag == 4), None)


def share(d: Path, perm: int) -> None:
    """目录 d 共享给 sparkle 组：归组、setgid（里面新建的自动归组）；perm 5 组只读，7 组读写，并加组默认可写
    （默认 ACL 只有属主、属组、其他三条，不指名用户；它不受 umask 影响，之后不管谁在里面建的都组可写）。
    不是本用户的目录（如 sparky 建的）、没有 sparkle 组时不动。"""
    import stat
    import subprocess

    gid = shared_gid()
    st = d.stat()
    if gid is None or st.st_uid != os.getuid():
        return
    if st.st_gid != gid:
        os.chown(d, -1, gid)
    mode = (stat.S_IMODE(st.st_mode) & ~0o070) | stat.S_ISGID | (0o070 if perm == 7 else 0o050)
    if stat.S_IMODE(st.st_mode) != mode:
        os.chmod(d, mode)
    if perm == 7 and group_default(d) != 7:
        try:
            subprocess.run(["setfacl", "-d", "-m", "g::rwx", str(d)], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def share_socket(path: Path) -> None:
    """服务的 socket：归 sparkle 组、660（connect 要写权限；组里的 sparky 能连，能调哪些 op 由各服务自己检查）。没有 sparkle 组时只留 600。"""
    gid = shared_gid()
    if gid is None:
        return
    os.chown(path, -1, gid)
    os.chmod(path, 0o660)


def host_tz() -> str:
    """本机时区名（IANA，如 America/Los_Angeles）：先读 /etc/timezone，没有就看 /etc/localtime 指向的 zoneinfo 文件，都没有用 UTC。
    容器的 TZ、日历与 Canvas 的时间、研究请求的"今天"、系统提示里注入的当前时间都按它（任务自己写明时区的除外）。"""
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"
