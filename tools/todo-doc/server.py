"""todo-doc 工具：读写 Joy 的 Obsidian 笔记库里的 UCI 文档（Syncthing 实时同步到 Mac，改完 1-2 秒就到）。
写入一律整篇换掉（同文件夹 _files.py 的 replace_text：临时文件 + 改名），不原地写：以 sparky 跑，只靠目录的权限。
笔记库 ~/Obsidian、能碰的子目录 UCI/、待办清单 UCI/UCI 新生待办清单.md：程序写死（PATHS 只放行 UCI/）。
读、加 / 勾待办、整篇写都开着；某个任务只能改哪些（比如只加 / 勾待办）写在任务的 system_prompt 里。路径用库内相对路径（如 UCI/UCI 新生待办清单.md）。
待办清单约定：`- [ ] 事项` 是没做的；完成写成 `- [x] ~~事项~~ ✅ 说明(日期)`，不删行；截止日期加粗写在事项里。
清单里的日期一律带年份（Joy 2026-09-27）：加 / 勾待办、整篇写待办清单时，新写进去的不带年份的日期（9/25、9 月 25 日、Sep 25）直接拒。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer

import sys
from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center.paths import host_tz
from _files import replace_text

ROOT = Path("~/Obsidian").expanduser().resolve()
PATHS = ["UCI"]
TODO = "UCI/UCI 新生待办清单.md"
MAX_CHARS = 200_000
OPEN = re.compile(r"^(\s*)- \[ \] (.*)$")
DONE = re.compile(r"^(\s*)- \[x\] ", re.I)
HEADING = re.compile(r"^## ")
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
YEARLESS = (      # 不带年份的日期：9/25（后面没有 /年份）、9 月 25 日（前面没有 2026 年）、Sep 25（后面没有 , 2026）
    re.compile(r"(?<![\d/.\-])(?:1[0-2]|0?[1-9])/(?:3[01]|[12]\d|0?[1-9])(?!\d|/\d|\.\d)"),
    re.compile(r"(?<![\d.])(?:1[0-2]|0?[1-9])\s*月\s*(?:3[01]|[12]\d|0?[1-9])\s*[日号]"),
    re.compile(r"\b" + _MON + r"\s+(?:3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?\b(?!,?\s*\d{4})"),
)
YEAR_HINT = "待办清单里的日期必须带年份，写成 2026-09-25 这样（范围写 2026-09-28–2026-09-29）"
# 截止日（todo-doc_list 的 due）：只认带年份的 2026-10-09 写法。先找明确表示截止的（日期 [时刻] 前、最迟 / 截止 / due / deadline 后面的日期），
# 没有再看整段加粗的只是一个日期（**2026-10-08**、**2026-10-08 19:00**）；都没有就空着，不猜（加粗里还有别的字的如 **#749（2026-09-26 到）** 不算）
ISO = r"(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)"
DUE_MARKED = (
    re.compile(r"(?<!\d)" + ISO + r"(?:\s*\d{1,2}:\d{2})?\s*\**\s*前"),
    re.compile(r"(?:最迟|截止|\bdue\b|\bdeadline\b)[^\d\n]{0,8}?" + ISO, re.I),
)
DUE_BOLD = re.compile(r"\*\*\s*" + ISO + r"(?:\s*\d{1,2}:\d{2})?\s*\*\*")

def vault_path(root: Path, rel: str) -> Path:
    """库内路径 → 宿主路径：去掉首尾的 / 再接到 root 下，解析 `..` 与符号链接。"""
    return (root / str(rel).strip("/")).resolve()


server = MCPServer("todo-doc", instructions="Joy 的 UCI 文档（Obsidian 笔记库 UCI/ 目录）；路径用库内相对路径")


def _host(rel: str) -> Path:
    """库内相对路径 → 宿主路径。校验放在 resolve 之后：`UCI/../Sparky/x.md` 这类 `..` 与符号链接解析完必须仍在某个允许的子目录里。"""
    host = vault_path(ROOT, rel)
    allowed = [vault_path(ROOT, p) for p in PATHS]
    if not any(host == a or a in host.parents for a in allowed):
        raise ValueError(f"路径 {rel} 不在允许范围 {PATHS} 内")
    return host


# ---- 纯文本操作（可单测）----
def yearless_dates(text: str) -> list[str]:
    """text 里不带年份的日期（原样），没有就是空列表。"""
    out = []
    for rx in YEARLESS:
        for m in rx.finditer(text):
            if not re.search(r"\d{4}\s*年\s*$", text[:m.start()]):
                out.append(m.group(0))
    return out


def require_years(new: str, old: str = "") -> None:
    """new 里比 old 多出来的不带年份的日期 → ValueError（old 里原有的不算，只拦新写进去的）。"""
    left = yearless_dates(old)
    bad = []
    for d in yearless_dates(new):
        if d in left:
            left.remove(d)
        else:
            bad.append(d)
    if bad:
        raise ValueError(f"日期没写年份：{'、'.join(dict.fromkeys(bad))}。{YEAR_HINT}")


def add_todo(text: str, item: str, section: str | None = None, sub: str | None = None) -> tuple[str, str]:
    """把 `- [ ] item` 加到 section（按 `## ` 标题子串匹配；没给或没匹配到就放到「📥 新增待办」节）。返回 (新文本, 说明)。"""
    require_years(f"{item}\n{sub or ''}")
    lines = text.split("\n")
    line = f"- [ ] {item.strip()}" + (f"\n  - {sub.strip()}" if sub else "")
    idx = None
    if section:
        for i, l in enumerate(lines):
            if HEADING.match(l) and section in l:
                idx = i
                break
    if idx is None:
        section = "## 📥 新增待办"
        for i, l in enumerate(lines):
            if l.startswith(section):
                idx = i
                break
        if idx is None:   # 新建节，放在「常用链接」前；没有就放末尾
            at = next((i for i, l in enumerate(lines) if l.startswith("## 🔗")), len(lines))
            lines[at:at] = [section, ""]
            idx = at
    # 节尾 = 下一个 ## 标题前的最后一个非空行之后
    end = next((i for i in range(idx + 1, len(lines)) if HEADING.match(lines[i])), len(lines))
    while end > idx + 1 and lines[end - 1].strip() == "":
        end -= 1
    lines[end:end] = [line]
    return "\n".join(lines), f"已加到「{lines[idx].lstrip('# ').strip()}」：{line.splitlines()[0]}"


def check_todo(text: str, match: str, note: str = "") -> tuple[str, str]:
    """把唯一包含 match 的未勾行改成 `- [x] ~~事项~~ ✅ note(日期)`。0 个或多个匹配都报错让 agent 换关键词。"""
    require_years(note)
    lines = text.split("\n")
    hits = [i for i, l in enumerate(lines) if OPEN.match(l) and match in l]
    if len(hits) != 1:
        cands = "\n".join(lines[i] for i in hits[:8])
        raise ValueError(f"匹配到 {len(hits)} 行未勾事项，需要唯一。换更具体的关键词。" + (f"\n候选：\n{cands}" if hits else ""))
    i = hits[0]
    m = OPEN.match(lines[i])
    assert m
    indent, body = m.group(1), m.group(2).strip()
    body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)   # 去加粗，避免 ~~**x**~~ 渲染问题
    stamp = time.strftime("%Y-%m-%d")
    lines[i] = f"{indent}- [x] ~~{body}~~ ✅ " + (f"{note.strip()}({stamp})" if note.strip() else stamp)
    return "\n".join(lines), f"已勾掉：{lines[i].strip()}"


def _date(m: re.Match[str]) -> date | None:
    """匹配到的年、月、日 → 日期；2026-02-30 这种不合法的为 None。"""
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def due_date(line: str) -> date | None:
    """一条待办的截止日：明确表示截止的日期优先（多个取行里先出现的），其次整段加粗的日期，取不准为 None。"""
    marked = [(m.start(), d) for rx in DUE_MARKED for m in rx.finditer(line) if (d := _date(m))]
    if marked:
        return min(marked)[1]
    return next((d for m in DUE_BOLD.finditer(line) if (d := _date(m))), None)


def open_items(text: str, today: date) -> list[dict[str, object]]:
    """没勾的事项（只认 `- [ ]` 行）：section 所在节（## 标题）、text 原文、due 截止日 YYYY-MM-DD（取不准为空）、days 距 today 的天数（负数 = 已过期，没有 due 为 null）。"""
    out, section = [], ""
    for l in text.split("\n"):
        if HEADING.match(l):
            section = l.lstrip("# ").strip()
        elif m := OPEN.match(l):
            due = due_date(m.group(2))
            out.append({"section": section, "text": m.group(2).strip(), "due": due.isoformat() if due else "",
                        "days": (due - today).days if due else None})
    return out


def _today() -> date:
    return datetime.now(ZoneInfo(host_tz())).date()


# ---- 工具 ----
@server.tool(name="todo-doc_files", description="列出 UCI 笔记目录里的文件（只能碰 UCI 目录；整个笔记库用 obsidian）")
def obsidian_list(path: str = "UCI") -> str:
    p = _host(path)
    if not p.is_dir():
        return f"不是目录：{path}（不存在或是文件；用 todo-doc_files 看上一级目录）"
    return "\n".join(f"{'d' if x.is_dir() else 'f'} {x.name}" for x in sorted(p.iterdir(), key=lambda x: x.name) if not x.name.startswith(".")) or "（空目录）"


@server.tool(name="todo-doc_read", description="读 UCI 目录下的一篇笔记（如 UCI 新生待办清单、账号与基础信息），超长截断")
def obsidian_read(path: str, max_chars: int = 30000) -> str:
    p = _host(path)
    if not p.is_file():
        return f"不是文件：{path}（不存在或是目录；用 todo-doc_files 看目录里有什么）"
    t = p.read_text(errors="replace")
    n = max(1, min(int(max_chars), MAX_CHARS))
    return t if len(t) <= n else t[:n] + f"\n…[截断：共 {len(t)} 字符，只给了前 {n}；" + (f"要看全文就把 max_chars 加大到 {min(len(t), MAX_CHARS)}]" if n < MAX_CHARS else "超过单次上限，后面的部分交子代理 isolated-reader 读]")


@server.tool(name="todo-doc_list", description="列出 UCI 新生待办清单里没勾的事项，带截止日 due 与距今天数 days")
def todo_list() -> str:
    today = _today()
    return json.dumps({"today": today.isoformat(), "items": open_items(_host(TODO).read_text(errors="replace"), today)}, ensure_ascii=False)


@server.tool(name="todo-doc_add", description="往 UCI 待办清单加一条待办：加待办用它，不要整篇改清单")
def todo_add(item: str, section: str = "", sub: str = "") -> str:
    p = _host(TODO)
    new, msg = add_todo(p.read_text(errors="replace"), item, section or None, sub or None)
    replace_text(p, new)
    return msg


@server.tool(name="todo-doc_check", description="勾掉 UCI 待办清单里的一条（标成已完成，不删行）")
def todo_check(match: str, note: str = "") -> str:
    p = _host(TODO)
    new, msg = check_todo(p.read_text(errors="replace"), match, note)
    replace_text(p, new)
    return msg


@server.tool(name="todo-doc_write", description="整篇覆盖写 UCI 目录下的一篇笔记；只在 todo-doc_add / todo-doc_check 做不到时用")
def obsidian_write(path: str, content: str) -> str:
    p = _host(path)
    if p == _host(TODO):              # 待办清单：新写进去的日期必须带年份
        require_years(content, p.read_text(errors="replace") if p.is_file() else "")
    p.parent.mkdir(parents=True, exist_ok=True)
    replace_text(p, content)
    return f"已写入 {path}（{len(content)} 字符）"


if __name__ == "__main__":
    server.run("stdio")
