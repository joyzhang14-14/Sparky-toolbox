"""reply 工具：给 owner（Joy）的回复，交聊天中枢 stbcc-hub（reply op），中枢按这次运行的入口转给对应的适配器显示：
Discord 私信来的回 Discord，网页来的回网页；定时任务等没有入口的按中枢的规则发（见 control_center/hub.py）。本进程不直连任何聊天平台。
只回给 owner（程序写死：中枢只私信 Joy；干跑文件里的频道按 .env 的 STB_OWNER_DISCORD_ID 写）。
reply 是平台核心：附件能放的目录、个数与大小、role（不是渠道消息触发的运行发的记 cron）这些规则都在 control_center/reply.py，这里和中枢都按它校验。
附件 files 是模型写的路径（沙箱里外同一个路径，只能在 sparky 的家目录下）；中枢收到原样的路径（网页显示用）和校验过的宿主路径（Discord 上传用）。
role 按这次运行（STB_RUN_REF，网关给）算；渠道按它决定怎么显示。
要记下发了什么的任务用自己的 post_call hook（match: [reply]，输入的 args 就是发出的正文与附件；如私信任务的对话库、每日任务的任务消息库）。
STB_TEST_DISCORD_DRYRUN=<文件>：不交中枢，每条消息追加写一行 {ts, text, files（文件名）, role} 到文件（测试用）。
"""
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import sys
from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center import env, hub_client, reply as reply_rules
from control_center.paths import sparky_home

DRYRUN = os.environ.get("STB_TEST_DISCORD_DRYRUN")
REPLY_TIMEOUT = 200                                         # 比中枢等适配器的 REPLY_TIMEOUT（180 秒）长一点
CHANNEL = f"dm:{env.get('STB_OWNER_DISCORD_ID') or 'owner'}"   # 只用于 --check


def _role() -> str:
    """这条的身份（control_center/reply.role）：渠道消息触发的运行的回复 bot，别的运行 cron。"""
    return reply_rules.role(os.environ.get("STB_RUN_REF", ""))

server = MCPServer(reply_rules.TOOL, instructions="私信 owner（Joy）")
def _host_files(paths: list[str]) -> list[Path]:
    """附件：校验过的宿主路径（规则见 control_center/reply.host_files：sparky 家目录或工作目录下的文件）；报错用 ToolError（MCP 只把它的文字回给模型）。"""
    roots = [sparky_home(), *([Path(os.environ["STB_WORK_ROOT"])] if os.environ.get("STB_WORK_ROOT") else [])]
    try:
        return reply_rules.host_files(roots, paths)
    except reply_rules.FileError as e:
        raise ToolError(str(e)) from None


@server.tool(description="私信 Joy 一条消息。text 是正文。"
             "files 是附件的绝对路径列表，只能在 /home/sparky 下，最多 10 个，大小、类型不限；只发附件时 text 可留空。"
             "你最后的回答平台会代发，但只发文字、不带附件：要给 Joy 看的图片、文件用这个函数的 files 发。")
def reply(text: str = "", files: list[str] = []) -> str:
    paths = _host_files(files) if files else []
    if paths and not text.strip():
        text = "（附件：" + "、".join(p.name for p in paths) + "）"     # 只有附件时正文写附件名（对话记录、网页里看得出发了什么）
    if not text.strip():
        return "正文为空，未发送"
    if DRYRUN:
        with open(DRYRUN, "a") as f:
            f.write(json.dumps({"ts": time.time(), "text": text, "files": [p.name for p in paths], "role": _role()}, ensure_ascii=False) + "\n")
        return f"已发送 1 条消息（{len(text)} 字符）" + (f"，附件 {len(paths)} 个" if paths else "")
    r = hub_client.call_sync("reply", timeout=REPLY_TIMEOUT, run_ref=os.environ.get("STB_RUN_REF", ""), text=text, files=list(files),
                                host_files=[str(p) for p in paths], role=_role())
    if r is None:
        raise ToolError("聊天中枢 stbcc-hub 没有响应，回复没有发出去；可以稍后重试")
    if not r.get("ok"):
        raise ToolError(str(r.get("error") or "回复没有发出去"))
    return str(r.get("result") or "已发送")


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:      # 只验证能起来、owner 能解析（doctor / 测试用）
        print(f"user {CHANNEL[3:]}")
        sys.exit(0)
    server.run("stdio")
