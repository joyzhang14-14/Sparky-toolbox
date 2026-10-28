"""browser-script 共用（工具 server.py 与它登记的监听程序 browser_script_watch.py）：脚本格式、演练记录、执行器参数、结果文件。

- 脚本：任务工作目录 scripts/ 下的 Playwright 模块（.mjs）：export const start_url；export async function run({ page, final, log })；
  条件触发的另 export async function check({ page, log })。容器、登录档案、域名拦截同 browser 工具（_browser.py）。
- 演练记录：演练跑通（停在 final() 前或跑完）时记在本工具的状态目录 data/tool_state/browser-script/dry/<任务>/<脚本哈希>.json；登记要有当前脚本内容的演练记录。
- 紧急任务：登记成平台的一次性动态监听（control_center/monitors.py，程序是 browser_script_watch.py）。程序到点（定时：执行时刻前 LEAD_S）或条件为真时
  emit fire（平台入队一次私信运行，主模型并行加载），同时直接实跑已批准的脚本（内容哈希要和批准时一致，只实跑一次），结果写进监听的状态目录 RESULT，
  主模型用 browser_script_result 等它。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

LEAD_S = 600                          # 定时：执行时刻前 10 分钟触发（主模型加载、登录要 Duo 时找 Joy）
MISSED_S = 300                        # 定时：程序起来时已过执行时刻这么久就记 missed、不补跑
WATCH_DEFAULT_S, WATCH_MIN_S = 30, 5  # 条件的检查间隔
FINAL_TRIES, FINAL_SECONDS = 40, 30   # final()：最多试 40 次、30 秒；成功一次就停
WATCH_ERRORS_NOTIFY = 5               # 条件检查连续出错几次私信 Joy 一次
RESULT = "result.json"                # 监听状态目录里：实跑的结果（browser_script_result 读）
LIVE_MARK = "live-started"            # 监听状态目录里：开始实跑了（程序中途被重起时据此不再重跑：只执行一次）
SHOTS = "shots"                       # 监听状态目录里：实跑的截图
WATCHER = "tools/browser-script/browser_script_watch.py"   # 紧急任务的监听程序（仓库相对路径，登记时交给平台）


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def start_url(source: str) -> str:
    m = re.search(r"export\s+const\s+start_url\s*=\s*[\"'`]([^\"'`]+)[\"'`]", source)
    return m.group(1) if m else ""


def dry_path(task: str, sha: str) -> Path:
    from control_center.paths import TOOL_STATE

    return TOOL_STATE / "browser-script" / "dry" / task / f"{sha}.json"


def save_dry(task: str, sha: str, rec: dict[str, Any]) -> None:
    p = dry_path(task, sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    tmp.replace(p)


def load_dry(task: str, sha: str) -> dict[str, Any] | None:
    try:
        return json.loads(dry_path(task, sha).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def when(kind: str, at: float | None, interval: int | None) -> str:
    if kind == "at":
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(at or 0)))
    return f"每 {interval} 秒检查一次"


def short(url: str) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    return f"{u.hostname or ''}{u.path}"[:160]         # 只留域名和路径：查询参数里可能有页面带来的内容


def summarize(r: dict[str, Any], safe: Any) -> dict[str, Any]:
    """执行器的回复 → 交给主模型的记录（结果、出错行号和报错第一行、跳转过的网址、写入请求），不含页面正文；safe：改掉 === 开头的行。"""
    from urllib.parse import urlsplit

    err = r.get("error") or None
    last_final = next((str(e.get("msg") or "") for e in reversed(r.get("events") or []) if e.get("kind") == "final_error"), "")
    return {"result": str(r.get("result")), "seconds": r.get("seconds"),
            **({"error": {"message": safe(err.get("message", ""))[:200], "line": err.get("line")}} if isinstance(err, dict) else {}),
            **({"final_tries": r["final_tries"]} if r.get("final_tries") else {}),
            **({"final_error": safe(last_final)[:300]} if r.get("result") == "final_failed" and last_final else {}),
            "navigations": [short(e["url"]) for e in r.get("events") or [] if e.get("kind") == "nav"][:30],
            "writes": [{"method": w["method"], "host": urlsplit(w["url"]).hostname or "", "path": urlsplit(w["url"]).path[:120]} for w in r.get("writes") or []][:50]}
