"""工具网关（规格第 6 节、11.3、11.4、12）：一次运行一个实例，宿主机进程。

- 只加载任务勾选的 MCP 服务器（工具能做什么由它自己的 tool.yaml 与程序定，任务只决定用不用；限制写在任务的 system_prompt 里）；
- 对沙箱暴露 GET /tools、POST /call、GET /state（暂停信号）三个接口，Bearer 令牌一次运行一个；
- 读外部内容的工具只给 isolated 的子代理用（usable_by: subagent），主模型只拿到核对过引文的答案；
- 顺带做模型端点的 TCP 转发（tools-only 网络里容器只能到网关所在的桥 IP）；
- 每次调用写 gateway.jsonl（不含凭证、不含不可信原文；工具结构化结果里的 source、summary 一并记下，网页按它显示子代理每一步：函数名 + summary、读的是哪里）；
- 预算（D34）由任务 yaml 定死，agent 加不了：主模型时间（timeout）由运行器管，子代理调用与就地等问题卡 / 审批卡的时间不算；
  调用次数不限（主模型自己控制，要限就写进任务的 system_prompt）。失败分四类（记在 gateway.jsonl 的 code；给主模型的是报错原文，网关不加类别和建议）：调用有误（参数、白名单）、
  平台故障（容器没起来 / 被系统强制终止、owa-launcher 连不上、模型端点或 ComfyUI 不可用、工具或网关代码里的意外异常即程序错误）、
  超时（子代理的单次上限、工具自己的时限如解析容器的 180 秒）、子代理出错（交回的结果不合格）。
  每次工具结果后面附一行剩余的主模型时间（timeout: none 不附）；主模型时间到点后运行器开收尾轮（wrapup），这时 agent 的调用一律拒绝。
- 子代理的单次上限（subagents/<名>/subagent.yaml 的 timeout）：这次调用里工具发的审批卡等 Joy 处理的时间不算（发卡算）。
  timeout: none（不限）的子代理照运行器的停滞规则（runner.STALL_S）：调用里 STALL_S 秒既没有模型交回输出、也没有在途的工具调用，就结束这次调用、按子代理出错交回（_unstalled）。
- 每个工具的本次运行目录：环境变量 STB_TOOL_RUN_DIR = 运行目录 <工具名>/（平台现建、开给 sparky 读写，launch.prepare；工具名撞上
  平台自己的子目录 launch.RUN_DIR_OWN 的不给）。平台不管里面放什么、什么时候处理：跨运行的状态由工具自己的 hook 管
  （如读信工具把新已读位置写在这里，它的 run_end hook 在运行 complete 时写回 data/tool_state/<工具名>/）。
- 子代理调用结果：每次主模型调用子代理交回的结果（主模型拿到的那份，隔离的只有核对过的答案）记进运行目录的 SUBAGENT_RESULTS（主模型看不到运行目录），
  hook 按需自己读（如 send-email 的 pre_call 核对正文链接）。
- 审批卡（C3）：工具在调用里发了审批卡（payload.run 是本次运行），调用返回后网关就地等 Joy 处理（从发卡算 acquire_permission.WAIT_S），
  处理了就把结果接在工具结果后面；没处理就像问题卡一样让出（运行记 waiting），Joy 之后处理时按任务优先级入队续跑。
  批了就执行（acquire_permission 文件头）：主模型的工具调用在请求 _meta 里带 stb_call（这次调用的 id，工具发卡时记进 payload.call），
  网关据此把卡和调用对上；工具已经不在等时 Joy 同意了，网关就地还在等就当场以同样的参数再调用一次（_meta 带 stb_approved），已让出就由 stbcc-hub
  调 execute_approved 在同样的身份与环境里替它调用一次。
  工具因为发了卡先报调用有误（stbcc-docker 申请磁盘额度发的审批卡）也一样等，结果接在报错文字后面。
- 网关锁：任务的工具调用一次一个（持网关锁，子代理调用不占）；任何调用就地等问题卡 / 审批卡期间（_wait_round）放开网关锁、
  不算主模型时间，等完再拿回锁接着跑，别的调用照常进来。
- 用法规范按需给：工具有 guide.md 或任务 yaml 的 tools 项写了 guide 时，一次会话里第一次调用它（任何一个函数）不执行，
  先返回规范（通用 + 本任务追加），之后再调才执行。规范只在这次运行里有效：给过的记录从会话文件里认（结果以 GUIDE_MARK 开头的工具结果），
  同一次运行续跑（暂停、问题卡让出后恢复）不重给；每次运行都是新会话，下一次运行再调用就重给。压缩（compaction）之后的都算没给过，下次调用再给。
- 按需加载（平台工具 load-tool）：系统提示里有能加载的名字目录（prompts.catalog_text），主模型 load_tool 加载 tools/ 下 usable_by: universal 的工具
  或 subagents/ 下的子代理（registry.searchable）；load_tool 由网关自己处理（_load_tool，不起进程）：起那个工具（同任务的工具，整个运行开着）或把子代理加进 callees，
  结果是能直接调用的函数与参数表（含 guide.md，之后第一次调用不再先给规范）。
  /tools 的 tools 只列任务 yaml 的（发给模型的列表固定不变，前缀缓存不破），已加载的另在 loaded，还没加载的另在 loadable（任务列了 load-tool 时开跑算一次）。
  pi 的一次运行开跑时就把工具表快照下来、运行中注册的用不上，所以 pi 扩展开跑时把三者全部注册，只把 tools 发给模型；
  调到还没加载的函数，网关回「先用 load_tool 加载」、不执行。
  agent 子代理的工具表固定，不能加载。续跑时运行器把上一段加载过的名单（检查点 gateway.json 的 loaded）交给新网关重新加载（restore_loaded）。
- 缺凭证：工具用到的凭证 .env 里没有（报错带 registry.MISSING_CREDENTIAL 标记）：网关给 Joy 发凭证卡（同一个键只发一张），告诉模型填好后再调用。
- hooks（control_center/hooks.py）：每次执行调用前跑本任务的与工具（或被调用的子代理）自己的 pre_call（先任务的、再工具的；能拦、能改写参数），
  结束后（成功、失败、被取消）跑 post_call；平台代发的调用（origin system，如最终回答的 reply）一样跑。用法规范先返回、被 pre_call 拦下的不算执行。
- 子代理（subagents/<名>/subagent.yaml，任务 yaml 的 subagents 列出的）：主模型看到每个子代理一项可调用的函数（名字 = 子代理名、说明 = 它 yaml 的
  description、参数 = prompt（agent 另可附 files）或 prompt + 它声明的 inputs），调用时主模型这次调用一直挂着（冻结）。网关：用法规范（guide.md）先返回 →
  pre_call hook → 按铁律换模型（档位和主模型不同就卸主模型、装它的，跑完装回）→ 在单次上限里跑（agent：另起一个沙箱容器跑 pi，同主 agent，
  用它自己的系统提示、模型和工具，见 _run_agent_pi；generator / engine：它的程序）→ isolated 的核对引文 → output.py → post_call hook，
  结果交回主模型（control_center/subagent_run.py）。agent 子代理的 pi 用这次调用专用的令牌连网关：/tools 只给它自己的函数，/call 走 _sub_tool
  （isolated 的结果在这里标来源编号、另存原文），回复可带插话（见下面插话那条）、不附主模型的剩余预算；运行收尾、或它发的卡等不到让出时回复带 pause，pi 这一轮结束就退出。
  上一次没交回结果就中断了的，同一次运行里再调用时接着原会话（会话目录与状态见 subagent_run.open_session）。
  运行开始时不起任何子代理的东西，主模型真调用了才起：agent 的工具和任务的工具一样起（以 sparky、带运行信息），但只活一次子代理调用——
  这次调用里第一次用到时起、调用结束（交回、出错、超时）就关，工具进程里开着的东西（浏览器容器）随之关掉；generator / engine 的程序第一次调用时起，
  之后整个运行共用（_callee_session）。子代理的函数清单（交给它的模型）先看任务自己起的那份，再看磁盘缓存（data/runtime/tool_defs/，
  按工具 yaml 与工具文件夹里代码的 mtime、大小认，改了就作废），都没有才在这次调用里把那个工具起来拿清单（起的这份就留给这次调用用）。
  主模型看不到只给子代理的那些；子代理的函数清单里没有子代理，调不到子代理。
  子代理的工具结果里的图片按主模型那条路的上限筛过，子代理的档位能看图（vision）就随结果交给它的 pi（_sub_call）。
- 插话（D36）：队列里本任务带 steer 的新条目（listen.py 返回了 steer，如运行中 Joy 又发的消息），在下一次工具调用的回复里认领
  （从队列改名进运行目录，和运行器取条目一样原子，只有一方拿得到）交给 pi，pi 扩展用原生 steer 送进会话：这一轮工具跑完、下一次模型调用之前，
  作为一条用户消息（文字是条目的 steer）。回复带 pause（暂停、收尾、问题卡让出）时不认领，留在队列里照常开下一次运行。
  agent 子代理在跑时（主模型挂着等它），插话由子代理下一次工具调用的回复认领、先送进它的 pi（同样的 steer；隔离的把插话文字算进主模型问的问题，
  照抄插话里的问题的答案能过核对；档位看不了图的去掉图片、注明）；同一条记下待转交，子代理这次调用交回后随主模型那次调用的回复一并给主 pi
  （_sub_steers）。几个子代理同时在跑时谁先发调用谁认领，不重复送。generator / engine 没有 pi，插不进，插话照旧等主模型下一次调用。
  待转交的只在网关内存里：运行在转交前就停了（暂停、让出、续跑换了网关），运行器收尾时照常核对主会话里有没有它（runner.settle_injected），
  没有就把条目放回队列，下一次运行主模型照样收到（子代理那边已收到过）。
- builder 试跑子代理（builder-dev 的 builder_try_subagent，_try_subagent）：stbcc-builder 把开发区里计划新建 / 改的子代理装进预演目录后，
  网关另起一个只有它的试跑网关（_TrialGateway，运行身份 builder-trial/<计划>）按正式调用真跑一次，换模型借本网关的切换（同一把锁、同一个切换状态），结果交回 builder。
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from control_center import cards, acquire_permission, builder, hooks, hub_client, launch, reader, reply, sandbox, skills, subagent_run, subagents
from control_center.models import PI_PROVIDER, call_name, is_up, load_models, pi_models_json, profiles
from control_center.registry import (MISSING_CREDENTIAL, PLATFORM_ENV, PLATFORM_FAULT, PROGRAM_ERROR, LOADER, is_platform, TEST_ENV_PREFIX, TOOL_TIMEOUT, TOOLS,
                                     program_error, searchable)
from control_center.task import SubagentUse, Task, TaskError, ToolUse, task_callees
from control_center.tokmeter import TokenMeter
from control_center import paths

MAX_ARGS_CHARS = 200_000
MAX_RESULT_CHARS = 100_000      # 可信工具结果给模型的上限：超了全文存进本次运行的 media 目录（_overflow），提示写明存在哪、从哪接着读
MAX_ERROR_CHARS = 2000          # 工具报错原文给模型的上限
MAX_IMAGES = 4                  # 一次工具结果最多带给模型的图片数（生成图的预览、视频的缩略拼图）
MAX_IMAGE_B64 = 1_500_000       # 单张图片 base64 上限：超过的丢掉，结果文字里说明
WRAPUP_NOTE = "额度已用完：不要再调用工具，用已有材料收尾，标明哪些没做完"   # 收尾轮（D34）的提示，也是这时拒绝调用的说明
GUIDE_MARK = "〔用法规范：{tool}〕"                  # 规范结果的第一行：网关从会话文件里认哪些工具已给过（工具结果以它开头）
GUIDE_RE = re.compile(r"^〔用法规范：([a-z0-9-]+)〕")
GUIDE_TASK = "【本任务的规则】"
GUIDE_END = "——这次没有执行；按上面的规范确认参数后重新调用。"
GUIDE_NOTE = "（第一次调用先返回用法规范、不执行）"     # 有规范的工具，描述后面加这句
SUBAGENT_NOTE = "（子代理：做完才交回结果）"   # 子代理的描述后面加这句
LOADS_FILE = "loads.json"           # 运行目录里：本次运行加载过的工具与 skill 的加载结果 {texts: {键: 原文}}（load_texts）
RESTORED_FILE = "restored.txt"      # 运行目录里：最近一次压缩前加载过的那些的原文，pi 扩展插在压缩摘要后面（手册 §4.10 第 5 步）
RESTORED_HEAD = "（系统消息）上下文压缩前加载过的工具和 skill，原样接在这里，照常用："
CARRY_HEAD = "（系统消息）前面对话里加载过的工具和 skill，这次接着有效（已经加载好了，直接用）："
LOADED_SKILL_RE = re.compile(r"^(?:（[^\n]*）\n)?已(?:导入并)?加载 skill ([a-z0-9-]+)")   # skills 工具 load_skill / import_skill 成功的结果（开头可能先有一行装不上的包）


def _read_json(p: Path) -> dict[str, Any]:
    try:
        got = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_json(p: Path, data: dict[str, Any]) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


LOAD_TOOL = {"name": "load_tool", "description": "按系统提示里目录的名字加载一个工具或子代理：交回用法规范和函数参数表，之后直接调用",   # 平台工具 load-tool 的函数（_load_tool）
             "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "目录里的名字，如 transcribe、isolated-reader"}},
                             "required": ["name"]}}
STEER_IMAGES_SKIPPED = "\n（平台：这条消息附了 {n} 张图片，你的模型看不了图，没有附上）"   # 插话送进看不了图的子代理时
CARD_POLL_S = 5                                     # 子代理调用在途时多久重算一次审批卡等待（单次上限按它顺延）


def clip_error(text: str) -> str:
    """工具报错原文给模型：超过 MAX_ERROR_CHARS 留头留尾（改法常写在末尾），中间标明省略了多少。"""
    if len(text) <= MAX_ERROR_CHARS:
        return text
    head = MAX_ERROR_CHARS * 2 // 3
    return f"{text[:head]}…[省略 {len(text) - MAX_ERROR_CHARS} 字符]…{text[-(MAX_ERROR_CHARS - head):]}"


def bug(e: BaseException, where: str) -> "GatewayError":
    """网关代码里的意外异常：程序错误（平台故障）；traceback 写 stderr（gateway.stderr.log）。"""
    import sys
    import traceback

    traceback.print_exception(e, file=sys.stderr)
    return GatewayError("BUG", program_error(where, e))


def passed_env() -> dict[str, str]:
    """网关进程环境里要透传给工具 / 子代理程序的：平台自己的 registry.PLATFORM_ENV 与 registry.TEST_ENV_PREFIX 开头的测试开关（工具自己认，平台不列名单）。"""
    return {k: v for k, v in os.environ.items() if k in PLATFORM_ENV or k.startswith(TEST_ENV_PREFIX)}


META_KEYS = ("source", "summary", "stale")   # 工具结构化结果（MCP structured_content）里平台认的键：记进审计日志（gateway.jsonl），不给主模型；stale 交给子代理的 pi（tool_kit.result）


def tool_meta(res: Any) -> dict[str, str]:
    """工具结果的结构化信息（control_center/tool_kit.result 写进 structured_content）里平台认的键 META_KEYS：只要字符串，最多 500 字；没有就是空。"""
    sc = getattr(res, "structured_content", None)
    return {k: v[:500] for k in META_KEYS if isinstance(sc, dict) and isinstance(v := sc.get(k), str) and v.strip()}


SCHEMA_VALUES = ("default", "enum", "const", "examples")   # schema 里装参数值的键：inline_refs 不往里走


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 MCP 生成的 {"$ref": "#/$defs/X"} 就地展开（pi 扩展的 schema 转换不认 $ref），并去掉自动生成的 title（参数名首字母大写，模型用不上、白占 token）。
    default、enum、const、examples 是参数值，原样保留。"""
    defs = schema.get("$defs") or {}

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            ref = v.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and ref[8:] in defs:
                return walk({**defs[ref[8:]], **{k: x for k, x in v.items() if k != "$ref"}})
            return {k: ({n: walk(y) for n, y in x.items()} if k == "properties" and isinstance(x, dict)   # properties 的键是参数名（可能正好叫 default、title）
                        else x if k in SCHEMA_VALUES else walk(x))
                    for k, x in v.items() if k != "$defs" and not (k == "title" and isinstance(x, str))}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(schema)


def _type(p: dict[str, Any]) -> str:
    """签名里一个参数的类型：标量写类型名（几种可选用 |）、枚举写可选的值、字符串等的数组写 array[类型]；有字段的对象（嵌套结构）为空，由调用方给 JSON。"""
    if "enum" in p:
        return " | ".join(json.dumps(x, ensure_ascii=False) for x in p["enum"])
    if "anyOf" in p:
        subs = [_type(x) for x in p["anyOf"]]
        return " | ".join(subs) if all(subs) else ""
    t = p.get("type", "")
    if t == "array":
        item = _type(p.get("items") or {})
        return f"array[{item}]" if item else ""
    if t == "object" and p.get("properties"):
        return ""
    return t if isinstance(t, str) and t else "object" if t == "object" else "any"


def signature(fn: dict[str, Any]) -> str:
    """load_tool 结果里一个函数的写法（手册 §4.10 第 5 步）：名字(参数: 类型 = 默认值, …)：说明；有说明的参数另起一行；
    嵌套结构的参数（有字段的对象、对象数组）照旧给它的 JSON。pi 里注册的仍是完整定义，执行与类型修正不变。"""
    params = fn.get("parameters") or {}
    req = set(params.get("required") or [])
    sig, notes = [], []
    for k, p in (params.get("properties") or {}).items():
        t = _type(p) or "见下"
        sig.append(f"{k}: {t}" + ("" if k in req or "default" not in p else f" = {json.dumps(p['default'], ensure_ascii=False)}"))
        if not _type(p):
            notes.append(f"  - {k}：{json.dumps({x: y for x, y in p.items() if x != 'description'}, ensure_ascii=False)}" + (f"（{p['description']}）" if p.get("description") else ""))
        elif p.get("description"):
            notes.append(f"  - {k}：{p['description']}")
    return "\n".join([f"{fn['name']}({', '.join(sig)})：{fn.get('description', '')}", *notes])


def activity_state(use: Any) -> str:
    """工具调用期间的运行状态（渠道按它显示进行中标记，如 Discord 的表情）：等 Joy 回答 ask，其余 tool（子代理的见 Gateway._state：隔离的 untrusted；渠道还可以按子代理自己挑，见 Gateway._activity）。"""
    if use.name == acquire_permission.TOOL:
        return "ask"
    return "tool"

SUBAGENT_RESULTS = "subagent_results.jsonl"   # 运行目录里：每次子代理调用交回主模型的结果一行 {ts, subagent, kind, isolated, ok, result}
TOOL_DEFS = "tool_defs"           # data/runtime/tool_defs/<工具名>.json：子代理用的工具的函数清单缓存（cached_defs / save_defs）


def _defs_key(tool: str) -> list[Any] | None:
    """工具 tool 的函数清单缓存的钥匙：tool.yaml 与工具文件夹里的 .py（函数说明可能由服务器脚本之外的文件生成）的路径、mtime、大小，加上描述；读不到文件返回 None（不缓存）。"""
    from control_center.registry import YAML_FILE, _code_files

    spec = TOOLS.get(tool)
    if spec is None:
        return None
    files = [f for f in dict.fromkeys((spec.server, spec.folder / YAML_FILE, *_code_files(spec.folder))) if f.is_file()]
    try:
        return [spec.description, *([str(f), f.stat().st_mtime_ns, f.stat().st_size] for f in files)]
    except OSError:
        return None


def cached_defs(tool: str) -> list[dict[str, Any]] | None:
    """磁盘缓存里工具 tool 的函数定义：钥匙对得上（工具文件没改过）才用，否则 None。"""
    key = _defs_key(tool)
    try:
        got = json.loads((paths.RUNTIME / TOOL_DEFS / f"{tool}.json").read_text())
    except (OSError, ValueError):
        return None
    return got.get("defs") if key is not None and isinstance(got, dict) and got.get("key") == key and isinstance(got.get("defs"), list) else None


def save_defs(tool: str, defs: list[dict[str, Any]]) -> None:
    """把工具 tool 刚列出的函数定义写进磁盘缓存（先写临时文件再改名）；写不了就算了，下次再起。"""
    if (key := _defs_key(tool)) is None:
        return
    d = paths.RUNTIME / TOOL_DEFS
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{tool}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"key": key, "defs": defs}, ensure_ascii=False))
        tmp.replace(d / f"{tool}.json")
    except OSError:
        pass


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


SOURCE_PREFIX = re.compile(r"^〔来源 s\d+〕\n")    # 隔离子代理的工具结果开头的来源编号（subagent_run.SOURCE_MARK）：认规范时跳过


def session_guides(p: Path | None) -> tuple[set[str], int]:
    """pi 会话文件 p 里已给过规范的工具（最后一次压缩之后的）和压缩次数；读不到就是 (空, 0)。主 agent 与 agent 子代理的会话一样认。"""
    given: set[str] = set()
    n = 0
    try:
        lines = p.read_text(errors="replace").splitlines() if p else []
    except OSError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "compaction":
            given.clear()
            n += 1
        elif e.get("type") == "message" and isinstance(m := e.get("message"), dict) and m.get("role") == "toolResult":
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and (g := GUIDE_RE.match(SOURCE_PREFIX.sub("", str(c.get("text") or ""), 1))):
                    given.add(g.group(1))
    return given, n


@dataclasses.dataclass
class SubCall:
    """一次 agent 子代理调用（_run_agent_pi）：它的 pi 用 token 连网关，网关据此只给它自己的函数（fns），调用走 _sub_tool。
    folder / state：会话目录与状态（subagent_run.open_session）；guided：这个会话里已给过规范的工具（压缩后清空，compactions 记上次看到的压缩次数）。"""
    d: subagents.SubagentDef
    folder: Path
    state: dict[str, Any]
    fns: list[dict[str, Any]]
    session_for: Any
    rec: dict[str, Any]
    watch: dict[str, Any]
    vision: bool
    guided: set[str] = dataclasses.field(default_factory=set)
    compactions: int = 0


def profile_env(prof: dict[str, Any], name: str, effort: str | None = None) -> dict[str, str]:
    """generator / engine 子代理的程序拿到的模型档位（工具不拿）：STB_MODEL = 档位 name 在 models.yaml 里的整段配置（JSON）加 name、
    call_name（调用名：端点 /models 里属于这个档位的那一条的 id；端点不答 /models 的档位没有这个键），~ 开头的值展开成绝对路径。
    程序自己取要的字段（地址 endpoint、调用名 call_name、安装目录 home…），平台不按底座分支。"""
    conf = {k: os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v for k, v in prof.items()}
    called = call_name(prof) if prof.get("endpoint") else None
    return {"STB_MODEL": json.dumps({**conf, "name": name, **({"call_name": called} if called else {}), **({"effort": effort} if effort else {})},
                                    ensure_ascii=False)}   # effort：engine 子代理写了推理档位才有，用不用由程序决定


def run_env(task: str, run_dir: Path, work_dir: Path, tool: str) -> dict[str, str]:
    """每个工具进程都带的运行信息（registry.RUN_ENV，不用声明）：工作目录、运行 id / ref / 目录、附件目录、暂停信号、
    本次运行这个工具的目录 STB_TOOL_RUN_DIR（运行目录 <工具名>/，起工具时平台现建现开，launch.prepare；名字撞上 launch.RUN_DIR_OWN 的不给）。"""
    return {"STB_WORK_ROOT": str(work_dir), "STB_RUN_ID": run_dir.name, "STB_RUN_REF": f"{task}/{run_dir.name}",
            "STB_PAUSE_FILE": str(run_dir / "signal" / "pause"), "STB_RUN_DIR": str(run_dir), "STB_RUN_IN": str(run_dir / "in"),
            **({"STB_TOOL_RUN_DIR": str(run_dir / tool)} if tool not in launch.RUN_DIR_OWN else {})}


def overflow_note(work_dir: Path, run_id: str, name: str, text: str) -> str:
    """可信工具结果超过 MAX_RESULT_CHARS：全文存成本次运行 media 目录里的文本文件，返回接在截断处的说明（存在哪、从第几个字符接着读；不写工具名，
    用哪个能读文件的工具由主模型自己看手上有什么）。"""
    d = work_dir / "media" / run_id
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", name) + "-result"
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{stem}-{len(list(d.glob(stem + '-*.txt'))) + 1}.txt"
        f.write_text(text)
    except OSError as e:
        return f"\n…[截断：共 {len(text)} 字符，只给了前 {MAX_RESULT_CHARS} 字符；全文没能存下来（{e}），要看后面的就缩小范围重试]"
    where = str(f)
    return (f"\n…[截断：共 {len(text)} 字符，只给了前 {MAX_RESULT_CHARS} 字符；完整结果存在 {where}，"
            f"要看后面的就用你能读文件的工具从第 {MAX_RESULT_CHARS} 个字符接着读它，没有能读文件的工具就缩小范围重试]")


async def execute_approved(ref: str, r: dict[str, Any]) -> dict[str, Any]:
    """让出后 Joy 同意了记着调用的审批卡 r（acquire_permission 的一轮）：stbcc-hub 当场替那次调用执行一次，和网关起工具同样的身份与环境
    （以 sparky 起、Landlock 按 tool.yaml 的 network、同一运行的工作目录 / 运行目录 / 运行信息），调同名函数同参数（_meta 的 stb_approved 指明这张卡：
    工具经 card_use 用掉它，对不上就不执行、不发新卡）。pre_call hook 拦下时发的卡（kind hook）：以同样的输入再跑一遍 pre_call（approved_card 指明这张卡），
    还拦着就是结果，放行了返回 {proceed: True}（续跑时运行器调用这一次）。返回 {ok, text}（text 与网关交给模型的一样：超长的存文件、截断处写明）。"""
    from control_center.task import load_task

    c = r["call"]
    task, run_id = ref.split("/", 1)
    run_dir = paths.RUNS / task / run_id
    work_dir = paths.run_work_dir(run_dir)
    if c["kind"] == "hook":
        t = load_task(paths.task_file(task))
        owner = hooks.subagent_owner(c["tool"]) if c.get("subagent") else hooks.tool_owner(c["tool"])
        payload = {"task": task, "run_id": run_id, "tool": None if c.get("subagent") else c["tool"], "subagent": c["tool"] if c.get("subagent") else None,
                   "function": c["function"], "args": c["args"], "approved_card": r["card"]}
        got = await hooks.afire("pre_call", [o for o in (hooks.task_owner(t), owner) if o is not None], payload, run_dir, work_dir)
        return {"proceed": True} if got.blocked is None else {"ok": True, "text": got.blocked}
    spec = TOOLS[c["tool"]]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()), **run_env(task, run_dir, work_dir, c["tool"]), **passed_env()}
    cmd, env = launch.tool_command(spec, env, launch.RunCtx(work_dir, run_dir, run_id))
    with open(run_dir / "gateway.stderr.log", "a") as log:
        async with stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env), errlog=log) as (rd, wr):
            async with ClientSession(rd, wr) as session:
                await session.initialize()
                res = await session.call_tool(c["function"], c["args"], meta={"stb_call": secrets.token_hex(6), "stb_approved": r["card"]})
    text = "\n".join(x.text for x in getattr(res, "content", []) if getattr(x, "type", "") == "text")
    if getattr(res, "is_error", False):
        out = {"ok": False, "text": clip_error(text) or f"工具 {c['function']} 返回了错误但没有任何说明文字"}
    else:
        out = {"ok": True, "text": text[:MAX_RESULT_CHARS] + overflow_note(work_dir, run_id, c["function"], text) if len(text) > MAX_RESULT_CHARS else text}
    try:                                            # post_call 照正式调用一样跑（approved_card 标明这是批准后平台替它执行的）；出错不影响结果
        t = load_task(paths.task_file(task))
        await hooks.afire("post_call", [o for o in (hooks.task_owner(t), hooks.tool_owner(c["tool"])) if o is not None],
                          {"task": task, "run_id": run_id, "tool": c["tool"], "subagent": None, "function": c["function"], "args": c["args"],
                           "ok": out["ok"], "result": out["text"], "inflight": 0, "approved_card": r["card"]}, run_dir, work_dir)
    except Exception as e:  # noqa: BLE001
        print(f"批准后执行的 post_call 出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
    return out


class _CallTools:
    """一次 agent 子代理调用里起的工具进程（_run_agent_pi）：第一次用到时起（_open_tool，同任务的工具一样的身份与运行信息），调用结束时一起关。
    子代理的 pi 的调用从网关的 HTTP 处理任务里进来，MCP 的进程上下文要在同一个任务里进出，所以每个工具进程由一个后台任务持有（同 _hold_callee）。"""

    def __init__(self, gw: "Gateway") -> None:
        self.gw = gw
        self.live: dict[str, ClientSession] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.holders: list[asyncio.Task[Any]] = []
        self.release = asyncio.Event()

    async def open(self, tool: str) -> list[dict[str, Any]]:
        """起这次调用的工具 tool，返回它的函数定义；起不来（Landlock、sudo、依赖）是平台故障，说明交给子代理。"""
        ready: asyncio.Future[tuple[ClientSession, list[dict[str, Any]]]] = asyncio.get_running_loop().create_future()

        async def hold() -> None:
            try:
                async with AsyncExitStack() as stack:
                    try:
                        got = await self.gw._open_tool(tool, self.gw._errlog, stack)
                    except Exception as e:  # noqa: BLE001 — 原样交给等着的调用；退出 stack 关掉进程
                        ready.set_exception(e)
                        return
                    ready.set_result(got)
                    await self.release.wait()
            except BaseException as e:
                if not ready.done():
                    ready.cancel() if isinstance(e, asyncio.CancelledError) else ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
                elif isinstance(e, Exception):        # 关进程时的噪声不影响结果
                    self.gw._audit({"ev": "stop_error", "tool": tool, "error": str(e)[:200]})
        self.holders.append(asyncio.create_task(hold()))
        try:
            self.live[tool], defs = await asyncio.shield(ready)
        except Exception as e:  # noqa: BLE001
            raise GatewayError("PLATFORM", f"工具 {tool} 没起来：{type(e).__name__}: {str(e)[:200]}") from None
        return defs

    async def session(self, tool: str) -> ClientSession:
        async with self.locks.setdefault(tool, asyncio.Lock()):
            if tool not in self.live:
                await self.open(tool)
        return self.live[tool]

    async def close(self) -> None:
        self.release.set()
        if self.holders:
            await asyncio.wait(self.holders, timeout=30)


class Gateway:

    def __init__(self, task: Task, run_dir: Path, work_dir: Path, model_profile: dict[str, Any], bind_ip: str) -> None:
        self.task, self.run_dir, self.work_dir, self.profile, self.bind_ip = task, run_dir, work_dir, model_profile, bind_ip
        self.token = secrets.token_hex(32)
        self.sessions: dict[str, ClientSession] = {}          # 任务的工具名 → MCP 会话（整个运行开着；agent 子代理的工具每次调用另起，见 _run_callee）
        self._fn_defs: dict[str, list[dict[str, Any]]] = {}   # 工具名（任务的与 agent 子代理的，用到才有）→ 它的函数定义 {name, description, inputSchema}
        self.tools: dict[str, tuple[ToolUse, dict[str, Any]]] = {}   # agent 可见工具名 → (ToolUse, MCP 工具定义)
        self.rejections = 0
        self.bugs: list[str] = []             # 这次运行里出过意外异常（程序错误）的工具：运行器收尾时提醒 Joy
        self.bug_notes: dict[str, str] = {}   # 工具 → 第一次意外异常的「类型: 消息」（前 150 字），随提醒发给 Joy
        self.agent_messages = 0
        self.meter = TokenMeter()            # 子代理那边的模型调用的 token 用量（每次调用返回的 usage；主模型的由运行器从 pi 的事件流算）
        self.channel: str | None = None      # 来源渠道（渠道消息触发的运行，如 discord、web；别的运行为 None）；回复由中枢按 run.json 的 channel 送回渠道
        self.ref: dict[str, Any] | None = None   # 消息标记（渠道给的那条消息）：有它才报运行中的状态（_activity）
        self.calls = 0
        self._pause_requested = False       # 运行器请求暂停：下一次 /call 回复与 /state 都带 pause=true，沙箱里的 pi 在边界退出
        self.pause_file = run_dir / "signal" / "pause"  # 同时给长调用的程序（如联网研究子代理）看：看到就取消当前研究；单放一个目录 signal/（launch.RUN_DIR_OWN：不会成为工具的运行目录）
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.unlink(missing_ok=True)  # 被抢占后恢复：沿用同一运行目录，上一次留下的 pause 文件已过期，不清掉工具一启动就以为又要暂停
        self._yaml_mtime: float | None = None    # 任务 yaml 的 mtime：变了才重读，算出被撤掉的工具
        self._revoked: set[str] = set()

        self._stack = AsyncExitStack()
        self._errlog: Any = None               # gateway.stderr.log：各工具进程的标准错误（_start 打开，stop 关）
        self._http: asyncio.AbstractServer | None = None
        self._relays: dict[tuple[str, int], asyncio.AbstractServer] = {}   # 模型端点 (主机, 端口) → 它的 TCP 转发（relay_url）
        self._subcalls: dict[str, SubCall] = {}   # 在途的 agent 子代理调用：它的令牌 → 调用（_run_agent_pi）
        self._sub_busy: set[str] = set()          # 在途调用占着的子代理会话目录名（并发调用同一个子代理时不接同一个会话）
        self._pi_runs: set[asyncio.Task[Any]] = set()   # 在跑的子代理 pi 容器：abort / stop 时取消（sandbox.run_pi 随之停容器）
        self._sub_steers: list[dict[str, Any]] = []   # D36：子代理认领、已送进它的 pi 的插话 {text, images}，待随主模型下一次调用的回复转交主 pi
        self._lock = asyncio.Lock()
        self._log = open(run_dir / "gateway.jsonl", "a")
        self.url = ""
        self.model_url = ""
        self.progress_id: str | None = None   # 运行器给的进行中标记 id（渠道开跑时回的，原样带回）：工具调用期间报状态
        self.inflight = 0                     # 在途工具调用数：>0 时不算停滞（长调用由工具自己的超时兜底）
        self.last_activity = time.time()      # 最近一次有动静：工具调用开始/结束、模型端点有字节流过（含思考 token）
        self.started = time.time()
        self.wrapup = False                   # 收尾轮（D34）：主模型时间已到，agent 的调用一律拒绝，回复带 pause 让 pi 这一轮结束就退出
        self.elapsed_before = 0.0             # 之前各段已用的秒数（续跑时扣掉上一份额度），算主模型时间还剩多久
        # D32：子代理的档位（yaml 的 runtime + model，inherit 同主任务）；和主任务不同时，调用前后经运行器的 ensure_model 切换
        self.task_model: str | None = None    # 有子代理写了具体的 runtime + model 才解析档位名；都 inherit 时 sub_models 为空，不切换
        self.sub_models: dict[str, str] = {}              # 子代理名 → 与主任务不同的子档位名
        self.sub_profiles: dict[str, dict[str, Any]] = {}  # 子代理名 → 与主任务不同的那个档位
        # 子代理（任务 yaml 的 subagents）：主模型直接调用
        try:
            self.callees: dict[str, subagents.SubagentDef] = task_callees(task)
        except TaskError as e:
            raise GatewayError("SUBAGENT", str(e)) from None
        if any(not d.inherit for d in self.callees.values()):
            models = load_models()
            self.task_model = task.profile_name(models)
            for d in self.callees.values():
                self._sub_profile(d, models)
        self.loaded: dict[str, ToolUse | SubagentUse] = {}   # 运行中经 load-tool 的 load_tool 加载的工具 / 子代理 → 它的项（任务 yaml 里没有它的 guide 项）
        self.restore: list[str] = []          # 续跑：上一段加载过的（运行器的 load_checkpoint 给），restore_loaded 重新加载
        self.load_texts: dict[str, str] = _read_json(run_dir / LOADS_FILE).get("texts") or {}   # 本次运行加载过的工具（名字）/ skill（skill:<名>）→ 加载结果原文（压缩后补回、私信接着用）
        self._loaded_tools = _CallTools(self)  # 加载的工具的进程：加载发生在 /call 的处理任务里，由后台任务持有到 stop（同子代理调用里起的工具）
        self._loadable: dict[str, tuple[str, dict[str, Any]]] = {}   # 能加载、开跑时还不在本次运行的函数名 → (工具 / 子代理名, 函数定义)：_find_loadable
        self.sub_fns: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}   # agent 子代理名 → 它能调的函数 → (工具名, MCP 函数定义)：第一次调用它时才算（_sub_functions）
        self.sub_sessions: dict[str, ClientSession] = {}   # generator / engine 子代理名 → 它程序的 MCP 会话：第一次调用它时才起（_callee_session）
        self._callee_locks: dict[str, asyncio.Lock] = {}  # 同一个程序只起一份（并发的第一次调用排队等它起好）
        self._callee_holders: list[asyncio.Task[Any]] = []   # 每个起了的程序一个后台任务持有它的进程，stop 时放开（进程的上下文要在同一个任务里进出）
        self._release = asyncio.Event()
        self.switch_model: Any = None         # 运行器给的 async (档位, on_loading) -> 失败原因 | None
        self.on_fatal: Any = None             # 运行器给的：切换失败时停掉沙箱
        self.fatal: str | None = None         # 切换失败的原因：运行器据此判任务失败
        self.loading: str | None = None       # 正在加载的档位：状态栏"加载模型：X"，这期间不抢占、不强停
        self._loading_since = 0.0
        self.switch_seconds = 0.0             # 累计卸载 / 加载时间（只作记录：切换都发生在子代理调用里，已算在 sub_seconds 里）
        self.sub_seconds = 0.0                # 累计不算主模型的时间：有子代理调用在途、或就地等问题卡 / 审批卡的墙钟（并发的只算一次，含子代理触发的模型切换）
        self._sub_running, self._sub_since = 0, 0.0
        self._switch_lock = asyncio.Lock()
        self._switch_cond = asyncio.Condition(self._switch_lock)
        self._sub_active: str | None = None   # 切换上下文占着的子档位（占着期间也占着网关锁）；None = 主模型
        self._sub_calls = 0                   # 在用 _sub_active 的调用数（为 None 时是用主模型的并发调用）：同档位的共用一次切换，最后一个结束才切回
        self._sub_waiting = 0                 # 等着换到另一个子档位的调用数：有人等就不切回主模型，直接换过去
        self._sub_tasks: set[asyncio.Task[Any]] = set()   # 正在跑子档位工具的调用（/call 的处理任务）：agent 退出后 abort() 取消它们
        self._closing = False
        self.ask_waiting = False              # ask-user 正在就地等 Joy 回答（D35）
        self.ask_yield = False                # 运行器要它马上让出：就地等回答时来了更高优先级的条目
        self.ask_yielded: dict[str, Any] | None = None   # 这一段让出了的那一轮 {card, round, questions}：回复带 pause，运行记 waiting
        self.session_dir: Path | None = None        # 运行器给：pi 会话目录与恢复时接着用的会话文件（新会话为 None，pi 第一轮写完才落盘）、开跑前已有的会话文件
        self.session_file: Path | None = None
        self.session_before: dict[Path, float] = {}
        self._guided: dict[str, int | None] = {}   # 这一段已给过规范的工具 → 给的那一轮（pi 的轮次；会话文件还没落盘时靠它）
        self._compactions: int | None = None  # 上次看到的会话压缩次数：变了就清空 _guided
        self.injected: list[dict[str, Any]] = []   # D36：这一段认领的插话条目（队列条目原样），运行器收尾时核对有没有进会话、改标记
        self._inflight_tool: dict[str, int] = {}   # 工具 → 在途调用数：post_call hook 的输入带 inflight（并发调用共用的东西等最后一个结束再收）
        self._claimed_cards: set[str] = set()      # 已有调用在等的审批卡：等卡期间放开了网关锁，同时进来的别的调用不再认领它（_await_cards）
        self._trial_lock = asyncio.Lock()          # builder 试跑子代理一次一个（_try_subagent）

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested

    @pause_requested.setter
    def pause_requested(self, v: bool) -> None:
        self._pause_requested = v
        if v:
            self.pause_file.write_text(str(time.time()))
        else:
            self.pause_file.unlink(missing_ok=True)

    # ---- 生命周期 ----
    async def start(self) -> None:
        try:
            await self._start()
        except BaseException:
            await self._stack.aclose()
            raise

    async def _start(self) -> None:
        errlog = self._errlog = open(self.run_dir / "gateway.stderr.log", "a")
        for use in self.task.tools:
            if TOOLS[use.name].builtin:                  # 函数是 pi 自带的（平台工具 workspace）：不起进程，/tools 的 builtins 告诉 pi 扩展开哪几个
                continue
            if use.name == LOADER:                  # 平台工具 load-tool：load_tool 由网关处理（_load_tool），不起进程
                self.tools[LOAD_TOOL["name"]] = (use, LOAD_TOOL)
                continue
            for t in await self._start_tool(use.name, errlog):
                if t["name"] in self.tools:
                    raise GatewayError("TOOL_CONFLICT", f"工具名冲突：{t['name']}")
                self.tools[t["name"]] = (use, t)
        # 子代理（任务 yaml 的 subagents）这里什么都不起：主模型调用它时才起（agent 的工具见 _run_callee，generator / engine 的程序见 _callee_session）
        if any(u.name == LOADER for u in self.task.tools):
            await self._find_loadable(errlog)
        self._http = await asyncio.start_server(self._serve, self.bind_ip, 0)
        self.url = f"http://{self.bind_ip}:{self._http.sockets[0].getsockname()[1]}"
        self.model_url = await self.relay_url(str(self.profile["endpoint"]))
        self._audit({"ev": "start", "tools": list(self.tools), "url": self.url, "model_url": self.model_url})

    async def relay_url(self, endpoint: str) -> str:
        """模型端点 endpoint 在沙箱里的地址：沙箱只连得到网关所在的桥 IP，模型请求经网关的 TCP 转发。主模型的端点开跑时就转发好；
        agent 子代理的档位端点不同（主机或端口不同）就第一次用到时另开一个转发，运行结束（stop）一起关。"""
        u = urlsplit(endpoint)
        key = (u.hostname or "127.0.0.1", u.port or 80)
        if key not in self._relays:
            self._relays[key] = await asyncio.start_server(lambda r, w: self._pipe(r, w, key[0], key[1], self), self.bind_ip, 0)
        return f"http://{self.bind_ip}:{self._relays[key].sockets[0].getsockname()[1]}{u.path}"  # pyright: ignore[reportAttributeAccessIssue]

    async def _session(self, cmd: list[str], env: dict[str, str], errlog: Any, stack: AsyncExitStack | None = None) -> ClientSession:
        """起一个 MCP 服务器进程、建好会话；进程的寿命跟 stack（不给 = 整个运行的 _stack）走：stack 关掉时关会话、关标准输入，进程随之退出。"""
        stack = stack or self._stack
        read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env), errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _start_tool(self, name: str, errlog: Any) -> list[dict[str, Any]]:
        """起任务的工具 name（整个运行开着），返回它的函数定义（也记在 _fn_defs）。"""
        session, self._fn_defs[name] = await self._open_tool(name, errlog, self._stack)
        self.sessions[name] = session
        return self._fn_defs[name]

    async def _sub_functions(self, d: subagents.SubagentDef, open_for: Any) -> dict[str, tuple[str, dict[str, Any]]]:
        """agent 子代理 d 能调的函数 → (工具名, 函数定义)，这次运行第一次调用它时算好记进 sub_fns。每个工具的函数定义：任务自己起的那份（_fn_defs）
        → 磁盘缓存（cached_defs，工具文件没改过）→ 都没有就 open_for(工具名) 在这次子代理调用里把它起来（起的这份留给这次调用用），拿到的清单写进缓存。"""
        if d.name not in self.sub_fns:
            fns: dict[str, tuple[str, dict[str, Any]]] = {}
            for tool in d.tools:
                if TOOLS[tool].builtin:                  # pi 自带的函数（workspace）：子代理的 pi 自己有，/tools 的 builtins 打开
                    continue
                if tool not in self._fn_defs:
                    if (defs := await asyncio.to_thread(cached_defs, tool)) is None:
                        defs = await open_for(tool)
                        await asyncio.to_thread(save_defs, tool, defs)
                    self._fn_defs[tool] = defs
                for t in self._fn_defs[tool]:
                    fns[t["name"]] = (tool, t)
            self.sub_fns[d.name] = fns
        return self.sub_fns[d.name]

    async def _open_tool(self, name: str, errlog: Any, stack: AsyncExitStack) -> tuple[ClientSession, list[dict[str, Any]]]:
        """起工具 name 的 MCP 服务器（任务的工具与 agent 子代理的工具同一种起法，进程寿命跟 stack 走），返回 (会话, 函数定义)。"""
        spec = TOOLS[name]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}
        env.update(self.run_env(name))                  # 运行信息（registry.RUN_ENV）一律给，不用声明
        env.update(passed_env())
        cmd, env = launch.tool_command(spec, env, launch.RunCtx(self.work_dir, self.run_dir, self.run_dir.name))   # 以 sparky 起，联网按 network（control_center/launch.py）
        session = await self._session(cmd, env, errlog, stack)
        return session, [{"name": t.name, "description": t.description or "", "inputSchema": t.input_schema} for t in (await session.list_tools()).tools]

    async def _callee_session(self, d: subagents.SubagentDef) -> ClientSession:
        """generator / engine 子代理 d 的程序的会话：这次运行第一次调用它时才起（_hold_callee 在后台任务里持有进程），之后整个运行共用，stop 时关。
        起不来抛 GatewayError（程序没注册同名函数：SUBAGENT；别的：平台故障）。"""
        async with self._callee_locks.setdefault(d.name, asyncio.Lock()):
            if d.name not in self.sub_sessions:
                if self._closing:
                    raise GatewayError("STOPPED", "运行已在收尾，子代理停下")
                ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
                self._callee_holders.append(asyncio.create_task(self._hold_callee(d, ready)))
                try:
                    await asyncio.shield(ready)          # 这次调用被取消（超时）也不打断起程序：起好了照样记下，下次调用直接用
                except GatewayError:
                    raise
                except Exception as e:  # noqa: BLE001 — 起不来（Landlock、sudo、依赖、程序一启动就退出）
                    raise GatewayError("PLATFORM", f"子代理 {d.name} 的程序没起来：{type(e).__name__}: {str(e)[:200]}") from None
        return self.sub_sessions[d.name]

    async def _hold_callee(self, d: subagents.SubagentDef, ready: "asyncio.Future[ClientSession]") -> None:
        """后台任务：起 d 的程序、把会话交给 ready，一直持有到 stop（_release）再关——MCP 的进程上下文要在同一个任务里进出。"""
        try:
            async with AsyncExitStack() as stack:
                try:
                    session = await self._start_callee(d, self._errlog, stack)
                except Exception as e:  # noqa: BLE001 — 原样交给等着的调用（在任务组外抛，免得被包成 ExceptionGroup）；退出 stack 关掉进程
                    ready.set_exception(e)
                    return
                self.sub_sessions[d.name] = session
                ready.set_result(session)
                await self._release.wait()
        except BaseException as e:
            if not ready.done():
                ready.cancel() if isinstance(e, asyncio.CancelledError) else ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            elif isinstance(e, Exception):               # 关进程时的噪声不影响收尾
                self._audit({"ev": "stop_error", "subagent": d.name, "error": str(e)[:200]})
            if isinstance(e, asyncio.CancelledError):
                raise

    async def _start_callee(self, d: subagents.SubagentDef, errlog: Any, stack: AsyncExitStack) -> ClientSession:
        """起 generator / engine 子代理的程序（它文件夹里的 server，MCP），进程寿命跟 stack 走：环境带运行信息与它的模型档位（STB_MODEL）；is_editable: false 以平台身份跑，
        true 以 sparky 跑（平台给它开本次运行的 media 目录，只连它模型档位端点的端口）。程序要注册一个和子代理同名的函数。"""
        import sys

        prof = self.sub_profiles.get(d.name, self.profile)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()), "STB_SUBAGENT_NAME": d.name,
               "STB_WORK_ROOT": str(self.work_dir), "STB_RUN_ID": self.run_dir.name, "STB_RUN_REF": f"{self.task.name}/{self.run_dir.name}",
               "STB_PAUSE_FILE": str(self.pause_file), "STB_RUN_DIR": str(self.run_dir), "STB_RUN_IN": str(self.run_dir / "in"),
               "PYTHONPATH": paths.child_pythonpath(),                            # 程序 import 的 control_center 是本仓库的（同 launch.tool_command）
               **profile_env(prof, self._profile_name(d), d.effort), **passed_env()}
        cmd = [sys.executable, str(d.server)]
        if d.is_editable:
            port = urlsplit(str(prof.get("endpoint") or "")).port
            launch.grant(self.work_dir / "media" / self.run_dir.name)
            cmd, env = launch.server_command(d.server, (port,) if port else False, env, d.name)  # pyright: ignore[reportArgumentType]
        session = await self._session(cmd, env, errlog, stack)
        if d.name not in {t.name for t in (await session.list_tools()).tools}:
            raise GatewayError("SUBAGENT", f"subagents/{d.name}/{d.server.name if d.server else ''}：没有注册叫 {d.name} 的函数（程序的函数名要等于子代理名）")
        return session

    def _sub_profile(self, d: subagents.SubagentDef, models: dict[str, Any]) -> None:
        """子代理 d 的档位和主任务不同时记进 sub_profiles / sub_models（调用前后经运行器换模型）。"""
        try:
            pn = subagents.profile_name(d, models, self.task_model)  # pyright: ignore[reportArgumentType]
        except subagents.SubagentError as e:
            raise GatewayError("SUBAGENT", str(e)) from None
        if pn != self.task_model:
            self.sub_profiles[d.name], self.sub_models[d.name] = profiles(models)[pn], pn

    def _profile_name(self, d: subagents.SubagentDef) -> str:
        """子代理 d 用的档位名（给它程序的 STB_MODEL 带上）：自己的子档位，或任务的档位；查不到（任务档位不在 models.yaml）是空的。"""
        if name := self.sub_models.get(d.name) or self.task_model:
            return name
        try:
            return self.task.profile_name(load_models())
        except (TaskError, ValueError):
            return ""

    async def stop(self) -> None:
        self._closing = True                  # D32：收尾时不再切回主模型（跑完不切回）；等正在进行的切换做完，免得和下一个任务的加载重叠
        async with self._switch_lock:
            pass
        for s in (self._http, *self._relays.values()):
            if s:
                s.close()
        for t in list(self._pi_runs):         # 还在跑的子代理 pi 容器：取消（sandbox.run_pi 停容器）
            t.cancel()
        if self._pi_runs:
            await asyncio.wait(self._pi_runs, timeout=30)
        self._release.set()                   # 放开 generator / engine 子代理的程序（_hold_callee 关掉它们）
        await self._loaded_tools.close()      # 运行中加载的工具
        if self._callee_holders:
            await asyncio.wait(self._callee_holders, timeout=30)
        try:
            await self._stack.aclose()
        except Exception as e:  # MCP 子进程退出时的噪声不影响收尾
            self._audit({"ev": "stop_error", "error": str(e)[:200]})
        self.token = ""   # 令牌作废
        self._audit({"ev": "stop", "calls": self.calls, "rejections": self.rejections, "agent_messages": self.agent_messages})
        self._log.close()

    def abort(self) -> None:
        """沙箱里的 agent 已退出（完成、超时、停滞、强停、暂停），在途调用的结果没人收了：之后不再切换模型，也不切回主模型
        （同收尾：主模型由下一个任务的 ensure_model 按需加载）；正在跑子档位工具的调用取消掉（MCP 给工具进程发取消通知，
        媒体工具随之撤掉 / 中断 ComfyUI 里的这一单），排队等别的子档位的调用报 STOPPED。网关锁因此很快放开，
        运行器的收尾通知（reply）不用等孤儿生成跑完、再装回主模型。正在进行的切换不打断（同 stop，等它做完）。"""
        self._closing = True
        for t in list(self._sub_tasks) + list(self._pi_runs):     # 子代理的 pi 容器也停掉（sandbox.run_pi 被取消时 docker kill）
            t.cancel()

    def run_env(self, tool: str) -> dict[str, str]:
        """每个工具进程都带的运行信息（模块函数 run_env）。"""
        return run_env(self.task.name, self.run_dir, self.work_dir, tool)

    def revoked(self) -> set[str]:
        """运行中从任务 yaml 的 tools / subagents 里撤掉的工具与子代理（网页「撤销」）：下一次调用起就拒；加上的要等下次开跑。
        yaml 读不到或解析失败时沿用上一次的结果。"""
        import yaml

        try:
            p = paths.task_file(self.task.name)
            mt = p.stat().st_mtime
        except (OSError, ValueError):
            return self._revoked
        if mt != self._yaml_mtime:
            try:
                raw = yaml.safe_load(p.read_text()) or {}
                names = {k: {i if isinstance(i, str) else next(iter(i)) for i in raw.get(k) or [] if isinstance(i, (str, dict)) and i} for k in ("tools", "subagents")}
                self._revoked = ({u.name for u in self.task.tools} - names["tools"]) | ({u.name for u in self.task.subagents} - names["subagents"])
                self._yaml_mtime = mt
            except Exception:  # noqa: BLE001
                pass
        return self._revoked

    # ---- agent 可见的工具列表 ----
    def builtins(self, tools: Any = None) -> list[str]:
        """要在沙箱里打开的 pi 自带函数：tools（不给 = 任务的、去掉运行中撤掉的）里函数是 pi 自带的那些平台工具（registry.BUILTIN_FUNCTIONS）。"""
        names = [u.name for u in self.task.tools if u.name not in self.revoked()] if tools is None else list(tools)
        return [f for n in names if n in TOOLS for f in TOOLS[n].builtin]

    def tool_list(self) -> list[dict[str, Any]]:
        out = []
        revoked = self.revoked()
        for name, (use, t) in self.tools.items():
            if use.name in revoked or use.name in self.loaded:   # 加载的不进这张表（发给模型的列表固定不变），见 loaded_defs
                continue
            desc = t["description"]
            if self.guide_text(use):
                desc += GUIDE_NOTE
            out.append({"name": name, "description": desc, "parameters": inline_refs(t["inputSchema"])})
        for u in self.task.subagents:                  # 子代理：一个子代理一项，名字、说明只来自它的 yaml
            if u.name in revoked:
                continue
            d = self.callees[u.name]
            out.append({"name": d.name, "description": d.description + (GUIDE_NOTE if self.guide_text(u) else "") + SUBAGENT_NOTE,
                        "parameters": d.parameters()})
        return out

    # ---- 用法规范按需给 ----
    def guide_text(self, use: ToolUse | SubagentUse) -> str:
        """这个工具（或子代理）在本任务里的用法规范：工具的 guide.md（子代理的 guide.md）+ 任务 yaml 的 guide；都没有就是空（照常执行）。"""
        if isinstance(use, ToolUse) and is_platform(TOOLS[use.name]):   # 平台工具：guide 在系统提示里（prompts），没有第一次调用先给规范
            return ""
        own = self.callees[use.name].guide if isinstance(use, SubagentUse) else TOOLS[use.name].guide
        parts = [p for p in (own, f"{GUIDE_TASK}\n{use.guide}" if use.guide else "") if p]
        return "\n\n".join(parts)

    def _session_path(self) -> Path | None:
        """这次运行用的 pi 会话文件：恢复时接着用的那个，或这次新建的（pi 第一轮写完才落盘，之前是 None）。"""
        if self.session_file is not None:
            return self.session_file
        if self.session_dir is None or not self.session_dir.is_dir():
            return None
        new = [f for f in self.session_dir.glob("*.jsonl") if f not in self.session_before or f.stat().st_mtime > self.session_before[f]]
        return max(new, key=lambda f: f.stat().st_mtime) if new else None

    def guides_in_session(self) -> tuple[set[str], int]:
        """主 agent 的会话文件里已给过规范的工具（最后一次压缩之后的）和压缩次数（session_guides）。"""
        return session_guides(self._session_path())

    def guide_due(self, use: ToolUse | SubagentUse, turn: int | None = None) -> str | None:
        """这次调用该先给规范吗：工具有规范、这个会话里（最后一次压缩之后）还没给过 → 返回规范全文（记为已给），否则 None（照常执行）。
        同一轮（turn 相同，模型在看到规范之前一起发出的几次调用）里同一工具的其余调用也只给规范。"""
        text = self.guide_text(use)
        if not text or use.name in self.loaded:          # 加载的：load_tool 的结果里已经给了规范
            return None
        given, n = self.guides_in_session()
        if self._compactions is not None and n != self._compactions:     # 运行中压缩过：这一段给过的也已不在上下文里
            self._guided.clear()
        self._compactions = n
        if use.name in self._guided:
            if turn is None or self._guided[use.name] != turn:
                return None
        elif use.name in given:
            return None
        self._guided[use.name] = turn
        return f"{GUIDE_MARK.format(tool=use.name)}\n{text}\n\n{GUIDE_END}"

    # ---- 调用 ----
    async def _activity(self, state: str, subagent: str | None = None, tool: str | None = None) -> None:
        """报运行中的状态（think / tool / untrusted / loading / ask）；子代理调用期间另带它的名字、kind、isolated，工具调用期间另带工具名（入口自己决定显示成什么，平台不按名字挑）。"""
        if self.ref and self.progress_id:
            d = self.callees.get(subagent) if subagent else None
            extra: dict[str, Any] = {"subagent": d.name, "kind": d.kind, "isolated": d.isolated} if d else {}
            if tool:
                extra["tool"] = tool
            await hub_client.call("activity", id=self.progress_id, state=state, **extra)

    async def call(self, name: str, args: dict[str, Any], origin: str = "agent", images: list[dict[str, str]] | None = None,
                   turn: int | None = None, repaired: list[str] | None = None, call_id: str | None = None) -> Any:
        """images：传一个列表进来就收下可信工具结果里的图片块（{data, mimeType}，/call 回复里交给 pi）。
        turn：pi 的轮次（同一条模型回答里的几次调用相同）：给规范的那一轮里同一工具的其余调用也只给规范、不执行。
        repaired：pi 扩展在校验前修过的参数格式（字符串换成数组 / 对象、字段名去空白），记进审计。
        call_id：pi 的调用编号（同 toollog 的 id），没给就生成一个；记进审计，子代理的每一步也记它（网页按它把步骤挂到对应的调用下面）。"""
        t0 = time.monotonic()
        rec: dict[str, Any] = {"ev": "call", "origin": origin, "tool": name, "call_id": call_id or secrets.token_hex(6),
                               "args_chars": len(json.dumps(args, ensure_ascii=False))}
        if repaired:
            rec["repaired"] = [str(x)[:120] for x in repaired[:10]]
        callee = self.callees.get(name) if name not in self.tools else None   # 子代理（主模型直接调用）
        use: ToolUse | SubagentUse | None = (self.tools[name][0] if name in self.tools else
                                             next((u for u in self.task.subagents if u.name == name), self.loaded.get(name)) if callee else None)
        if origin in ("agent", "resume") and use is not None:
            await self._activity(self._state(use), *((use.name, None) if isinstance(use, SubagentUse) else (None, use.name)))
        if origin == "resume":                           # 续跑前平台替模型调用：主模型的 toollog 里没有这次调用，网页按这条与结束时那条显示（子代理的步骤边做边挂在下面）
            self._audit({"ev": "call_start", "origin": origin, "tool": name, "call_id": rec["call_id"], **({"subagent": name} if callee is not None else {}),
                         "args": self._audit_args(args)})
        self.inflight += 1
        if use is not None:
            self._inflight_tool[use.name] = self._inflight_tool.get(use.name, 0) + 1
        self.last_activity = time.time()
        agent = callee is not None
        outcome: Any = "调用被取消（运行结束）"
        if agent:                                        # 子代理时间：调用进来到出去（同一时刻有几个在途只算一次）；就地等卡的时间在 _wait_round 里另算
            self._off_clock()
        try:
            if callee is not None:                       # 子代理不占网关锁（同工具里的并发项）；换模型时 _model_for 才拿锁
                rec["subagent"] = name
                result = await self._call_subagent(callee, use, args, origin, rec, images, turn)  # pyright: ignore[reportArgumentType]
                self._record_result(callee, result, not (isinstance(result, dict) and result.get("timeout")))
            else:                                        # 工具：持网关锁跑；就地等问题卡 / 审批卡时 _wait_round 放开、等完拿回（rec["_locked"] 记着持没持）
                await self._lock.acquire()
                rec["_locked"] = True
                try:
                    result = await self._call(name, args, origin, rec, images, turn)
                finally:
                    if rec.pop("_locked"):
                        self._lock.release()
            rec["ok"] = True
            outcome = result
            return result
        except Exception as e:
            raw = None if isinstance(e, GatewayError) else f"{type(e).__name__}: {str(e)[:150]}"
            if not isinstance(e, GatewayError):
                e = bug(e, "网关")
            rec.update(ok=False, code=e.code, message=e.message[:300])
            if e.code == "BUG" and origin == "agent" and name not in self.bugs:   # 只算 agent 调的工具；运行器收尾时的调用失败另有报错
                self.bugs.append(name)
                self.bug_notes[name] = raw or e.message[:150]   # 给 Joy 的提醒带上异常类型与消息（不给模型）
            if e.code in ("TOOL_NOT_ALLOWED", "SCOPE", "LIMIT"):
                self.rejections += 1
            outcome = e.message
            raise e from None
        finally:
            if rec.get("hooked"):                         # 调用执行了（过了 pre_call）：成功、失败、被取消都跑 post_call（超时算失败）
                await self._post_call(use, name, rec, bool(rec.get("ok")) and not rec.get("timeout"), outcome)
            if agent:
                self._on_clock()
            self.inflight -= 1
            if use is not None:
                self._inflight_tool[use.name] -= 1
            self.last_activity = time.time()
            rec["seconds"] = round(time.monotonic() - t0, 3)
            if origin == "resume":
                rec["result"] = (outcome if isinstance(outcome, str) else json.dumps(outcome, ensure_ascii=False, default=str))[:3000]
            self._audit(rec)
            if origin in ("agent", "resume") and use is not None:
                await self._activity("think")

    def _state(self, use: ToolUse | SubagentUse) -> str:
        """调用期间的运行状态：子代理按隔离与否（📡 / 🛠️），工具按 activity_state。"""
        if isinstance(use, SubagentUse):
            return "untrusted" if self.callees[use.name].isolated else "tool"
        return activity_state(use)

    def _hook_payload(self, use: ToolUse | SubagentUse, name: str, args: dict[str, Any], subagent: str | None = None) -> dict[str, Any]:
        """hook 的输入：调用子代理时 tool 是 null、subagent 与 function 是子代理名；子代理循环里调它的工具时 subagent 是那个子代理。"""
        if isinstance(use, SubagentUse):
            return {"task": self.task.name, "run_id": self.run_dir.name, "tool": None, "subagent": use.name, "function": name, "args": args}
        return {"task": self.task.name, "run_id": self.run_dir.name, "tool": use.name, "subagent": subagent, "function": name, "args": args}

    def _call_owners(self, use: ToolUse | SubagentUse) -> list[hooks.Owner]:
        """调用的 hook 主人：先本任务（任务 yaml 的 pre_call / post_call 管本任务运行里的每次调用），再被调用的子代理或工具自己（它的检查最后说了算）。"""
        owner = hooks.subagent_owner(use.name) if isinstance(use, SubagentUse) else hooks.tool_owner(use.name)
        return [o for o in (hooks.task_owner(self.task), owner) if o is not None]

    async def _pre_call(self, use: ToolUse | SubagentUse, name: str, args: dict[str, Any], rec: dict[str, Any], subagent: str | None = None) -> dict[str, Any]:
        """工具或子代理的 pre_call hook（control_center/hooks.py）：退出码 2 拦下这次调用（stderr 原文交给模型；hook 这期间给本运行发了审批卡、
        卡还没处理的，同工具发的卡一样就地等（_await_cards），等不到就让出运行），stdout {"args": {...}} 改写参数。之后这次调用算执行了：结束时跑 post_call。返回（可能改写过的）参数。"""
        rec["hooked"] = True
        owners = self._call_owners(use)
        payload = self._hook_payload(use, name, args, subagent)
        t0 = time.time()
        got = await hooks.afire("pre_call", owners, payload, self.run_dir, self.work_dir)
        if got.blocked is not None and await asyncio.to_thread(acquire_permission.new_for_run, f"{self.task.name}/{self.run_dir.name}", t0):
            call = {"kind": "hook", "tool": use.name, "subagent": isinstance(use, SubagentUse), "function": name, "args": args, "asked": args}
            text = await self._await_cards(got.blocked, t0, rec, call)   # hook 发了审批卡还没等到：同工具一样就地等，等不到就让出
            if not (ok := rec.pop("approved_inline", None)):
                rec.pop("hooked")
                raise GatewayError("HOOK_BLOCKED", text)
            got = await hooks.afire("pre_call", owners, {**payload, "approved_card": ok}, self.run_dir, self.work_dir)   # 就地等到 Joy 同意：带着那张卡再跑一遍
        if got.blocked is not None:
            rec.pop("hooked")
            raise GatewayError("HOOK_BLOCKED", got.blocked)
        if got.args is not None:
            rec["args_rewritten"] = True
            args = got.args
        rec["_args"] = args                       # post_call 的输入用（不进审计日志）
        return args

    async def _post_call(self, use: ToolUse | SubagentUse | None, name: str, rec: dict[str, Any], ok: bool, result: Any, subagent: str | None = None) -> None:
        """post_call hook：成功失败都调；输入带结果、成败与 inflight（这个工具在途的别的调用数）。
        hook 出错只记日志、私信 Joy，不影响结果。"""
        if use is None:
            return
        payload = {**self._hook_payload(use, name, rec.pop("_args", {}), subagent), "ok": ok, "result": result, "inflight": self._inflight_tool.get(use.name, 1) - 1}
        try:
            await hooks.afire("post_call", self._call_owners(use), payload, self.run_dir, self.work_dir)
        except Exception as e:  # noqa: BLE001 — hook 的意外不影响这次调用的结果
            self._audit({"ev": "hook_error", "tool": use.name, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def _off_clock(self) -> None:
        """不算主模型时间的一段开始（子代理调用、就地等问题卡 / 审批卡）；几段同时在途只算一次（可以嵌套）。"""
        if not self._sub_running:
            self._sub_since = time.time()
        self._sub_running += 1

    def _on_clock(self) -> None:
        self._sub_running -= 1
        if not self._sub_running:
            self.sub_seconds += time.time() - self._sub_since

    async def _call(self, name: str, args: dict[str, Any], origin: str, rec: dict[str, Any], images: list[dict[str, str]] | None = None,
                    turn: int | None = None) -> Any:
        if self.wrapup and origin == "agent":
            raise GatewayError("BUDGET", WRAPUP_NOTE)
        if name not in self.tools:
            if (o := self._loadable.get(name)) is not None:
                raise GatewayError("NOT_LOADED", f"{name} 还没加载：先用 load_tool 加载 {o[0]}")
            raise GatewayError("TOOL_NOT_ALLOWED", f"工具 {name} 不在本任务的白名单里")
        if rec["args_chars"] > MAX_ARGS_CHARS:
            raise GatewayError("LIMIT", f"参数过大（{rec['args_chars']} 字符，上限 {MAX_ARGS_CHARS}）：大段内容先写进工作目录下的文件，参数里只传路径，或分几次调用")
        use, _t = self.tools[name]
        if origin == "agent" and use.name in self.revoked():
            raise GatewayError("TOOL_NOT_ALLOWED", f"工具 {use.name} 已被撤销，本次运行不能再用")
        if use.name == LOADER:
            return await self._load_tool(str(args.get("name", "")).strip())
        if origin == "agent" and (guide := self.guide_due(use, turn)) is not None:
            rec["guide"] = True
            return guide
        if origin == "agent" and (self.run_dir / "ask.json").exists() and (done := await asyncio.to_thread(acquire_permission.take_executed, self.run_dir, use.name, name, args)) is not None:
            rec["executed_earlier"] = True                # 挂起期间 Joy 同意后平台已替这次调用执行过：交回那次的结果，不重复执行
            return done
        asked = args
        args = await self._pre_call(use, name, args, rec)
        self.calls += 1
        if use.name == acquire_permission.TOOL:
            if self.ask_waiting or self.ask_yielded:
                raise GatewayError("LIMIT", "这一轮已经发出一组问题：相关的问题放进同一次 ask_user 调用（一轮最多 6 题），等这轮答完再问下一轮")
            self.ask_waiting = True
            try:
                return await self._ask(name, args, use, rec)
            finally:
                self.ask_waiting = False

        if use.name == reply.TOOL:                    # agent 自己发了几条：运行结束通知据此决定补不补发最终回答
            if origin == "agent":
                self.agent_messages += 1
            rec["message_chars"] = len(str(args.get("text", "")))
            if isinstance(args.get("files"), list):
                rec["files"] = len(args["files"])
        t_call = time.time()
        call = {"kind": "tool", "id": secrets.token_hex(6), "tool": use.name, "function": name, "args": args, "asked": asked}
        try:
            async with self._model_for(use.name, False, activity_state(use)):   # D32：子代理换走了主模型时，工具等它回来再跑
                result = await self._invoke(name, args, use, rec, images, meta={"stb_call": call["id"]})
            waited = await asyncio.to_thread(acquire_permission.waited, f"{self.task.name}/{self.run_dir.name}", t_call, acquire_permission.WAIT_S)
            if waited:                                  # 工具就地等审批卡（如 shell 逐条审批）的时间不算主模型时间（发卡算）
                self.sub_seconds += waited
                rec["card_wait_seconds"] = round(waited, 1)
        except Exception as e:
            if not isinstance(e, GatewayError):
                e = bug(e, "网关")
            if e.code == "TOOL_ERROR" and await asyncio.to_thread(acquire_permission.new_for_run, f"{self.task.name}/{self.run_dir.name}", t_call):
                return await self._await_cards(e.message, t_call, rec)   # 工具因为发了审批卡先报错（stbcc-docker 申请磁盘额度发的审批卡）：照样就地等卡，处理结果接在报错后面
            raise e from None
        result = await self._await_cards(result, t_call, rec, call)
        if ok := rec.pop("approved_inline", None):     # 工具已经不在等、网关就地等到 Joy 同意：照原样再调用一次，这次的结果就是调用结果
            try:
                async with self._model_for(use.name, False, activity_state(use)):
                    result = await self._invoke(name, args, use, rec, images, meta={"stb_call": secrets.token_hex(6), "stb_approved": ok})
            except GatewayError as e:
                await self._note_cards([ok], {"ok": False, "text": e.message})
                raise
        if use.name == skills.TOOL and name in ("load_skill", "import_skill") and isinstance(result, str) and (m := LOADED_SKILL_RE.match(result)):
            self._record_load(f"skill:{m.group(1)}", result)
        ids = await asyncio.to_thread(acquire_permission.approved_of_call, f"{self.task.name}/{self.run_dir.name}", call["id"], t_call)
        await self._note_cards(ids, {"ok": True, "text": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)})
        return result

    # ---- 按需加载（load-tool）----
    async def _load_tool(self, name: str) -> str:
        """load_tool：name 在能加载的范围里就加载（_load），结果第一行「已加载 X，现在可以直接调用…」、说明、用法规范、函数与参数表；
        不在范围里报原因（GatewayError）。已在本次运行里的照样交回这些（参数表记不清时再调一次，不重起）。"""
        items = await asyncio.to_thread(searchable)
        if name not in items:
            spec = TOOLS.get(name)
            why = (f"{name} 就是 load_tool 所在的平台工具，不用加载" if name == LOADER
                   else f"{name} 只给子代理用，主模型不能加载" if spec is not None and spec.usable_by == "subagent"
                   else f"{name} 是平台自带的工具，任务 yaml 列了才有，不能加载" if spec is not None and is_platform(spec)
                   else f"没有叫 {name} 的工具或子代理：name 照系统提示里的目录写")
            raise GatewayError("TOOL_NOT_ALLOWED", why)
        it = items[name]
        if name in {u.name for u in (*self.task.tools, *self.task.subagents)}:   # 本来就常驻（任务 yaml 列了）：结果里已带规范，之后第一次调用不再先给
            self._guided[name] = -1
        await self._load(name)
        fns = self._functions_of(name)
        head = f"已加载 {name}，现在可以直接调用 {name}" if it["kind"] == "subagent" else f"已加载 {name}，现在可以直接调用：{'、'.join(f['name'] for f in fns)}"
        parts = [head, f"说明：{it['description']}", *([f"用法规范：\n{it['guide']}"] if it.get("guide") else []),
                 "函数与参数（类型后的 = 是默认值，不写就用它）：\n" + "\n".join(signature(f) for f in fns)]
        return self._record_load(name, "\n\n".join(parts))

    def _record_load(self, key: str, text: str) -> str:
        """记下一次加载的结果原文（同一个再加载就换成新的），写进运行目录的 loads.json（续跑、下一条私信用）。返回 text。"""
        self.load_texts.pop(key, None)
        self.load_texts[key] = text
        _write_json(self.run_dir / LOADS_FILE, {"texts": self.load_texts})
        return text

    async def carry(self, earlier: dict[str, str]) -> str:
        """私信任务开跑时：上下文起点之后加载过的（runner.earlier_loads）这次接着有效——工具 / 子代理重新加载（列了 load-tool 才行，结果按现在的定义重写），
        skill 用原来的结果（列了 skills 才行）；都记进本次的 loads.json（下一次、压缩后补回照样有）。返回放在用户消息最前面的一段（没有就是空）。"""
        names = {u.name for u in self.task.tools}
        texts = []
        for key, text in earlier.items():
            try:
                if key.startswith("skill:"):
                    if skills.TOOL in names:
                        texts.append(self._record_load(key, text))
                elif LOADER in names:
                    texts.append(await self._load_tool(key))
            except GatewayError as e:                    # 不能再加载了（删了、改成只给子代理用）：跳过，模型要用时照常报错
                self._audit({"ev": "carry_error", "name": key, "code": e.code, "message": e.message[:300]})
        return "\n\n".join([CARRY_HEAD, *texts]) if texts else ""

    def compacted(self) -> str:
        """pi 压缩完了（扩展 POST /compacted）：这时加载过的全部（压缩前的）拼成一段存进 restored.txt，扩展之后每次调用模型前把它插在压缩摘要后面。"""
        text = "\n\n".join([RESTORED_HEAD, *self.load_texts.values()]) if self.load_texts else ""
        (self.run_dir / RESTORED_FILE).write_text(text)
        self._audit({"ev": "compacted", "restored": list(self.load_texts)})
        return text

    def restored(self) -> str:
        try:
            return (self.run_dir / RESTORED_FILE).read_text()
        except OSError:
            return ""

    async def _load(self, name: str) -> None:
        """把可搜索范围（registry.searchable）里的工具 / 子代理 name 加进本次运行：工具起它的 MCP 进程（同任务的工具，整个运行开着）、函数加进工具表；
        子代理加进 callees（档位和主任务不同的照样换模型）。已在本次运行里的（任务 yaml 列了、加载过）什么都不做。不能加载抛 GatewayError。"""
        if name in self.loaded or name in self.callees or name in {u.name for u, _ in self.tools.values()}:
            return
        it = (await asyncio.to_thread(searchable)).get(name)
        if it is None:
            raise GatewayError("TOOL_NOT_ALLOWED", f"{name} 不能加载：只有 tools/ 下 usable_by: universal 的工具和 subagents/ 下的子代理能加载")
        if it["kind"] == "subagent":
            if name in self.tools:
                raise GatewayError("TOOL_CONFLICT", f"工具名冲突：{name}")
            try:
                d = subagents.load_def(name)
            except subagents.SubagentError as e:
                raise GatewayError("SUBAGENT", str(e)) from None
            if not d.inherit:
                models = load_models()
                self.task_model = self.task_model or self.task.profile_name(models)
                self._sub_profile(d, models)
            self.callees[name], self.loaded[name] = d, SubagentUse(name=name)
        else:
            defs = self._fn_defs[name] = await self._loaded_tools.open(name)   # 起不来报平台故障
            self.sessions[name] = self._loaded_tools.live[name]
            if clash := [t["name"] for t in defs if t["name"] in self.tools or t["name"] in self.callees]:
                raise GatewayError("TOOL_CONFLICT", f"工具名冲突：{clash}")
            use = self.loaded[name] = ToolUse(name=name)
            for t in defs:
                self.tools[t["name"]] = (use, t)
        self._audit({"ev": "load", "name": name, "kind": it["kind"]})

    async def _find_loadable(self, errlog: Any) -> None:
        """任务列了 load-tool 时开跑算一次 _loadable：可搜索范围里还不在本次运行的全部函数定义（pi 扩展开跑时全部注册，不发给模型）。
        工具的定义先看磁盘缓存（cached_defs），没有就起一次拿到、写进缓存、随即关掉（同 _sub_functions）；子代理用它的参数。起不来的记审计、跳过。"""
        items = await asyncio.to_thread(searchable)
        have = {u.name for u, _ in self.tools.values()} | set(self.callees)

        async def defs_of(name: str) -> list[dict[str, Any]]:
            if items[name]["kind"] == "subagent":
                d = subagents.load_def(name)
                return [{"name": d.name, "description": d.description + SUBAGENT_NOTE, "parameters": d.parameters()}]
            if (defs := await asyncio.to_thread(cached_defs, name)) is None:
                async with AsyncExitStack() as stack:
                    defs = (await self._open_tool(name, errlog, stack))[1]
                await asyncio.to_thread(save_defs, name, defs)
            return [{"name": t["name"], "description": t["description"], "parameters": inline_refs(t["inputSchema"])} for t in defs]

        names = [n for n in items if n not in have]
        for n, fns in zip(names, await asyncio.gather(*map(defs_of, names), return_exceptions=True)):
            if isinstance(fns, BaseException):
                self._audit({"ev": "loadable_error", "name": n, "error": f"{type(fns).__name__}: {str(fns)[:200]}"})
                continue
            for f in fns:
                if f["name"] not in self.tools and f["name"] not in self.callees:
                    self._loadable.setdefault(f["name"], (n, f))

    def loadable_defs(self) -> list[dict[str, Any]]:
        """还没加载的能加载函数（GET /tools 的 loadable）。"""
        return [f for n, f in self._loadable.values() if n not in self.loaded]

    def _functions_of(self, name: str) -> list[dict[str, Any]]:
        """本次运行里工具 / 子代理 name 的函数定义 {name, description, parameters}：工具每个函数一项（inline_refs 过），子代理一项。"""
        fns = [{"name": n, "description": t["description"], "parameters": inline_refs(t["inputSchema"])} for n, (u, t) in self.tools.items() if u.name == name]
        if not fns and (d := self.callees.get(name)):
            fns = [{"name": d.name, "description": d.description + SUBAGENT_NOTE, "parameters": d.parameters()}]
        return fns

    def loaded_defs(self) -> list[dict[str, Any]]:
        """本次运行已加载的全部函数定义（GET /tools 的 loaded：pi 扩展注册它们，但不放进发给模型的工具列表）。"""
        return [f for n in self.loaded for f in self._functions_of(n)]

    async def restore_loaded(self) -> None:
        """续跑（新网关）：上一段加载过的（restore）重新加载；不能再加载的（工具删了、改成只给子代理用）记进审计、跳过，模型再调用时照常报错。"""
        for name in self.restore:
            try:
                await self._load(name)
            except GatewayError as e:
                self._audit({"ev": "load_error", "name": name, "code": e.code, "message": e.message[:300]})

    async def _note_cards(self, ids: list[str], out: dict[str, Any]) -> None:
        """这次调用的卡 Joy 同意后执行完了：一句结果写到卡片上，各入口刷新。"""
        for cid in ids:
            if await asyncio.to_thread(acquire_permission.note_executed, cid, out):
                await hub_client.call("card_refresh", id=cid)

    # ---- 子代理（主模型直接调用）----
    async def _call_subagent(self, d: subagents.SubagentDef, use: SubagentUse, args: dict[str, Any], origin: str, rec: dict[str, Any],
                             images: list[dict[str, str]] | None = None, turn: int | None = None) -> Any:
        """主模型调用子代理 d（主模型这次调用一直挂着）：规范先返回 → 查参数 → pre_call hook（所有子代理一样，在换模型之前：便宜的检查放这里，
        不过就拦下、不白卸主模型）→ 按铁律换模型（_model_for：档位和主模型不同就卸主模型、装它的，跑完装回）→ 单次上限里跑
        （_within：等审批卡的时间顺延；换模型的时间不算）→ 交回。到点返回超时说明。"""
        if self.wrapup and origin == "agent":
            raise GatewayError("BUDGET", WRAPUP_NOTE)
        if rec["args_chars"] > MAX_ARGS_CHARS:
            raise GatewayError("LIMIT", f"参数过大（{rec['args_chars']} 字符，上限 {MAX_ARGS_CHARS}）：大段内容先写进工作目录下的文件，用 files 附上或在 prompt 里写路径")
        if origin == "agent" and use.name in self.revoked():
            raise GatewayError("TOOL_NOT_ALLOWED", f"子代理 {use.name} 已被撤销，本次运行不能再用")
        if use.name == LOADER:
            return await self._load_tool(str(args.get("name", "")).strip())
        if origin == "agent" and (guide := self.guide_due(use, turn)) is not None:
            rec["guide"] = True
            return guide
        self._subagent_args(d, args)
        if origin == "agent" and (self.run_dir / "ask.json").exists() and (done := await asyncio.to_thread(acquire_permission.take_executed, self.run_dir, use.name, d.name, args)) is not None:
            rec["executed_earlier"] = True
            return done
        args = await self._pre_call(use, d.name, args, rec)
        self._subagent_args(d, args)                     # pre_call 改写过的参数照样要合格
        self.calls += 1
        async with self._model_for(d.name, True, self._state(use)):
            t0 = time.monotonic()                        # 单次上限从换好模型算起
            if d.timeout is None:                        # timeout: none：没有单次上限，只按停滞规则结束卡住的调用（_unstalled）
                watch = {"t": time.monotonic(), "busy": 0}
                return await self._unstalled(self._run_callee(d, args, rec, images, watch), watch, d.name)
            try:
                return await self._within(self._run_callee(d, args, rec, images), d.timeout, t0, time.time())
            except TimeoutError:
                return self._timed_out(d.name, d.timeout, t0, rec)

    def _subagent_args(self, d: subagents.SubagentDef, args: dict[str, Any]) -> None:
        """主模型传给子代理的参数：按 d.parameters() 查（prompt 非空；agent 的 files 是 sparky 家目录或工作目录下已有文件的绝对路径；generator / engine 的 inputs 按声明）。不合格抛 GatewayError（调用有误）。"""
        try:
            reader.check(args, d.parameters())
        except reader.ReaderError as e:
            raise GatewayError("ARGS", f"子代理 {d.name} 的参数不对：{e}（参数说明见它的函数定义）") from None
        if not str(args.get("prompt") or "").strip():
            raise GatewayError("ARGS", "prompt 不能为空：写清要子代理做什么、回答哪些问题")
        roots = (paths.sparky_home().resolve(), self.work_dir.resolve())
        root = roots[0]
        for f in args.get("files") or []:
            try:
                host = Path(str(f)).resolve()
                ok = str(f).startswith("/") and any(r in host.parents for r in roots) and host.is_file()
            except (OSError, ValueError):
                ok = False
            if not ok:
                raise GatewayError("ARGS", f"files 要写 {root} 下已有文件的绝对路径，这个不存在或不在那里：{f}")

    async def _run_callee(self, d: subagents.SubagentDef, args: dict[str, Any], rec: dict[str, Any], images: list[dict[str, str]] | None,
                          watch: dict[str, Any] | None = None) -> Any:
        """跑子代理 d：agent 另起沙箱容器跑 pi（_run_agent_pi），generator / engine 交给它的程序；结果过 subagent_run.finish（隔离核对、output.py）。
        交回的结果不合格且重写用完、output.py 出错：子代理出错（READER_FAILED）。
        watch：停滞规则用的动静记录（_unstalled）——模型有输出、工具调用开始 / 结束时更新 t，在途的工具调用（generator / engine 的程序调用算一个）记在 busy。"""
        watch = watch if watch is not None else {"t": 0.0, "busy": 0}
        inputs = {k: v for k, v in args.items() if k not in ("prompt", "files")}

        async def done(text: str, raw: dict[str, str], last: bool, labels: dict[str, str] | None = None) -> Any:
            return await subagent_run.finish(d, text, raw, last, prompt=str(args["prompt"]), inputs=inputs,
                                             run_dir=self.run_dir, work_dir=self.work_dir, run_id=self.run_dir.name, labels=labels)  # pyright: ignore[reportArgumentType]
        try:
            if d.kind == "agent":
                return self._result_images(await self._run_agent_pi(d, args, rec, watch), images, rec)
            last: dict[str, Any] = {}

            async def call(a: dict[str, Any]) -> str:
                watch["busy"] += 1
                try:
                    res = await (await self._callee_session(d)).call_tool(d.name, a)
                finally:
                    watch["busy"] -= 1
                    watch["t"] = time.monotonic()
                text = "\n".join(c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text")
                if getattr(res, "is_error", False):
                    await self._tool_error(d.name, d.name, text)
                    raise GatewayError("TOOL_ERROR", clip_error(text) or f"子代理 {d.name} 的程序返回了错误但没有任何说明文字")
                last["content"] = getattr(res, "content", [])
                rec["result_chars"] = len(text)
                return text
            result = await subagent_run.program_loop(d, args, call=call, done=done)
            if isinstance(result, dict) and isinstance(result.get("usage"), dict):   # 程序交的 usage（它那些模型调用的合计）：记进这次运行的 token 用量，不交给主模型
                self.meter.add(result.pop("usage"))
            if not d.isolated and isinstance(result, str):   # 生成的图片预览（同可信工具结果里的图片块）；隔离的只交核对过的答案
                result += self._images(last.get("content", []), images, rec)
            return self._result_images(result, images, rec)
        except subagent_run.SubagentRunError as e:
            raise GatewayError("READER_FAILED", f"{d.name}：{e}") from None

    async def _run_agent_pi(self, d: subagents.SubagentDef, args: dict[str, Any], rec: dict[str, Any], watch: dict[str, Any]) -> Any:
        """agent 子代理 d 的一次调用：同主 agent，另起一个沙箱容器跑 pi（系统提示 subagent_run.system_text、用户消息是主模型的 prompt、它自己档位的模型），
        pi 用这次调用的令牌连网关，只拿到 d 自己的函数（_serve_sub）。pi 正常结束后最后一条回复交 finish：被打回就在同一会话里接着要它重交（最多 MAX_REJECTS 次）。
        会话目录与状态（subagent_run.open_session）：上一次没交回就中断了的会话这次接着用（用户消息前面加 RESUMED_NOTE），交回了的标 done，下次开新会话。
        这次调用的工具进程（_CallTools）只活这一次子代理调用：第一次用到时起，调用结束（交回、出错、超时被取消）时一起关，
        进程里开着的东西（浏览器容器）随之关掉；任务自己也用的工具另起一份，和任务的那份互不影响。"""
        prof = self.sub_profiles.get(d.name, self.profile)
        if not (called := call_name(prof)):
            raise GatewayError("PLATFORM", f"子代理 {d.name} 的模型端点 {prof.get('endpoint')} 的 /models 里没有 {prof.get('model_path')}")
        model_url = await self.relay_url(str(prof["endpoint"]))
        folder, state, resumed = subagent_run.open_session(self.run_dir, d.name, self._sub_busy)
        self._sub_busy.add(folder.name)
        state["prompts"].append(str(args["prompt"]))
        subagent_run.save_state(folder, state)
        rec.update(session=folder.name, **({"resumed": True} if resumed else {}))
        inputs = {k: v for k, v in args.items() if k not in ("prompt", "files")}
        token = secrets.token_hex(32)
        result: Any = None
        tools = _CallTools(self)
        try:
            fns = [{"name": n, "description": t["description"] + (GUIDE_NOTE if TOOLS[tool].guide else ""), "parameters": inline_refs(t["inputSchema"])}
                   for n, (tool, t) in (await self._sub_functions(d, tools.open)).items()]
            sc = SubCall(d, folder, state, fns, tools.session, rec, watch, bool(prof.get("vision")))
            self._subcalls[token] = sc
            models_json = folder / "models.json"
            models_json.write_text(json.dumps(pi_models_json(prof, model_url, called, d.effort)))
            text = subagent_run.user_text(str(args["prompt"]), list(args.get("files") or []))
            text = subagent_run.RESUMED_NOTE + text if resumed else text
            nudged = False
            while True:
                out = await self._launch_pi(sc, token, models_json, called, text)
                reply_text = await self._pi_done(sc, out)
                if sandbox.last_empty(out):           # 最后一条只有 thinking（没文字、没调工具）：同一会话补一次「继续」，仍空就交回无回答
                    if not nudged:
                        nudged, text = True, subagent_run.EMPTY_NOTE
                        continue
                    state["status"] = "done"
                    raise subagent_run.SubagentRunError(f"无回答：补问一次「继续」后最后一条回复仍没有文字、也没有调用工具（会话 {folder}）")
                try:
                    # 接着原会话时，前几次调用写的问题也算主模型问的（隔离答案的 question 核对）
                    result = await subagent_run.finish(d, reply_text, state["raw"], state["rejects"] >= subagent_run.MAX_REJECTS,
                                                       prompt="\n\n".join(state["prompts"]), inputs=inputs, run_dir=self.run_dir,
                                                       work_dir=self.work_dir, run_id=self.run_dir.name, labels=state["labels"])
                except subagent_run.Reject as r:
                    state["rejects"] += 1
                    subagent_run.save_state(folder, state)
                    text = subagent_run.REJECT_NOTE.format(why=r.why)
                    continue
                except subagent_run.SubagentRunError:
                    state["status"] = "done"           # 交回过了（只是不合格）：下次开新会话
                    raise
                state["status"] = "done"
                return result
        finally:
            self._subcalls.pop(token, None)
            self._sub_busy.discard(folder.name)
            subagent_run.save_state(folder, state)
            await tools.close()


    async def _launch_pi(self, sc: SubCall, token: str, models_json: Path, model_id: str, prompt: str) -> Path:
        """起一次子代理的 pi 容器（同一会话：有会话文件就 --session 接着它），跑到它自己退出，返回这次的事件流文件。
        被取消（单次上限、卡住、运行收尾 abort / stop）时 sandbox.run_pi 停掉容器；不是这次调用自己被取消（运行收尾）就按 STOPPED 交回。"""
        st = sc.state
        st["launches"] += 1
        subagent_run.save_state(sc.folder, st)
        out = sc.folder / f"pi-output-{st['launches']}.jsonl"
        name = f"stb-sub-{self.task.name}-{self.run_dir.name}-{sc.folder.name}-{st['launches']}"
        cmd = sandbox.pi_command(name=name, out_dir=sc.folder, settings=sandbox.pi_settings_file(self.run_dir), work_dir=self.work_dir,
                                 models_json=models_json, gateway_url=self.url, token=token, model=f"{PI_PROVIDER}/{model_id}",
                                 net_args=["--network", sandbox.NET_TOOLS_ONLY], system=subagent_run.system_text(sc.d, self.work_dir), prompt=prompt,
                                 session_dir=sc.folder / "session", session_file=subagent_run.session_file(sc.folder), effort=sc.d.effort)
        run = asyncio.ensure_future(sandbox.run_pi(cmd, name, out, sc.folder / "pi.stderr.log", stdin=sc.folder / sandbox.PROMPT_FILE))
        self._pi_runs.add(run)
        beat = asyncio.ensure_future(self._pi_heartbeat(out, sc.watch))
        try:
            sc.rec["exit_code"] = await run
        except asyncio.CancelledError:
            if (me := asyncio.current_task()) is not None and me.cancelling():
                raise
            raise GatewayError("STOPPED", "运行已在收尾，子代理停下") from None
        finally:
            self._pi_runs.discard(run)
            beat.cancel()
        return out

    @staticmethod
    async def _pi_heartbeat(out: Path, watch: dict[str, Any]) -> None:
        """子代理的 pi 在动（事件流文件有更新，流式输出时 sandbox._pump 每秒刷新修改时间）就更新停滞判定的 watch（_unstalled）。"""
        last = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                m = out.stat().st_mtime
            except OSError:
                continue
            if m > last:
                last, watch["t"] = m, time.monotonic()

    async def _pi_done(self, sc: SubCall, out: Path) -> str:
        """子代理的 pi 退出了：token 用量记进 meter；运行在收尾、运行器要暂停、它发的卡等不到让出（STOPPED）、模型报错、没正常结束（平台故障）都抛出去
        （会话留着 open，下次调用接着）；正常结束返回它最后一条回复。"""
        text, usage, ended = sandbox.final_text(out)
        self.meter.add(usage)
        sc.rec["model_calls"] = sc.rec.get("model_calls", 0) + self._assistant_turns(out)
        if self._closing:
            raise GatewayError("STOPPED", "运行已在收尾，子代理停下")
        if self.pause_requested:
            raise GatewayError("STOPPED", f"运行要暂停或停下，子代理 {sc.d.name} 这次调用先停下；续跑时再调用它（接着原来的对话）")
        if self.ask_yielded is not None:
            raise GatewayError("STOPPED", f"子代理 {sc.d.name} 发的卡片还在等 Joy 处理，这次调用先停下；Joy 处理后运行会续跑，到时再调用它（接着原来的对话）")
        rc = sc.rec.get("exit_code")
        if rc == 0 and ended and (err := sandbox.last_error(out)):
            raise GatewayError("PLATFORM", f"子代理 {sc.d.name} 的模型报错：{err[:300]}")
        if rc != 0 or not ended:
            try:
                tail = (sc.folder / "pi.stderr.log").read_text(errors="replace").strip()[-300:]
            except OSError:
                tail = ""
            raise GatewayError("PLATFORM", f"子代理 {sc.d.name} 的 pi 没有正常结束（退出码 {rc}）" + (f"：{tail}" if tail else ""))
        return text

    @staticmethod
    def _assistant_turns(out: Path) -> int:
        """事件流里模型回答的条数（审计记 model_calls）。"""
        n = 0
        try:
            lines = out.read_text(errors="replace").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant"
        return n

    def _sub_stop(self) -> bool:
        """子代理的 pi 要不要在这一轮结束就退出：运行在收尾、运行器要暂停（抢占、/stop、手动暂停），或这次运行有卡等不到让出了。"""
        return self._closing or self.pause_requested or self.ask_yielded is not None

    async def _sub_call(self, sc: SubCall, name: str, args: dict[str, Any]) -> tuple[str, list[dict[str, str]], str | None]:
        """子代理的 pi 调它的一个函数（_sub_tool）：规范按会话给（压缩后重给）；isolated 的结果开头标来源编号〔来源 sN〕，原文与来源存进会话状态；
        图片：档位能看图就随结果交给 pi（isolated 的注明图片不能当引文），看不了图的注明没附上。
        返回 (结果文字, 图片, stale)：stale 是工具给的过时后的替换文字（tool_kit.result，isolated 的同样标来源编号），没给是 None。"""
        given, n = session_guides(subagent_run.session_file(sc.folder))
        if n != sc.compactions:                  # 压缩过：之前给过的规范已不在上下文里
            sc.guided.clear()
            sc.compactions = n
        sc.guided |= given
        text, imgs, meta = await self._sub_tool(sc.d, name, args, sc.guided, sc.rec, sc.session_for, sc.watch)
        stale = meta.get("stale")
        if sc.d.isolated:
            st = sc.state
            sid = f"s{len(st['raw']) + 1}"
            st["raw"][sid], st["labels"][sid] = text, meta.get("source")
            subagent_run.save_state(sc.folder, st)
            text = subagent_run.SOURCE_MARK.format(sid=sid) + text
            stale = stale and subagent_run.SOURCE_MARK.format(sid=sid) + stale
        if imgs and not sc.vision:
            text, imgs = text + subagent_run.IMAGES_SKIPPED.format(n=len(imgs)), []
        elif imgs and sc.d.isolated:
            text += subagent_run.IMAGES_ISOLATED
        return text, imgs, stale

    async def _serve_sub(self, sc: SubCall, method: str, path: str, body: bytes, w: asyncio.StreamWriter) -> None:
        """agent 子代理的 pi 的请求（令牌是这次调用的）：/tools 只给它自己的函数，/call 走 _sub_call，回复可带插话（_sub_take_steers）、不附主模型的剩余预算。"""
        if method == "GET" and path == "/tools":
            return await self._reply(w, 200, {"tools": sc.fns, "builtins": self.builtins(sc.d.tools)})
        if method == "GET" and path == "/state":
            return await self._reply(w, 200, {"pause": self._sub_stop()})
        if method == "POST" and path == "/call":
            try:
                req = json.loads(body or b"{}")
                name, args = str(req.get("name", "")), req.get("arguments") or {}
                if not isinstance(args, dict):
                    raise GatewayError("SCOPE", "arguments 必须是对象")
                text, imgs, stale = await self._sub_call(sc, name, args)
                reply = {"ok": True, "result": text, **({"images": imgs} if imgs else {}), **({"stale": stale} if stale else {}), "pause": self._sub_stop()}
                if not reply["pause"] and (msgs := await self._sub_take_steers(sc)):
                    reply["messages"] = msgs
                return await self._reply(w, 200, reply)
            except GatewayError as e:
                return await self._reply(w, 200, {"ok": False, "error": {"code": e.code, "message": e.message}, "pause": True})
        await self._reply(w, 404, {"error": "not found"})

    @staticmethod
    def _audit_args(v: Any) -> Any:
        """审计里留的子代理调用参数：长字符串截到 2000 字（网页聊天展开看，同主代理 toollog 的显示）。"""
        if isinstance(v, str):
            return v if len(v) <= 2000 else v[:2000] + f"…（共 {len(v):,} 字）"
        if isinstance(v, dict):
            return {k: Gateway._audit_args(x) for k, x in v.items()}
        if isinstance(v, list):
            return [Gateway._audit_args(x) for x in v]
        return v

    async def _sub_tool(self, d: subagents.SubagentDef, fn: str, args: dict[str, Any], guided: set[str], parent: dict[str, Any],
                        session_for: Any, watch: dict[str, Any] | None = None) -> tuple[str, list[dict[str, str]], dict[str, str]]:
        """agent 子代理的一次工具调用：只能调它自己 tools 里的函数；工具有 guide.md 时这次子代理调用里第一次先给规范、不执行；pre_call / post_call hook
        （输入的 subagent 是这个子代理）；ask-user 照常发问题卡就地等；工具发的审批卡就地等，等不到（让出）时回复带 pause，pi 这一轮结束就退出（_pi_done 停下这次子代理调用）。
        session_for(工具名)：这次子代理调用里这个工具的 MCP 会话（_run_agent_pi 第一次用到时起，调用结束关）。
        出错（调用有误、工具报错、平台故障）把说明作为结果交给子代理，让它换个做法；运行收尾、模型切换失败往外抛。结果原文交给子代理。
        返回 (结果文字, 图片, 结构化信息)：工具结果里的图片块按主模型那条路的上限筛（_images：最多 MAX_IMAGES 张、单张 base64 不超过 MAX_IMAGE_B64），
        给不给子代理的模型看由 _sub_call 按它档位的 vision 定；结构化信息是工具给的 source、stale（tool_meta，没给 / 出错为空）。"""
        parent["steps"] = parent.get("steps", 0) + 1
        if self._closing:
            raise GatewayError("STOPPED", "运行已在收尾，子代理停下")
        if fn not in self.sub_fns[d.name]:
            return f"没有函数 {fn}；你能用的：{', '.join(self.sub_fns[d.name]) or '无'}", [], {}
        tool = self.sub_fns[d.name][fn][0]
        use = ToolUse(name=tool)
        rec: dict[str, Any] = {"ev": "subagent_call", "subagent": d.name, "tool": fn, **({"call_id": parent["call_id"]} if parent.get("call_id") else {}),
                               "args_chars": len(json.dumps(args, ensure_ascii=False)), "args": self._audit_args(args)}
        if TOOLS[tool].guide and tool not in guided:
            guided.add(tool)
            rec["guide"] = True
            self._audit(rec)
            return f"{GUIDE_MARK.format(tool=tool)}\n{TOOLS[tool].guide}\n\n{GUIDE_END}", [], {}
        t0, t_call = time.monotonic(), time.time()
        imgs: list[dict[str, str]] = []
        out: Any = None  # pyright: ignore[reportRedeclaration]
        self._inflight_tool[tool] = self._inflight_tool.get(tool, 0) + 1
        watch = watch if watch is not None else {"t": 0.0, "busy": 0}
        watch["busy"] += 1
        out: Any = "调用被取消（运行结束）"
        try:
            args = await self._pre_call(use, fn, args, rec, subagent=d.name)
            if tool == acquire_permission.TOOL:
                out = await self._ask(fn, args, use, rec, await session_for(tool))   # 问题卡用这次子代理调用起的 ask-user（任务不一定列了它）
            else:
                res = await (await session_for(tool)).call_tool(fn, args)
                text = "\n".join(c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text")
                if getattr(res, "is_error", False):
                    await self._tool_error(fn, tool, text)
                    raise GatewayError("TOOL_ERROR", clip_error(text) or f"工具 {fn} 返回了错误但没有任何说明文字")
                rec["result_chars"] = len(text)
                rec.update(tool_meta(res))
                if fn == builder.TRY_SUBAGENT:            # stbcc-builder 装好了子代理的预演副本：网关接着按正式调用真跑它
                    text = await self._try_subagent(args, imgs)
                if len(text) > MAX_RESULT_CHARS:
                    text = text[:MAX_RESULT_CHARS] + self._overflow(fn, text)
                text += self._images(getattr(res, "content", []), imgs, rec)
                out = await self._await_cards(text, t_call, rec)
            rec["ok"] = True
        except GatewayError as e:
            if e.code in ("STOPPED", "MODEL_SWITCH"):
                raise
            rec.update(ok=False, code=e.code, message=e.message[:300])
            out = e.message
        finally:
            if rec.get("hooked"):
                await self._post_call(use, fn, rec, bool(rec.get("ok")), out, subagent=d.name)
            self._inflight_tool[tool] -= 1
            watch["busy"] -= 1
            watch["t"] = time.monotonic()
            rec["seconds"] = round(time.monotonic() - t0, 3)
            if isinstance(out, str):                     # 交给子代理的结果开头（网页聊天展开看）
                rec["result"] = out[:3000]
            self._audit(rec)
        return (out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)), imgs, {k: rec[k] for k in ("source", "stale") if k in rec and (k == "source" or rec.get("ok"))}

    async def _try_subagent(self, args: dict[str, Any], imgs: list[dict[str, str]]) -> str:
        """builder_try_subagent：builder 在开发区里新建 / 改的子代理按正式调用真调一次，一次一个（_trial_lock）。builder-dev 已让 stbcc-builder 查过计划、
        把开发区 subagents/<名>/ 装进预演目录（builder.stage_subagent）；这里不信工具交回的内容，按计划自己再认一遍（只认本计划新建 / 改的，yaml 用批准的、文件用预演副本）。
        跑法：另起一个只有这个子代理的试跑网关（_TrialGateway），走正式调用的同一条路（查参数 → pre_call hook → 换模型 → 跑 → output.py → post_call hook）。
        运行身份照工具试跑（builder_try_tool）的目录：运行目录 data/hub/builder-trial/<计划>/、工作目录它下面的 work/，STB_RUN_REF = builder-trial/<计划>，
        成品在 work/media/<计划>/，ComfyUI 里的副本在 input|output/stb/<计划>/；计划结束时 clean_plan 删试跑目录（ComfyUI 里的副本同正式调用，留着）。
        换模型借本网关的 _model_for（同一把网关锁与切换状态，本运行别的调用照常排队）：试跑的档位临时登记在 sub_models（键 builder-trial/<名>，不和本运行同名的子代理混）。
        调用方 builder 自己的调用在 _model_for 里占着主模型的一份（有子档位时并发调用都计数），它的 pi 正在等这个结果、不发模型请求：
        试跑期间借出这一份（不然要换档位的试跑永远等它），跑完等主模型回来再还上。试跑网关跑完就 stop：程序、pi 容器、工具进程都收掉。
        返回给 builder 的文字（JSON）：ok、result（主模型会拿到的结果）或 error、model、seconds、stderr（这次试跑里程序的标准错误尾部）；预览图放进 imgs。"""
        name = str(args.get("name") or "")
        p = await asyncio.to_thread(builder.active_plan)
        if not p or p.get("status") != "ready" or name not in builder.trial_subagents(p):
            raise GatewayError("ARGS", f"{name} 不是当前计划（已就绪）新建或改的子代理，不能试跑")
        try:
            d = await asyncio.to_thread(builder.trial_subagent_def, p, name)
            models = load_models()
            main = self.task_model or self.task.profile_name(models)
            tier = subagents.profile_name(d, models, main)
        except (builder.BuilderError, subagents.SubagentError, TaskError) as e:
            raise GatewayError("ARGS", str(e)) from None
        inputs, files = args.get("inputs") or {}, args.get("files") or []
        if not isinstance(inputs, dict) or not isinstance(files, list):
            raise GatewayError("ARGS", "inputs 写 {参数名: 值}，files 写工作目录下文件的绝对路径列表")
        root = builder._trial_root(p["id"])
        work, mine = root / "work", self.work_dir.resolve()
        work.mkdir(exist_ok=True)
        copied: list[str] = []
        for f in files:                                   # files（各种子代理都行）：调用方工作目录下的文件拷到试跑工作目录的同一位置；只有 agent 再把拷过去的路径当参数 files 交给子代理
            src = Path(str(f)).resolve()
            if not (str(f).startswith("/") and mine in src.parents and src.is_file()):
                raise GatewayError("ARGS", f"files 要写工作目录 {mine} 下已有文件的绝对路径，这个不存在或不在那里：{f}")
            (work / src.relative_to(mine)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, work / src.relative_to(mine))
            copied.append(str(work / src.relative_to(mine)))
        key = f"{builder.TRIAL_TASK}/{name}"
        out: dict[str, Any] = {"subagent": name, "kind": d.kind, "model": tier if tier != main else "主模型（不换）"}
        got: list[dict[str, str]] = []
        log = root / "gateway.stderr.log"
        async with self._trial_lock:
            lend = bool(self.sub_models)                 # 调用方的调用进 _model_for 时算进了 _sub_calls（这时 sub_models 里还没有试跑的键）
            if tier != main:
                self.task_model = main                    # 换回主模型要知道它的档位（任务的子代理都 inherit 时原来没解析）
                self.sub_models[key], self.sub_profiles[key] = tier, profiles(models)[tier]
            me = asyncio.current_task()
            if me is not None:
                self._sub_tasks.add(me)                   # 运行收尾（abort）连试跑一起取消
            pos = log.stat().st_size if log.is_file() else 0
            t0 = time.monotonic()
            if lend:
                async with self._switch_cond:
                    self._sub_calls -= 1
                    self._switch_cond.notify_all()
            child: _TrialGateway | None = None
            try:
                child = _TrialGateway(self, d, key, root, work)
                await child.start()
                out.update(ok=True, result=await child.call(name, {"prompt": args.get("prompt"), **inputs, **({"files": copied} if copied and d.kind == "agent" else {})},
                                                            origin=builder.TRIAL_TASK, images=got))
            except Exception as e:  # noqa: BLE001 — 试跑的失败（调用有误、hook 拦下、程序报错、平台故障）都交回 builder
                out.update(ok=False, error=(e if isinstance(e, GatewayError) else bug(e, "试跑")).message)
            finally:
                if child is not None:
                    await child.stop()
                    self.meter.add(child.meter.total())
                self.sub_models.pop(key, None)
                self.sub_profiles.pop(key, None)
                if me is not None:
                    self._sub_tasks.discard(me)
                if lend:
                    async with self._switch_cond:
                        await self._switch_cond.wait_for(lambda: self._sub_active is None)
                        self._sub_calls += 1
                for f in (root / subagent_run.SUB_DIR).glob(f"{name}-*"):   # 试跑没交回就停了的 agent 会话不留给下一次试跑接着用
                    if f.is_dir() and (st := subagent_run.load_state(f))["status"] == "open":
                        st["status"] = "done"
                        subagent_run.save_state(f, st)
        imgs.extend(got)
        out["seconds"] = round(time.monotonic() - t0)
        try:
            with log.open("rb") as fh:
                fh.seek(pos)
                out["stderr"] = fh.read().decode(errors="replace")[-3000:]
        except OSError:
            out["stderr"] = ""
        return json.dumps(out, ensure_ascii=False, default=str)

    async def _await_cards(self, result: Any, since: float, rec: dict[str, Any], call: dict[str, Any] | None = None) -> Any:
        """C3：这次调用里工具发的审批卡——就地等 Joy 处理（从发卡算 acquire_permission.WAIT_S 秒，和问题卡一样可提前让出），结果接在工具结果后面；
        等不到就记下让出（运行转 waiting）。call：这次主模型调用（工具调用按 payload.call 对上自己的卡，hook 按 payload.tool）：对上的卡记下这次调用，
        Joy 同意后照原样执行（就地等到同意：卡 id 记进 rec["approved_inline"]，调用方再调用一次；已让出：stbcc-hub 替它执行）。别的调用的卡（payload.call 不同）不认领。"""
        def mine(pl: dict[str, Any]) -> bool:
            return (call is not None and call["kind"] == "tool" and pl["call"] == call["id"]) if pl.get("call") else True

        def bound(pl: dict[str, Any]) -> bool:
            return call is not None and (pl.get("call") == call.get("id") if call["kind"] == "tool" else not pl.get("call") and pl.get("tool") == call["tool"])

        cards = [c for c in await asyncio.to_thread(acquire_permission.new_for_run, f"{self.task.name}/{self.run_dir.name}", since)
                 if c["id"] not in self._claimed_cards and mine(c.get("payload") or {})]   # 别的调用已在等的卡（发卡时间和这次调用挨得近）不算这次的
        self._claimed_cards.update(c["id"] for c in cards)
        notes = []
        for card in cards:
            rnd = acquire_permission.next_round(self.run_dir)
            ours = bound(card.get("payload") or {})
            acquire_permission.record_card(self.run_dir, rnd, card["id"], card["title"], {k: v for k, v in call.items() if k != "id"} if ours and call else None,
                                           auto=bool((card.get("payload") or {}).get("auto")))
            rec.setdefault("cards", []).append(card["id"])
            got = await self._wait_round(rnd, float(card["created"]), rec, card["id"])
            if got is None:
                self.ask_yielded = {"card": card["id"], "round": rnd, "title": card["title"]}
                notes.append((acquire_permission.CALL_PENDING_NOTE if ours else acquire_permission.CARD_PENDING_NOTE).format(title=card["title"]))
                break
            if ours and acquire_permission.approved_call(got) and "approved_inline" not in rec:
                rec["approved_inline"] = card["id"]
                continue
            notes.append(acquire_permission.tool_result(got))
        if not notes:
            return result
        if isinstance(result, dict):
            return {**result, "approval": "\n".join(notes)}
        return f"{result}\n" + "\n".join(notes)

    async def _wait_round(self, rnd: int, t0: float, rec: dict[str, Any], card_id: str | None = None) -> dict[str, Any] | None:
        """就地等这一轮（问题卡或审批卡）：从 t0 算最多 acquire_permission.WAIT_S 秒，运行器要让出、被要求暂停、来了可插话的条目（steer_pending）都提前放弃。
        审批卡（card_id）被处理了但轮次还没记上（处理器比网关先到）就在这里补记。等到了返回这一轮，放弃返回 None（已记下让出）。
        等的期间不算主模型时间（发卡算），这次调用持着网关锁就先放开（rec["_locked"]），等完再拿回：别的工具调用照常跑。"""
        prev, self.ask_waiting, self.ask_yield = self.ask_waiting, True, False
        await self._activity("ask")
        self._off_clock()
        if rec.get("_locked"):
            self._lock.release()
            rec["_locked"] = False
        try:
            got = None
            while True:
                if card_id and (c := cards.load(card_id)) and c.get("resolved"):
                    acquire_permission.answer(self.run_dir, card_id, "submit", {"action": c["resolved"]["action"], "result": c["resolved"]["result"]})
                got = acquire_permission.settle(self.run_dir, rnd, give_up=False)
                if got is not None or time.time() - t0 >= acquire_permission.WAIT_S or self.ask_yield or self.pause_requested or self._closing or self.steer_pending():
                    break
                await asyncio.sleep(acquire_permission.POLL_S)
            return got or acquire_permission.settle(self.run_dir, rnd, give_up=True)     # 持锁：让出与 Joy 处理同时发生时只认一个
        finally:
            self.ask_waiting = prev
            self._on_clock()
            if rec.get("_locked") is False:          # 等完拿回网关锁（被取消时没拿到就不算持有，call 收尾不放）
                await self._lock.acquire()
                rec["_locked"] = True

    async def _invoke(self, name: str, args: dict[str, Any], use: ToolUse, rec: dict[str, Any],
                      images: list[dict[str, str]] | None = None, meta: dict[str, Any] | None = None) -> Any:
        res = await self.sessions[use.name].call_tool(name, args, meta=meta)  # pyright: ignore[reportArgumentType]
        text = "\n".join(c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text")
        if getattr(res, "is_error", False):
            await self._tool_error(name, use.name, text)
            if TOOL_TIMEOUT in text:                    # 工具自己的时限到了（control_center/tool_kit.py tool_timeout）：按超时交给模型
                why = text.split(TOOL_TIMEOUT, 1)[1].strip()[:300]
                rec.update(timeout=True)
                return {"timeout": True, "tool": use.name, "note": f"超时（{why}）"}
            raise GatewayError("TOOL_ERROR", clip_error(text) or f"工具 {name} 返回了错误但没有任何说明文字")
        rec["result_chars"] = len(text)
        rec.update(tool_meta(res))
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + self._overflow(name, text)
            rec["result_saved"] = True
        return text + self._images(getattr(res, "content", []), images, rec)

    async def _tool_error(self, name: str, tool: str, text: str) -> None:
        """工具报错（is_error）里平台要接手的几类，抛 GatewayError：缺凭证（发凭证卡）、平台故障、程序错误、没带标记的崩溃。别的（超时、工具自己的报错）返回，调用方处理。"""
        if m := re.search(re.escape(MISSING_CREDENTIAL) + r"([A-Z][A-Z0-9_]*)", text):   # 工具用到的凭证 .env 里没有：发凭证卡给 Joy
            raise await self._missing_credential(m.group(1), tool)
        if PLATFORM_FAULT in text:                      # 工具标了平台故障（control_center/tool_kit.py platform_fault）
            raise GatewayError("PLATFORM", clip_error(text.split(PLATFORM_FAULT, 1)[1].strip()) or f"工具 {name} 报了平台故障但没写原因")
        if PROGRAM_ERROR in text:                       # 工具代码里的意外异常（control_center/tool_kit.py 统一加标记）
            raise GatewayError("BUG", clip_error(text.split(PROGRAM_ERROR, 1)[1].strip()))
        if text.strip() == f"Error executing tool {name}":   # 没带标记的崩溃（SDK 的通用说明）
            raise GatewayError("BUG", f"程序错误：工具 {name} 里的意外异常；traceback 见运行目录的 gateway.stderr.log")

    async def _missing_credential(self, key: str, tool: str) -> "GatewayError":
        """工具要的凭证 key 在 .env 里没有：给 Joy 发一张凭证卡（cards.ENV_KIND：只有他能填，值经中枢内存由处理器写进 .env，卡片与日志里不留），
        同一个键已有没处理的卡就不再发；返回给模型的错（CREDENTIAL：原样交给模型，不就地等卡）。"""
        pending = any(c.get("kind") == cards.ENV_KIND and (c.get("payload") or {}).get("key") == key
                      for c in await asyncio.to_thread(cards.pending))
        if not pending:
            body = (f"任务 {self.task.name} 的工具 {tool} 要用 {key}，.env 里没有。\n"
                    "点「填写」亲自输入：值直接写进 .env，不经过 agent，卡片和日志里都不留")
            await hub_client.call("card", kind=cards.ENV_KIND, title=f"🔑 {tool} 要凭证 {key}", body=body,
                                  buttons=[{"id": "enter", "label": "填写", "style": "primary", "modal": True},
                                           {"id": "reject", "label": "不给", "style": "danger"}],
                                  modal={"title": key[:45], "fields": [{"id": "password", "label": "值（直接写进 .env，不会记录）", "secret": True}]},
                                  payload={"key": key, "tool": tool, "for_run": f"{self.task.name}/{self.run_dir.name}"})
        return GatewayError("CREDENTIAL", f"缺凭证 {key}，已发卡请 Joy 填，填好后再调用")

    async def _ask(self, name: str, args: dict[str, Any], use: ToolUse, rec: dict[str, Any], session: ClientSession | None = None) -> str:
        """D35：服务器校验问题 → 发问题卡 → 就地等回答（最多 acquire_permission.WAIT_S 秒）。等到了答案就是调用结果；
        没等到（或运行器要让出、被要求暂停）就记下让出，回复带 pause，pi 这一轮结束就退出，运行记 waiting。
        session：子代理调用里的 ask-user 会话（不给就是任务的）。"""
        res = await (session or self.sessions[use.name]).call_tool(name, args)
        text = "\n".join(c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text")
        if getattr(res, "is_error", False):
            raise GatewayError("TOOL_ERROR", clip_error(text) or "问题卡不合格（ask-user 没写原因）：对照 ask_user 的说明检查 questions 的写法")
        data = json.loads(text)
        qs = data["questions"]
        rnd = acquire_permission.next_round(self.run_dir)
        r = await hub_client.call("card", **acquire_permission.card_spec(self.task.name, self.run_dir.name, rnd, qs, data.get("why") or ""))
        if not (r and r.get("ok")):
            raise GatewayError("TOOL_ERROR", f"问题卡没发出去（{(r or {}).get('error', 'stbcc-hub 没有响应')}），可以稍后再试，或直接在回复里问 Joy")
        acquire_permission.record(self.run_dir, rnd, qs, str(r["id"]))
        rec.update(ask_round=rnd, questions=len(qs), card=r["id"])
        t0 = time.time()
        got = await self._wait_round(rnd, t0, rec)
        rec["ask_wait_seconds"] = round(time.time() - t0, 1)
        if got is not None:
            rec["answered"] = got["status"]
            return acquire_permission.tool_result(got)
        self.ask_yielded = {"card": str(r["id"]), "round": rnd, "questions": len(qs)}
        rec["yielded"] = True
        return acquire_permission.PENDING_NOTE

    def _result_images(self, result: Any, out: list[dict[str, str]] | None, rec: dict[str, Any]) -> Any:
        """任何子代理的结果（JSON 对象）里的 images [{file: 路径, …}]（通用约定）→ 图片块附给主模型看：只认本任务工作目录 media/ 下已有的文件，
        长边缩到 1024 的 JPEG，最多 MAX_IMAGES 张、单张 base64 不超过 MAX_IMAGE_B64。结果原样返回。
        要不要交图、交哪些由子代理自己定（如联网研究交的是研究容器里看图模型打分、代码按规则筛过的图，2026-09-29 Joy 定主模型可以看筛过的图）。"""
        import base64
        import io

        from PIL import Image

        found = result.get("images") if isinstance(result, dict) else None
        if not isinstance(found, list) or not found:
            return result
        media = (self.work_dir / "media").resolve()
        kept: list[dict[str, str]] = []
        for im in found[:MAX_IMAGES]:
            f = str(im.get("file") or "") if isinstance(im, dict) else ""
            try:
                host = Path(f).resolve() if f.startswith("/") else None
                if host is None or media not in host.parents or not host.is_file():
                    continue
                with Image.open(host) as pic:
                    pic = pic.convert("RGB")
                    pic.thumbnail((1024, 1024))
                    buf = io.BytesIO()
                    pic.save(buf, "JPEG", quality=85)
            except (OSError, ValueError):
                continue
            data = base64.b64encode(buf.getvalue()).decode()
            if len(data) <= MAX_IMAGE_B64:
                kept.append({"data": data, "mimeType": "image/jpeg"})
        if out is not None:
            out.extend(kept)
        rec.update(images=len(kept), image_bytes=sum(len(i["data"]) for i in kept))
        return result

    def _overflow(self, name: str, text: str) -> str:
        """可信工具结果超过 MAX_RESULT_CHARS：全文存文件，返回接在截断处的说明（模块函数 overflow_note）。"""
        return overflow_note(self.work_dir, self.run_dir.name, name, text)

    @staticmethod
    def _images(content: list[Any], out: list[dict[str, str]] | None, rec: dict[str, Any]) -> str:
        """可信工具结果里的图片块收进 out（最多 MAX_IMAGES 张，单张 base64 超过 MAX_IMAGE_B64 的丢掉）；审计只记张数与字节数。
        返回要接在结果文字后面的说明（有图被丢时）。"""
        blocks = [c for c in content if getattr(c, "type", "") == "image"]
        if not blocks:
            return ""
        kept: list[dict[str, str]] = []
        for c in blocks:
            data, mime = str(getattr(c, "data", "") or ""), str(getattr(c, "mime_type", "") or "")
            if len(kept) < MAX_IMAGES and 0 < len(data) <= MAX_IMAGE_B64 and mime.startswith("image/"):
                kept.append({"data": data, "mimeType": mime})
        if out is not None:
            out.extend(kept)
        rec.update(images=len(kept), image_bytes=sum(len(i["data"]) for i in kept))   # base64 字节数，不记内容
        dropped = len(blocks) - len(kept)
        if not dropped:
            return ""
        rec["images_dropped"] = dropped
        return f"\n（网关：另有 {dropped} 张图片超出上限（每次最多 {MAX_IMAGES} 张、单张 base64 不超过 1.5 MB），没有附上）"

    async def _unstalled(self, call: Any, watch: dict[str, Any], who: str) -> Any:
        """timeout: none 的子代理调用：每 CARD_POLL_S 秒看一次，没有在途的工具调用、又 runner.STALL_S 秒没有动静（watch，_run_callee 更新）
        就取消这次调用、按子代理出错交回（READER_FAILED）。工具执行期间不算（长调用由工具自己的时限兜底）。"""
        from control_center.runner import STALL_S

        fut = asyncio.ensure_future(call)
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=CARD_POLL_S)
                if done:
                    return fut.result()
                if not watch["busy"] and (idle := time.monotonic() - watch["t"]) > STALL_S:
                    raise GatewayError("READER_FAILED", f"{who}：停滞 {int(idle)} 秒没有模型输出、也没有在途的工具调用，判为卡住，已结束这次调用")
        finally:
            if not fut.done():
                fut.cancel()
                await asyncio.wait({fut})

    def _timed_out(self, who: str, limit: int, t0: float, rec: dict[str, Any]) -> dict[str, Any]:
        """子代理到了自己的单次上限：告诉主模型是超时（哪个子代理、跑了多久），不算工具报错。"""
        s = int(time.monotonic() - t0)
        rec.update(timeout=True, subagent=who, subagent_seconds=s)
        return {"timeout": True, "subagent": who, "elapsed_seconds": s,
                "note": f"超时（{who} 这次跑了 {s // 60} 分 {s % 60} 秒，到了单次上限 {limit // 60} 分钟，已停止，没有结果）"}

    async def _within(self, call: Any, limit: int, t0: float, since: float) -> Any:
        """子代理的单次上限（从 t0 算 limit 秒）：这次调用里发的审批卡（payload.run 是本次运行）等 Joy 处理的时间顺延上限（发卡本身算）。
        到点取消这次调用、抛 TimeoutError（同 asyncio.wait_for）。"""
        fut = asyncio.ensure_future(call)
        run = f"{self.task.name}/{self.run_dir.name}"
        try:
            while True:
                left = limit + await asyncio.to_thread(acquire_permission.waited, run, since, acquire_permission.WAIT_S) - (time.monotonic() - t0)
                if left <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({fut}, timeout=min(left, CARD_POLL_S))
                if done:
                    return fut.result()
        finally:
            if not fut.done():
                fut.cancel()
                await asyncio.wait({fut})

    # ---- 子代理换模型（D32）----
    @asynccontextmanager
    async def _model_for(self, key: str, concurrent: bool, state: str) -> AsyncIterator[None]:
        """key：工具名或子代理名（子代理名才可能在 sub_models 里）；concurrent：这个调用不占网关锁；state：调用期间的运行状态（渠道显示进行中标记用）。
        子代理的档位与主任务不同：卸主 → 起子 → 跑子代理 → 卸子 → 起主，再把结果交回主代理。
        成功失败都切回；同一子档位的并发调用共用一次切换，最后一个结束才切回；要别的子档位的调用等在跑的都结束再换
        （有人等着就直接换过去，不先切回主模型）。子档位在跑期间占着网关锁，非并发工具排队；用主模型的并发工具（同任务档位的研究子代理）
        等主模型回来再跑，它们在跑时也不换走主模型。"""
        sub = self.sub_models.get(key)
        if sub is None and not (self.sub_models and concurrent):
            yield
            return
        async with self._switch_cond:
            waited = self._sub_active is not None and self._sub_active != sub
            if sub is None:
                await self._switch_cond.wait_for(lambda: self._sub_active is None)
                if waited and self._closing:              # 等的是主模型回来，可运行已结束、不再切回：主模型不在
                    raise GatewayError("STOPPED", "运行已在收尾，不再装回主模型")
            else:
                self._sub_waiting += 1
                try:
                    await self._switch_cond.wait_for(lambda: self._sub_active == sub or not self._sub_calls)
                except BaseException:                     # 等的时候被取消：没人要这个子档位了就切回
                    self._sub_waiting -= 1
                    await self._settle()
                    raise
                self._sub_waiting -= 1
                if self._sub_active != sub:
                    if self._closing or self.fatal:       # 收尾中、或之前的切换已失败（任务已判失败）：不再切换
                        await self._settle()
                        raise GatewayError("STOPPED", f"任务已失败（{self.fatal}），不再切换模型" if self.fatal else "运行已在收尾，不再切换模型")
                    if self._sub_active is None:
                        await self._lock.acquire()
                    try:
                        await self._switch(sub)
                    except BaseException:
                        self._sub_active = None
                        self._lock.release()
                        self._switch_cond.notify_all()
                        raise
                    self._sub_active = sub
                if self._closing:                         # agent 在切换期间（或同档位的调用在跑时）退出了：不再跑这个工具
                    await self._settle()
                    raise GatewayError("STOPPED", "运行已在收尾，不再调用子代理工具")
            self._sub_calls += 1
            task = asyncio.current_task()
            if sub is not None and task is not None:
                self._sub_tasks.add(task)
        try:
            if sub is not None or waited:
                await self._activity(state, key)
            yield
        finally:
            self._sub_tasks.discard(task)
            async with self._switch_cond:
                self._sub_calls -= 1
                self._switch_cond.notify_all()
                await self._settle()

    async def _settle(self) -> None:
        """没有调用在用子档位、也没有调用等着换别的子档位：切回主模型（收尾中不切回），放开网关锁。调用方持有 _switch_cond。"""
        if self._sub_active is None or self._sub_calls or (self._sub_waiting and not self._closing):
            return
        try:
            if not self._closing:
                await self._switch(self.task_model)  # pyright: ignore[reportArgumentType]
        finally:
            self._sub_active = None
            self._lock.release()
            self._switch_cond.notify_all()

    async def _switch(self, model: str) -> None:
        """经运行器的 ensure_model 切到档位 model；加载期间报 loading 状态（Discord 的 ⏳）、状态栏"加载模型：X"，卸载 / 加载时间不计入任务超时和子代理的单次上限。
        失败 = 任务失败（D32 第 2 条，不回退）：记下原因、让运行器停掉沙箱。"""
        async def on_loading() -> None:
            self.loading, self._loading_since = model, time.time()
            await self._activity("loading")

        try:
            why = await self.switch_model(model, on_loading) if self.switch_model else "网关没有模型切换入口"
        finally:
            if self.loading:
                self.switch_seconds += time.time() - self._loading_since
                self.loading = None
        self._audit({"ev": "model_switch", "to": model, "error": why})
        if why:
            self.fatal = f"子代理工具切换模型到 {model} 失败：{why}"
            if self.on_fatal:
                self.on_fatal()
            raise GatewayError("MODEL_SWITCH", self.fatal)

    # ---- 插话（D36）----
    def _steer_entries(self) -> list[Path]:
        """队列里本任务带 steer 的新条目（不含恢复 / 续跑条目），按入队顺序。"""
        out = []
        for p in sorted(paths.QUEUE.glob("*.json")):
            try:
                e = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if e.get("task") == self.task.name and e.get("steer") is not None and not e.get("resume"):
                out.append(p)
        return out

    def steer_pending(self) -> bool:
        """就地等问题卡回答时来了可插话的条目：让出，让它照常开一次运行，问题卡留着不当它是回答。"""
        return bool(self._steer_entries())

    async def take_steers(self) -> list[dict[str, Any]]:
        """主模型这次调用的回复带的插话 {text, images}：先是子代理期间认领、已送进子代理的（_sub_steers，按认领顺序），再认领队列里新的。
        回复带 pause 时都不给（待转交的留着，运行器收尾时核对、没进主会话的放回队列）。"""
        if self.wrapup or self.pause_requested or self.ask_yielded is not None or self._closing:
            return []
        out, self._sub_steers = self._sub_steers, []
        return out + await self._claim_steers()

    async def _sub_take_steers(self, sc: SubCall) -> list[dict[str, Any]]:
        """agent 子代理这次工具调用的回复带的插话：认领队列里新的，记下待转交主模型（_sub_steers）；隔离的把插话文字算进主模型问的问题
        （会话状态 prompts，finish 的 question 核对按它）；档位看不了图的去掉图片、注明。"""
        if self.wrapup or self.pause_requested or self.ask_yielded is not None or self._closing:
            return []
        got = await self._claim_steers(sc.d.name)
        if not got:
            return []
        self._sub_steers += got
        sc.state["prompts"] += [m["text"] for m in got]
        subagent_run.save_state(sc.folder, sc.state)
        if sc.vision:
            return got
        return [{"text": m["text"] + (STEER_IMAGES_SKIPPED.format(n=len(m["images"])) if m["images"] else ""), "images": []} for m in got]

    async def _claim_steers(self, subagent: str | None = None) -> list[dict[str, Any]]:
        """认领排队的可插话条目，变成给 pi 的 steer 消息 {text, images}。图片以外的附件按类型附上说明（要隔离读的拷进本次运行的 in/），图片按原样附上。
        subagent：认领它的 agent 子代理（审计记下）；不写 = 主模型。"""
        from control_center.runner import files_note, stage_files

        out = []
        for p in self._steer_entries():
            dest = self.run_dir / "injected" / p.name
            dest.parent.mkdir(exist_ok=True)
            try:
                p.rename(dest)                     # 原子认领：运行器取条目也是改名，只有一方成功
                e = json.loads(dest.read_text())
            except (OSError, ValueError):
                continue
            text = str(e.get("steer") or e.get("prompt") or "")    # steer：listen.py 给的插话文字（只有这条消息，不重复带它拼进 prompt 的上下文）
            if files := stage_files(self.task, self.run_dir, list(e.get("files") or [])):
                text += "\n\n" + files_note(files)
            images = []
            for a in e.get("attachments") or []:
                f = Path(str(a))
                ext = f.suffix.lower().lstrip(".")
                try:
                    images.append({"data": base64.b64encode(f.read_bytes()).decode(), "mimeType": f"image/{'jpeg' if ext == 'jpg' else ext}"})
                except OSError:
                    continue
            e.update(delivered_text=text, queue_file=p.name)
            dest.write_text(json.dumps(e, ensure_ascii=False))
            self.injected.append(e)
            out.append({"text": text, "images": images})
            self._audit({"ev": "injected", "queue": p.name, "chars": len(text), "images": len(images), **({"subagent": subagent} if subagent else {})})
            if ref := e.get("ref"):
                await hub_client.call("progress_start", task=self.task.name, started=time.time(), ref=ref)   # 那条消息的标记换成进行中
        return out

    # ---- 预算（D34）----
    def extra_seconds(self) -> int:
        """子代理时间（含正在进行的那段）；运行器的超时按它延长。
        任务的 timeout 只算主模型干活的时间：子代理调用（连同调用里的模型卸载 / 加载）都不算，每次子代理调用只受 subagents/<名>/subagent.yaml 的单次上限管。"""
        return int(self.sub_seconds + (time.time() - self._sub_since if self._sub_running else 0))

    def seconds_left(self) -> float | None:
        """主模型时间还剩的秒数（timeout + 子代理时间 − 已用时间）；timeout: none（不限）返回 None。"""
        if self.task.timeout_seconds is None:
            return None
        return self.task.timeout_seconds + self.extra_seconds() - self.elapsed_before - (time.time() - self.started)

    def budget_line(self) -> str:
        """每次工具结果后面附的一行剩余预算：主模型时间（timeout: none 不写，这时返回空串）。"""
        if (left := self.seconds_left()) is None:
            return ""
        return "（网关：" + (f"主模型时间还剩 {int(left) // 60} 分钟" if left >= 60 else "主模型时间还剩不到 1 分钟") + "）"

    def with_budget(self, result: Any) -> Any:
        """把剩余预算附在工具结果后面：文字结果另起一行，JSON 结果加 budget 字段；没有可写的预算原样返回。"""
        if not self.budget_line():
            return result
        if isinstance(result, str):
            return f"{result}\n{self.budget_line()}"
        if isinstance(result, dict):
            return {**result, "budget": self.budget_line()}
        return result

    def _record_result(self, d: subagents.SubagentDef, result: Any, ok: bool) -> None:
        """子代理这次交回主模型的结果记进运行目录的 SUBAGENT_RESULTS（主模型看不到运行目录；hook 按需自己读，如 send-email 核对正文链接）。"""
        try:
            with open(self.run_dir / SUBAGENT_RESULTS, "a") as f:
                f.write(json.dumps({"ts": time.time(), "subagent": d.name, "kind": d.kind, "isolated": d.isolated, "ok": ok, "result": result},
                                   ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            self._audit({"ev": "result_record_error", "subagent": d.name, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    # ---- 审计 ----
    def _audit(self, rec: dict[str, Any]) -> None:
        self._log.write(json.dumps({"ts": time.time(), **rec}, ensure_ascii=False) + "\n")
        self._log.flush()

    # ---- HTTP（只有 /tools、/call、/state）----
    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            body = await r.readexactly(int(headers.get("content-length", "0"))) if headers.get("content-length") else b""
            auth = headers.get("authorization", "")
            if not self.token or not secrets.compare_digest(auth, f"Bearer {self.token}"):
                sc = next((c for t, c in list(self._subcalls.items()) if secrets.compare_digest(auth, f"Bearer {t}")), None)
                if sc is None:
                    return await self._reply(w, 401, {"error": "unauthorized"})
                return await self._serve_sub(sc, method, path, body, w)     # agent 子代理的 pi（这次调用的令牌）
            if method == "GET" and path == "/tools":
                return await self._reply(w, 200, {"tools": self.tool_list(), "builtins": self.builtins(), "loaded": self.loaded_defs(),
                                                  "loadable": self.loadable_defs(), "restored": self.restored()})
            if method == "POST" and path == "/compacted":
                return await self._reply(w, 200, {"ok": True, "text": self.compacted()})
            if method == "GET" and path == "/state":
                return await self._reply(w, 200, {"pause": self.pause_requested})
            if method == "POST" and path == "/call":
                try:
                    req = json.loads(body or b"{}")
                    name, args = str(req.get("name", "")), req.get("arguments") or {}
                    if not isinstance(args, dict):
                        raise GatewayError("SCOPE", "arguments 必须是对象")
                    images: list[dict[str, str]] = []
                    turn, repaired, cid = req.get("turn"), req.get("repaired"), req.get("call_id")
                    result = self.with_budget(await self.call(name, args, images=images, turn=turn if isinstance(turn, int) else None,
                                                              repaired=repaired if isinstance(repaired, list) else None,
                                                              call_id=cid if isinstance(cid, str) and cid else None))
                    reply = {"ok": True, "result": result, **({"images": images} if images else {}),
                             "pause": self.pause_requested or self.wrapup or self.ask_yielded is not None}
                    if msgs := await self.take_steers():
                        reply["messages"] = msgs
                    return await self._reply(w, 200, reply)
                except GatewayError as e:
                    msg = e.message if self.wrapup or not self.budget_line() else f"{e.message}\n{self.budget_line()}"
                    reply = {"ok": False, "error": {"code": e.code, "message": msg}, "pause": self.pause_requested or self.wrapup or self.ask_yielded is not None}
                    if msgs := await self.take_steers():
                        reply["messages"] = msgs
                    return await self._reply(w, 200, reply)
            await self._reply(w, 404, {"error": "not found"})
        except Exception as e:
            try:
                await self._reply(w, 500, {"error": str(e)[:200]})
            except Exception:
                pass
        finally:
            w.close()

    @staticmethod
    async def _reply(w: asyncio.StreamWriter, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        w.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await w.drain()

    # ---- 模型端点 TCP 转发 ----
    @staticmethod
    async def _pipe(cr: asyncio.StreamReader, cw: asyncio.StreamWriter, host: str, port: int, gw: "Gateway | None" = None) -> None:
        try:
            ur, uw = await asyncio.open_connection(host, port)
        except OSError:
            cw.close()
            return

        async def one(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while chunk := await src.read(65536):
                    if gw is not None:
                        gw.last_activity = time.time()      # 模型在吐字（含思考 token）就是活动
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(one(cr, uw), one(ur, cw))


class _TrialGateway(Gateway):
    """builder_try_subagent 的一次试跑（Gateway._try_subagent）：只有一个子代理 d（定义来自预演副本），运行身份是试跑的（任务名 builder-trial、
    运行目录 data/hub/builder-trial/<计划>/，所以 STB_RUN_REF = builder-trial/<计划>、运行 id 是计划 id）；调用走 Gateway 的正式代码。
    和正式调用不同的只有两处：子代理的 hooks 用预演副本里的（_call_owners），换模型借调用方网关的 _model_for（key 是调用方登记试跑档位的键）。"""

    def __init__(self, parent: Gateway, d: subagents.SubagentDef, key: str, run_dir: Path, work_dir: Path) -> None:
        task = Task(name=builder.TRIAL_TASK, enabled=False, timeout=None, runtime=parent.task.runtime, model=parent.task.model, system_prompt=d.description)
        super().__init__(task, run_dir, work_dir, parent.profile, parent.bind_ip)
        self.parent, self.key = parent, key
        self.task.subagents = [SubagentUse(name=d.name)]
        self.callees = {d.name: d}
        self.task_model = parent.task_model
        if key in parent.sub_models:
            self.sub_models, self.sub_profiles = {d.name: parent.sub_models[key]}, {d.name: parent.sub_profiles[key]}

    def _call_owners(self, use: ToolUse | SubagentUse) -> list[hooks.Owner]:
        if isinstance(use, SubagentUse):
            d = self.callees[use.name]
            return [hooks.Owner("subagent", d.name, d.hooks, d.is_editable)] if d.hooks else []
        return super()._call_owners(use)

    def _model_for(self, key: str, concurrent: bool, state: str) -> Any:
        return self.parent._model_for(self.key, concurrent, state)
