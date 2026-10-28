"""平台给工具服务器的接口（tools/、subagents/ 的程序 `from control_center import tool_kit` 或 `from control_center.tool_kit import …`）：
报错标记（平台故障、超时、程序错误）、调用时限的写法（call_timeout）、发通用审批卡并就地等（ask_permission）、
带结构化信息的结果（result：source 来源、summary 一句话摘要）。凭证见 control_center/credential.py。
import 本模块就给 MCP SDK 装上程序错误标记（_mark_crashes）：工具服务器都先 import 它。"""
from __future__ import annotations

import sys
import traceback
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from control_center.credential import MissingCredential


def platform_fault(message: str) -> Exception:
    """平台故障（容器没起来 / 被系统强制终止、owa-launcher 连不上、模型端点或 ComfyUI 不可用）：报错文字带 PLATFORM_FAULT 标记，
    网关据此按平台故障把原文报给主模型（标记在 control_center/registry.py，网关认同一个）；能不能重试、要不要 Joy 处理写在 message 里。"""
    from mcp.server.mcpserver.exceptions import ToolError

    from control_center.registry import PLATFORM_FAULT

    return ToolError(PLATFORM_FAULT + message)


def tool_timeout(message: str) -> Exception:
    """工具自己的时限到了（如解析容器默认的 180 秒）：报错文字带 TOOL_TIMEOUT 标记，网关按超时处理。"""
    from mcp.server.mcpserver.exceptions import ToolError

    from control_center.registry import TOOL_TIMEOUT

    return ToolError(TOOL_TIMEOUT + message)


TIMEOUT_DOC = "timeout 可选：这次最多跑多久（秒数，或 90s、5m、1h 这种写法），不写用默认 {default}，不设上限；任务大（大文件、长音频）就写大一点。"
DISK_DOC = "平常不写；一次最多新写 20 GB，要更多写如 \"40g\" 并写 reason，要 Joy 批准，批了用同样的 disk 再调一次"
REASON_DOC = "disk 要超过 20 GB 时写一句为什么"


def call_timeout(value: Any, default: float) -> float:
    """工具调用时模型给的 timeout（秒数或 90s / 5m / 1h30m，同 shell 工具）→ 秒数；不写（0 / 空）用 default。不设上限。
    写法不对抛 ValueError（原文交给主模型，调用有误）。"""
    if value in (None, "", 0, "0"):
        return default
    from control_center.task import TaskError, parse_duration

    s = str(value).strip()
    try:
        sec = float(s) if s.replace(".", "", 1).isdigit() else float(parse_duration(s))
    except TaskError:
        sec = 0
    if sec <= 0:
        raise ValueError(f"timeout 写秒数或 90s、5m、1h、1h30m 这种写法（你给的是 {value!r}）")
    return sec


def timeout_note(what: str, used: float, limit: float, given: bool) -> str:
    """工具自己的时限到了的说明（配 tool_timeout）：用了多久、上限多少（模型给的还是默认），怎么改。"""
    return (f"{what}超时（用了 {used:.0f} 秒，{'你给的是' if given else '默认'} {limit:.0f} 秒），已停止。"
            "如果任务本身就大，可以加大 timeout 重试")


def result(content: Any, *, source: str | None = None, summary: str | None = None, stale: str | None = None) -> Any:
    """工具结果（MCP CallToolResult）：content 是给模型的文字（str）或内容块列表；structured_content 里放给平台的键（网关读，模型看不到）：
    - source：这次读的内容从哪来（浏览器的当前页网址、附件文件名、笔记路径、接口地址、邮箱 + 这次的邮件范围…）。usable_by: subagent 的读取工具每个函数都要给
      （tests/stb_platform/test_tool_sources.py 扫描落实）：隔离子代理的答案引用这次结果时，主模型看到的 sources 里就是它；没给是 null。
    - summary（可选）：这次调用的一句话摘要，只给网页 / 日志看（网关记进 gateway.jsonl，网页的子代理过程显示「函数名 + summary」），不给主模型。
    - stale（可选）：这个结果过时后在模型上下文里换成的文字。agent 子代理的会话里带 stale 的结果只有最近 2 个（stb-gateway.ts 的 STALE_KEEP）原样给模型，
      更早的在每次调用模型前换成它（pi 扩展 stb-gateway.ts 的 context 事件；会话文件、隔离核对的原文不变）。给每步都返回整页（元素表、截图）的工具用。
    函数的返回类型标 CallToolResult（不生成输出 schema，网关原样拿到 structured_content）。"""
    from mcp.types import CallToolResult, TextContent

    blocks = [TextContent(type="text", text=content)] if isinstance(content, str) else list(content)
    meta = {k: str(v)[:500] for k, v in (("source", source), ("summary", summary), ("stale", stale)) if v}
    return CallToolResult(content=blocks, structured_content=meta or None)  # pyright: ignore[reportArgumentType]


APPROVAL_WAIT_S = 300                            # 工具就地等审批卡（同网关等问题卡的 5 分钟）
CALL: "ContextVar[dict[str, Any] | None]" = ContextVar("stb_call_meta", default=None)   # 这次调用请求的 _meta（_mark_crashes 里按调用设好）：stb_call / stb_approved


async def ask_permission(tool: str, action: str, key: str, detail: str = "", image: str | None = None, password: str = "",
                         poll: float = 3) -> tuple[str, str | None]:
    """发平台的通用审批卡（control_center/acquire_permission.permission_card：卡面文字 action / detail 由工具给，可选附图 image、密码框 password）
    并就地等 Joy 处理，最多 APPROVAL_WAIT_S 秒（运行要暂停就提前停）。返回 (结果, 值)：
    ("approve", None)；带密码框的 ("approve", 密码)（经中枢 secret op 取，只此一次、不落盘）；("reject", Joy 写的话，没写是 "")；
    ("pending", None)：没等到，卡还在等（带密码框的卡没等到或调用被结束就作废：中枢的 card_expire）（网关随后让出运行；Joy 之后同意了，续跑时用同样的 key 再来就直接放行）；("failed", 原因)：卡没发出去。
    同一次运行里 Joy 之前已经同意过同一 key 的卡（挂起后续跑）：经中枢 card_use 用掉，直接放行（带密码框的卡不复用：密码只交给发卡的那次调用）。
    主模型的调用（网关在请求的 _meta 里给 stb_call）发的卡在 payload.call 记下这次调用：工具没等到、Joy 之后同意时，平台以同样的参数替它再调用一次
    （_meta 的 stb_approved 是那张卡的 id；pre_call hook 里是环境变量 STB_APPROVED_CARD）：这时只认那张卡（经 card_use 用掉，key 对不上就不执行），不发新卡。"""
    import asyncio
    import os
    import time

    from control_center import acquire_permission, cards, hub_client

    run, kind = os.environ.get("STB_RUN_REF", ""), acquire_permission.PERMISSION_KIND
    if not run:
        return "failed", "不在任务运行里（试跑），发不了审批卡"
    meta = CALL.get() or {}
    approved = str(meta.get("stb_approved") or os.environ.get("STB_APPROVED_CARD") or "")
    if approved and not password:                    # 平台替已同意的调用重跑：只认那张卡
        r = await hub_client.call("card_use", kind=kind, run_ref=run, id=approved, match={"key": key, "tool": tool})
        return ("approve", None) if (r or {}).get("used") else ("failed", "Joy 同意的那件事和这次要做的对不上（内容在发卡之后变了），没有执行")
    if not password and ((await hub_client.call("card_use", kind=kind, run_ref=run, match={"key": key, "tool": tool})) or {}).get("used"):
        return "approve", None
    pl = {"run": run, "tool": tool, "action": action, "key": key, **({"detail": detail} if detail else {}),
          **({"image": image} if image else {}), **({"password": password} if password else {}),
          **({"call": str(meta["stb_call"])} if meta.get("stb_call") and not password else {})}
    r = await hub_client.call("card", kind=kind, title="", body="", buttons=[], payload=pl)
    card_id = (r or {}).get("id")
    if not card_id:
        return "failed", str((r or {}).get("error") or "stbcc-hub 没有响应")
    give_up, pause = time.time() + APPROVAL_WAIT_S, os.environ.get("STB_PAUSE_FILE")
    try:
        got = await _wait_card(card_id, run, kind, password, give_up, pause, poll)
    except asyncio.CancelledError:                   # 调用被结束（子代理停下、进程收尾）：密码卡作废再退出
        if password:
            await hub_client.call("card_expire", run_ref=run, id=card_id)
        raise
    if got is None and password:                     # 没等到：密码卡作废（Joy 填不了；值只能交给这次调用，之后没人取）
        await hub_client.call("card_expire", run_ref=run, id=card_id)
    return got or ("pending", None)


async def _wait_card(card_id: str, run: str, kind: str, password: str, give_up: float, pause: str | None, poll: float) -> tuple[str, str | None] | None:
    """ask_permission 就地等卡：等到了返回 (结果, 值)，到点或要暂停返回 None。"""
    import asyncio
    import time

    from control_center import cards, hub_client

    while time.time() < give_up and not (pause and Path(pause).exists()):
        got = (cards.load(card_id) or {}).get("resolved")
        if got:
            if got.get("action") != "approve":
                return "reject", str((got.get("value") or {}).get("feedback") or "").strip() if isinstance(got.get("value"), dict) else ""
            if password:
                v = await hub_client.call("secret", id=card_id, run_ref=run)
                return "approve", str((v or {}).get("value") or "")
            await hub_client.call("card_use", kind=kind, run_ref=run, id=card_id)   # 就地用掉，续跑时不再放行
            return "approve", None
        await asyncio.sleep(poll)
    return None


def _mark_crashes() -> None:
    """工具代码里的意外异常（bug）：SDK 只回 "Error executing tool <名>"，这里改成带 PROGRAM_ERROR 标记、异常类型和出错位置，
    网关据此报平台故障（程序错误）；traceback 照写 stderr（网关记进 gateway.stderr.log）。
    有意报的错不算：ToolError 不受影响；工具用 ValueError（恰好是这个类型，不含子类）报的参数错误原文交给主模型（调用有误）；
    MissingCredential 原文（[缺凭证]<键>）交网关，由它给 Joy 发凭证卡。
    工具服务器都先 import 本模块，在这里统一装上。"""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from mcp.server.mcpserver.tools import base

    from control_center.registry import PROGRAM_ERROR, program_error

    run = base.Tool.run

    async def marked(self, *args, **kwargs):
        try:
            ctx = args[1] if len(args) > 1 else kwargs.get("context")
            meta = getattr(getattr(ctx, "_request_context", None), "meta", None)
        except Exception:  # noqa: BLE001 — 拿不到 _meta 就当没有
            meta = None
        CALL.set({k: v for k, v in meta.items() if str(k).startswith("stb_")} if isinstance(meta, dict) else None)
        try:
            return await run(self, *args, **kwargs)
        except UnexpectedToolError as e:
            cause: BaseException = e
            while isinstance(cause, UnexpectedToolError) and cause.__cause__ is not None:
                cause = cause.__cause__
            if type(cause) is ValueError or isinstance(cause, MissingCredential):   # 缺凭证：原文带 [缺凭证] 标记，网关发凭证卡
                raise ToolError(str(cause)) from cause
            traceback.print_exception(cause, file=sys.stderr)
            raise ToolError(PROGRAM_ERROR + program_error(f"工具 {self.name} ", cause)) from cause

    base.Tool.run = marked


_mark_crashes()
