"""任务的待办清单（todo 工具写，平台读）：任务工作目录 todo/<时间>.md，一份一个清单，第一行是标题（建的时刻与运行）。每项一行：
- [ ] 3. 文字                 没清掉
- [x] 3. 文字 —— 依据：…       做完（todo_done 写的依据）
- [-] 3. 文字 —— 不做了：…     放弃（todo_drop 写的原因）
编号在同一任务里不重复（新项 = 所有清单里最大的编号 + 1）。平台：运行开头把没清掉的列进用户消息（start_note），
运行正常结束时还有就同一会话补一轮提醒（nudge_prompt），仍有就附在结束通知末尾（end_note）。"""
from __future__ import annotations

import re
import time
from pathlib import Path

from control_center import paths

TOOL = "todo"
LINE = re.compile(r"^- \[([ x-])\] (\d+)\. (.*)$")
MAX_ITEMS = 20                    # 一次 create / add 最多几项
MAX_TEXT = 200                    # 一项最多多少字
MAX_NOTE = 200                    # 依据 / 原因最多多少字
SHOW = 20                         # 提醒里最多列几项
RECENT_S = 600                    # 刚清掉的清单在网页待办面板上再留多久
STATES = {" ": "open", "x": "done", "-": "dropped"}


def folder(task: str) -> Path:
    return paths.WORK / task / "todo"


def items(task: str) -> list[dict]:
    """所有清单的所有项（按文件名即建的时刻排）：{file, n, state（" " 没清掉 / "x" 做完 / "-" 放弃）, text}。"""
    out = []
    for f in sorted(folder(task).glob("*.md")):
        for line in f.read_text().splitlines():
            if m := LINE.match(line):
                out.append({"file": f, "n": int(m[2]), "state": m[1], "text": m[3]})
    return out


def panel(task: str) -> list[dict]:
    """网页待办面板：还没清掉的清单，加上 RECENT_S 内刚清掉的；每份 {name, title, items: [{n, state（open / done / dropped）, text, note（依据或原因）}]}。"""
    out = []
    for f in sorted(folder(task).glob("*.md")):
        lines = f.read_text().splitlines()
        its = []
        for line in lines:
            if m := LINE.match(line):
                text, _, note = m[3].partition(" —— ")
                its.append({"n": int(m[2]), "state": STATES[m[1]], "text": text, "note": note.split("：", 1)[-1]})
        if its and (any(i["state"] == "open" for i in its) or time.time() - f.stat().st_mtime < RECENT_S):
            out.append({"name": f.name, "title": lines[0].lstrip("# ").strip() if lines else f.stem, "items": its})
    return out


def open_items(task: str) -> list[dict]:
    return [i for i in items(task) if i["state"] == " "]


def listing(its: list[dict]) -> str:
    shown = "\n".join(f"{i['n']}. {i['text']}" for i in its[:SHOW])
    return shown + (f"\n…另有 {len(its) - SHOW} 项" if len(its) > SHOW else "")


def status(task: str) -> str:
    """工具每次调用的返回：还没清掉的全部项。"""
    o = open_items(task)
    return f"还没清掉的待办（{len(o)} 项）：\n{listing(o)}" if o else "待办都清掉了"


def _clean(texts: list[str]) -> list[str]:
    out = [" ".join(str(t).split()) for t in texts]
    if not out or not all(out):
        raise ValueError("待办要有至少一项，每项不能为空")
    if len(out) > MAX_ITEMS:
        raise ValueError(f"一次最多 {MAX_ITEMS} 项")
    for t in out:
        if len(t) > MAX_TEXT:
            raise ValueError(f"每项不超过 {MAX_TEXT} 字：{t[:40]}…")
        if " —— " in t:
            raise ValueError(f"待办文字里不能有「 —— 」（依据和原因用它隔开）：{t[:40]}")
    return out


def _next_n(task: str) -> int:
    return max((i["n"] for i in items(task)), default=0) + 1


def create(task: str, run_ref: str, texts: list[str]) -> Path:
    """新建一份清单，返回文件。"""
    texts = _clean(texts)
    n0 = _next_n(task)
    folder(task).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    f = folder(task) / f"{stamp}.md"
    k = 1
    while f.exists():                         # 同一秒建了两份
        f, k = folder(task) / f"{stamp}-{k}.md", k + 1
    f.write_text(f"# 待办 {time.strftime('%Y-%m-%d %H:%M')}（运行 {run_ref}）\n"
                 + "".join(f"- [ ] {n0 + j}. {t}\n" for j, t in enumerate(texts)))
    return f


def add(task: str, run_ref: str, texts: list[str]) -> Path:
    """加到最新一份还有没清掉项的清单；没有就新建一份。"""
    texts = _clean(texts)
    files = sorted({i["file"] for i in open_items(task)})
    if not files:
        return create(task, run_ref, texts)
    n0 = _next_n(task)
    f = files[-1]
    body = f.read_text()
    f.write_text(body + ("" if body.endswith("\n") else "\n") + "".join(f"- [ ] {n0 + j}. {t}\n" for j, t in enumerate(texts)))
    return f


def find(task: str, item: str) -> dict:
    """item：编号（「3」「#3」）或原文（整句一致，或只有一项包含它）；只在没清掉的项里找。"""
    o = open_items(task)
    key = str(item).strip().lstrip("#")
    if key.isdigit():
        hit = [i for i in o if i["n"] == int(key)]
        if hit:
            return hit[0]
        raise ValueError(f"没有没清掉的第 {key} 项（已经打勾或放弃了，或编号不对）\n{status(task)}")
    text = " ".join(str(item).split())
    hit = [i for i in o if i["text"] == text] or [i for i in o if text and text in i["text"]]
    if len(hit) == 1:
        return hit[0]
    raise ValueError(("有 %d 项都包含「%s」，写编号" % (len(hit), text)) if hit else f"没清掉的待办里没有「{text}」\n{status(task)}")


def mark(task: str, item: str, done: bool, note: str) -> dict:
    """打勾（done）或放弃：改那一行，依据 / 原因接在后面。"""
    note = " ".join(str(note).split())
    if not note:
        raise ValueError("要写一句依据（做完怎么确认的）" if done else "要写一句不做的原因")
    if len(note) > MAX_NOTE:
        raise ValueError(f"依据 / 原因不超过 {MAX_NOTE} 字")
    it = find(task, item)
    old = f"- [ ] {it['n']}. {it['text']}"
    new = f"- [{'x' if done else '-'}] {it['n']}. {it['text']} —— {'依据' if done else '不做了'}：{note}"
    lines = it["file"].read_text().splitlines()
    lines[lines.index(old)] = new
    it["file"].write_text("\n".join(lines) + "\n")
    return it


def start_note(task: str) -> str:
    """运行开头接在用户消息后面：上次留下、还没清掉的待办（没有就空）。"""
    o = open_items(task)
    return (f"（系统消息）之前留下、还没清掉的待办（{len(o)} 项，在工作目录 todo/ 下）：\n{listing(o)}\n"
            "这只是提醒，不是新的要求：先按这次的消息做；Joy 让先别做、等他确认的，照样不动。这次做完了的用 todo_done 打勾并写依据；"
            "不做了用 todo_drop 写原因；留到以后做的不动它。") if o else ""


def nudge_prompt(task: str) -> str:
    o = open_items(task)
    return (f"（系统消息）还有 {len(o)} 项待办没清掉：\n{listing(o)}\n"
            "这只是提醒，不是新的要求：Joy 让先别做、等他确认的，照样不动。已经做完的用 todo_done 打勾并写依据（只写工具结果里看得到的）；"
            "不做了的用 todo_drop 写原因；留到以后做的不动它，在回复里告诉 Joy。") if o else ""


def end_note(task: str) -> str:
    """结束通知末尾：还没清掉的待办（没有就空）。"""
    o = open_items(task)
    return f"📋 还有 {len(o)} 项待办没清掉（下次运行接着管）：\n{listing(o)}" if o else ""
