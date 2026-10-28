"""Obsidian 笔记库的读（obsidian 工具用；写在同文件夹的 obsidian_write.py）。
整库（~/Obsidian，ROOT）只认 .md 文档，跳过 . 开头的目录与文件（.obsidian、.git、.trash）；路径一律用库内相对路径，
解析 `..` 与符号链接之后必须仍在库内，否则拒绝。"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_FIND = 50
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


ROOT = Path("~/Obsidian").expanduser().resolve()   # Joy 的笔记库（程序写死）


def resolve(root: Path, rel: str) -> Path:
    """库内相对路径 → 宿主上的 .md 文件；越出库、. 开头的目录或文件、不是 .md、不存在都抛 ValueError。"""
    p = (root / str(rel).strip().strip("/")).resolve()
    if root not in p.parents:
        raise ValueError(f"路径 {rel} 不在笔记库内")
    if any(x.startswith(".") for x in p.relative_to(root).parts):
        raise ValueError(f"不读 . 开头的目录或文件：{rel}")
    if p.suffix.lower() != ".md" or not p.is_file():
        raise ValueError(f"没有这篇 .md 文档：{rel}")
    return p


def docs(root: Path):
    """库里所有 .md（库内相对路径, 宿主路径），按路径排序；. 开头的目录不进，符号链接指到库外的跳过。"""
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.startswith(".") or not f.lower().endswith(".md"):
                continue
            p = Path(d) / f
            real = p.resolve()
            if root in real.parents:
                yield str(p.relative_to(root)), real


def find(root: Path, keyword: str) -> dict[str, Any]:
    """文件名（含路径）或正文里含 keyword（不分大小写）的文档路径，最多 MAX_FIND 条。"""
    k = keyword.strip().lower()
    if not k:
        raise ValueError("keyword 不能为空")
    hits: list[str] = []
    truncated = False
    for rel, p in docs(root):
        try:
            hit = k in rel.lower() or k in p.read_text(errors="replace").lower()
        except OSError:
            continue
        if hit:
            if len(hits) >= MAX_FIND:
                truncated = True
                break
            hits.append(rel)
    return {"paths": hits, "truncated": truncated, "note": f"只列前 {MAX_FIND} 篇，换更具体的关键词" if truncated else ""}


def _birth(p: Path) -> int | None:
    """文件创建时间（ext4 的 birth time，stat %W）；文件系统不记就是 None。"""
    try:
        v = int(subprocess.run(["stat", "-c", "%W", "--", str(p)], capture_output=True, text=True, timeout=10).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return v or None


def info(root: Path, rel: str) -> dict[str, Any]:
    p = resolve(root, rel)
    text = p.read_text(errors="replace")
    front, body = "", text
    if m := re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S):
        front, body = m.group(1), text[m.end():]
    heads = [(len(h.group(1)), h.group(2)) for line in body.splitlines() if (h := HEADING.match(line))]
    title = next((t for n, t in heads if n == 1), p.stem)
    st = p.stat()
    fmt = lambda t: None if t is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    return {"path": str(p.relative_to(root)), "title": title, "chars": len(text), "created": fmt(_birth(p)), "modified": fmt(st.st_mtime),
            "frontmatter": front[:4000], "headings": [("#" * n) + " " + t for n, t in heads[:100]]}


def read(root: Path, rel: str, chars: int | None = None, start: int = 0) -> tuple[str, int]:
    """从 start 起的 chars 个字符（None 读到结尾）；返回 (文本, 全文字符数)。"""
    text = resolve(root, rel).read_text(errors="replace")
    start = max(0, int(start))
    return (text[start:] if chars is None else text[start: start + max(1, int(chars))]), len(text)
