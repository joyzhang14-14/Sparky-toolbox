"""研究容器内的组件装配：配置、出站策略、模型客户端、搜索客户端、抓取器、浏览器、证据存储、工具分发器。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from owa_research.core.config import Settings
from owa_research.core.evidence import EvidenceStore
from owa_research.core.model_client import ModelClient
from owa_research.core.schema import ResearchRequest
from owa_research.isolation.allowlist import ToolDispatcher
from owa_research.isolation.egress import EgressPolicy
from owa_research.providers.searxng import SearxngClient
from owa_research.readers.browser import BrowserService
from owa_research.readers.http import HttpFetcher


@dataclass
class StageTimer:
    seconds: dict[str, float] = field(default_factory=dict)

    def add(self, stage: str, s: float) -> None:
        self.seconds[stage] = self.seconds.get(stage, 0.0) + s

    class _Ctx:
        def __init__(self, timer: "StageTimer", stage: str) -> None:
            self.timer, self.stage = timer, stage

        def __enter__(self) -> None:
            self.t0 = time.monotonic()

        def __exit__(self, *a: object) -> None:
            self.timer.add(self.stage, time.monotonic() - self.t0)

    def track(self, stage: str) -> "StageTimer._Ctx":
        return StageTimer._Ctx(self, stage)


@dataclass
class Runtime:
    settings: Settings
    policy: EgressPolicy
    model: ModelClient
    search: SearxngClient
    fetcher: HttpFetcher
    browser: BrowserService
    evidence: EvidenceStore
    dispatcher: ToolDispatcher
    timer: StageTimer
    cancel: asyncio.Event
    started_at: float

    @classmethod
    def build(cls, settings: Settings, request: ResearchRequest | None, artifact_dir: Path) -> "Runtime":
        policy = EgressPolicy.from_lists(
            [str(d) for d in (request.preferred_domains or [])] if request else None,
            [str(d) for d in (request.blocked_domains or [])] if request else None,
            max_url_length=settings.max_url_length,
        )
        return cls(
            settings=settings,
            policy=policy,
            model=ModelClient(settings.model_base_url, settings.model_id, api_key=settings.model_api_key,
                              timeout_seconds=settings.model_timeout_seconds, concurrency=settings.model_concurrency),
            search=SearxngClient(settings.searxng_base_url, user_agent=settings.user_agent),
            fetcher=HttpFetcher(settings, policy),
            browser=BrowserService(settings, policy),
            evidence=EvidenceStore(artifact_dir),
            dispatcher=ToolDispatcher(),
            timer=StageTimer(),
            cancel=asyncio.Event(),
            started_at=time.monotonic(),
        )

    async def aclose(self) -> None:
        await self.browser.stop()
        await self.fetcher.aclose()
        await self.search.aclose()
        await self.model.aclose()
