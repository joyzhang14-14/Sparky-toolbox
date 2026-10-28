"""网页渠道的 stb 子命令（control_center/cli.py 启动时扫描 channels/*/cli.py 注册）：
stb web 起网页服务；stb chat 命令行发私信、看回复、处理卡片——和网页聊天同一条路（channel: web，附件照网页存），网页上能看到。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from control_center.cli import _die, _install_service
from control_center.reply import TOOL as REPLY_TOOL
from control_center.task import TaskError


def _chat_turn_text(t: dict) -> str:
    """一轮对话的终端视图：用户消息、工具调用（含子代理过程条数）、回复与附件（宿主路径）、卡片。"""
    from channels.web import chat as webchat

    def host(rel: str) -> str:
        try:
            return str(webchat.file_path(rel))
        except TaskError:
            return rel
    tok = t.get("tokens_all") or t.get("tokens") or {}
    out = [f"[{t['run_id']}] {t.get('status')}，{round(float(t.get('seconds') or 0) or (time.time() - float(t.get('first_started') or time.time()) if t.get('status') == 'running' else float(t.get('elapsed_seconds') or 0)))} 秒"
           + (f"，token 入 {tok.get('input', 0):,} / 出 {tok.get('output', 0):,}" if tok else "") + (f"，{t['error']}" if t.get("error") else ""),
           f"> {t['user']['text']}"]
    out += [f"  附件 {host(f)}" for f in t["user"].get("images", []) + t["user"].get("files", [])]
    out += [f"> （插话）{x.get('text', '')}" for x in t.get("injected") or []]
    ends = {e.get("id"): e for e in t["events"] if e["ev"] == "tool_end"}
    for e in t["events"]:
        if e["ev"] == "tool_start" and e.get("tool") != REPLY_TOOL:       # reply 的正文另列在下面（回复）
            end = ends.get(e.get("id"))
            args = json.dumps(e.get("args"), ensure_ascii=False)
            state = "进行中" if end is None else ("出错" if end.get("error") else f"{(end.get('ms') or 0) / 1000:.0f} 秒")
            sub = e.get("sub") or {}
            out.append(f"  · {e['tool']}({args[:160]}{'…' if len(args) > 160 else ''}) {state}" + (f"，子代理 {len(sub['rows'])} 条" if sub.get("rows") else ""))
            if end and end.get("error"):
                out.append(f"      {str(end.get('result', ''))[:300]}")
    for r in t["replies"]:
        out.append(f"< {r['text']}")
        out += [f"  附件 {host(f)}" for f in r.get("files") or []]
    for c in t.get("cards") or []:
        done = c.get("resolved")
        out.append(f"  卡片 {c['id']}（{c['kind']}）{c.get('title', '')}：" + (f"已处理 {done.get('action')}" if done else "待处理"))
        if not done:
            out += [f"      {x}" for x in _card_hint(c)]
    return "\n".join(out)


def _card_hint(c: dict) -> list[str]:
    """待处理的卡能做什么：stb chat card <id> <动作> [值] [--field 键=值]。"""
    from control_center import cards

    if cards.secret_fields(c):
        return ["要输入密码 / 凭证的卡：只能 Joy 在网页或 Discord 上亲自输入"]
    a = c.get("ask")
    if a:
        i, qs = a.get("current", 0), a.get("questions") or []
        if i >= len(qs):
            return ["回答总览：submit 提交，prev 回到最后一题"]
        q, ans = qs[i], (a.get("answers") or [{}])[i]
        if q["type"] == "fill":                      # 填空题（备注是最后一页的填空）
            return [("备注（最后一页，可留空）" if q.get("notes") else f"第 {i + 1}/{len(qs)} 题（填空）：{q['question']}"),
                    *([f"提示：{q['hint']}"] if q.get("hint") else []),
                    f"other <文字>：填这一题（现在：{ans.get('text') or '空'}；other 不带文字 = 清空）",
                    "prev：上一题；submit：交卡" if q.get("notes") else "prev / next：翻页（最后一题的 next 是回答总览）"]
        return [f"第 {i + 1}/{len(qs)} 题（{'多选' if q['type'] == 'multi' else '单选'}）：{q['question']}",
                *[f"L{j}：{o}" + ("（已选）" if j in (ans.get("picked") or []) else "") for j, o in enumerate(q["options"])],
                f"other <文字>：{a.get('other', '其他')}" + (f"（已填：{ans['other']}）" if ans.get("other") else ""), "prev / next：翻页（最后一题的 next 是回答总览）"]
    hint = [f"{b['id']}：{b.get('label', '')}" + ("（弹框：" + "、".join(f"--field {f['id']}=…" for f in (c.get("modal") or {}).get("fields") or []) + "）" if b.get("modal") else "")
            for b in c.get("buttons") or []]
    if (c.get("select") or {}).get("options"):
        hint.append("select <值>：" + "、".join(str(o.get("value") if isinstance(o, dict) else o) for o in c["select"]["options"]))
    if c.get("body"):
        hint.insert(0, str(c["body"])[:300])
    return hint


def cmd_chat(a: argparse.Namespace) -> None:
    """命令行发私信：和网页聊天同一条路（channel: web），网页上能看到全部过程；--wait 等这一轮跑完（或停下等卡片）再打印。"""
    import base64
    import mimetypes

    from channels.web import chat as webchat

    if a.op == "card":
        from control_center import cards

        card = cards.load(a.id)
        if card is None:
            _die(f"没有卡片 {a.id}")
        if cards.secret_fields(card):
            _die("要输入密码 / 凭证的卡只能 Joy 在网页或 Discord 上亲自输入")
        value = dict(f.split("=", 1) for f in a.field) if a.field else (" ".join(a.value) if a.value else None)
        try:
            r = webchat.card_act(a.id, a.action, value)
        except TaskError as e:
            _die(str(e))
        c = r["card"]
        print((r.get("note") or "") + f"卡片 {c['id']}：" + (f"已处理 {c['resolved'].get('action')}" if c.get("resolved") else "待处理"))
        if not c.get("resolved"):
            print("\n".join(f"  {x}" for x in _card_hint(c)))
        return
    if a.op == "show":
        turns = webchat.history(max(a.n, 1), fold=False)["turns"][-a.n:]
        print(json.dumps(turns, ensure_ascii=False, indent=1) if a.json else "\n\n".join(_chat_turn_text(t) for t in turns))
        return
    text = " ".join(a.text)
    files = []
    for f in a.file or []:
        p = Path(f).expanduser()
        if not p.is_file():
            _die(f"附件不存在：{f}")
        files.append({"name": p.name, "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream", "data": base64.b64encode(p.read_bytes()).decode()})
    t0 = time.time()
    try:
        r = webchat.send(text, files, origin="cli")
    except TaskError as e:
        _die(str(e))
    if r.get("system"):                                   # 命令（/stop、/clear、开关）当场执行
        print(r["system"].get("reply", ""))
        return
    print(f"已发送（网页入口，排队 {r['queued']}）" + (f"；跳过附件 {'、'.join(r['skipped'])}" if r.get("skipped") else ""))
    if not a.wait:
        return
    from control_center.task import parse_duration

    deadline, rid = t0 + parse_duration(a.timeout), None
    while time.time() < deadline:
        time.sleep(3)
        h = webchat.history(5)
        for t in h["turns"]:
            started = float(t.get("first_started") or 0)
            mine = (started >= t0 - 1 and t["user"]["text"].startswith(text)) or any(
                float(x.get("ts") or 0) >= t0 - 1 and str(x.get("text", "")).startswith(text) for x in t.get("injected") or [])   # 跑着时发的会插进那一轮
            if mine:
                rid = t["run_id"]
        t = next((x for x in h["turns"] if x["run_id"] == rid), None)
        if t and t.get("status") not in ("running", "queued"):
            print(_chat_turn_text(t))
            return
    _die(f"等了 {a.timeout} 还没跑完" + (f"（运行 {rid}）；`stb chat show` 看进度" if rid else "，还在排队"), 1)


def cmd_web(a: argparse.Namespace) -> None:
    if a.install:
        return _install_service("stbcc-web")
    from channels.web.server import main as web_main

    web_main()


def register(sub: argparse._SubParsersAction) -> None:
    c = sub.add_parser("chat", help="命令行发私信（和网页聊天同一条路，网页上能看到）").add_subparsers(dest="op", required=True)
    p = c.add_parser("send", help="发一条消息给现在接私信的任务；/ 开头的是命令"); p.add_argument("text", nargs="+"); p.add_argument("--file", action="append", help="附件（可重复）")
    p.add_argument("--wait", action="store_true", help="等这一轮跑完（或停下等卡片）再打印"); p.add_argument("--timeout", default="30m"); p.set_defaults(fn=cmd_chat)
    p = c.add_parser("card", help="处理一张卡（问题卡作答、审批卡按钮）；密码卡只能 Joy 亲自填"); p.add_argument("id"); p.add_argument("action"); p.add_argument("value", nargs="*")
    p.add_argument("--field", action="append", help="弹框字段 键=值（可重复）"); p.set_defaults(fn=cmd_chat)
    p = c.add_parser("show", help="最近几轮对话"); p.add_argument("-n", type=int, default=1); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("web", help="网页界面（只在 Tailscale 网卡监听，端口 18090）；--install 装成用户服务"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_web)
