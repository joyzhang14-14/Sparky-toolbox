"""读 / 改 USER.md 与 memory.md 的规则：memory 工具改 memory.md（直接改）与 USER.md（Joy 在审批卡上同意后写）都走这里。
- 文件在 Joy 的 Obsidian USER/（docs_dir；程序写死 ~/Obsidian/USER，工具的 HOME 是平台用户的家目录；测试用 STB_TEST_USER_DIR 换到临时目录）。
  任务要不要把它们注入上下文写在任务 yaml 的 memory 清单里（平台的 control_center/memory_docs.py 只读注入），和这里无关。
- 行格式 `YYYY-MM-DD 内容 <!-- origin: <来源>[; n: N; evidence: …] -->`：来源是 owner（Joy 自己写的）或写这行的任务名。
- 每次写入：锁下重读、逐行校验、扫密钥、查上限（LIMITS；memory.md 只算正文：日期 + 内容，来源标记和证据不占额度）、先备份，再整篇换掉。
- memory.md 的行可以带证据：n = 被 Joy 的私信确认过几天，evidence = 最近 MAX_EVIDENCE 条原话（写入时只读核对，对不上就拒：quote_seen——
  Joy 的话只认 dm-assistant 的对话库 data/messages/dm.sqlite3 里 role owner 的行、任务消息库 tasks.sqlite3 里 role answer 的行（Joy 在任务问题卡上的回答），
  和卡片记录 data/hub/approvals/ 里 Joy 处理卡片时写的字（如「拒绝，你应该…」填的意见）；output、status 是模型 / 平台写的，不算）。
  两个库 sparky 也能写，所以核对只防模型犯傻、防不了恶意工具；卡片记录只有中枢能写。"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from _files import replace_text
from control_center import paths, threat_patterns

FILES = {"user": "USER.md", "memory": "memory.md"}
LIMITS = {"user": 6000, "memory": 5000}
ORIGIN_NAME = r"[a-z0-9][a-z0-9_-]{0,40}"                                       # 来源：owner 或任务名（旧行的 agent / dream 也合这个格式）
ORIGIN_RE = re.compile(rf"\s*<!--\s*origin:\s*({ORIGIN_NAME})\s*(?:;\s*([^<>]*?))?\s*-->\s*$")   # 第 2 组：n、evidence 等附加字段
EVIDENCE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})「([^「」]*)」")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.*)$")
EVIDENCE_DBS = (("dm.sqlite3", "owner"), ("tasks.sqlite3", "answer"))           # 核对原话只读打开的库（data/messages/ 下）与认的 role：Joy 的私信、Joy 在任务问题卡上的回答
MAX_LINE = 400
MAX_EVIDENCE = 3                                                                # 每条最多留几条原话（最近的）
QUOTE_MIN, QUOTE_MAX = 2, 60


def docs_dir() -> Path:
    """USER.md / memory.md 所在目录：Joy 的 Obsidian USER/（测试经 STB_TEST_USER_DIR 换掉）。调用时才算。"""
    return Path(os.environ.get("STB_TEST_USER_DIR") or "~/Obsidian/USER").expanduser()


def target_path(target: str) -> Path:
    if target not in FILES:
        raise ValueError(f"记忆文件只有 user（USER.md）和 memory（memory.md）：{target}")
    return docs_dir() / FILES[target]


def read_lines(target: str) -> list[str]:
    try:
        return [l for l in target_path(target).read_text().splitlines() if l.strip()]
    except OSError:
        return []


def scan(text: str) -> str | None:
    """命中的威胁模式名（control_center/threat_patterns.py），否则 None：写入前扫一次，读出来时每行再扫。"""
    return threat_patterns.first_threat(text)


def body(line: str) -> str:
    """一行的正文：去掉行首日期与行尾 origin 标记。"""
    m = DATE_RE.match(ORIGIN_RE.sub("", line))
    return (m.group(2) if m else ORIGIN_RE.sub("", line)).strip()


def meta(line: str) -> dict[str, Any]:
    """一行的日期、来源、确认次数 n（没写 = 1）、证据 [(日期, 原话)]。"""
    m, o = DATE_RE.match(ORIGIN_RE.sub("", line)), ORIGIN_RE.search(line)
    fields = {k.strip(): v.strip() for k, v in (f.split(":", 1) for f in (o.group(2) or "").split(";") if ":" in f)} if o else {}
    try:
        n = max(1, int(fields.get("n", 1)))
    except ValueError:
        n = 1
    return {"date": m.group(1) if m else "", "origin": o.group(1) if o else "", "n": n,
            "evidence": EVIDENCE_RE.findall(fields.get("evidence", ""))}


def clean_quote(q: str) -> str:
    """原话里去掉会弄坏行格式的符号（<>、|、;、「」），空白压成一个空格。"""
    return " ".join(re.sub(r"[<>|;「」]", " ", str(q)).split())


def _card_texts(start: float) -> list[str]:
    """那一天 Joy 处理卡片时写的字：data/hub/approvals/<卡片>.json 的 resolved.value（字符串，或映射里的字符串值，如拒绝意见 feedback）；
    卡片作废（obsolete，平台收的）不算。处理卡片时文件会重写，比那天早的文件不用读。"""
    out: list[str] = []
    for p in paths.APPROVALS.glob("*.json") if paths.APPROVALS.is_dir() else []:
        try:
            if p.stat().st_mtime < start:
                continue
            r = json.loads(p.read_text()).get("resolved") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if r.get("action") == "obsolete" or not start <= float(r.get("at") or 0) < start + 86400:
            continue
        v = r.get("value")
        out += [v] if isinstance(v, str) else [x for x in v.values() if isinstance(x, str)] if isinstance(v, dict) else []
    return out


def quote_seen(day: str, quote: str) -> bool:
    """那一天 Joy 说过的话里有没有这段原话（空白和大小写不计）：只读打开 EVIDENCE_DBS（dm-assistant 的私信、任务问题卡上的回答），
    再加那天他处理卡片时写的字（_card_texts）；没有库算没有。"""
    start = datetime.fromisoformat(day).timestamp()
    rows = []
    for name, role in EVIDENCE_DBS:
        db = paths.MESSAGES / name
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            rows += con.execute("SELECT text FROM messages WHERE role = ? AND ts >= ? AND ts < ?", (role, start, start + 86400)).fetchall()
        finally:
            con.close()
    q = clean_quote(quote).lower()
    return any(q in clean_quote(t).lower() for t in [t for (t,) in rows] + _card_texts(start))


def _size(target: str, text: str) -> int:
    """算上限用的字数：memory.md 只算注入给模型的正文（日期 + 内容，每行一个换行），USER.md 算整个文件。"""
    if target != "memory":
        return len(text)
    return sum(len(f"{meta(l)['date']} {body(l)}") + 1 for l in text.splitlines() if l.strip())


def usage(target: str) -> tuple[int, int]:
    p = target_path(target)
    return (_size(target, p.read_text()) if p.exists() else 0), LIMITS[target]


def _lock():
    """写记忆文档的锁 data/tool_state/memory/.lock：只读打开就能 flock（以 sparky 跑的 memory 工具对这个目录只有读权限）；没有就建。"""
    paths.MEMORY_STATE.mkdir(parents=True, exist_ok=True)
    f = open(os.open(paths.MEMORY_STATE / ".lock", os.O_RDONLY | os.O_CREAT, 0o664))
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _backup(p: Path) -> None:
    if p.exists():
        (paths.MEMORY_STATE / "backups").mkdir(parents=True, exist_ok=True)   # 备份留在工具状态目录，不进笔记库
        shutil.copy(p, paths.MEMORY_STATE / "backups" / f"{p.name}.bak.{time.time_ns()}")


def _check_lines(p: Path) -> list[str]:
    """锁下重读并解析；每行必须是「日期 内容 <!-- origin -->」。解析失败抛 ValueError。"""
    if not p.exists():
        return []
    lines = []
    for i, l in enumerate(p.read_text().splitlines(), 1):
        if not l.strip():
            continue
        if not DATE_RE.match(l) or not ORIGIN_RE.search(l):
            raise ValueError(f"{p.name} 第 {i} 行不符合格式：{l[:60]}")
        lines.append(l)
    return lines


def format_line(text: str, origin: str, day: date | None = None, n: int = 1, evidence: list[tuple[str, str]] | None = None) -> str:
    text = " ".join(text.split())
    extra = (f"; n: {n}" if n > 1 else "") + (f"; evidence: {' | '.join(f'{d}「{q}」' for d, q in evidence)}" if evidence else "")
    return f"{(day or date.today()).isoformat()} {text} <!-- origin: {origin}{extra} -->"


def _day(value: Any) -> str:
    """证据日期：YYYY-MM-DD，或私信里的 MM-DD（补成不晚于今天的最近那个）；不写 = 今天。"""
    s, today = str(value or "").strip(), date.today()
    try:
        if not s:
            return today.isoformat()
        if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
            d = date(today.year, *map(int, s.split("-")))
            return (d if d <= today else d.replace(year=today.year - 1)).isoformat()
        d = date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"证据日期要写 YYYY-MM-DD 或 MM-DD：{s}") from None
    if d > today:
        raise ValueError(f"证据日期不能在今天之后：{s}")
    return d.isoformat()


def _evidence(item: dict[str, Any]) -> tuple[str, str | None]:
    """一条证据 → (日期, 原话或 None)。原话要真在那天 Joy 的私信里，否则拒。"""
    day = _day(item.get("date"))
    q = clean_quote(item.get("quote") or "")
    if not q:
        return day, None
    if not QUOTE_MIN <= len(q) <= QUOTE_MAX:
        raise ValueError(f"原话要 {QUOTE_MIN}–{QUOTE_MAX} 字，摘最能说明问题的一段：{q[:40]}")
    if hit := scan(q):
        raise ValueError(f"原话含密钥 / 威胁模式（{hit}），不能写；改法：换一段不含命中内容的原话，或不带 quote 只写转述")
    if not quote_seen(day, q):
        raise ValueError(f"原话对不上：{day} Joy 的私信和卡片回答里没有「{q[:40]}」（照原文摘一段，不要改写；日期写他说这话的那天）")
    return day, q


def _strengthen(line: str, text: str, origin: str, day: str, quote: str | None) -> str:
    """confirm / revise：同一天不重复计数；日期取较新的；原话追加（去重、按日期留最近 MAX_EVIDENCE 条）。"""
    m = meta(line)
    counted = {m["date"], *(d for d, _ in m["evidence"])}
    ev = list(m["evidence"])
    if quote and (day, quote) not in ev:
        ev.append((day, quote))
    ev = sorted(ev)[-MAX_EVIDENCE:]
    return format_line(text, origin, date.fromisoformat(max(m["date"] or day, day)), m["n"] + (day not in counted), ev)


def _apply(target: str, remove: list[str], add: list[Any], origin: str, write: bool,
           confirm: list[dict[str, Any]] | None = None, revise: list[dict[str, Any]] | None = None) -> str:
    """锁下重读 → 删掉正文等于 remove 各项的行 → revise（改写正文，次数与证据保留）→ confirm（加强已有的行）→ 追加 add
    → 逐行校验、扫密钥、查上限。每项要正好对上一行。add 的一项是字符串，或 {text, date?, quote?}（带证据，只给 memory.md）。
    write=False 只检查（出审批卡前用）。不合格抛 ValueError（说明原样给 agent）；合格返回结果说明。"""
    if not re.fullmatch(ORIGIN_NAME, str(origin)):
        raise ValueError(f"origin 只能是 owner 或任务名（小写字母、数字、- _）：{origin!r}")
    confirm, revise = list(confirm or []), list(revise or [])
    items = [x if isinstance(x, dict) else {"text": x} for x in add]
    if target != "memory" and (confirm or revise or any(x.get("quote") or x.get("date") for x in items)):
        raise ValueError("证据、确认次数只给 memory.md")
    remove = [" ".join(str(x).split()) for x in remove if str(x).strip()]
    items = [{**x, "text": " ".join(str(x.get("text", "")).split())} for x in items if str(x.get("text", "")).strip()]
    revise = [{**x, "old": " ".join(str(x.get("old", "")).split()), "new": " ".join(str(x.get("new", "")).split())} for x in revise]
    confirm = [{**x, "text": " ".join(str(x.get("text", "")).split())} for x in confirm]
    if not remove and not items and not confirm and not revise:
        raise ValueError("没有要删、要加、要确认或要改的内容")
    for t in [x["text"] for x in items] + [x["new"] for x in revise]:
        if not t:
            raise ValueError("revise 的 new 不能是空的")
        if len(t) > MAX_LINE:
            raise ValueError(f"单条超过 {MAX_LINE} 字：{t[:40]}…")
        if hit := scan(t):
            raise ValueError(f"含密钥 / 威胁模式（{hit}），不能写：{t[:40]}…；改法：去掉命中的那段（密钥、指令式的话），用自己的话转述要记的事实")
    p = target_path(target)
    with _lock():
        try:
            lines = _check_lines(p)
        except ValueError as e:
            _backup(p)
            raise ValueError(f"文件解析失败，已留备份，没有写：{e}。这是文件本身坏了，不是这次的写法问题：不要重试；"
                             f"告诉 Joy 按格式修 {p} 的那一行（每行：YYYY-MM-DD 内容 <!-- origin: owner 或任务名 -->）") from None
        kept = list(lines)

        def find(t: str) -> int:
            hits = [i for i, l in enumerate(kept) if body(l) == t]
            if len(hits) != 1:
                raise ValueError(f"{'没找到' if not hits else '有几行都是'}这一条：{t[:60]}（照读出来的正文原样写，不含日期）")
            return hits[0]

        for t in remove:
            kept.pop(find(t))
        for x in revise:
            i = find(x["old"])
            if x["new"] != x["old"] and x["new"] in {body(l) for l in kept}:
                raise ValueError(f"已有相同条目：{x['new'][:60]}")
            day, q = _evidence(x)
            kept[i] = _strengthen(kept[i], x["new"], origin, day, q)
        for x in confirm:
            i = find(x["text"])
            day, q = _evidence(x)
            kept[i] = _strengthen(kept[i], x["text"], meta(kept[i])["origin"] or origin, day, q)
        bodies = {body(l) for l in kept}
        if dup := [x["text"] for x in items if x["text"] in bodies]:
            raise ValueError(f"已有相同条目：{dup[0][:60]}（又被提到就用 confirm）")
        for x in items:
            day, q = _evidence(x) if target == "memory" else (date.today().isoformat(), None)
            kept.append(format_line(x["text"], origin, date.fromisoformat(day), 1, [(day, q)] if q else None))
        new = "\n".join(kept)
        new = new + "\n" if new else ""
        if (size := _size(target, new)) > LIMITS[target]:
            raise ValueError(f"{p.name} 改完是 {size} 字，超过上限 {LIMITS[target]}：先删掉已过时或不重要的（确认次数少、很久没再被提到的先删）、"
                             "合并相近的条目（一次调用可以同时删和加）")
        if not write:
            return "检查通过"
        _backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        replace_text(p, new)          # 整篇换掉（不原地写，见 _files.replace_text）
    return (f"已改 {p.name}：删 {len(remove)} 条，加 {len(items)} 条" + (f"，确认 {len(confirm)} 条" if confirm else "")
            + (f"，改写 {len(revise)} 条" if revise else "") + f"，现在 {size}/{LIMITS[target]} 字")


def edit(target: str, remove: list[str], add: list[Any], origin: str,
         confirm: list[dict[str, Any]] | None = None, revise: list[dict[str, Any]] | None = None, write: bool = True) -> str:
    """memory.md（memory_edit，直接生效）与 USER.md（memory_user_edit，Joy 在审批卡上同意之后）都走这里。write=False 只检查（memory_user_edit 出审批卡前用）。"""
    return _apply(target, remove, add, origin, write=write, confirm=confirm, revise=revise)


def read_view(target: str) -> dict[str, Any]:
    """memory_read / memory_user_read 的返回：每行的日期、正文、来源（命中威胁模式的正文换成 [BLOCKED]），已用 / 上限字数。"""
    size, limit = usage(target)
    out = []
    for l in read_lines(target):
        m, blocked = meta(l), bool(scan(l))
        row = {"date": m["date"], "text": "[BLOCKED]" if blocked else body(l), "origin": m["origin"]}
        if target == "memory":                          # 确认次数与原话：dreaming 据此决定加强、改写还是先删
            row.update(n=m["n"], evidence=[] if blocked else [{"date": d, "quote": q} for d, q in m["evidence"]])
        out.append(row)
    return {"file": FILES[target], "chars": size, "limit": limit, "lines": out}
