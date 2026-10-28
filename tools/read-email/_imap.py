"""IMAP 只读取信（read-email 里走 IMAP 的邮箱文件用，同文件夹的 server.py）。
- 登录后先发一次 IMAP ID（RFC 2971）：网易不发就回 "Unsafe Login"；Gmail 不强制，发了无害。imaplib 没有 id()，用 _simple_command 手发，
  而且 ID 不在 imaplib 的命令状态表里，不注册就 KeyError: 'ID'（登录后 state 是 AUTH，readonly select 之后是 SELECTED）。
- readonly 选 INBOX、正文一律 BODY.PEEK[]：绝不把 Joy 邮箱里的邮件标成已读。
- SINCE 只到天：粗筛后按 INTERNALDATE 给每封精确时间（ts），由调用方精筛。发件人、标题词在本地按解码后的 From / Subject 筛（中文不用走 IMAP 的字符集搜索）。
密码（163 的客户端授权码、Gmail 的应用专用密码）由调用方经 control_center.credential 读来传入，不写日志、不回显。"""
from __future__ import annotations

import calendar
import email
import email.policy
import imaplib
import re
import time
from html import unescape
from typing import Any

PORT = 993
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
ID_ARGS = '("name" "sparky-toolbox" "version" "1.0" "vendor" "stb" "support" "local self-hosted mail reader")'
imaplib.Commands["ID"] = ("AUTH", "SELECTED")


class MailError(RuntimeError):
    """连不上、登录被拒、搜索或取信失败（消息里不含密码）。"""


def _internaldate(raw: bytes) -> int:
    """INTERNALDATE "01-Oct-2026 14:00:00 +0800" → epoch 秒；解析不出来返回 0（这封算旧邮件丢掉）。"""
    m = re.search(rb'INTERNALDATE "?(\[?)(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]?"?', raw)
    if not m:
        return 0
    _b, d, mon, y, hh, mm, ss, off = m.groups()
    mo = MONTHS.get(mon.decode().lower())
    if not mo:
        return 0
    epoch = calendar.timegm((int(y), mo, int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    sign = 1 if off[:1] == b"+" else -1
    return epoch - sign * (int(off[1:3]) * 3600 + int(off[3:5]) * 60)


def _raw_of(fetched) -> bytes:
    """imaplib 对带字面量的 FETCH 返回 [(头部, 报文), b')']：拼回一整段再看。"""
    chunks: list[bytes] = []
    for part in fetched or []:
        if isinstance(part, tuple):
            chunks.extend(x for x in part if isinstance(x, bytes))
        elif isinstance(part, bytes):
            chunks.append(part)
    return b"".join(chunks)


def _literal(raw: bytes) -> bytes:
    """IMAP FETCH 响应里 {...字节数}<报文> 的那段报文；按声明的长度截，别把收尾的 ) 混进正文。
    真 imaplib 会把 {...} 后面的 \r\n 吃掉，所以那个行尾是可选的。"""
    m = None
    for m in re.finditer(rb"\{(\d+)\}(?:\r\n)?", raw):
        pass
    if not m:
        return raw
    n = int(m.group(1))
    return raw[m.end(): m.end() + n]


def _text_of(msg: Any) -> str:
    """MIME → 纯文本：优先 text/plain，其次把 text/html 去标签；附件正文不要。"""
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_content_disposition() == "attachment":
            continue
        mime = part.get_content_type()
        if mime not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except Exception:  # noqa: BLE001 — 坏编码的邮件不该让整批失败
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        body = body if isinstance(body, str) else str(body)
        (plain if mime == "text/plain" else html).append(body)
    if plain:
        return "\n".join(plain).strip()
    joined = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html))
    return re.sub(r"[ \t]+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", joined))).strip()


def _fetch(box: Any, uid: bytes, what: str) -> bytes:
    typ, fetched = box.uid("fetch", uid, what)
    raw = _raw_of(fetched)
    if typ != "OK" or not raw:
        raise MailError(f"取信失败（uid {uid.decode()}）：{typ} {str(fetched)[:200]}")
    return raw


def mails(host: str, user: str, password: str, since_ts: int, unread_only: bool = False,
          sender: str = "", subject: str = "", newest: int | None = None) -> tuple[int, list[dict[str, Any]]]:
    """since_ts 那天起（SINCE 只到天）INBOX 里的邮件，按发件人、标题词（不分大小写的子串）筛：(符合的封数, [{from, subject, body, ts}] 从旧到新)。
    newest 是数字：只取最新的这么多封的正文；None：全部取，哪一封没取到就整次报错（已读位置不能越过没取到的邮件）。"""
    try:
        box = imaplib.IMAP4_SSL(host, PORT, timeout=60)
    except OSError as e:                     # DNS、连不上、TLS 握手失败
        raise MailError(f"连不上 {host}:{PORT}：{e}") from None
    try:
        box.login(user, password)
        box._simple_command("ID", ID_ARGS)          # noqa: SLF001 — imaplib 没有 id()
        box.select("INBOX", readonly=True)
        since_day = time.strftime("%d-%b-%Y", time.localtime(since_ts))
        typ, data = box.uid("search", None, *(["UNSEEN"] if unread_only else []), "SINCE", since_day)
        if typ != "OK":
            raise MailError(f"搜索失败：{typ} {str(data)[:200]}")
        uids = data[0].split() if data and data[0] else []
        if sender or subject:                 # 先只取 From / Subject 两行来筛
            keep = []
            for uid in uids:
                h = email.message_from_bytes(_literal(_fetch(box, uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")), policy=email.policy.default)
                if sender.lower() in str(h.get("From") or "").lower() and subject.lower() in str(h.get("Subject") or "").lower():
                    keep.append(uid)
            uids = keep
        out = []
        for uid in uids if newest is None else uids[-newest:]:      # UID 越大越新
            raw = _fetch(box, uid, "(UID INTERNALDATE BODY.PEEK[])")   # PEEK：不把邮件标成已读
            p = email.message_from_bytes(_literal(raw), policy=email.policy.default)
            ts = _internaldate(raw)
            out.append({"from": str(p.get("From") or ""), "subject": str(p.get("Subject") or ""), "body": _text_of(p), "ts": ts, "uid": int(uid)})
        out.sort(key=lambda m: (m["ts"], m["uid"]))
        return len(uids), out
    except (imaplib.IMAP4.error, OSError) as e:      # 授权码 / 应用专用密码不对、Unsafe Login、断线
        raise MailError(f"登录/取信出错：{e}") from None
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001 — 收尾断连失败不该盖住真正的错误
            pass
