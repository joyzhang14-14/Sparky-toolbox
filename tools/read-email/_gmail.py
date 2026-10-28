"""Gmail API 只读取信（read-email 里走 Gmail API 的邮箱文件用，同文件夹的 server.py）。OAuth 凭证（.env 的 STB_GMAIL_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN，
scope gmail.readonly；用同文件夹的 gmail-auth.py 拿）用 credential() 读，只在工具进程里用。
查询串只由这里拼（时间 / 只看未读 / 过了字符白名单的发件人、标题词），模型没有查询语法可传。"""
from __future__ import annotations

import base64
import re
from html import unescape
from typing import Any

import httpx

from control_center.credential import credential

API = "https://gmail.googleapis.com/gmail/v1/users/me"


def token() -> str:
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": credential("STB_GMAIL_CLIENT_ID"), "client_secret": credential("STB_GMAIL_CLIENT_SECRET"),
        "refresh_token": credential("STB_GMAIL_REFRESH_TOKEN"), "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"刷新 Gmail 令牌失败：{r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", unescape(text)).strip()


def body(payload: dict[str, Any]) -> str:
    plain, html = [], []

    def walk(p: dict[str, Any]) -> None:
        data = (p.get("body") or {}).get("data")
        mime = p.get("mimeType", "")
        if data and mime == "text/plain":
            plain.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
        elif data and mime == "text/html":
            html.append(html_to_text(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")))
        for part in p.get("parts") or []:
            walk(part)

    walk(payload)
    return "\n".join(plain) if plain else "\n".join(html)


def count(account: str, query: str) -> int:
    """按查询串数有多少封（只翻 id 列表，不取正文）。"""
    with httpx.Client(headers={"Authorization": f"Bearer {token()}"}, timeout=60) as c:
        prof = c.get(f"{API}/profile").json()
        if prof.get("emailAddress", "").lower() != account.lower():
            raise RuntimeError(f"凭证对应账号 {prof.get('emailAddress')} 与要读的账号（{account}）不符")
        n, params = 0, {"q": query, "maxResults": 500}
        while True:
            r = c.get(f"{API}/messages", params=params)
            r.raise_for_status()
            page = r.json()
            n += len(page.get("messages") or [])
            if not page.get("nextPageToken"):
                return n
            params["pageToken"] = page["nextPageToken"]


def fetch(account: str, query: str, max_messages: int | None) -> list[dict[str, Any]]:
    """按查询串取信：[{from, date, subject, body, ts}]。凭证对应的账号必须等于 account（.env 的 STB_GMAIL_ACCOUNT）。
    max_messages 是数字：只取第一页里最新的这么多封。None：按 nextPageToken 翻页取回全部，
    哪一页或哪一封没取到就整次报错，不静默少取（已读位置不能越过没取到的邮件，D28）。"""
    with httpx.Client(headers={"Authorization": f"Bearer {token()}"}, timeout=60) as c:
        prof = c.get(f"{API}/profile").json()
        if prof.get("emailAddress", "").lower() != account.lower():
            raise RuntimeError(f"凭证对应账号 {prof.get('emailAddress')} 与要读的账号（{account}）不符")
        ids: list[dict[str, Any]] = []
        params: dict[str, Any] = {"q": query, "maxResults": 500 if max_messages is None else max_messages}
        while True:
            r = c.get(f"{API}/messages", params=params)
            if max_messages is None:
                r.raise_for_status()
            page = r.json()
            ids += page.get("messages") or []
            if max_messages is not None or not page.get("nextPageToken"):
                break
            params["pageToken"] = page["nextPageToken"]
        msgs = []
        for ref in ids[:max_messages]:
            r = c.get(f"{API}/messages/{ref['id']}", params={"format": "full"})
            if max_messages is None:
                r.raise_for_status()
            m = r.json()
            h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
            msgs.append({"from": h.get("from", ""), "date": h.get("date", ""), "subject": h.get("subject", ""),
                         "body": body(m.get("payload") or {}), "ts": int(m.get("internalDate", 0)) // 1000})
        return msgs



def mails(account: str, since_ts: int, unread_only: bool = False, sender: str = "", subject: str = "",
          newest: int | None = None) -> tuple[int, list[dict[str, Any]]]:
    """since_ts 之后的邮件（可只看未读，按发件人、标题词缩小）：(符合的封数, [{from, date, subject, body, ts}] 从旧到新)。
    newest 是数字：只取最新的这么多封；None：翻页取全（fetch）。发件人、标题词调用方已过字符白名单，查询串由这里拼。"""
    q = f"after:{since_ts}" + (" is:unread" if unread_only else "") + (f' from:"{sender}"' if sender else "") \
        + (f' subject:"{subject}"' if subject else "")
    msgs = sorted(fetch(account, q, newest), key=lambda m: m["ts"])     # Gmail 按新到旧给，读者按旧到新读
    return (count(account, q) if newest is not None else len(msgs)), msgs
