"""所有组件连 stbcc-hub（聊天中枢，control_center/hub.py）的客户端：平台、渠道、工具都用 call；中枢 ↔ 各渠道也走同样的帧
（unix socket，一行 JSON 请求、一行 JSON 回复）。对方不在、超时、回复坏时 request / call 返回 None；要知道原因（连不上 / 超时 / 对方返回错误的原文）
用 request_detail / call_detail。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

from control_center.paths import SOCKETS

LIMIT = 2 ** 22          # 一行 JSON 的上限（卡片带问题 / 计划全文时会超过默认的 64 KB）
PUBLISH_TIMEOUT = 150    # 发布事件（publish）等中枢交完的上限：要等同来源排在前面的事件，和订阅者的 listen.py（每次最多 listen.CALL_TIMEOUT_S 秒）

ZWSP = "​"


def escape(text: str) -> str:
    """来自 agent 或不可信来源、由运行器贴出的文本：去控制字符、转义 Markdown、反引号用零宽空格切开。"""
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c)[0] != "C")
    text = re.sub(r"([\\*_~|>#\[\]()-])", r"\\\1", text)
    return text.replace("`", "`" + ZWSP)


def socket_path() -> Path:
    """中枢的 socket：~/sparky-toolbox/data/sockets/hub.sock（调用时才算，测试换了路径也跟着走）。"""
    return SOCKETS / "hub.sock"


async def request_detail(path: Path, req: dict[str, Any], timeout: float = 15) -> tuple[dict[str, Any] | None, str]:
    """同 request，另返回出错原因（成功且对方没报错时是空串）：连不上 / 超时 / 回复坏（带原文）/ 对方返回 {ok: false, error}（带原文）。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=LIMIT), 3)
    except asyncio.TimeoutError:
        return None, f"连不上 {path}：3 秒没连上"
    except OSError as e:
        return None, f"连不上 {path}：{type(e).__name__}: {e}"
    try:
        w.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout)
    except asyncio.TimeoutError:
        return None, f"{path} 超时：{timeout:g} 秒没回复"
    except (OSError, ValueError) as e:                  # ValueError：回复一行超过 LIMIT
        return None, f"和 {path} 通信出错：{type(e).__name__}: {e}"
    finally:
        w.close()
    if not line:
        return None, f"{path} 没回复就断开了"
    try:
        reply = json.loads(line)
    except ValueError as e:
        return None, f"{path} 的回复不是合法 JSON（{e}）：{line[:300].decode(errors='replace')}"
    if isinstance(reply, dict) and reply.get("ok") is False:
        return reply, f"返回错误：{reply.get('error') or json.dumps(reply, ensure_ascii=False)[:300]}"
    return reply, ""


async def request(path: Path, req: dict[str, Any], timeout: float = 15) -> dict[str, Any] | None:
    return (await request_detail(path, req, timeout))[0]


async def call(op: str, timeout: float = 15, **kw: Any) -> dict[str, Any] | None:
    """调中枢（op 见 control_center/hub.py）：平台各处（status、presence、send、card…）、渠道（publish、card_act、hello）、
    reply 工具（reply）、以 sparky 跑的工具（card、card_use、secret、monitor）都用它。timeout：等回复的秒数（publish 等得久，见 PUBLISH_TIMEOUT）。"""
    return await request(socket_path(), {"op": op, **kw}, timeout=timeout)


async def call_detail(op: str, **kw: Any) -> tuple[dict[str, Any] | None, str]:
    """同 call，另返回出错原因（见 request_detail）；要把原因告诉模型 / Joy 的调用方用它。"""
    return await request_detail(socket_path(), {"op": op, **kw})


def call_sync(op: str, timeout: float = 15, **kw: Any) -> dict[str, Any] | None:
    """同步版的 call（网页的请求线程、reply 工具用）：另起线程跑事件循环，调用方在不在事件循环里都能用。"""
    box: dict[str, Any] = {}
    t = threading.Thread(target=lambda: box.update(r=asyncio.run(call(op, timeout=timeout, **kw))), daemon=True)
    t.start()
    t.join()
    return box.get("r")
