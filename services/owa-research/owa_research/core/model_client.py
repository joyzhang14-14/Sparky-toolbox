"""OpenAI 兼容模型客户端（规格 3.2、9）。

- 只用 /v1/models 与 /v1/chat/completions。
- 进程内 asyncio.Semaphore 保证研究循环对模型的调用串行（OWA_MODEL_CONCURRENCY=1）。
- 记录 usage 与缓存命中；后端不暴露的指标填 None，不编造。
- endpoint/认证由可信宿主注入，不交给研究模型。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from owa_research.core.errors import ErrorCode, ToolError


@dataclass
class ModelUsage:
    prompt: int = 0
    completion: int = 0
    cached_prompt: int | None = None
    calls: int = 0
    seconds: float = 0.0

    def add(self, usage: dict[str, Any] | None, seconds: float) -> None:
        self.calls += 1
        self.seconds += seconds
        if not usage:
            return
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is not None:
            self.cached_prompt = (self.cached_prompt or 0) + int(cached)


@dataclass
class ModelResponse:
    text: str
    usage: dict[str, Any] | None
    finish_reason: str | None
    seconds: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class ModelClient:
    def __init__(
        self,
        base_url: str,
        model_id: str,
        *,
        api_key: str = "local",
        timeout_seconds: float = 600,
        concurrency: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds, connect=10),
        )
        self._lease = asyncio.Semaphore(max(1, concurrency))
        self.usage = ModelUsage()
        self.verified_vision: bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify(self) -> dict[str, Any]:
        """只读核验：endpoint 可达且 model_id 已注册。"""
        try:
            r = await self._client.get("/models")
        except httpx.HTTPError as e:
            raise ToolError(ErrorCode.MODEL_UNAVAILABLE, f"模型端点不可达: {type(e).__name__}", retryable=True, stage="model") from None
        if r.status_code != 200:
            raise ToolError(ErrorCode.MODEL_UNAVAILABLE, f"/models 返回 {r.status_code}", retryable=True, stage="model")
        models = {m.get("id"): m for m in r.json().get("data", [])}
        if self.model_id not in models:
            raise ToolError(
                ErrorCode.MODEL_CAPABILITY_UNVERIFIED,
                f"端点未注册模型 {self.model_id!r}，可用: {sorted(models)}",
                stage="model",
            )
        return models[self.model_id]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        json_schema: dict[str, Any] | None = None,
        signal: asyncio.Event | None = None,
        thinking: bool = True,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # 机械性调用（排序、抽取、OCR）关闭思维链，省 token 与时间；vLLM 对 Qwen3 系列支持该模板参数
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": json_schema.get("title", "output"), "schema": json_schema},
            }
        if signal is not None and signal.is_set():
            raise ToolError(ErrorCode.CANCELLED, "研究已取消", stage="model")
        async with self._lease:
            t0 = time.monotonic()
            try:
                r = await self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException:
                raise ToolError(ErrorCode.MODEL_UNAVAILABLE, "模型调用超时", retryable=True, stage="model") from None
            except httpx.HTTPError as e:
                raise ToolError(ErrorCode.MODEL_UNAVAILABLE, f"模型调用失败: {type(e).__name__}", retryable=True, stage="model") from None
            seconds = time.monotonic() - t0
        if r.status_code == 429:
            raise ToolError(ErrorCode.RATE_LIMITED, "模型端点限流", retryable=True, stage="model")
        if r.status_code != 200:
            raise ToolError(ErrorCode.MODEL_UNAVAILABLE, f"模型端点返回 {r.status_code}", retryable=r.status_code >= 500, stage="model")
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = data.get("usage")
        self.usage.add(usage, seconds)
        return ModelResponse(text=text, usage=usage, finish_reason=choice.get("finish_reason"), seconds=seconds, raw=data)

    async def chat_json(self, messages: list[dict[str, Any]], *, json_schema: dict[str, Any], **kw: Any) -> tuple[Any, ModelResponse]:
        resp = await self.chat(messages, json_schema=json_schema, **kw)
        try:
            return json.loads(_strip_fence(resp.text)), resp
        except json.JSONDecodeError:
            why = "输出在 max_tokens 处被截断" if resp.finish_reason == "length" else "模型未返回合法 JSON"
            raise ToolError(ErrorCode.EXTRACTION_FAILED, why, retryable=True, stage="model") from None

    @staticmethod
    def image_part(image_bytes: bytes, mime: str = "image/png") -> dict[str, Any]:
        """构造 OpenAI 兼容的图片输入；同一个 endpoint 串行读取页图。"""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
