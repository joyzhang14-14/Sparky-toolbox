"""工具登记（规格第 6 节第 4 条、第 13 节 `stb tools list`）：每个工具一个文件夹 tools/<工具名>/，本模块只做发现、解析和校验。

每个工具 = 一个 MCP 服务器（宿主机进程，以 sparky 用户跑）。工具能做什么由它自己的程序与 tool.yaml 定（文件看 sparky 的文件权限，network 是 Landlock 的联网限制，
审批卡是工具的固定行为）；任务只决定用不用它，不给工具配作用域（2026-10-05 Joy 定：限制写进任务的 system_prompt，
数量、时长、时间窗这类做成函数参数由模型传）。任务里工具项的 guide 由网关处理，不传给服务器。
读外部内容（邮件、附件、网页、别人写的评论）的工具写 usable_by: subagent（yaml 里用注释写明「读外部内容」），只给 isolated 的子代理用，
原文只进子代理、主模型只拿到核对过引文的答案（control_center/subagent_run.py）。
- 平台自带的工具在 control_center/tools/<工具名>/（PLATFORM_TOOLS_DIR，写法同 tools/，builder 改不了）：和别的工具一样任务列了才有；
  都没有 server.py：BUILTIN_FUNCTIONS 里的（workspace）函数是沙箱里 pi 自带的（网关 /tools 的 builtins 告诉 pi 扩展开哪几个），
  GATEWAY_FUNCTIONS 里的（load-tool）函数由网关自己处理；guide 写进系统提示（prompts），没有第一次调用先给规范。
- 按需加载：tools/ 下 usable_by: universal 的工具与 subagents/ 下的子代理在列了 load-tool 的任务里由主模型 load_tool 加载（searchable、gateway.py _load）；
  系统提示里有它们的名字目录（prompts.catalog_text）；平台自带的与只给子代理用的不在范围里。
- 文件夹（不论谁做的都一样，2026-10-07 Joy 定）：tools/<工具名>/ 里约定的文件——tool.yaml（登记）、server.py（MCP 服务器，SERVER_FILE）、
  guide.md（用法规范，可选）、requirements.txt（Python 依赖，可选：有就用它自己的依赖环境 data/runtime/venvs/tools/<工具名>/（平台的依赖一起装），没有就用平台的 venv）、
  hook 脚本与工具自己的数据文件。下划线开头的文件夹不算工具。工具的代码只 import 平台（control_center）和自己文件夹里的模块（folder_imports 查）。
  每次 load_task、网页刷新时重扫（refresh），新加的工具不用重启就认得；某个 tool.yaml 写坏了只跳过它（tool_errors 列出原因）。
- 运行信息（RUN_ENV）：网关给每个工具进程都带上（不用声明）；模型档位归子代理（generator / engine 的程序拿到 STB_MODEL，gateway.profile_env）。
- 用法规范（按需给）：工具文件夹里的 guide.md（参数怎么填、规则、常见错误、例子）；任务 yaml 的 tools 项可再写 guide: 追加本任务的规则（网关键，
  不传给服务器）。一次会话里第一次调用这个工具时网关不执行，先返回规范（control_center/gateway.py guide_due）。
- 以谁的身份跑（control_center/launch.py）：全部工具以 sparky 用户跑（文件看 sparky 的文件权限，Landlock 只限联网：network: false 禁 TCP）。
- 谁能改：is_editable（默认 true，builder 的计划能提出修改、Joy 批；写 false 的 builder 一律不能改）。
字段（TOOL_FIELDS）：name、description、is_editable、network、docker、
usable_by（universal：任务和子代理都能列；subagent：只给子代理用，任务 yaml 写了就拒）、
hooks（调用前后、运行开始结束时平台执行的文件，control_center/hooks.py）。写了表外的键就拒（未知字段，报错指向手册的字段表）。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from control_center import hooks, scheme_check
from control_center.paths import REPO

GATEWAY_KEYS = ("guide",)
PLATFORM_CREDENTIALS = ("STB_DISCORD_TOKEN",)   # 平台服务自己用的凭证（不属于哪个工具、不塞给工具进程）：任务 yaml 同样不能用 ${} 展开，stb doctor 同样查
PLATFORM_FAULT = "[平台故障]"   # 工具报错文字里的标记（control_center/tool_kit.py platform_fault）：网关按平台故障处理，原文交给主模型
PROGRAM_ERROR = "[程序错误]"   # 工具代码里的意外异常（bug，control_center/tool_kit.py 统一加标记）：网关按平台故障处理，说明以「程序错误：」开头（program_error）
TOOL_TIMEOUT = "[超时]"        # 工具自己的时限到了（control_center/tool_kit.py tool_timeout，如解析容器的 180 秒）：网关按超时处理


def program_error(where: str, e: BaseException) -> str:
    """程序错误（意外异常）的说明：在哪里、异常类型、出错位置（仓库里最内层的一帧）。不带异常文字（可能含外来内容），
    完整 traceback 在运行目录的 gateway.stderr.log。"""
    import traceback

    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.startswith(str(REPO)) and "/.venv/" not in f.filename]
    at = f"，{Path(frames[-1].filename).relative_to(REPO)}:{frames[-1].lineno}" if frames else ""
    return f"程序错误：{where}里的 {type(e).__name__}{at}；traceback 见运行目录的 gateway.stderr.log"


# 网关给每个工具进程都带上的运行信息（环境变量 → 说明；不用声明）；stb schema tool-yaml 按它生成手册的表
RUN_ENV = {
    "STB_WORK_ROOT": "任务的工作目录 <workspace>/<任务>/（沙箱里外同一个路径，沙箱启动时就在这里）",
    "STB_RUN_ID": "本次运行 id（运行目录名）",
    "STB_RUN_REF": "<任务>/<运行 id>，审批卡上写来源、请服务代办时报身份用",
    "STB_PAUSE_FILE": "暂停信号文件，出现就尽快停手",
    "STB_RUN_IN": "本次运行的附件目录 in/（沙箱看不到它）",
    "STB_RUN_DIR": "本次运行目录（宿主上，沙箱看不到它）：放主 agent 不该读到的排查记录",
    "STB_TOOL_RUN_DIR": "本次运行这个工具的目录 <工具名>/（运行目录里，平台现建、开给 sparky 读写；工具名撞上平台自己的子目录 agent / in / out / signal / web 的不给）："
                        "放只属于这次运行、主 agent 看不到的东西（排查记录、运行结束后由工具自己的 hook 处理的东西，如读信工具的新已读位置）",
    "STB_STATE_DIR": "这个工具自己的状态目录 data/tool_state/<工具名>/（跨运行保留；没有时平台现建，整个 data/tool_state/ 对 sparky 读写）",
    "STB_MEDIA_DIR": "本次运行的 media 目录 <工作目录>/media/<运行 id>/（平台现建、开给 sparky 读写）：成品放这里",
    "STB_MODEL": "只给 generator / engine 子代理的程序（不给工具）：它的模型档位在 models.yaml 里的整段配置（JSON，另加 name = 档位名、"
                 "call_name = 调用名（端点 /models 里属于这个档位的那一条的 id；端点不答 /models 的没有），~ 开头的值展开成绝对路径）；程序自己取要的字段（endpoint、call_name、home…）",
}
PLATFORM_ENV = ("STB_ENV_FILE", "STB_HOME", "STB_CONFIG")   # 平台自己的（.env、数据目录、设置的位置）：网关照旧透传给工具与子代理的程序，hook 也带
TEST_ENV_PREFIX = "STB_TEST_"                                # 测试开关：这个前缀的环境变量一律透传给工具、子代理的程序与 hook（工具自己认，如 STB_TEST_EMAIL_DRYRUN）
MISSING_CREDENTIAL = "[缺凭证]"   # 工具报错文字里的标记（control_center/credential.py MissingCredential）：网关给 Joy 发凭证卡，告诉模型填好后再调用
CREDENTIAL_KEY = re.compile(r"[A-Z][A-Z0-9_]*")   # 凭证键名的写法


def credential_calls(text: str) -> list[str]:
    """一段工具代码里真正读凭证的键：调用 credential("KEY")（含 _need_credential 这类名字以 credential 结尾的包装）、第一个参数是字面字符串的。
    按语法树找调用，注释和文档字符串里的示例写法（如 credential("KEY")）不算；读不了语法树（写坏了）的文件没有键。stb doctor、任务 yaml 的 ${} 展开按它静态扫。"""
    import ast

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else ""
        a = n.args[0]
        if name.endswith("credential") and isinstance(a, ast.Constant) and isinstance(a.value, str) and CREDENTIAL_KEY.fullmatch(a.value):
            out.append(a.value)
    return list(dict.fromkeys(out))


def credential_keys(root: Path = REPO) -> dict[str, list[str]]:
    """工具、子代理程序与任务（tools/、subagents/、tasks/ 下的 .py，含 workflow 的步骤）里 credential("KEY") 读的键 → 用到它的文件（仓库相对路径）。
    stb doctor 查 .env 缺哪个，任务 yaml 不能 ${} 展开它们。"""
    out: dict[str, list[str]] = {}
    for f in sorted([*(root / "tools").rglob("*.py"), *(root / "subagents").rglob("*.py"), *(root / "tasks").rglob("*.py")]):
        if any(x.startswith(".") for x in f.relative_to(root).parts):   # 任务文件夹里可能有不进 git 的 .venv
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for k in credential_calls(text):
            out.setdefault(k, []).append(str(f.relative_to(root)))
    return out


FOLDER_KINDS = ("tools", "subagents", "tasks")   # 工具 / 子代理 / 任务文件夹：彼此隔离（folder_imports）


def _code_files(folder: Path) -> list[Path]:
    """文件夹里的 .py（. 开头的目录、__pycache__ 不进：任务文件夹里可能有不进 git 的 .venv）。"""
    import os

    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
        out += [Path(d) / f for f in sorted(files) if f.endswith(".py")]
    return out


def _top_modules(folder: Path) -> set[str]:
    """文件夹里能被 import 的顶层模块名：*.py 与带 __init__.py 的子目录。"""
    try:
        return {p.stem if p.suffix == ".py" else p.name for p in folder.iterdir()
                if p.suffix == ".py" or (p.is_dir() and (p / "__init__.py").is_file())}
    except OSError:
        return set()


def folder_imports(folder: Path, root: Path = REPO) -> list[str]:
    """工具 / 子代理 / 任务文件夹（tools/<名>/、subagents/<名>/、tasks/<名>/）彼此隔离：代码只 import 平台（control_center）、标准库与依赖、自己文件夹里的模块，不 import 别的工具 / 子代理 / 任务文件夹；要用别处的代码就复制进自己文件夹（只留用到的）。
    返回不合格之处（「文件:行 说明」，空 = 合格）：import 了别的文件夹的顶层模块（自己文件夹里有同名的、标准库的不算）；文件夹里有指到文件夹外的符号链接。
    测试扫全部文件夹（tests/stb_platform/test_folder_isolation.py），builder 部署前查计划动到的文件夹。"""
    import ast
    import os
    import sys

    folder = Path(folder)
    own = _top_modules(folder)
    others: dict[str, str] = {}
    for kind in FOLDER_KINDS:
        for d in sorted((root / kind).iterdir()) if (root / kind).is_dir() else []:
            if d.is_dir() and d.resolve() != folder.resolve() and not d.name.startswith((".", "_")):
                for m in _top_modules(d):
                    others.setdefault(m, f"{kind}/{d.name}")
    foreign = {m: w for m, w in others.items() if m not in own and m not in sys.stdlib_module_names}
    out = []
    for d, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__"]
        for x in [*dirs, *files]:
            p = Path(d) / x
            if p.is_symlink() and folder.resolve() not in p.resolve().parents:
                out.append(f"{p.relative_to(root)}：指到文件夹外的符号链接（{os.readlink(p)}），换成文件夹里自己的文件")
    for f in _code_files(folder):
        try:
            tree = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module] if isinstance(n, ast.ImportFrom) and n.module and not n.level else []
            for name in names:
                if (top := name.split(".")[0]) in foreign:
                    out.append(f"{f.relative_to(root)}:{n.lineno}：import {name} 是 {foreign[top]} 的模块，复制进自己文件夹（只留用到的）")  # pyright: ignore[reportAttributeAccessIssue]
    return out


# tool.yaml 的字段 → (类型, 默认, 说明)：来自字段表 control_center/schema/fields.yaml（scheme_check）；stb schema tool-yaml 按它生成手册的字段表
TOOL_FIELDS: dict[str, tuple[str, str, str]] = {k: (v["type"], v["default"], v["desc"]) for k, v in scheme_check.table("tool").items()}
TOOL_KEYS = set(TOOL_FIELDS)
YAML_FILE = "tool.yaml"                         # 工具文件夹里的约定文件：登记
SERVER_FILE = "server.py"                       # MCP 服务器
GUIDE_FILE = "guide.md"                         # 用法规范（可选）
REQUIREMENTS_FILE = "requirements.txt"          # Python 依赖（可选）：有就用它自己的依赖环境（venv_dir）
PLATFORM_TOOLS_DIR = Path("control_center") / "tools"   # 平台自带的工具（相对仓库根）
TOOLS_DIRS = ((REPO / "tools").resolve(),)               # 工具文件夹所在的目录（仓库的 tools/）：searchable 只认这里的工具
BUILTIN_FUNCTIONS: dict[str, tuple[str, ...]] = {          # 函数是 pi 自带的平台工具 → 那几个函数（在沙箱容器里以 sparky 执行，能碰什么看挂载与文件权限）
    "workspace": ("read", "write", "edit", "ls", "grep", "find", "bash"),
}
GATEWAY_FUNCTIONS: dict[str, tuple[str, ...]] = {          # 函数由网关自己处理的平台工具 → 那几个函数（不起进程；gateway.py）
    "load-tool": ("load_tool",),
}
USABLE_BY = ("universal", "subagent")           # usable_by 的取值：universal 任务和子代理都能列，subagent 只给子代理用
DOCKER_DEFAULTS: dict[str, object] = {"memory": "4g", "cpus": 2, "pids": 512, "tmp": "1g", "disk": "20g", "gpu": False}
DOCKER_MAX: dict[str, object] = {"memory": "8g", "cpus": 8, "pids": 2048, "tmp": "4g", "disk": "20g"}   # 资源上限（disk 超了要调用时发卡申请）；timeout 没有上限
DOCKER_KEYS = {"images", "memory", "cpus", "pids", "tmp", "disk", "gpu", "timeout"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    folder: Path                                # 工具文件夹 tools/<名>/（绝对路径）：服务器、guide、requirements.txt、hook 脚本都在这里
    usable_by: str = "universal"                # universal：任务和子代理都能列；subagent：只给子代理用（任务 yaml 写了就拒）
    is_editable: bool = True                    # builder 的计划能不能提出修改它（写 false 的 builder 一律不能改）
    network: bool | tuple[int, ...] = False     # false 禁 TCP；端口元组 = 禁 TCP 但放行连这些端口；true 不限
    docker: dict[str, Any] | None = None     # 容器声明（补好默认值）；启动器 stbcc-docker 按它代起
    deps: tuple[str, ...] = ()                  # requirements.txt 的 包==版本：有就用自己的依赖环境 data/runtime/venvs/tools/<名>/
    guide: str = ""                             # 用法规范 guide.md；会话里第一次调用时网关先返回它、不执行
    hooks: dict[str, tuple[Any, ...]] = field(default_factory=dict)   # 事件 → (hooks.Hook…)：调用前后、运行开始结束时平台执行（control_center/hooks.py）
    builtin: tuple[str, ...] = ()               # 函数是 pi 自带的（BUILTIN_FUNCTIONS）：没有 MCP 服务器，网关不起进程
    gateway: tuple[str, ...] = ()               # 函数由网关自己处理（GATEWAY_FUNCTIONS）：没有 MCP 服务器

    @property
    def server(self) -> Path:
        """MCP 服务器：文件夹里的 server.py。"""
        return self.folder / SERVER_FILE


def _strs(p: Path, key: str, v: object) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{p}：{key} 要是字符串列表")
    return tuple(v)


def parse_tool_yaml(p: Path, root: Path = REPO) -> ToolSpec:
    """tools/<名>/tool.yaml → ToolSpec；格式不对抛 ValueError。root 是仓库根（builder 在别的克隆里校验时给）。"""
    import yaml

    raw = yaml.safe_load(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}：顶层要是映射")
    if extra := set(raw) - TOOL_KEYS:
        raise ValueError(f"{p}：未知字段 {sorted(extra)}（可用：{', '.join(TOOL_FIELDS)}；字段表见手册 How-to-set-a-task-on-Sparky.md，或 stb schema tool-yaml）")
    name = str(raw.get("name", ""))
    if name != p.parent.name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        raise ValueError(f"{p}：name 要等于文件夹名（小写字母、数字、连字符）")
    platform = p.parent.parent.resolve() == (root / PLATFORM_TOOLS_DIR).resolve()
    builtin = BUILTIN_FUNCTIONS.get(name, ()) if platform else ()
    gateway = GATEWAY_FUNCTIONS.get(name, ()) if platform else ()
    if not builtin and not gateway and not (p.parent / SERVER_FILE).is_file():
        raise ValueError(f"{p}：文件夹里没有 {SERVER_FILE}：工具的 MCP 服务器一律写成 tools/{name}/{SERVER_FILE}")
    if bad := [k for k in ("is_editable", "network") if not isinstance(raw.get(k, False), bool) and not (k == "network" and _ports(raw[k]))]:
        raise ValueError(f"{p}：{bad} 要写 true / false" + ("（network 也可以写本机端口列表，如 [8188]）" if bad == ["network"] else ""))
    if raw.get("usable_by", "universal") not in USABLE_BY:
        raise ValueError(f"{p}：usable_by 写 universal（任务和子代理都能列）或 subagent（只给子代理用），现在是 {raw['usable_by']!r}")
    if "network" not in raw:
        raise ValueError(f"{p}：要写 network（true / false / 本机端口列表）：工具以 sparky 用户跑，联不联网要写明")
    docker = _docker(p, raw["docker"]) if "docker" in raw else None
    return ToolSpec(name=name, description=str(raw.get("description", "")), folder=p.parent.resolve(),
                    usable_by=str(raw.get("usable_by", "universal")), deps=_requirements(p.parent / REQUIREMENTS_FILE),
                    is_editable=bool(raw.get("is_editable", True)),
                    network=tuple(raw["network"]) if isinstance(raw.get("network"), list) else bool(raw["network"]), docker=docker,
                    guide=_guide(p.parent / GUIDE_FILE), hooks=hooks.parse(str(p), raw.get("hooks"), p.parent), builtin=builtin, gateway=gateway)


def _requirements(p: Path) -> tuple[str, ...]:
    """工具的 requirements.txt：一行一个 包==版本（# 开头与空行跳过）；没有这个文件 = 没有依赖（用平台的 venv）。"""
    try:
        lines = [x.split("#", 1)[0].strip() for x in p.read_text().splitlines()]
    except FileNotFoundError:
        return ()
    if bad := [x for x in lines if x and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],]*==[A-Za-z0-9.+!\-]+", x)]:
        raise ValueError(f"{p}：{bad} 要写成 包==版本（写死版本，一行一个），如 requests==2.32.3")
    return tuple(x for x in lines if x)


def _guide(p: Path) -> str:
    """工具的用法规范 guide.md：没有就是空（这个工具照常执行，不先给规范）。"""
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


def _ports(v: object) -> bool:
    """network 的端口列表：非空、每项是 1–65535 的整数。"""
    return isinstance(v, list) and bool(v) and all(isinstance(x, int) and not isinstance(x, bool) and 0 < x < 65536 for x in v)


def _size(s: object) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg])", str(s).strip().lower())
    if not m:
        raise ValueError(f"大小 {s!r} 要写成数字加 k / m / g，如 4g、512m")
    return float(m.group(1)) * {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m.group(2)]


def _docker(p: Path, v: object) -> dict[str, object]:
    """docker 声明：补默认值，超过上限（DOCKER_MAX）就拒；timeout 是预计时长，格式对就行，没有上限。"""
    if not isinstance(v, dict) or (extra := set(v) - DOCKER_KEYS):
        raise ValueError(f"{p}：docker 要写成映射，字段只能是 {', '.join(sorted(DOCKER_KEYS))}" + (f"（多了 {sorted(extra)}）" if isinstance(v, dict) else ""))  # pyright: ignore[reportPossiblyUnboundVariable]
    d = {**DOCKER_DEFAULTS, **v}
    if not isinstance(d.get("images"), list) or not d["images"] or not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?", x) for x in d["images"]):
        raise ValueError(f"{p}：docker.images 要写镜像名列表，如 [stb-video]")
    try:
        for k in ("memory", "tmp", "disk"):
            if _size(d[k]) > _size(DOCKER_MAX[k]):
                raise ValueError(f"docker.{k} 是 {d[k]}，最多 {DOCKER_MAX[k]}")
    except ValueError as e:
        raise ValueError(f"{p}：{e}") from None
    for k in ("cpus", "pids"):
        if isinstance(d[k], bool) or not isinstance(d[k], (int, float)) or not 0 < d[k] <= DOCKER_MAX[k] or (k == "pids" and not isinstance(d[k], int)):
            raise ValueError(f"{p}：docker.{k} 是 {d[k]!r}，要是大于 0、最多 {DOCKER_MAX[k]} 的数")
    if not isinstance(d["gpu"], bool):
        raise ValueError(f"{p}：docker.gpu 要写 true / false")
    if "timeout" in d:      # 格式同任务的时长（task.parse_duration；这里不 import task：仓库工具在 import registry 时就解析，会循环 import）
        t = d["timeout"]
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", t.strip()) if isinstance(t, str) else None
        if not (m and any(int(x or 0) for x in m.groups())) and not (isinstance(t, int) and not isinstance(t, bool) and t > 0):
            raise ValueError(f"{p}：docker.timeout 是按难度估的预计时长，写成 20m、2h、1h30m（没有上限）")
    return d


def _functions(path: Path) -> list[str]:
    """服务器脚本里 @server.tool 注册的函数名（name= 写了用它，没写用函数名）：静态读源码，不起进程。"""
    import ast

    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool":
                    out.append(next((str(k.value.value) for k in d.keywords if k.arg == "name" and isinstance(k.value, ast.Constant)), node.name))
    return out


def functions(spec: ToolSpec) -> list[str]:
    """工具暴露给 agent 的函数名（stb task check 查重用）；按条件注册的（如只读模式没有写函数）也列出来。"""
    return list(spec.builtin or spec.gateway) if spec.builtin or spec.gateway else _functions(spec.server) if spec.server.is_file() else []


def is_platform(spec: ToolSpec) -> bool:
    """平台自带的工具（control_center/tools/ 下）：guide 写进系统提示（prompts），没有第一次调用先给规范那一轮。"""
    return spec.folder.parent == (REPO / PLATFORM_TOOLS_DIR).resolve()


LOADER = "load-tool"   # 按需加载（平台自带，control_center/tools/load-tool/）：函数 load_tool 由网关处理（gateway.py _load_tool），能加载的名字目录写进系统提示（prompts.catalog_text）


def searchable() -> dict[str, dict[str, Any]]:
    """主模型能经 load-tool 加载的：tools/ 下 usable_by: universal 的工具与 subagents/ 下的子代理 →
    {kind: tool / subagent, description, functions, guide}。平台自带的（control_center/tools/）、只给子代理用的不在里面；写坏了的子代理跳过。"""
    from control_center import subagents

    out = {n: {"kind": "tool", "description": s.description, "functions": functions(s), "guide": s.guide} for n, s in sorted(TOOLS.items())
           if s.folder.parent in TOOLS_DIRS and s.usable_by == "universal"}
    for n in subagents.folder_names():
        try:
            d = subagents.load_def(n)
        except subagents.SubagentError:
            continue
        out.setdefault(n, {"kind": "subagent", "description": d.description, "functions": [n], "guide": d.guide})
    return out


TOOL_ERRORS: dict[str, str] = {}                # 写坏了、这次扫描跳过的工具文件夹 → 原因（stb tools list、stb doctor 显示）


def _scan(root: Path = REPO) -> tuple[dict[str, ToolSpec], dict[str, str]]:
    """tools/*/tool.yaml 与 control_center/tools/*/tool.yaml（下划线开头的文件夹不算）→ (工具名 → ToolSpec, 跳过的文件夹 → 原因)。
    一个写坏了只跳过它，不让整个平台起不来；两边同名的跳过 tools/ 那个。"""
    out: dict[str, ToolSpec] = {}
    errors: dict[str, str] = {}
    for p in sorted((root / PLATFORM_TOOLS_DIR).glob(f"*/{YAML_FILE}")) + sorted((root / "tools").glob(f"*/{YAML_FILE}")):
        if p.parent.name.startswith("_"):
            continue
        if p.parent.name in out:
            errors[str(p.parent.relative_to(root))] = f"和平台自带的工具 {PLATFORM_TOOLS_DIR / p.parent.name} 同名"
            continue
        try:
            out[p.parent.name] = parse_tool_yaml(p, root)
        except Exception as e:  # noqa: BLE001 — 一个坏的 tool.yaml 不能让整个平台起不来
            errors[str(p.parent.relative_to(root))] = str(e)
            print(f"跳过 {p}：{e}", file=sys.stderr)
    return out, errors


def refresh(root: Path = REPO) -> None:
    """重扫 root 下的 tools/*/tool.yaml，就地更新 TOOLS（各模块 import 的是同一个字典）与 TOOL_ERRORS：新加的工具、改过的说明不用重启就认得；
    这个 tools/ 里文件夹没了（或 tool.yaml 写坏了）的从表里去掉。只动扫描放进表里的那些（别处直接放进表里的不动，如测试替换的工具）。"""
    fresh, errors = _scan(root)
    dirs = ((root / "tools").resolve(), (root / PLATFORM_TOOLS_DIR).resolve())
    for n, spec in list(_SCANNED.items()):
        if n not in fresh and spec.folder.parent in dirs:
            if TOOLS.get(n) is spec:
                del TOOLS[n]
            del _SCANNED[n]
    for n, spec in fresh.items():
        if n not in TOOLS or TOOLS[n] is _SCANNED.get(n):
            TOOLS[n] = spec
        _SCANNED[n] = spec
    TOOL_ERRORS.clear()
    TOOL_ERRORS.update(errors)


reload = refresh                                # 网页服务常驻：重读全部工具（同 refresh）
TOOLS: dict[str, ToolSpec] = {}
_SCANNED: dict[str, ToolSpec] = {}              # 上一次扫描放进 TOOLS 的：refresh 只更新、去掉这些
refresh()


def venv_dir(name: str, kind: str = "tools") -> Path:
    """有 requirements.txt 的工具 / 任务自己的依赖环境：data/runtime/venvs/tools/<名>/、data/runtime/venvs/tasks/<名>/（builder 部署时建，平台依赖一起装；平台用户建，sparky 只读）。"""
    from control_center.paths import RUNTIME

    return RUNTIME / "venvs" / kind / name


def python_of(spec: ToolSpec) -> str:
    """起这个工具用的 python：有 requirements.txt 用它自己的依赖环境（venv_dir），没有用平台的 venv。"""
    return str(venv_dir(spec.name) / "bin" / "python") if spec.deps else sys.executable


def server_command(spec: ToolSpec) -> list[str]:
    return [python_of(spec), str(spec.server)]
