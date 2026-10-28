"""向 Joy 要回答 / 许可的运行控制：就地等回答或审批、等不到就挂起、拿到再续跑。现在承载 ask-user 的问题卡。

ask-user（D35）：任务的模型向 Joy 提问，一次调用 = 一轮 1–6 题（可选再加一个备注框），问题卡发到私信。任何任务在 tools 里列了它就能用。
- 回答在运行目录的 ask.json 里；要不要记进对话记录由任务自己定（如私信任务的 run_end hook 记进它的对话库）。

- 校验（validate）：MCP 服务器 tools/ask-user/server.py 调它。模型每题只给 question、type（single / multi）、1–3 个选项文字；
  程序总在最后补一个「其他…」（模型自己写的「其他 / other」选项拒收），所以每题最多 A–D 四个字母选项。
  填空题（type: fill，2026-10-11）：一个文本框，可带预填 default（模型的猜测，Joy 改或留着）和填写提示 hint；notes: true 时程序在最后加一题备注
  （填空、notes 标记）。所有题都可以不答，留空的交回「无回答」。网页上有填空题的卡一次展开所有题（单选 / 多选是选框，填空是文本框）+ 提交
  （submit 带整张卡的回答列表）；Discord 填空题一页一题（「填写」弹框），备注页（最后一页）直接有「提交」。
- 卡（card_spec / interact / render；Discord 上的样子在 channels/discord/service.py 的 ask_layout）：一次只显示一题（「第 i/n 题 · 单选/多选」、题面、
  A. … 形式的选项，✅ 标当前选择，下面是别的题的回答摘要），同一条消息原地改。按钮：每个字母一个（单选点了就换成它，多选点了切换；
  「其他…」那个字母弹框写自由回答，单选时它就是这题的选择），「上一题」「下一题」（只翻页不提交）。最后一题点「下一题」进回答总览：
  列出各题回答，按钮只有「上一题」「提交」（没答的题报未回答；不想答就一路下一题、空着提交）。只认 owner 点，不过期；
  进度与各题回答存在卡的 payload 里（stbcc-hub 与各入口重启不丢），提交才交处理器（runner._handle_ask_card）。
- 等回答：网关先就地等 WAIT_S 秒（工具调用不返回，沙箱与模型都留着）；Joy 这期间答了，答案就是这次调用的结果。
  没答（或来了更高优先级的条目、被要求暂停）就让出：调用返回"已挂起"，pi 在这一轮结束时以 75 退出，运行记 waiting，不自动恢复；
  Joy 之后回答，处理器把续跑按任务优先级入队，答案作为下一条用户消息送进同一会话（reply_prompt）。
- 状态在运行目录的 ask.json（rounds 列表），网关（stbcc-runner 进程）和处理器（stbcc-hub 进程）用 flock 串行改：
  让出与回答同时发生时，谁先拿到锁谁说了算，答案不会丢，也不会既当调用结果又当下一条消息。
- 通用审批卡（PERMISSION_KIND，2026-10-05）：以 sparky 跑的工具要 Joy 点头才做的事（改笔记、执行 shell 命令、浏览器提交、要密码…），经中枢 card op 发这一种卡，
  卡面由这里按 payload 生成（permission_card：哪个任务的哪个工具、要做什么、细节，可选附图与密码框）；Joy 同意后由工具自己执行（就地等卡，或续跑时
  经中枢的 card_use 用掉已同意的卡、按 payload.key 对上同一件事），平台不替工具执行，工具也不用自带卡片处理器。
- 审批卡（C3）走同一套：工具在运行中发的卡（payload.run = "<任务>/<运行 id>"，如通用审批卡）记成 kind: approval 的一轮
  （record_card），网关同样就地等 WAIT_S（new_for_run 找这次调用发的卡，waited 算等卡的时间）、没处理就让出；Joy 处理后「结果」就是答案（{action, result}），
  续跑时工具经中枢的 card_use 用掉已同意的卡（take_approved）。卡片本身（存卡、处理器、密码）在 control_center/cards.py。
- 批了就执行：主模型的一次调用发的卡记下这次调用（record_card 的 call：工具调用发的卡按 payload.call 对上，pre_call hook 拦下时发的卡按 payload.tool 对上）。
  Joy 同意时工具已经不在等：网关还在就地等就由网关以同样的参数再调用一次、结果当这次调用的结果；已让出就由 stbcc-hub 当场在同样的身份与环境里
  替它调用一次（gateway.execute_approved），结果存进这一轮（set_executed），续跑时和拒绝一样连同这次调用写进下一条用户消息（reply_prompt）；
  pre_call hook 放行了的（如 builder 的开发计划：批准时建好开发区）续跑时由运行器先调用这一次，结果同样写进下一条用户消息。
  模型续跑后仍用同样的参数再调用：网关直接交回存下的结果（take_executed），不重复执行。
"""
from __future__ import annotations

import hashlib

import fcntl
import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from control_center import cards
from control_center.paths import APPROVALS

TOOL = "ask-user"
KIND = "ask"                     # 问题卡的 kind
WAIT_S = 300                     # 就地等回答的上限：超过就让出运行器
POLL_S = 1.0
MAX_QUESTIONS, MAX_OPTIONS = 6, 3          # 模型给的选项最多 3 个，程序再补「其他…」；题数不含程序加的备注
MAX_QUESTION, MAX_LABEL, MAX_TEXT, MAX_HINT = 300, 50, 1000, 100
OTHER = "其他…"
LETTERS = "ABCD"
DECLINED = "Joy 拒绝回答"
UNANSWERED = "无回答"
NOTES = {"question": "备注", "type": "fill", "default": "", "hint": "上面没问到、想补充的写这里", "notes": True}   # notes: true 时程序加在最后
FILL_TAIL = "（预填没改的就是 Joy 认可了你的猜测；无回答的按你自己的判断做，并在交付说明里写明你替他定了什么。）"
PENDING_NOTE = ("Joy 暂时没有回答（问题卡还在他的私信里，不会过期）。本次运行现在挂起、让出机器：这一轮结束后不要再调用工具。"
                "他回答（或拒绝回答）后，答案会作为下一条用户消息送到，你再接着做。")
CARD_PENDING_NOTE = ("（网关：Joy 暂时没有处理审批卡「{title}」（卡不会过期）。本次运行现在挂起、让出机器：这一轮结束后不要再调用工具。"
                     "他处理后，结果会作为下一条用户消息送到，你再接着做。）")
CALL_PENDING_NOTE = ("Joy 还没处理审批卡「{title}」：这一轮结束后不要再调用工具。"
                     "他同意后这次调用会照原样执行，结果或他的意见会在下一条用户消息里给你，你再接着做。")
RECALL_NOTE = "要做的事还没做成的话，用同样的参数再调用一次"
_OTHER_RE = re.compile(r"^(其他|其它|other|others)[\s.。…:：]*$", re.I)


def validate(questions: Any, notes: bool = False) -> list[dict[str, Any]]:
    """严格校验一轮问题，返回规整后的 [{question, type, options: [文字]}]（options 不含程序补的「其他…」）；填空题是 {question, type: fill, default, hint}；
    notes 为真时最后加一题备注（NOTES）。不合格抛 ValueError。"""
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"一轮要有 1–{MAX_QUESTIONS} 个问题（这次 {len(questions) if isinstance(questions, list) else 0} 个，不含备注）；更多的分几轮问")
    out, seen = [], set()
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError(f"第 {i} 题格式不对：每题要写成对象 {{question, type: single|multi, options: [1–{MAX_OPTIONS} 个选项]}}（填空写 {{question, type: fill, default?, hint?}}），"
                             f"这次是 {type(q).__name__}：{str(q)[:80]}")
        text = str(q.get("question") or "").strip()
        if not 1 <= len(text) <= MAX_QUESTION:
            raise ValueError(f"第 {i} 题的 question 要有 1–{MAX_QUESTION} 字")
        if text in seen:
            raise ValueError(f"第 {i} 题和前面的问题重复")
        seen.add(text)
        kind = q.get("type")
        if kind not in ("single", "multi", "fill"):
            raise ValueError(f"第 {i} 题的 type 只能是 single（单选）、multi（多选）或 fill（填空）")
        if kind == "fill":
            if q.get("options"):
                raise ValueError(f"第 {i} 题是填空（fill）：不要写 options")
            default, hint = str(q.get("default") or "").strip(), " ".join(str(q.get("hint") or "").split())
            if len(default) > MAX_TEXT or len(hint) > MAX_HINT:
                raise ValueError(f"第 {i} 题的 default 最多 {MAX_TEXT} 字、hint 最多 {MAX_HINT} 字")
            out.append({"question": text, "type": "fill", "default": default, "hint": hint})
            continue
        opts = q.get("options")
        if not isinstance(opts, list) or not 1 <= len(opts) <= MAX_OPTIONS:
            raise ValueError(f"第 {i} 题要有 1–{MAX_OPTIONS} 个选项（程序会自动在最后加「{OTHER}」让 Joy 自由回答）")
        norm: list[str] = []
        for j, o in enumerate(opts, 1):
            label = str(o).strip() if isinstance(o, str) else ""
            if not 1 <= len(label) <= MAX_LABEL:
                raise ValueError(f"第 {i} 题第 {j} 个选项要有 1–{MAX_LABEL} 字")
            if _OTHER_RE.match(label):
                raise ValueError(f"第 {i} 题不要自己写「{label}」选项：程序会自动在最后加「{OTHER}」")
            if label in norm:
                raise ValueError(f"第 {i} 题的选项重复：{label}")
            norm.append(label)
        out.append({"question": text, "type": kind, "options": norm})
    return out + [dict(NOTES)] if notes else out


# ---------- 运行目录里的 ask.json ----------
@contextmanager
def _rounds(run_dir: Path) -> Iterator[list[dict[str, Any]]]:
    """持锁读改写 ask.json 的 rounds（两个进程都会改，flock 串行）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / ".ask.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        p = run_dir / "ask.json"
        try:
            rounds = json.loads(p.read_text()).get("rounds", []) if p.exists() else []
        except (OSError, ValueError):
            rounds = []
        yield rounds
        tmp = p.with_name(f".ask.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"rounds": rounds}, ensure_ascii=False, indent=1))
        os.replace(tmp, p)


def next_round(run_dir: Path) -> int:
    with _rounds(run_dir) as rounds:
        return len(rounds) + 1


def record(run_dir: Path, rnd: int, questions: list[dict[str, Any]], card_id: str) -> None:
    with _rounds(run_dir) as rounds:
        rounds.append({"round": rnd, "card": card_id, "questions": questions, "asked_at": time.time(), "status": "pending"})


def record_card(run_dir: Path, rnd: int, card_id: str, title: str, call: dict[str, Any] | None = None, auto: bool = False) -> None:
    """工具在运行中发的审批卡：记成一轮（kind approval），和问题卡一样就地等、没处理就让出。
    call：发卡的那次主模型调用 {kind: tool | hook, tool, subagent, function, args（交给工具 / hook 的参数）, asked（模型给的参数）}，Joy 同意时照它再调用一次。
    auto：卡的 payload 带 auto（同意后平台自己做完，如一次性监听）：给模型的结果不附 RECALL_NOTE。"""
    with _rounds(run_dir) as rounds:
        rounds.append({"round": rnd, "card": card_id, "kind": "approval", "title": title, "questions": [], "asked_at": time.time(), "status": "pending",
                       **({"call": call} if call else {}), **({"auto": True} if auto else {})})


def approved_call(r: dict[str, Any]) -> bool:
    """这一轮是 Joy 同意了的、记着调用的审批卡（要照原样执行那次调用）。"""
    return bool(r.get("call")) and (r.get("answers") or {}).get("action") == "approve"


def set_executed(run_dir: Path, card_id: str, out: dict[str, Any]) -> None:
    """让出后 Joy 同意、平台替那次调用执行完了：结果存进这一轮。out：{ok, text}（执行完了）或 {proceed: True}（pre_call hook 放行了，续跑时调用）。"""
    with _rounds(run_dir) as rounds:
        for r in rounds:
            if r.get("card") == card_id:
                r["executed"] = {**out, "at": time.time()}


def claim_execution(run_dir: Path, card_id: str) -> bool:
    """平台要替这一轮的调用执行：已在执行或执行过的返回 False（不重复执行），否则记下开始执行（executing）返回 True。"""
    with _rounds(run_dir) as rounds:
        r = next((x for x in rounds if x.get("card") == card_id), None)
        if r is None or r.get("executed") or r.get("executing"):
            return False
        r["executing"] = time.time()
        return True


INTERRUPTED = {"ok": False, "text": "执行到一半时 stbcc-hub 重启了，这次调用做没做成不确定：先查一下做成没有，再决定要不要重做"}


def unfinished(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """挂起的运行里 Joy 已同意、平台还没执行完的轮次：(没开始执行的, 执行到一半的)。stbcc-hub 启动时补上（runner.recover_approved）。"""
    if not (run_dir / "ask.json").exists():
        return [], []
    with _rounds(run_dir) as rounds:
        todo = [dict(r) for r in rounds if approved_call(r) and r.get("yielded") and not r.get("executed")]
    return [r for r in todo if not r.get("executing")], [r for r in todo if r.get("executing")]


def note_executed(card_id: str, out: dict[str, Any]) -> bool:
    """批准后平台执行完：把一句结果（成功 / 失败 + 摘要）接在卡片的结果文字后面（网页、Discord 刷新后都看得到）。已写过的不再写。返回写没写。"""
    card = cards.load(card_id)
    res = (card or {}).get("resolved")
    if not res or res.get("executed"):
        return False
    if out.get("proceed"):
        note = "已放行，接着做"
    else:
        first = " ".join(str(out.get("text") or "").split())
        note = ("已执行：" if out.get("ok", True) else "执行出错：") + (first[:80] + "…" if len(first) > 80 else first or "（没有输出）")
    res.update(result=f"{res.get('result', '')} → {note}", executed=time.time())
    cards.save(card)  # pyright: ignore[reportArgumentType]
    return True


def approved_of_call(run: str, call_id: str, since: float) -> list[str]:
    """这次运行里 since 之后、payload.call 是 call_id、Joy 已同意的卡（工具就地等到同意、自己执行了的）。"""
    out = []
    for p in APPROVALS.glob("*.json"):
        try:
            if p.stat().st_mtime < since - 1:
                continue
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        pl = c.get("payload") or {}
        if pl.get("run") == run and pl.get("call") == call_id and (c.get("resolved") or {}).get("action") == "approve":
            out.append(c["id"])
    return out


def expire_password_cards(run: str) -> list[str]:
    """运行（这一段）结束：这次运行还没处理的带密码框的卡作废（发卡的调用都已结束，密码交不出去）。返回卡 id（调用方让各入口刷新）。"""
    ids = []
    for p in APPROVALS.glob("*.json"):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not c.get("resolved") and (c.get("payload") or {}).get("run") == run and cards.secret_fields(c):
            cards.resolve(c, "expired")
            ids.append(c["id"])
    return ids


def take_executed(run_dir: Path, tool: str, function: str, args: dict[str, Any]) -> str | None:
    """模型续跑后仍用同样的参数再调用：平台已经替它执行过的那次（没取过）的结果，取一次；没有返回 None。"""
    if not (run_dir / "ask.json").exists():
        return None
    with _rounds(run_dir) as rounds:
        for r in rounds:
            c, done = r.get("call") or {}, r.get("executed") or {}
            if "text" in done and not r.get("reclaimed") and (c.get("tool"), c.get("function"), c.get("asked")) == (tool, function, args):
                r["reclaimed"] = time.time()
                return str(done["text"])
    return None


def settle(run_dir: Path, rnd: int, give_up: bool) -> dict[str, Any] | None:
    """网关看这一轮：已回答（或拒绝）就返回这一轮，答案当调用结果；没答且 give_up 就记下让出（之后的回答走入队续跑）。"""
    with _rounds(run_dir) as rounds:
        r = next((x for x in rounds if x["round"] == rnd), None)
        if r is None:
            return None
        if r["status"] in ("answered", "declined"):
            r["delivered"] = "inline"
            return dict(r)
        if give_up and r["status"] == "pending":
            r["yielded"] = time.time()
        return None


def answer(run_dir: Path, card_id: str, action: str, answers: Any) -> dict[str, Any] | None:
    """处理器记下 Joy 的回答（action submit）或拒绝（decline）。这一轮已不在等（作废、已答）返回 None。"""
    with _rounds(run_dir) as rounds:
        r = next((x for x in rounds if x.get("card") == card_id), None)
        if r is None or r["status"] != "pending":
            return None
        r.update(status="declined" if action == "decline" else "answered", answered_at=time.time())
        if action != "decline":
            r["answers"] = answers
        return dict(r)


def resume_rounds(run_dir: Path) -> list[dict[str, Any]]:
    """续跑时要送进会话的各轮：让出后才答的、还没送达的（同一次挂起期间的几张卡都在；同意了、记着调用的要等平台执行完才算）。"""
    out = []
    with _rounds(run_dir) as rounds:
        for r in rounds:
            if (r["status"] in ("answered", "declined") and r.get("yielded") and not r.get("delivered")
                    and not (approved_call(r) and not r.get("executed"))):
                r["delivered"] = "resume"
                out.append(dict(r))
    if not out:
        raise RuntimeError("这次运行没有待送达的回答")
    return out


def obsolete_pending(run_dir: Path, why: str) -> list[str]:
    """运行没进 waiting 就结束了（失败、超时、暂停……）：还在等的问题作废，卡收掉。返回卡 id（调用方再经 stbcc-hub 让各入口刷新这些卡）。"""
    ids = []
    if not (run_dir / "ask.json").exists():
        return ids
    with _rounds(run_dir) as rounds:
        for r in rounds:
            if r["status"] == "pending":
                r["status"] = "obsolete"
                card = cards.load(str(r.get("card") or ""))
                if card and not card.get("resolved"):
                    card["resolved"] = {"action": "obsolete", "value": None, "result": why, "at": time.time()}
                    cards.save(card)
                    ids.append(card["id"])
    return ids


# ---------- 运行中等卡 ----------
def new_for_run(run: str, since: float) -> list[dict[str, Any]]:
    """这次运行（payload.run）在 since 之后发出、还没处理的卡（网关就地等它们）。带密码框的卡（payload.password）不算：
    密码只交给发卡的那次调用，由工具自己就地等，网关不等也不让出。"""
    out = []
    for p in sorted(APPROVALS.glob("*.json")):
        try:
            if p.stat().st_mtime < since - 1:
                continue
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        pl = c.get("payload") or {}
        if not c.get("resolved") and pl.get("run") == run and not pl.get("password") and float(c.get("created", 0)) >= since - 1:
            out.append(c)
    return sorted(out, key=lambda c: c["created"])


def waited(run: str, since: float, cap: float) -> float:
    """这次运行（payload.run）在 since 之后发的卡等 Joy 处理的秒数之和：从发卡到处理（没处理就到现在），每张最多算 cap 秒
    （工具就地等卡的上限）。子代理的单次上限不算这段时间（发卡本身算）。"""
    now, total = time.time(), 0.0
    for p in APPROVALS.glob("*.json"):
        try:
            if p.stat().st_mtime < since - 1:
                continue
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        created = float(c.get("created", 0))
        if (c.get("payload") or {}).get("run") != run or created < since - 1:
            continue
        end = float((c.get("resolved") or {}).get("at") or now)
        total += max(0.0, min(end, created + cap) - created)
    return total


def take_approved(kind: str, run: str, card_id: str | None = None, **match: str) -> bool:
    """同一次运行里已经同意、还没用过、payload 对得上 match 的卡（写了 card_id 就只看这一张）：用掉它（标 used）返回 True。
    工具的审批挂起后续跑时直接放行、就地等到同意后标用掉（以 sparky 跑的工具经中枢的 card_use op 调）。"""
    for p in ([cards._path(card_id)] if card_id else sorted(APPROVALS.glob("*.json"))):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        pl = c.get("payload") or {}
        if (c.get("kind") == kind and (c.get("resolved") or {}).get("action") == "approve" and pl.get("run") == run and not pl.get("used")
                and all(pl.get(k) == v for k, v in match.items())):
            pl["used"] = time.time()
            cards.save(c)
            return True
    return False


# ---------- 答案怎么给模型 ----------
def answered(a: dict[str, Any]) -> bool:
    return bool(a.get("picked") or a.get("other") or a.get("text"))


def has_fill(questions: list[dict[str, Any]]) -> bool:
    """这一轮有填空题（含备注）。"""
    return any(q.get("type") == "fill" for q in questions)


def _blank(q: dict[str, Any]) -> dict[str, Any]:
    """一题的初始回答：填空题是预填的字，选择题什么都没选。"""
    return {"text": q.get("default") or ""} if q.get("type") == "fill" else {"picked": [], "other": ""}


def _summary(q: dict[str, Any], a: dict[str, Any]) -> str:
    """一题的回答：选中的选项文字（字母换回文字）和 / 或其他里写的字；填空题是填的字；都没有就是无回答。"""
    if q.get("type") == "fill":
        return a.get("text") or UNANSWERED
    parts = ["选了" + "".join(f"「{q['options'][j]}」" for j in a["picked"])] if a.get("picked") else []
    if a.get("other"):
        parts.append(f"其他：{a['other']}")
    return "；".join(parts) or UNANSWERED


def answer_lines(r: dict[str, Any]) -> str:
    return "\n".join(f"{i}. {q['question']}\n   回答：{_summary(q, a)}"
                      for i, (q, a) in enumerate(zip(r["questions"], r.get("answers") or []), 1))


def card_outcome(r: dict[str, Any]) -> str:
    """审批卡这一轮的结果：Joy 同意 / 拒绝了哪张卡，处理器执行的结果。没记调用、也不是平台自己做完（auto）的卡同意了：模型自己再调用一次（RECALL_NOTE）。"""
    a = r.get("answers") or {}
    verb = "同意" if a.get("action") in ("approve", "submit") else "拒绝" if a.get("action") in ("reject", "decline") else f"处理（{a.get('action')}）"
    tail = f"。{RECALL_NOTE}" if a.get("action") == "approve" and not r.get("call") and not r.get("auto") else ""
    return f"Joy {verb}了审批卡「{r.get('title', '')}」：{a.get('result', '')}{tail}"


def call_text(c: dict[str, Any]) -> str:
    """一次调用给模型看的写法：函数名(参数 JSON)，参数太长只留前面。"""
    args = json.dumps(c.get("asked") or {}, ensure_ascii=False)
    return f"{c.get('function')}({args[:500] + '…' if len(args) > 500 else args})"


def call_outcome(r: dict[str, Any], done: str | None = None) -> str:
    """记着调用的审批卡这一轮给模型的话：同意了是那次调用的执行结果（done：续跑时才调用的结果），拒绝了是 Joy 的意见。"""
    head = f"你上次的调用 {call_text(r['call'])}"
    if not approved_call(r):
        return f"Joy 拒绝了审批卡「{r.get('title', '')}」，{head} 没有执行：{(r.get('answers') or {}).get('result', '')}"
    ex = r.get("executed") or {}
    text = done if done is not None else str(ex.get("text", ""))
    return f"Joy 同意了审批卡「{r.get('title', '')}」，{head} 已经执行" + ("，结果：" if ex.get("ok", True) else "，但出错了：") + f"\n{text}"


def tool_result(r: dict[str, Any]) -> str:
    """就地等到的答案：直接作为这次 ask_user 调用的结果。"""
    if r.get("kind") == "approval":
        return f"（网关：{card_outcome(r)}）"
    if r["status"] == "declined":
        return f"{DECLINED}这一轮的问题。任务说明里写了没回答怎么处理的，照任务说明做；没写的按你自己的判断继续，在回复里说明你替他做了哪些选择。"
    return f"Joy 的回答：\n{answer_lines(r)}" + (f"\n{FILL_TAIL}" if has_fill(r["questions"]) else "")


def reply_prompt(rs: list[dict[str, Any]] | dict[str, Any], done: dict[str, str] | None = None) -> str:
    """让出后才答的（resume_rounds 的各轮）：续跑时作为下一条用户消息送进同一会话。done：续跑时才调用的（pre_call hook 放行的）卡 id → 调用结果。"""
    tail = "上面的对话与工具结果都已保留，已经发出去的消息不要重发；接着完成原任务。"
    rs = [rs] if isinstance(rs, dict) else rs
    if len(rs) == 1 and not rs[0].get("call"):
        return _single_reply(rs[0], tail)
    parts = [call_outcome(r, (done or {}).get(r["card"])) if r.get("call") else card_outcome(r) if r.get("kind") == "approval"
             else _single_reply(r, "").removeprefix("（系统消息）").strip() for r in rs]
    if len(parts) == 1:
        return f"（系统消息）运行挂起期间 {parts[0]}\n{tail}"
    return "（系统消息）运行挂起期间：\n" + "\n\n".join(f"{i}. {x}" for i, x in enumerate(parts, 1)) + f"\n{tail}"


def _single_reply(r: dict[str, Any], tail: str) -> str:
    if r.get("kind") == "approval":
        return f"（系统消息）运行挂起期间 {card_outcome(r)}。{tail}"
    head = f"（系统消息）这是你第 {r['round']} 轮 ask_user 的答案（运行挂起期间 Joy 在问题卡上作答）。"
    if r["status"] == "declined":
        return f"{head}{DECLINED}这一轮的问题。任务说明里写了没回答怎么处理的，照任务说明做；没写的按你自己的判断继续，在回复里说明你替他做了哪些选择。{tail}"
    return f"{head}\n{answer_lines(r)}\n" + (f"{FILL_TAIL}\n" if has_fill(r["questions"]) else "") + tail


# ---------- 问题卡 ----------
def card_spec(task: str, run_id: str, rnd: int, questions: list[dict[str, Any]], why: str = "") -> dict[str, Any]:
    """问题卡；why（可选）是模型写的一句为什么问，放在正文最前面。"""
    n = sum(not q.get("notes") for q in questions)
    count = f"共 {n} 题" + (" + 备注" if n < len(questions) else "") + (" · 都可以不答" if has_fill(questions) else "")
    return {"kind": KIND, "title": f"❓ {task} 在问你（第 {rnd} 轮）", "body": (f"{why}\n" if why else "") + f"运行 {run_id} · {count} · 这张卡不会过期",
            "ttl_seconds": None, "buttons": [],
            "payload": {"task": task, "run_id": run_id, "round": rnd, "questions": questions, "current": 0,
                        "answers": [_blank(q) for q in questions]}}


def render(card: dict[str, Any]) -> str:
    """卡的正文：当前这一题（第 i/n 题 · 单选/多选、题面、A. … 选项，✅ 标当前选择；别的题的回答看最后的总览）；
    最后一题之后是回答总览（current == 题数）；卡交掉以后只列各题回答。"""
    p = card["payload"]
    qs, ans = p["questions"], p["answers"]
    lines = []
    if not card.get("resolved") and p["current"] >= len(qs):
        lines.append(f"**回答总览**（共 {len(qs)} 题，没问题就点提交；要改点上一题回去）")
        lines += [f"{n + 1}. {qs[n]['question'][:60]} → {_summary(qs[n], ans[n])[:120]}" for n in range(len(qs))]
        return "\n".join(lines)
    if not card.get("resolved"):
        i = p["current"]
        q, a = qs[i], ans[i]
        if q["type"] == "fill":                                  # 填空题：题面、提示、现在填的字
            lines.append("**备注**（最后一页，可留空）" if q.get("notes") else f"**第 {i + 1}/{len(qs)} 题 · 填空**：{q['question']}")
            if q.get("hint"):
                lines.append(f"（{q['hint']}）")
            lines.append(f"现在：{a['text'][:1000]}" if a.get("text") else "现在：（空）")
            return "\n".join(lines)
        lines.append(f"**第 {i + 1}/{len(qs)} 题 · {'多选' if q['type'] == 'multi' else '单选'}**：{q['question']}")
        for j, o in enumerate(q["options"]):
            lines.append(f"{'✅' if j in a['picked'] else '▫️'} {LETTERS[j]}. {o}")
        k = len(q["options"])
        lines.append(f"{'✅' if a['other'] else '▫️'} {LETTERS[k]}. {OTHER}" + (f"：{a['other'][:200]}" if a["other"] else ""))
        return "\n".join(lines)
    lines += ["回答：", *(f"{n + 1}. {qs[n]['question'][:60]} → {_summary(qs[n], ans[n])[:120]}" for n in range(len(qs)))]
    return "\n".join(lines)


def other_modal(card: dict[str, Any]) -> dict[str, Any]:
    """「其他…」弹框：只问当前这一题，带上已写的字；清空再交 = 撤掉其他。"""
    p = card["payload"]
    i = p["current"]
    q = p["questions"][i]
    if q["type"] == "fill":                              # 填空题：这一题的文本框，带上现在的字（预填或已填）
        return {"title": "备注" if q.get("notes") else f"第 {i + 1} 题：{q['question']}", "label": "可留空（清空再交 = 不答）",
                "default": p["answers"][i].get("text") or "", "placeholder": q.get("hint") or ""}
    return {"title": f"第 {i + 1} 题：{OTHER}", "label": "自由回答（清空再交 = 撤掉）", "default": p["answers"][i]["other"]}


def _set_all(qs: list[dict[str, Any]], value: Any) -> list[dict[str, Any]] | str:
    """网页一次交上来的整张卡的回答（每题 {picked, other} 或 {text}）：逐题校验规整，不合格返回给 Joy 的提示。"""
    if not isinstance(value, list) or len(value) != len(qs) or not all(isinstance(v, dict) for v in value):
        return "回答的格式不对（要和题目一一对应），刷新页面再交"
    out = []
    for q, v in zip(qs, value):
        if q["type"] == "fill":
            out.append({"text": str(v.get("text") or "").strip()[:MAX_TEXT]})
            continue
        picked = sorted({int(j) for j in v.get("picked") or [] if str(j).isdigit() and int(j) < len(q["options"])})
        other = str(v.get("other") or "").strip()[:MAX_TEXT]
        if q["type"] == "single":                       # 单选：写了其他就是选其他（同 interact）
            picked = [] if other else picked[:1]
        out.append({"picked": picked, "other": other})
    return out


def interact(card: dict[str, Any], action: str, value: Any = None) -> str | None:
    """Joy 在问题卡上的一次操作（stbcc-hub 调；只认 owner 由调用方保证）。L<j>：点了第 j 个字母（单选换成它，多选切换）；
    other：「其他…」弹框交上来的文字（单选时它就是这题的选择）；prev / next：翻页，最后一题的下一页是回答总览；
    submit：在总览页整轮交给处理器（没答的报未回答）。返回给 Joy 的临时提示（None = 卡已更新）。"""
    p = card["payload"]
    qs, ans = p["questions"], p["answers"]
    i = p["current"]
    if action == "submit" and isinstance(value, list):   # 网页一次交整张卡（有填空题的卡在网页上一次展开）
        got = _set_all(qs, value)
        if isinstance(got, str):
            return got
        p["answers"] = got
        cards.resolve(card, "submit", got)
        return None
    if action == "submit" and i < len(qs) and qs[i].get("notes"):   # 备注页（最后一页）直接提交
        cards.resolve(card, "submit", ans)
        return None
    if i >= len(qs):                                   # 回答总览：只能提交或回到最后一题
        if action == "submit":
            cards.resolve(card, "submit", ans)
            return None
        if action != "prev":
            return "在回答总览页：要改就点上一题回去"
        p["current"] = len(qs) - 1
        cards.save(card)
        return None
    q, a = qs[i], ans[i]
    if q["type"] == "fill" and action == "other":       # 填空题：「填写」弹框交上来的字（空 = 不答）
        a["text"] = str(value or "").strip()[:MAX_TEXT]
    elif q["type"] == "fill" and action not in ("prev", "next"):
        return f"这一题是填空，没有「{action}」这个操作（能做：填写、上一题、下一题），可能是旧卡：看最新的那张卡再点"
    elif action.startswith("L") and action[1:].isdigit() and int(action[1:]) < len(q["options"]):
        j = int(action[1:])
        if q["type"] == "single":
            a.update(picked=[j], other="")
        else:
            a["picked"] = sorted(set(a["picked"]) ^ {j})
    elif action == "other":
        text = str(value or "").strip()[:MAX_TEXT]
        a["other"] = text
        if q["type"] == "single" and text:
            a["picked"] = []
    elif action in ("prev", "next"):
        n = i + (1 if action == "next" else -1)
        if n < 0:
            return "已经是第一题"
        p["current"] = n                               # n == 题数：回答总览
    else:
        letters = "、".join(LETTERS[k] for k in range(len(q["options"])))
        return f"这一题没有「{action}」这个操作（能点：{letters}、{OTHER}、上一题、下一题），可能是旧卡：看最新的那张卡再点"
    cards.save(card)
    return None


PERMISSION_KIND = "permission"         # 通用审批卡：以 sparky 跑的工具要 Joy 同意才做的事，同意后工具自己执行
MAX_DETAIL = 1500                      # 卡上细节（如 diff）最多显示多少字
MAX_ACTION = 300                       # 卡上 action 那一句最多显示多少字
MAX_PASSWORD_LABEL = 45                # 密码框的说明最多多少字（Discord 弹框字段名的上限）
MAX_TITLE = 60                         # 工具给的卡标题最多多少字


def permission_card(pl: dict[str, Any]) -> dict[str, Any]:
    """通用审批卡（payload {run, tool, action, detail?, key, image?, password?, title?, auto?}）→ 中枢 card 接口的参数：调用方给的标题正文不用，按 payload 生成；
    title（可选）：卡的标题（工具给，如「☁️ 下载 a.tar.gz」），不写是「🔐 <工具> 要你同意」；auto（可选）：同意后由平台自己做完（一次性监听），网关给模型的结果不附「再调用一次」。
    卡面文字（action 一句话、detail 细节）由工具自己给。key 是工具认「同一件事」的指纹（续跑时 card_use 按它对上）；run 是发卡的运行（网关就地等它，等不到就让出）。
    call（可选）：发卡的那次主模型调用（工具经 tool_kit.ask_permission 发卡时自动带上），Joy 同意时平台照原样执行它。
    image（可选）：附图的宿主路径（如提交前那一屏截图；中枢 hub.card_file 只认工作目录 media/ 等几处），各入口随卡显示。
    password（可选）：密码框的说明文字。写了就是要 Joy 亲自输入密码的卡：按钮是「输入密码」（弹框，字段标 secret：值只放中枢内存、取一次，
    卡片记录、处理结果、日志里都不留）和「拒绝」；值只交回发卡的那次调用（工具就地等、经中枢的 secret op 取），网关不等这张卡、不让出（new_for_run）。
    不合格抛 ValueError、不出卡。"""
    if not all(isinstance(pl.get(k), str) and pl[k].strip() for k in ("run", "tool", "action", "key")):
        raise ValueError("permission 卡的 payload 要有 run、tool、action、key（非空字符串）")
    for k in ("detail", "image", "password", "call", "title", "auto"):
        if pl.get(k) is not None and not isinstance(pl[k], str):
            raise ValueError(f"permission 卡的 payload 里 {k} 要是字符串")
    pl = {k: str(pl[k]) for k in ("run", "tool", "action", "detail", "key", "image", "password", "call", "title", "auto") if pl.get(k)}
    action = " ".join(pl["action"].split())
    pl.update(run=pl["run"][:200], tool=pl["tool"][:60], action=action[:MAX_ACTION], key=pl["key"][:128])
    if pl.get("title"):
        pl["title"] = " ".join(pl["title"].split())[:MAX_TITLE]
    if pl.get("call"):
        pl["call"] = pl["call"][:64]
    detail, full = pl.get("detail", ""), None
    if len(detail) > MAX_DETAIL or len(action) > MAX_ACTION:
        if not pl.get("image"):                    # 太长：全文（action 与细节都不截）作附件（没有附图时），卡面只显示前面
            full = cards.attach_dir(PERMISSION_KIND) / f"{hashlib.sha256((action + detail).encode()).hexdigest()[:16]}.md"
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f"{action}\n\n{detail}\n")
    more = "（太长，全文见附件）" if full else "（太长，只显示前面）"
    if len(detail) > MAX_DETAIL:
        pl["detail"] = detail = detail[:MAX_DETAIL] + f"\n…{more}"
    shown = pl["action"] + (f"…{more}" if len(action) > MAX_ACTION else "")
    body = f"任务 {pl['run'].split('/', 1)[0]} 的工具 **{pl['tool']}** 想：{shown}" + (f"\n{detail}" if detail else "")
    card = {"kind": PERMISSION_KIND, "title": pl.get("title") or f"🔐 {pl['tool']} 要你同意", "body": body, "ttl_seconds": None, "payload": pl,
            **({"file": pl["image"]} if pl.get("image") else {"file": str(full)} if full else {})}
    if pl.get("password"):
        pl["password"] = " ".join(pl["password"].split())[:MAX_PASSWORD_LABEL]
        card["body"] += "\n点「输入密码」亲自填：密码只交给这一次调用，不显示、不记录"
        return {**card, "buttons": [{"id": "approve", "label": "输入密码", "style": "primary", "modal": True}, {"id": "reject", "label": "拒绝", "style": "danger"}],
                "modal": {"title": f"{pl['tool']} 要密码"[:45], "fields": [{"id": "password", "label": pl["password"], "secret": True}]}}
    return {**card, "buttons": [{"id": "approve", "label": "同意", "style": "success"}, {"id": "reject", "label": "拒绝，你应该…", "style": "danger", "modal": True}],
            "modal": {"title": "拒绝，你应该…", "fields": [{"id": "feedback", "label": "哪里不对、该怎么改（不写 = 不要做）", "long": True}]}}


def _permission(card: dict[str, Any], action: str, value: Any) -> str:
    pl = card.get("payload") or {}
    what = str(pl.get("action", ""))
    if action == "approve" and pl.get("password"):
        return f"Joy 输入了密码（只交给发卡的那次调用，不记录）：{what}"
    if action == "approve":
        return f"Joy 同意了：{what}"
    feedback = str((value or {}).get("feedback") or "").strip() if isinstance(value, dict) else ""
    return f"Joy 拒绝了（{what}），他说：「{feedback[:1000]}」，照他说的改完再提议" if feedback else f"Joy 拒绝了（{what}），不要再提议"


cards.register(PERMISSION_KIND, _permission, review=permission_card, sparky=True)
