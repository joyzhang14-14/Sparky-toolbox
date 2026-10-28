"""reply：给 owner（Joy）发消息，平台核心（同 acquire_permission 之于 ask-user）。入口是 reply 工具（tools/reply/server.py），
消息经聊天中枢 stbcc-hub 转给入口适配器。reply 的规则都在这里，平台各处引用它，不另写一份：
- TOOL：工具名。网关按它数 agent 自己发了几条（运行结束通知据此决定补不补发最终回答），运行器按它判断任务能不能自己发消息；
- 附件：只能是 sparky 家目录（沙箱里能写的地方，paths.sparky_home）或本次运行工作目录下的文件（host_files：reply 工具与中枢都按它校验），最多 MAX_FILES 个，大小不限（网页照常显示；Discord 发不出的大文件由 Discord 渠道跳过）；
- role：由渠道消息触发的运行（run.json 有来源渠道 channel）的回复记成 bot，其余运行（定时、监听、回调、手动）发的记成 cron（任务的成品）；
- 回复正文不截断：网页对话、stb chat 显示 reply 调用的参数原文（其余工具的参数只显示开头）。
"""
from __future__ import annotations

from pathlib import Path

TOOL = "reply"
MAX_FILES = 10                                              # Discord 一条消息最多 10 个附件
ROLES = ("bot", "cron")                                     # 回复的身份：渠道消息触发的运行的回复 / 别的运行的成品（入口按它决定怎么显示）


class FileError(ValueError):
    """附件不合规则：说明原样交给模型（reply 工具）或写进拒绝（中枢）。"""


def role(run_ref: str) -> str:
    """运行 run_ref（<任务>/<运行 id>）发的消息记成什么：run.json 有来源渠道（channel）是 bot，没有是 cron；拿不到运行、run.json 读不了算 bot。"""
    import json

    from control_center.paths import RUNS

    parts = str(run_ref or "").split("/")
    if len(parts) != 2 or not all(parts) or ".." in parts:
        return "bot"
    try:
        info = json.loads((RUNS / parts[0] / parts[1] / "run.json").read_text())
    except (OSError, ValueError):
        return "bot"
    return "bot" if isinstance(info, dict) and info.get("channel") else "cron"


def host_files(roots: list[Path], files: list[str]) -> list[Path]:
    """附件路径（沙箱里外同一个路径）→ 校验过的宿主路径：要写绝对路径，realpath 后在 roots（sparky 的家目录、本次运行的工作目录）之一下
    （符号链接逃不出去），是文件，最多 MAX_FILES 个。不合规则抛 FileError。"""
    if len(files) > MAX_FILES:
        raise FileError(f"一条消息最多 {MAX_FILES} 个附件，这次给了 {len(files)} 个")
    roots = [r.resolve() for r in roots]
    out = []
    for p in files:
        if not isinstance(p, str) or not p.startswith("/"):
            raise FileError(f"附件要写绝对路径（如 {roots[-1]}/out/图.png）：{p}")
        host = Path(p).resolve()
        if not any(r in host.parents for r in roots):
            raise FileError(f"附件只能是 {'、'.join(map(str, roots))} 之下的文件（别处的先拷进工作目录）：{p}")
        if not host.is_file():
            raise FileError(f"附件不存在：{p}")
        out.append(host)
    return out
