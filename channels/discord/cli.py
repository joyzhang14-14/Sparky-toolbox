"""Discord 渠道的 stb 子命令（control_center/cli.py 启动时扫描 channels/*/cli.py 注册）：stb discord 起 Discord 入口适配器。"""
from __future__ import annotations

import argparse
import asyncio

from control_center.cli import _install_service


def cmd_discord(a: argparse.Namespace) -> None:
    if a.install:
        return _install_service("stbcc-discord")
    from channels.discord.service import main as discord_main

    asyncio.run(discord_main())


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("discord", help="Discord 入口适配器（在线状态、表情进度、私信与卡片按钮转给中枢）；--install 装成用户服务"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_discord)
