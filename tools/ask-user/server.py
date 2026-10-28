"""ask-user 工具：只做校验（control_center/acquire_permission.py 的 validate），返回规整后的问题 JSON；发卡、等回答、挂起与续跑都由网关和运行器做（D35）。
没有凭证、不对外发送；任何任务在 tools 里列了就能用。搭建需求的题库（参考清单）在同文件夹的 guide.md。"""
import json
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center import acquire_permission  # noqa: E402

server = MCPServer("ask-user", instructions="向 Joy 提问")


class Question(BaseModel):
    question: str = Field(description=f"完整的问句，{acquire_permission.MAX_QUESTION} 字以内")
    type: Literal["single", "multi", "fill"] = Field(description="single：单选；multi：可多选；fill：填空（一个文本框）")
    options: list[str] = Field([], description=f"single / multi 写 1–{acquire_permission.MAX_OPTIONS} 个选项，每个 {acquire_permission.MAX_LABEL} 字以内；不要写「其他」（程序会自动加）。fill 不写")
    default: str = Field("", description="只给 fill：你猜的答案；猜不出就不写")
    hint: str = Field("", description=f"只给 fill：一句填写提示（{acquire_permission.MAX_HINT} 字以内），可不写")


@server.tool(description=(
    "向 Joy 提问。请求不清楚、或有几种合理做法需要他拍板时，先问再做，不要猜；简单明确的事直接做，不要为问而问。"
    f"相关的问题放进同一次调用（一轮 1–{acquire_permission.MAX_QUESTIONS} 题），一次运行可以问多轮。why 写一句为什么问"))
def ask_user(questions: list[Question], notes: bool = False, why: str = "") -> str:
    try:
        why = " ".join(why.split())
        if len(why) > acquire_permission.MAX_QUESTION:
            raise ValueError(f"why 最多 {acquire_permission.MAX_QUESTION} 字")
        return json.dumps({"questions": acquire_permission.validate([q.model_dump() for q in questions], notes=notes), "why": why}, ensure_ascii=False)
    except ValueError as e:
        raise ToolError(str(e)) from None


if __name__ == "__main__":
    server.run("stdio")
