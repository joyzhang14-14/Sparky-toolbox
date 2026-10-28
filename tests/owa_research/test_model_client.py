import asyncio
import json

import httpx
import pytest

from owa_research.core.errors import ErrorCode, ToolError
from owa_research.core.model_client import ModelClient


def make_client(handler):
    c = ModelClient("http://model.test/v1", "qwen")
    c._client = httpx.AsyncClient(base_url="http://model.test/v1", transport=httpx.MockTransport(handler))
    return c


async def test_verify_rejects_unregistered_model():
    def handler(req):
        return httpx.Response(200, json={"data": [{"id": "other"}]})

    with pytest.raises(ToolError) as ei:
        await make_client(handler).verify()
    assert ei.value.code == ErrorCode.MODEL_CAPABILITY_UNVERIFIED


async def test_chat_records_usage_and_null_cache():
    def handler(req):
        body = json.loads(req.content)
        assert body["model"] == "qwen"
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None},
        })

    c = make_client(handler)
    obj, resp = await c.chat_json([{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert obj == {"a": 1}
    assert c.usage.prompt == 10 and c.usage.completion == 5 and c.usage.calls == 1
    assert c.usage.cached_prompt is None


async def test_chat_is_serialized():
    active = 0
    peak = 0

    async def handler(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    c = make_client(handler)
    await asyncio.gather(*(c.chat([{"role": "user", "content": "x"}]) for _ in range(5)))
    assert peak == 1 and c.usage.calls == 5


async def test_model_down_is_structured():
    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(ToolError) as ei:
        await make_client(handler).chat([{"role": "user", "content": "x"}])
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE and ei.value.info.retryable
