"""browser-script 工具用的浏览器部分（同文件夹的程序用；browser、browser-script、download 各有一份，只留自己用到的）：
一次性浏览器容器、登录档案、档案的域名清单、工作目录里的文件。

- 容器：owa-research 镜像的 `worker browse`（出站代理只连公网 IP + 无头 Chromium，驱动是 services/owa-research/browser/agent.mjs，一行命令、一行回复），
  以调用它的工具的名义请 stbcc-docker 代起（control_center/docker_client.session，stbcc-docker 按那个工具 tool.yaml 的 docker 校验镜像、资源）；
  关掉连接 = 请 stbcc-docker 停掉容器，所以工具进程退出时容器也跟着停。镜像与 docker 网络是本工具的事，写在这里（IMAGE / NETWORK）。
- 登录档案：data/tool_state/browser/profiles/<名>.json（Playwright storageState，660：sparky 与平台用户能读写），Joy 在 Mac 上用 sparky-browser-login 登录后传上来，
  同时传上来档案自带的域名清单 data/tool_state/browser/domains/<名>.txt（一行一个主域名，# 注释；Joy 在审批卡上同意加域名后由 browser 工具追加）。
  用哪个档案由调用时的 profile 参数定（能用的 = 有域名清单的档案）：用档案时主页面只能导航到清单里的域名（含子域，nav_domains），
  清单文件不在就拒绝并说明怎么建；不用档案 = 任意公网（没有登录状态，带不走信息）。代码里不写任何域名。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError

from control_center.tool_kit import platform_fault  # noqa: F401 — 浏览器工具从这里拿

IMAGE, NETWORK = "owa-research:latest", "owa-net"          # 浏览器容器的镜像与 docker 网络（内存、CPU 按各工具 tool.yaml 的 docker）
PROFILES = "data/tool_state/browser/profiles"              # 登录档案目录（相对仓库根，不进 git）
ERR_KEEP = 8192                                            # 浏览器容器 stderr 留最后多少字节（意外退出时附在报错里）


class BrowserError(ToolError):
    """有意报给模型的错（原因 + 改法）：ToolError 原文交给模型，不当程序错误。"""


def profiles_dir() -> Path:
    from control_center.paths import REPO

    return REPO / PROFILES


def domains_dir() -> Path:
    """登录档案的域名清单目录 data/tool_state/browser/domains/：<档案>.txt，Joy 在 Mac 上用 sparky-browser-login 登录时一起上传。"""
    from control_center.paths import TOOL_STATE

    return TOOL_STATE / "browser" / "domains"


def profile_domains(profile: str) -> list[str]:
    """登录档案 profile 的域名清单（一行一个主域名，# 后面是注释；用档案时只能去这些域名及其子域）。档案名不合法、清单不在或是空的抛 ValueError（写明怎么建）。
    浏览器（browser / browser-script 的 nav_domains）与带档案的下载（download 工具：cookie 只给这些域名）读的是同一份清单（三个工具各有一份这段代码）。"""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", profile):
        raise ValueError(f"登录档案名不合法：{profile!r}（只能是小写字母、数字和 -，字母或数字开头）")
    rel = f"data/tool_state/browser/domains/{profile}.txt"
    how = (f"请 Joy 在 Mac 上运行 sparky-browser-login {profile} <登录网址> 重新登录并上传（会一起生成域名清单 {rel}），"
           "或在 Sparky 上手动建这个文件：一行一个主域名（如 uci.edu），# 开头是注释")
    try:
        text = (domains_dir() / f"{profile}.txt").read_text()
    except FileNotFoundError:
        have = sorted(p.stem for p in domains_dir().glob("*.txt")) if domains_dir().is_dir() else []
        raise ValueError(f"登录档案 {profile} 没有域名清单（{rel}），不能用（能用的：{', '.join(have) or '无'}）。{how}") from None
    out = []
    for line in text.splitlines():
        d = line.split("#", 1)[0].strip().lower().lstrip(".")
        if d and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
            out.append(d)
    if not out:
        raise ValueError(f"登录档案 {profile} 的域名清单 {rel} 是空的，不能用。{how}")
    return out


def nav_domains(profile: str) -> list[str]:
    """profile 用哪个登录档案：不用（空）→ ["*"] 任意公网；用 → 档案的域名清单（profile_domains：含子域）。
    清单文件不在、或是空的 → 拒绝，说明怎么建。"""
    if not profile:
        return ["*"]
    try:
        return profile_domains(profile)
    except ValueError as e:
        raise BrowserError(str(e)) from None


def host_allowed(url: str, domains: list[str]) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and bool(host) and any(d == "*" or host == d or host.endswith("." + d) for d in domains)


def load_profile(profile: str) -> dict[str, Any] | None:
    if not profile:
        return None
    p = profiles_dir() / f"{profile}.json"
    if not p.is_file():                                  # 还没建：从空白开始，登录（ask_password）成功后写回
        return {"cookies": [], "origins": []}
    return json.loads(p.read_text())


def save_profile(profile: str, state: dict[str, Any]) -> None:
    """以 sparky 写：档案目录组（sparkle）可写，sparky 与平台用户（Mac 上传覆盖）都能读写；
    文件模式 660 而不是 600——600 时平台用户就读不了 sparky 写的档案。别人（other）没有权限。"""
    p = profiles_dir() / f"{profile}.json"
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)       # 第一次登录时目录还没建
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.chmod(0o660)
    tmp.replace(p)


def _safe(text: Any) -> str:
    """以 === 开头的行改掉（browser-script 的报错文字原样交给模型前用：别的工具结果分段标记只能由程序写）。"""
    return re.sub(r"(?m)^===", "= ==", str(text))


class Container:
    """一次性浏览器容器（请 stbcc-docker 代起，control_center/docker_client.session）；send 发一条命令、等一行回复。
    镜像、docker 网络取 IMAGE / NETWORK；内存、CPU、进程数、/tmp 内存盘按 tool.yaml 的 docker（stbcc-docker 按它起，这里不写）。
    根目录只读、/tmp 是内存盘：Chromium 带 --disable-dev-shm-usage，共享内存和临时用户目录都落 /tmp；HOME 指到 /tmp（容器以 sparky 的 uid 跑，镜像里 /home/owa 进不去）。
    tool：以哪个工具的名义起（browser / browser-script，stbcc-docker 按它的 tool.yaml 校验）；listen：常驻监听里起的（如紧急任务的条件监控）写监听的实例名，
    容器属于这个监听而不是一次运行（不带 STB_RUN_REF）。"""

    def __init__(self, tool: str = "browser", listen: str | None = None) -> None:
        self.tool = tool
        self.listen = listen
        self.s: Any = None

    async def __aenter__(self) -> "Container":
        from control_center import docker_client

        try:
            self.s = await docker_client.session(IMAGE, ["browse"], tool=self.tool, run=None if self.listen else os.environ.get("STB_RUN_REF") or None,
                                                 listen=self.listen, network=NETWORK, err_keep=ERR_KEEP,
                                                 env={"TZ": os.environ.get("TZ", "America/Los_Angeles"), "HOME": "/tmp"})
        except docker_client.DockerError as e:
            raise platform_fault(f"浏览器容器没起来：{e}") from None
        return self

    async def err_tail(self, n: int = 300) -> str:
        """容器 stderr 的末尾 n 字（接在退出报错后面）。"""
        t = bytes(self.s.err).decode(errors="replace").strip()[-n:]
        return f"\n容器 stderr 末尾：{t}" if t else "（容器没有 stderr 输出）"

    async def send(self, cmd: dict[str, Any], timeout: float = 90) -> dict[str, Any]:
        await self.s.write((json.dumps(cmd) + "\n").encode())
        line = await asyncio.wait_for(self.s.stdout.readline(), timeout)
        if not line:
            try:
                rc = await asyncio.wait_for(self.s.wait(), 5)
            except TimeoutError:
                rc = None
            if rc == 137:                            # 被 SIGKILL：内存上限（tool.yaml 的 docker.memory）触发的 OOM 最常见
                raise platform_fault("浏览器容器被系统强制终止（退出码 137，通常是内存超过容器上限）；页面很重时换个轻一点的页面（如移动版、打印版）或分几次看"
                                     + await self.err_tail())
            raise platform_fault(f"浏览器容器意外退出（退出码 {rc if rc is not None else '未知：5 秒内没退出'}）" + await self.err_tail())
        return json.loads(line)

    async def kill(self) -> None:
        """关掉连接：stbcc-docker 随即停掉容器。"""
        if self.s is not None:
            self.s.close()

    async def __aexit__(self, *_: Any) -> None:
        try:
            await self.send({"op": "close"}, timeout=10)
        except Exception:  # noqa: BLE001 — 关不掉就直接停
            pass
        await self.kill()
        if self.s is not None:
            try:
                await asyncio.wait_for(self.s.wait(), 15)
            except TimeoutError:
                pass


def work_file(path: str) -> Path:
    """这次任务工作目录里的文件（沙箱里外同一个路径）：只能是里面的普通文件，最大 10 MB。"""
    root = Path(os.environ.get("STB_WORK_ROOT", "")).resolve()
    rel = str(path).strip()
    if not os.environ.get("STB_WORK_ROOT") or not rel.startswith("/"):
        raise ValueError(f"path 要写工作目录下文件的绝对路径，比如 {root}/in/图片.jpg（你给的 path 是 {rel!r}）")
    f = Path(rel).resolve()
    if not f.is_relative_to(root) or f == root:
        raise ValueError(f"{rel} 不在工作目录 {root} 下（按解析 .. 和符号链接之后的位置算）；别处的文件先拷进工作目录")
    if not f.is_file():
        raise ValueError(f"工作目录里没有 {rel}（文件不存在或不是普通文件）")
    if (size := f.stat().st_size) > 10 * 1024 * 1024:
        raise ValueError(f"{rel} 有 {size / 2 ** 20:.1f} MB，超过上限 10 MB。改法：换小一点的文件（压缩图片、导出更小的版本）；"
                         "实在要传这个就在结果里写明需要 Joy 手动上传哪个文件")
    return f
