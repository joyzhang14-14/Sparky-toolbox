"""一次性动态监听（平台通用，不认工具）：以 sparky 跑的工具在运行里登记一个监听程序，Joy 在通用审批卡上同意后由中枢当作登记它的任务的常驻监听来起、监督；
程序到条件就触发一次（作为事件交给登记它的任务），做完自己注销。登记什么程序、程序做什么（等时间、查条件、下载……）都是工具自己的事，写在工具的文件夹里。

- 登记（中枢的 monitor op，do=create，hub_op；以 sparky 跑的工具经 hub.sock 调，run_ref 已核对是在跑的运行）：
  {name, tool, program, ctx, action, detail?, image?, prompt?, priority?, key?}
  · tool：发起登记的工具，这次运行的任务要能用它（任务 yaml 的 tools 或它的子代理列了它，docker_service._tools_of）；
    program：监听程序（写了 watch(ctx, emit) 的 .py），仓库相对路径，必须在这个工具的文件夹里；联网按这个工具 tool.yaml 的 network。
  · ctx：交给程序的数据（JSON 对象，程序的 ctx["watch"]，另加登记的 id、name），平台不看内容。
  · 卡：通用审批卡（acquire_permission.PERMISSION_KIND，payload.run 是这次运行：网关照常就地等、等不到就让出）；卡面文字 action / detail、附图 image 由工具给。
    key：工具认「同一件事」的指纹（不给按 tool + name 算）：同一个任务里同一 key 还在等 / 在跑的登记直接返回它，不重复登记、不重复发卡。
  · prompt：触发时事件里的 prompt（emit 里给了 prompt 就用 emit 的）；priority：那次运行的优先级（1–10，不写用任务的）。
- 状态（data/hub/monitors/<id>.json）：pending（等卡）→ watching（Joy 同意）→ fired（程序 emit 了 fire，程序接着跑）→ done（程序 emit 了 done）；
  rejected（拒绝 / 卡作废）、cancelled（取消）。中枢的监听循环每秒调 tick()：看卡的结果改状态，刚生效的发一张「取消」卡。
- 监督：watchers() 把 watching / fired 的登记交给监听循环（listen.Listener 的 extra，常驻监听 watch 那一套：以 sparky 跑、宿主 listen_host.py、崩了按等待重起、
  不再需要就停）。实例名 monitor.<id>，状态目录 data/listen/monitor.<id>/state（程序自己记做到哪一步，重起后接着做）；程序起的容器属于这个监听（stbcc-docker）。
- emit 的协议（on_emit，可以同时写几项）：{"fire": true, "prompt"?}：第一次 fire 作为事件交给登记它的任务（中枢的 publish，只交给它；来源名 = 登记它的工具名，
  event = {name, prompt}）：任务的 listen_events 订阅了这个来源就由它的 on_event 决定入不入队、prompt 是什么，没订阅就直接入队（trigger listen，用这个 prompt）；
  priority 按登记，状态栏写登记名。之后的 fire 不再交；
  {"notify": 文字}：私信 Joy；{"done": true}：注销（状态 done，监听循环随即停掉它、不再起）。
- 取消：monitor op 的 do=cancel {name}（本任务的），或「取消」卡（CANCEL_KIND）；状态 cancelled，监听循环随即停掉程序（它的容器跟着停）。
- 登记结束（done、经 op 取消）时它的「取消」卡作废（close_cancel_card），各入口的卡跟着刷新、按钮变灰。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

from control_center import cards
from control_center.paths import HUB, REPO

MAX_CTX = 20_000                      # ctx 的 JSON 最多多少字
ACTIVE = ("pending", "watching", "fired")
LIVE = ("watching", "fired")          # 程序该在跑的状态
CANCEL_KIND = "monitor-cancel"        # 生效后发的「取消」卡


def _dir() -> Path:
    return HUB / "monitors"                      # 调用时才算：测试换了数据目录也跟着走


def _write(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)


def path(mid: str) -> Path:
    return _dir() / f"{mid}.json"


def load(mid: str) -> dict[str, Any] | None:
    if not mid or not mid.isalnum():
        return None
    try:
        return json.loads(path(mid).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save(m: dict[str, Any]) -> None:
    _write(path(m["id"]), m)


def all_monitors() -> list[dict[str, Any]]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def active(task: str) -> list[dict[str, Any]]:
    """这个任务还在等审批、在跑的登记。"""
    return [m for m in all_monitors() if m.get("task") == task and m.get("status") in ACTIVE]


def watching_names() -> list[str]:
    """在跑的登记的名字（状态栏「监听中」）。"""
    return [m["name"] for m in all_monitors() if m.get("status") in LIVE]


def watch_key(mid: str) -> str:
    """登记的常驻监听实例名（data/listen/<它>/、stbcc-docker 请求的 listen）。"""
    return f"monitor.{mid}"


async def close_cancel_card(m: dict[str, Any], why: str) -> None:
    """登记结束了：还没处理的「取消」卡作废（同 acquire_permission.obsolete_pending 的写法），让各入口刷新这张卡。"""
    from control_center import hub_client

    card = cards.load(str(m.get("cancel_card") or ""))
    if card and not card.get("resolved"):
        card["resolved"] = {"action": "obsolete", "value": None, "result": why, "at": time.time()}
        cards.save(card)
        await hub_client.call("card_refresh", id=card["id"])


def cancel(m: dict[str, Any], by: str) -> str:
    if m.get("status") not in ACTIVE:
        return f"一次性监听 {m['name']} 已经结束（{m['status']}），不用取消"
    m.update(status="cancelled", cancelled_at=time.time(), cancelled_by=by)
    save(m)
    return f"已取消一次性监听 {m['name']}"


# ---------- 中枢的 monitor op（stbcc-hub 进程里：hub.run_op 转来，run_ref 已核对是在跑的运行） ----------
def _program(tool: str, program: Any) -> Path:
    """program（仓库相对路径）→ 宿主上的文件：要在 tool 的文件夹里、是 .py。不对抛 ValueError。"""
    from control_center.registry import TOOLS

    spec = TOOLS.get(tool)
    if spec is None:
        raise ValueError(f"没有工具 {tool!r}")
    folder = spec.folder.resolve()
    f = (REPO / str(program or "")).resolve()
    if f.suffix != ".py" or folder not in f.parents or not f.is_file():
        shown = folder.relative_to(REPO.resolve()) if folder.is_relative_to(REPO.resolve()) else folder
        raise ValueError(f"program 要写工具 {tool} 文件夹（{shown}/）里的监听程序 .py（仓库相对路径），现在是 {program!r}")
    return f


async def hub_op(run_ref: str, req: dict[str, Any]) -> dict[str, Any]:
    """以 sparky 跑的工具请中枢代办（只动这次运行所属任务的登记）。do：create {…}（登记并发通用审批卡，见模块说明）、cancel {name}。"""
    from control_center import hub_client
    from control_center.docker_service import _tools_of
    from control_center.paths import task_file

    task, do = run_ref.split("/", 1)[0], req.get("do")

    def no(why: str) -> dict[str, Any]:
        return {"ok": False, "error": why}

    if do == "cancel":
        m = next((x for x in active(task) if x["name"] == str(req.get("name", "")).strip()), None)
        if m is None:
            return no(f"没有叫 {req.get('name')} 的一次性监听在等或在跑")
        text = cancel(m, "tool")
        await close_cancel_card(m, f"一次性监听 {m['name']} 已取消，这张卡作废")
        return {"ok": True, "text": text}
    if do != "create":
        return no(f"不认识的 do {do!r}（create / cancel）")
    a = args if isinstance(args := req.get("args"), dict) else {}
    name, tool = " ".join(str(a.get("name") or "").split())[:40], str(a.get("tool") or "")
    if not name or not str(a.get("action") or "").strip():
        return no("登记要有 name（短名字，状态栏显示）和 action（卡上一句话：要监听什么、触发后做什么）")
    if tool not in _tools_of(task_file(task)):
        return no(f"任务 {task} 没有用工具 {tool!r}（任务 yaml 的 tools 或它的子代理里没有），不能以它的名义登记")
    try:
        program = _program(tool, a.get("program"))
    except ValueError as e:
        return no(str(e))
    ctx = a.get("ctx") if isinstance(a.get("ctx"), dict) else {}
    if len(json.dumps(ctx, ensure_ascii=False)) > MAX_CTX:
        return no(f"ctx 太大（JSON 超过 {MAX_CTX} 字）：大的东西放文件里，ctx 只写路径")
    pri = a.get("priority")
    if pri is not None and (isinstance(pri, bool) or not isinstance(pri, int) or not 1 <= pri <= 10):
        return no("priority 要写 1–10 的整数（不写用任务的优先级）")
    key = str(a.get("key") or hashlib.sha256(f"{tool}\0{name}".encode()).hexdigest())[:128]
    if same := next((m for m in active(task) if m.get("key") == key), None):
        return {"ok": True, "monitor": same, "existing": True}
    if any(m["name"] == name for m in active(task)):
        return no(f"已经有叫 {name} 的一次性监听在等或在跑：换个名字，或先取消它")
    rel = str(program.relative_to(REPO.resolve())) if program.is_relative_to(REPO.resolve()) else str(program)
    m = {"id": secrets.token_hex(6), "task": task, "run_ref": run_ref, "name": name, "tool": tool, "program": rel,
         "ctx": ctx, "key": key, "prompt": str(a.get("prompt") or ""), "priority": pri, "status": "pending", "created": time.time()}
    save(m)
    from control_center import acquire_permission

    r = await hub_client.call("card", kind=acquire_permission.PERMISSION_KIND, title="", body="", buttons=[],
                              payload={"run": run_ref, "tool": tool, "action": str(a["action"]), "key": f"monitor:{m['id']}", "auto": "1",   # 同意后平台自己起监听
                                       **{k: str(a[k]) for k in ("detail", "image", "title") if a.get(k)}})
    if not (r or {}).get("id"):
        m.update(status="rejected", feedback="审批卡没发出去")
        save(m)
        return no(f"审批卡没发出去（{(r or {}).get('error') or 'stbcc-hub 没响应'}），没有登记；可以稍后重试")
    m["card"] = r["id"]  # pyright: ignore[reportOptionalSubscript]
    save(m)
    return {"ok": True, "monitor": m}


# ---------- 卡（stbcc-hub 进程里） ----------
def _cancel_card(card: dict[str, Any], action: str, _value: Any) -> str:
    m = load(str((card.get("payload") or {}).get("monitor", "")))
    if m is None:
        return "这个登记不存在了"
    return cancel(m, "button") if action == "cancel" else "没有取消"


cards.register(CANCEL_KIND, _cancel_card)


async def tick() -> None:
    """中枢的监听循环每秒调一次：等卡的看 Joy 处理了没有（同意 → watching，别的 → rejected）；刚生效的发一张「取消」卡。"""
    from control_center import hub_client

    for m in all_monitors():
        st = m.get("status")
        if st == "pending":
            got = (cards.load(str(m.get("card") or "")) or {}).get("resolved")
            if not got:
                continue
            if got.get("action") == "approve":
                m.update(status="watching", approved_at=time.time())
            else:
                fb = (got.get("value") or {}).get("feedback") if isinstance(got.get("value"), dict) else None
                m.update(status="rejected", feedback=str(fb or got.get("result") or "")[:1000])
            save(m)
            st = m["status"]
        if st in LIVE and not m.get("cancel_card"):
            r = await hub_client.call("card", kind=CANCEL_KIND, title=f"一次性监听已生效：{m['name']}", body=f"任务 {m['task']} 的 {m['tool']}；不需要了点「取消」",
                                      buttons=[{"id": "cancel", "label": "取消", "style": "danger"}], payload={"monitor": m["id"]})
            m["cancel_card"] = (r or {}).get("id") or "failed"
            save(m)


# ---------- 触发 ----------
async def fire(m: dict[str, Any], prompt: str) -> None:
    """触发：作为来源 <登记它的工具名> 的事件只交给登记它的任务（中枢的 publish，task 写它；规则见模块说明）。"""
    from control_center import hub_client
    from control_center.paths import RUNNER_STATE

    m.update(status="fired", fired_at=time.time())
    save(m)
    r = await hub_client.call("publish", timeout=hub_client.PUBLISH_TIMEOUT, source=m["tool"], task=m["task"],
                             event={"name": m["name"], "prompt": prompt or m.get("prompt") or ""}, priority=m.get("priority"), watch=m["name"])
    if not (r and r.get("ok")):
        print(f"一次性监听 {m['name']}：交给 {m['task']} 失败：{(r or {}).get('error') or 'stbcc-hub 没响应'}", flush=True)
    try:
        cur = json.loads(RUNNER_STATE.read_text()).get("current")
    except (OSError, json.JSONDecodeError):
        cur = None
    if cur and not cur.get("preemptible", True):
        await hub_client.call("send", text=f"⚠ 一次性监听 {m['name']} 触发了，但被 {cur.get('task')} 挡住（它不能被抢占），要不要手动停掉它？")


def watchers() -> list[Any]:
    """在跑的登记（watching / fired）→ 常驻监听（listen.Watch）：属于登记它的任务，程序是登记的 program，以 sparky 跑、联网按登记它的工具的 tool.yaml。
    登记的内容批准后不变，指纹用登记 id。"""
    from control_center import listen
    from control_center.paths import task_file
    from control_center.registry import TOOLS
    from control_center.task import TaskError, load_task

    out = []
    for m in all_monitors():
        if m.get("status") not in LIVE or not m.get("program"):
            continue
        try:
            task = load_task(task_file(m["task"]))
        except TaskError as e:
            print(f"一次性监听 {m['name']}：任务 {m['task']} 的定义读不了，先不起：{str(e)[:200]}", flush=True)
            continue
        f, key, spec = REPO / m["program"], watch_key(m["id"]), TOOLS.get(m["tool"])
        out.append(listen.Watch(
            key=key, task=task, tasks=[task.name], file=f, sig=m["id"], label=f"一次性监听 {m['name']}（{m['program']}）",
            command=lambda key=key, task=task.name, f=f, net=getattr(spec, "network", False): listen.host_command(key, task, f.parent, net),
            ctx={"watch": {**m.get("ctx", {}), "id": m["id"], "name": m["name"]}},
            on_emit=lambda data, mid=m["id"]: on_emit(mid, data)))
    return out


async def on_emit(mid: str, data: dict[str, Any]) -> None:
    """程序的一次 emit：notify → 私信 Joy；第一次 fire → 入队；done → 注销。登记已经不在跑（取消了、结束了）的不处理。"""
    from control_center import hub_client

    m = load(mid)
    if m is None or m.get("status") not in LIVE:
        return
    if isinstance(data.get("notify"), str) and data["notify"].strip():
        await hub_client.call("send", text=data["notify"][:1900])
    if data.get("fire") is True and m["status"] == "watching":
        await fire(m, str(data.get("prompt") or ""))
    if data.get("done") is True:
        m = load(mid) or m
        m.update(status="done", finished_at=time.time())
        save(m)
        await close_cancel_card(m, f"一次性监听 {m['name']} 已结束，这张卡作废")
