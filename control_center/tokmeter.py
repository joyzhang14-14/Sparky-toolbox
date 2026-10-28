"""一次运行的 token 用量：把每次模型调用返回的 usage 累加起来（OpenAI 兼容的 prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens，
或 input / output / cached）。cached 是输入里命中前缀缓存的部分（已含在 input 里）。
平台看得到的模型调用：沙箱里的主模型（pi 事件流里每条回答的 usage，sandbox.final_text）、agent 子代理（它的 pi 容器的事件流，同主模型，
gateway._pi_done）、generator / engine 子代理的程序在结果里交的 usage（它自己那些模型调用的合计，如联网研究容器；网关记下后从结果里拿掉）。
不读推理服务的专用指标。run.json 的 tokens 是主模型的，tokens_all 是这些加起来的（网页优先显示 tokens_all）。"""
from __future__ import annotations

from typing import Any


def _count(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


class TokenMeter:
    def __init__(self) -> None:
        self.input = self.output = self.cached = 0

    def add(self, usage: Any) -> None:
        """一次（或一批）模型调用返回的 usage；不是映射、没有计数的不算。"""
        if not isinstance(usage, dict):
            return
        self.input += _count(usage.get("prompt_tokens", usage.get("input")))
        self.output += _count(usage.get("completion_tokens", usage.get("output")))
        details = usage.get("prompt_tokens_details")
        self.cached += _count(usage.get("cached", details.get("cached_tokens") if isinstance(details, dict) else 0))

    def total(self) -> dict[str, int]:
        return {"input": self.input, "output": self.output, "cached": self.cached}
