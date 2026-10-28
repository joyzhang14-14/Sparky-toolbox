"""stb 命令行（规格第 13 节）。任务是文件，改文件就是改任务。"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import NoReturn

from control_center import env, schedule, systemd
from control_center.models import load_models
from control_center.paths import QUEUE, REPO, RUNNER_STATE, RUNS, WORK, ensure_dirs, task_file, task_files, task_name_of
from control_center.registry import TOOLS
from control_center.task import TaskError, load_task


def _die(msg: str, code: int = 2) -> NoReturn:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(code)


def _validate(file: str):
    """task add / task check 共用的校验：真实加载器 + models.yaml。不合格就报错退出；返回 (src, task)。"""
    src = Path(file)
    if src.is_dir():                                    # 文件夹形式的任务：tasks/<名>/
        src = src / "task.yaml"
    try:
        env.check_no_values(src.read_text(), str(src))   # .env 的值不能写死在任务里
        task = load_task(src, load_models())
    except (TaskError, ValueError, env.EnvError) as e:                  # ValueError：models.yaml 缺失或写错
        _die(str(e))
    except OSError as e:
        _die(f"读不到任务文件 {src}：{e.strerror}")
    return src, task


def cmd_task_check(a: argparse.Namespace) -> None:
    """只校验，不登记、不拷贝：和 stb task add 同一套检查。"""
    _src, task = _validate(a.file)
    from control_center.registry import functions

    for t in task.tools:                                # 任务能用的函数（load_task 已查过重名）
        print(f"  {t.name}：{', '.join(functions(TOOLS[t.name])) or '（无）'}")
    print(f"OK {task.name}（触发：{task.trigger_label}）")


def cmd_task_add(a: argparse.Namespace) -> None:
    ensure_dirs()
    src, task = _validate(a.file)
    from control_center.runner import mark_registered

    dst = task_file(task.name)
    if src.resolve() != dst.resolve():                  # 任务定义只有 tasks/<名>/task.yaml 这一份；从别处（如测试夹具）登记时才拷进来
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    mark_registered(task.name)
    print(f"已登记 {task.name}（{dst}），触发：{task.trigger_label}")
    if task.schedule:
        nxt = schedule.next_run(task, time.time())
        print(f"时间表 {schedule.text(task.schedule)}：stbcc-hub 到点入队" + (f"，下次 {time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt))}" if nxt else "（yaml 的 enabled 是 false，不触发）"))
    if task.listen is not None:
        print(f"监听 {task.listen}：stbcc-hub 运行 {task.listen_file}（{task.trigger_label}），fire 就入队")
    if task.schedule or task.listen is not None:
        print(f"启停改 yaml 的 enabled，或 `stb task enable / disable {task.name}`；也可 `stb run --now` 手动触发")
    else:
        print("trigger: manual：只能 `stb run --now` 手动触发")


def _last_run(task_name: str) -> dict | None:
    d = RUNS / task_name
    if not d.exists():
        return None
    runs = sorted(p for p in d.iterdir() if (p / "run.json").exists())
    if not runs:
        return None
    return json.loads((runs[-1] / "run.json").read_text())


def cmd_task_list(_a: argparse.Namespace) -> None:
    ensure_dirs()
    rows = []
    for p in task_files():
        try:
            task = load_task(p)
        except TaskError as e:
            rows.append((task_name_of(p), "?", "?", "?", f"定义无效：{e}", "", ""))
            continue
        nxt = schedule.next_run(task, time.time())
        last = _last_run(task.name)
        rows.append((task.name, str(task.priority), task.trigger_label, schedule.text(task.schedule) or "-",
                     "是" if task.enabled else "否", f"{last['status']} {last.get('run_id', '')}" if last else "-",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else "-"))
    if not rows:
        print("（没有登记的任务）")
        return
    head = ("任务", "优先级", "触发", "时间表", "启用", "上次", "下次")
    w = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(r)))


def _rm_targets(name: str, keep_runs: bool = False, keep_work: bool = False) -> list[Path]:
    """stb task rm 要删的：存在的定义、运行记录（keep_runs 时不含）、工作目录（keep_work 时不含）。"""
    f = task_file(name)
    return [p for p in (f.parent, *([] if keep_runs else [RUNS / name]), *([] if keep_work else [WORK / name])) if p.exists()]


def remove_task(name: str, keep_runs: bool = False, keep_work: bool = False) -> None:
    """删掉任务：删定义与工作目录、注销登记与补跑状态（定时触发的记录由 stbcc-hub 下一分钟自己清掉）；keep_runs 时运行记录（logs/runs/<任务>/）留着（一次性任务跑完自动注销用），
    keep_work 时工作目录（<workspace>/<任务>/）也留着（builder 下线任务用 --keep-data：两样都留）。"""
    from control_center.runner import REGISTERED, _atomic_write, _catchup_state_path, load_registered

    for p in _rm_targets(name, keep_runs, keep_work):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    for path, loader in ((REGISTERED, load_registered), (_catchup_state_path(), None)):
        try:
            d = loader() if loader else json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if d.pop(name, None) is not None:
            _atomic_write(path, json.dumps(d, ensure_ascii=False, indent=1))


def cmd_task_rm(a: argparse.Namespace) -> None:
    """删掉一个任务（临时任务拿到结果后用）：登记、定义、运行记录、工作目录。不带 --yes 只列清单。"""
    from control_center.runner import load_registered, task_pending

    cur = json.loads(RUNNER_STATE.read_text()).get("current") if RUNNER_STATE.exists() else None
    if cur and cur.get("task") == a.name:
        _die(f"{a.name} 正在跑，等它结束（或 `stb pause`）再删")
    if task_pending(a.name):
        _die(f"{a.name} 还在队列里或还有没跑完的步骤，等它跑完再删")
    from control_center.task import listen_dependents, listen_dependents_hint

    if dep := listen_dependents(a.name):
        _die(listen_dependents_hint(a.name, *dep))
    targets = _rm_targets(a.name, a.keep_data, a.keep_data)
    registered = a.name in load_registered()
    if not targets and not registered:
        _die(f"没有任务 {a.name}")
    print(f"将删除 {a.name}：" + ("登记、" if registered else "") + "、".join(str(p) for p in targets))
    if not a.yes:
        print("结果和数据先拷到仓库外（如 ~/research/<任务>/），确认后加 --yes")
        return
    remove_task(a.name, a.keep_data, a.keep_data)
    print(f"已删除 {a.name}" + (f"（运行记录 {RUNS / a.name} 与工作目录 {WORK / a.name} 保留）" if a.keep_data else ""))


def cmd_task_toggle(a: argparse.Namespace, enable: bool) -> None:
    """把任务 yaml 的 enabled 改成 true / false（保留注释与格式）：stbcc-hub 按它定时触发、检查监听、交事件（现读 yaml，不用重启）。"""
    from control_center.task_edit import save_task

    try:
        save_task(a.name, None, {"enabled": enable})
    except (TaskError, OSError, RuntimeError) as e:
        _die(f"任务 {a.name}：{e}")
    print(f"{'已启用' if enable else '已停用'} {a.name}（yaml enabled: {str(enable).lower()}）")


def cmd_run(a: argparse.Namespace) -> None:
    from control_center.runner import HELD_SUFFIX, enqueue, load_registered

    if not task_file(a.name).exists():
        _die(f"任务 {a.name} 不存在：没有 tasks/{a.name}/task.yaml")
    if a.name not in load_registered():                 # 用内存 / GPU 的活一律先登记再跑（临时测试也一样，跑完 stb task rm）
        _die(f"任务 {a.name} 没登记：先 `stb task add {task_file(a.name)}`")
    p = enqueue(a.name, trigger="manual")
    print(f"已入队 {p.name}")
    if not RUNNER_STATE.exists() or time.time() - json.loads(RUNNER_STATE.read_text()).get("updated", 0) > 3600 * 24:
        print("提示：stbcc-runner 似乎没在跑（`stb runner --install` 安装为用户服务，或 `stb runner` 前台运行）")
    if a.wait:
        before = {r.name for r in (RUNS / a.name).glob("*")} if (RUNS / a.name).exists() else set()
        while True:
            time.sleep(3)
            now = sorted(r for r in (RUNS / a.name).glob("*") if r.name not in before) if (RUNS / a.name).exists() else []
            if now and (now[-1] / "run.json").exists():
                info = json.loads((now[-1] / "run.json").read_text())
                if info.get("status") not in ("running", "stepping"):     # workflow 步骤之间（stepping）还没跑完
                    print(json.dumps(info, ensure_ascii=False, indent=1))
                    return
            if (held := p.with_name(p.name + HELD_SUFFIX)).exists():    # 开跑前加载模型失败，条目留着等处理（runner.hold_entry）：说原因、退出，不干等
                why = json.loads(held.read_text()).get("held_reason") or "（没记原因）"
                _die(f"{a.name} 没开跑，条目留着等处理（{held.name}）：{why}\n处理好之后点私信卡上的「重试」放回队列")
            if not p.exists() and not now and not RUNNER_STATE.exists():
                _die("队列条目被取走但没有运行记录")


def cmd_pause(a: argparse.Namespace) -> None:
    """让正在跑的这个任务在下一个边界暂停（和抢占同一条路，但不会自动恢复）。"""
    from control_center.paths import CONTROL

    cur = json.loads(RUNNER_STATE.read_text()).get("current") if RUNNER_STATE.exists() else None
    if not cur or cur.get("task") != a.name:
        _die(f"{a.name} 没在跑（当前：{cur.get('task') if cur else '空闲'}）")
    if not cur.get("preemptible"):
        _die(f"{a.name} 是写了 resumable: false 的 workflow 任务，步骤中间不能暂停")
    CONTROL.mkdir(parents=True, exist_ok=True)
    (CONTROL / f"pause-{a.name}").write_text(str(time.time()))
    print(f"已请求暂停 {a.name}（在下一个模型/工具边界生效；`stb resume {a.name}` 恢复）")


def cmd_stop(a: argparse.Namespace) -> None:
    """停下这个任务正在跑的那次（同私信的 /stop：写停止标记，runner 或 workflow 调度在下一个边界取走，记 stopped、不投递不续跑）。
    agent 任务、workflow 任务正在跑的步骤或步骤之间都行；队列里还没开跑的、被抢占等恢复的不动。"""
    from control_center import workflow
    from control_center.paths import CONTROL

    cur = (json.loads(RUNNER_STATE.read_text()).get("current") if RUNNER_STATE.exists() else None) or {}
    if cur.get("task") != a.name and not workflow.active(a.name):
        _die(f"{a.name} 没在跑（当前：{cur.get('task') or '空闲'}）")
    CONTROL.mkdir(parents=True, exist_ok=True)
    (CONTROL / f"stop-{a.name}").write_text(str(time.time()))
    print(f"已请求停止 {a.name}（在下一个模型 / 工具边界停下，记为 stopped；排队没开跑的不动）")


def cmd_resume(a: argparse.Namespace) -> None:
    from control_center import paths                     # 调用时才取运行目录：测试换了数据目录也跟着走
    from control_center.runner import enqueue

    d = paths.RUNS / a.name
    paused = [p for p in sorted(d.iterdir()) if (p / "run.json").exists() and json.loads((p / "run.json").read_text()).get("status") == "paused"] if d.exists() else []
    if not paused:
        _die(f"{a.name} 没有 paused 的运行")
    run_id = paused[-1].name
    info = json.loads((paused[-1] / "run.json").read_text())
    from control_center.runner import held_entries, norm_trigger, requeue

    if held := [h for h in held_entries() if h[1].get("task") == a.name and h[1].get("resume") == run_id]:
        p = requeue(held[0][1])                          # 开跑前加载失败留着的恢复条目：原样放回（优先级、入队时刻不变），卡上的「重试」随之作废
        for h, _ in held:
            h.unlink(missing_ok=True)
        print(f"已放回队列恢复 {a.name} {run_id}（加载失败留着的条目）→ {p.name}")
        return
    p = enqueue(a.name, trigger=norm_trigger(info.get("trigger")), resume=run_id, priority=info.get("priority"))
    print(f"已入队恢复 {a.name} {run_id} → {p.name}")


def cmd_runs(a: argparse.Namespace) -> None:
    from control_center import paths                     # 调用时才取运行目录（同 cmd_resume）

    d = paths.RUNS / a.name
    if not d.exists():
        print("（没有运行记录）")
        return
    for p in sorted(d.iterdir()):
        if (p / "run.json").exists():
            i = json.loads((p / "run.json").read_text())
            extra = f" preempted={i['preempt_count']}" if i.get("preempt_count") else ""
            print(f"{p.name}  {i.get('status'):9} {str(i.get('seconds', '')):>7}s  calls={i.get('calls', '')} rej={i.get('rejections', '')}{extra}  {i.get('error', '')}")


def cmd_logs(a: argparse.Namespace) -> None:
    d = RUNS / a.name
    runs = sorted(p for p in d.iterdir()) if d.exists() else []
    if not runs:
        _die("没有运行记录")
    run = d / a.run if a.run else runs[-1]
    for f in ("run.json", "gateway.jsonl", "agent/toollog.jsonl", "agent.stderr.log"):
        p = run / f
        if p.exists():
            print(f"===== {p} =====")
            print(p.read_text(errors="replace")[-20000:])


def cmd_tools_list(_a: argparse.Namespace) -> None:
    for spec in TOOLS.values():
        flags = "subagent-only" if spec.usable_by == "subagent" else ""   # 只给子代理用的
        print(f"{spec.name:20} {flags:14} {spec.description}")
    from control_center.registry import TOOL_ERRORS

    for folder, why in TOOL_ERRORS.items():
        print(f"⚠ {folder}：tool.yaml 不合格，没登记：{why}")


def cmd_subagents_list(_a: argparse.Namespace) -> None:
    """子代理配置（subagents/<名>/subagent.yaml：种类、模型、单次上限、工具或程序、隔离）；顺带校验（缺字段、未知档位、未知工具就报错退出）。"""
    from control_center import subagents

    try:
        defs = subagents.load_defs()
        models = load_models()
        for d in defs:
            subagents.profile_name(d, models, "")
    except subagents.SubagentError as e:
        _die(str(e))
    for d in defs:
        model = "inherit（同调用它的任务）" if d.inherit else f"{d.runtime} + {d.model}"
        what = f"工具 {', '.join(d.tools) or '无'}" if d.kind == "agent" else f"程序 {d.server.name if d.server else ''}，参数 prompt{''.join(', ' + k for k in d.inputs)}"
        print(f"{d.name:28} {d.kind}（主模型直接调用）；模型 {model}；单次上限 {'不限' if d.timeout is None else f'{d.timeout // 60} 分钟'}；{what}" + (("；隔离（程序自身是隔离边界，结果原样交回）" if d.prevalidated else "；隔离（核对引文）") if d.isolated else ""))


def cmd_schema(a: argparse.Namespace) -> None:
    """从字段表 control_center/schema/fields.yaml 生成 Markdown 表；--write-manual 把手册里成对标记之间的表换成最新的；approve 发审批卡，Joy 同意后更新 control_center/schema/fields.lock。"""
    from control_center import schema, scheme_check

    if a.kind == "approve":
        print(asyncio.run(scheme_check.approve()))
        return
    if a.kind == "check":
        probs = scheme_check.problems()
        print("\n".join(probs) or "字段表与 fields.lock 一致，仓库里的 yaml 都只用表内字段")
        sys.exit(1 if probs else 0)
    if a.write_manual is None:
        for k in [a.kind] if a.kind else schema.KINDS:
            print(f"## {k}\n\n{schema.table(k)}\n")
        return
    p = Path(a.write_manual).expanduser() if a.write_manual else schema.MANUAL
    if not p.is_file():
        _die(f"手册不在：{p}")
    old = p.read_text()
    new = schema.render_manual(old)
    if new != old:
        p.write_text(new)
    missing = [k for k in schema.KINDS if f"<!-- schema:{k}:begin -->" not in new]
    print(f"{'已更新' if new != old else '已是最新'} {p}" + (f"；缺标记：{', '.join(missing)}" if missing else ""))


def cmd_builder(a: argparse.Namespace) -> None:
    """builder 当前计划的状态；serve 是服务 stbcc-builder（--install 装成用户服务）；worker 由 systemd-run 起，不手动用。"""
    if a.install:
        return _install_service("stbcc-builder")
    if a.op == "serve":
        from control_center.builder.service import main as builder_main

        return builder_main()
    from control_center import builder

    if a.op == "worker":
        if len(a.args) != 2:
            _die("用法：stb builder worker post <计划id>")
        return builder.worker(a.args[0], a.args[1])      # 认不认识 op 由 worker 判断：出错私信 Joy
    print(builder.status_text())


def cmd_docker(a: argparse.Namespace) -> None:
    """stbcc-docker（起容器的启动器）：list 看在跑的容器；serve 是服务本身（--install 装成用户服务）；build <工具名> 用工具目录的 Dockerfile 建 stb-<工具名>。"""
    if a.install:
        return _install_service("stbcc-docker")
    if a.op == "serve":
        from control_center.docker_service import main as docker_main

        return docker_main()
    from control_center import docker_client

    try:
        if a.op == "build":
            if len(a.args) != 1:
                _die("用法：stb docker build <工具名>")
            r = docker_client.build(a.args[0])
            sys.stdout.write(r.stdout.decode(errors="replace"))
            sys.exit(r.returncode)
        rows = docker_client.containers()
    except docker_client.DockerError as e:
        _die(str(e), 1)
    if not rows:
        print("没有在跑的容器")
    for c in rows:
        exp = f"/预计 {int(c['expected'])}s" if c.get("expected") else ""
        owner = f"监听 {c['listen']}" if c.get("listen") else f"运行 {c['run'] or '-'}"
        print(f"{c['container']}  {c['tool']}  {c['image']}  {owner}  已跑 {c['seconds']}s{exp}" + ("  ⚠ 超过预计时长" if c.get("overdue") else "")
              + (f"  正在停（{c['stopping']}）" if c.get("stopping") else ""))


def cmd_doctor(a: argparse.Namespace) -> None:
    from control_center.doctor import doctor

    report = doctor(smoke=a.smoke)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    sys.exit(0 if report["ok"] else 1)


def _install_service(name: str, timer: bool = False) -> None:
    unit = systemd.UNIT_DIR / f"{name}.service"
    systemd.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit.write_text((REPO / "services" / "systemd" / f"{name}.service").read_text()
                    .replace("{STB}", str(Path(sys.executable).parent / "stb")).replace("{REPO}", str(REPO)))
    if timer:                                         # 定时跑的服务：同名 .timer 一起装，启用的是定时器
        (systemd.UNIT_DIR / f"{name}.timer").write_text((REPO / "services" / "systemd" / f"{name}.timer").read_text())
    systemd.systemctl("daemon-reload")
    r = systemd.systemctl("enable", "--now", f"{name}.{'timer' if timer else 'service'}", check=False)
    print(unit, "已安装", r.stderr.strip() or "并启动")


def cmd_runner(a: argparse.Namespace) -> None:
    if a.install:
        return _install_service("stbcc-runner")
    from control_center import scheme_check
    from control_center.runner import serve

    scheme_check.require("stbcc-runner")             # 字段表锁对不上就不启动（私信 Joy）
    asyncio.run(serve())


def cmd_run_unit(a: argparse.Namespace) -> None:
    from control_center.runner import exec_unit

    asyncio.run(exec_unit(a.unit))


def cmd_hub(a: argparse.Namespace) -> None:
    if a.install:
        return _install_service("stbcc-hub")
    from control_center.hub import main as hub_main

    asyncio.run(hub_main())


def _register_channels(sub: argparse._SubParsersAction) -> None:
    """渠道自己注册 stb 子命令（如 stb web、stb discord）：扫描 channels/*/cli.py（不写死名单），调它的 register(subparsers)。
    某个渠道导入或注册失败只打一行警告，其余命令照常。平台核心只在这里动态加载渠道代码。"""
    import importlib

    for f in sorted((REPO / "channels").glob("*/cli.py")):
        name = f.parent.name
        try:
            importlib.import_module(f"channels.{name}.cli").register(sub)
        except Exception as e:                          # noqa: BLE001  坏一个渠道不拖垮整个 stb
            print(f"警告：渠道 {name} 的 stb 子命令没加载（{type(e).__name__}: {e}）", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="stb", description="sparky-toolbox：Sparky 的无人值守任务平台")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("task", help="任务登记与启停").add_subparsers(dest="sub", required=True)
    p = t.add_parser("add", help="校验 + 登记（定时、监听由 stbcc-hub 按 yaml 触发）"); p.add_argument("file"); p.set_defaults(fn=cmd_task_add)
    p = t.add_parser("check", help="只校验任务定义（同 add 的检查），不登记"); p.add_argument("file"); p.set_defaults(fn=cmd_task_check)
    t.add_parser("list", help="名称、触发方式、时间表、上次状态、下次时间").set_defaults(fn=cmd_task_list)
    p = t.add_parser("rm", help="删掉任务（临时任务拿到结果后用）：登记、定义、运行记录、工作目录；不带 --yes 只列清单"); p.add_argument("name"); p.add_argument("--yes", action="store_true")
    p.add_argument("--keep-data", action="store_true", help="运行记录与工作目录保留（只注销登记、定义）"); p.set_defaults(fn=cmd_task_rm)
    p = t.add_parser("enable"); p.add_argument("name"); p.set_defaults(fn=lambda a: cmd_task_toggle(a, True))
    p = t.add_parser("disable"); p.add_argument("name"); p.set_defaults(fn=lambda a: cmd_task_toggle(a, False))
    p = sub.add_parser("run", help="立即跑一次（入队，由 runner 执行）"); p.add_argument("name"); p.add_argument("--now", action="store_true", required=True); p.add_argument("--wait", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("runs", help="运行历史"); p.add_argument("name"); p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("pause", help="在下一个边界暂停正在跑的任务（agent 任务，或 workflow 任务正在跑的要资源的那一步）"); p.add_argument("name"); p.set_defaults(fn=cmd_pause)
    p = sub.add_parser("resume", help="恢复最近一次 paused 的运行"); p.add_argument("name"); p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("stop", help="停下任务正在跑的那次（同私信 /stop；agent 任务、workflow 任务都行，排队没开跑的不动）"); p.add_argument("name"); p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("logs", help="看最近一次运行的日志"); p.add_argument("name"); p.add_argument("--run"); p.set_defaults(fn=cmd_logs)
    sub.add_parser("tools", help="工具注册表").add_subparsers(dest="sub", required=True).add_parser("list").set_defaults(fn=cmd_tools_list)
    sub.add_parser("subagents", help="子代理配置（subagents/）").add_subparsers(dest="sub", required=True).add_parser("list").set_defaults(fn=cmd_subagents_list)
    p = sub.add_parser("schema", help="字段表：task / tool-yaml / subagents / models / stbcc 生成 Markdown，--write-manual 写进手册的标记之间；"
                                      "check 查字段表锁与 yaml；approve 发审批卡，Joy 同意后更新 control_center/schema/fields.lock")
    p.add_argument("kind", nargs="?", choices=["task", "tool-yaml", "subagents", "models", "stbcc", "check", "approve"]); p.add_argument("--write-manual", nargs="?", const="", default=None, metavar="手册路径")
    p.set_defaults(fn=cmd_schema)
    p = sub.add_parser("builder", help="builder：status 看当前计划；serve 是服务 stbcc-builder，--install 装成用户服务（worker 由平台内部调用）")
    p.add_argument("op", nargs="?", default="status", choices=["status", "serve", "worker"]); p.add_argument("args", nargs="*"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_builder)
    p = sub.add_parser("docker", help="stbcc-docker（起容器的启动器）：list 看在跑的容器；serve 是服务，--install 装成用户服务；build <工具名> 建 stb-<工具名> 镜像")
    p.add_argument("op", nargs="?", default="list", choices=["list", "serve", "build"]); p.add_argument("args", nargs="*"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_docker)
    p = sub.add_parser("doctor", help="在跑的档位、沙箱镜像、内部网络、runner、定时触发、凭证、字段表锁、工具 / 子代理文件夹里的 check.py；--smoke 起最小沙箱"); p.add_argument("--smoke", action="store_true"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("runner", help="常驻执行器；--install 装成 systemd 用户服务"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_runner)
    p = sub.add_parser("cleanup", help="每日清理 stbcc-cleanup：--install 装成用户定时器（.service 与 .timer）"); p.add_argument("--install", action="store_true", required=True)
    p.set_defaults(fn=lambda a: _install_service("stbcc-cleanup", timer=True))
    p = sub.add_parser("launcher", help="owa-launcher（联网研究的一次性容器）：--install 装成用户服务"); p.add_argument("--install", action="store_true", required=True)
    p.set_defaults(fn=lambda a: _install_service("owa-launcher"))
    p = sub.add_parser("run-unit", help="（stbcc-runner 起的运行单元用）跑一次运行"); p.add_argument("unit"); p.set_defaults(fn=cmd_run_unit)
    _register_channels(sub)
    p = sub.add_parser("hub", help="聊天中枢（卡片、系统通知、私信与命令、事件、任务监听、状态与进度）；--install 装成用户服务"); p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_hub)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
