"""stb doctor：模型端点、docker 与镜像、内部网络、runner、定时触发、凭证（规格第 13 节），以及工具 / 子代理自己的体检（folder_checks：
文件夹里的约定文件 check.py）。--smoke 再起一个最小沙箱验证只读根与网关连通。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


from control_center import schedule, scheme_check, systemd
from control_center.models import call_name, load_models, profiles, running, state
from control_center import env
from control_center.paths import REPO, RUNNER_STATE, ensure_dirs, sparky_user, task_files
from control_center.runner import load_registered
from control_center.task import load_task
from control_center.registry import PLATFORM_CREDENTIALS, credential_keys
from control_center.sandbox import NET_TOOLS_ONLY, bridge_ip, ensure_network


def _sh(*cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def secrets_check() -> dict[str, str]:
    """凭证：平台自己的（PLATFORM_CREDENTIALS）与工具代码里 credential("KEY") 读的键（registry.credential_keys 静态扫）在不在 .env；
    缺的写明哪些文件要它。.env 读不了（权限不对）就是 {"env": 原因}。不带值。"""
    out: dict[str, str] = {}
    try:
        have = env.values()
        for s in PLATFORM_CREDENTIALS:
            out[s] = "ok" if have.get(s) else "缺失"
        for s, files in credential_keys().items():
            out[s] = "ok" if have.get(s) else f"缺失（{'、'.join(files)} 要用）"
    except env.EnvError as e:
        out["env"] = str(e)
    return out


CHECK_FILE = "check.py"     # 约定文件：工具 / 子代理文件夹里有它，stb doctor 就跑它
CHECK_TIMEOUT = 60          # 每个 check.py 最多跑多久（秒）


def _check_owners() -> dict[Path, Any]:
    """有 check.py 的文件夹 → 用谁的身份跑它（hooks.Owner）：工具文件夹按那个工具，子代理文件夹按子代理。"""
    from control_center import hooks, subagents
    from control_center.registry import TOOLS

    owners: dict[Path, Any] = {}
    for spec in sorted(TOOLS.values(), key=lambda s: s.name):
        owners.setdefault(spec.folder, hooks.Owner("tool", spec.name, {}, spec.is_editable, spec.network))
    for d in sorted((REPO / "subagents").iterdir()) if (REPO / "subagents").is_dir() else []:
        if subagents.is_folder(d.name):
            try:
                a = subagents.load_def(d.name)
            except subagents.SubagentError:
                continue
            owners.setdefault(d, hooks.Owner("subagent", a.name, {}, a.is_editable))
    return {d: o for d, o in owners.items() if (d / CHECK_FILE).is_file()}


def run_check(owner: Any, f: Path) -> dict[str, Any]:
    """跑一个 check.py：身份同 hook（hooks._command：is_editable: false 的主人以平台身份，别的以 sparky、联网按主人的 network），带超时 CHECK_TIMEOUT；
    stdout 最后一行是 JSON {ok, detail}。退出码非 0、超时、输出不是这个格式都算不通过。"""
    from control_center import hooks

    try:
        argv, env = hooks._command(owner, hooks.Hook(command=str(f), timeout=CHECK_TIMEOUT), None, None, "doctor")
        p = subprocess.run(argv, input=b"", capture_output=True, timeout=CHECK_TIMEOUT, env=env, cwd=str(f.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"超时（{CHECK_TIMEOUT} 秒）"}
    except Exception as e:  # noqa: BLE001 — 起不来
        return {"ok": False, "detail": f"起不来：{type(e).__name__}: {str(e)[:200]}"}
    lines = [x for x in p.stdout.decode(errors="replace").splitlines() if x.strip()]
    try:
        got = json.loads(lines[-1]) if lines else None
    except ValueError:
        got = None
    if p.returncode != 0 or not isinstance(got, dict) or "ok" not in got:
        return {"ok": False, "detail": f"退出码 {p.returncode}；{(p.stderr.decode(errors='replace').strip() or (lines[-1] if lines else '没有输出'))[-300:]}"}
    return {"ok": bool(got["ok"]), "detail": got.get("detail")}


def folder_checks() -> dict[str, dict[str, Any]]:
    """工具 / 子代理文件夹里的 check.py 逐个跑（run_check），按文件的仓库路径汇总 {ok, detail}。"""
    return {str((d / CHECK_FILE).relative_to(REPO)): run_check(o, d / CHECK_FILE) for d, o in _check_owners().items()}


def doctor(smoke: bool = False) -> dict[str, Any]:
    ensure_dirs()
    checks: dict[str, dict[str, Any]] = {}
    models = load_models()
    live = running(models)
    for name, prof in profiles(models).items():
        if name not in live:                                     # 只查此刻在跑的档位（档位按任务需要加载，不在跑不算问题）
            continue
        st, called = state(prof, timeout=10), call_name(prof, timeout=10)
        checks[f"model:{name}"] = {"ok": st == "up", "detail": "加载中" if st != "up" else f"调用名 {called}" if called else "端点不答 /models，回 HTTP 200 算在跑"}
    checks.update(folder_checks())                                # 工具 / 子代理自己的体检（如联网研究依赖的服务）
    d = _sh("docker", "image", "inspect", "stb-agent:latest", "--format", "{{.Id}}")
    checks["image"] = {"ok": d.returncode == 0, "detail": d.stdout.strip()[:19] or d.stderr.strip()[:200] + "（services/stb-agent/build.sh 构建）"}
    try:
        ensure_network()
        checks["network"] = {"ok": True, "detail": f"{NET_TOOLS_ONLY} gateway {bridge_ip(NET_TOOLS_ONLY)}"}
    except Exception as e:  # noqa: BLE001
        checks["network"] = {"ok": False, "detail": str(e)[:200]}
    st = systemd.systemctl("is-active", "stbcc-runner.service", check=False).stdout.strip()
    checks["runner"] = {"ok": st == "active", "detail": {"service": st, "state_file": json.loads(RUNNER_STATE.read_text()) if RUNNER_STATE.exists() else None}}
    timers = []
    for p in task_files():
        try:
            t = load_task(p)
        except Exception:  # noqa: BLE001 — 定义坏了的任务另有报错
            continue
        if t.schedule:
            nxt = schedule.next_run(t, time.time())
            timers.append({"task": t.name, "schedule": t.schedule, "enabled": t.enabled, "registered": t.name in load_registered(),
                           "next": time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else None})
    checks["schedule"] = {"ok": True, "detail": timers}
    checks["secrets"] = {"ok": True, "detail": {"file": str(env.path()), **secrets_check()}}
    probs = scheme_check.problems()
    checks["schema"] = {"ok": not probs, "detail": probs or "字段表与 fields.lock 一致，仓库里的 yaml 都只用表内字段"}
    if smoke:
        try:
            gw = bridge_ip(NET_TOOLS_ONLY)
            user = sparky_user() or "1001:1001"
            r = _sh("docker", "run", "--rm", "--network", NET_TOOLS_ONLY, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", user,
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--tmpfs", f"/home/agent/.pi/agent:rw,size=16m,uid={user.split(':')[0]},gid={user.split(':')[1]}",
                    "--entrypoint", "bash", "stb-agent:latest", "-c",
                    f"pi --version && (touch /x 2>/dev/null && echo RW || echo RO) && (curl -s -m 3 -o /dev/null -w '%{{http_code}}' https://example.com/ || echo blocked)")
            out = r.stdout.split()
            checks["smoke"] = {"ok": r.returncode == 0 and "RO" in out and "200" not in out, "detail": r.stdout.strip()[:300] or r.stderr.strip()[:300], "gateway_ip": gw}
        except Exception as e:  # noqa: BLE001
            checks["smoke"] = {"ok": False, "detail": str(e)[:200]}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}
