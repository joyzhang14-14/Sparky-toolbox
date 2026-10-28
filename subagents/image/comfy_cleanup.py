"""image / video 子代理的 post_call hook（子代理 yaml 的 hooks，以平台身份跑：两个子代理都是 is_editable: false；
image、video 各有一份）：子代理的程序经 ComfyUI 接口留下的输入图与成品副本（~/apps/ComfyUI/input/stb/<运行 id>/、~/apps/ComfyUI/output/stb/<运行 id>/）sparky 删不了，
每次调用结束后（成功失败都）在这里删。同一运行还有这个子代理的别的调用在途（输入的 inflight > 0）就先不删，等最后一个结束再删。
运行 id 带路径字符、或解析符号链接后不在这两个目录之下（不含它本身）的不删。"""
import json
import re
import shutil
import sys
from pathlib import Path

ROOTS = ("~/apps/ComfyUI/input/stb", "~/apps/ComfyUI/output/stb")


def targets(run_id: str) -> list[Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        return []
    out = []
    for r in ROOTS:
        root = Path(r).expanduser().resolve()
        p = (root / run_id).resolve()
        if root in p.parents:
            out.append(p)
    return out


def main() -> None:
    data = json.loads(sys.stdin.read() or "{}")
    if int(data.get("inflight") or 0) > 0:
        return
    for p in targets(str(data.get("run_id") or "")):
        try:
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
