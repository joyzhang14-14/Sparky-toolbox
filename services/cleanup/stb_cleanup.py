"""每天清一次过期的运行数据（普通脚本 + systemd 用户定时器 stbcc-cleanup.timer，不是 stb 任务：固定步骤、不占模型）。

保留期（手册 §2 的保留期表）：
- logs/runs/<任务>/<运行>/：run.json 最后一次更新超过 90 天的删掉；还没结束的（running / paused / waiting / partial）不管多久都留着
- data/hub/inbox/<任务>/：私信发来的要隔离读的附件原件，超过 30 天删
- data/hub/approvals/：已处理的审批卡 / 问题卡（resolved 时刻）超过 30 天删；没处理的一律留着
- 一次性监听（data/hub/monitors/<id>.json 与 data/listen/monitor.<id>/）：已结束（状态不在 monitors.ACTIVE）、登记文件最后一次更新超过 30 天的删；进行中的留着
- <workspace>/<任务>/media/<运行 id>/：生图、视频成品目录超过 90 天没动过删（生成时 _comfy.prune_media 也按同一条清）
- builder 计划的中间产物（data/hub/builder/<计划>/ 的 wheelhouse、check 等，data/hub/builder-trial/<计划>/，/home/sparky/{dev,stage}/<计划>/）：
  计划结束时平台就删（builder.end_plan）；结束超过 7 天还残留的在这里兜底删（builder.clean_plan，sparky 侧以 sparky 删）
- data/tool_state/owa/artifacts/<研究>/：研究证据目录超过保留期（config/stbcc.yaml 的 launcher.artifact_ttl_seconds）没动过删
  （owa-launcher 每分钟也按同一条清；这里兜底）
一项删不了（如权限不够）记一行、接着删别的。
共享数据 data/messages/（对话库、任务消息库）一直留着；工作区其他内容不自动清。补跑只看最近一个原定时刻之后的 run.json，不受 90 天影响。
用法：stb_cleanup.py [--dry-run]（只列出会删什么，不动手）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control_center.paths import APPROVALS, HUB, INBOX, LISTEN, OWA_HOME, RUNS, WORK  # noqa: E402

DAY = 86400
RUN_DAYS, INBOX_DAYS, CARD_DAYS, MEDIA_DAYS, PLAN_DAYS, MONITOR_DAYS = 90, 30, 30, 90, 7, 30
BUILDER_KIND = "builder 中间产物"


def artifact_ttl() -> int:
    """研究证据的保留期（秒）：config/stbcc.yaml 的 launcher.artifact_ttl_seconds（owa-launcher 读同一项），没写用 3600（同 owa-launcher）。"""
    from control_center.paths import settings

    try:
        return int((settings().get("launcher") or {}).get("artifact_ttl_seconds", 3600))
    except (OSError, ValueError, TypeError, AttributeError):
        return 3600


def ended_plans(now: float) -> list[str]:
    """结束（builder.ENDED）超过 PLAN_DAYS 天的开发计划 id（一次性任务的计划不算）。"""
    from control_center import builder

    out = []
    for f in sorted((HUB / "builder" / "plans").glob("*.json")):
        try:
            p = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        at = max((float(h.get("at", 0)) for h in p.get("history") or [] if h.get("status") == p.get("status")), default=f.stat().st_mtime)
        if p.get("kind") != "oneoff" and p.get("status") in builder.ENDED and at < now - PLAN_DAYS * DAY:
            out.append(f.stem)
    return out
UNFINISHED = ("running", "paused", "waiting", "partial")


def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
    except OSError:                     # 读不了的（如 sparky 的目录里没权限的子目录）不计大小
        return 0


def candidates(now: float | None = None) -> list[tuple[str, Path]]:
    """到期的东西：[(类别, 路径)]。"""
    now = now or time.time()
    out: list[tuple[str, Path]] = []
    for rj in sorted(RUNS.glob("*/*/run.json")):
        try:
            status = json.loads(rj.read_text()).get("status")
        except (OSError, ValueError):
            continue
        if status not in UNFINISHED and rj.stat().st_mtime < now - RUN_DAYS * DAY:
            out.append(("运行记录", rj.parent))
    for f in sorted(INBOX.glob("*/*")):
        if f.is_file() and f.stat().st_mtime < now - INBOX_DAYS * DAY:
            out.append(("私信附件", f))
    for f in sorted(APPROVALS.glob("*.json")):
        try:
            done = (json.loads(f.read_text()).get("resolved") or {}).get("at")
        except (OSError, ValueError):
            continue
        if done and float(done) < now - CARD_DAYS * DAY:
            out.append(("审批卡", f))
    from control_center import monitors

    for f in sorted((HUB / "monitors").glob("*.json")):
        try:
            status = json.loads(f.read_text()).get("status")
        except (OSError, ValueError):
            continue
        if status not in monitors.ACTIVE and f.stat().st_mtime < now - MONITOR_DAYS * DAY:
            out.append(("一次性监听", f))
            if (LISTEN / f"monitor.{f.stem}").is_dir():
                out.append(("一次性监听", LISTEN / f"monitor.{f.stem}"))
    for d in sorted(WORK.glob("*/media/*")):
        if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < now - MEDIA_DAYS * DAY:
            out.append(("媒体成品", d))
    from control_center import builder

    for pid in ended_plans(now):
        plat, sparky = builder.plan_leftovers(pid)
        out += [(BUILDER_KIND, p) for p in (*plat, *sparky)]
    ttl = artifact_ttl()
    for d in sorted((OWA_HOME / "artifacts").glob("*")):
        if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < now - ttl:
            out.append(("研究证据", d))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="清理过期的运行记录、私信附件、已处理的审批卡、媒体成品")
    ap.add_argument("--dry-run", action="store_true", help="只列出会删什么")
    a = ap.parse_args(argv)
    from control_center import builder

    found = candidates()
    total: dict[str, list[int]] = {}
    failed = []
    for kind, p in found:
        n = _size(p)
        total.setdefault(kind, [0, 0])
        total[kind][0] += 1
        total[kind][1] += n
        if a.dry_run:
            print(f"会删 {kind}：{p}（{n / 1024 ** 2:.1f} MiB）")
        elif kind != BUILDER_KIND:                      # builder 的按计划整个删（下面），sparky 侧的要以 sparky 删
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError as e:
                failed.append(f"{p}（{type(e).__name__}: {e}）")
    if not a.dry_run:
        for pid in sorted({p.name if p.parent.name in ("builder-trial", "dev", "stage") else p.parent.name for k, p in found if k == BUILDER_KIND}):
            failed += builder.clean_plan(pid)
    summary = "；".join(f"{k} {c} 项 {b / 1024 ** 2:.1f} MiB" for k, (c, b) in total.items())
    print(f"{'会删' if a.dry_run else '已删'}：{summary}" if summary else "没有到期的东西")
    for f in failed:
        print(f"删不了：{f}")


if __name__ == "__main__":
    main()
