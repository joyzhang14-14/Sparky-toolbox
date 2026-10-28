"""主模型传给子代理的参数的最小 JSON schema 校验（check，网关的 _subagent_args 按子代理的 parameters() 查）：
type / properties / required / additionalProperties=false / enum / maxLength / maxItems / minimum / maximum / items；
FORBIDDEN_KEYS 是指挥字段（参数名不能用，subagents._inputs 也按它拒）。
"""
from __future__ import annotations

from typing import Any

FORBIDDEN_KEYS = {"commands", "next_actions", "instructions", "system_prompt", "tool_calls", "actions"}


class ReaderError(ValueError):
    pass


def check(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """最小 JSON schema 校验：type / properties / required / additionalProperties=false / enum / maxLength / maxItems / minimum / maximum / items。"""
    t = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        raise ReaderError(f"{path} 不在枚举内")
    if t == "object":
        if not isinstance(value, dict):
            raise ReaderError(f"{path} 应为对象")
        props = schema.get("properties", {})
        for k in value:
            if k in FORBIDDEN_KEYS:
                raise ReaderError(f"{path}.{k} 是禁止的指挥字段")
            if k not in props and schema.get("additionalProperties", True) is False:
                raise ReaderError(f"{path}.{k} 是 schema 外的字段")
        for k in schema.get("required", []):
            if k not in value:
                raise ReaderError(f"{path} 缺少 {k}")
        for k, v in value.items():
            if k in props:
                check(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            raise ReaderError(f"{path} 应为数组")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ReaderError(f"{path} 超过 maxItems")
        for i, v in enumerate(value):
            check(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            raise ReaderError(f"{path} 应为字符串")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ReaderError(f"{path} 超过 maxLength")
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReaderError(f"{path} 应为整数")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ReaderError(f"{path} 超出范围")
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ReaderError(f"{path} 应为数字")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ReaderError(f"{path} 超出范围")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ReaderError(f"{path} 应为布尔")
