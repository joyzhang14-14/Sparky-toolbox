"""工具统一启动方式（Sparky 工具统一身份方案）：工具一律以 sparky 用户运行，能碰哪些文件看 sparky 用户本身的文件权限
（能读整个平台仓库与 Joy 家目录的大部分，读不到 ~/.ssh、~/.claude、~/.codex 等；不能 sudo、不在 docker 组）；Landlock 只用来限制联网。
需要特权的事（发消息 / 卡 → stbcc-hub，开发部署 → stbcc-builder，起容器 → stbcc-docker，调度 → stbcc-runner）只能请服务代办，服务负责检查。

- 怎么起：网关执行 `sudo -n -E -u sparky -- <python> control_center/sparky_launch.py <名字>`，cfg 经环境变量 STB_LAUNCH 传入。
  sudo 把命令行记进 journald：命令行里不放源码与配置，只留一行（哪个工具 / 监听）；环境用 -E 整个带过去（子进程环境本来就只有平台给的键），
  不用 --preserve-env=<键>——那样 sudo 会把这些变量连值（含凭证）记进日志。启动器来自仓库（sparky 改不了）；先按 network 上 Landlock
  （false 禁 TCP；端口列表只放行连本机这些端口；true 不限），再 runpy 跑工具的服务器脚本。
- 平台固定准备的可写目录（不按工具声明，启动前现建、现开给 sparkle 组读写，见 prepare）：本次运行的 media 目录 {work}/media/<运行 id>/、
  本工具的本次运行目录 {run}/<工具名>/（STB_TOOL_RUN_DIR）；工具自己的状态目录 data/tool_state/<工具名>/ 现建（tool_state），整个 tool_state/ 组读写（paths.sparky_access）。
  别的目录能不能写看 sparky 的文件权限（如任务工作区 <workspace>/ 组读写）；要给 sparky 新开权限的由 Joy 批了再开，这里不动。
- 环境：网关给的运行信息（registry.RUN_ENV）与透传的平台变量、STB_TEST_ 测试开关（gateway.passed_env）；凭证不塞：工具用到时 credential("KEY") 自己读 .env（.env 对 sparky 只读，只有平台的 env.update 写）；
  HOME 保持平台用户的家目录，工具代码里的 ~ 展开成它（能不能碰由文件权限决定）。
- 任务的 command（command_argv）与以 sparky 跑的 hook：同一个启动器，联网限制之后 exec bash -c <命令>（Landlock 跟着 exec 与子进程走）；network 按任务 yaml（hook 按主人的 network）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from control_center import landlock  # noqa: F401 — 测试与 builder 经 launch.landlock 查内核支持
from control_center.paths import REPO, SHARED_GROUP, TOOL_STATE, TOOL_USER, child_pythonpath, group_default, share, shared_gid, sparky_uid

SPARKY = TOOL_USER         # 工具用户（stbcc.yaml 的 tool_user）
LAUNCHER = REPO / "control_center" / "sparky_launch.py"   # 启动器：按 network 上 Landlock 再跑服务器 / 命令
LAUNCH_ENV = "STB_LAUNCH"   # cfg 经这个环境变量交给启动器（不进命令行，sudo 日志里没有）
RUN_DIR_OWN = ("agent", "in", "out", "signal", "web", "steps")   # 运行目录里平台自己的子目录（steps：workflow 步骤可写的目录）：工具名撞上它们就不给开 {run}/<工具名>/


class LaunchError(ValueError):
    pass


@dataclass(frozen=True)
class RunCtx:
    """本次运行信息：run 为 None 时没有运行目录，只准备 media 目录。"""
    work: Path
    run: Path | None
    run_id: str


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """外部命令（find + chgrp / chmod / setfacl）的唯一出口：测试替换它。"""
    return subprocess.run(argv, capture_output=True, timeout=30)


def grant(d: Path) -> None:
    """可写目录：建好，共享给 sparkle 组读写（paths.share：归组、setgid、组默认可写），sparky 与平台用户（joyzhang14）谁建的另一个都能改（reply 发附件、运行器收尾）。
    第一次（目录还没有组默认可写）把里面早先建的、平台用户自己的也改成组读写；sparky 建的归 sparky，建时已是组可写（sparky 的进程 umask 002）。
    开过的不再扫：之后谁在里面建的都继承组默认可写，大目录不用每次启动都扫一遍。"""
    d.mkdir(parents=True, exist_ok=True)
    if shared_gid() is None or group_default(d) == 7:   # 没有 sparkle 组（builder 的测试容器）只建目录；开过了不再扫
        return
    me = str(os.getuid())
    for argv in (["-exec", "chgrp", SHARED_GROUP, "{}", "+"], ["-type", "d", "-exec", "chmod", "g+rwxs", "{}", "+"],
                 ["-type", "d", "-exec", "setfacl", "-d", "-m", "g::rwx", "{}", "+"], ["-type", "f", "-exec", "chmod", "g+rw", "{}", "+"]):
        r = _run(["find", str(d), "-mindepth", "1", "-user", me, *argv])
        if r.returncode != 0:
            raise LaunchError(f"给 sparkle 组开 {d} 失败：{r.stderr.decode(errors='replace')[-300:]}")
    share(d, 7)                                     # 目录本身最后开：它带上组默认可写才算开过，上面扫到一半失败的下次还会重扫


def prepare(name: str, ctx: RunCtx) -> Path:
    """平台给以 sparky 跑的工具每次运行固定准备的可写目录（现建现开，开过的不再扫），返回 media 目录：
    本次运行的 media 目录 {work}/media/<运行 id>/；有运行目录时再加本工具的本次运行目录 {run}/<工具名>/（STB_TOOL_RUN_DIR：主 agent 看不到，
    放只属于这次运行的东西，如 browser 的动作记录、读信工具的新已读位置；名字撞上 RUN_DIR_OWN 的不开）。"""
    media = ctx.work / "media" / ctx.run_id
    grant(media)
    if ctx.run is not None and name not in RUN_DIR_OWN:
        grant(ctx.run / name)
    return media


def tool_state(name: str) -> Path:
    """工具自己的状态目录 data/tool_state/<工具名>/：没有就现建。整个 tool_state/ 组读写（paths.sparky_access），新建的继承组默认可写。"""
    d = TOOL_STATE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def net_cfg(network: Any) -> dict[str, Any]:
    """network → 启动器的联网设置：true 不限；false 禁 TCP；端口列表（仓库工具）禁 TCP 但放行连本机这些端口。"""
    cfg: dict[str, Any] = {"deny_tcp": network is not True}
    if isinstance(network, (list, tuple)):
        cfg["ports"] = list(network)
    return cfg


def sparky_command(python: str, cfg: dict[str, Any], envs: dict[str, str], label: str = "") -> list[str]:
    """以 sparky 用户起一个工具服务器：cfg = {deny_tcp, ports?, code, server}（或 {deny_tcp, ports?, exec}）。
    cfg 放进 envs[STB_LAUNCH]（就地加，调用方照旧用 envs 当子进程环境，-E 原样带给 sparky，sudo 不记值）；label 只给日志看（sudo 记下的命令行里认得出是谁）。"""
    envs[LAUNCH_ENV] = json.dumps(cfg)
    return ["sudo", "-n", "-E", "-u", SPARKY, "--", python, str(LAUNCHER), label or "-"]


def command_argv(task: Any, command: str, envs: dict[str, str]) -> list[str]:
    """任务的 command：以 sparky 跑 bash -c <command>，联网按任务 yaml 的 network（不写 = 禁 TCP）。"""
    return sparky_command(sys.executable, {**net_cfg(task.network), "exec": command}, envs, f"task:{task.name}")


def server_command(server: Path, network: Any, envs: dict[str, str], label: str) -> tuple[list[str], dict[str, str]]:
    """用仓库 venv 的 python、以 sparky 起一个 MCP 服务器脚本（is_editable 不是 false 的子代理程序）。"""
    out = {**envs, "PYTHONPATH": child_pythonpath(), "TMPDIR": "/tmp"}     # 程序 import 的 control_center 是本仓库的（同 tool_command）
    if os.environ.get("STB_TEST_LAUNCH_PLAIN"):     # 测试钩子（tests/stb_platform/conftest.py）：起真工具的网关测试在临时目录里跑，sparky 进不去，照旧以当前用户起
        return [sys.executable, str(server)], out
    cfg = {**net_cfg(network), "code": str(server.parent), "server": str(server)}
    return sparky_command(sys.executable, cfg, out, label), out


def tool_command(spec: Any, envs: dict[str, str], ctx: RunCtx) -> tuple[list[str], dict[str, str]]:
    """起一个工具（不论谁做的都一样）：准备平台固定的可写目录（prepare、tool_state），带上状态目录与 media 目录（STB_STATE_DIR、STB_MEDIA_DIR），
    PYTHONPATH 是本仓库根（工具 import 的 control_center 是起它的这个仓库里的；以 sparky 起时 sudo 丢掉它，由依赖环境里指向仓库的 .pth 补上），以 sparky 起：有 requirements.txt 的用它自己的依赖环境
    （registry.venv_dir，平台用户建、sparky 只读），没有的用平台的 venv。envs 是网关给的环境（运行信息…）。"""
    from control_center import registry

    media = prepare(spec.name, ctx)
    out = {**envs, "STB_STATE_DIR": str(tool_state(spec.name)), "STB_MEDIA_DIR": str(media), "PYTHONPATH": child_pythonpath(), "TMPDIR": "/tmp"}
    python = registry.python_of(spec)
    if os.environ.get("STB_TEST_LAUNCH_PLAIN"):     # 测试钩子（tests/stb_platform/conftest.py）：起真工具的网关测试在临时目录里跑，sparky 进不去，照旧以当前用户起
        return [python if Path(python).is_file() else sys.executable, str(spec.server)], out
    if spec.deps and not Path(python).is_file():
        raise LaunchError(f"工具 {spec.name} 有 {registry.REQUIREMENTS_FILE}，它的依赖环境 {registry.venv_dir(spec.name)} 还没建（builder 部署时建）")
    cfg = {**net_cfg(spec.network), "code": str(spec.folder), "server": str(spec.server)}
    return sparky_command(python, cfg, out, spec.name), out
