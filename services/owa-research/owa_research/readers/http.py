"""普通 HTTP 抓取器（规格 5、9、10）。

- 关闭自动重定向，逐跳检查；只自动跟随同协议、同端口、无账号密码、主机名相同或只差 www. 的重定向，最多 max_redirects 跳；
  跨主机重定向不跟，把目标 URL 放进 FetchResult.redirect_to 交给研究循环决定。
- 对端 IP 在 socket 连接事件上检查（连接后检查，天然免疫 DNS rebinding）；每一跳新连接重新检查。
- 每域名退避限流（429/503 指数退避）、总并发信号量、有界重试、下载字节上限。
- 不检查 robots.txt（D17）：这里的抓取都是替人取内容，与浏览器打开同性质；对站点的礼貌由上面几项承担。
- 页面缓存以原始 URL 为键（不是 https 升级或重定向后的 URL），15 分钟、总容量 50 MB、LRU。
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from owa_research.core.config import Settings
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.isolation.egress import EgressPolicy
from owa_research.readers.challenge import detect_challenge

_MAX_RETRIES = 3
_MEM_PAUSE_RATIO = 0.90
_MEM_PAUSE_MAX_WAIT = 30.0


def cgroup_memory_ratio() -> float | None:
    """容器 cgroup v2 内存使用比例；宿主或不可读时返回 None。"""
    try:
        cur = int(open("/sys/fs/cgroup/memory.current").read())
        mx = open("/sys/fs/cgroup/memory.max").read().strip()
        return None if mx == "max" else cur / int(mx)
    except (OSError, ValueError):
        return None


async def wait_for_memory(stage: str) -> None:
    """内存到限额约 90% 时暂停新抓取，等待释放；超时返回 RESOURCE_PRESSURE。"""
    waited = 0.0
    while (ratio := cgroup_memory_ratio()) is not None and ratio >= _MEM_PAUSE_RATIO:
        if waited >= _MEM_PAUSE_MAX_WAIT:
            raise ToolError(ErrorCode.RESOURCE_PRESSURE, f"容器内存已用 {ratio:.0%}，暂停新抓取超过 {_MEM_PAUSE_MAX_WAIT:.0f} 秒", retryable=True, stage=stage)
        await asyncio.sleep(1.0)
        waited += 1.0


class _CheckedBackend(httpcore.AsyncNetworkBackend):
    """连接建立后读 socket 对端地址做检查；不合规立即关闭。"""

    def __init__(self, policy: EgressPolicy) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._policy = policy

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
        sock = stream.get_extra_info("socket")
        peer = sock.getpeername() if sock is not None else None
        try:
            if peer is None:
                raise ToolError(ErrorCode.SSRF_BLOCKED, "无法读取对端地址", stage="egress")
            self._policy.check_peer(peer[0], host=host)
        except ToolError:
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise ToolError(ErrorCode.SSRF_BLOCKED, "不允许 unix socket", stage="egress")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class CheckedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, policy: EgressPolicy, **kw: Any) -> None:
        super().__init__(**kw)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            network_backend=_CheckedBackend(policy),
        )


@dataclass
class FetchResult:
    url: str  # 原始请求 URL（缓存键）
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    content_type: str
    fetched_at: str
    elapsed: float
    from_cache: bool = False
    redirect_to: str | None = None  # 跨主机重定向目标，未跟随
    hops: int = 0

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()

    def text(self) -> str:
        enc = _charset(self.content_type) or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


FIXTURE_PREFIX = "https://fixtures.invalid/"
FIXTURE_DIR = Path("/fixtures")


def _fixture(url: str) -> FetchResult:
    """bench 实验样本（OWA_FIXTURE_URLS=1 才走到这里）：只认挂载目录下的文件名，不带路径。"""
    name = url[len(FIXTURE_PREFIX):].split("?")[0]
    p = FIXTURE_DIR / name
    if not name or "/" in name or name.startswith(".") or not p.is_file():
        raise ToolError(ErrorCode.FETCH_FAILED, f"没有这个实验样本：{name}", stage="fetch")
    ctype = "application/pdf" if name.endswith(".pdf") else "application/octet-stream"
    return FetchResult(url=url, final_url=url, status=200, headers={"content-type": ctype}, content=p.read_bytes(), content_type=ctype,
                       fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), elapsed=0.0)


class _PageCache:
    def __init__(self, ttl: float, max_bytes: int) -> None:
        self._ttl, self._max = ttl, max_bytes
        self._items: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self._size = 0
        self.hits = 0
        self.misses = 0

    def get(self, url: str) -> FetchResult | None:
        item = self._items.get(url)
        if item and item[0] > time.monotonic():
            self._items.move_to_end(url)
            self.hits += 1
            return item[1]
        if item:
            self._evict(url)
        self.misses += 1
        return None

    def put(self, url: str, r: FetchResult) -> None:
        if len(r.content) > self._max:
            return
        if url in self._items:
            self._evict(url)
        self._items[url] = (time.monotonic() + self._ttl, r)
        self._size += len(r.content)
        while self._size > self._max and self._items:
            self._evict(next(iter(self._items)))

    def _evict(self, url: str) -> None:
        _, r = self._items.pop(url)
        self._size -= len(r.content)


@dataclass
class _HostState:
    next_allowed: float = 0.0
    backoff: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HttpFetcher:
    def __init__(self, settings: Settings, policy: EgressPolicy) -> None:
        self.settings = settings
        self.policy = policy
        self._client = httpx.AsyncClient(
            transport=CheckedTransport(policy),
            follow_redirects=False,
            timeout=httpx.Timeout(settings.http_timeout_seconds, connect=15),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
            },
        )
        self._sem = asyncio.Semaphore(max(1, settings.http_concurrency))
        self._hosts: dict[str, _HostState] = {}
        self.cache = _PageCache(settings.page_cache_seconds, settings.page_cache_bytes)
        self.pages_fetched = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, *, max_bytes: int | None = None, use_cache: bool = True) -> FetchResult:
        original = url
        if use_cache and (cached := self.cache.get(original)):
            return cached
        if self.settings.fixture_urls and url.startswith(FIXTURE_PREFIX):
            return _fixture(url)
        current = self.policy.validate_url(url)
        limit = max_bytes or self.settings.max_download_bytes
        hops = 0
        t0 = time.monotonic()
        await wait_for_memory("fetch")
        async with self._sem:
            while True:
                resp = await self._get_with_backoff(current, limit)
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    target = urljoin(current, resp.headers["location"])
                    hops += 1
                    if hops > self.settings.max_redirects:
                        raise ToolError(ErrorCode.FETCH_FAILED, f"重定向超过 {self.settings.max_redirects} 跳", stage="fetch")
                    if _same_site(current, target):
                        current = self.policy.validate_url(target)
                        continue
                    return FetchResult(
                        url=original, final_url=current, status=resp.status_code, headers=dict(resp.headers),
                        content=b"", content_type="", fetched_at=_now(), elapsed=time.monotonic() - t0,
                        redirect_to=target, hops=hops,
                    )
                content = resp.content
                result = FetchResult(
                    url=original, final_url=current, status=resp.status_code, headers=dict(resp.headers),
                    content=content, content_type=resp.headers.get("content-type", ""), fetched_at=_now(),
                    elapsed=time.monotonic() - t0, hops=hops,
                )
                self.pages_fetched += 1
                ctype = result.content_type.lower()
                if "html" in ctype or resp.status_code in (403, 429, 503) or not ctype:
                    challenge = detect_challenge(resp.status_code, result.headers, content[:200_000].decode("utf-8", errors="replace"))
                    if challenge:
                        raise ToolError(ErrorCode.NEEDS_HUMAN_VERIFICATION, f"挑战页: {challenge}", stage="fetch")
                if resp.status_code >= 400:
                    raise ToolError(ErrorCode.FETCH_FAILED, f"HTTP {resp.status_code}", retryable=resp.status_code >= 500, stage="fetch")
                if use_cache:
                    self.cache.put(original, result)
                return result

    async def _get_with_backoff(self, url: str, limit: int) -> httpx.Response:
        host = urlsplit(url).netloc.lower()
        state = self._hosts.setdefault(host, _HostState())
        attempt = 0
        while True:
            async with state.lock:
                wait = state.next_allowed - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
            try:
                resp = await self._stream_limited(url, limit)
            except ToolError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                attempt += 1
                if attempt > _MAX_RETRIES:
                    raise ToolError(ErrorCode.FETCH_FAILED, f"网络失败: {type(e).__name__}", retryable=True, stage="fetch") from None
                await asyncio.sleep(min(2**attempt, 20) + random.random())
                continue
            if resp.status_code in (429, 503):
                if detect_challenge(resp.status_code, dict(resp.headers), resp.content[:200_000].decode("utf-8", errors="replace")):
                    return resp  # 挑战页不重试，交给上层返回 NEEDS_HUMAN_VERIFICATION
                state.backoff = min(max(state.backoff * 2, 2.0), 120.0)
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else state.backoff
                state.next_allowed = time.monotonic() + delay
                attempt += 1
                if attempt <= _MAX_RETRIES and delay <= 30:
                    continue
                raise ToolError(ErrorCode.RATE_LIMITED, f"{host} 返回 {resp.status_code}", retryable=True, stage="fetch")
            state.backoff = 0.0
            return resp

    async def post_form(self, url: str, *, params: dict[str, Any], files: dict[str, tuple[str, bytes, str]], limit: int = 2_000_000) -> httpx.Response:
        """表单上传（以图搜图用）：同样过出站策略（CheckedTransport + validate_url），不跟随重定向，响应超过 limit 字节就报错。"""
        url = self.policy.validate_url(url)
        await wait_for_memory("fetch")
        async with self._sem:
            try:
                resp = await self._client.post(url, params=params, files=files)
            except httpx.HTTPError as e:
                raise ToolError(ErrorCode.FETCH_FAILED, f"上传失败: {type(e).__name__}", retryable=True, stage="fetch") from None
        if len(resp.content) > limit:
            raise ToolError(ErrorCode.FETCH_FAILED, f"响应超过上限 {limit} 字节", stage="fetch")
        return resp

    async def _stream_limited(self, url: str, limit: int) -> httpx.Response:
        async with self._client.stream("GET", url) as resp:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ToolError(ErrorCode.FETCH_FAILED, f"内容 {declared} 字节超过上限 {limit}", stage="fetch")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes(65536):
                size += len(chunk)
                if size > limit:
                    raise ToolError(ErrorCode.FETCH_FAILED, f"下载超过上限 {limit} 字节", stage="fetch")
                chunks.append(chunk)
            resp._content = b"".join(chunks)  # noqa: SLF001 — 在 stream 上下文中固定已读内容
            return resp


def _same_site(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    if pa.scheme != pb.scheme or pa.port != pb.port or "@" in pb.netloc:
        return False
    ha, hb = (pa.hostname or "").lower(), (pb.hostname or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb)


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset":
            return v.strip().strip('"').lower()
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
