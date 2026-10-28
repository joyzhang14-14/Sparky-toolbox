"""modify-calendar 工具：读写 Joy 的 iCloud 日历（CalDAV，和 Mac / iPhone 上的 Apple Calendar 是同一份）。
凭证（.env 的键，用到时 credential() 读）：STB_ICLOUD_USER（Apple ID）、STB_ICLOUD_PASSWORD（App 专用密码）；STB_ICLOUD_URL（可省，默认 https://caldav.icloud.com）。
Joy 的全部日历都能查、都能加；加 / 删都要写 calendar（放哪个日历由任务的 system_prompt 定，平台不限）。只能查和新增，不能改、不能删别人的事件；
本工具新建的事件带 X-STB-CREATED 标记，只有这些才能被 modify-calendar_delete 删掉（用来撤销自己加错的）。
时间一律用本机时区（control_center.paths.host_tz）的 "YYYY-MM-DD HH:MM"；全天事件只写 "YYYY-MM-DD"。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import caldav
from icalendar import Alarm, Calendar, Event
from mcp.server.mcpserver import MCPServer

import sys
from control_center.credential import credential
from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center.paths import host_tz

TZ = ZoneInfo(host_tz())
MARK = "X-STB-CREATED"

server = MCPServer("modify-calendar", instructions=f"Joy 的 iCloud 日历；时间用 {TZ.key}（本机时区）的 YYYY-MM-DD HH:MM")


def _principal():
    from control_center import env

    return caldav.DAVClient(url=env.get("STB_ICLOUD_URL") or "https://caldav.icloud.com", username=credential("STB_ICLOUD_USER"),
                            password=credential("STB_ICLOUD_PASSWORD")).principal()


def _calendars() -> list:
    return list(_principal().calendars())


def _calendar(name: str):
    cals = _calendars()
    for cal in cals:
        if cal.get_display_name() == name:
            return cal
    raise ValueError(f"iCloud 里找不到日历 {name}（现有：{', '.join(c.get_display_name() for c in cals) or '无'}；可能被改名或删了，用问题卡问 Joy）")


def parse_when(s: str) -> date | datetime:
    s = s.strip()
    if len(s) == 10:
        return date.fromisoformat(s)
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def build_event(title: str, start: str, end: str = "", location: str = "", notes: str = "", alarm_minutes: int | None = None) -> tuple[bytes, str]:
    st = parse_when(start)
    if end:
        en = parse_when(end)
    else:
        en = st + (timedelta(days=1) if not isinstance(st, datetime) else timedelta(hours=1))
    if type(st) is not type(en):
        raise ValueError("start 和 end 要么都是全天（YYYY-MM-DD），要么都带时间")
    if en <= st:
        raise ValueError("end 必须晚于 start")
    uid = f"{uuid.uuid4()}@sparky"
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", title.strip())
    ev.add("dtstart", st)
    ev.add("dtend", en)
    ev.add("dtstamp", datetime.now(tz=TZ))
    if location:
        ev.add("location", location.strip())
    if notes:
        ev.add("description", notes.strip())
    ev.add(MARK, "1")
    if alarm_minutes is not None and alarm_minutes >= 0:
        a = Alarm()
        a.add("action", "DISPLAY")
        a.add("description", title.strip())
        a.add("trigger", timedelta(minutes=-int(alarm_minutes)))
        ev.add_component(a)
    cal = Calendar()
    cal.add("prodid", "-//sparky-toolbox//modify-calendar//ZH")
    cal.add("version", "2.0")
    cal.add_component(ev)
    return cal.to_ical(), uid


def _fmt(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    return v.isoformat() if isinstance(v, date) else str(v)


@server.tool(name="modify-calendar_list", description="查 Joy 的 iCloud 日历某段时间的事件：回答那几天有什么安排，加事件前查重、查冲突")
def cal_list(start: str, end: str = "", calendar: str = "") -> str:
    d0 = date.fromisoformat(start.strip())
    d1 = date.fromisoformat(end.strip()) if end.strip() else d0 + timedelta(days=7)
    t0, t1 = datetime(d0.year, d0.month, d0.day, tzinfo=TZ), datetime(d1.year, d1.month, d1.day, tzinfo=TZ)
    rows = []
    for cal in ([_calendar(calendar)] if calendar else _calendars()):
        name = cal.get_display_name()
        for obj in cal.search(start=t0, end=t1, event=True, expand=True):
            for ev in obj.icalendar_instance.walk("VEVENT"):
                rows.append((_fmt(ev.get("dtstart").dt), name, str(ev.get("summary", "")), _fmt(ev.get("dtend").dt) if ev.get("dtend") else "",
                             str(ev.get("location", "")), str(ev.get("uid", "")), bool(ev.get(MARK))))
    rows.sort()
    if not rows:
        return f"{d0} ~ {d1}（不含）没有事件"
    return "\n".join(f"{s} → {e} [{c}] {t}" + (f" @ {loc}" if loc else "") + (f"  (uid {u}, 本工具创建)" if mine else "") for s, c, t, e, loc, u, mine in rows)


@server.tool(name="modify-calendar_add", description="在 Joy 的 iCloud 日历里加一个事件")
def cal_add(title: str, start: str, calendar: str, end: str = "", location: str = "", notes: str = "", alarm_minutes: int = -1) -> str:
    ics, uid = build_event(title, start, end, location, notes, alarm_minutes if alarm_minutes >= 0 else None)
    _calendar(calendar).save_event(ics.decode())
    return f"已加到「{calendar}」：{start}{' → ' + end if end else ''} {title}（uid {uid}）"


@server.tool(name="modify-calendar_delete", description="删除本工具加的事件（撤销加错的）；别人建的删不了")
def cal_delete(uid: str, calendar: str) -> str:
    # iCloud 不支持按 UID 查询（412），在前后一段时间里按 UID 找
    now = datetime.now(tz=TZ)
    found = [o for o in _calendar(calendar).search(start=now - timedelta(days=90), end=now + timedelta(days=400), event=True)
             if any(str(c.get("uid")) == uid for c in o.icalendar_instance.walk("VEVENT"))]
    if not found:
        raise ValueError(f"「{calendar}」里找不到 uid {uid}（只搜前 90 天到后 400 天）")
    ev = found[0]
    comp = next(iter(ev.icalendar_instance.walk("VEVENT")))
    if not comp.get(MARK):
        raise ValueError("这个事件不是本工具创建的，不能删")
    ev.delete()
    return f"已从「{calendar}」删除 {comp.get('summary')}"


if __name__ == "__main__":
    server.run("stdio")
