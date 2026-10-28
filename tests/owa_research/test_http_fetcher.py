"""用本地 HTTP server 验证抓取器行为：重定向策略、大小上限、退避、挑战页、缓存、socket 级拦截。"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from owa_research.core.config import Settings
from owa_research.core.errors import ErrorCode, ToolError
from owa_research.isolation.egress import EgressPolicy
from owa_research.readers.http import HttpFetcher, _same_site


class _Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        _Handler.hits[self.path] = _Handler.hits.get(self.path, 0) + 1
        if self.path == "/robots.txt":
            self._send(200, b"User-agent: *\nDisallow: /\n")
        elif self.path == "/private/x":
            self._send(200, b"private ok")
        elif self.path == "/ok":
            self._send(200, b"<html><title>ok</title><body>hello world</body></html>", "text/html; charset=utf-8")
        elif self.path == "/big":
            self._send(200, b"x" * 20000)
        elif self.path == "/cf":
            self._send(503, b"<html><title>Just a moment...</title><form id='challenge-form'></form></html>", "text/html", {"Server": "cloudflare"})
        elif self.path == "/redir-same":
            self._send(302, b"", headers={"Location": "/ok"})
        elif self.path == "/redir-cross":
            self._send(302, b"", headers={"Location": "https://other.example.org/x"})
        elif self.path == "/flaky":
            n = _Handler.hits[self.path]
            if n == 1:
                self._send(503, b"busy", headers={"Retry-After": "0"})
            else:
                self._send(200, b"fine")
        else:
            self._send(404, b"nope")

    def _send(self, code, body, ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class _LoopbackAllowed(EgressPolicy):
    """测试专用：放行 127.0.0.1 与 http，以便本地 server 可用；socket 级检查单独测。"""

    def validate_url(self, url):
        return url

    def check_peer(self, ip_text, *, host=""):
        return None


def make(settings=None, policy=None):
    return HttpFetcher(settings or Settings(http_timeout_seconds=5, max_download_bytes=10000), policy or _LoopbackAllowed())


async def test_fetch_ok_and_cache(server):
    f = make()
    r = await f.fetch(f"{server}/ok")
    assert r.status == 200 and "hello world" in r.text() and not r.from_cache
    r2 = await f.fetch(f"{server}/ok")
    assert r2 is r and f.cache.hits == 1


async def test_size_limit(server):
    with pytest.raises(ToolError) as ei:
        await make().fetch(f"{server}/big")
    assert ei.value.code == ErrorCode.FETCH_FAILED and "上限" in ei.value.info.message


async def test_challenge_page(server):
    with pytest.raises(ToolError) as ei:
        await make().fetch(f"{server}/cf")
    assert ei.value.code == ErrorCode.NEEDS_HUMAN_VERIFICATION and "cloudflare-js" in ei.value.info.message


async def test_same_site_redirect_followed_cross_host_not(server):
    f = make()
    r = await f.fetch(f"{server}/redir-same")
    assert r.status == 200 and r.hops == 1 and r.final_url.endswith("/ok")
    r = await f.fetch(f"{server}/redir-cross")
    assert r.redirect_to == "https://other.example.org/x" and r.content == b""


async def test_robots_txt_is_not_checked(server):
    """D17：robots.txt 写了 Disallow: / 也照常抓取，而且根本不去请求它。"""
    _Handler.hits.pop("/robots.txt", None)
    r = await make().fetch(f"{server}/private/x")
    assert r.status == 200 and r.content == b"private ok"
    assert "/robots.txt" not in _Handler.hits


async def test_503_backoff_then_success(server):
    r = await make().fetch(f"{server}/flaky")
    assert r.status == 200 and r.content == b"fine"


async def test_socket_level_block_on_loopback(server):
    """真实策略下，即使 URL 粗筛被绕过（这里直接构造），连接后对端检查仍拦截 127.0.0.1。"""
    policy = EgressPolicy()
    policy.validate_url = lambda url: url  # 模拟粗筛失效 / rebinding
    f = HttpFetcher(Settings(http_timeout_seconds=5), policy)
    with pytest.raises(ToolError) as ei:
        await f.fetch(f"{server}/ok")
    assert ei.value.code == ErrorCode.SSRF_BLOCKED


def test_same_site_rules():
    assert _same_site("https://a.com/x", "https://www.a.com/y")
    assert not _same_site("https://a.com/x", "http://a.com/y")
    assert not _same_site("https://a.com/x", "https://a.com:8443/y")
    assert not _same_site("https://a.com/x", "https://u:p@a.com/y")
    assert not _same_site("https://a.com/x", "https://b.com/y")


async def test_fixture_urls_only_with_bench_switch(tmp_path, monkeypatch):
    """D20 实验样本：OWA_FIXTURE_URLS 开时 https://fixtures.invalid/<名> 读挂载目录；关着时照常走出站检查（假域名解析不了）。"""
    from owa_research.readers import http as h

    (tmp_path / "booths.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(h, "FIXTURE_DIR", tmp_path)
    on = HttpFetcher(Settings(fixture_urls=True), EgressPolicy())
    try:
        r = await on.fetch("https://fixtures.invalid/booths.pdf")
        assert r.content.startswith(b"%PDF") and r.content_type == "application/pdf"
        for bad in ("https://fixtures.invalid/../etc/passwd", "https://fixtures.invalid/nope.pdf", "https://fixtures.invalid/.x"):
            with pytest.raises(ToolError):
                await on.fetch(bad)
    finally:
        await on.aclose()
    off = HttpFetcher(Settings(http_timeout_seconds=2), EgressPolicy())
    try:
        with pytest.raises(ToolError):
            await off.fetch("https://fixtures.invalid/booths.pdf")
    finally:
        await off.aclose()
