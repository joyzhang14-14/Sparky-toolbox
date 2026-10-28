"""download 工具用的浏览器部分（同文件夹的程序用；browser、browser-script、download 各有一份，只留自己用到的）：
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
import re
from pathlib import Path


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
