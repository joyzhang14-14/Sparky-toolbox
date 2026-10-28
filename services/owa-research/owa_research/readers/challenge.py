"""Cloudflare/CAPTCHA 等挑战页识别（规格第 8 节）。只识别，不点击、不求解。

四层叠加，输出挑战类型而不是只报"失败"：
(1) HTTP 层：状态码 + 响应头；(2) DOM 层：标题与选择器；(3) 脚本特征；(4) 其他厂商（DataDome、PerimeterX、Akamai、Kasada）与空壳页启发式。
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_DOM_TITLE = re.compile(r"<title>\s*(Just a moment\.\.\.|DDoS-Guard|Attention Required!\s*\|\s*Cloudflare|Access denied|Please verify you are a human|Bot Verification)", re.I)
_DOM_SELECTORS = re.compile(r"(id=[\"']?(cf-challenge-running|challenge-spinner|turnstile-wrapper|challenge-form|cf-please-wait)|name=[\"']?cf-turnstile-response|class=[\"'][^\"']*\bcf-turnstile\b)", re.I)
_SCRIPT_HINTS = re.compile(r"(cType:\s*'(non-interactive|managed|interactive)'|challenges\.cloudflare\.com/turnstile|/cdn-cgi/challenge-platform/)", re.I)
_CF_TRACE = re.compile(r"/cdn-cgi/images/trace/(captcha|managed)/", re.I)
_DATADOME = re.compile(r"(datadome|dd\.js|captcha-delivery\.com)", re.I)
_PERIMETERX = re.compile(r"(_pxhd|px-captcha|perimeterx|human-challenge)", re.I)
_AKAMAI = re.compile(r"(ak_bmsc|_abck|akamai)", re.I)
_KASADA = re.compile(r"(kpsdk|x-kpsdk-|kasada)", re.I)
_GENERIC_CAPTCHA = re.compile(r"(g-recaptcha|hcaptcha\.com|recaptcha/api\.js|<title>[^<]*captcha)", re.I)


def detect_challenge(status: int, headers: Mapping[str, str], body: str | None) -> str | None:
    """返回挑战类型字符串，或 None（不是挑战页）。body 只需前若干 KB。"""
    h = {k.lower(): v for k, v in headers.items()}
    server = h.get("server", "").lower()
    text = body or ""
    head = text[:200_000]

    # (1) HTTP 层
    if "cloudflare" in server:
        if status in (503, 429) and ("challenge-form" in head or "cf-challenge" in head or "Just a moment" in head):
            return "cloudflare-js"
        if status == 403 and _CF_TRACE.search(head):
            return "cloudflare-managed"
        if h.get("cf-error-code") == "1020" or (status == 403 and "error code: 1020" in head.lower()):
            return "cloudflare-firewall"
        if h.get("cf-mitigated") == "challenge":
            return "cloudflare-managed"
    if "ddos-guard" in server or "__ddg1" in h.get("set-cookie", "").lower():
        return "ddos-guard"
    # (4) 其他厂商响应头
    if "x-datadome" in h or "x-dd-b" in h or "datadome" in h.get("set-cookie", "").lower():
        return "datadome"
    if "x-px" in h or "_pxhd" in h.get("set-cookie", ""):
        return "perimeterx"
    if "x-kpsdk-ct" in h or "x-kpsdk-cd" in h:
        return "kasada"
    if any(k.startswith("x-kpsdk") for k in h):
        return "kasada"

    # (2) DOM 层 / (3) 脚本特征
    m = _DOM_TITLE.search(head)
    if m or _DOM_SELECTORS.search(head):
        if "interactive" in head.lower() and "non-interactive" not in head.lower():
            return "cloudflare-interactive"
        if "managed" in head.lower() and "cType" in head:
            return "cloudflare-managed"
        if m and "ddos-guard" in m.group(1).lower():
            return "ddos-guard"
        return "cloudflare-js"
    if _SCRIPT_HINTS.search(head):
        return "cloudflare-js"
    if _DATADOME.search(head) and ("captcha" in head.lower() or status in (403, 429)):
        return "datadome"
    if _PERIMETERX.search(head) and status in (403, 429):
        return "perimeterx"
    if _KASADA.search(head) and status in (403, 429):
        return "kasada"
    if _AKAMAI.search(h.get("set-cookie", "")) and status in (403, 429) and len(head.strip()) < 2000:
        return "akamai"
    if _GENERIC_CAPTCHA.search(head) and status in (403, 429, 503):
        return "captcha"
    # 403/503 空壳页启发式：极短 HTML 且没有正文
    if status in (403, 503) and "<html" in head.lower() and len(re.sub(r"<[^>]+>", "", head).strip()) < 200:
        return "blocked-empty-page"
    return None
