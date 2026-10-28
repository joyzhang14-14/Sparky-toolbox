"""出站策略（规格第 10 节）：地址黑名单、域名规则、URL 校验、连接后对端 IP 检查。

- 进程内 URL 校验只是第一道粗筛；真正的拦截在 socket 连接事件（HTTP 路径）和 CONNECT 代理（浏览器路径）。
- 黑名单覆盖 IPv4 映射 IPv6、NAT64、6to4、localhost、*.internal、*.local、metadata、host.docker.internal。
- 可信服务例外（SearXNG、vLLM）只在各自客户端里放行，抓取器一律拒绝。
- 域名检查缓存只缓存"允许"；拒绝和失败每次重查；失败按拒绝处理。
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from owa_research.core.errors import ErrorCode, ToolError

IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "host.docker.internal", "searxng"}
_BLOCKED_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa", ".docker")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_6TO4 = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")
_ALLOW_CACHE_TTL = 300.0  # 短于页面缓存的 15 分钟


def is_public_ip(ip: IPAddr) -> bool:
    """只有真正的公网单播地址返回 True。"""
    if ip.version == 6:
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_public_ip(mapped)
        if ip in _NAT64 or ip in _NAT64_LOCAL:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            return is_public_ip(embedded)
        if ip in _6TO4:
            embedded = ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
            return is_public_ip(embedded)
        if ip in _TEREDO:
            return False
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 6 and ip.is_site_local)
        or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))  # CGNAT
        or (ip.version == 4 and ip in ipaddress.ip_network("0.0.0.0/8"))
        or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
        or (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"))
    )


def parse_ip(text: str) -> IPAddr | None:
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1]
    try:
        return ipaddress.ip_address(t)
    except ValueError:
        return None


def is_blocked_hostname(host: str) -> bool:
    h = host.rstrip(".").lower()
    if h in _BLOCKED_HOSTNAMES or h.endswith(_BLOCKED_SUFFIXES):
        return True
    ip = parse_ip(h)
    if ip is not None:
        return not is_public_ip(ip)
    # 十进制/八进制/十六进制整数形式的 IPv4（如 2130706433、0x7f000001、0177.0.0.1）
    if _looks_like_numeric_ipv4(h):
        return True
    return False


def _looks_like_numeric_ipv4(h: str) -> bool:
    """十进制/八进制/十六进制整数点分形式（正常域名含字母，整体全为数字形态即可疑）。"""
    parts = h.split(".")
    return 0 < len(parts) <= 4 and all(re.fullmatch(r"0x[0-9a-f]+|[0-9]+", p) for p in parts)


@dataclass
class DomainRule:
    host: str
    path_prefix: str = ""

    @classmethod
    def parse(cls, text: str) -> "DomainRule":
        t = text.strip().lower()
        if "://" in t:
            parts = urlsplit(t)
            return cls(parts.netloc, parts.path.rstrip("/"))
        host, _, path = t.partition("/")
        return cls(host, ("/" + path).rstrip("/") if path else "")

    def matches(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc.lower().split("@")[-1].split(":")[0]
        if not (host == self.host or host.endswith("." + self.host)):
            return False
        if not self.path_prefix:
            return True
        path = parts.path or "/"
        # 按路径段匹配：/a 只匹配 /a 本身或 /a/...，不匹配 /a-evil
        return path == self.path_prefix or path.startswith(self.path_prefix + "/")


@dataclass
class EgressPolicy:
    """研究工具 GET 读取层的域名策略；出站网络白名单不继承它。"""

    preferred: list[DomainRule] = field(default_factory=list)
    blocked: list[DomainRule] = field(default_factory=list)
    max_url_length: int = 2000
    _allow_cache: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_lists(cls, preferred: list[str] | None, blocked: list[str] | None, **kw) -> "EgressPolicy":
        return cls(
            preferred=[DomainRule.parse(d) for d in preferred or []],
            blocked=[DomainRule.parse(d) for d in blocked or []],
            **kw,
        )

    def is_preferred(self, url: str) -> bool:
        return any(r.matches(url) for r in self.preferred)

    def validate_url(self, url: str) -> str:
        """粗筛 + 规范化：长度上限、拒绝用户名密码、http 升级 https、黑名单主机、blocked_domains。返回规范化 URL。"""
        cached = self._allow_cache.get(url)
        if cached is not None and cached > time.monotonic():
            return self._normalized(url)
        if len(url) > self.max_url_length:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"URL 超过 {self.max_url_length} 字符", stage="egress")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ToolError(ErrorCode.SSRF_BLOCKED, f"只允许 http(s)，收到 {parts.scheme!r}", stage="egress")
        if "@" in parts.netloc:
            raise ToolError(ErrorCode.SSRF_BLOCKED, "拒绝带用户名/密码的 URL", stage="egress")
        host = parts.hostname or ""
        if not host:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "URL 缺少主机名", stage="egress")
        if is_blocked_hostname(host):
            raise ToolError(ErrorCode.SSRF_BLOCKED, f"目标主机 {host!r} 不允许访问", stage="egress")
        if parts.port is not None and parts.port not in (80, 443):
            raise ToolError(ErrorCode.SSRF_BLOCKED, f"只允许 80/443 端口，收到 {parts.port}", stage="egress")
        normalized = self._normalized(url)
        if any(r.matches(normalized) for r in self.blocked):
            raise ToolError(ErrorCode.SSRF_BLOCKED, f"域名在 blocked_domains 内: {host}", stage="egress")
        self._allow_cache[url] = time.monotonic() + _ALLOW_CACHE_TTL
        return normalized

    @staticmethod
    def _normalized(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme == "http":
            return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
        return url

    def check_peer(self, ip_text: str, *, host: str = "") -> None:
        """连接建立后对对端 IP 的检查；拒绝时抛 SSRF_BLOCKED。"""
        ip = parse_ip(ip_text)
        if ip is None or not is_public_ip(ip):
            raise ToolError(ErrorCode.SSRF_BLOCKED, f"{host or '目标'} 解析到非公网地址", stage="egress")
