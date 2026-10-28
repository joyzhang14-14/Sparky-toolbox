"""研究工具统一错误：code、message、retryable、stage、source_id（可选），不带 stack trace。"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_CAPABILITY_UNVERIFIED = "MODEL_CAPABILITY_UNVERIFIED"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    NEEDS_HUMAN_VERIFICATION = "NEEDS_HUMAN_VERIFICATION"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    FETCH_FAILED = "FETCH_FAILED"


class ToolErrorInfo(BaseModel):
    """工具返回给研究模型 / 主会话的错误结构。"""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    stage: str | None = None
    source_id: str | None = None


class ToolError(Exception):
    """在容器内部抛出的异常；到达工具边界时转成 ToolErrorInfo。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        stage: str | None = None,
        source_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.info = ToolErrorInfo(
            code=code, message=message, retryable=retryable, stage=stage, source_id=source_id
        )

    @property
    def code(self) -> ErrorCode:
        return self.info.code
