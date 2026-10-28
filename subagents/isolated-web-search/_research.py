"""把研究请求交给宿主的 owa-launcher（隔离的一次性研究容器）并等结果（isolated-web-search 子代理的程序用，同文件夹）。
暂停：pause_file 出现就取消研究（DELETE），launcher 返回的阶段性结果就是暂停边界。
任务号记到运行目录（STB_RUN_DIR）的 research_tasks.jsonl（RESEARCH_LOG）：运行的一段结束（任何结束方式）时子代理的 run_end hook
（subagents/isolated-web-search/cancel_research.py）据此取消还没结束的研究（D18）。没有运行目录（手动调用）时不记。"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from control_center.credential import credential
from control_center.paths import host_tz
from control_center.tool_kit import platform_fault

LAUNCHER = os.environ.get("STB_TEST_OWA_LAUNCHER_URL", "http://127.0.0.1:18081")   # owa-launcher 的地址；测试用 STB_TEST_OWA_LAUNCHER_URL 指到假服务
RESEARCH_LOG = "research_tasks.jsonl"     # 运行目录里记研究任务号的文件（run_end hook cancel_research.py 读它）
POLL_WAIT = 20


def research_model(conf: dict[str, Any]) -> dict[str, str]:
    """研究容器访问这个档位的模型（D32）：launcher 只收 http://host.docker.internal:<端口>/v1 与模型 id，按档位的 endpoint、调用名 call_name 拼；
    没有（端点不答 /models、手动调用没给 STB_MODEL）返回空，用 launcher 的配置。"""
    if not conf.get("endpoint") or not conf.get("call_name"):
        return {}
    u = urlsplit(str(conf["endpoint"]))
    return {"model_base_url": f"http://host.docker.internal:{u.port or 80}{u.path}", "model_id": str(conf["call_name"])}


MODEL = research_model(json.loads(os.environ.get("STB_MODEL") or "{}"))   # 网关给子代理的程序它的模型档位（STB_MODEL，models.yaml 的整段配置）
LOCAL_TZ = ZoneInfo(host_tz())   # 本机时区（D22 J6）：研究请求的"今天"按它算
# launcher 报的这些失败是平台没把研究跑起来（容器没起来 / 被杀、宿主内存不够、模型或搜索服务不可用）：报平台故障
PLATFORM_CODES = {"LAUNCH_FAILED", "RESOURCE_PRESSURE", "WORKER_ERROR", "MODEL_UNAVAILABLE", "SEARCH_UNAVAILABLE"}
BLOCKED_MARK = "研究未完成："        # blocked() 的 summary 开头：launcher 没把研究跑起来（不是研究模型写的）
TIMEOUT_MARK = "超时（isolated-web-search 超过单次上限"   # timed_out() 的说明：launcher 到点强停


def local_date(now: datetime | None = None) -> str:
    return (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ).date().isoformat()


def blocked(code: str, message: str, seconds: float) -> dict[str, Any]:
    return {"status": "blocked", "summary": f"{BLOCKED_MARK}{code}: {message}", "claims": [], "tables": [], "sources": [],
            "disagreements": [], "missing_information": [], "access_failures": [], "usage": {"wall_seconds": seconds}}


def platform_error(err: dict[str, Any]) -> Exception:
    msg = str(err.get("message", ""))
    if re.search(r"退出码 137\b", msg):          # 被 SIGKILL：容器 --memory 上限触发的 OOM 最常见
        return platform_fault("研究容器被系统强制终止（退出码 137，通常是内存不足）")
    return platform_fault(f"研究没跑起来（{err.get('code', '')}：{msg[:300]}）")


def _headers() -> dict[str, str]:
    """launcher 的令牌：.env 的 OWA_LAUNCHER_TOKEN（credential；缺了报 [缺凭证]，网关给 Joy 发凭证卡）。"""
    return {"Authorization": f"Bearer {credential('OWA_LAUNCHER_TOKEN')}"}


def image(task_id: str, artifact_id: str) -> bytes | None:
    """研究选中的一张图（launcher 证据目录 <任务号>/<artifact_id>.png）：经 launcher 的 GET /tasks/<任务号>/images/<artifact_id>.png 取
    （证据目录归 launcher 管，按任务号经它取）；没有或取不到返回 None。"""
    try:
        r = httpx.get(f"{LAUNCHER}/tasks/{task_id}/images/{artifact_id}.png", headers=_headers(), timeout=30)
    except httpx.HTTPError:
        return None
    return r.content if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/png") else None


def run(body: dict[str, Any], pause_file: str | None = None, task: dict[str, str] | None = None) -> dict[str, Any]:
    """提交研究请求，轮询到结束，返回 ResearchResult（dict）。研究本身的失败返回结构化结果；平台故障（launcher 连不上或 5xx、
    研究容器没起来 / 被杀、宿主内存不够、模型或搜索服务不可用）抛平台故障。"""
    headers = _headers()
    t0 = time.time()
    body = {**body, **MODEL, "current_date": body.get("current_date") or local_date()}
    try:
        return _run(body, headers, pause_file, t0, task)
    except httpx.TransportError as e:
        raise platform_fault(f"owa-launcher 连不上（{LAUNCHER}）：{e!r}") from None


def _run(body: dict[str, Any], headers: dict[str, str], pause_file: str | None, t0: float, task: dict[str, str] | None = None) -> dict[str, Any]:
    with httpx.Client(base_url=LAUNCHER, headers=headers, timeout=POLL_WAIT + 40) as c:
        r = c.post("/tasks", json=body)
        if r.status_code >= 500:
            raise platform_fault(f"owa-launcher 返回 {r.status_code}：{r.text[:200]}")
        if r.status_code >= 300:
            return blocked("LAUNCHER", f"{r.status_code} {r.text[:200]}", 0)
        tid = r.json()["task_id"]
        if task is not None:
            task["id"] = tid                           # 调用方按任务号经 launcher 取图（images，image()）、读证据目录里的来源原文
        view = r.json()
        _log_task({"task_id": tid, "at": time.time()})
        while view.get("status") in ("queued", "running"):
            if pause_file and os.path.exists(pause_file):
                c.delete(f"/tasks/{tid}")           # 运行器要暂停：取消研究，拿部分结果当边界
            view = c.get(f"/tasks/{tid}", params={"wait": POLL_WAIT}).json()
        _log_task({"task_id": tid, "done": True})
        if view.get("status") == "timeout":           # launcher 到点强停（单次上限 + 30 秒）：告诉主模型子代理已超时，有阶段性结果就一起给
            return timed_out(view.get("result"), int((body.get("budget") or {}).get("max_seconds", 0)), time.time() - t0)
        if view.get("status") != "done":
            if isinstance(view.get("result"), dict) and view["result"].get("status"):
                return view["result"]                # 取消/超时时的阶段性结果（暂停边界就靠它）
            err = view.get("error") or {"code": str(view.get("status", "")).upper(), "message": ""}
            if str(err.get("code", "")) in PLATFORM_CODES:
                raise platform_error(err)
            return blocked(err.get("code", "ERROR"), err.get("message", ""), time.time() - t0)
        return view["result"]


def timed_out(partial: Any, limit: int, seconds: float) -> dict[str, Any]:
    note = f"{TIMEOUT_MARK} {limit // 60} 分钟（研究容器从开跑算），已停止；这次调用共 {int(seconds)} 秒）"
    if isinstance(partial, dict) and partial.get("status"):
        return {**partial, "summary": (f"（{note}；以下是超时前的阶段性结果）" + str(partial.get("summary") or ""))[:12000]}
    return {**blocked("TIMEOUT", "", seconds), "summary": note}


def _log_task(rec: dict[str, Any]) -> None:
    """任务号记进运行目录的 RESEARCH_LOG（run_end hook 读的就是这个文件）；没有 STB_RUN_DIR 不记。"""
    if run_dir := os.environ.get("STB_RUN_DIR"):
        with open(Path(run_dir) / RESEARCH_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
