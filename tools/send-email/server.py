"""send-email 工具：用宿主上的 himalaya 发一封 HTML 邮件。
- 发件人：himalaya 配置里的默认账号（只有一个 himalaya 凭证）的 display-name 与 email（from_address）。
- 收件人 / 抄送：函数参数 to / cc，但模型不用填也改不了——tool.yaml 的 pre_call hook（recipients_hook.py，以平台身份跑）调用前按同文件夹的
  recipients.txt 改写；hook 也核对正文链接必须来自本次运行的研究来源（固定行为）。
himalaya 用默认配置（~/.config/himalaya/config.toml，HOME 是平台用户的家目录），密码由配置里的 password.command 从 .env 读。
STB_TEST_EMAIL_DRYRUN=<文件>：不真发，把邮件原文追加到这个文件（测试用）。
"""

import os
import shutil
import subprocess
import time
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from mcp.server.mcpserver import MCPServer

import sys
from pathlib import Path
from control_center.tool_kit import platform_fault

DRYRUN = os.environ.get("STB_TEST_EMAIL_DRYRUN")
MAX_HTML = 200_000
HIMALAYA = shutil.which("himalaya") or os.path.expanduser("~/.local/bin/himalaya")

HIMALAYA_CONFIG = Path(os.environ.get("STB_TEST_HIMALAYA_CONFIG") or "~/.config/himalaya/config.toml").expanduser()

server = MCPServer("send-email", instructions="发一封 HTML 邮件；收件人由平台按 recipients.txt 定")


def from_address() -> str:
    """发件人：himalaya 配置里 default = true 的账号（没标就是唯一的那个）的「display-name <email>」。读不到 / 不止一个又没标默认就报平台故障。"""
    import tomllib

    try:
        accounts = tomllib.loads(HIMALAYA_CONFIG.read_text()).get("accounts") or {}
    except (OSError, ValueError) as e:
        raise platform_fault(f"读不了 himalaya 的配置 {HIMALAYA_CONFIG}：{e}") from None
    picked = [a for a in accounts.values() if isinstance(a, dict) and a.get("default")] or (list(accounts.values()) if len(accounts) == 1 else [])
    if not picked or not picked[0].get("email"):
        raise platform_fault(f"himalaya 的配置 {HIMALAYA_CONFIG} 里找不到默认账号（default = true）的 email")
    a = picked[0]
    return formataddr((str(a.get("display-name") or ""), str(a["email"])))


def build(subject: str, html: str, to: list[str], cc: list[str], sender: str) -> bytes:
    if not to:
        raise ValueError("没有收件人：收件人由平台按 tools/send-email/recipients.txt 填（pre_call hook），这个文件里没有 to: 行，请 Joy 补上")
    subject = subject.strip()
    if not subject or any(c in subject for c in "\r\n"):
        raise ValueError("subject 不能为空、不能换行")
    if len(html) > MAX_HTML:
        raise ValueError(f"html 超过 {MAX_HTML} 字符")
    name, addr = parseaddr(sender)
    heads = [
        ("From", formataddr((str(Header(name, "utf-8")), addr)) if name else addr),
        ("To", ", ".join(to)),
        *([("Cc", ", ".join(cc))] if cc else []),
        ("Subject", Header(subject, "utf-8").encode()),
        ("Date", formatdate(localtime=True)),
        ("Message-ID", make_msgid(domain=addr.split("@")[-1] or "sparky")),
        ("MIME-Version", "1.0"),
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Transfer-Encoding", "8bit"),
    ]
    return ("\r\n".join(f"{k}: {v}" for k, v in heads) + "\r\n\r\n" + html.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode()


from pydantic import BaseModel, Field


class DigestItem(BaseModel):
    date: str = Field(description="M/DD")
    zh: str = Field(description="中文标题")
    en: str = Field(description="英文原标题")
    url: str = Field(description="原文网址")
    source: str = Field(description="来源简称")
    stars: int = Field(default=0, ge=0, le=3, description="星数 0–3，没有就 0")
    type: str = Field(default="", description="AI Models：类型")
    org: str = Field(default="", description="AI Models：来源机构")
    innovation: str = Field(default="", description="AI Models：创新点（中文一句话）")
    use_case: str = Field(default="", description="AI Models：使用场景（中文一句话）")


class DigestSection(BaseModel):
    name: str = Field(description="板块标题")
    items: list[DigestItem]


THEMES = [  # (名字, 背景, 正文, 标题色, 小标题色, 链接色, 次要色, 分隔线, 卡片底色, 字体)
    ("ember",     "#1a1a1a", "#e0e0e0", "#ff8c00", "#7eb8f7", "#7eb8f7", "#888",    "#444",    "#222",    "sans-serif"),
    ("newsprint", "#fdfbf7", "#2b2b2b", "#b3261e", "#1a4d8f", "#1a4d8f", "#8a8a8a", "#e4ded3", "#fffdf8", "Georgia,serif"),
    ("midnight",  "#0f1b2d", "#dbe4f0", "#5ee6c1", "#f5c35b", "#8fc7ff", "#7d8ba1", "#233650", "#16263d", "-apple-system,Helvetica,sans-serif"),
    ("violet",    "#ffffff", "#1d1d1f", "#6e3ff3", "#0a7a5a", "#6e3ff3", "#999",    "#ececec", "#faf8ff", "-apple-system,Helvetica,sans-serif"),
    ("parchment", "#f4f1ea", "#33302b", "#2f5d50", "#9a4d1f", "#2f5d50", "#8c857a", "#d9d2c3", "#faf7f0", "Charter,Georgia,serif"),
    ("terminal",  "#121212", "#eaeaea", "#e5484d", "#f2c94c", "#56ccf2", "#8a8a8a", "#2e2e2e", "#1b1b1b", "Menlo,monospace"),
    ("sakura",    "#fff7f9", "#3a2b30", "#d6336c", "#7048e8", "#c2255c", "#a6949a", "#f3d9e1", "#ffffff", "-apple-system,'PingFang SC',sans-serif"),
    ("forest",    "#10231c", "#dfeee6", "#9ae6b4", "#f6e05e", "#90cdf4", "#7fa393", "#24453a", "#15302a", "Georgia,serif"),
    ("nordic",    "#eceff4", "#2e3440", "#5e81ac", "#bf616a", "#5e81ac", "#8a93a6", "#d8dee9", "#f8f9fb", "-apple-system,Helvetica,sans-serif"),
    ("solarized", "#fdf6e3", "#586e75", "#cb4b16", "#268bd2", "#2aa198", "#93a1a1", "#eee8d5", "#fffbee", "Menlo,monospace"),
    ("dracula",   "#282a36", "#f8f8f2", "#ff79c6", "#8be9fd", "#bd93f9", "#6272a4", "#44475a", "#2f3140", "-apple-system,Helvetica,sans-serif"),
    ("ocean",     "#f0f7fb", "#1b2b36", "#0b6e99", "#e76f51", "#0b6e99", "#7a8d99", "#cfe3ee", "#ffffff", "Avenir,Helvetica,sans-serif"),
    ("mono",      "#ffffff", "#111111", "#111111", "#555555", "#111111", "#999999", "#dddddd", "#f7f7f7", "'Helvetica Neue',Helvetica,sans-serif"),
    ("sunset",    "#2b1b2f", "#f3e6ee", "#ffb86b", "#ff6f91", "#ffc75f", "#a58ca8", "#44304a", "#352338", "Georgia,serif"),
    ("matcha",    "#f5f8ef", "#2d3a24", "#5b8c2a", "#b0532d", "#4a7a1f", "#8a9680", "#dde6cf", "#ffffff", "-apple-system,'PingFang SC',sans-serif"),
    ("slate",     "#1e232b", "#d5dbe3", "#79c0ff", "#ffa657", "#79c0ff", "#8b949e", "#30363d", "#252b34", "-apple-system,Helvetica,sans-serif"),
]
LAYOUTS = ["card", "bar", "numbered", "compact", "badge"]


def render_digest(title: str, intro: str, sections: list[dict], seed: int | None = None) -> str:
    """结构化条目 → HTML。每次发送随机一套配色和一种版式（seed 只给测试用）。"""
    import random
    from html import escape as e
    rnd = random.Random(seed) if seed is not None else random.SystemRandom()
    _, bg, fg, h1, h2, a, mute, line, card_bg, font = rnd.choice(THEMES)
    layout = rnd.choice(LAYOUTS)
    out = [f'<html><body style="margin:0;background:{bg}"><div style="font-family:{font};max-width:640px;margin:auto;padding:24px;background:{bg};color:{fg};line-height:1.7">',
           f'<h2 style="color:{h1};border-bottom:2px solid {line};padding-bottom:10px;margin-top:0">{e(title)}</h2>']
    if intro:
        out.append(f'<p style="color:{mute};margin-top:-4px">{e(intro)}</p>')
    for sec in sections:
        out.append(f'<h3 style="color:{h2};margin:28px 0 8px">{e(str(sec.get("name", "")))}</h3>')
        for n, it in enumerate(sec.get("items") or [], 1):
            stars = " " + "⭐" * int(it.get("stars") or 0) if it.get("stars") else ""
            link = f'<a href="{e(str(it.get("url", "")), quote=True)}" style="color:{a};text-decoration:none">{e(str(it.get("source", "")))}</a>'
            head = f'<strong>{e(str(it.get("zh", "")))}{stars}</strong>'
            extra = [f"{label}：{e(str(it[k]))}" for k, label in (("type", "📌 类型"), ("org", "🏛️ 来源机构"), ("innovation", "💡 创新点"), ("use_case", "🎯 使用场景")) if it.get(k)]
            date = f'<small style="color:{mute}">{e(str(it.get("date", "")))}</small>'
            en = f'<span style="color:{mute}">{e(str(it.get("en", "")))}</span>'
            if layout == "card":
                body = [date, head, en, f"🔗 {link}", *extra]
                out.append(f'<p style="padding:12px 14px;margin:10px 0;border:1px solid {line};border-radius:10px;background:{card_bg}">' + "<br>".join(body) + "</p>")
            elif layout == "bar":
                body = [date, head, en, f"🔗 {link}", *extra]
                out.append(f'<p style="margin:12px 0;padding-left:12px;border-left:3px solid {h2}">' + "<br>".join(body) + "</p>")
            elif layout == "numbered":
                body = [f'{head} {date}', en, f"🔗 {link}", *extra]
                out.append(f'<table role="presentation" style="margin:10px 0;border-collapse:collapse"><tr><td style="vertical-align:top;padding-right:10px;color:{h1};font-size:20px;font-weight:700">{n:02d}</td>'
                           f'<td style="vertical-align:top">' + "<br>".join(body) + "</td></tr></table>")
            elif layout == "compact":
                body = [f'{date} · {head} · {link}', f'<span style="font-size:13px">{en}</span>', *[f'<span style="font-size:13px">{x}</span>' for x in extra]]
                out.append(f'<p style="margin:8px 0;padding-bottom:8px;border-bottom:1px dashed {line}">' + "<br>".join(body) + "</p>")
            else:  # badge
                badge = f'<span style="display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;background:{h2};color:{bg}">{e(str(it.get("source", "")))}</span>'
                body = [f'{badge} {date}', head, en, f'🔗 <a href="{e(str(it.get("url", "")), quote=True)}" style="color:{a}">原文</a>', *extra]
                out.append(f'<p style="padding:12px 14px;margin:10px 0;border-radius:10px;background:{card_bg};border:1px solid {line}">' + "<br>".join(body) + "</p>")
        out.append(f'<hr style="border:none;border-top:1px solid {line};margin-top:20px">')
    out.append(f'<p style="color:{mute};font-size:12px">Sparky · {time.strftime("%Y-%m-%d %H:%M")}</p></div></body></html>')
    return "".join(out)


@server.tool(name="send-email_send_digest", description=(
    "发一封简报类邮件：你给结构化内容，工具生成 HTML。subject 写完整标题；intro 是标题下一句导语；"
    "sections 是板块列表 {name, items}；收件人由平台填，to / cc 不用写。正文链接必须来自本次研究的来源"))
def send_digest(subject: str, title: str, sections: list[DigestSection], intro: str = "", to: list[str] | None = None, cc: list[str] | None = None) -> str:
    if not sections:
        raise ValueError("sections 不能为空")
    return send(subject, render_digest(title, intro, [x.model_dump() if isinstance(x, BaseModel) else x for x in sections]), to, cc)


@server.tool(name="send-email_send", description="发一封 HTML 邮件：给 subject（完整标题）和完整的 html 正文，写好再发；收件人由平台填，to / cc 不用写。正文链接必须来自本次研究的来源")
def send(subject: str, html: str, to: list[str] | None = None, cc: list[str] | None = None) -> str:
    to, cc = [str(x) for x in to or []], [str(x) for x in cc or []]
    raw = build(subject, html, to, cc, from_address())
    if DRYRUN:
        with open(DRYRUN, "ab") as f:
            f.write(raw + b"\n\n=====\n\n")
    else:
        cmd = [HIMALAYA, "message", "send"]                 # himalaya 的默认账号（同 from_address）
        r = subprocess.run(cmd, input=raw, capture_output=True, timeout=120)
        if r.returncode != 0:
            raise platform_fault(f"himalaya 发送失败（退出码 {r.returncode}），邮件没发出去：{(r.stderr or r.stdout).decode(errors='replace')[-500:]}")
    return f"已发送给 {', '.join(to)}" + (f"（抄送 {', '.join(cc)}）" if cc else "") + ("（dry-run）" if DRYRUN else "")


if __name__ == "__main__":
    server.run("stdio")
