"""安装脚本（约定文件 install.py）：builder 部署改动了本文件夹的计划时，合入之前由平台跑一次（is_editable: false 以平台身份跑）。
装 comfy_nodes.yaml 列的、ComfyUI 里还没有的自定义节点，重启 ComfyUI 试跑（同文件夹 _comfy.py 的 install_nodes）；不过就自己撤回、退出码 1（不合入）。
模型档位（ComfyUI 的地址、安装目录、启停命令）在环境变量 STB_MODEL 里，同运行时。"""
import sys
from pathlib import Path

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
import _comfy

if __name__ == "__main__":                 # 被 import 时不装
    try:
        print(_comfy.install_nodes(Path(__file__).resolve().parent))
    except _comfy.InstallError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
