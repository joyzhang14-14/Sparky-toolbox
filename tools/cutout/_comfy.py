"""cutout 工具用的 ComfyUI 安装位置：网关给的模型档位（STB_MODEL，cutout 不拿档位）的 home，没给默认 ~/apps/ComfyUI；
带依赖（torch、BiRefNet）的 python 是 <home>/venv/bin/python。
"""
import json
import os
from pathlib import Path
from typing import Any


def model() -> dict[str, Any]:
    """网关给的模型档位（STB_MODEL）；没有（工具、手动调用）是空的。"""
    try:
        got = json.loads(os.environ.get("STB_MODEL") or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def comfy_home() -> Path:
    return Path(str(model().get("home") or "~/apps/ComfyUI")).expanduser().resolve()


def comfy_python() -> str:
    return str(comfy_home() / "venv" / "bin" / "python")
