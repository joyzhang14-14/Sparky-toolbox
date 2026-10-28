"""网页聊天：私信任务（订阅消息 listen_events: [message] 的任务，现在接消息的那个）的网页入口（适配器）。
- 现在接私信的任务（current_task）：data/messages/route.json 的 holder，没人接管时是它的 default（channels.current_task；route.json 由私信任务的 listen.py 写）。
- 发消息：附件规则同 Discord（图片随消息给模型看，音视频进任务工作目录的 in/，其余类型的文件交能读附件的隔离子代理（如 isolated-reader）读，单个 ≤ 10 MB），先放进本渠道的暂存目录（channels.staging），
  发布来源 message 的事件交聊天中枢 stbcc-hub（publish，channel: web），由订阅它的任务的 listen.py 决定接不接（附件中枢拷进接下它的任务），交完删掉暂存的；
  这次运行的回复由中枢转回网页（见下面的适配器），不发 Discord。对话记录归任务，网页不记。
- 适配器：stbcc-web 启动时开 data/sockets/channels/web.sock 向中枢报到（接口说明见 hub.py）。网页靠轮询文件显示，所以只有两个 op 要做事：
  send（系统通知）记进 data/hub/web/notices.jsonl，聊天按时间插进对话；reply（网页发起的运行的回复）写进运行目录的 web/outbox.jsonl；
  没有入口的定时任务消息（role cron）网页回收到（ok）但不存：聊天从任务消息库 data/messages/tasks.sqlite3 读它们（_task_messages），按时间插进对话、标「📬 任务名」。
  卡片、进度、状态栏网页自己从文件轮询，这些 op 直接回 ok；别的入口发起的运行的回复回 skipped（网页从运行记录里展示私信任务的回复）。
- 命令：命令表 data/messages/commands.json（私信任务各自登记，channels.commands）里的命令，状态里带给页面做补全；命令由任务的 listen.py 认，
  回话记在 data/hub/web/system.jsonl。
- 对话记录：按私信任务的运行拼（两个入口的都在）：用户消息、插话、工具调用、回复、token 与耗时、这次运行的卡片。
- 卡片：问题卡与审批卡网页上也能处理：决定交中枢（card_act），中枢再让 Discord 上的那张卡跟着改。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

from control_center import cards, acquire_permission, builder, hub_client, hub, runner, sandbox, todo
import channels
from channels import IMAGE_EXT, MAX_ATTACHMENT_BYTES, MESSAGE, commands, doc_name, holder_since, staging
from control_center.models import is_up, load_models, profiles, running
from control_center.paths import APPROVALS, CONTROL, HUB, INBOX, MESSAGES, QUEUE, RUNNER_STATE, RUNS, WORK, task_file
from control_center.reply import TOOL as REPLY_TOOL
from control_center.task import TaskError, load_task, subscribers
from control_center.console.api import resume_queued, shown_status

MAX_FILES = 10
HUB_DOWN = "聊天中枢 stbcc-hub 没有响应"
_CACHE: dict[str, tuple[float, Any]] = {}


def current_task() -> str:
    """现在接私信的任务（channels.current_task：route.json 的 holder，没人接管时是 default）；聊天的对话、工具栏、状态栏都跟着它。"""
    name = channels.current_task()
    if not name:
        raise TaskError("还定不下现在接私信的任务（data/messages/route.json 还没有：私信任务收到第一条私信时写）")
    return name


def _cached(key: str, ttl: float, fn: Any) -> Any:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[key] = (time.time(), v)
    return v


def _system_log() -> Path:
    return HUB / "web" / "system.jsonl"


def _say(command: str, reply: str) -> dict[str, Any]:
    p = _system_log()
    p.parent.mkdir(parents=True, exist_ok=True)
    item = {"ts": time.time(), "command": command, "reply": reply}
    with p.open("a") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return {"system": item}


def _save_files(msg_id: str, files: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """网页上传的附件 [{name, type, data(base64)}] → 放进本渠道的暂存目录，返回 (图片的宿主路径, 其他文件的宿主路径, 跳过的文件名)；命名同 Discord 私信。"""
    images: list[str] = []
    docs: list[str] = []
    skipped: list[str] = []
    for i, f in enumerate(files[:MAX_FILES]):
        fname = str(f.get("name") or "file")
        try:
            data = base64.b64decode(str(f.get("data") or ""), validate=True)
        except ValueError:
            skipped.append(fname)
            continue
        ext = IMAGE_EXT.get(str(f.get("type") or "").split(";")[0].strip())
        doc = None if ext else doc_name(msg_id, i, fname)
        if not (ext or doc) or not data or len(data) > MAX_ATTACHMENT_BYTES:
            skipped.append(fname)
            continue
        p = staging("web") / (f"{msg_id}-{i}{ext}" if ext else str(doc))
        p.write_bytes(data)
        (images if ext else docs).append(str(p))
    skipped += [str(f.get("name") or "file") for f in files[MAX_FILES:]]
    return images, docs, skipped


def send(text: str, files: list[dict[str, Any]] | None = None, origin: str | None = None) -> dict[str, Any]:
    """网页发一条消息：附件放进暂存目录后发布来源 message 的事件（publish，channel: web），由订阅它的任务的 listen.py 接下入队或回一句话（命令）。
    origin="cli"（stb chat send，Claude 测试用）：同样走网页入口，事件带 origin=cli、运行带 source.origin=cli，私信任务据此不记进对话库。"""
    text = (text or "").strip()
    msg_id = f"web{int(time.time() * 1000)}"
    images, docs, skipped = _save_files(msg_id, list(files or [])) if files else ([], [], [])
    try:
        if not text and not images and not docs:
            raise TaskError("消息是空的" + (f"（跳过了附件 {'、'.join(skipped)}：只收 10 MB 以内的文件）" if skipped else ""))
        r = hub_client.call_sync("publish", timeout=hub_client.PUBLISH_TIMEOUT, source=MESSAGE, channel="web", ts=time.time(), images=images, docs=docs,
                                event={"text": text, "msg_id": msg_id, **({"origin": origin} if origin else {})})
    finally:
        for f in [*images, *docs]:
            Path(f).unlink(missing_ok=True)
    if r is None:
        raise TaskError(f"{HUB_DOWN}，这条消息没有处理")
    if not r.get("ok"):
        raise TaskError(str(r.get("error") or "没有处理"))
    if r.get("task"):                                   # 有任务接下、入队了
        return {"queued": r.get("queued"), "skipped": skipped}
    if r.get("reply") is not None:
        return _say(text, str(r["reply"]))
    raise TaskError(f"没有任务接这条消息（{'、'.join(r.get('tasks') or {}) or '没有启用的任务订阅 message'}）")


# ---- 对话记录 ----
def _jsonl(p: Path) -> list[dict[str, Any]]:
    out = []
    try:
        for line in p.read_text(errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return out


def _work_url(path: str, task: str | None = None) -> str:
    """附件路径 → 网页附件路径（file_path 认的）：任务工作目录下的文件（任何子目录，如项目的 out/）；当前私信任务的写 work/…，别的任务的（或给了 task）写 task/<任务>/…。
    旧记录里的 /work/…（改成真实路径之前）算 task（不给 = 当前私信任务）的。别处的给空。"""
    s = str(path)
    if s.startswith("/work/"):
        t, rel = task or current_task(), s[len("/work/"):]
    else:
        try:
            parts = Path(s).resolve().relative_to(WORK.resolve()).parts
        except ValueError:
            return ""
        if len(parts) < 3:
            return ""
        t, rel = parts[0], "/".join(parts[1:])
    return f"work/{rel}" if task is None and t == current_task() else f"task/{t}/{rel}"


ARG_CLIP = 2000
ARG_BRIEF = 300                                   # 对话列表里的参数只够画那一行摘要；展开时再经 call_detail 取完整的


def _clip(v: Any, n: int = ARG_CLIP) -> Any:
    """聊天里展示的工具参数：很长的字符串（如 render 的整页 HTML）截短，完整的在运行详情里。"""
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…（共 {len(v):,} 字，完整内容见运行详情）"
    if isinstance(v, dict):
        return {k: _clip(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x, n) for x in v]
    return v


_SUB_KEYS = ("ts", "ev", "subagent", "tool", "call_id", "ok", "seconds", "summary", "message", "source", "hooked", "args", "result")


def _inner_calls(gateway: list[dict[str, Any]], name: str, start: float | None, end: float | None, full: bool = True,
                 call_id: str | None = None) -> list[dict[str, Any]]:
    """子代理 name 在 call_id 这次调用里调过的工具（网关审计的 subagent_call，调用结束时记，记着属于哪次调用的 call_id；
    旧记录没有 call_id 就按时间段 [start, end] 认，end 为 None = 还在跑）→ 一次一条，和主代理的工具调用同样显示：
    {tool, at: 开始时刻, ms, error, result: 工具给的 summary / 出错说明（都没有写成功或错误码）, brief: summary / 出错说明的开头（没有不给）,
    arg: 工具给的 source, info: 网关记的其余字段（参数原文网关不记，只有 args_chars 等）}。不按工具名认，所有子代理一样。"""
    out = []
    for g in gateway:
        ts = g.get("ts")
        if g.get("ev") != "subagent_call" or g.get("subagent") != name or not isinstance(ts, (int, float)):
            continue
        if g.get("call_id") is not None:             # 按调用编号认：同一子代理并行的几次调用不混
            if g["call_id"] != call_id:
                continue
        elif (start is not None and ts < start - 1) or (end is not None and ts > end + 1):
            continue
        sec = g.get("seconds") if isinstance(g.get("seconds"), (int, float)) else None
        bad = not g.get("guide") and not g.get("ok")
        text = "先返回用法规范" if g.get("guide") else str(g.get("summary") or g.get("message") or "")
        row = {"tool": str(g.get("tool") or ""), "at": ts - (sec or 0), "ms": round(sec * 1000) if sec is not None else None, "error": bad,
               **({"args": g["args"] if full else _clip(g["args"], 120)} if isinstance(g.get("args"), dict) else {}),   # 网关从 2026-10 起记参数（截短）；之前的只有 args_chars
               **({"brief": text[:160]} if text else {}), **({"arg": str(g["source"])[:300]} if g.get("source") else {})}
        if full:                                     # 结果与网关的其余记录只在展开时给（call_detail）
            row.update(result=(str(g.get("result") or "") if g.get("result") and not bad else text or ("成功" if not bad else str(g.get("code") or "出错")))[:3000],
                       info=_clip({k: v for k, v in g.items() if k not in _SUB_KEYS}))
        out.append(row)
    return out[-80:]


def _running_inner(run_dir: Path, name: str, calls: list[dict[str, Any]], since: float | None, full: bool, prompt: str | None,
                   only: bool) -> dict[str, Any] | None:
    """子代理 name 正在调、还没返回的那个工具（网关的 subagent_call 要等它返回才记）：从这次调用的会话目录（subagents/<名>-<n>/，并行的几次各用各的）的工具日志里
    找开始了没结束的那条。哪个目录是这次调用的：它的 state.json 记着问过的 prompt，和这次调用的 prompt 一样的就是；对不上时只有这一次在跑才用最近写的那个（only）。
    日志里的 t 是子代理这一段启动后的秒数，绝对时间按它上一个工具结束（= 网关记的那条）往后推；推不出就用上一个工具结束或这次调用开始的时刻。"""
    def asked(log: Path) -> list[str]:
        try:
            return list(json.loads((log.parent / "state.json").read_text()).get("prompts") or [])
        except (OSError, ValueError, AttributeError):
            return []
    logs = sorted(run_dir.glob(f"subagents/{name}-*/toollog.jsonl"), key=lambda p: p.stat().st_mtime)
    mine = [x for x in logs if prompt and prompt in asked(x)]
    logs = mine or (logs if only else [])
    if not logs:
        return None
    opened: dict[Any, dict[str, Any]] = {}
    last_end = None
    for r in _jsonl(logs[-1]):
        if r.get("ev") == "session_start":
            opened, last_end = {}, None
        elif r.get("ev") == "tool_start":
            opened[r.get("id")] = r
        elif r.get("ev") == "tool_end":
            opened.pop(r.get("id"), None)
            last_end = r.get("t")
    if not opened:
        return None
    r = list(opened.values())[-1]
    prev = calls[-1]["at"] + (calls[-1]["ms"] or 0) / 1000 if calls and calls[-1].get("at") is not None else since
    at = prev + float(r.get("t") or 0) - float(last_end) if prev is not None and last_end is not None else prev
    args = r.get("args")
    return {"tool": str(r.get("tool") or ""), "at": at, "ms": None, "error": False, "running": True,
            **({"args": args if full else _clip(args, 120)} if isinstance(args, dict) else {}), **({"info": {}} if full else {})}


def _attach_subs(run_dir: Path, events: list[dict[str, Any]], full: bool = True) -> None:
    """给每次子代理调用（工具名是 subagents/ 里的子代理名，不按名字写死）挂上它的过程 e["sub"]：它调过的工具（网关审计的 subagent_call，每个工具调用结束就记，
    还在跑的随轮询更新），e["sub"]["calls"] 见 _inner_calls；还在跑的那次调用，末尾再加上子代理正在调的那个工具（running，见 _running_inner）。
    同一个子代理并行的几次调用按 prompt 认各自的会话目录（events 里的参数可能截短了，完整的 prompt 从工具日志 / 网关的 call_start 取）。"""
    from control_center import subagents

    names = set(subagents.folder_names())
    gateway = _jsonl(run_dir / "gateway.jsonl")
    ends = {e.get("id"): e for e in events if e["ev"] == "tool_end"}
    live = [e for e in events if e["ev"] == "tool_start" and e.get("tool") in names and e.get("id") not in ends]
    prompts: dict[str, str] = {}
    if live:
        for r in [*_jsonl(run_dir / "agent" / "toollog.jsonl"), *({**g, "id": g.get("call_id")} for g in gateway if g.get("ev") == "call_start")]:
            if r.get("ev") in ("tool_start", "call_start") and isinstance(r.get("args"), dict) and isinstance(r["args"].get("prompt"), str):
                prompts[str(r.get("id"))] = r["args"]["prompt"]
    for e in events:
        if e["ev"] != "tool_start" or e.get("tool") not in names:
            continue
        end = ends.get(e.get("id"))
        calls = _inner_calls(gateway, str(e["tool"]), e.get("at"), end.get("at") if end is not None else None, full, str(e.get("id")))
        if end is None and (now := _running_inner(run_dir, str(e["tool"]), calls, e.get("at"), full, prompts.get(str(e.get("id"))),
                                                   sum(x.get("tool") == e["tool"] for x in live) == 1)):
            calls = [*calls, now]
        if calls:
            e["sub"] = {"calls": calls}


def _events(run_dir: Path, full: bool = True) -> list[dict[str, Any]]:
    """pi 的工具日志 → 给网页的精简事件（模型每轮的文字与 token、工具调用与结果），带绝对时间 at：
    日志里的 t 是这一段 pi 启动后的秒数；第 k 段（第 k 个 session_start）从第 k 次网关启动（gateway.jsonl 的 start）算起。
    full=False（对话列表）：参数只截到够画摘要那一行，不带结果；网页展开某一条时经 call_detail 取完整的。"""
    gw = _jsonl(run_dir / "gateway.jsonl")
    starts: list[float] = []
    for g in gw:
        if g.get("ev") == "start" and g.get("ts"):
            starts.append(float(g["ts"]))
        elif g.get("ev") == "call" and g.get("origin") == "resume" and starts and isinstance(g.get("ts"), (int, float)):
            starts[-1] = max(starts[-1], float(g["ts"]))      # 续跑前平台替模型调用完才起 pi：这一段的 t 从那时算
    out, seg = [], -1
    for e in _jsonl(run_dir / "agent" / "toollog.jsonl"):
        ev = e.get("ev")
        if ev == "session_start":
            seg += 1
            continue
        base = starts[min(max(seg, 0), len(starts) - 1)] if starts else None
        at = base + float(e.get("t") or 0) if base is not None else None
        if ev == "assistant":
            out.append({"ev": "assistant", "at": at, "text": e.get("text") or "", "input": e.get("input"), "output": e.get("output"), "calls": e.get("calls") or []})
        elif ev == "tool_start":
            out.append({"ev": "tool_start", "at": at, "id": e.get("id"), "tool": e.get("tool"),
                        "args": e.get("args") if e.get("tool") == REPLY_TOOL else _clip(e.get("args"), ARG_CLIP if full else ARG_BRIEF)})   # reply 的参数就是回复正文，不截
        elif ev == "tool_end" and not full:
            out.append({"ev": "tool_end", "at": at, "id": e.get("id"), "tool": e.get("tool"), "ms": e.get("ms"), "error": bool(e.get("error"))})
        elif ev == "tool_end":
            res, approval = str(e.get("result") or ""), None
            try:
                obj = json.loads(res) if res.startswith("{") else None
            except ValueError:
                obj = None
            if isinstance(obj, dict):                    # JSON 结果分行显示；网关附带的 Joy 的回答（approval）单独拿出来
                approval = obj.pop("approval", None)
                res = json.dumps(obj, ensure_ascii=False, indent=1)
            out.append({"ev": "tool_end", "at": at, "id": e.get("id"), "tool": e.get("tool"), "ms": e.get("ms"), "error": bool(e.get("error")),
                        "result": res[:3000], **({"approval": str(approval)[:1500]} if approval else {})})
    return _platform_calls(gw, out, full)


PLATFORM_ORIGINS = ("resume", "approved")      # 平台替模型做的调用：续跑前运行器替它调用（resume）；挂起后 Joy 批了卡，中枢替它执行（approved）


def _platform_calls(gw: list[dict[str, Any]], out: list[dict[str, Any]], full: bool) -> list[dict[str, Any]]:
    """平台替模型做的调用（网关日志里开始一条 call_start、结束一条 call，按 call_id 对上；旧记录只有结束那条、没有 call_id，开始按用时倒推）：
    主模型的 toollog 里没有，按网关记录补成同样的 tool_start / tool_end（id 用 call_id，子代理的步骤按它挂上），再和其余事件按时间排。"""
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for i, g in enumerate(gw):
        if g.get("origin") in PLATFORM_ORIGINS and g.get("ev") in ("call_start", "call") and isinstance(g.get("ts"), (int, float)):
            pairs.setdefault(str(g.get("call_id") or f"{g['origin']}-{i}"), {})[g["ev"]] = g
    if not pairs:
        return out
    for cid, p in pairs.items():
        b, d = p.get("call_start"), p.get("call")
        sec = float(d["seconds"]) if d is not None and isinstance(d.get("seconds"), (int, float)) else 0.0
        at, tool = b["ts"] if b is not None else d["ts"] - sec, (b or d or {}).get("tool")  # pyright: ignore[reportOptionalSubscript]
        out.append({"ev": "tool_start", "at": at, "id": cid, "tool": tool, "args": _clip((b or {}).get("args") or {}, ARG_CLIP if full else ARG_BRIEF)})
        if d is not None:
            end = {"ev": "tool_end", "at": d["ts"], "id": cid, "tool": tool, "ms": round(sec * 1000), "error": not d.get("ok")}
            if full:
                end["result"] = str(d.get("result") or d.get("message") or "")[:3000]
            out.append(end)
    return sorted(out, key=lambda e: e["at"] if isinstance(e.get("at"), (int, float)) else 0.0)


def _iso_ts(v: Any) -> float | None:
    from datetime import datetime

    try:
        return float(v) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def _run_at(task: str, ts: float) -> str | None:
    """ts 时刻正在跑的运行 id（卡片没记运行时用）：从新到旧找第一次开跑早于 ts、结束晚于 ts 的。"""
    for d in sorted((RUNS / task).iterdir(), reverse=True) if (RUNS / task).exists() else []:
        try:
            info = json.loads((d / "run.json").read_text())
        except (OSError, ValueError):
            continue
        start = float(info.get("first_started") or info.get("started") or 0)
        if start and start <= ts:
            end = _iso_ts(info["ended"]) if info.get("ended") else time.time()
            return d.name if ts <= (end or time.time()) + 5 else None
    return None


def _owner(c: dict[str, Any]) -> str | None:
    """这张卡是哪个任务发的：payload 的 task，或 run（<任务>/<运行 id>）的任务；没记的是 None。"""
    pl = c.get("payload") or {}
    run = str(pl.get("run") or "")
    return str(pl.get("task") or "") or (run.split("/", 1)[0] if "/" in run else "") or None


def _cards_by_run(task: str) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """卡片挂到哪一轮：payload 记了运行的挂那一轮；没记的（任何种类，如一次性监听的取消卡）按发卡时间挂到当时在跑的那一轮；
    那时没有运行在跑的（后台发的）单独返回，网页按时间插进对话，和 Discord 私信里一样。别的任务发的卡也单独返回（带 from：哪个任务），
    同 Discord 私信一样按时间出现、各自处理。"""
    out: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for p in sorted(APPROVALS.glob("*.json")) if APPROVALS.exists() else []:
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        pl = c.get("payload") or {}
        run = str(pl.get("run") or "")
        rid = pl.get("run_id") if pl.get("task") == task else (run.split("/", 1)[1] if run.startswith(task + "/") else None)
        if not rid and (owner := _owner(c)) and owner != task:
            loose.append(card_view(c, task, brief=True))
            continue
        if not rid and not run and not pl.get("task") and c.get("created"):
            rid = _run_at(task, float(c["created"]))
            if not rid:
                loose.append(card_view(c, task, brief=True))
                continue
        if rid:
            out.setdefault(str(rid), []).append(card_view(c, task, brief=True))
    return out, loose


def card_full(card_id: str) -> dict[str, Any] | None:
    """对话里展开的一张已处理的卡：完整的 card_view。没有返回 None。"""
    if not re.fullmatch(r"[0-9a-f]+", card_id) or not (p := APPROVALS / f"{card_id}.json").is_file():
        return None
    try:
        return card_view(json.loads(p.read_text()))
    except (OSError, ValueError):
        return None


def _card_image(path: Any, task: str | None = None) -> str | None:
    """卡片附图（工具发卡时给的截图等）→ 网页附件路径：工作目录 <workspace>/<任务> 的 media/。"""
    if not path:
        return None
    name, p = task or current_task(), Path(str(path))
    try:
        rel = p.relative_to(WORK / name)
        return f"work/{rel}" if rel.parts[:1] == ("media",) else None
    except ValueError:
        return None


def card_view(c: dict[str, Any], task: str | None = None, brief: bool = False) -> dict[str, Any]:
    """卡片给网页的样子：问题卡带题目与当前选择，审批卡带正文、按钮、下拉与弹框字段。task：现在接私信的任务（一次列很多卡时由调用方给，免得每张卡都重读一遍任务定义）。
    brief：已处理的卡只给折叠那一行要的（标题、结果、来自哪个任务），正文等展开时经 card_full 取。"""
    task = task or channels.current_task()
    if brief and c.get("resolved"):
        r = c["resolved"] if isinstance(c["resolved"], dict) else {}
        v = {"id": c.get("id"), "kind": c.get("kind"), "title": c.get("title"), "created": c.get("created"), "brief": True,
             "resolved": {k: r[k] for k in ("action", "result") if k in r}}
        if (owner := _owner(c)) and owner != task:
            v["from"] = owner
        if c.get("kind") == acquire_permission.KIND:
            v["ask"] = {}
        return v
    v = {k: c.get(k) for k in ("id", "kind", "title", "body", "buttons", "select", "modal", "created", "resolved")}
    v["image"] = _card_image(c.get("file"), task)
    if (owner := _owner(c)) and owner != task:     # 别的任务发的卡：网页标「来自 <任务>」
        v["from"] = owner
    if c.get("kind") == acquire_permission.KIND:
        p = c.get("payload") or {}
        v["ask"] = {"questions": p.get("questions"), "answers": p.get("answers"), "current": p.get("current", 0), "other": acquire_permission.OTHER, "letters": acquire_permission.LETTERS}
    return v


def _turn(run_dir: Path, info: dict[str, Any], cards: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    name, rid = run_dir.parent.name, run_dir.name
    web = info.get("channel") == "web"                       # 入口：run.json 的 channel
    events = _events(run_dir, full=False)
    _attach_subs(run_dir, events, full=False)
    if web:
        replies = [{"ts": r.get("ts"), "text": r.get("text") or "", "files": [_work_url(f) for f in r.get("files") or []]}
                   for r in _jsonl(run_dir / "web" / "outbox.jsonl")]
    else:                                                 # Discord 发起的：agent 调的 reply 就是回复；没调的由运行器发最终回答
        replies = [{"ts": e.get("at"), "text": str((e.get("args") or {}).get("text") or ""), "files": [_work_url(f) for f in (e.get("args") or {}).get("files") or []]}
                   for e in events if e["ev"] == "tool_start" and e.get("tool") == REPLY_TOOL]
        if not replies and info.get("status") == "complete" and info.get("final_text"):
            end = info.get("ended")
            replies = [{"ts": _iso_ts(end) if end else None, "text": str(info["final_text"]), "files": []}]
    injected = []
    for p in sorted((run_dir / "injected").glob("*.json")) if (run_dir / "injected").exists() else []:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        injected.append({"ts": e.get("enqueued_at"), "text": e.get("steer") or e.get("prompt") or "", "channel": e.get("channel") or "discord",
                         "images": [_work_url(a) for a in e.get("attachments") or []], "files": list(e.get("files") or [])})
    keep = ("status", "trigger", "started", "first_started", "ended", "elapsed_seconds", "seconds", "tokens", "tokens_all", "calls", "rejections", "error",
            "load_seconds", "model_switch_seconds", "subagent_seconds", "paused_seconds", "session", "preempt_count", "final_text")
    channel = "web" if web else "discord"
    return {"task": name, "run_id": rid, "channel": channel, "label": "命令行" if (info.get("source") or {}).get("origin") == "cli" else "", **{k: info.get(k) for k in keep},
            "user": {"text": info.get("steer") or info.get("prompt") or "", "images": [_work_url(a) for a in info.get("attachments") or []],
                     "files": [_work_url(f) if "/" in str(f) else f"run/{rid}/in/{f}" for f in info.get("files") or []]},   # 音视频记工作目录 in/ 下的路径，其余记运行目录 in/ 下的文件名
            "injected": injected, "events": events, "replies": replies, "cards": cards.get(rid, [])}


def call_detail(run_id: str, call_id: str) -> dict[str, Any] | None:
    """对话里展开的一次工具调用：完整的参数（长的照样截到 ARG_CLIP）、结果、Joy 的回答，子代理调用连同它调过的每个工具的参数与结果。没有返回 None。"""
    if not re.fullmatch(r"[0-9T-]+", run_id):
        return None
    run_dir = RUNS / current_task() / run_id
    if not run_dir.is_dir():
        return None
    events = _events(run_dir)
    _attach_subs(run_dir, events)
    start = next((e for e in events if e["ev"] == "tool_start" and str(e.get("id")) == call_id), None)
    if start is None:
        return None
    return {"start": start, "end": next((e for e in events if e["ev"] == "tool_end" and str(e.get("id")) == call_id), None)}


FOLD = ("complete", "failed", "stopped", "timeout", "partial")   # 跑完的轮：对话列表里折起工具过程


def _fold(t: dict[str, Any]) -> None:
    """跑完、也没有待处理卡片的一轮：工具过程只留一行（调用几次、中间说明几段、从什么时候开始），点开再经 turn_events 取。"""
    if t.get("status") not in FOLD or any(not c.get("resolved") for c in t.get("cards") or []):
        return
    ev = t.pop("events")
    calls = [e for e in ev if e["ev"] == "tool_start" and e.get("tool") != REPLY_TOOL]
    says = [e for e in ev if e["ev"] == "assistant" and e.get("text")]
    t["events"], t["folded"] = [], {"calls": len(calls), "says": len(says), "at": min((e["at"] for e in calls + says if e.get("at") is not None), default=None)}


def _sig(t: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(t, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def turn_events(run_id: str) -> dict[str, Any] | None:
    """折起来的一轮点开时：它的工具过程（同对话列表里没折的一轮：参数只够画摘要，展开某一条再经 call_detail 取完整的）。"""
    if not re.fullmatch(r"[0-9T-]+", run_id) or not (d := RUNS / current_task() / run_id).is_dir():
        return None
    events = _events(d, full=False)
    _attach_subs(d, events, full=False)
    return {"events": events}


def history(limit: int = 20, before: str | None = None, have: str = "", fold: bool = True) -> dict[str, Any]:
    """最近 limit 次私信任务的运行（新的在后），加上还在排队的消息与网页上执行过的命令。before：只要这个运行 id 之前的（往上翻）。
    跑完的轮折起工具过程（_fold；fold=False 不折，stb chat show 用），每轮带 sig；have = 网页已有的「运行 id:sig」（逗号隔开），没变的那些只回 {run_id, same: true}，网页用自己手里的。"""
    name = current_task()
    d = RUNS / name
    dirs = sorted((p for p in d.iterdir() if (p / "run.json").exists()), key=lambda p: p.name) if d.exists() else []
    if before:
        dirs = [p for p in dirs if p.name < before]
    more = len(dirs) > limit
    dirs = dirs[-limit:]
    cards, loose = _cards_by_run(name)
    resumes = resume_queued()
    turns = []
    for p in dirs:
        try:
            info = json.loads((p / "run.json").read_text())
        except (OSError, ValueError):
            continue
        t = _turn(p, info, cards)
        shown_status(name, p.name, t, resumes)
        if fold:
            _fold(t)
        t["sig"] = _sig(t)
        turns.append(t)
    since = float(turns[0].get("first_started") or 0) if turns and more else 0.0
    queued = []
    # 排队中的 *.json，和运行器已经取走、正在加载模型的 *.json.loading（要换模型时第一条先换模型，几分钟）
    for p in sorted([*QUEUE.glob("*.json"), *QUEUE.glob("*.json.loading")]) if QUEUE.exists() else []:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if e.get("task") == name and e.get("channel") and not e.get("resume"):      # 渠道发来、排着的消息
            queued.append({"ts": e.get("enqueued_at"), "priority": e.get("priority"), "loading": p.name.endswith(".loading"), "text": e.get("steer") or e.get("prompt") or "", "channel": e.get("channel") or "discord",
                           "images": [_work_url(a) for a in e.get("attachments") or []], "files": list(e.get("files") or [])})
    system = [s for s in _jsonl(_system_log()) if float(s.get("ts") or 0) >= since][-50:]
    upto = float("inf")
    if before:                                        # 往上翻：只要这一页时间段里的
        try:
            upto = float(json.loads((d / before / "run.json").read_text()).get("first_started") or upto)
        except (OSError, ValueError):
            pass
    loose = sorted((c for c in loose if since <= float(c.get("created") or 0) < upto), key=lambda c: float(c.get("created") or 0))[-30:]   # 最近的 30 张
    msg_tasks = set(subscribers(MESSAGE))                        # 接消息的任务自己的结果在它那一轮里已经显示，不重复
    notices = [{"ts": n.get("ts"), "text": n.get("text") or ""} for n in _jsonl(_notices_file())
               if n.get("task") not in msg_tasks and since <= float(n.get("ts") or 0) < upto][-50:]
    crons = _task_messages(since, upto)              # 定时任务的消息：按时间插进对话，标「📬 任务名」
    known = set(have.split(",")) if have else set()
    turns = [{"run_id": t["run_id"], "same": True} if f"{t['run_id']}:{t['sig']}" in known else t for t in turns]
    has_todo = _cached(f"todo:{name}", 30, lambda: load_task(task_file(name)).tool(todo.TOOL) is not None)   # 任务列了 todo 才有待办面板
    return {"task": name, "turns": turns, "more": more, "queued": queued, "system": system, "cards": loose, "notices": notices, "crons": crons,
            "todos": todo.panel(name) if has_todo else []}


TASK_DB = "tasks.sqlite3"
_OUTPUT_ID = re.compile(r"(.+):([0-9a-f]{12})")      # 任务 record_run.py 记成品的 id：<任务>/<运行 id>:<正文 sha1 前 12 位>


def _task_messages(since: float, upto: float) -> list[dict[str, Any]]:
    """定时任务给 Joy 的成品：任务消息库 data/messages/tasks.sqlite3 里 role output 的（各任务的 record_run.py 记，post_call hook，match: [reply]），
    取 [since, upto) 里最近 50 条，旧的在前，{ts, task, text, files}。只读打开；库不存在或读不了就没有。
    渠道消息触发的运行（run.json 有 channel）的回复在那一轮对话里显示，不重复。附件见 _output_files。"""
    import sqlite3

    p = MESSAGES / TASK_DB
    if not p.is_file():
        return []
    try:
        con = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT id, ts, text, task FROM messages WHERE role = 'output' AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 50",
                               (since, min(upto, 1e18))).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for mid, ts, text, task in reversed(rows):
        got = _output_files(str(mid))
        if got is None:
            continue
        files, gone = got
        out.append({"ts": ts, "task": task or "", "text": str(text or "") + (f"\n\n（附件 {'、'.join(gone)} 已经取不到了）" if gone else ""), "files": files})
    return out


def _output_files(mid: str) -> tuple[list[str], list[str]] | None:
    """一条成品的附件：按 id 找到那次运行，在它的工具日志里找正文摘要对得上的 reply 调用，files（工作目录里的路径）换成网页路径 task/<任务>/<in|media|out>/…；
    返回 (网页路径, 已经不在的文件名)。id 不是 <任务>/<运行 id>:<摘要> 的、运行记录没了、找不到那次调用的没有附件；出自渠道消息触发的运行返回 None。"""
    m = _OUTPUT_ID.fullmatch(mid)
    if not m or not hub.RUN_REF_RE.fullmatch(m[1]):
        return [], []
    run = RUNS / m[1]
    try:
        if json.loads((run / "run.json").read_text()).get("channel"):
            return None
    except (OSError, ValueError, AttributeError):
        pass
    for e in _jsonl(run / "agent" / "toollog.jsonl"):
        if e.get("ev") != "tool_start" or e.get("tool") != REPLY_TOOL:
            continue
        args = e.get("args") or {}
        fs = [str(f) for f in args.get("files") or []]
        text = str(args.get("text") or "").strip() or ("（附件：" + "、".join(Path(f).name for f in fs) + "）" if fs else "")   # 同 record_run.py
        if hashlib.sha1(text.encode()).hexdigest()[:12] == m[2]:
            break
    else:
        return [], []
    task = m[1].split("/", 1)[0]
    urls = {f: u for f in fs if (u := _work_url(f, task)) and (WORK / task / u.split("/", 2)[2]).is_file()}
    return list(urls.values()), [Path(f).name for f in fs if f not in urls]


# ---- 卡片 ----
def card_act(card_id: str, action: str, value: Any = None) -> dict[str, Any]:
    """网页上处理一张卡：决定交中枢（card_act，判定逻辑各入口相同；密码 / 凭证字段的值只放中枢内存、取一次），
    中枢再让其余入口（Discord）的同一张卡跟着改。"""
    r = hub_client.call_sync("card_act", origin="web", id=card_id, action=action, value=value)
    if r is None:
        raise TaskError(f"{HUB_DOWN}，卡片没有处理，稍后再试")
    if not r.get("ok"):
        raise TaskError(str(r.get("error") or "没有处理"))
    return {"card": card_view(r["card"]), "note": r.get("note")}


# ---- 状态栏 ----
def _dir_size(d: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _active_model(models: dict[str, Any], main: str) -> dict[str, Any] | None:
    """机器上实际在跑的档位：主模型在跑返回 None；调子代理时换成了子代理的档位（如生图、做视频的 ComfyUI）就返回它和用它的子代理。"""
    from control_center import subagents

    names = _cached("running", 15, lambda: running(models))
    if main in names or not names:
        return None
    pname = names[0]
    p = profiles(models)[pname]
    try:
        agents = [a.name for a in subagents.load_defs() if a.runtime == p.get("runtime") and a.model == p.get("model_path")]
    except subagents.SubagentError:
        agents = []
    if not agents:                  # 在跑的是别的任务的模型（如切开关时还没换完）：不算子代理，状态栏照常显示本任务的模型（未加载 / 加载中）
        return None
    return {"profile": pname, "runtime": p.get("runtime"), "model": p.get("model_path"), "subagent": agents[0]}


def _last_input(run_dir: Path) -> int | None:
    """这次运行最后一次模型调用读入的 token（= 当时的上下文占用）；还没有模型调用回来时 None。"""
    return next((e.get("input") for e in reversed(_events(run_dir)) if e["ev"] == "assistant" and e.get("input")), None)


def _running(name: str) -> bool:
    try:
        return (json.loads(RUNNER_STATE.read_text()).get("current") or {}).get("task") == name
    except (OSError, ValueError):
        return False


def status() -> dict[str, Any]:
    """输入框下面的信息：模型档位与上下文占用（最近一次运行最后一次模型调用读入的 token；这一轮还没调用过是 None）、
    沙箱配置与工作目录大小、接管状态、命令表（输入 / 时的补全与工具栏的命令列表）。"""
    name = current_task()
    task = load_task(task_file(name))
    models = load_models()
    pname = task.profile_name(models)
    prof = profiles(models)[pname]
    d = RUNS / name
    last = max((p for p in d.iterdir() if (p / "run.json").exists()), key=lambda p: p.name, default=None) if d.exists() else None
    used = _last_input(last) if last else None
    return {
        "task": name,
        "active": _active_model(models, pname),
        "model": {"profile": pname, "runtime": task.runtime, "model": task.model, "vision": bool(prof.get("vision")),
                  "context_window": int(prof["context_window"]), "loaded": _cached(f"up:{pname}", 30, lambda: is_up(prof, timeout=2)),
                  "loading": any(json.loads(p.read_text()).get("task") == name for p in QUEUE.glob("*.json.loading")) if QUEUE.exists() else False},
        "context": {"used": used, "run_id": last.name if last else None},
        "sandbox": {"image": sandbox.IMAGE, "memory": sandbox.MEMORY, "work_bytes": _cached(f"du:{name}", 60, lambda: _dir_size(WORK / name))},
        "claim": holder_since(),
        "session": {"running": _running(name)},
        "commands": [{"name": k, "description": v.get("description") or "", "args": v.get("args") or []} for k, v in sorted(commands().items())],
    }


# ---- 附件 ----
def thumb(p: Path, width: int) -> Path:
    """图片的缩略图（对话里的微缩图用，点开大图才取原图）：按原图 mtime 与宽度缓存成 JPEG，放 data/hub/web/thumbs/；
    不是静态图片（视频、GIF、文档）或已经够小的，原样返回。"""
    import hashlib

    width = max(64, min(width, 2048))
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return p
    st = p.stat()
    out = HUB / "web" / "thumbs" / (hashlib.sha1(f"{p}:{st.st_mtime_ns}:{width}".encode()).hexdigest()[:20] + ".jpg")
    if out.is_file():
        return out
    from PIL import Image

    with Image.open(p) as im:
        if im.width <= width:
            return p
        im.thumbnail((width, width * 4))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        im.convert("RGB").save(tmp, "JPEG", quality=82, optimize=True)
        tmp.rename(out)
    return out


def file_path(rel: str) -> Path:
    """网页可读的附件：work/…（私信任务的工作目录，任何子目录）、inbox/…（附件原件）、run/<运行 id>/in/…、task/<任务>/…（定时任务消息的附件，在那个任务的工作目录）；
    realpath 后必须还在这些目录里。"""
    name = current_task()
    parts = rel.split("/")
    if parts[0] == "work" and len(parts) > 1:
        root, sub = WORK / name, parts[1:]
    elif parts[0] == "inbox" and len(parts) > 1:
        root, sub = INBOX / name, parts[1:]
    elif parts[0] == "run" and len(parts) > 3 and parts[2] == "in" and parts[1].replace("-", "").replace("T", "").isdigit():
        root, sub = RUNS / name / parts[1] / "in", parts[3:]
    elif parts[0] == "task" and len(parts) > 2 and re.fullmatch(r"[\w][\w.-]*", parts[1]):
        root, sub = WORK / parts[1], parts[2:]
    else:
        raise TaskError("不能读这个路径")
    root = root.resolve()
    p = root.joinpath(*sub).resolve()
    if root not in p.parents or not p.is_file():
        raise TaskError("文件不存在")
    return p


# ---- 适配器（data/sockets/channels/web.sock） ----
def _notices_file() -> Path:
    """网页自己的系统通知记录 data/hub/web/notices.jsonl（_keep_notice 写，聊天按时间插进对话）。"""
    return HUB / "web" / "notices.jsonl"


def _keep_notice(text: str, task: Any) -> None:
    """系统通知：记进 data/hub/web/notices.jsonl，网页聊天按时间插进对话。"""
    p = _notices_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "text": text, "task": task}, ensure_ascii=False) + "\n")


def _reply(req: dict[str, Any]) -> dict[str, Any]:
    """网页发起的运行的回复：写运行目录的发件箱（网页轮询展示）。别的入口发起的运行的回复不归网页管。
    没有入口的定时任务消息（role cron）回收到、自己不存：网页靠任务消息库显示它（_task_messages），库由任务的 post_call hook 在 reply 成功后写；
    回收到让这次 reply 算送达（Discord 不在时也一样），任务才会记进库、网页才看得到。"""
    ref = str(req.get("run_ref") or "")
    if not req.get("channel") and req.get("role") == "cron":
        return {"ok": True, "result": "网页聊天会显示（从任务消息库读）"}
    if req.get("channel") != "web":
        return {"ok": True, "skipped": True}
    if not hub.RUN_REF_RE.fullmatch(ref) or not (RUNS / ref).is_dir():
        return {"ok": False, "error": f"找不到运行 {ref}"}
    text, files = str(req.get("text") or ""), [str(f) for f in req.get("files") or []]
    outbox = RUNS / ref / "web" / "outbox.jsonl"
    outbox.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with outbox.open("a") as f:
        f.write(json.dumps({"ts": now, "text": text, "files": files}, ensure_ascii=False) + "\n")
    return {"ok": True, "result": f"已发送到网页聊天（{len(text)} 字符）" + (f"，附件 {len(files)} 个" if files else "")}


def adapter(req: dict[str, Any]) -> dict[str, Any]:
    """中枢发来的一个 op（见 hub.py 的适配器接口）。"""
    op = req.get("op")
    if op == "send":
        _keep_notice(str(req.get("text", "")), req.get("task"))
        return {"ok": True}
    if op == "reply":
        return _reply(req)
    if op == "status":
        return {"ok": True, "ready": True}
    if op in ("presence", "loading", "progress_start", "activity", "progress_end", "card", "card_refresh"):
        return {"ok": True}                            # 网页从文件轮询这些
    return {"ok": False, "error": f"unknown op {op}"}


class _AdapterHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            resp = adapter(json.loads(self.rfile.readline(hub_client.LIMIT) or b"{}"))
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


def serve_adapter() -> socketserver.ThreadingUnixStreamServer:
    """向中枢报到：在 data/sockets/channels/web.sock（600）开本机接口，后台线程处理。返回服务器（测试里用完 shutdown）。"""
    path = hub.adapter_socket("web")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    srv = socketserver.ThreadingUnixStreamServer(str(path), _AdapterHandler)
    srv.daemon_threads = True
    os.chmod(path, 0o600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
