"""systemd 用户单元的操作（systemctl --user）。平台服务的单元文件在 services/systemd/；定时触发由 stbcc-hub 做（control_center/schedule.py），任务没有单元。"""
from __future__ import annotations

import subprocess
from pathlib import Path

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)
