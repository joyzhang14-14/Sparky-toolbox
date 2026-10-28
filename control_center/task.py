"""任务定义文件的解析与校验（规格第 5 节、第 16 节"任务校验"）。

两类：agent 任务（system_prompt，主模型带工具）与 workflow 任务（steps，平台按步骤跑任务文件夹里的 py，runner.run_workflow 与 control_center/workflow.py）。
拒绝：缺 timeout / enabled、agent 任务缺 runtime / model、引用未登记工具或只给子代理用的工具（usable_by: subagent）、subagents 写了不存在的子代理、引用只写了 start / stop 之一的模型档位（都没写 = 常驻接口）、
工具项写了 guide 以外的键、trigger 与 schedule / listen 对不上、写了字段表以外的字段（未知字段：报错指向手册的字段表）。
平台统一的规则（任务 yaml 不写）：agent 一律在一次性容器里跑（control_center/sandbox.py）；运行结束一律通知 Joy（runner.deliver）；
停滞判定（agent 任务）、被抢占后等检查点的上限是 runner 的常量（STALL_S；PREEMPT_GRACE_AGENT_S / PREEMPT_GRACE_COMMAND_S）；总时长不限，只有 timeout；
运行期间同一任务又入队：订阅事件（listen_events 不空）的排队，其余丢掉（Task.queues_overlap）。

触发方式（trigger，写一种）三种，`stb run --now` / 网页"跑"对所有任务都可用：
- schedule：配 schedule（cron）→ stbcc-hub 到点入队（control_center/schedule.py）；只有它参与补跑。
- listen：监听程序 listen.py（任务 yaml 写 listen: <监听名>），由 stbcc-hub 运行（control_center/listen.py）：
  check(ctx) 每 LISTEN_EVERY_DEFAULT 秒调一次；on_event(event, ctx) 在订阅的来源（listen_events）有事件时调；watch(ctx, emit) 常驻（有它就不按间隔调 check），
  自己决定什么时候 emit。返回 / emit 的 fire 为真就入队，prompt 作这次的用户消息。
  听什么、什么时候触发由 listen.py 决定，平台只负责运行它、按结果入队。
  监听是有名字、可复用的定义：每个监听名只在一个任务的文件夹里有 listen.py（这个任务是定义者）；别的任务写同一个 listen: <名> 就是引用，
  自己不放 listen.py。check fire 时所有引用它的任务（含定义者）都用同一个 prompt 入队；事件按来源的路由规则交给任务。
- manual：只手动跑，不定时触发，不补跑。
listen、listen_events 只给 trigger: listen 的任务。
enabled（必填，true / false）是启停的唯一真源：stb task enable / disable 与网页改的都是它；停用的任务不定时触发、不补跑，stbcc-hub 不检查、事件不交给它。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from control_center import env, hooks, scheme_check, subagents
from control_center.registry import GATEWAY_KEYS, PLATFORM_CREDENTIALS, TOOLS, credential_keys, functions, refresh as refresh_tools
from control_center.schedule import check as check_schedule

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
TriggerKind = Literal["schedule", "listen", "manual"]
LISTEN_EVERY_DEFAULT = 30   # check() 的间隔（秒），平台统一
LISTEN_FILE = "listen.py"   # 监听程序：任务文件夹里的这个文件
BUILTIN_LISTEN = "api"      # 平台自带的监听：算力 API 的门口（control_center/api_door.py，stbcc-hub 里），没有 listen.py，只给控制台「API」页建的 api-* 任务
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")   # 监听名、事件来源名的格式（只拦格式，不设白名单）


class TaskError(ValueError):
    pass


FIELD_TABLE = "字段表见手册 How-to-set-a-task-on-Sparky.md，或 stb schema"   # 未知字段的报错指向这里


def unlimited(timeout: Any) -> bool:
    """任务的 timeout 写 none（或 null）：主模型时间不限。"""
    return timeout is None or str(timeout).strip().lower() == "none"


def parse_duration(s: str | int) -> int:
    if isinstance(s, int):
        return s
    m = _DURATION.match(str(s).strip())
    if not m or not any(m.groups()):
        raise TaskError(f"时长格式不对：{s!r}（例：20m、1h30m、90s）")
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


class ToolUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    guide: str = ""                     # 本任务给这个工具追加的用法规则（网关键 guide）：和工具的 guide.md 一起在第一次调用时给


# 推理档位（任务与子代理的 effort）：pi 的思考级别里除 off 以外的几个，原样交给 pi 的 --thinking
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


class SubagentUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                           # 子代理名（subagents/<名>/subagent.yaml），也是主模型看到的调用名
    guide: str = ""                     # 本任务给这个子代理追加的规则：和它的 guide.md 一起在主模型第一次调用它时给


@dataclass(frozen=True)
class Step:
    """workflow 任务的一步（steps 的一行）：编号、任务文件夹里的 py、要不要资源、要的模型档位（runtime + model，只给要资源的步骤；不写 = 不起模型、只空出机器）。"""
    n: int
    file: str
    resource: bool
    runtime: str | None = None
    model: str | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")   # 小写字母、数字、连字符，≤41 字符；等于任务文件夹名 tasks/<名>/
    description: str = ""                              # 一句话说明
    enabled: bool                                      # 启停的唯一真源：true / false
    trigger: TriggerKind | None = None                 # 触发方式：schedule / listen / manual（写一种）；不写时有 schedule 当 schedule，否则 manual
    schedule: str | list[str] | None = None            # trigger: schedule 时必填：一条 5 段 cron，或几条的列表（几个时刻分钟不一样时）
    # 监听（control_center/listen.py）：任务文件夹里的 listen.py 决定听什么、什么时候触发、这次的用户消息（prompt，可以为空）。
    listen: str | None = None                          # 监听名；任务文件夹里有 listen.py = 定义它（同名只能一个定义），没有 = 引用别的任务定义的同名监听；trigger: listen 时必填
    listen_events: list[str] = Field(default_factory=list)   # 订阅的事件来源名（如 [message]：渠道发布的 Joy 发来的消息）：中枢收到这个来源的事件就调 listen.py 的 on_event(event, ctx)
    timeout: str | int | None                          # agent：主模型时间预算（none = 不限），到点给一轮收尾记 partial；workflow：各步骤在跑的时间合计，到点终止记 timeout
    priority: int = Field(default=5, ge=1, le=10)      # 1–10，大的先跑，同优先级按入队先后；builder 只能设 1–9
    resumable: bool = True                             # 只给 workflow 任务：要资源的步骤能在中间被抢占（SIGTERM → 存进度以 75 退出，恢复时重跑这一步）；false = 跑完这一步才让出
    runtime: str | None = None                         # agent 任务必填：主模型的服务栈（如 vllm-eugr）；workflow 任务不写
    model: str | None = None                           # agent 任务必填：主模型的真实模型 id；与 runtime 一起对上 models.yaml 的一个档位（runtime + model_path）；workflow 任务不写
    effort: str | None = None                          # agent 任务可写：主模型的推理档位（EFFORTS 之一，交给 pi 的 --thinking）；不写 = 不传档位，用模型模板的默认；workflow 任务不写
    tools: list[ToolUse] = Field(default_factory=list)   # 权限清单：- 名字 或 - 名字: {guide}（guide 追加本任务的用法规则）；只能写 usable_by: universal 的工具；${KEY} 只在这里的值里展开
    subagents: list[SubagentUse] = Field(default_factory=list)   # 主模型能直接调用的子代理（subagents/<名>/subagent.yaml）：- 名字 或 - 名字: {guide: …}
    system_prompt: str = ""                            # agent 任务必填：固定说明与注意事项（定时任务的任务说明也写这里），接在平台人设后面作系统提示
    steps: list[list[Any]] | None = None               # workflow 任务：每步 [编号, 文件, 要不要资源] 或 [编号, 文件, true, {runtime, model}]（解析成 Step，load_task 填 _steps）
    network: bool | list[int] | None = None            # listen.py、workflow 的步骤与任务 hook 以 sparky 跑时要不要联网（不写 = 禁 TCP；端口列表 = 只连本机这些端口）
    memory: list[str] = Field(default_factory=list)    # 记忆文件清单：每项是文件或文件夹（文件夹 = 那一层所有 .md），~ 展开；每次运行读出来注入用户消息（只读，control_center/memory_docs.py）
    hooks: dict[str, Any] | None = None                # 平台执行的文件（control_center/hooks.py）：run_start、run_end（运行开始 / 结束），pre_call、post_call（本任务的运行里调用工具 / 子代理前后，match 写函数名或工具名）；command 是任务文件夹 tasks/<名>/ 里的文件
    _dir: Path | None = PrivateAttr(default=None)       # 文件夹形式的任务文件夹（load_task 填）：定义的 listen.py 在这里
    _hooks: dict[str, Any] = PrivateAttr(default_factory=dict)   # hooks 解析后的 {事件: (hooks.Hook…)}（load_task 填）
    _root: Path | None = PrivateAttr(default=None)      # 任务定义所在的 tasks/ 目录（load_task 填）：引用的监听在这里找定义者
    _steps: list[Step] = PrivateAttr(default_factory=list)   # steps 解析后的步骤（load_task 填）

    @property
    def timeout_seconds(self) -> int | None:
        """主模型时间预算的秒数；timeout: none（不限）返回 None。"""
        return None if unlimited(self.timeout) else parse_duration(self.timeout)  # pyright: ignore[reportArgumentType]

    @property
    def queues_overlap(self) -> bool:
        """运行期间同一任务又入队时排队（True）还是丢掉（False）：订阅了事件（listen_events 不空，如私信）的排队，每条事件都要处理；
        定时 / 检查 / 手动触发的丢掉（跑完这次就是最新的，runner.Runner._settle）。"""
        return bool(self.listen_events)

    @property
    def workflow(self) -> bool:
        """workflow 任务（写了 steps）。"""
        return self.steps is not None

    @property
    def step_list(self) -> list[Step]:
        return self._steps

    @property
    def preemptible(self) -> bool:
        """能被更高优先级的条目抢占、被 stb pause 暂停：agent 任务一律可以（在工具边界停、从检查点续跑）；workflow 任务看 resumable（默认可以，在步骤中间停、恢复时重跑这一步）。"""
        return not self.workflow or self.resumable

    @property
    def hook_map(self) -> dict[str, Any]:
        """hooks 解析后的 {事件: (hooks.Hook…)}（control_center/hooks.py）。"""
        return self._hooks

    def tool(self, name: str) -> ToolUse | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def triggers(self) -> tuple[str, ...]:
        """触发方式（一种）：trigger 不写时有 schedule 当 schedule，否则 manual。"""
        if self.trigger is None:
            return ("schedule",) if self.schedule is not None else ("manual",)
        return (self.trigger,)

    @property
    def trigger_label(self) -> str:
        if self.listen == BUILTIN_LISTEN:
            return f"监听 {BUILTIN_LISTEN}（平台：算力 API 的门口）"
        if self.listen is not None:
            how = ["常驻"] if self.listen_has("watch") else [f"每 {LISTEN_EVERY_DEFAULT}s 检查"] if self.listen_has("check") else []
            how += [f"事件 {'、'.join(self.listen_events)}"] if self.listen_events else []
            return f"监听 {self.listen}" + (f"（{'，'.join(how)}）" if how else "")
        return self.triggers[0]

    @property
    def defines_listen(self) -> bool:
        """自己的任务文件夹里有 listen.py：是这个监听的定义者。"""
        return self.listen is not None and self._dir is not None and (self._dir / LISTEN_FILE).is_file()

    @property
    def listen_owner(self) -> str | None:
        """定义这个监听的任务名：自己定义的是自己，引用的是定义者；找不到定义返回 None。"""
        if self.listen is None:
            return None
        if self.defines_listen:
            return self.name
        d = listen_definer(self.listen, self._root)
        return d[0] if d else None

    @property
    def listen_file(self) -> Path | None:
        """监听程序：自己定义的是 <任务文件夹>/listen.py，引用的是定义者文件夹里的 listen.py；没写 listen、找不到定义返回 None。"""
        if self.listen is None:
            return None
        if self.defines_listen:
            return self._dir / LISTEN_FILE  # pyright: ignore[reportOptionalOperand]
        d = listen_definer(self.listen, self._root)
        return d[1] if d else None

    def listen_has(self, fn: str) -> bool:
        """listen.py 里定义了 fn（check / on_event / watch）。"""
        f = self.listen_file
        try:
            return f is not None and fn in listen_functions(f)
        except TaskError:
            return False

    def profile_name(self, models: dict[str, Any]) -> str:
        """agent 任务的 (runtime, model) 对应的 models.yaml 档位名（档位名只在内部用）。"""
        return _profile(models, str(self.runtime), str(self.model), "任务")

    def step_profile(self, models: dict[str, Any], n: int) -> str | None:
        """workflow 第 n 步的模型档位名；这一步没写模型返回 None（不起模型，只空出机器）。"""
        st = self._steps[n - 1]
        return _profile(models, str(st.runtime), str(st.model), f"第 {n} 步") if st.model is not None else None


if set(Task.model_fields) != set(scheme_check.top("task")):          # 字段表 control_center/schema/fields.yaml 是任务字段的唯一来源：名字和 Task 模型对不上就不让加载
    raise RuntimeError(f"任务字段和字段表对不上：Task 模型有 {sorted(set(Task.model_fields) - set(scheme_check.top('task')))}、"
                       f"字段表有 {sorted(set(scheme_check.top('task')) - set(Task.model_fields))}（control_center/schema/fields.yaml 的 task）")


def task_callees(task: "Task") -> dict[str, subagents.SubagentDef]:
    """任务 yaml 的 subagents（主模型直接调用的子代理）：名字 → 文件夹写法的配置。不合格抛 TaskError。"""
    out = {}
    for u in task.subagents:
        if not subagents.is_folder(u.name):
            raise TaskError(f"没有子代理 {u.name}（子代理是文件夹 subagents/{u.name}/subagent.yaml；stb subagents list 查看）")
        try:
            out[u.name] = subagents.load_def(u.name)
        except subagents.SubagentError as e:
            raise TaskError(str(e)) from None
    return out


def _profile(models: dict[str, Any], runtime: str, model: str, who: str) -> str:
    from control_center.models import profile_for

    name = profile_for(models, runtime, model)
    if name is None:
        raise TaskError(f"{who}的 runtime {runtime} + model {model} 没有对应的模型档位：models.yaml 里要有 runtime 与 model_path 都对上的档位"
                        f"（独占 runtime 如 train 只看 runtime，model 写被训练的底座路径或 none）")
    return name


def _raw_tasks() -> list[tuple[str, dict[str, Any]]]:
    """tasks/ 里每个定义的 (任务名, yaml 原文映射)：只解析 yaml，不做完整校验、不展开 .env；读不了 / 写坏了的跳过。"""
    return [(n, raw) for n, raw, _ in _raw_task_paths()]


def _raw_task_paths(root: Path | None = None) -> list[tuple[str, dict[str, Any], Path]]:
    """同 _raw_tasks，多给定义文件路径；root 是 tasks/ 目录（不给 = 平台的 tasks/）。"""
    from control_center.paths import TASK_FILE, TASKS, task_name_of

    root = TASKS if root is None else root
    out = []
    for p in sorted(x for x in root.glob(f"*/{TASK_FILE}") if not x.parent.name.startswith("_")):
        try:
            raw = yaml.safe_load(p.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict):
            out.append((task_name_of(p), raw, p))
    return out


def listen_defs(root: Path | None = None) -> dict[str, list[tuple[str, Path]]]:
    """监听名 → 定义它的任务 [(任务名, listen.py)]：任务文件夹里有 listen.py、yaml 写 listen: <名>。正常每个名字只有一个。"""
    out: dict[str, list[tuple[str, Path]]] = {}
    for n, raw, p in _raw_task_paths(root):
        f = p.parent / LISTEN_FILE
        if isinstance(raw.get("listen"), str) and f.is_file():
            out.setdefault(raw["listen"], []).append((n, f))
    return out


def listen_definer(name: str, root: Path | None = None) -> tuple[str, Path] | None:
    """监听名 → (定义者任务名, listen.py)：先在 root（任务定义所在的 tasks/）找，找不到再找平台的 tasks/；没有定义返回 None。"""
    from control_center.paths import TASKS

    for r in dict.fromkeys([root or TASKS, TASKS]):
        if found := listen_defs(r).get(name):
            return found[0]
    return None


def listen_users(root: Path | None = None) -> dict[str, list[str]]:
    """监听名 → 写了 listen: <它> 的任务名（定义者与引用者）。"""
    out: dict[str, list[str]] = {}
    for n, raw, _ in _raw_task_paths(root):
        if isinstance(raw.get("listen"), str) and raw["listen"]:
            out.setdefault(raw["listen"], []).append(n)
    return out


def listen_dependents(task: str, root: Path | None = None) -> tuple[str, list[str]] | None:
    """task 定义了监听、还有别的任务引用它时返回 (监听名, 引用者)；下线 / 删除 / 改名定义者前查。"""
    for name, defs in listen_defs(root).items():
        if any(n == task for n, _ in defs) and (refs := [n for n in listen_users(root).get(name, []) if n != task]):
            return name, refs
    return None


def listen_dependents_hint(task: str, name: str, refs: list[str]) -> str:
    """定义者不能直接下线 / 删除 / 改监听名时的改法。"""
    return (f"任务 {task} 定义了监听 {name}（tasks/{task}/listen.py），还有任务 {'、'.join(refs)} 引用它。"
            f"先把定义迁到另一个任务的文件夹（把 listen.py 移到其中一个引用者的 tasks/<任务>/ 下，它成为定义者），或先撤掉这些任务的 listen: {name}，再下线 / 删除 / 改名")


def _events(raw: dict[str, Any]) -> list[str]:
    ev = raw.get("listen_events")
    return [str(x) for x in ev] if isinstance(ev, list) else []


def subscribers(source: str, enabled_only: bool = False) -> list[str]:
    """订阅了事件来源 source 的任务名（enabled_only：只要 yaml 写了 enabled: true 的）。"""
    return [n for n, raw in _raw_tasks() if source in _events(raw) and (not enabled_only or raw.get("enabled") is True)]


def listen_functions(f: Path) -> dict[str, bool]:
    """listen.py 里顶层定义的 check / on_event / watch → 签名对不对（check 能以 (ctx) 调、on_event 与 watch 能以 (event, ctx) / (ctx, emit) 调）。只解析语法，不执行；
    读不了或语法错抛 TaskError。"""
    try:
        text = f.read_text()
    except OSError as e:
        raise TaskError(f"读不了监听程序 {f}：{e}") from None
    return listen_source_functions(text, str(f))


def listen_source_functions(text: str, label: str = LISTEN_FILE) -> dict[str, bool]:
    """同 listen_functions，给源码文本（builder 计划里的 listen.py）。"""
    import ast

    try:
        tree = ast.parse(text, label)
    except SyntaxError as e:
        raise TaskError(f"监听程序 {label} 语法错：第 {e.lineno} 行 {e.msg}") from None
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in LISTEN_ARGS:
            a, n = node.args, LISTEN_ARGS[node.name]
            pos = len(a.posonlyargs) + len(a.args)
            out[node.name] = (isinstance(node, ast.FunctionDef) and (pos >= n or a.vararg is not None) and pos - len(a.defaults) <= n
                              and all(d is not None for d in a.kw_defaults))
    return out


LISTEN_ARGS = {"check": 1, "on_event": 2, "watch": 2}   # 监听程序的函数 → 平台调用时给几个位置参数


def _normalize_tools(raw: Any) -> list[dict[str, Any]]:
    """YAML 里 `- workspace` / `- obsidian: {guide: …}` 两种写法统一成 ToolUse 的输入。工具项只能写网关项（GATEWAY_KEYS）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TaskError("tools 必须是列表")
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            name, opts = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, opts = next(iter(item.items()))
            opts = opts or {}
        else:
            raise TaskError(f"tools 条目格式不对：{item!r}；每项写工具名（如 - workspace），或只有一个键的映射：工具名: 工具项（如 - obsidian: {{guide: …}}）")
        if str(name) not in TOOLS and subagents.is_folder(str(name)):
            raise TaskError(f"{name} 是子代理（主模型直接调用）：从 tools 删掉，写进 subagents（- {name}）")
        spec = TOOLS.get(str(name))
        if spec is None:
            raise TaskError(f"未登记的工具：{name}（stb tools list 查看可用工具）")
        if spec.usable_by == "subagent":
            who = _listed_by(name)
            raise TaskError(f"{name} 只给子代理用（tool.yaml 写了 usable_by: subagent），任务的 tools 里不能写；"
                            + (f"从 tools 删掉，在 subagents 里写 {' / '.join(who)}（它的工具里有 {name}），让主模型调用它"
                               if who else "要用它就交给在子代理 yaml 的 tools 里列了它的子代理（任务 yaml 的 subagents 写子代理名）"))
        if not isinstance(opts, dict):
            raise TaskError(f"工具 {name} 的选项必须是映射")
        if unknown := [k for k in opts if k not in GATEWAY_KEYS]:
            raise TaskError(f"工具 {name} 的 {unknown} 不是能写的项：工具项只能写 {', '.join(GATEWAY_KEYS)}（{FIELD_TABLE} task）；"
                            f"从 {name} 下删掉这几项，没有别的项就写成 - {name}")
        out.append({"name": name, "guide": _guide_opt(name, opts.get("guide"))})
    return out


def _normalize_subagents(raw: Any) -> list[dict[str, Any]]:
    """任务 yaml 的 subagents：`- image` / `- isolated-browser: {guide: …}` 两种写法统一成 SubagentUse 的输入；只能写 guide。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TaskError("subagents 必须是列表")
    out = []
    for item in raw:
        if isinstance(item, str):
            name, opts = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, opts = next(iter(item.items()))
            opts = opts or {}
        else:
            raise TaskError(f"subagents 条目格式不对：{item!r}；每项写子代理名（如 - image），或 - 名字: {{guide: …}}")
        if not isinstance(opts, dict) or (extra := sorted(set(opts) - {"guide"})):
            raise TaskError(f"子代理 {name} 的选项只能写 guide（本任务追加的规则）" + (f"，多了 {extra}" if isinstance(opts, dict) else ""))  # pyright: ignore[reportPossiblyUnboundVariable]
        out.append({"name": str(name), "guide": _guide_opt(str(name), opts.get("guide"))})
    return out


def _guide_opt(name: str, v: Any) -> str:
    """tools 项的 guide：本任务追加给这个工具的用法规则（多行文字）。"""
    if v is None:
        return ""
    if not isinstance(v, str):
        raise TaskError(f"工具 {name} 的 guide 要写成文字（多行用 guide: |）")
    return v.strip()


def load_task(path: Path, models: dict[str, dict[str, Any]] | None = None, refresh: bool = True, folder: Path | None = None) -> Task:
    """解析 + 校验。返回 task；拒绝时抛 TaskError。refresh：先重扫 tools/*/tool.yaml（builder 部署的新工具不用重启就认得）。
    folder：任务文件夹（path 不是 <文件夹>/task.yaml 时给，如网页保存前校验的临时文件）：listen.py、hook 文件在这里找。"""
    if refresh:
        refresh_tools()
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise TaskError(f"YAML 解析失败：{e}") from None
    if not isinstance(raw, dict):
        raise TaskError("任务文件顶层必须是映射")
    if raw.get("tools") is not None:                    # ${KEY} 只在 tools 下的配置值里展开，取自 .env（个人标识不写进仓库）；凭证键不许展开
        try:
            raw["tools"] = env.expand_tree(raw["tools"], {*PLATFORM_CREDENTIALS, *credential_keys()})
        except env.EnvError as e:
            raise TaskError(str(e)) from None
    if "timeout" not in raw:
        raise TaskError("timeout 必填（agent 任务：主模型时间上限，到点先收尾再结束，记 partial；workflow 任务：各步骤在跑的时间合计，到点终止）")
    if "enabled" not in raw:
        raise TaskError("enabled 必填（true / false）")
    if "steps" in raw:
        if "runtime" in raw or "model" in raw:
            raise TaskError("workflow 任务不写 runtime / model：要模型的步骤把档位写在第 4 项，如 - [2, chains.py, true, {runtime: vllm-eugr, model: …}]")
        if "effort" in raw:
            raise TaskError("workflow 任务不写 effort：步骤里的脚本自己调模型，档位由脚本在请求里定")
    elif "runtime" not in raw or "model" not in raw:
        raise TaskError("runtime 与 model 必填（如 runtime: vllm-eugr + model: local-inference-lab/Qwen3.8-Flash-Next-NVFP4，要对上 models.yaml 的一个档位）")
    if unknown := sorted(set(raw) - set(Task.model_fields)):
        raise TaskError(f"未知字段 {unknown}（可用：{', '.join(Task.model_fields)}；{FIELD_TABLE} task）")
    raw = dict(raw)
    raw["tools"] = _normalize_tools(raw.get("tools"))
    raw["subagents"] = _normalize_subagents(raw.get("subagents"))
    try:
        task = Task.model_validate(raw)
    except ValidationError as e:
        raise TaskError("任务定义不合法：" + "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from None
    if task.effort is not None and task.effort not in EFFORTS:
        raise TaskError(f"effort 写 {' / '.join(EFFORTS)} 之一（还要是模型模板认的），现在是 {task.effort!r}；不需要就不写")
    task._dir = (folder or path.parent).absolute()
    task._root = task._dir.parent
    if task.hooks:
        try:
            task._hooks = hooks.parse(f"任务 {task.name}", task.hooks, task._dir)
        except ValueError as e:
            raise TaskError(str(e)) from None
    if not unlimited(task.timeout):
        parse_duration(task.timeout)  # pyright: ignore[reportArgumentType]
    if bad := [m for m in task.memory if not str(m).strip() or not Path(str(m)).expanduser().is_absolute()]:
        raise TaskError(f"memory 写记忆文件或文件夹的绝对路径（可以 ~ 开头），如 ~/Obsidian/USER/USER.md：{bad}")
    if task.workflow:
        if task.system_prompt.strip() or task.tools or task.subagents or task.memory:
            raise TaskError("workflow 任务不起 agent：不写 system_prompt / tools / subagents / memory")
        task._steps = parse_steps(task.steps, task._dir)
    elif not task.system_prompt.strip():
        raise TaskError("system_prompt 必填（固定说明，定时任务的任务说明也写这里；步骤固定、不需要 agent 的活写成 workflow 任务的 steps）")
    _check_location(path, task)
    _check_trigger(task)
    _check_command_decl(task)
    if "resumable" in raw and not task.workflow:
        raise TaskError("agent 任务不写 resumable：agent 任务一律可被私信抢占、从检查点续跑；resumable 只给 workflow 任务（steps:）")
    if task.schedule is not None:
        try:
            check_schedule(task.schedule)
        except ValueError as e:
            raise TaskError(f"schedule 不合法：{e}") from None
    callees = task_callees(task)                 # 主模型直接调用的子代理（subagents/<名>/）：缺字段、未知工具等在这里拒
    if models is not None and task.workflow:
        from control_center.models import exclusive

        for st in task.step_list:
            if (prof := task.step_profile(models, st.n)) is not None and not exclusive(models[prof]) and bool(models[prof].get("start")) != bool(models[prof].get("stop")):
                raise TaskError(f"模型档位 {prof}（第 {st.n} 步）缺 start / stop 命令，无法切换")
    elif models is not None:
        main = task.profile_name(models)
        from control_center.models import main_missing

        if missing := main_missing(models[main]):
            raise TaskError(f"runtime {task.runtime} + model {task.model} 的档位 {main} 当不了 agent 任务的主模型：models.yaml 里缺 {'、'.join(missing)}")
        try:
            subs = {n: subagents.profile_name(d, models, main) for n, d in callees.items()}
        except subagents.SubagentError as e:
            raise TaskError(str(e)) from None
        from control_center.models import exclusive

        for model in dict.fromkeys([main, *subs.values()]):   # D32：子代理可用自己的 runtime + model（子档位不要求 context_window）
            prof = models[model]
            if not exclusive(prof) and bool(prof.get("start")) != bool(prof.get("stop")):   # 独占档位没有服务可起停；都没写 = 常驻接口（models.resident）
                raise TaskError(f"模型档位 {model} 缺 start / stop 命令，无法切换")
    # memory 清单与 memory / search-history 工具任何任务都可以用；读不可信外部内容的任务给不给记忆写入工具由布置任务的人决定
    names = {t.name for t in task.tools}
    if len(names) != len(task.tools):
        listed = [t.name for t in task.tools]
        dup = sorted({n for n in listed if listed.count(n) > 1})
        raise TaskError(f"同一工具不能列两次：{dup}；把两处合并成一项（键都写进同一个 - {dup[0]}: {{…}}），删掉另一项")
    owner: dict[str, str] = {}
    for t in task.tools:                         # 两个工具有同名函数，网关起不来（TOOL_CONFLICT）：check 时就拒
        for fn in functions(TOOLS[t.name]):
            if owner.setdefault(fn, t.name) != t.name:
                raise TaskError(f"函数名 {fn} 在工具 {owner[fn]} 和 {t.name} 里都有，两个工具不能同时用（只留一个，或给新工具的函数改名）")
    if len(callees) != len(task.subagents):
        raise TaskError(f"同一子代理不能列两次：{sorted({u.name for u in task.subagents if [x.name for x in task.subagents].count(u.name) > 1})}")
    if clash := sorted(set(callees) & set(owner)):    # 子代理名就是主模型看到的调用名
        raise TaskError(f"子代理 {clash} 和工具 {', '.join(owner[c] for c in clash)} 的函数同名，主模型分不开：两个不能同时用")
    return task


def _check_location(path: Path, task: Task) -> None:
    """tasks/ 里的定义：每个任务一个文件夹 tasks/<名>/task.yaml，文件夹名必须等于 name。"""
    from control_center.paths import TASK_FILE, TASKS

    tasks = TASKS.resolve()
    p = path.resolve()
    if p.parent == tasks:
        raise TaskError(f"任务定义写成文件夹 tasks/{task.name}/{TASK_FILE}（不是 tasks/ 下的单个 yaml）")
    if p.name == TASK_FILE and p.parent.parent == tasks and p.parent.name != task.name:
        raise TaskError(f"name（{task.name}）要和文件夹名 {p.parent.name} 一致")


def parse_steps(raw: Any, folder: Path | None) -> list[Step]:
    """workflow 的 steps → [Step]：每步 [编号, 文件, 要不要资源] 或 [编号, 文件, true, {runtime, model}]；编号从 1 起连续，文件是任务文件夹里的 .py。不合格抛 TaskError。"""
    how = "每步一行 [编号, 文件, 要不要资源]，要模型的加第 4 项，如 - [1, ingest.py, false]、- [2, chains.py, true, {runtime: vllm-eugr, model: …}]"
    if not isinstance(raw, list) or not raw:
        raise TaskError(f"steps 写成非空列表：{how}")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, list) or len(item) not in (3, 4):
            raise TaskError(f"steps 第 {i} 项 {item!r} 格式不对：{how}")
        n, f, res = item[:3]
        if type(n) is not int or n != i:
            raise TaskError(f"steps 的编号从 1 起连续、按顺序写：第 {i} 项写了 {n!r}")
        if not isinstance(f, str) or "/" in f or not f.endswith(".py") or f.startswith("."):
            raise TaskError(f"第 {i} 步的文件写任务文件夹里的 .py 文件名（不带路径）：{f!r}")
        if folder is not None and not (folder / f).is_file():
            raise TaskError(f"第 {i} 步的 {f} 不在任务文件夹 {folder} 里")
        if not isinstance(res, bool):
            raise TaskError(f"第 {i} 步的第 3 项写 true / false（要不要资源：GPU、大内存、模型）：{res!r}")
        runtime = model = None
        if len(item) == 4:
            m = item[3]
            if not res:
                raise TaskError(f"第 {i} 步不要资源（false），不能写模型（第 4 项）")
            if not isinstance(m, dict) or set(m) != {"runtime", "model"} or not all(isinstance(v, str) and v for v in m.values()):
                raise TaskError(f"第 {i} 步的第 4 项写 {{runtime: …, model: …}}（要对上 models.yaml 的一个档位）：{m!r}")
            runtime, model = m["runtime"], m["model"]
        out.append(Step(n, f, res, runtime, model))
    return out


def _check_command_decl(task: Task) -> None:
    """任务级 network：只给有 listen.py 的任务或 workflow 任务（这些以 sparky 跑，按它限制联网）；写法同 tool.yaml。"""
    from control_center import registry

    if task.network is None:
        return
    if not task.workflow and not task.defines_listen:
        raise TaskError("network 只给定义监听（文件夹里有 listen.py）的任务或 workflow 任务（限制它们联网）；agent 任务的工具在 tool.yaml 里声明"
                        + (f"；引用的监听按定义者 {task.listen_owner or '（找不到）'} 的 network 跑，写在它的 task.yaml 里" if task.listen is not None else ""))
    if not isinstance(task.network, bool) and not registry._ports(task.network):
        raise TaskError("network 写 true / false，或本机端口列表（如 [8188]）")


def _check_trigger(task: Task) -> None:
    """trigger 与 schedule / listen 对得上；监听程序在、函数签名对（_check_listen）。"""
    kind = task.triggers[0]
    if (kind == "schedule") != (task.schedule is not None):
        raise TaskError("trigger: schedule 时必须写 schedule，别的触发方式不能写")
    if (kind == "listen") != (task.listen is not None):
        raise TaskError("trigger: listen 时必须写 listen（监听名，监听程序放任务文件夹里的 listen.py）；别的触发方式不写 listen")
    if task.listen is not None:
        _check_listen(task)
    elif task.listen_events:
        raise TaskError("listen_events 要和 listen（监听名 + 任务文件夹里的 listen.py）一起写")


def _check_listen(task: Task) -> None:
    """listen：监听名格式；自己定义的（文件夹里有 listen.py）同名只能这一个定义，引用的要有定义者；要被调的函数在、签名对；事件来源格式。"""
    name = str(task.listen)
    if not NAME_RE.fullmatch(name):
        raise TaskError(f"listen 写监听名（小写字母、数字、- _，如 news）：{name!r}")
    if name == BUILTIN_LISTEN:                     # 平台自带：门口按请求触发，不跑 listen.py
        if task._dir is not None and (task._dir / LISTEN_FILE).is_file():
            raise TaskError(f"监听 {name} 是平台自带的（算力 API 的门口），任务文件夹里不放 {LISTEN_FILE}")
        if task.listen_events:
            raise TaskError(f"监听 {name} 是平台自带的（算力 API 的门口），不写 listen_events")
        if not task.name.startswith(f"{BUILTIN_LISTEN}-"):
            raise TaskError(f"listen: {name} 只给控制台「API」页建的任务（名字以 {BUILTIN_LISTEN}- 开头）")
        return
    if task.defines_listen:
        if others := [n for n, f in listen_defs(task._root).get(name, []) if f.parent.resolve() != task._dir.resolve()]:  # pyright: ignore[reportOptionalMemberAccess]
            raise TaskError(f"监听 {name} 已由任务 {'、'.join(others)} 定义（tasks/{others[0]}/{LISTEN_FILE}）：同名只能有一个定义。"
                            f"要复用就删掉 tasks/{task.name}/{LISTEN_FILE}（本任务改为引用）；要另写一个就换监听名")
    elif task.listen_file is None:
        known = sorted({*listen_defs(task._root), *listen_defs()})
        raise TaskError(f"监听 {name} 不存在：没有任务定义它（定义 = 任务文件夹 tasks/<任务>/ 里有 {LISTEN_FILE}，task.yaml 写 listen: {name}）。"
                        f"改法：引用已有的监听（现有：{'、'.join(known) or '没有'}），或在本任务的文件夹里写 tasks/{task.name}/{LISTEN_FILE} 定义它")
    f = task.listen_file
    assert f is not None                           # 上面已拦下找不到定义的
    if bad := [e for e in task.listen_events if not NAME_RE.fullmatch(e)]:
        raise TaskError(f"listen_events 写事件来源名（小写字母、数字、- _，如 message）：{bad}")
    fns = listen_functions(f)
    if bad := [k for k, ok in fns.items() if not ok]:
        raise TaskError(f"{f.name} 的 {'、'.join(bad)} 签名不对：要写成 def check(ctx) / def on_event(event, ctx) / def watch(ctx, emit)（普通函数，不是 async）")
    if task.listen_events and "on_event" not in fns:
        raise TaskError(f"listen_events 订阅了 {task.listen_events}，{f.name} 里要有 on_event(event, ctx)")
    if not (task.listen_events or "watch" in fns) and "check" not in fns:
        raise TaskError(f"{f.name} 里要有 check(ctx)：没订阅事件、也没有常驻的 watch(ctx, emit)，只能按间隔调 check(ctx)")


def _listed_by(tool: str) -> list[str]:
    """tools 里列了这个工具的文件夹写法子代理（读坏的跳过）：任务误写只给子代理用的工具时，报错里告诉它该调哪个子代理。"""
    out = []
    for n in subagents.folder_names():
        try:
            if tool in subagents.load_def(n).tools:
                out.append(n)
        except subagents.SubagentError:
            continue
    return out
