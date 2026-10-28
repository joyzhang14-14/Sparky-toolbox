"""渠道（入口）：Joy 和平台说话的地方（channels/discord、channels/web）。平台核心不 import 这里的代码，只认渠道协议：
渠道启动时在 data/sockets/channels/<名字>.sock 开本机 unix socket = 向中枢 stbcc-hub 报到（接口见 control_center/hub.py），
调中枢经 control_center/hub_client.py；中枢往哪些渠道发由 config/stbcc.yaml 的 channels 决定。
渠道收到 Joy 的消息就发布来源 MESSAGE 的事件（中枢的 publish，不判断交给谁）；附件先放进渠道自己的暂存目录（staging），中枢入队时拷进接下它的任务，交完渠道删掉暂存的。
本文件放各渠道共用的附件收取规则，和渠道读的私信共享数据（data/messages/ 的接管状态 route.json、命令表 commands.json，由私信任务的 listen.py 写，渠道只读）。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from control_center import paths

MESSAGE = "message"         # 渠道发布 Joy 发来的消息用的事件来源名（订阅它的任务 listen_events: [message]）
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# 图片以外的附件不按类型丢，都当文件交中枢：音视频进任务工作目录的 in/；其余任何类型主 agent 只拿到文件名，交能读附件的隔离子代理读（D17；paths.MEDIA_EXT 处的说明）


def doc_name(message_id: int | str, index: int, filename: str) -> str:
    """文件附件落盘名：<消息id>-<序号>-<原名里的安全字符><扩展名里的安全字符>；名字清洗后为空用 file，没有扩展名就不带。"""
    base = Path(filename).name
    ext = re.sub(r"[^0-9A-Za-z]", "", Path(base).suffix.lower())[:10]
    stem = re.sub(r"[^\w.\-一-鿿]", "_", Path(base).stem).strip("._")[:60] or "file"
    return f"{message_id}-{index}-{stem}" + (f".{ext}" if ext else "")


def staging(channel: str) -> Path:
    """渠道 channel 的附件暂存目录 data/hub/<渠道>/staging/（0750：同整个 hub/，sparky 只读）：发布前把附件放这里，publish 返回后删掉。"""
    d = paths.HUB / channel / "staging"
    d.mkdir(parents=True, exist_ok=True, mode=0o750)
    return d


def _read(file: str) -> dict[str, Any]:
    try:
        d = json.loads((paths.MESSAGES / file).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def current_task() -> str | None:
    """现在接消息的任务：route.json 的 holder（还订阅 MESSAGE、还启用才算），否则 default；还没有 route.json 时只有一个订阅 MESSAGE 的任务就是它，否则 None。
    网页聊天的对话与状态栏跟着它（写 route.json 的是私信任务的 listen.py，见 tasks/dm-assistant/_chat.py）。"""
    from control_center.task import subscribers

    r = _read("route.json")
    holder, subs = r.get("holder"), subscribers(MESSAGE, enabled_only=True)
    if holder and holder in subs:
        return str(holder)
    if r.get("default"):
        return str(r["default"])
    return subs[0] if len(subs) == 1 else None          # 还没有 route.json（还没收过消息）：只有一个订阅 MESSAGE 的任务就是它


def holder_since() -> dict[str, Any]:
    """接管着私信的任务 {task, since}（网页状态栏显示）；没有接管是空。"""
    r = _read("route.json")
    return {"task": r["holder"], "since": r.get("since")} if r.get("holder") == current_task() and r.get("holder") else {}


def commands() -> dict[str, dict[str, Any]]:
    """命令表 commands.json：{"/命令": {tasks, description, args: [{name, description, required?, choices: [{name, value}]}]}}（各私信任务登记自己的）。"""
    return {k: v for k, v in _read("commands.json").items() if isinstance(v, dict) and str(k).startswith("/")}
