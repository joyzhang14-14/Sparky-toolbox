"""Obsidian 笔记库的写入规则（obsidian 工具；2026-10-03 Joy 定：整个库对 sparky 开放，改免审区域以外的地方要审批）。工具服务器用这里的路径规则与卡面内容。

- 免审区域：UCI/ 下直接写（工具以 sparky 跑，整篇替换写，同文件夹 _files.py 的 replace_text）。
- USER/memory.md、USER/USER.md 有专用工具（memory_edit 直接改、memory_user_edit 发卡），这里拒并指过去：它们负责格式、上限、锁与备份。
- 其他地方：发平台核心的通用审批卡（acquire_permission.PERMISSION_KIND，卡面由中枢按 payload 生成：路径、新建 / 修改、diff 摘要、原因），
  Joy 同意后由工具自己（sparky，~/Obsidian 组可写）整篇替换写；拒绝可写意见，原文返回给模型。
  发卡之后文件又变了（Joy 在 Mac 上改过）就不写，让模型重读再提议（卡的 key 带写之前的内容指纹，对不上就不算同一件事）。
- 禁写：库外、. 开头的目录或文件（.obsidian、.stfolder、.stversions、.stignore、.trash、.git…）、不是 .md。不提供删除。
"""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any

FREE = ("UCI",)                      # 免审区域（库内顶层目录）
DEDICATED = {"USER/memory.md": "memory_edit（memory 工具，直接生效）", "USER/USER.md": "memory_user_edit（memory 工具，发审批卡）"}
MAX_CHARS = 200_000
SHOWN = 1300                         # 卡上 diff 最多显示多少字（通用审批卡的细节上限是 acquire_permission.MAX_DETAIL）


def target(root: Path, rel: str) -> tuple[Path, str]:
    """库内相对路径 → (宿主路径, 规范化的库内相对路径)。解析 .. 与符号链接后还要在库内；禁写的、交专用工具的抛 ValueError。文件可以还不存在（新建）。"""
    root = Path(root).resolve()
    p = (root / str(rel).strip().strip("/")).resolve()
    if root not in p.parents:
        raise ValueError(f"路径 {rel} 不在笔记库内；改法：写库内的相对路径，如 UCI/课程.md，不要写绝对路径或 ..")
    r = p.relative_to(root).as_posix()
    if any(x.startswith(".") for x in r.split("/")):
        raise ValueError(f"不写 . 开头的目录或文件（.obsidian、.stfolder、.stversions、.stignore、.trash 等）：{rel}")
    if p.suffix.lower() != ".md" or (p.exists() and not p.is_file()):
        raise ValueError(f"只写 .md 文档：{rel}")
    if r in DEDICATED:
        raise ValueError(f"{r} 用 {DEDICATED[r]} 改，不用这里")
    return p, r


def free(rel: str) -> bool:
    """免审区域（UCI/ 下）：直接写。"""
    return "/" in rel and rel.split("/", 1)[0] in FREE


def check_content(content: Any) -> str:
    if not isinstance(content, str):
        raise ValueError("content 要是整篇文本")
    if len(content) > MAX_CHARS:
        raise ValueError(f"太长（{len(content)} 字，上限 {MAX_CHARS}）")
    return content


def _read(p: Path) -> str | None:
    return p.read_text(errors="replace") if p.is_file() else None


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def change(rel: str, old: str | None, content: str) -> tuple[str, str, str]:
    """一处改动 → (key, action, detail)：key 是「同一件事」的指纹（路径 + 写之前的内容 + 写之后的内容）；action 一句话（新建 / 修改、行数、字数）；
    detail 是 diff（卡上只显示前面一段，见 acquire_permission.MAX_DETAIL）。"""
    diff = list(difflib.unified_diff((old or "").splitlines(), content.splitlines(), lineterm="", n=1))[2:]
    plus, minus = sum(l.startswith("+") for l in diff), sum(l.startswith("-") for l in diff)
    action = f"{'新建' if old is None else '修改'} Obsidian 笔记 {rel}（+{plus} / -{minus} 行，写入后 {len(content)} 字）"
    shown = "\n".join(diff).replace("```", "ˋˋˋ")
    if len(shown) > SHOWN:
        shown = shown[:SHOWN] + "\n…（改动太长，只显示前面）"
    return _sha(rel + chr(0) + (old or "") + chr(0) + content) or "", action, f"```diff\n{shown}\n```"


def read(p: Path) -> str | None:
    return _read(p)
