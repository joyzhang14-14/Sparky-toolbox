"""来源 provider 接口（规格 7、8）。

- SearchProvider：关键词搜索（SearXNG 实现）。
- DatasetProvider：垂直来源的专有查询协议（SEC 首个实现；GitHub、arXiv 后续各自按服务实现）。
- RemoteFetchProvider：商用抓取服务的"远程抓取后备"槽位；v1 只定义接口不实现，接入与否由 Joy 决定。
"""
from __future__ import annotations

from typing import Any, Protocol


class SearchProvider(Protocol):
    async def search(self, query: str, **kw: Any) -> Any: ...


class DatasetProvider(Protocol):
    name: str

    async def lookup(self, **kw: Any) -> dict[str, Any]: ...


class RemoteFetchProvider(Protocol):
    """把 URL 交给第三方抓取服务并按次计费的后备路径。v1 不实现。"""

    name: str

    async def fetch(self, url: str) -> bytes: ...


class RemoteFetchNotConfigured:
    name = "remote-fetch-unconfigured"

    async def fetch(self, url: str) -> bytes:
        raise NotImplementedError("远程抓取后备未接入（v1 设计决定）")
