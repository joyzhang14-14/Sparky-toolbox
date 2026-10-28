"""子代理 builder 的 post_call hook（以平台身份跑：is_editable: false）：这一轮 builder 调用结束（成功、失败、被取消），
请 stbcc-builder 关掉这次运行里 builder_try_tool 留着的试跑工具进程（control_center/builder 的 end_trials）。关不掉只写 stderr，不影响调用结果。"""
import json
import sys

from control_center.builder import service


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    r = service.request("end_trials", f"{data.get('task')}/{data.get('run_id')}")
    if not r.get("ok"):
        print(f"关试跑进程没成：{r.get('error')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
