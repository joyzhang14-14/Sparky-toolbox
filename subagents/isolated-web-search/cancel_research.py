"""isolated-web-search 的 run_end hook（子代理 yaml 的 hooks，is_editable: false 以平台身份跑）：运行的一段结束（任何结束方式，含被杀、超时、暂停、
运行进程没收尾的遗留运行）时，取消这次运行交给 owa-launcher、还没结束的研究（D18）——研究容器不在运行单元的 cgroup 里，不会跟着退出。
任务号是本子代理的程序（同文件夹的 _research.py）写在运行目录的 research_tasks.jsonl（_research.RESEARCH_LOG）；launcher 的地址与令牌同程序
（_research.LAUNCHER、.env 的 OWA_LAUNCHER_TOKEN）。launcher 不在、令牌读不到时静默跳过；有回应的都记回这个文件（done），重复收尾不再发，连不上的下次再试。
输入：stdin 一段 JSON {event, task, run_id, run_dir, status, …}。"""
import json
import re
import sys
from pathlib import Path

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
import _research  # noqa: E402
import httpx  # noqa: E402


def pending(log: Path) -> list[str]:
    """记过、还没标 done 的任务号（按记录顺序，去重）。"""
    started, done = [], set()
    for line in log.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        (done.add if rec.get("done") else started.append)(str(rec.get("task_id")))
    return [t for t in dict.fromkeys(started) if t not in done and re.fullmatch(r"[A-Za-z0-9-]+", t)]


def cancel(run_dir: Path) -> dict[str, int]:
    """取消 run_dir 里记着、还没结束的研究；返回 {任务号: launcher 的 HTTP 状态码}（2xx 才算已取消）。"""
    log = run_dir / _research.RESEARCH_LOG
    if not log.exists() or not (todo := pending(log)):
        return {}
    try:
        headers = _research._headers()
    except Exception:  # noqa: BLE001 — 令牌读不到（缺凭证、.env 读不了）：静默跳过
        return {}
    out: dict[str, int] = {}
    with httpx.Client(base_url=_research.LAUNCHER, headers=headers, timeout=5) as c:
        for t in todo:
            try:
                out[t] = c.delete(f"/tasks/{t}").status_code
            except httpx.HTTPError:
                pass
    with log.open("a") as f:
        for t, code in out.items():
            f.write(json.dumps({"task_id": t, "done": True, "cancelled": 200 <= code < 300, "status": code}) + "\n")
    return out


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    if req.get("run_dir") and (res := cancel(Path(req["run_dir"]))):
        print(f"取消研究：{json.dumps(res)}", file=sys.stderr)


if __name__ == "__main__":
    main()
