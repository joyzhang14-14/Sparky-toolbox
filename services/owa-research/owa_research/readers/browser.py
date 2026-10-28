"""浏览器子服务的 Python 客户端（规格 5 render_page）。

启动本地 CONNECT 代理和 Node 子服务（browser/server.mjs），经 localhost HTTP 调用；
研究模型只能传 URL 与只读动作，不能传脚本、代理配置或本地路径。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from owa_research.core.config import Settings
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.isolation.egress import EgressPolicy
from owa_research.isolation.egress_proxy import EgressProxy
from owa_research.readers.challenge import detect_challenge

_DEFAULT_SERVER = Path(__file__).resolve().parents[2] / "browser" / "server.mjs"


@dataclass
class RenderResult:
    url: str
    final_url: str
    status: int | None
    title: str
    text: str
    text_length: int
    html_head: str
    fetched_at: str
    elapsed: float


class BrowserService:
    def __init__(self, settings: Settings, policy: EgressPolicy, *, server_path: Path | None = None) -> None:
        self.settings = settings
        self.policy = policy
        self._server_path = server_path or Path(os.environ.get("OWA_BROWSER_SERVER", _DEFAULT_SERVER))
        self._proxy = EgressProxy()
        self._proc: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._start_lock = asyncio.Lock()
        self.renders = 0
        self.start_seconds: float | None = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._proc is not None:
                return
            t0 = time.monotonic()
            await self._proxy.start()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
                "OWA_PROXY_URL": self._proxy.url,
                "OWA_BROWSER_CONCURRENCY": str(self.settings.browser_concurrency),
                "OWA_USER_AGENT": self.settings.user_agent,
            }
            for k in ("PLAYWRIGHT_BROWSERS_PATH", "OWA_BROWSER_NAV_TIMEOUT_MS"):
                if k in os.environ:
                    env[k] = os.environ[k]
            self._proc = await asyncio.create_subprocess_exec(
                "node", str(self._server_path), env=env, cwd=str(self._server_path.parent),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            assert self._proc.stdout is not None
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
            except asyncio.TimeoutError:
                await self.stop()
                raise ToolError(ErrorCode.RESOURCE_PRESSURE, "浏览器子服务 30 秒内未就绪", retryable=True, stage="browser") from None
            info = json.loads(line or b"{}")
            port = info.get("port")
            if not port:
                await self.stop()
                raise ToolError(ErrorCode.RESOURCE_PRESSURE, "浏览器子服务启动失败", retryable=True, stage="browser")
            self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=httpx.Timeout(120, connect=5))
            self.start_seconds = time.monotonic() - t0

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.post("/shutdown")
            except httpx.HTTPError:
                pass
            await self._client.aclose()
            self._client = None
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._proc.kill()
            self._proc = None
        await self._proxy.stop()

    async def render(
        self,
        url: str,
        *,
        action: str = "navigate",
        wait_for_selector: str | None = None,
        scroll_to_bottom: bool = False,
        max_chars: int = 16000,
    ) -> RenderResult:
        url = self.policy.validate_url(url)
        if self._client is None:
            await self.start()
        assert self._client is not None
        t0 = time.monotonic()
        payload: dict[str, Any] = {"url": url, "action": action, "wait_for_selector": wait_for_selector, "scroll_to_bottom": scroll_to_bottom, "max_chars": max_chars}
        try:
            r = await self._client.post("/render", json=payload)
        except httpx.HTTPError as e:
            raise ToolError(ErrorCode.FETCH_FAILED, f"浏览器子服务调用失败: {type(e).__name__}", retryable=True, stage="browser") from None
        data = r.json()
        if r.status_code != 200:
            msg = str(data.get("error", ""))
            if "Timeout" in msg or "timeout" in msg:
                raise ToolError(ErrorCode.FETCH_FAILED, "浏览器导航超时", retryable=True, stage="browser")
            raise ToolError(ErrorCode.FETCH_FAILED, f"浏览器渲染失败: {msg[:200]}", retryable=False, stage="browser")
        self.renders += 1
        if data.get("blocked"):
            raise ToolError(ErrorCode.SSRF_BLOCKED, "浏览器出站被代理拒绝", stage="browser")
        probe = data.get("challenge_probe") or {}
        status = data.get("status")
        challenge = detect_challenge(status or 200, data.get("headers") or {}, data.get("html_head") or "")
        if not challenge and (probe.get("selectors") or probe.get("turnstile")):
            challenge = {"managed": "cloudflare-managed", "interactive": "cloudflare-interactive"}.get(probe.get("cType"), "cloudflare-js")
        if challenge:
            raise ToolError(ErrorCode.NEEDS_HUMAN_VERIFICATION, f"挑战页: {challenge}", stage="browser")
        if status and status >= 400:
            raise ToolError(ErrorCode.FETCH_FAILED, f"HTTP {status}", retryable=status >= 500, stage="browser")
        return RenderResult(
            url=url, final_url=data.get("final_url") or url, status=status, title=data.get("title") or "",
            text=data.get("text") or "", text_length=int(data.get("text_length") or 0), html_head=data.get("html_head") or "",
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), elapsed=time.monotonic() - t0,
        )
