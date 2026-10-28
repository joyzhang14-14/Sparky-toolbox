"""研究工具分发器：allowlist 检查 + 参数校验（规格 3.3）。

即使模型请求 bash/read/write/exec 或陌生工具，也返回 TOOL_NOT_ALLOWED。
隐藏工具描述或提示"不要执行命令"都不能替代这个检查。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from owa_research.core.errors import ErrorCode, ToolError, ToolErrorInfo
from owa_research.core.tools import RESEARCH_TOOL_ALLOWLIST, TOOL_SPECS

Handler = Callable[[BaseModel], Awaitable[Any]]


class ToolDispatcher:
    def __init__(self, allowlist: frozenset[str] = RESEARCH_TOOL_ALLOWLIST) -> None:
        self._allowlist = allowlist
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        if name not in self._allowlist:
            raise ValueError(f"{name} 不在 allowlist 内，不能注册")
        self._handlers[name] = handler

    def restrict(self, names: list[str]) -> None:
        """只留下这些工具（请求的 tools）：注册过的其它工具之后调用一律 TOOL_NOT_ALLOWED。"""
        self._allowlist = self._allowlist & frozenset(names)

    @property
    def available(self) -> list[str]:
        return sorted(n for n in self._allowlist if n in self._handlers)

    async def call(self, name: str, args: dict[str, Any] | None) -> Any:
        """返回 handler 结果；任何失败都以 ToolError 抛出，调用方用 to_error_info 转成结构化错误。"""
        if name not in self._allowlist or name not in TOOL_SPECS:
            raise ToolError(ErrorCode.TOOL_NOT_ALLOWED, f"工具 {name!r} 不在允许列表内", stage="dispatch")
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(ErrorCode.TOOL_NOT_ALLOWED, f"工具 {name!r} 当前未启用", stage="dispatch")
        try:
            params = TOOL_SPECS[name].params.model_validate(args or {})
        except ValidationError as e:
            from owa_research.core.tools import compact_params  # 局部导入避免循环

            raise ToolError(ErrorCode.INVALID_ARGUMENT, _brief(e) + f"。期望参数: {compact_params(TOOL_SPECS[name])}", stage="dispatch") from None
        return await handler(params)


def to_error_info(exc: BaseException) -> ToolErrorInfo:
    """把任意异常变成不泄露 stack trace 的结构化错误。"""
    if isinstance(exc, ToolError):
        return exc.info
    return ToolErrorInfo(code=ErrorCode.EXTRACTION_FAILED, message=type(exc).__name__, retryable=False)


def _brief(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "参数校验失败: " + "; ".join(parts)
