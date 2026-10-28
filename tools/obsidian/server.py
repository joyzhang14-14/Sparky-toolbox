"""obsidian 工具：读写 Joy 的整个 Obsidian 笔记库（~/Obsidian，Syncthing 同步到 Mac），以 sparky 运行（sparky 对整库可读可写）。
只认 .md，跳过 .obsidian、.git、.trash 等 . 开头的目录；路径用库内相对路径，越出库外的一律拒绝。
读：find 按关键词找文档路径、info 看一篇的字符数等信息、read 按给定字符数读开头。
写：write 整篇替换写一篇 .md（新建或修改，不提供删除）。UCI/ 下直接写；别处发平台核心的通用审批卡（tool_kit.ask_permission，经 stbcc-hub），
就地等 Joy 处理（最多 tool_kit.APPROVAL_WAIT_S，运行要暂停就提前停）：同意 → 本进程自己写（写前再核对文件没被改过）；拒绝 → 他的意见交回模型；
没处理 → 不写，网关随后让出运行（waiting），Joy 之后同意了，平台以同样的参数替这次调用再调用一次、写入（tool_kit.ask_permission）。
只能写 / 改哪些笔记由任务的 system_prompt 定。规则见同文件夹的 obsidian_write.py。"""
import json
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from control_center import tool_kit
import _vault
import obsidian_write as obsidian   # 同文件夹：写入规则
from _files import replace_text

ROOT = _vault.ROOT

server = MCPServer("obsidian", instructions="Joy 的 Obsidian 笔记库；路径用库内相对路径；改 UCI/ 以外的地方要 Joy 审批")


@server.tool(name="obsidian_find", description="在 Joy 的 Obsidian 笔记库里按关键词（文件名或正文）找笔记，返回库内相对路径；查笔记从这里开始。"
              "UCI 待办清单的加 / 勾用 todo-doc_*，记忆用 memory_*")
def find(keyword: str) -> str:
    return json.dumps(_vault.find(ROOT, keyword), ensure_ascii=False)


@server.tool(name="obsidian_info", description="看一篇笔记的标题、字符数 chars、时间、frontmatter 和标题列表：读正文前先看它")
def info(path: str) -> CallToolResult:
    return tool_kit.result(json.dumps(_vault.info(ROOT, path), ensure_ascii=False), source=path)   # 来源：隔离读者引用它时主模型看到的出处


@server.tool(name="obsidian_read", description="读一篇笔记从 start 起的 chars 个字符（start 默认 0）")
def read(path: str, chars: int, start: int = 0) -> str:
    text, total = _vault.read(ROOT, path, chars, start)
    return tool_kit.result(json.dumps({"path": path, "total": total, "start": start, "returned": len(text), "text": text}, ensure_ascii=False), source=path)


async def _approved(rel: str, old: str | None, content: str, reason: str) -> bool | str | None:
    """通用审批卡（tool_kit.ask_permission）：同意 True、拒绝返回 Joy 写的话（str，没写是 ""）、没处理 None；卡发不出去抛 ValueError。
    同一次运行里 Joy 已经同意过同一处改动（同一 key：路径、写之前与写之后的内容都一样）的卡：经中枢 card_use 用掉，直接放行。"""
    key, action, detail = obsidian.change(rel, old, content)
    got, value = await tool_kit.ask_permission("obsidian", action, key, f"原因：{' '.join(reason.split())[:300]}\n{detail}")
    if got == "failed":
        raise ValueError(f"UCI/ 以外的笔记要 Joy 审批，审批卡没发出去：{value}")
    return True if got == "approve" else None if got == "pending" else str(value or "")


@server.tool(name="obsidian_write", description="整篇写一篇笔记（新建或覆盖）：只在 Joy 要你写 / 改笔记时用；UCI/ 以外要 Joy 同意才写；UCI 待办清单的加 / 勾用 todo-doc_add / todo-doc_check，不整篇改")
async def write(path: str, content: str, reason: str) -> str:
    p, rel = obsidian.target(ROOT, path)
    obsidian.check_content(content)
    if obsidian.free(rel):
        p.parent.mkdir(parents=True, exist_ok=True)
        replace_text(p, content)
        return f"已写入 {rel}（{len(content)} 字）"
    old = obsidian.read(p)
    if old == content:
        raise ValueError(f"{rel} 的内容和现在一样，不用改")
    ok = await _approved(rel, old, content, reason)
    if ok is True:
        if obsidian.read(p) != old:
            return f"没写：{rel} 在发卡之后被改过，重新读一遍再提议"
        p.parent.mkdir(parents=True, exist_ok=True)
        replace_text(p, content)
        return f"Joy 同意了，已写入 {rel}（{len(content)} 字）"
    if ok is None:
        return f"已发审批卡给 Joy，他还没处理，{rel} 还没写；不要改内容重复提议"
    return f"Joy 拒绝了，没写 {rel}；他说：「{ok[:1000]}」，照他说的改完再提议" if ok else f"Joy 拒绝了，没写 {rel}；不要再提议这处改动"


if __name__ == "__main__":
    server.run("stdio")
