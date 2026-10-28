"""沙箱共用的安装环境（手册 §4.7 的安装）：micromamba 建在 ENV（data/tool_state/download/env/，sparky 拥有，宿主上以 sparky 装），
沙箱里按原路径只读可见、PATH 把它的 bin/ 放最前（control_center/sandbox.py），装好后正在跑的沙箱马上能用。
download 工具的 download_install（批准后的后台程序 tools/download/install_watch.py）和 skills 工具导入时装依赖都调 install：
每个名字先从 conda-forge 装、装不上再用环境里的 pip，逐个装（一个装不上不连累别的）；同一时刻只有一个在装（文件锁）。"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess

from control_center.paths import PLATFORM_HOME, TOOL_STATE

ENV = TOOL_STATE / "download" / "env"
MAMBA = PLATFORM_HOME / "apps" / "micromamba" / "bin" / "micromamba"
PYTHON = "3.12"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[<>=!~]=?[A-Za-z0-9.*]+)?")   # 包名，可带一个版本条件（如 pypdf>=4）
TIMEOUT = 1200


def check(pkgs: list[str]) -> list[str]:
    """包名去空白、去重；有不像包名的报 ValueError。"""
    out = list(dict.fromkeys(str(p).strip() for p in pkgs if str(p).strip()))
    if bad := [p for p in out if not NAME_RE.fullmatch(p)]:
        raise ValueError(f"不像包名：{'、'.join(bad[:5])}（只写名字，可带一个版本条件，如 poppler、pypdf>=4）")
    if not out:
        raise ValueError("packages 至少写一个包名，如 [\"poppler\", \"pdfplumber\"]")
    return out


def _run(argv: list[str]) -> bool:
    env = {**os.environ, "MAMBA_ROOT_PREFIX": str(ENV.parent / "mamba"), "PIP_NO_CACHE_DIR": "1"}
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, env=env).returncode == 0


def install(pkgs: list[str]) -> tuple[list[str], list[str]]:
    """装进 ENV（还没有就先建，带 python 与 pip），返回 (装上的, 装不上的)。环境本身建不起来报 RuntimeError。"""
    ENV.parent.mkdir(parents=True, exist_ok=True)
    with open(ENV.parent / "env.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (ENV / "bin" / "python").exists() and not _run([str(MAMBA), "create", "-y", "-q", "-p", str(ENV), "-c", "conda-forge", f"python={PYTHON}", "pip"]):
            raise RuntimeError(f"建不起沙箱的安装环境 {ENV}（micromamba：{MAMBA}）")
        done, failed = [], []
        for p in pkgs:
            ok = _run([str(MAMBA), "install", "-y", "-q", "-p", str(ENV), "-c", "conda-forge", p]) or _run([str(ENV / "bin" / "pip"), "install", "-q", p])
            (done if ok else failed).append(p)
        return done, failed
