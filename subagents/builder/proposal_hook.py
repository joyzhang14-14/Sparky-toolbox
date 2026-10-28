"""子代理 builder 的 pre_call hook（以平台身份跑：is_editable: false）：主模型这次调用的 prompt 是提案（plan: 开发计划 / oneoff: 一次性任务）。
校验（control_center/builder review_proposal）→ 发平台的通用审批卡并就地等 Joy（tool_kit.ask_permission，最多 5 分钟；同一提案已批准过就用掉那张卡直接过）→
批准：开发计划建好开发区后退出 0（放行，子代理开始开发）；一次性任务建好入队，退出 2 把结果交回主模型。
不合格、拒绝、没等到、卡没发出去：退出 2，stderr 交回主模型（没等到时网关接着就地等这张卡，等不到就让出运行；Joy 同意时平台带着 approved_card
再跑一遍本 hook：建好开发区放行，续跑时由运行器替主模型调用 builder，见 control_center/acquire_permission.py 的批了就执行）。"""
import asyncio
import json
import os
import sys
from pathlib import Path

from control_center import tool_kit
from control_center import builder  # noqa: E402


async def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    run = f"{data.get('task')}/{data.get('run_id')}"
    os.environ["STB_RUN_REF"] = run                                   # ask_permission 按它发卡、card_use
    if data.get("approved_card"):                                     # 平台替 Joy 已同意的这次调用重跑：ask_permission 只认这张卡
        os.environ["STB_APPROVED_CARD"] = str(data["approved_card"])
    if data.get("run_dir"):
        os.environ["STB_PAUSE_FILE"] = str(Path(data["run_dir"]) / "signal" / "pause")
    try:
        r = await asyncio.to_thread(builder.review_proposal, str((data.get("args") or {}).get("prompt") or ""), run)
    except builder.BuilderError as e:
        print(builder.explain(e), file=sys.stderr)
        return 2
    if r.get("ready"):
        return 0                                                       # 同一份计划已批准、开发区就绪：接着开发
    got, value = await tool_kit.ask_permission("builder", r["action"], r["key"], r["detail"])
    if got == "approve":
        ok, text = await asyncio.to_thread(builder.accept, r, run)
        if ok:
            return 0
        print(text, file=sys.stderr)
    elif got == "reject":
        print(builder.rejected_text(value), file=sys.stderr)
    elif got == "pending":
        print("审批卡已发给 Joy，他还没处理", file=sys.stderr)
    else:
        print(f"审批卡没发出去：{value}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
