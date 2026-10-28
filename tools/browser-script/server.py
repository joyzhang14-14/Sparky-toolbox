"""browser-script：浏览器脚本和它的紧急任务（给要掐时间的事：抢课、抢名额）。脚本格式、演练记录、结果文件见同目录的 _script.py。

容器、登录档案、域名拦截和 browser 工具同一套（同文件夹的 _browser.py：owa-research 镜像的 worker browse，驱动 agent.mjs 的 script 操作；以 sparky + Landlock 跑，容器以本工具的名义请 stbcc-docker 代起）。
- browser_script_run mode=dry（演练）：跑到 final() 前停下（final 里的不执行），记下写入型请求；跑通就记演练记录（登记要它）。
- browser_script_run mode=check：调 check()，返回真假（条件触发用）。
- browser_script_schedule：登记成平台的一次性动态监听（经 stbcc-hub 的 monitor op，control_center/monitors.py），程序是同目录的 browser_script_watch.py；
  Joy 在通用审批卡上同意才生效。定时的执行时刻前 10 分钟、条件的为真时程序触发：平台入队一次 priority 10 的私信运行（主模型并行加载），
  程序同时直接实跑已批准的脚本（final() 最多试 40 次 / 30 秒，成功一次就停）。
- browser_script_result：主模型等程序实跑的结果（监听状态目录里的 result.json）；_list / _cancel：本任务登记的紧急任务。
输出只有执行器自己的记录（结果、出错行号和报错第一行、跳转过的网址、写入请求、截图路径），不含页面正文；
脚本 log() 的内容和完整时间线写运行目录（主 agent 读不到），要看页面内容用子代理 isolated-browser。
登录档案同 browser 工具：profile 写哪个由模型定，用档案时只能去档案的域名清单（data/tool_state/browser/domains/<名>.txt）里的网站，不用档案可去任意公网。
"""
import asyncio
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _browser as bs  # noqa: E402 — 容器、档案、域名同 browser 工具
import _script as sc  # noqa: E402
from control_center import hub_client, listen, monitors  # noqa: E402

server = MCPServer("browser-script", instructions="执行工作目录里的 Playwright 脚本（演练 / 条件检查），登记到点或条件满足时执行的紧急任务")
TOOL = "browser-script"
calls = 0
RESULT_WAIT_S = 900                   # browser_script_result 默认最多等多久


class ScriptError(ToolError):
    """有意报给主模型的错（原因 + 改法）：ToolError 原文交给模型，不当程序错误。"""


def _task() -> str:
    return os.environ.get("STB_RUN_REF", "/").split("/", 1)[0]


def script_file(path: str) -> Path:
    try:
        f = bs.work_file(path)                       # 只能是工作目录里的文件
    except ValueError as e:
        raise ScriptError(str(e)) from None
    if f.suffix != ".mjs":
        raise ScriptError(f"{path} 不是 .mjs 脚本；浏览器脚本要写成工作目录 scripts/<名>.mjs（ES 模块）")
    return f


@server.tool(name="browser_script_run", description=(
    "执行工作目录 scripts/ 里的 Playwright 脚本（mode：dry 演练 / check 查条件）"))
async def browser_script_run(path: str, mode: str = "dry", profile: str = "") -> str:
    global calls
    if mode not in ("dry", "check"):
        raise ScriptError("mode 只能是 dry / check（实跑只在登记的紧急任务触发时由平台执行）")
    f = script_file(path)
    source = f.read_text()
    sha = sc.digest(source)
    domains = bs.nav_domains(profile.strip())
    url = sc.start_url(source)
    if not url:
        raise ScriptError('脚本要写 export const start_url = "https://…"（打开的第一个网址）')
    if not bs.host_allowed(url, domains):
        raise ScriptError(f"start_url 的域名不在允许范围（{urlsplit(url).hostname or url[:100]} 不在 {domains} 里）；改脚本的 start_url，或换用允许这个域名的登录档案")
    state = bs.load_profile(profile.strip())
    calls += 1
    rel = Path("media") / os.environ.get("STB_RUN_ID", "run") / f"script-{calls}"
    shots_dir = Path(os.environ["STB_WORK_ROOT"]) / rel if os.environ.get("STB_WORK_ROOT") else None
    async with bs.Container(TOOL) as c:
        r = await c.send({"op": "start", "start_url": url, "nav_domains": domains, "storage_state": state})
        if not r.get("ok"):
            raise bs.platform_fault(f"浏览器没起来：{r.get('error', '')}")
        run = asyncio.ensure_future(c.send({"op": "script", "source": source, "mode": mode, "final_tries": sc.FINAL_TRIES, "final_seconds": sc.FINAL_SECONDS},
                                           timeout=600))
        pause = os.environ.get("STB_PAUSE_FILE")
        while not run.done():                        # /stop、暂停：马上杀掉容器，脚本不再往下跑（不等这次调用结束）
            if pause and Path(pause).exists():
                await c.kill()
                run.cancel()
                break
            await asyncio.wait([run], timeout=0.5)
        try:
            r = run.result()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — 被杀的容器读不到回复
            if not (pause and Path(pause).exists()):
                raise
            r = {"ok": True, "result": "stopped", "events": [], "writes": [], "shots": []}
        if profile.strip() and r.get("result") != "stopped":
            st = await c.send({"op": "state"})
            if st.get("ok") and st.get("storage_state"):
                bs.save_profile(profile.strip(), st["storage_state"])
    if not r.get("ok"):
        raise ScriptError(f"执行失败：{str(r.get('error', ''))[:200]}")
    shots = []
    if shots_dir is not None:
        shots_dir.mkdir(parents=True, exist_ok=True)
        for i, s in enumerate(r.get("shots") or [], 1):
            p = shots_dir / f"{i:02d}.jpg"
            p.write_bytes(base64.b64decode(s["data"]))
            shots.append({"why": s["why"], "path": str(p), "host": str(p)})
    if os.environ.get("STB_RUN_DIR"):                # 完整时间线（含脚本 log 的内容）：运行目录，主 agent 读不到
        log = Path(os.environ["STB_RUN_DIR"]) / TOOL / f"script-{calls}.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"path": path, "mode": mode, "sha": sha, **{k: v for k, v in r.items() if k != "shots"}}, ensure_ascii=False, indent=1))
    out = {**sc.summarize(r, bs._safe), "screenshots": [{"why": s["why"], "path": s["path"]} for s in shots]}
    if mode == "dry":
        ok = out["result"] in ("stopped_at_final", "done")
        if ok:
            sc.save_dry(_task(), sha, {"path": path, "script": str(f), "sha": sha, "at": time.time(), "run_ref": os.environ.get("STB_RUN_REF", ""),
                                       "result": out["result"], "writes": out["writes"], "profile": profile.strip(), "domains": domains,
                                       "final_shot": next((s["host"] for s in shots if s["why"] == "final 之前"), shots[-1]["host"] if shots else None)})
        out["note"] = "演练跑通了，可以用 browser_script_schedule 登记" if ok else "演练没跑通，改脚本再试（行号是脚本里的行）"
    return json.dumps(out, ensure_ascii=False)


def urgent_prompt(name: str, kind: str, at: float | None) -> str:
    when = f"，脚本会等到 {time.strftime('%H:%M:%S', time.localtime(float(at or 0)))} 整点才提交" if kind == "at" else ""
    return (f"【紧急任务】{name}（{'定时' if kind == 'at' else '条件已满足'}）：已批准的脚本正在由平台直接执行{when}。"
            f"现在调用 browser_script_result(name=\"{name}\") 等它的结果。结果 done 就把结果和截图私信 Joy；final_failed、error 或 missed 就用子代理 isolated-browser 接手做完"
            "（最后提交仍由它先发审批卡），并私信 Joy 说明发生了什么。")


@server.tool(name="browser_script_schedule", description=(
    "把演练跑通的脚本登记成紧急任务：到点或条件满足时以最高优先级直接实跑；要 Joy 批准才生效"))
async def browser_script_schedule(name: str, script: str, at: str = "", watch: bool = False, interval: int = sc.WATCH_DEFAULT_S) -> str:
    name = str(name).strip()[:40]
    if not name:
        raise ScriptError("name 不能为空")
    if bool(at.strip()) == bool(watch):
        raise ScriptError("at（定时）和 watch（条件）二选一")
    f = script_file(script)
    source = f.read_text()
    sha = sc.digest(source)
    dry = sc.load_dry(_task(), sha)
    if dry is None or dry.get("script") != str(f):
        raise ScriptError("这份脚本（当前内容）还没演练跑通：先 browser_script_run mode=dry")
    when_at, every = None, None
    if at.strip():
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if at.strip().count(":") == 2 else "%Y-%m-%d %H:%M"
            when_at = time.mktime(time.strptime(at.strip(), fmt))
        except ValueError:
            raise ScriptError("at 写成 \"YYYY-MM-DD HH:MM\" 或 \"YYYY-MM-DD HH:MM:SS\"（本机时间）") from None
        if when_at <= time.time() + 60:
            raise ScriptError("at 至少要在一分钟之后")
    else:
        if not re.search(r"export\s+async\s+function\s+check\b", source):
            raise ScriptError("条件触发的脚本要 export async function check({ page })，返回 true 就触发")
        every = max(sc.WATCH_MIN_S, int(interval or sc.WATCH_DEFAULT_S))
    kind = "at" if when_at else "watch"
    how = f"{sc.when(kind, when_at, every)} 执行" if kind == "at" else f"条件满足就执行（{sc.when(kind, when_at, every)}）"
    writes = dry.get("writes") or []
    hosts: dict[str, int] = {}
    for w in writes:
        hosts[w.get("host", "?")] = hosts.get(w.get("host", "?"), 0) + 1
    wtxt = ("演练发出写入请求 " + "、".join(f"{h} ×{n}" for h, n in hosts.items())) if writes else "演练没有发出写入请求"
    code = source if len(source) <= 1100 else source[:1100] + "\n…（太长，只显示前面）"
    r, why = await hub_client.call_detail("monitor", run_ref=os.environ.get("STB_RUN_REF", ""), do="create", args={
        "name": name, "tool": TOOL, "program": sc.WATCHER, "key": f"{TOOL}:{name}:{sha}", "priority": 10,
        "action": f"登记紧急任务 {name}：{how}，最后提交成功最多 1 次（最多试 {sc.FINAL_TRIES} 次 / {sc.FINAL_SECONDS} 秒）；{wtxt}——看截图和脚本，没问题就同意",
        "detail": f"脚本 {script.strip()}：\n```js\n{code.replace('```', 'ˋˋˋ')}\n```", **({"image": dry["final_shot"]} if dry.get("final_shot") else {}),
        "prompt": urgent_prompt(name, kind, when_at),
        "ctx": {"script": str(f), "rel": script.strip(), "sha": sha, "kind": kind, "at": when_at, "interval": every,
                "profile": str(dry.get("profile") or ""), "domains": list(dry.get("domains") or ["*"])}})
    if not (r or {}).get("ok"):
        raise ScriptError(str((r or {}).get("error") or f"stbcc-hub 没响应（{why}），没有登记；可以稍后重试"))
    m = r["monitor"]
    if r.get("existing"):
        return f"紧急任务 {name} 已经登记过（{m['status']}），不用再登记"
    return f"已给 Joy 发登记卡：{name}（{sc.when(kind, when_at, every)}）。他同意才生效"


def _mine() -> list[dict[str, Any]]:
    return [m for m in monitors.all_monitors() if m.get("task") == _task() and m.get("tool") == TOOL]


@server.tool(name="browser_script_list", description="列出本任务登记过、还在等、在监控、刚触发或刚做完的紧急任务（名字、方式、时间、状态）")
async def browser_script_list() -> str:
    ms = [m for m in _mine() if m.get("status") in (*monitors.ACTIVE, "done")][-20:]
    return "\n".join(f"{m['name']}：{'定时' if m['ctx'].get('kind') == 'at' else '条件'}，{sc.when(m['ctx'].get('kind'), m['ctx'].get('at'), m['ctx'].get('interval'))}，{m['status']}"
                     for m in ms) or "没有登记的紧急任务"


@server.tool(name="browser_script_cancel", description="取消一个登记了、还没执行完的紧急任务（Joy 说\"取消 xx\"时用），name 写登记时的名字")
async def browser_script_cancel(name: str) -> str:
    if not any(x["name"] == str(name).strip() for x in monitors.active(_task()) if x.get("tool") == TOOL):
        raise ScriptError(f"没有叫 {name} 的紧急任务在等或在跑")
    r, why = await hub_client.call_detail("monitor", run_ref=os.environ.get("STB_RUN_REF", ""), do="cancel", name=str(name).strip())
    if not (r or {}).get("ok"):
        raise ScriptError(str((r or {}).get("error") or f"stbcc-hub 没响应（{why}）"))
    return str(r["text"])


@server.tool(name="browser_script_result", description=(
    "紧急任务触发后，等平台实跑脚本的结果（name 写登记时的名字；wait 最多等多少秒，默认 900）：结果、出错行号、跳转过的网址、写入请求、截图路径"))
async def browser_script_result(name: str, wait: int = RESULT_WAIT_S) -> str:
    m = next((x for x in reversed(_mine()) if x["name"] == str(name).strip()), None)
    if m is None:
        raise ScriptError(f"本任务没有叫 {name} 的紧急任务")
    f = listen.state_dir(monitors.watch_key(m["id"])) / sc.RESULT
    give_up, pause = time.time() + max(0, int(wait)), os.environ.get("STB_PAUSE_FILE")
    while not f.exists():
        m = monitors.load(m["id"]) or m
        if m.get("status") not in ("watching", "fired") or time.time() >= give_up or (pause and Path(pause).exists()):
            return json.dumps({"result": None, "status": m.get("status"), "note": "还没有实跑结果" + ("（紧急任务已不在跑）" if m.get("status") not in ("watching", "fired") else "")},
                              ensure_ascii=False)
        await asyncio.sleep(2)
    out = json.loads(f.read_text())
    shots = []
    if os.environ.get("STB_WORK_ROOT"):                  # 截图拷进这次运行的 media 目录：主模型能把它发给 Joy
        rel = Path("media") / os.environ.get("STB_RUN_ID", "run") / f"urgent-{m['id']}"
        d = Path(os.environ["STB_WORK_ROOT"]) / rel
        d.mkdir(parents=True, exist_ok=True)
        for s in out.pop("shots", []):
            src = f.parent / str(s.get("file") or "")
            if src.is_file() and src.parent == f.parent / sc.SHOTS:
                shutil.copy(src, d / src.name)
                shots.append({"why": s.get("why"), "path": str(d / src.name)})
    return json.dumps({**out, "screenshots": shots}, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
