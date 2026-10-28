"""浏览器路径的本地 CONNECT 代理（规格第 10 节）。

浏览器所有连接（含子资源、iframe、WebSocket）都经这里：
- 对每个域名解析一次，要求所有解析结果都是公网地址；
- 把连接钉在那个 IP 上，不按域名再解析；
- 只允许 80/443；拒绝时返回 403 加 `X-OWA-Egress: blocked`，浏览器侧识别后转成 SSRF_BLOCKED。
支持 CONNECT（TLS 隧道）和绝对形式的明文 HTTP 请求。
"""
from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import urlsplit

from owa_research.isolation.egress import is_blocked_hostname, is_public_ip, parse_ip

log = logging.getLogger("owa.egress_proxy")
BLOCK_RESPONSE = b"HTTP/1.1 403 Forbidden\r\nX-OWA-Egress: blocked\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_ALLOWED_PORTS = {80, 443}


async def resolve_pinned(host: str, port: int) -> str:
    """解析并要求所有地址公网；返回钉住的 IP 文本。失败抛 PermissionError。"""
    if port not in _ALLOWED_PORTS or is_blocked_hostname(host):
        raise PermissionError(f"{host}:{port} 不允许")
    ip = parse_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise PermissionError(f"{host} 非公网")
        return str(ip)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise PermissionError(f"{host} 解析失败") from None
    addrs = [parse_ip(i[4][0]) for i in infos]
    if not addrs or any(a is None or not is_public_ip(a) for a in addrs):
        raise PermissionError(f"{host} 解析到非公网地址")
    # 优先 IPv4
    addrs.sort(key=lambda a: a.version)
    return str(addrs[0])


class EgressProxy:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host, self._port = host, port
        self._server: asyncio.AbstractServer | None = None
        self.blocked = 0
        self.tunnels = 0

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    async def start(self) -> None:
        self._handlers: set[asyncio.Task] = set()
        self._server = await asyncio.start_server(self._spawn, self._host, self._port)

    def _spawn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        t = asyncio.ensure_future(self._handle(reader, writer))
        self._handlers.add(t)
        t.add_done_callback(self._handlers.discard)

    def allow_host(self, host: str) -> bool:
        """子类可覆盖：按域名放行（sparky-toolbox 的 allowlist 网络档位用）。默认全部放行，只做对端 IP 检查。"""
        return True

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for t in list(self._handlers):
                t.cancel()
            await asyncio.gather(*self._handlers, return_exceptions=True)
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote_w: asyncio.StreamWriter | None = None
        try:
            remote_w = await self._serve(reader, writer)
        finally:  # 取消（stop）或异常都要关掉两端连接，否则 wait_closed 挂起
            for w in (writer, remote_w):
                if w is not None:
                    w.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> asyncio.StreamWriter | None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        line = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        parts = line.split(" ")
        if len(parts) != 3:
            await self._block(writer)
            return None
        method, target, _ = parts
        try:
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                if not self.allow_host(host.strip("[]")):
                    raise PermissionError(f"域名不在放行名单：{host}")
                ip = await resolve_pinned(host.strip("[]"), int(port_s or 443))
                remote_r, remote_w = await asyncio.wait_for(asyncio.open_connection(ip, int(port_s or 443)), timeout=20)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                first = b""
            else:
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    raise PermissionError("只支持绝对形式的 http 请求")
                port = u.port or 80
                if not self.allow_host(u.hostname):
                    raise PermissionError(f"域名不在放行名单：{u.hostname}")
                ip = await resolve_pinned(u.hostname, port)
                remote_r, remote_w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=20)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                first = head.replace(line.encode("latin-1"), f"{method} {path} HTTP/1.1".encode("latin-1"), 1)
        except (PermissionError, ValueError, OSError, asyncio.TimeoutError) as e:
            log.info("blocked %s %s: %s", method, target, e)
            await self._block(writer)
            return None
        self.tunnels += 1
        if first:
            remote_w.write(first)
            await remote_w.drain()
        await asyncio.gather(_pipe(reader, remote_w), _pipe(remote_r, writer), return_exceptions=True)
        return remote_w

    async def _block(self, writer: asyncio.StreamWriter) -> None:
        self.blocked += 1
        try:
            writer.write(BLOCK_RESPONSE)
            await writer.drain()
        except OSError:
            pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except OSError:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    proxy = EgressProxy(port=0)
    await proxy.start()
    print(proxy.url, flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
