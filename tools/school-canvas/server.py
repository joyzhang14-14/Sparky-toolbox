"""school-canvas 工具：用 Canvas API token 只读查询 UCI Canvas（canvas.eee.uci.edu）。只给子代理用（tool.yaml 写 usable_by: subagent，
列在 subagents/isolated-reader/ 的 tools）：课程名、作业名、模块名也是老师写的文字，主模型只拿到隔离读者核对过引文的答案。
凭证：.env 的 STB_CANVAS_TOKEN，用 credential() 读（Joy 在 Canvas 设置里生成，2026-12-12 到期，可随时撤销）。
只返回结构化字段（课程名、作业名、截止时间、提交状态、分数、模块完成度），**不返回作业说明、公告正文、讨论帖**：
那些是大段自由文本；要读就让子代理 isolated-browser 用登录档案 uci 去看。
时间一律换成本机时区（control_center.paths.host_tz）的 "YYYY-MM-DD HH:MM"。
"""
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from control_center.credential import credential
from control_center.tool_kit import platform_fault, result
from control_center.paths import host_tz

TZ = ZoneInfo(host_tz())
MAX_NAME = 120
API = "https://canvas.eee.uci.edu/api/v1"     # 结果的来源写这次查的接口（source）

server = MCPServer("school-canvas", instructions="Joy 的 UCI Canvas（只读）")


def _cfg() -> dict:
    return {"base": "https://canvas.eee.uci.edu", "token": credential("STB_CANVAS_TOKEN")}


def _get(path: str, params: dict | None = None) -> list | dict:
    """GET + 跟着 Link: rel="next" 翻页（最多 20 页）。"""
    c = _cfg()
    url = c.get("base", "https://canvas.eee.uci.edu").rstrip("/") + "/api/v1" + path
    headers = {"Authorization": f"Bearer {c['token']}"}
    out: list = []
    params = {"per_page": 100, **(params or {})}
    with httpx.Client(timeout=30, headers=headers) as h:
        for _ in range(20):
            try:
                r = h.get(url, params=params)
            except httpx.TransportError as e:
                raise platform_fault(f"连不上 Canvas（{e!r}）") from None
            if r.status_code == 401:
                raise platform_fault("Canvas token 无效或已过期（HTTP 401）：重试没用，要 Joy 在 Canvas → Account → Settings 重新生成；用问题卡告诉他")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return data
            out.extend(data)
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
            if not m:
                break
            url, params = m.group(1), None
    return out


def _t(s: str | None) -> str:
    if not s:
        return ""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _name(s: str | None) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:MAX_NAME]


def _courses() -> list[dict]:
    cs = _get("/courses", {"enrollment_state": "active", "include[]": ["total_scores", "term"]})
    out = []
    for c in cs:
        if not isinstance(c, dict) or c.get("access_restricted_by_date"):
            continue
        enr = next((e for e in c.get("enrollments") or [] if e.get("type") == "student"), {})
        out.append({"id": c["id"], "code": _name(c.get("course_code")), "name": _name(c.get("name")),
                    "term": _name((c.get("term") or {}).get("name")),
                    "score": enr.get("computed_current_score"), "grade": enr.get("computed_current_grade")})
    return out


@server.tool(name="school-canvas_courses", description="列出 Joy 当前在 Canvas 上的课程：id、课号、课名、学期、当前总分和等级（没出分就是 null）")
def courses() -> CallToolResult:
    return result(json.dumps(_courses(), ensure_ascii=False), source=f"{API}/courses（当前课程与总分）")


@server.tool(name="school-canvas_upcoming", description="未来 days 天内（默认 7 天）所有课程要交的作业 / 测验：课程、名称、截止时间（本机时区）、分值、是否已提交、迟交 / 缺交。不给作业说明")
def upcoming(days: int = 7) -> CallToolResult:
    days = max(1, min(int(days), 60))
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    rows = []
    for c in _courses():
        try:
            items = _get(f"/courses/{c['id']}/assignments", {"bucket": "future", "include[]": ["submission"], "order_by": "due_at"})
            items += _get(f"/courses/{c['id']}/assignments", {"bucket": "overdue", "include[]": ["submission"], "order_by": "due_at"})
        except httpx.HTTPStatusError:
            continue
        for a in items:
            due = a.get("due_at")
            if not due:
                continue
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            sub = a.get("submission") or {}
            overdue = dt < now
            if dt > end or (overdue and sub.get("workflow_state") in ("submitted", "graded")):
                continue
            rows.append({"course": c["code"], "name": _name(a.get("name")), "due": _t(due), "points": a.get("points_possible"),
                         "submitted": sub.get("workflow_state") in ("submitted", "graded", "pending_review"),
                         "late": bool(sub.get("late")), "missing": bool(sub.get("missing")) or (overdue and not sub.get("submitted_at")),
                         "types": a.get("submission_types")})
    rows.sort(key=lambda r: r["due"])
    return result(json.dumps(rows, ensure_ascii=False), source=f"{API}/courses/<各课>/assignments（未来 {days} 天要交的与逾期的）")


@server.tool(name="school-canvas_missing", description="Canvas 标记为缺交（missing）的作业：课程、名称、截止时间、分值")
def missing() -> CallToolResult:
    names = {c["id"]: c["code"] for c in _courses()}
    items = _get("/users/self/missing_submissions", {"filter[]": "current_grading_period"})
    return result(json.dumps([{"course": names.get(a.get("course_id"), a.get("course_id")), "name": _name(a.get("name")),
                        "due": _t(a.get("due_at")), "points": a.get("points_possible")} for a in items], ensure_ascii=False),
                  source=f"{API}/users/self/missing_submissions（缺交的作业）")


@server.tool(name="school-canvas_grades", description="某门课（course_id，来自 school-canvas_courses）已出分的作业：名称、得分 / 满分、评分时间；不给评语")
def grades(course_id: int) -> CallToolResult:
    items = _get(f"/courses/{int(course_id)}/assignments", {"include[]": ["submission"], "order_by": "due_at"})
    rows = []
    for a in items:
        sub = a.get("submission") or {}
        if sub.get("score") is None:
            continue
        rows.append({"name": _name(a.get("name")), "score": sub.get("score"), "points": a.get("points_possible"), "graded_at": _t(sub.get("graded_at"))})
    return result(json.dumps(rows, ensure_ascii=False), source=f"{API}/courses/{int(course_id)}/assignments（已出分的）")


@server.tool(name="school-canvas_modules", description="某门课（course_id）的模块完成进度：每个模块名、状态（locked / unlocked / started / completed）、已完成项数 / 必做项数")
def modules(course_id: int) -> CallToolResult:
    ms = _get(f"/courses/{int(course_id)}/modules", {"include[]": "items"})
    out = []
    for m in ms:
        items = m.get("items") or []
        req = [i for i in items if i.get("completion_requirement")]
        done = [i for i in req if (i.get("completion_requirement") or {}).get("completed")]
        out.append({"module": _name(m.get("name")), "state": m.get("state"), "done": len(done), "required": len(req)})
    return result(json.dumps(out, ensure_ascii=False), source=f"{API}/courses/{int(course_id)}/modules（模块完成进度）")


if __name__ == "__main__":
    server.run("stdio")
