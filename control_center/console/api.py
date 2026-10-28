"""控制台的接口：网页上看和管任务的那部分（规格第 13 节 / P3）。外壳 channels/web/server.py 按路径交给 get / post（不是控制台的路径返回 None）。
看：运行中 / 排队 / 被打断的任务、状态栏文字、已登记任务与下次定时触发、工具、子代理、最近运行（run.json 元数据、任务 yaml 快照、网关审计、工具日志、stderr、运行结束时工作目录 out/ 里的文件名）、平台服务的日志。
看任务定义：网页不能整篇改任务 yaml，改定义用 git；只能改 enabled、priority 与 tools 段。
动：立即跑、启用/停用（改 yaml 的 enabled，stbcc-hub 现读）、暂停/恢复、改优先级、给任务加 / 撤工具（撤即时生效，加从下次开跑生效）。
前端在同目录的 frontend/（console.js、console.css），由外壳的页面引用，经 get 的 /console/<文件> 取。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from control_center import registry, schedule, subagents, systemd, workflow
from control_center.models import load_models
from control_center.paths import CONTROL, HUB, QUEUE, REPO, RUNNER_STATE, RUNS, TASKS, task_file, task_files, task_name_of
from control_center.registry import GATEWAY_KEYS, PLATFORM_TOOLS_DIR, TOOLS, reload as reload_tools
from control_center.task import TaskError, load_task
from control_center.task_edit import save_task

SERVICES_TTL_S = 60                   # 服务列表（list_services）多久重新找一次
_services: tuple[float, dict[str, dict[str, str]]] = (0.0, {})
FRONTEND = Path(__file__).parent / "frontend"
STATIC = {"console.js": "text/javascript", "console.css": "text/css"}    # 前端文件 → Content-Type（get 的 /console/<文件>）
JSON = "application/json"
TAIL = 30_000


def _read(p: Path, limit: int = TAIL) -> str:
    try:
        t = p.read_text(errors="replace")
        return t if len(t) <= limit else "…[前面已省略]\n" + t[-limit:]
    except OSError:
        return ""


def _jsonl(p: Path, limit: int = 400) -> list[Any]:
    out = []
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for l in lines[-limit:]:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out


def _in_repo(arg: str) -> bool:
    try:
        return arg.startswith("/") and REPO.resolve() in Path(arg).resolve().parents
    except (OSError, ValueError):
        return False


def list_services() -> dict[str, dict[str, str]]:
    """平台的服务 = 用户 systemd 服务里 ExecStart 运行的是本仓库里的程序的（程序本身在仓库里，如 .venv/bin/stb；或解释器跑的第一个文件在仓库里，
    如 node services/owa-launcher/server.ts）；由 timer / path 触发的（有同名的 .timer / .path 单元，或 TriggeredBy 不空：如清理、手册同步；
    timer 停用了也不算）、模板单元、仓库外的程序不算。
    不写名单：新装的服务自动列上。返回 {单元名（不带 .service）: {state: ActiveState, description}}，SERVICES_TTL_S 内用缓存。"""
    global _services
    if time.time() - _services[0] < SERVICES_TTL_S:
        return _services[1]
    files = {line.split()[0] for line in systemd.systemctl("list-unit-files", "--no-legend", check=False).stdout.splitlines() if line.split()}
    names = sorted(u for u in files if u.endswith(".service") and "@" not in u
                   and not {u.removesuffix(".service") + ".timer", u.removesuffix(".service") + ".path"} & files)
    out: dict[str, dict[str, str]] = {}
    if names:
        r = systemd.systemctl("show", *names, "-p", "Id", "-p", "ExecStart", "-p", "TriggeredBy", "-p", "ActiveState", "-p", "Description", check=False)
        for block in r.stdout.split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            m = re.search(r"argv\[\]=(.*?) ;", props.get("ExecStart", ""))
            argv = m.group(1).split() if m else []
            prog = argv[:1] + [a for a in argv[1:] if not a.startswith("-")][:1]     # 程序本身，和解释器跑的第一个文件
            if props.get("Id") in names and not props.get("TriggeredBy") and any(_in_repo(a) for a in prog):
                out[props["Id"].removesuffix(".service")] = {"state": props.get("ActiveState", ""), "description": props.get("Description", "")}
    _services = (time.time(), dict(sorted(out.items())))
    return _services[1]


def _brief(e: dict[str, Any]) -> dict[str, Any]:
    """运行条每 3 秒拉一次 state：排队条目的 prompt / steer 只给开头（网页只显示第一行），队列文件本身不动。"""
    return {**e, **{k: str(e[k])[:200] for k in ("prompt", "steer") if isinstance(e.get(k), str) and len(e[k]) > 200}}


def state() -> dict[str, Any]:
    st = json.loads(RUNNER_STATE.read_text()) if RUNNER_STATE.exists() else {}
    entries = []
    for p in sorted(QUEUE.glob("*.json")):
        try:
            entries.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    entries.sort(key=lambda e: (-int(e.get("priority", 5)), float(e.get("enqueued_at", 0))))
    found = list_services()
    services = {u: systemd.systemctl("is-active", f"{u}.service", check=False).stdout.strip() for u in found}
    service_desc = {u: v["description"] for u, v in found.items()}
    cur = st.get("current")
    if cur:
        run = RUNS / cur["task"] / cur["run_id"]
        cur = {**cur, "elapsed": round(time.time() - float(cur.get("started", time.time())), 1),
               "calls": len([e for e in _jsonl(run / "gateway.jsonl") if e.get("ev") == "call"]),
               "last_tool": next((e.get("tool") for e in reversed(_jsonl(run / "gateway.jsonl")) if e.get("ev") == "call"), None)}
    loading = None                                   # 开跑前加载模型：条目改名成 .json.loading，runner.json 的 current 还是空的
    for p in sorted(QUEUE.glob("*.json.loading")):
        try:
            e = json.loads(p.read_text())
            loading = {"task": e.get("task"), "priority": e.get("priority"), "since": p.stat().st_mtime}
            lt = load_task(task_file(str(e.get("task"))))
            loading["model"] = lt.model if not lt.workflow else None
        except (OSError, json.JSONDecodeError, TaskError, ValueError):
            pass
        break
    return {"now": time.time(), "utc_offset": -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone), "runner_pid": st.get("pid"), "runner_updated": st.get("updated"), "current": cur,
            "loading": loading if not cur else None,
            "paused": [_brief(e) for e in entries if e.get("resume")], "queued": [_brief(e) for e in entries if not e.get("resume")], "services": services,
            "service_desc": service_desc, "side": workflow.side_units()}


def _last_run(name: str) -> dict[str, Any] | None:
    d = RUNS / name
    if not d.exists():
        return None
    runs = sorted(p for p in d.iterdir() if (p / "run.json").exists())
    if not runs:
        return None
    info = json.loads((runs[-1] / "run.json").read_text())
    return {k: info.get(k) for k in ("run_id", "status", "seconds", "error", "ended", "calls", "rejections", "preempt_count")}


def tasks() -> list[dict[str, Any]]:
    from control_center import runner, sandbox

    out = []
    models = load_models()
    for p in task_files():
        name = task_name_of(p)
        row: dict[str, Any] = {"name": name, "yaml": p.read_text(), "last_run": _last_run(name), "in_repo": (REPO / "tasks" / p.relative_to(TASKS)).exists(),
                               "removed_tools": removed_tools(name), "removed_subagents": removed_tools(name, "subagents")}
        try:
            t = load_task(p, models)
            row.update(valid=True, enabled=t.enabled, priority=t.priority, trigger=t.trigger_label, schedule=t.schedule, timeout=t.timeout,
                       # 平台统一的规则（任务 yaml 不写）：停滞判定（workflow 任务不判）、等检查点的上限、运行期间又入队排队还是丢掉、沙箱
                       stall_seconds=runner.STALL_S if not t.workflow else None, preempt_grace_seconds=runner.preempt_grace(t),
                       overlap="queue" if t.queues_overlap else "skip", preemptible=t.preemptible, runtime=t.runtime, model=t.model,
                       tools=[{"name": u.name, "guide": u.guide} for u in t.tools],
                       sandbox={"image": sandbox.IMAGE, "memory": sandbox.MEMORY}, description=t.description, system_prompt=t.system_prompt,
                       spec=t.model_dump(mode="json", exclude={"tools", "system_prompt"}))   # 其余字段（listen、listen_events、steps…）原样给界面翻译
            row["next_run"] = schedule.next_run(t, time.time())     # 下次定时触发（stbcc-hub 按 yaml 算；停用、没登记的是 null）
        except TaskError as e:
            row.update(valid=False, error=str(e))
        out.append(row)
    return out


def resume_queued() -> set[tuple[str, str]]:
    """队列里排着的续跑 {(任务, 运行 id)}：卡片处理完、被抢占的，等前面的跑完再接着跑。"""
    out = set()
    for p in [*QUEUE.glob("*.json"), *QUEUE.glob("*.json.loading")] if QUEUE.exists() else []:
        try:
            e = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if e.get("resume"):
            out.add((str(e.get("task")), str(e.get("resume"))))
    return out


def shown_status(task: str, rid: str, row: dict[str, Any], resumes: set[tuple[str, str]]) -> None:
    """停着（等卡片 / 暂停）但续跑已经排上队的运行：网页显示「排队续跑」，不再说「等 Joy 处理」。"""
    if row.get("status") in ("waiting", "paused") and (task, rid) in resumes:
        row.update(status="resuming", error="卡片已处理，排队等着接着跑" if row["status"] == "waiting" else "排队等着接着跑")


def runs(limit: int = 60, task: str | None = None) -> list[dict[str, Any]]:
    rows = []
    resumes = resume_queued()
    for d in RUNS.iterdir() if RUNS.exists() else []:
        if task and d.name != task:
            continue
        for r in d.iterdir():
            f = r / "run.json"
            if f.exists():
                try:
                    info = json.loads(f.read_text())
                except json.JSONDecodeError:
                    continue
                rows.append({k: info.get(k) for k in ("task", "run_id", "status", "trigger", "priority", "started", "ended", "seconds", "elapsed_seconds", "calls", "rejections",
                                                      "agent_messages", "preempt_count", "tokens", "tokens_all", "error", "memory_peak", "session", "attachments",
                                                      "first_started", "load_seconds", "subagent_seconds", "paused_seconds", "segments")}
                            | {"mtime": f.stat().st_mtime, "trigger": "schedule" if info.get("trigger") == "timer" else info.get("trigger")})
                shown_status(d.name, r.name, rows[-1], resumes)
    rows.sort(key=lambda x: x["mtime"], reverse=True)
    return rows[:limit]


def _log(p: Path, limit: int = 6000) -> dict[str, Any]:
    """一步的输出：末尾 limit 字、文件字节数、有没有截掉前面。"""
    text = _read(p, limit)
    return {"text": text, "len": p.stat().st_size if p.is_file() else 0, "cut": text.startswith("…[前面已省略]")}


def run_detail(task: str, run_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-z0-9-]+", task) or not re.fullmatch(r"[0-9T-]+", run_id):
        return None
    d = RUNS / task / run_id
    if not (d / "run.json").exists():
        return None
    info = json.loads((d / "run.json").read_text())
    snap = _read(d / "task.yaml")
    try:
        steps = (yaml.safe_load(snap) or {}).get("steps")
    except yaml.YAMLError:
        steps = None
    if isinstance(steps, list):                              # workflow 运行：任务快照里的每一步 + 它的输出（step-<编号>.log 的末尾）、发给 Joy 的 steps/reply.txt
        rows = [x for x in steps if isinstance(x, list) and len(x) >= 3]
        logs = {str(x[0]): _log(d / f"step-{x[0]}.log") for x in rows}
        wf = {"steps": [{"n": x[0], "file": x[1], "needs": bool(x[2]), "model": (x[3] or {}).get("model") if len(x) > 3 and isinstance(x[3], dict) else None,
                         "log": logs[str(x[0])]} for x in rows], "reply": _read(d / "steps" / "reply.txt", 4000)}
    elif (d / "command.log").is_file():                     # 改成 workflow 之前的命令任务：一条命令当作一步
        wf = {"steps": [{"n": 1, "file": "command", "needs": True, "model": None,
                         "log": _log(d / "command.log")}], "reply": ""}
    else:
        wf = None
    return {"run": info, "task_yaml": snap, "workflow": wf, "gateway": _jsonl(d / "gateway.jsonl"),
            "toollog": _jsonl(d / "agent" / "toollog.jsonl"), "agent_stderr": _read(d / "agent.stderr.log", 8000),
            "gateway_stderr": _read(d / "gateway.stderr.log", 8000), "out_files": info.get("out_files") or [], "dir": str(d)}


def tool_info(name: str) -> dict[str, Any]:
    """一个工具在网页上的样子：说明、usable_by、函数清单。函数清单和网关交给子代理模型的同源：先看网关的磁盘缓存（data/runtime/tool_defs/，
    工具文件没改过才认，带每个函数的说明），没有就静态读服务器脚本里注册的函数名（registry.functions，不起进程）。functions_from 标明出处。"""
    from control_center.gateway import cached_defs
    from control_center.registry import functions

    spec = TOOLS.get(name)
    if spec is None:
        return {"name": name, "missing": True, "functions": []}
    defs = cached_defs(name)
    fns = [{"name": x.get("name"), "description": x.get("description") or ""} for x in defs] if defs is not None else [{"name": n} for n in functions(spec)]
    return {"name": name, "description": spec.description, "usable_by": spec.usable_by, "functions": fns, "functions_from": "cache" if defs is not None else "source"}


def tools_list() -> list[dict[str, Any]]:
    """工具页：每个工具的说明、usable_by（subagent = 只给子代理用），以及 tools 里列了它的子代理（subagents）。"""
    reload_tools()
    users: dict[str, list[str]] = {}
    try:
        for d in subagents.load_defs():
            for n in d.tools:
                users.setdefault(n, []).append(d.name)
    except subagents.SubagentError:
        pass                                             # 子代理配置有误：子代理页报错，工具页照常
    can = registry.searchable()
    return [{"name": t.name, "description": t.description, "usable_by": t.usable_by, "platform": _platform(t), "loader": t.name == registry.LOADER,
             "loadable": t.name in can, "subagents": users.get(t.name, [])} for t in TOOLS.values()]


def _platform(spec: Any) -> bool:
    """平台自带的工具（control_center/tools/<名>/，registry.PLATFORM_TOOLS_DIR）。"""
    return spec.folder.parent.resolve() == (REPO / PLATFORM_TOOLS_DIR).resolve()


def _source_descriptions(path: Path) -> dict[str, str]:
    """服务器脚本里 @server.tool 注册的函数 → 说明（description= 写的字符串，没写用函数的文档字符串）：只读语法树。"""
    import ast

    def text(v: ast.expr) -> str | None:                 # 字面字符串；f-string 与 + 拼接里算不出的部分写成 …
        if isinstance(v, ast.Constant):
            return v.value if isinstance(v.value, str) else None
        if isinstance(v, ast.JoinedStr):
            return "".join(text(x) or "" if isinstance(x, ast.Constant) else "…" for x in v.values)
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            a, b = text(v.left), text(v.right)
            return None if a is None and b is None else (a or "…") + (b or "…")
        return None

    out: dict[str, str] = {}
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool":
                    kw = {k.arg: t for k in d.keywords if (t := text(k.value)) is not None}
                    out[kw.get("name", node.name)] = kw.get("description") or ast.get_docstring(node) or ""
    return out


def tool_detail(name: str) -> dict[str, Any] | None:
    """工具详情页（#/tools/<名>）：tool_info 之外加 tool.yaml 的登记（联网、容器、能不能被 builder 改、hooks 事件）、requirements.txt 的依赖、
    工具文件夹里代码读的凭证键（只给键名，不给值）、guide.md（截到 4000 字）、文件夹路径、列了它的子代理。没有这个工具返回 None。"""
    from control_center.registry import credential_calls

    reload_tools()
    spec = TOOLS.get(name)
    if spec is None:
        return None
    keys: set[str] = set()
    for f in spec.folder.rglob("*.py"):
        if "__pycache__" not in f.parts:
            keys.update(credential_calls(f.read_text(errors="replace")))
    try:
        users = [d.name for d in subagents.load_defs() if name in d.tools]
    except subagents.SubagentError:
        users = []
    folder = spec.folder.relative_to(REPO) if spec.folder.is_relative_to(REPO) else spec.folder
    info = tool_info(name)
    if info["functions_from"] == "source":                   # 没有网关缓存：说明也静态读服务器脚本（不起进程）
        docs = _source_descriptions(spec.server)
        info["functions"] = [{"name": x["name"], "description": docs.get(x["name"], "")} for x in info["functions"]]
    catalog = [{"name": n, "kind": it["kind"], "description": it["description"]} for n, it in registry.searchable().items()] if name == registry.LOADER else None
    return {**info, "platform": _platform(spec), "builtin": bool(spec.builtin), "gateway": bool(spec.gateway), "catalog": catalog, "is_editable": spec.is_editable, "network": spec.network, "docker": spec.docker, "deps": list(spec.deps),
            "credentials": sorted(keys), "hooks": sorted(spec.hooks), "guide": spec.guide[:4000], "guide_len": len(spec.guide),
            "folder": str(folder), "subagents": users}


def skills_list() -> list[dict[str, Any]]:
    """Skills 页：skills/ 下装好的 skill（control_center/skills.py）的名字与说明。"""
    from control_center import skills

    return [{"name": n, "description": m["description"]} for n, m in skills.all_skills().items()]


SKILL_DEPS: dict[str, dict[str, Any]] = {}       # 网页导入后在后台装依赖：skill 名 → {state: installing / done / failed, failed, error}（只在本进程里记）


def skill_import(body: dict[str, Any]) -> dict[str, Any]:
    """Skills 页的手动导入：body.files = [{path（相对路径）, data（base64）}] → skills.import_upload；要装的 Python 包在后台装进沙箱共用的环境。"""
    import base64
    import threading

    from control_center import sandbox_env, skills

    try:
        files = [(str(f.get("path") or ""), base64.b64decode(str(f.get("data") or ""), validate=True)) for f in body.get("files") or []]
        r = skills.import_upload(files)
    except (ValueError, RuntimeError, OSError) as e:
        raise TaskError(str(e)) from None
    if r["requirements"]:
        SKILL_DEPS[r["name"]] = {"state": "installing"}

        def deps(name: str = r["name"]) -> None:
            try:
                SKILL_DEPS[name] = {"state": "done", "failed": sandbox_env.install(r["requirements"])[1]}
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                SKILL_DEPS[name] = {"state": "failed", "error": str(e)}

        threading.Thread(target=deps, daemon=True).start()
    return r


def skill_detail(name: str) -> dict[str, Any] | None:
    """一个 skill 的详情页（#/skills/<名>）：说明、文件夹、文件清单、要装的 Python 包、SKILL.md 正文（去掉开头的 YAML，截到 8000 字）。没有返回 None。"""
    from control_center import skills

    m = skills.all_skills().get(name)
    if m is None:
        return None
    folder = m["folder"]
    body = re.sub(r"\A---\s*\n.*?\n---\s*(\n|$)", "", (folder / "SKILL.md").read_text(errors="replace"), count=1, flags=re.S).strip()
    try:
        fs, reqs = [str(f) for f in skills.files(folder)], skills.requirements(folder)
    except (ValueError, OSError) as e:
        fs, reqs = [f"读不了文件清单：{e}"], []
    return {"name": name, "description": m["description"], "folder": str(folder.relative_to(REPO)), "files": fs, "requirements": reqs,
            "body": body[:8000], "body_len": len(body), "deps": SKILL_DEPS.get(name)}


def subagents_list() -> list[dict[str, Any]]:
    """子代理的配置（只读）：subagents/<名>/ 里每个子代理的 yaml 与约定文件——种类、模型、单次上限、隔离、能不能被 builder 改、
    工具（tool_info：说明与函数清单）、参数（inputs 的名字与原样的 JSON Schema：类型、说明、默认值、上下限、可选值）、程序、系统提示、guide.md、有没有 output.py、hooks 的事件。
    网页按这些字段统一渲染，不按名字写死。某一份不合格时整页给出错误，不猜。"""
    from control_center import subagents

    reload_tools()
    try:
        can = registry.searchable()
        return [{"name": d.name, "description": d.description, "loadable": d.name in can, "kind": d.kind, "runtime": d.runtime, "model": d.model, "timeout": d.timeout, "prompt": d.system_prompt,
                 "isolated": d.isolated, "prevalidated": d.prevalidated, "is_editable": d.is_editable, "tools": [tool_info(n) for n in d.tools],
                 "inputs": [{"name": k, **v} for k, v in d.inputs.items()],
                 "server": d.server.name if d.server else None, "guide": d.guide, "output": d.output is not None, "hooks": sorted(d.hooks)}
                for d in subagents.load_defs()]
    except subagents.SubagentError as e:
        return [{"error": str(e)}]


def journal(unit: str = "stbcc-runner", n: int = 200) -> str:
    if unit not in list_services():                  # 只看平台的服务（list_services），别的单元的日志不给
        return ""
    return subprocess.run(["journalctl", "--user", "-u", f"{unit}.service", "-n", str(n), "--no-pager", "-o", "short-iso"], capture_output=True, text=True).stdout


def _tool_items(lines: list[str], key: str = "tools") -> tuple[int, int, list[tuple[int, int, str]], str] | None:
    """任务 yaml 顶层 key 段（tools 或 subagents）：(段首行, 段尾行, [(项首行, 项尾行, 名字)], 项的缩进)；没有这一段返回 None。"""
    import yaml

    start = next((i for i, l in enumerate(lines) if re.match(key + r":[ \t]*(\[\][ \t]*)?(#.*)?$", l)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t-"):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():      # 段尾的空行留在段外
        end -= 1
    heads = [i for i in range(start + 1, end) if re.match(r"[ \t]*- ", lines[i])]
    indent = re.match(r"[ \t]*", lines[heads[0]]).group(0) if heads else "  "  # pyright: ignore[reportOptionalMemberAccess]
    heads = [i for i in heads if lines[i].startswith(indent + "- ")]
    items = []
    for k, h in enumerate(heads):
        stop = heads[k + 1] if k + 1 < len(heads) else end
        item = (yaml.safe_load("\n".join(l[len(indent):] if l.startswith(indent) else l for l in lines[h:stop])) or [None])[0]
        items.append((h, stop, item if isinstance(item, str) else next(iter(item or {"": 0}))))
    return start, end, items, indent


def _removed_file(key: str = "tools") -> Path:
    return HUB / "web" / f"removed-{key}.json"          # 工具、子代理各记一份：同名也不串


def removed_tools(name: str, key: str = "tools") -> dict[str, Any]:
    """网页撤掉的工具（key=subagents：子代理）当时的项：{名字: 选项}；网页按它加回（edit_tools 对工具只取其中的网关项，子代理原样）。"""
    try:
        return dict(json.loads(_removed_file(key).read_text()).get(name) or {})
    except (OSError, ValueError):
        return {}


def _remember_removed(name: str, tool: str, conf: Any, key: str = "tools") -> None:
    f = _removed_file(key)
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    per = data.setdefault(name, {})
    if conf is None:
        per.pop(tool, None)
    else:
        per[tool] = conf
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def edit_tools(name: str, action: str, body: dict[str, Any], key: str = "tools") -> dict[str, Any]:
    """网页改任务的 tools 段（key=subagents：subagents 段；其余内容与注释不动）：add 加一个（只写名字；从下次开跑生效），remove 撤掉一个（正在跑的运行下一次调用起就拒）。
    网页不编辑项的选项；body 的 yaml 只用来把撤掉时记下的选项加回：工具只取网关项（guide，GATEWAY_KEYS），子代理原样。
    写入前走 save_task 的完整校验，不合格原文不变。"""
    import yaml

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name) or not task_file(name).exists():
        raise TaskError("任务未登记")
    tool = str(body.get("name") or "")
    lines = task_file(name).read_text().splitlines()
    blk = _tool_items(lines, key)
    what = "工具" if key == "tools" else "子代理"
    have = [it[2] for it in blk[2]] if blk else []
    old = None
    if action == "remove":
        if tool not in have:
            raise TaskError(f"任务里没有{what} {tool}")
        assert blk is not None                     # have 不空，块一定在
        h, stop, _ = next(it for it in blk[2] if it[2] == tool)
        old = (yaml.safe_load("\n".join(l[len(blk[3]):] if l.startswith(blk[3]) else l for l in lines[h:stop])) or [None])[0]
        del lines[h:stop]
        if len(have) == 1:
            lines[blk[0]] = f"{key}: []"
    elif action == "add":
        reload_tools()
        if tool not in (TOOLS if key == "tools" else subagents.folder_names()):
            raise TaskError(f"没有{what} {tool}")
        if tool in have:
            raise TaskError(f"任务里已经有 {tool}")
        try:
            conf = yaml.safe_load(str(body.get("yaml") or "")) or {}
        except yaml.YAMLError as e:
            raise TaskError(f"yaml 解析失败：{e}") from e
        if not isinstance(conf, dict):
            if m := re.search(r"^[ \t]*([\w-]+):(?=\S)", str(body.get("yaml") or ""), flags=re.M):
                raise TaskError(f"yaml 的冒号后面要有空格：写成「{m.group(1)}: …」")
            raise TaskError("yaml 要写成「键: 值」的映射")
        if key == "tools":
            conf = {k: v for k, v in conf.items() if k in GATEWAY_KEYS}   # 撤掉时记下的旧工具项里可能有已删除的作用域键：只加回网关项
        indent = blk[3] if blk else "  "
        item = yaml.safe_dump([{tool: conf} if conf else tool], allow_unicode=True, sort_keys=False, default_flow_style=False)
        new = [indent + l for l in item.splitlines()]
        if blk is None:
            lines += [f"{key}:", *new]
        else:
            if re.match(key + r":[ \t]*\[\]", lines[blk[0]]):
                lines[blk[0]] = f"{key}:"
            lines[blk[1]:blk[1]] = new
    else:
        raise TaskError(f"未知动作 {action}")
    out = save_task(name, "\n".join(lines) + "\n", None, what=f"{ {'add': '加上', 'remove': '撤掉'}.get(action, '改') }{what} {tool}")
    _remember_removed(name, tool, (old.get(tool) or {} if isinstance(old, dict) else {}) if action == "remove" else None, key)
    return out


def act(name: str, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name) or not task_file(name).exists():
        raise TaskError("任务未登记")
    if action == "run":
        from control_center.runner import enqueue, load_registered

        if name not in load_registered():
            raise TaskError(f"任务 {name} 没登记：先 stb task add")
        return {"ok": True, "queued": enqueue(name, trigger="manual").name}
    if action in ("enable", "disable"):                 # 改 yaml 的 enabled（stbcc-hub 现读，定时与监听跟着变）
        return save_task(name, None, {"enabled": action == "enable"})
    if action == "schedule":                            # 改 yaml 的 schedule（一条 cron 或几条的列表；save_task 走完整校验）：stbcc-hub 现读，下一分钟起按新的
        v = (body or {}).get("schedule")
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        try:
            schedule.check(v)
        except ValueError as e:
            raise TaskError(f"定时不合法：{e}") from None
        if "schedule" not in load_task(task_file(name)).triggers:
            raise TaskError(f"{name} 不是定时任务")
        return save_task(name, None, {"schedule": v})
    if action == "priority":                            # 改 yaml 的 priority（save_task 走完整校验：1–10）
        try:
            p = int(str((body or {}).get("priority")))
        except ValueError:
            raise TaskError("优先级要写 1–10 的整数") from None
        return save_task(name, None, {"priority": p})
    if action == "pause":
        CONTROL.mkdir(parents=True, exist_ok=True)
        (CONTROL / f"pause-{name}").write_text(str(time.time()))
        return {"ok": True}
    if action == "resume":
        from control_center.runner import enqueue, norm_trigger

        d = RUNS / name
        paused = [p for p in sorted(d.iterdir()) if (p / "run.json").exists() and json.loads((p / "run.json").read_text()).get("status") == "paused"] if d.exists() else []
        if not paused:
            raise TaskError("没有 paused 的运行")
        info = json.loads((paused[-1] / "run.json").read_text())
        return {"ok": True, "queued": enqueue(name, trigger=norm_trigger(info.get("trigger")), resume=paused[-1].name, priority=info.get("priority")).name}
    raise TaskError(f"未知动作 {action}")


def get(path: str, q: dict[str, list[str]]) -> tuple[int, Any, str] | None:
    """控制台的 GET：返回 (状态码, 内容, Content-Type)；不是控制台的路径返回 None。出错抛异常（TaskError 外壳回 404，别的回 500）。"""
    if path.startswith("/console/") and path[len("/console/"):] in STATIC:
        name = path[len("/console/"):]
        return 200, (FRONTEND / name).read_text(), STATIC[name]
    if path == "/api/state":
        return 200, state(), JSON
    if path == "/api/tasks":
        reload_tools()
        return 200, tasks(), JSON
    if path == "/api/runs":
        return 200, runs(int(q.get("limit", ["60"])[0]), q.get("task", [None])[0]), JSON
    m = re.fullmatch(r"/api/runs/([^/]+)/([^/]+)", path)
    if m:
        d = run_detail(m.group(1), m.group(2))
        return (200, d, JSON) if d else (404, {"error": "no such run"}, JSON)
    if path == "/api/journal":
        return 200, {"text": journal(q.get("unit", ["stbcc-runner"])[0], int(q.get("n", ["200"])[0]))}, JSON
    if path == "/api/tools":
        return 200, tools_list(), JSON
    m = re.fullmatch(r"/api/tools/([^/]+)", path)
    if m:
        d = tool_detail(m.group(1))
        return (200, d, JSON) if d else (404, {"error": "没有这个工具"}, JSON)
    if path == "/api/subagents":
        return 200, subagents_list(), JSON
    if path == "/api/skills":
        return 200, skills_list(), JSON
    m = re.fullmatch(r"/api/skills/([^/]+)", path)
    if m:
        d = skill_detail(m.group(1))
        return (200, d, JSON) if d else (404, {"error": "没有这个 skill"}, JSON)
    if path == "/api/apis":                             # 「API」页：各 API 的状态与用量、模型菜单、现有任务的优先级
        from control_center import api_door

        return 200, api_door.overview(), JSON
    return None


def post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """控制台的 POST（立即跑、启停、暂停 / 恢复、改优先级、加 / 撤工具与子代理）：返回结果；不是控制台的路径返回 None。出错抛异常（TaskError 外壳回 400）。"""
    m = re.fullmatch(r"/api/tasks/([^/]+)/(run|enable|disable|pause|resume|priority|schedule)", path)
    if m:
        return act(m.group(1), m.group(2), body)
    m = re.fullmatch(r"/api/tasks/([^/]+)/(tools|subagents)/(add|remove)", path)
    if m:
        return edit_tools(m.group(1), m.group(3), body, m.group(2))
    if path == "/api/skills/import":
        return skill_import(body)
    m = re.fullmatch(r"/api/apis/(create|([^/]+)/(rekey|stop|delete))", path)
    if m:                                               # 「API」页：建（回一次密钥原文）、换密钥、结束会话、删
        from control_center import api_door

        try:
            if m.group(1) == "create":
                return {"ok": True, **api_door.create(body.get("name", ""), body.get("tier", ""), body.get("priority"), bool(body.get("public")))}
            return {"ok": True, **getattr(api_door, m.group(3))(m.group(2))}
        except api_door.ApiError as e:
            raise TaskError(str(e)) from None
    return None
