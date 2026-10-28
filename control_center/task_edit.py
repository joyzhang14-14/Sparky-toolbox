"""改任务 yaml 的唯一入口：网页（channels/web）与 stb 命令（stb task enable / disable）都调这里。
旋钮只改顶层标量（保留注释与顺序），写入前完整校验（同 stb task check，.env 的值不能写死），写完在 main 上提交这一个文件（定时触发、监听由 stbcc-hub 现读 yaml）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from control_center import env
from control_center.models import load_models
from control_center.paths import REPO, TASKS, task_file
from control_center.task import TaskError, load_task

KNOBS = ("enabled", "priority", "timeout", "schedule", "resumable", "runtime", "model")


def commit_task(path: Path, message: str) -> bool:
    """网页 / stb task enable 改完的任务 yaml 立刻在 main 上提交（只提交这一个文件）：main 不留未提交改动，builder 部署才合得进。
    不在 git 仓库、不在 main、内容没变都不提交；提交失败不影响保存。"""
    def git(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(path.parent), *a], capture_output=True, text=True)

    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main" or not git("status", "--porcelain", "--", path.name).stdout.strip():
        return False
    return git("-c", "user.name=stb-web", "-c", "user.email=web@sparky", "commit", "-q", "-m", message, "--", path.name).returncode == 0


def save_task(name: str, yaml_text: str | None, knobs: dict[str, Any] | None, what: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        raise TaskError("任务名不合法")
    src = task_file(name)
    if not src.exists():
        raise TaskError("任务未登记")
    text = yaml_text if yaml_text is not None else src.read_text()
    for k, v in (knobs or {}).items():
        if k not in KNOBS:
            raise TaskError(f"不支持的旋钮 {k}")
        text = _set_top(text, k, v)
    try:
        env.check_no_values(text, f"tasks/{src.relative_to(TASKS)}")   # .env 的值不能写死在任务里
    except env.EnvError as e:
        raise TaskError(str(e)) from None
    with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False, dir=str(src.parent)) as tmp:   # 不用 .yaml 后缀：不被当成任务定义
        tmp.write(text)
    try:
        task = load_task(Path(tmp.name), load_models(), folder=src.parent)
        if task.name != name:
            raise TaskError("不能在这里改 name")
    finally:
        os.unlink(tmp.name)
    src.write_text(text)
    mirrored = False
    repo_copy = REPO / "tasks" / src.relative_to(TASKS)
    if repo_copy.exists():
        repo_copy.write_text(text)
        mirrored = True
    what = what or ("、".join(f"{k} → {v}" for k, v in (knobs or {}).items()) or "改 yaml")
    committed = commit_task(repo_copy if mirrored else src, f"chore(tasks): 网页修改 {name}：{what}")
    return {"ok": True, "mirrored_to_repo": mirrored, "committed": committed}


def _set_top(text: str, key: str, value: Any) -> str:
    """改顶层标量键，保留注释与顺序；不存在就追加在第一段（name/description/timeout…）后。"""
    if value is None or value == "":
        return re.sub(rf"^{key}:.*\n?", "", text, count=1, flags=re.M)
    v = (json.dumps(value, ensure_ascii=False) if isinstance(value, list) or (isinstance(value, str) and re.search(r"[\s:#*]", value))   # 列表写成一行（如 schedule 的几条 cron）
         else str(value).lower() if isinstance(value, bool) else str(value))
    if blk := re.search(rf"^{key}:[ \t]*(?:#.*)?\n(?:[ \t]+.*\n)+", text, flags=re.M):   # 映射写法（如 enabled: {schedule, listen} 分行）：连子行一起换成标量
        return text[:blk.start()] + f"{key}: {v}\n" + text[blk.end():]
    if re.search(rf"^{key}:", text, flags=re.M):
        return re.sub(rf"^({key}:)[ \t]*[^#\n]*?([ \t]*(?:#.*)?)$", lambda m: f"{m.group(1)} {v}{m.group(2)}", text, count=1, flags=re.M)
    return re.sub(r"^(timeout:.*\n)", rf"\g<1>{key}: {v}\n", text, count=1, flags=re.M) if re.search(r"^timeout:", text, flags=re.M) else text + f"\n{key}: {v}\n"
