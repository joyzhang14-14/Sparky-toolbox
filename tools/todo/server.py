"""todo 工具：任务的待办清单（control_center/todo.py：任务工作目录 todo/<时间>.md）。建清单、加项、打勾（写依据）、放弃（写原因）；
每次调用都返回还没清掉的全部项。任务是谁从网关给的 STB_RUN_REF（<任务>/<运行 id>）取。平台另外在运行开头、结束时按清单提醒（runner）。"""
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from control_center import todo
from control_center import tool_kit  # noqa: F401 — 装上程序错误标记

server = MCPServer("todo", instructions="待办清单：把要做、答应要做的事记下来，做完打勾写依据，不做了写原因")


def _ref() -> tuple[str, str]:
    ref = os.environ.get("STB_RUN_REF", "")
    if "/" not in ref:
        raise ToolError("不在任务运行里，没有待办清单")
    return ref.split("/", 1)[0], ref


def _do(fn, *args) -> Any:
    try:
        return fn(*args)
    except ValueError as e:
        raise ToolError(str(e)) from None


@server.tool(description="新建一份待办清单：items 每项一件要做的事（≤20 项）；返回带编号的清单")
def todo_create(items: list[str]) -> str:
    task, ref = _ref()
    f = _do(todo.create, task, ref, items)
    return f"建好了 todo/{f.name}\n{todo.status(task)}"


@server.tool(description="往最新一份没清掉的清单里加几项（没有就新建一份）")
def todo_add(items: list[str]) -> str:
    task, ref = _ref()
    f = _do(todo.add, task, ref, items)
    return f"加进了 todo/{f.name}\n{todo.status(task)}"


@server.tool(description="打勾：item 写编号或原文；evidence 一句依据（工具结果里看得到的：测出的数、文件路径…）")
def todo_done(item: str, evidence: str) -> str:
    task, _ = _ref()
    it = _do(todo.mark, task, item, True, evidence)
    return f"第 {it['n']} 项打勾了\n{todo.status(task)}"


@server.tool(description="不做了：item 写编号或原文；reason 一句原因（会告诉 Joy）")
def todo_drop(item: str, reason: str) -> str:
    task, _ = _ref()
    it = _do(todo.mark, task, item, False, reason)
    return f"第 {it['n']} 项不做了\n{todo.status(task)}"


if __name__ == "__main__":
    server.run("stdio")
