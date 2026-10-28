"""image / video 子代理的 pre_call hook（子代理 yaml 的 hooks，以平台身份跑：两个都是 is_editable: false；
image、video 各有一份）：子代理的 pre_call 在换模型之前跑，这里先查输入图，不过就退出码 2 拦下
（stderr 的说明原样交给主模型），不白卸主模型、不白起 ComfyUI。
查的是参数里的输入图（INPUT_KEYS：image 的 images，video 的 first_frame / last_frame / references）：要是本次运行工作目录里已有的文件、
不越界（同程序的 media.host_path），是 png / jpg / webp / gif（media.IMAGE_EXT）。别的参数（取值范围、组合）由程序自己查。
输入：stdin 一段 JSON {event, task, run_id, run_dir, work_dir, subagent, function, args, …}。"""
import json
import os
import sys
from pathlib import Path

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center import media

INPUT_KEYS = ("images", "first_frame", "last_frame", "references")


def problems(args: dict, work: Path) -> list[str]:
    """参数里不合格的输入图，每张一句说明。"""
    os.environ["STB_WORK_ROOT"] = str(work)          # media.host_path 按它查路径在不在工作目录里
    out = []
    for key in INPUT_KEYS:
        v = args.get(key) or []
        for p in v if isinstance(v, list) else [v]:
            try:
                host = media.host_path(str(p))
            except media.MediaError as e:
                out.append(f"{key}：{e}")
                continue
            if not host.is_file():
                out.append(f"{key} 要写工作目录下已有文件的绝对路径，这个不存在：{p}")
            elif host.suffix.lower() not in media.IMAGE_EXT:
                out.append(f"{key} 只收 png / jpg / webp / gif 图片：{p}")
    return out


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    if not req.get("work_dir") or not isinstance(req.get("args"), dict):
        return
    if bad := problems(req["args"], Path(req["work_dir"])):
        print("输入图不对，这次没有换模型、没有生成：\n" + "\n".join(bad), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
