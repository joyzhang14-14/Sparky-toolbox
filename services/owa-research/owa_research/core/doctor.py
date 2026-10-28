"""doctor：默认只读核验模型 endpoint/alias、搜索 JSON、浏览器、依赖和配置；--smoke 才做实际生成。"""
from __future__ import annotations

import importlib
import shutil
from typing import Any

from owa_research.core.errors import ToolError
from owa_research.core.runtime import Runtime

_OPTIONAL_MODULES = ["trafilatura", "pymupdf", "pdfplumber", "docx", "openpyxl", "pptx", "PIL"]


async def run_doctor(rt: Runtime, *, smoke: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "checks": {}}

    def rec(name: str, ok: bool, detail: Any = None) -> None:
        report["checks"][name] = {"ok": ok, "detail": detail}
        if not ok:
            report["ok"] = False

    try:
        info = await rt.model.verify()
        rec("model_endpoint", True, {"id": info.get("id"), "root": info.get("root"), "max_model_len": info.get("max_model_len")})
    except ToolError as e:
        rec("model_endpoint", False, e.info.model_dump())
    rec("search_healthz", await rt.search.healthz(), rt.settings.searxng_base_url)
    try:
        r = await rt.search.search("open web access doctor", max_results=3)
        rec("search_json", True, {"hits": len(r.hits), "engines": r.engines_used, "warnings": r.warnings})
    except ToolError as e:
        rec("search_json", False, e.info.model_dump())
    rec("node", shutil.which("node") is not None, shutil.which("node"))
    rec("libreoffice", shutil.which("soffice") is not None, shutil.which("soffice"))
    mods = {}
    for m in _OPTIONAL_MODULES:
        try:
            importlib.import_module(m)
            mods[m] = True
        except Exception:  # noqa: BLE001
            mods[m] = False
    rec("python_modules", True, mods)
    rec("config", True, {k: v for k, v in rt.settings.model_dump().items() if k not in ("model_api_key",)})
    try:
        await rt.browser.start()
        rec("browser_start", True, {"seconds": rt.browser.start_seconds})
    except ToolError as e:
        rec("browser_start", False, e.info.model_dump())
    if smoke:
        try:
            obj, resp = await rt.model.chat_json(
                [{"role": "user", "content": "Reply with JSON {\"ok\": true}."}],
                json_schema={"title": "smoke", "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                max_tokens=64, thinking=False,
            )
            rec("model_smoke", obj.get("ok") is True, {"usage": resp.usage, "seconds": resp.seconds})
        except ToolError as e:
            rec("model_smoke", False, e.info.model_dump())
        try:
            r = await rt.browser.render("https://example.com/", max_chars=200)
            rec("browser_smoke", "Example Domain" in r.title, {"seconds": r.elapsed})
        except ToolError as e:
            rec("browser_smoke", False, e.info.model_dump())
    return report
