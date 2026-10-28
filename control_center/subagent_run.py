"""主模型调用文件夹写法的子代理（subagents/<名>/subagent.yaml，control_center/subagents.py 的 SubagentDef）时网关怎么跑它（§二、§3.2）：
网关（gateway.py 的 _call_subagent）负责换模型、单次上限、hooks、起工具与程序、起 agent 的 pi 容器，这里是和网关无关的部分——
agent 的会话目录与状态、隔离核对、output.py。

- agent：和主 agent 一样由沙箱容器里的 pi 跑工具循环（gateway._run_agent_pi，每次调用另起一个容器）：系统提示就是 yaml 的 system_prompt
  （平台不拼共用说明，交回方式、隔离的交回格式都写在各自的提示里）；用户消息 = 主模型的 prompt（附了 files 就列出路径）。pi 经网关扩展拉工具清单、转发调用，
  网关按这次调用的令牌只给它自己的工具；pi 最后一条不调用工具的回复就是交回的结果。没有轮数上限，只受单次上限（网关管）；子代理调不到子代理。
  上下文快满时 pi 自己压缩（写摘要，同主 agent 的 sandbox.PI_SETTINGS），模型报超长时先压缩再重试；隔离核对按网关另存的原文，不看摘要。
  工具结果里的图片（网关按主模型那条路的上限筛过：一次最多 4 张、单张 base64 不超过 1.5 MB）：档位能看图（models.yaml 的 vision: true）就随结果交给 pi，
  看不了图的只在结果后面注明没附上（IMAGES_SKIPPED）。
  会话目录：运行目录的 SUB_DIR/<子代理>-<序号>/（pi 的会话文件 session/、每次起 pi 的事件流 pi-output-<n>.jsonl、STATE）。上一次调用没交回结果就结束了
  （卡片让出、暂停 / 抢占、超时、出错、运行被停），同一次运行里再调用这个子代理时接着原会话（RESUMED_NOTE + 这次的 prompt 作为新的用户消息）；
  正常交回过的下次开新会话。续上时网关进程可能已经换了，所以隔离的原文与来源（raw、labels）、打回次数、问过的 prompt 都存在 STATE 里。
- generator / engine：把 prompt + inputs 交给它的程序（server 的同名函数），程序的结果就是交回的结果。
- isolated（读外部内容）：交回的只能是 {"answers": [{question, answer, quote, source[, item]}]}（格式写在各隔离子代理自己的 system_prompt 里；item 可选，正整数，标第几项，如第几封邮件）。agent 的每次工具结果开头标着来源编号〔来源 sN〕，
  平台记下原文和它的来源（工具在结构化结果里给的 source：浏览器当前页网址、附件文件名、笔记路径…，平台不猜；没给为 null）。每条的 question 要照抄主模型这次调用写的文字（check_questions：忽略空白、大小写后是 prompt
  或某个 inputs 字符串的一段）——不然 question 就成了夹带外部内容的口子；同一个问题要对几项分别作答（如每封邮件）就照抄同一句、用 item 区分。
  每条有答案的都要有 quote，quote 要逐字出现在 source 那段原文里（owa 的 quote_in：忽略空白、大小写），答案里的编号 / 多位数字要出现在 quote 里
  （owa 的 missing_tokens）。question 不照抄、格式不对：打回重写（最多 MAX_REJECTS 次），最后一次还有不照抄的 question 整条去掉；
  最后一次格式仍不对不算失败：交回没有答案的 {answers: [], unanswered, sources: {}}（format_failed），unanswered 逐条列它交回里认得出的照抄问题、原因 REASON_FORMAT，
  认不出就一条总的（question 为 null）；
  引文核对不上（含答案里的数字不在引文里）不打回，答案照交、标 unverified: true。主模型拿到的是 {answers, unanswered, sources}（shape），没有别的自由文字：
  answers 按原顺序（引文核对不上的带 unverified: true）；unanswered [{question, reason[, item]}] 按原顺序列每个没答上的问题和原因
  （子代理写了 null 的；question 不是照抄的不带文字，question 为 null）；sources {来源编号: 来源} 只列 answers 引用到的来源（来源就是工具给的 source，没给为 null）。
- output.py（可选，在子代理文件夹里）：stdin 给 {subagent, kind, prompt, inputs, result}；退出码 0 = 合格（stdout 写 {"result": …} 就换成它，不写照旧），
  2 = 打回（stderr 是原因，交给子代理重写，计入 MAX_REJECTS），别的 = 出错（子代理出错）。身份同子代理的 hooks（is_editable: false 以平台身份跑）。
  isolated 的先过平台核对再跑 output.py；output.py 换掉的答案要再核对一遍（问题、引文核对不上算子代理出错），unanswered、sources 只用平台的，放宽不了。
- isolated 的 engine（SubagentDef.prevalidated：交回它自己 harness 校验过的结果，由它的 server 程序按格式校验，如联网研究的 ResearchResult）：
  不过问答核对，交回的 JSON 对象原样交给主模型（prevalidated_result 只查是 JSON 对象），里面有什么键由它自己定。
- 任何子代理的结果（JSON 对象）里有 images [{file: 工作目录 media/ 下的文件, …}]：网关把这些图附给主模型看（gateway._result_images，沿用图片张数与大小上限）。
- 被打回的草稿（所有子代理一样）：每次交回的结果被打回（格式、问题核对、output.py），或最后一次仍不合格这次调用失败，
  那版原文和原因追加进运行目录的 REJECTED（{ts, subagent, why, final, draft}；final = 这次调用因此失败）。运行目录主模型看不到，不影响隔离。
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from owa_research.core.answers import missing_tokens, quote_in

MAX_REJECTS = 2                 # 交回的结果被打回（格式、问题核对、output.py）后最多重写几次；再不合格按最后一次处理
OUTPUT_TIMEOUT = 60             # output.py 的时限（秒）
REJECTED = "subagent_rejected.jsonl"   # 运行目录里：子代理每次被打回的那版原文和原因（见模块说明）
MAX_ANSWERS = 500              # 一次交回最多几条答案（邮件摘要一次读完一个邮箱的新邮件、每封五六条）
MAX_ITEM = 10_000              # item（第几项）的上限
ANSWER_LIMITS = {"question": 500, "answer": 2000, "quote": 1500, "source": 40}
REASON_NULL = "原文里没找到"
REASON_QUESTION = "问题不是照抄主模型原文，已去掉"
REASON_FORMAT = "子代理交回的格式不对"
REJECT_NOTE = "（平台）你交回的结果不合格：{why}\n按要求重新交回（需要的话可以再调用工具核对原文）。"
RESUMED_NOTE = "（平台）上一次调用你时没等到交回结果就中断了；这是接着原来的对话，下面是主模型这次的要求。\n\n"
EMPTY_NOTE = "（平台）你上一条回复没有任何文字，也没有调用工具，调用就停下了。请接着把事情做完；做完了就直接交回结果。"
IMAGES_SKIPPED = "\n（平台：这个结果附带 {n} 张图片，你的模型看不了图，没有附上）"
IMAGES_ISOLATED = "\n（平台：这个结果附带的图片里的文字没法逐字核对，答案的引文要从文字结果里复制）"
SOURCE_MARK = "〔来源 {sid}〕\n"
SUB_DIR = "subagents"           # 运行目录里 agent 子代理的会话目录：<子代理>-<序号>/
STATE = "state.json"            # 会话目录里的状态：{status: open / done, raw, labels, rejects, prompts, launches}


class SubagentRunError(ValueError):
    """子代理交回的结果不合格且已用完重写次数、output.py 出错：网关按「子代理出错」告诉主模型。"""


class FormatFailed(SubagentRunError):
    """隔离 agent 子代理最后一次交回的格式仍不对：不算失败，交回 result（format_failed：没读成，问题进 unanswered）。"""

    def __init__(self, why: str, result: dict[str, Any]) -> None:
        super().__init__(why)
        self.result = result


class Reject(Exception):
    """这次交回的结果被打回（还能重写）。"""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def system_text(d: Any, work: Any = None) -> str:
    """agent 子代理的系统提示：yaml 的 system_prompt（各子代理自己写，平台不拼共用说明；隔离的交回格式也写在它自己的提示里）；
    它的工具里有平台工具 workspace 时接上工作目录 work 的说明（prompts.workspace_text，函数是 pi 自带的、没有第一次调用先给规范那一轮）。"""
    from control_center.prompts import workspace_text

    ws = workspace_text(d.tools, work) if work is not None else ""
    return "\n\n".join(x for x in (str(d.system_prompt or "").strip(), ws) if x)


def user_text(prompt: str, files: list[str]) -> str:
    """agent 子代理的用户消息：主模型的 prompt，附了文件就列出路径（子代理用自己的工具读）。"""
    return prompt + ("\n\n附带的文件（工作目录里的路径）：\n" + "\n".join(f"- {f}" for f in files) if files else "")


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def parse_answers(obj: Any) -> list[dict[str, Any]]:
    """isolated 的交回 {"answers": [...]} → 答案列表；格式不对抛 Reject（说清哪里不对）。"""
    if not isinstance(obj, dict) or set(obj) != {"answers"} or not isinstance(obj["answers"], list):
        raise Reject('最后的回复要是只有 answers 一个键的 JSON 对象：{"answers": [{"question", "answer", "quote", "source", "item"（可选）}]}，不要别的字段和文字')
    if len(obj["answers"]) > MAX_ANSWERS:
        raise Reject(f"answers 最多 {MAX_ANSWERS} 条")
    out = []
    for i, a in enumerate(obj["answers"], 1):
        if not isinstance(a, dict) or set(a) - {"item"} != set(ANSWER_LIMITS):
            raise Reject(f"第 {i} 条要正好有 question、answer、quote、source 四个键（另可带 item）")
        if "item" in a and not (type(a["item"]) is int and 1 <= a["item"] <= MAX_ITEM):
            raise Reject(f"第 {i} 条的 item 要是正整数（第几项，如第几封邮件），不能是文字")
        if not (isinstance(a["question"], str) and a["question"].strip()):
            raise Reject(f"第 {i} 条的 question 要是非空字符串")
        for k, n in ANSWER_LIMITS.items():
            if a[k] is not None and not isinstance(a[k], str):
                raise Reject(f"第 {i} 条的 {k} 要是字符串或 null")
            if isinstance(a[k], str) and len(a[k]) > n:
                raise Reject(f"第 {i} 条的 {k} 太长（最多 {n} 字）")
        out.append({**{k: (a[k].strip() if isinstance(a[k], str) else None) for k in ANSWER_LIMITS}, **({"item": a["item"]} if "item" in a else {})})
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def asked_texts(prompt: str, inputs: dict[str, Any]) -> list[str]:
    """主模型这次调用写的文字：prompt 和 inputs 里的每个字符串（列表、映射里的也算），都按 _norm 规整过。"""
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(_norm(v))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    walk(prompt)
    walk(inputs)
    return [t for t in out if t]


def check_questions(answers: list[dict[str, Any]], asked: list[str]) -> tuple[list[dict[str, Any]], list[int]]:
    """逐条核对 question 是主模型写的：规整后是 asked（asked_texts）里某段文字的一部分。
    返回 (合格的条目, 不合格的条目序号（从 1 数）)。"""
    ok, bad = [], []
    for i, a in enumerate(answers, 1):
        q = a["question"]
        if (nq := _norm(q)) and any(nq in t for t in asked):
            ok.append(a)
        else:
            bad.append(i)
    return ok, bad


def checked(answers: list[dict[str, Any]], raw: dict[str, str]) -> list[tuple[dict[str, Any], bool]]:
    """逐条核对：有答案的要有 quote，quote 逐字在 source 那段原文里（source 不对就在全部原文里找，找到改成那一段），答案里的编号 / 多位数字在 quote 里。
    返回 [(条目, 核对上了没有)]，原顺序；没答案的条目 quote / source 置空、算核对上。"""
    out = []
    for a in answers:
        if not a["answer"]:
            out.append(({**a, "answer": None, "quote": None, "source": None}, True))
            continue
        q = a["quote"] or ""
        sid = a["source"] if a["source"] in raw and quote_in(q, raw[a["source"]]) else next((k for k, t in raw.items() if quote_in(q, t)), None)
        ok = sid is not None and not missing_tokens(a["answer"], q)
        out.append(({**a, "source": sid if sid is not None else (a["source"] if a["source"] in raw else None)}, ok))
    return out


def shape(rows: list[tuple[dict[str, Any], bool]], wrong: int, labels: dict[str, str | None]) -> dict[str, Any]:
    """核对后（checked）给主模型的结果：answers 是有答案的条目（原顺序，引文核对不上的加 unverified: true）；unanswered 按原顺序列
    子代理写了 null 的问题（原因 REASON_NULL），question 不是照抄的 wrong 条只记原因（question 为 null，不带它的文字）；
    sources 是 answers 引用到的来源编号 → 来源（labels：工具给的 source，没给为 null）。"""
    answers = [a if good else {**a, "unverified": True} for a, good in rows if a["answer"]]
    unanswered = [{"question": a["question"], "reason": REASON_NULL, **({"item": a["item"]} if "item" in a else {})}
                  for a, good in rows if not a["answer"]]
    unanswered += [{"question": None, "reason": REASON_QUESTION} for _ in range(wrong)]
    return {"answers": answers, "unanswered": unanswered,
            "sources": {sid: labels.get(sid) for sid in dict.fromkeys(a["source"] for a in answers if a["source"])}}


def format_failed(obj: Any, asked: list[str]) -> dict[str, Any]:
    """最后一次交回的格式仍不对时给主模型的结果：没有答案，问题都进 unanswered（原因 REASON_FORMAT）。交回里认得出照抄主模型的问题
    （同 check_questions：规整后是主模型这次写的文字的一段）就逐条列（同一问题、同一 item 只列一次），认不出就一条总的（question 为 null）。"""
    rows = obj.get("answers") if isinstance(obj, dict) else None
    out: list[dict[str, Any]] = []
    for a in rows if isinstance(rows, list) else []:
        q = a.get("question") if isinstance(a, dict) else None
        if not (isinstance(q, str) and (nq := _norm(q)) and any(nq in t for t in asked)):
            continue
        item = a["item"] if type(a.get("item")) is int and 1 <= a["item"] <= MAX_ITEM else None
        row = {"question": q.strip()[:ANSWER_LIMITS["question"]], "reason": REASON_FORMAT, **({"item": item} if item else {})}
        if row not in out:
            out.append(row)
    return {"answers": [], "unanswered": out or [{"question": None, "reason": REASON_FORMAT}], "sources": {}}


def isolated_result(text: str, raw: dict[str, str], last: bool, asked: list[str] = [],  # noqa: B006
                    labels: dict[str, str | None] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """isolated agent 的交回 → ({answers, unanswered, sources}（shape）, 原文)。asked：核对 question 用（check_questions）；labels：agent 的来源编号 → 工具给的 source。
    格式不对、question 不是照抄的：没到最后一次打回（Reject），最后一次格式不对抛 FormatFailed（带 format_failed 的结果）、question 不对的整条去掉（进 unanswered）；
    引文核对不上的不打回，答案标 unverified（shape）。"""
    try:
        obj = json.loads(_strip_fence(text))
    except ValueError:
        obj = None
    try:
        answers = parse_answers(obj)
    except Reject as r:
        if last:
            raise FormatFailed(f"交回的结果格式不对（{r.why}），已打回 {MAX_REJECTS} 次", format_failed(obj, asked)) from None
        raise
    answers, wrong = check_questions(answers, asked)
    if wrong and not last:
        raise Reject(f"第 {'、'.join(map(str, wrong[:20]))} 条的 question 不是主模型问的：question 要从用户消息里逐字复制那个问题（可以只抄问题那一句），"
                     "不改写、不翻译、不加自己的话；同一个问题对几项分别作答时照抄同一句、用 item 标第几项")
    rows = checked(answers, raw)
    return shape(rows, len(wrong), labels or {}), raw


def prevalidated_result(text: str, last: bool) -> dict[str, Any]:
    """prevalidated engine 的交回：JSON 对象原样交给主模型（内容由它自己的 server 程序校验）。不是 JSON 对象：没到最后一次打回，最后一次抛 SubagentRunError。"""
    try:
        obj = json.loads(_strip_fence(text))
        if not isinstance(obj, dict):
            raise Reject("engine 的交回要是一个 JSON 对象")
        return obj
    except (ValueError, Reject) as e:
        why = e.why if isinstance(e, Reject) else f"不是 JSON（{e}）"
        if last:
            raise SubagentRunError(f"交回的结果格式不对（{why}），已打回 {MAX_REJECTS} 次") from None
        raise Reject(why) from None


def run_output(d: Any, payload: dict[str, Any], run_dir: Path | None, work_dir: Path | None, run_id: str) -> tuple[Any, str | None]:
    """跑子代理的 output.py：返回 (结果, 打回原因)。退出码 0：stdout 有 {"result": …} 就换成它；2：打回；别的、超时、stdout 不对：抛 SubagentRunError。同步，调用方在线程里跑。"""
    from control_center import hooks

    owner = hooks.Owner("subagent", d.name, {}, d.is_editable)
    argv, env = hooks._command(owner, hooks.Hook(command=str(d.output), timeout=OUTPUT_TIMEOUT), run_dir, work_dir, run_id)
    try:
        p = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False, default=str).encode(), capture_output=True,
                           timeout=OUTPUT_TIMEOUT, env=env, cwd=str(d.folder))
    except subprocess.TimeoutExpired:
        raise SubagentRunError(f"output.py 超时（{OUTPUT_TIMEOUT} 秒）") from None
    err = p.stderr.decode(errors="replace").strip()
    if p.returncode == 2:
        return payload["result"], err or "output.py 打回了（没写原因）"
    if p.returncode != 0:
        raise SubagentRunError(f"output.py 出错（退出码 {p.returncode}）：{err[-300:]}")
    out = p.stdout.decode(errors="replace").strip()
    if not out:
        return payload["result"], None
    try:
        got = json.loads(out)
        if not isinstance(got, dict) or "result" not in got:
            raise ValueError("要是 {\"result\": …}")
    except ValueError as e:
        raise SubagentRunError(f"output.py 的 stdout 不是 {{\"result\": …}}：{e}") from None
    return got["result"], None


def record_rejected(run_dir: Path | None, name: str, text: str, why: str, final: bool) -> None:
    """被打回的那版原文和原因追加进运行目录的 REJECTED；没有运行目录、写不了就算了（不影响这次调用）。"""
    if run_dir is None:
        return
    try:
        with open(Path(run_dir) / REJECTED, "a") as f:
            f.write(json.dumps({"ts": time.time(), "subagent": name, "why": why, "final": final, "draft": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


async def finish(d: Any, text: str, raw: dict[str, str], last: bool, *, prompt: str, inputs: dict[str, Any],
                 run_dir: Path | None, work_dir: Path | None, run_id: str, labels: dict[str, str | None] | None = None) -> Any:
    """子代理交回的文字 → 给主模型的结果（_finish）；被打回或最后一次仍不合格时，那版原文和原因记进运行目录（record_rejected）。
    隔离 agent 最后一次格式仍不对（FormatFailed）：记下后交回它的结果（没读成），不算子代理出错。"""
    try:
        return await _finish(d, text, raw, last, prompt=prompt, inputs=inputs, run_dir=run_dir, work_dir=work_dir, run_id=run_id, labels=labels)
    except Reject as r:
        record_rejected(run_dir, d.name, text, r.why, False)
        raise
    except SubagentRunError as e:
        record_rejected(run_dir, d.name, text, str(e), True)
        if isinstance(e, FormatFailed):
            return e.result
        raise


async def _finish(d: Any, text: str, raw: dict[str, str], last: bool, *, prompt: str, inputs: dict[str, Any],
                  run_dir: Path | None, work_dir: Path | None, run_id: str, labels: dict[str, str | None] | None = None) -> Any:
    """子代理交回的文字 → 给主模型的结果：isolated 先过引文核对，再跑 output.py（有的话）。要打回抛 Reject（last 时抛 SubagentRunError）。
    labels：agent 的来源编号 → 工具给的 source（网关处理它的工具调用时记的）。"""
    asked = asked_texts(prompt, inputs)
    if d.isolated and d.prevalidated:
        result = prevalidated_result(text, last)
    elif d.isolated:
        result, raw = isolated_result(text, raw, last, asked=asked, labels=labels)
    elif d.kind == "engine":
        try:
            result = json.loads(text)
        except ValueError:
            result = text
    else:
        result = text
    if d.output is None:
        return result
    got, why = await asyncio.to_thread(run_output, d, {"subagent": d.name, "kind": d.kind, "prompt": prompt, "inputs": inputs, "result": result},
                                       run_dir, work_dir, run_id)
    if why is not None:
        if last:
            raise SubagentRunError(f"output.py 打回了 {MAX_REJECTS + 1} 次：{why}")
        raise Reject(why)
    if d.isolated and not d.prevalidated and got != result:      # output.py 只能更严：换掉的结果照样要过核对
        try:
            answers = parse_answers({"answers": got.get("answers")} if isinstance(got, dict) else None)
        except Reject as r:
            raise SubagentRunError(f"output.py 改出的结果不是核对过的答案格式：{r.why}") from None
        if check_questions(answers, asked)[1]:
            raise SubagentRunError("output.py 改出了不是主模型问的问题（isolated 的结果只能比平台核对的更严）")
        rows = checked(answers, raw)
        if not all(good for _, good in rows):
            raise SubagentRunError("output.py 改出了核对不上的答案（isolated 的结果只能比平台核对的更严）")
        new = shape(rows, 0, labels or {})       # output.py 去掉的答案按「没找到」补进 unanswered；平台记的没答上的照留，output.py 带不进别的文字
        listed = {(u["question"], u.get("item")) for u in result["unanswered"]}  # pyright: ignore[reportArgumentType, reportAttributeAccessIssue]
        got = {"answers": new["answers"], "unanswered": result["unanswered"] + [u for u in new["unanswered"] if (u["question"], u.get("item")) not in listed],  # pyright: ignore[reportArgumentType, reportOperatorIssue]
               "sources": new["sources"]}
    return got


async def program_loop(d: Any, args: dict[str, Any], *, call: Callable[[dict[str, Any]], Awaitable[str]],
                       done: Callable[[str, dict[str, str], bool], Awaitable[Any]]) -> Any:
    """generator / engine：把 prompt + inputs 交给程序，结果交 done（finish）核对；被打回就把原因接在 prompt 后面重跑，打回超过 MAX_REJECTS 次按最后一次处理。"""
    rejects, a = 0, dict(args)
    while True:
        text = await call(a)
        try:
            return await done(text, {}, rejects >= MAX_REJECTS)
        except Reject as r:
            rejects += 1
            a = {**args, "prompt": f"{args.get('prompt', '')}\n\n{REJECT_NOTE.format(why=r.why)}"}


# ---------- agent 子代理的会话目录（gateway._run_agent_pi）----------
def new_state() -> dict[str, Any]:
    return {"status": "open", "raw": {}, "labels": {}, "rejects": 0, "prompts": [], "launches": 0}


def load_state(folder: Path) -> dict[str, Any]:
    """会话目录的 STATE；读不到（还没写、坏了）当新的。"""
    try:
        st = json.loads((folder / STATE).read_text())
    except (OSError, ValueError):
        return new_state()
    return {**new_state(), **st} if isinstance(st, dict) else new_state()


def save_state(folder: Path, state: dict[str, Any]) -> None:
    tmp = folder / (STATE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(folder / STATE)


def open_session(run_dir: Path, name: str, busy: set[str]) -> tuple[Path, dict[str, Any], bool]:
    """这次调用子代理 name 用的会话目录：这次运行里它最近一个不在用（busy 是在途调用占着的目录名）的会话还没交回结果（status open）就接着用，
    否则新建 SUB_DIR/<名>-<序号>/。返回 (会话目录, 状态, 是不是接着上一次)。"""
    root = Path(run_dir) / SUB_DIR
    seq = lambda d: int(d.name[len(name) + 1:])   # noqa: E731
    mine = sorted((d for d in root.glob(f"{name}-*") if d.is_dir() and d.name[len(name) + 1:].isdigit()), key=seq) if root.is_dir() else []
    idle = [d for d in mine if d.name not in busy]
    if idle and (st := load_state(idle[-1]))["status"] == "open":
        return idle[-1], st, True
    folder = root / f"{name}-{(seq(mine[-1]) if mine else 0) + 1}"
    folder.mkdir(parents=True)
    st = new_state()
    save_state(folder, st)
    return folder, st, False


def session_file(folder: Path) -> Path | None:
    """会话目录里 pi 的会话文件（最新的那个）；pi 第一轮回答写完之前还没有。"""
    files = sorted((folder / "session").glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if (folder / "session").is_dir() else []
    return files[-1] if files else None
