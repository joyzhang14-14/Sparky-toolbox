"""todo-doc 写文件的写法（整篇换掉）。"""
from __future__ import annotations

import os
from pathlib import Path


def replace_text(p: Path, text: str) -> None:
    """Syncthing 同步目录里的文档（Obsidian 的 USER/、UCI/）整篇换掉：同目录写临时文件，再 os.replace。
    不原地写：Syncthing 同步时会换掉文件，换来的文件不一定带给 sparky 的写权限；换文件只要目录的写权限（目录组可写）。
    临时文件设成 0664：组（sparkle）可写，sparky 与平台用户（joyzhang14）谁写的另一个都能改。"""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.stb-tmp")
    try:
        tmp.write_text(text)
        tmp.chmod(0o664)
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
