import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from owa_research.isolation.egress_proxy import EgressProxy, resolve_pinned


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture(scope="module")
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


@pytest.fixture
async def proxy():
    p = EgressProxy()
    await p.start()
    yield p
    await p.stop()


async def test_resolve_pinned_rejects_private_and_bad_ports():
    for host, port in [("127.0.0.1", 80), ("localhost", 443), ("host.docker.internal", 8000), ("example.com", 8080), ("[::ffff:10.0.0.1]", 443)]:
        with pytest.raises(PermissionError):
            await resolve_pinned(host.strip("[]"), port)


async def test_proxy_blocks_loopback_plain_http(proxy, local_server):
    async with httpx.AsyncClient(proxy=proxy.url, timeout=5) as c:
        r = await c.get(f"http://127.0.0.1:{local_server}/")
    assert r.status_code == 403 and r.headers.get("x-owa-egress") == "blocked"
    assert proxy.blocked == 1


async def test_proxy_blocks_connect_to_loopback(proxy, local_server):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(f"CONNECT 127.0.0.1:{local_server} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    line = await reader.readline()
    assert b"403" in line
    writer.close()


async def test_proxy_allows_public_connect(proxy):
    """真实外网：example.com:443 经代理应建立隧道。网络不可用时跳过。"""
    try:
        async with httpx.AsyncClient(proxy=proxy.url, timeout=15) as c:
            r = await c.get("https://example.com/")
    except httpx.HTTPError as e:
        pytest.skip(f"网络不可用: {e}")
    assert r.status_code == 200 and proxy.tunnels == 1
