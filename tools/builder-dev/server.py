"""builder-dev 工具：子代理 builder 的开发区操作（只给子代理用，usable_by: subagent）。计划由主模型调用 builder 时提案、Joy 批准后才有开发区：
builder_write_file / builder_dev_run 在开发区（sparky 用户、断网容器）开发测试，builder_try_tool 真实运行环境试跑，builder_try_subagent 按正式调用真调一次子代理
（stbcc-builder 装好预演副本，网关接着换模型、真跑、换回：gateway._try_subagent），builder_verify 按部署检查的全部规则检查，
builder_submit 交给平台合入（合入之后的上线由平台在后台做，结果私信 Joy）；builder_guide 查规范（control_center/builder/guide/）；builder_access 查 sparky 能读写哪些路径。
这里只是客户端：以 sparky 用户运行（tool.yaml 的 network: false 禁 TCP），除 builder_access（就在本进程以 sparky 现场实测）外每个函数经本机 socket data/sockets/builder.sock 请服务 stbcc-builder
（control_center/builder/service.py，以 joyzhang14 运行）代办，所有检查在服务里；错误原样回给 agent。"""
import asyncio
import grp
import json
import os
import pwd
import stat
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from control_center import env, launch, tool_kit
from control_center.builder.plan import TOPICS, topic_lines
from control_center.paths import HOME, REPO, RUNS, SHARED_GROUP, TOOL_STATE, WORK, sparky_access

RUN = os.environ.get("STB_RUN_REF", "")
LIMIT = 2 ** 24


async def _call(op: str, wait: float = 900, **args: Any) -> Any:
    """一个请求发给 stbcc-builder，等回复；服务不在算平台故障，服务回的错误原样给 agent。"""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(HOME / "sockets" / "builder.sock"), limit=LIMIT), 10)
        w.write(json.dumps({"op": op, "run": RUN, "args": args}, ensure_ascii=False).encode() + b"\n")
        await w.drain()
        line = await asyncio.wait_for(r.readline(), wait)
        w.close()
        resp = json.loads(line) if line else None
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        raise tool_kit.platform_fault(f"连不上 stbcc-builder（data/sockets/builder.sock）：{type(e).__name__}") from None
    if not isinstance(resp, dict):
        raise tool_kit.platform_fault("stbcc-builder 没有回复")
    if not resp.get("ok"):
        raise ToolError(str(resp.get("error") or "stbcc-builder 拒绝了请求"))
    return resp.get("result")


def _json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


server = MCPServer("builder-dev", instructions="开发区操作：builder_write_file / builder_dev_run 开发测试，builder_try_tool 试跑工具，builder_try_subagent 真调子代理，builder_verify 检查，builder_submit 合入，builder_access 查 sparky 能读写哪些路径")


@server.tool(name="builder_guide", description="查一个主题的规范。主题：\n" + topic_lines())
async def guide(topic: str) -> str:
    return await _call("guide", topic=topic)


@server.tool(name="builder_status", description="当前计划：id、全文、状态（ready 可以开发 / deploying 在合入 / merged 已合入 / deployed / failed）、上次失败原因、凭证填没填")
async def status() -> str:
    return _json(await _call("status"))


@server.tool(name="builder_request_credential", description="请 Joy 填一个 .env 里还没有的凭证。note：一两句话写这是什么、去哪拿")
async def request_credential(key: str, note: str) -> str:
    return _json(await _call("request_credential", key=key, note=note))


@server.tool(name="builder_try_tool", description=(
    "在上线后的运行环境里试本计划的一个工具：装上开发区 tools/<名>/ 的当前代码、按上线方式起它，调 function（以 <工具名>_ 开头）；"
    "要连着调几个就不写 function，写 steps：[{\"function\": …, \"args\": {…}}, …]，按顺序调，有一步失败就停。"
    "工具进程在这一轮 builder 调用里留着，下次试接着用（打开的文档等状态还在）；代码改了、restart=true、builder_verify / builder_submit 之后、运行暂停之后重新起，这一轮调用结束时关掉。"
    "返回 started（能不能起来）、functions、ok、text（不可信工具只回字数）、steps（写了 steps 时每步的结果）、stderr（这次的日志与 traceback 尾部），重新起过的带 restarted"))
async def try_tool(tool: str, function: str = "", args: dict[str, Any] | None = None, steps: list[dict[str, Any]] | None = None, restart: bool = False) -> str:
    return _json(await _call("try_tool", wait=12 * 3600, tool=tool, function=function, args=args or {}, steps=steps, restart=restart))   # 试跑上限由服务按计划的 docker.timeout 定（builder.trial_timeout），这里只兜底


@server.tool(name="builder_try_subagent", description=(
    "按正式调用的方式真调一次本计划新建或改的子代理（开发区 subagents/<名>/ 已 commit 的内容，yaml 要和计划一致，hooks 照跑）；要换模型的会换、跑完换回，约 5 分钟。"
    "prompt 同主模型调用它时写的；inputs 是它 yaml 声明的参数（{参数名: 值}）；files 是你工作目录下要带进去的文件的绝对路径（参考图等先写进 files，再在 inputs 里写它拷进试跑工作目录后的路径：同一相对位置）。"
    "返回 ok、result（主模型会拿到的结果，含预览图）或 error、model、seconds、stderr；成品在试跑的 media 目录，不在你的工作目录"))
async def try_subagent(name: str, prompt: str, inputs: dict[str, Any] | None = None, files: list[str] | None = None) -> str:
    return _json(await _call("try_subagent", wait=600, name=name))


@server.tool(name="builder_check", description=(
    "重新验证一个已部署工具的凭证（调它的 <工具名>_check）；过了而且部署时因为没过还没加进任务，就现在按计划的 add_to 加。返回过没过和检查输出"))
async def check(tool: str) -> str:
    return _json(await _call("check", tool=tool))


@server.tool(name="builder_write_file", description="在开发区仓库里写一个文件（path 是仓库内相对路径，如 tools/<工具名>/server.py；整篇覆盖）")
async def write_file(path: str, content: str) -> str:
    return await _call("write_file", path=path, content=content)


@server.tool(name="builder_dev_run", description=(
    "在开发区的断网容器里跑一条命令：argv 是参数列表、不经 shell（如 [\"git\", \"add\", \"-A\"]）；cwd 是 /repo 下的相对目录；"
    "timeout 秒（默认 600，最多 1800）。返回退出码与 stdout / stderr 的尾部"))
async def dev_run(argv: list[str], cwd: str = "", timeout: int = 600) -> str:
    return _json(await _call("dev_run", wait=max(1, min(int(timeout), 1800)) + 300, argv=argv, cwd=cwd, timeout=timeout))


@server.tool(name="builder_verify", description=(
    "按部署检查的全部规则检查开发区已 commit 的内容（没提交的改动、改动范围、yaml 与计划一致、凭证值扫描、全部测试、真实运行环境试跑），不合入、不部署。返回 ok、errors、passed、note（main 有新提交时的提示，不用 fetch / rebase）；有错照改法改、commit 再调它"))
async def verify() -> str:
    return _json(await _call("verify", wait=3600))


@server.tool(name="builder_submit", description=(
    "builder_verify 全过后调用，合入 main。不过就不合入，报错直接返回"))
async def submit() -> str:
    return _json(await _call("submit", wait=7200))


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _platform_home() -> Path:
    """平台用户（仓库属主）的家目录；查不到这个用户（测试容器）时用本进程的 ~（工具进程的 HOME 就是平台用户的家目录，见 launch.py）。"""
    try:
        return Path(pwd.getpwuid(REPO.stat().st_uid).pw_dir)
    except (KeyError, OSError):
        return Path.home()


def _probe(p: Path) -> str:
    """以本进程的身份（sparky）现场实测一个路径：存在否、读写、目录能不能进入、属主与属组；不存在的看最近的已有父目录能不能写。"""
    try:
        st = p.stat()
    except FileNotFoundError:
        parent = p.parent
        while not os.path.exists(parent) and parent != parent.parent:
            parent = parent.parent
        can = os.access(parent, os.W_OK | os.X_OK)
        return f"不存在；最近的已有父目录 {parent} {'可写（能在里面建）' if can else '不可写'}"
    except OSError as e:
        return f"挡住（{type(e).__name__}：上级目录进不去）"
    r, w = os.access(p, os.R_OK), os.access(p, os.W_OK)
    out = "读写" if r and w else "只读" if r else "挡住（可写不可读）" if w else "挡住"
    if stat.S_ISDIR(st.st_mode):
        out = "目录，" + out + ("、能进入" if os.access(p, os.X_OK) else "、进不去")
    else:
        out = "文件，" + out
    return f"{out}，属主 {_owner(st.st_uid)}，属组 {_group(st.st_gid)}"


def _summary() -> str:
    """总表：平台 data 下按文件夹的权限、平台给工具运行时开的可写目录、常用位置的实测。"""
    lines = [f"sparky 能读写哪些路径（本进程以 {_owner(os.getuid())} 现场实测；别的路径带 paths 再调）",
             f"sparky 与 Joy 共用 {SHARED_GROUP} 组：Joy 的家目录组只读（凭证除外，进不去），下面列出的几处组读写；sparky 的家目录组读写。",
             "一、平台 data 下按文件夹的权限（paths.sparky_access，文件夹里面不再细分）："]
    lines += [f"  {d}/：{ {7: '读写', 5: '只读'}.get(perm, f'权限位 {perm}') }" for d, perm in sparky_access().items()]
    lines += ["二、平台给工具运行时开的可写目录（每次起工具现建、开给 sparky 读写，launch.prepare / launch.tool_state）：",
              f"  STB_STATE_DIR = {TOOL_STATE}/<工具名>/：工具自己跨运行的状态",
              f"  STB_MEDIA_DIR = {WORK}/<任务>/media/<运行 id>/（沙箱里外同一个路径）：本次运行的成品",
              f"  STB_TOOL_RUN_DIR = {RUNS}/<任务>/<运行 id>/<工具名>/：本次运行这个工具的目录，主 agent 看不到（工具名撞上 {' / '.join(launch.RUN_DIR_OWN)} 的不给）"]
    now = [f"{k}={os.environ[k]}" for k in ("STB_STATE_DIR", "STB_MEDIA_DIR", "STB_TOOL_RUN_DIR") if os.environ.get(k)]
    if now:
        lines.append("  本次：" + "；".join(now))
    home = _platform_home()
    spots = [("任务工作区", WORK), ("笔记", home / "Obsidian"), ("应用", home / "apps"), ("模型", home / "models"), ("研究", home / "research"),
             ("平台仓库", REPO), ("凭证", env.path()), ("凭证键名", env.keys_path()), ("SSH", home / ".ssh"), ("HuggingFace 缓存", home / ".cache" / "huggingface")]
    try:
        spots.insert(0, (f"{launch.SPARKY} 的家目录", Path(pwd.getpwnam(launch.SPARKY).pw_dir)))
    except KeyError:
        pass
    lines.append("三、常用位置实测：")
    lines += [f"  {label} {p}：{_probe(p)}" for label, p in spots]
    return "\n".join(lines)


@server.tool(name="builder_access", description=(
    "现场实测路径能不能读写。不传 paths：返回总表（平台 data 下各文件夹、工具运行时的可写目录、常用位置）；"
    "传 paths：逐个实测（存在否、读写、目录能不能进入、属主、属组）"))
async def access(paths: list[str] = []) -> str:
    if not paths:
        return _summary()
    out = []
    for raw in paths:
        p = Path(os.path.realpath(os.path.expanduser(raw)))
        out.append(f"{raw}{'' if str(p) == raw else f' → {p}'}：{_probe(p)}")
    return "\n".join(out)


if __name__ == "__main__":
    server.run("stdio")
