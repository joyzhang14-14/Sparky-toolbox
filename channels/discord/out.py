"""Discord 出站加固（规格 11.7、D14）：Markdown 感知分块、出站清洗、转义、带 nonce 的幂等发送。
reply MCP 服务器与 stbcc-discord 服务共用。"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx

from control_center.hub_client import ZWSP, escape  # noqa: F401 — 转义经中枢发出的文本，各渠道共用

LIMIT = 2000
SUPPRESS_EMBEDS = 1 << 2   # 消息标志：不生成链接预览。预览是 Discord 服务器去抓网址，回复里带数据的网址不点也会被访问
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_SCAFFOLD_LINE = re.compile(r"^\s*(analysis|thinking|reasoning|thought)\s*:", re.I)
_SCAFFOLD_BLOCK = re.compile(r"<(think|thinking|analysis|tool_call|tool_calls|function_call)>.*?</\1>|<\|im_start\|>.*?<\|im_end\|>|<\|im_start\|>.*$", re.S)


def _grapheme_ok(text: str, i: int) -> bool:
    """能否在 text[i] 前面切：后面不能是组合记号 / ZWJ / 变体选择符 / 低位代理，前面不能是 ZWJ。"""
    if i <= 0 or i >= len(text):
        return True
    c = text[i]
    if unicodedata.category(c) in ("Mn", "Mc", "Me") or c in "‍️︎" or "\U0001F3FB" <= c <= "\U0001F3FF" or text[i - 1] == "‍":
        return False
    return True


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    """按 2000 字符分块：优先在换行切，其次空格，最后字形边界；跨块的代码围栏先闭合再在下一块重开。"""
    out: list[str] = []
    fence: str | None = None      # 当前打开的围栏（含语言），如 "```python"
    cur = ""

    def flush() -> None:
        nonlocal cur, out
        if cur.strip():
            out.append(cur + ("\n" + fence[:3] if fence and not cur.endswith("\n" + fence[:3]) else ""))
        cur = ""

    for line in text.splitlines(keepends=True):
        m = _FENCE.match(line.rstrip("\n"))
        prefix = ("" if cur or not fence else fence + "\n")
        if len(cur) + len(prefix) + len(line) > limit - 8:
            flush()
            prefix = fence + "\n" if fence else ""
        piece = prefix + line
        while len(piece) > limit - 8:
            cut = piece.rfind(" ", 0, limit - 8)
            if cut < len(prefix) + 1:
                cut = limit - 8
            while cut > len(prefix) and not _grapheme_ok(piece, cut):
                cut -= 1
            cur = piece[:cut]
            flush()
            piece = (fence + "\n" if fence else "") + piece[cut:].lstrip(" ")   # 在空格处切：下一块不以空格开头
        cur += piece
        if m:
            fence = None if fence and m.group(2)[0] == fence[0] else (m.group(2) + m.group(3).strip() if not fence else fence)
    flush()
    return out or [""]


def sanitize(text: str) -> str:
    """剔除本地模型漏出的思维链行与工具调用脚手架（代码围栏内保留）。"""
    text = _SCAFFOLD_BLOCK.sub("", text)
    out, fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fence = not fence
            out.append(line)
            continue
        if not fence and _SCAFFOLD_LINE.match(line):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


class SendError(RuntimeError):
    pass


def send(channel_id: str, content: str, token: str, *, dryrun_file: str | None = None, files: list[Path] | None = None,
         nonce_prefix: str = "") -> list[str]:
    """清洗 → 分块 → 逐块发送。每块带 nonce（enforce_nonce），网络错误可安全重放；429 等待后重试；
    5xx 结果不明，不重放，直接报错。零提及。files（宿主路径，调用方已校验：reply 工具校验，sparky 发来的 stbcc-hub 再校验一遍）随最后一块以 multipart 发出。
    nonce_prefix：nonce 的前缀（如 channels/discord/service.py 的 CRON_NONCE、SYSTEM_NONCE）。
    返回消息 id 列表（dry-run 返回空 id）。"""
    ids: list[str] = []
    parts = [p for p in chunk(sanitize(content)) if p.strip()] or ([""] if files else [])
    for i, part in enumerate(parts):
        att = files if files and i == len(parts) - 1 else None
        if dryrun_file:
            with open(dryrun_file, "a") as f:
                row = {"ts": time.time(), "channel_id": channel_id, "content": part, **({"files": [a.name for a in att]} if att else {}),
                       **({"nonce_prefix": nonce_prefix} if nonce_prefix else {})}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            ids.append("")
            continue
        ids.append(_post(channel_id, part, token, att, nonce_prefix))
    return ids


def _post(channel_id: str, content: str, token: str, files: list[Path] | None = None, nonce_prefix: str = "") -> str:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body: dict[str, Any] = {"content": content, "nonce": (nonce_prefix + secrets.token_hex(8))[:25], "enforce_nonce": True, "allowed_mentions": {"parse": []}, "flags": SUPPRESS_EMBEDS}
    headers = {"Authorization": f"Bot {token}", "User-Agent": "sparky-toolbox (stb, 0.1)"}
    form: dict[str, Any] = {"json": body}
    if files:       # 附件：multipart，正文与同一个 nonce 放在 payload_json 里，重放一样安全
        body["attachments"] = [{"id": i, "filename": p.name} for i, p in enumerate(files)]
        form = {"data": {"payload_json": json.dumps(body)},
                "files": {f"files[{i}]": (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream") for i, p in enumerate(files)}}
    for attempt in range(4):
        try:
            r = httpx.post(url, **form, headers=headers, timeout=30)
        except httpx.TransportError:
            if attempt == 3:
                raise SendError("Discord 网络错误，重试用尽") from None
            time.sleep(1.5 * (attempt + 1))      # 请求可能没到：带 nonce 重放安全
            continue
        if r.status_code == 429:
            time.sleep(float((r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("retry_after", 2)))
            continue                              # 明确未创建：重放安全
        if r.status_code >= 500:
            raise SendError(f"Discord 返回 {r.status_code}，结果不明，不重放")
        if r.status_code >= 300:
            raise SendError(f"Discord 返回 {r.status_code}: {r.text[:200]}")
        return str(r.json().get("id", ""))
    raise SendError("Discord 限流重试用尽")
