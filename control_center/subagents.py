"""子代理配置（2026-10-05 Joy 定，§3.2）：subagents/<名>/subagent.yaml（文件夹写法）+ 同文件夹的约定文件，git 管理。

子代理是工具的升级版：任务 yaml 的 subagents 列出它，主模型就看到一个叫 <名> 的可调用项（description 只来自它的 yaml），传 prompt（agent 可附 files，
generator / engine 另传 yaml 里声明的 inputs）；调用期间主模型冻结，网关按它的种类跑完、交回结果（control_center/subagent_run.py、gateway.py 的 _call_subagent）。
子代理不能调子代理。三种（kind）与各自能写的字段见 KIND_FIELDS（按它逐项校验，写了不属于这种的字段就拒，stb schema subagents 也按它生成手册的表）：
- agent：语言模型带工具（system_prompt + tools 必填），网关另起沙箱容器跑 pi（同主 agent），交回最后的回复（如隔离读者 isolated-reader、浏览器 isolated-browser）；
- generator：生成程序（文件夹里的 server.py + inputs 必填），prompt 与精确参数进、文件出，一定用自己的模型档位（不能 inherit）（如 image、video）；
- engine：自带循环的程序（文件夹里的 server.py，inputs 可写），prompt 进、结构化结果出（如联网研究 isolated-web-search）。
isolated: true（读外部内容的 agent / engine 必须写，读外部内容的工具是 usable_by: subagent、yaml 里注释写明的那些），这样的子代理 is_editable 必须写 false（只由 Joy 建、改）：
- agent：结果只能是带原文引文的答案，平台逐字核对后才交给主模型（每条的 question 也要照抄主模型这次调用写的文字；可带 item 标第几项）；
- engine：交回它自己 harness 校验过的结果（由它的 server 程序按格式校验；联网研究：沙箱研究容器里按 schema 校验过的 ResearchResult），
  平台不做问答引文核对，结果（JSON 对象）原样交给主模型。约定文件：subagent.yaml（配置）、server.py（generator / engine 的程序）、
guide.md（主模型这次运行第一次调用时先返回、不执行）、output.py（检查 / 整理结果，退出码 2 打回重写；
isolated 的在平台引文核对之后跑，只能更严）、hooks 的文件（同工具，事件 pre_call / post_call / run_start / run_end）。
写了表外的字段就拒（未知字段，报错指向手册的字段表）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from control_center import scheme_check
from control_center.paths import REPO

DIR = REPO / "subagents"
INHERIT = "inherit"


class SubagentError(ValueError):
    pass


def profile_name(agent: "SubagentDef", models: dict[str, Any], task_profile: str) -> str:
    """子代理的模型档位名：inherit 就是任务的档位，否则按 runtime + model 对 models.yaml（对不上抛 SubagentError）。"""
    if agent.inherit:
        return task_profile
    from control_center.models import profile_for

    name = profile_for(models, agent.runtime, agent.model)
    if name is None:
        raise _def_err(agent.name, f"runtime {agent.runtime} + model {agent.model} 在 models.yaml 里没有对应档位（runtime 与 model_path 都要对上）")
    return name


# ---- 文件夹写法（subagents/<名>/subagent.yaml）----
KINDS = ("agent", "generator", "engine")
REQ, OPT, NO = "必填", "可写", "不写"
# 字段 → (类型, {种类: 必填 / 可写 / 不写}, 允许的值, 说明)：来自字段表 control_center/schema/fields.yaml（scheme_check）；load_def 按它校验，stb schema subagents 按它生成手册的表
KIND_FIELDS: dict[str, tuple[str, dict[str, str], str, str]] = {k: (v["type"], dict(v["kinds"]), v.get("allowed", ""), v["desc"])
                                                                for k, v in scheme_check.table("subagent").items()}
INPUT_KEYS = {"type", "description", "default", "enum", "items", "minimum", "maximum", "maxLength", "maxItems"}
INPUT_TYPES = ("string", "integer", "number", "boolean", "array", "object")
RESERVED_INPUTS = ("prompt", "files")
YAML_FILE = "subagent.yaml"             # 子代理文件夹里的约定文件：配置
SERVER_FILE = "server.py"               # generator / engine 的程序（MCP 服务端，注册一个和子代理同名的函数，参数 prompt + inputs）：
                                        # is_editable: false 以平台身份跑，true 以 sparky 跑（文件看 sparky 的文件权限，平台开好本次运行的 media 目录，只连它模型档位的端口）
GUIDE_FILE = "guide.md"
OUTPUT_FILE = "output.py"


@dataclass(frozen=True)
class SubagentDef:
    """文件夹写法的子代理（subagents/<名>/subagent.yaml + 约定文件）。主模型调用它：网关按 kind 跑（control_center/subagent_run.py）。"""
    name: str
    description: str
    kind: str                               # agent / generator / engine
    runtime: str
    model: str
    timeout: int | None                     # 单次调用上限（秒）；None = 不限（yaml 写 none，同任务 timeout 的 none）
    folder: Path
    system_prompt: str | None = None        # agent
    tools: tuple[str, ...] = ()             # agent
    server: Path | None = None              # generator / engine：本文件夹里的 server.py（MCP 服务端）
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)   # 参数名 → JSON schema
    isolated: bool = False
    is_editable: bool = True
    hooks: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    guide: str = ""                         # guide.md：主模型这次运行第一次调用时先返回
    output: Path | None = None              # output.py：检查 / 整理结果
    effort: str | None = None               # agent / engine：推理档位（task.EFFORTS 之一）；None = 不传

    @property
    def inherit(self) -> bool:
        return self.model == INHERIT

    @property
    def prevalidated(self) -> bool:
        """isolated 的 engine：交回它自己 harness 校验过的结果，平台不做问答引文核对，原样转交。"""
        return self.isolated and self.kind == "engine"

    def parameters(self) -> dict[str, Any]:
        """主模型看到的参数（JSON schema）：prompt 必传；agent 可附 files（工作目录里的路径），generator / engine 另有 inputs（没写 default 的必传）。"""
        props: dict[str, Any] = {"prompt": {"type": "string", "description": "交给子代理的任务、问题、侧重点"}}
        required = ["prompt"]
        if self.kind == "agent":
            props["files"] = {"type": "array", "items": {"type": "string"}, "description": "可选：附给子代理的文件（工作目录下已有文件的绝对路径）"}
        for k, v in self.inputs.items():
            props[k] = dict(v)
            if "default" not in v:
                required.append(k)
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def def_path(name: str) -> Path:
    """子代理的配置文件 subagents/<名>/subagent.yaml。"""
    return Path(DIR) / name / YAML_FILE


def is_folder(name: str) -> bool:
    """name 是文件夹写法的子代理（subagents/<名>/subagent.yaml 在）。"""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name)) and def_path(name).is_file()


def folder_names() -> list[str]:
    """subagents/ 下全部子代理名（有 subagent.yaml 的文件夹；下划线开头的不算）。"""
    return [p.parent.name for p in sorted(Path(DIR).glob(f"*/{YAML_FILE}")) if not p.parent.name.startswith("_")]


def _def_err(name: str, msg: str) -> SubagentError:
    return SubagentError(f"subagents/{name}/{YAML_FILE}：{msg}")


def load_def(name: str) -> SubagentDef:
    """读并校验文件夹写法的子代理 name（subagents/<名>/subagent.yaml 与约定文件）；不合格抛 SubagentError（中文说明哪一项不对）。"""
    p = def_path(name)
    if not p.is_file():
        raise _def_err(name, f"文件不存在（子代理是文件夹 subagents/<名>/，配置是里面的 {YAML_FILE}）")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise _def_err(name, f"YAML 解析失败：{e}") from None
    return parse_def(name, raw)


def parse_def(name: str, raw: Any, folder: Path | None = None, tools: Any = None, check_files: bool = True) -> SubagentDef:
    """文件夹写法的 yaml 内容（已解析）→ SubagentDef：按 KIND_FIELDS 查这一种必填的写齐了、不属于这一种的没写，再逐项查值。
    folder：子代理文件夹（默认 subagents/<名>/）；tools：认得的工具名（默认登记的工具 registry.TOOLS；builder 的计划另加本计划的新工具）；
    check_files：查 server.py、hooks 的文件在不在（builder 的计划还没写文件时不查，部署检查时查）。"""
    from control_center.registry import LOADER, TOOLS
    from control_center.task import EFFORTS, TaskError, parse_duration

    known = TOOLS if tools is None else tools

    if not isinstance(raw, dict):
        raise _def_err(name, "顶层要是映射")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise _def_err(name, f"kind 写 {' / '.join(KINDS)}，现在是 {kind!r}")
    if unknown := sorted(set(raw) - set(KIND_FIELDS)):
        raise _def_err(name, f"未知字段 {unknown}（可用：{', '.join(KIND_FIELDS)}；字段表见手册 How-to-set-a-task-on-Sparky.md，或 stb schema subagents）")
    if wrong := [k for k in raw if KIND_FIELDS[k][1][kind] == NO]:
        raise _def_err(name, f"kind: {kind} 不写 {wrong}（{kind} 能写：{', '.join(k for k, v in KIND_FIELDS.items() if v[1][kind] != NO)}）")
    if missing := [k for k, v in KIND_FIELDS.items() if v[1][kind] == REQ and k not in raw]:
        raise _def_err(name, f"kind: {kind} 缺必填字段 {missing}")
    if raw["name"] != name:
        raise _def_err(name, f"name（{raw['name']}）要等于文件夹名：{name}")
    if name in known:
        raise _def_err(name, f"和工具 {name} 重名：子代理名就是主模型看到的调用名，不能和工具撞")
    if not isinstance(raw["description"], str) or not raw["description"].strip():
        raise _def_err(name, "description 要是非空字符串")
    rt, model = raw["runtime"], raw["model"]
    if not (isinstance(rt, str) and rt and isinstance(model, str) and model):
        raise _def_err(name, "runtime 与 model 要是非空字符串")
    if (rt == INHERIT) != (model == INHERIT):
        raise _def_err(name, "runtime 与 model 要么都写 inherit（同调用它的任务），要么都写具体的")
    if kind == "generator" and rt == INHERIT:
        raise _def_err(name, "generator 一定用自己的模型档位（生成档位），不能写 inherit")
    try:
        t = raw["timeout"]
        timeout = None if isinstance(t, str) and t.strip().lower() == "none" else parse_duration(t)
    except TaskError as e:
        raise _def_err(name, f"timeout {e}（不限时写 none）") from None
    if timeout is not None and timeout <= 0:
        raise _def_err(name, "timeout 要大于 0（不限时写 none）")
    effort = raw.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise _def_err(name, f"effort 写 {' / '.join(EFFORTS)} 之一（还要是模型模板认的），现在是 {effort!r}；不需要就不写")
    for k in ("isolated", "is_editable"):
        if k in raw and not isinstance(raw[k], bool):
            raise _def_err(name, f"{k} 要写 true / false")
    isolated, editable = bool(raw.get("isolated", False)), bool(raw.get("is_editable", True))
    if isolated and editable:
        raise _def_err(name, "isolated: true 的子代理 is_editable 必须写 false（外部内容隔离只由 Joy 改）")
    folder = folder or Path(DIR) / name
    output = folder / OUTPUT_FILE
    out: dict[str, Any] = {}
    if kind == "agent":
        sp = raw["system_prompt"]
        if not (isinstance(sp, str) and sp.strip()):
            raise _def_err(name, "system_prompt 要是非空字符串")
        tools = raw["tools"]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise _def_err(name, "tools 要是工具名列表（没有就写 []）")
        if bad := [t for t in tools if t not in known]:
            raise _def_err(name, f"tools 里的 {bad} 不是登记的工具（stb tools list 查看）；子代理不能调子代理，子代理名也不能写在这里")
        if LOADER in tools:
            raise _def_err(name, f"tools 里不能写 {LOADER}：子代理的工具表是固定的，加载只给主模型")
        out.update(system_prompt=sp.strip(), tools=tuple(dict.fromkeys(tools)))
    else:
        if check_files and not (folder / SERVER_FILE).is_file():
            raise _def_err(name, f"kind: {kind} 的程序是本文件夹（{folder}）里的 {SERVER_FILE}，现在没有这个文件")
        out["server"] = (folder / SERVER_FILE).resolve()
        out["inputs"] = _inputs(name, raw.get("inputs"), required=kind == "generator")
    try:
        from control_center import hooks

        hk = hooks.parse(f"subagents/{name}/{YAML_FILE}", raw.get("hooks"), folder) if check_files else {}
    except ValueError as e:
        raise SubagentError(str(e)) from None
    guide = folder / GUIDE_FILE
    return SubagentDef(name=name, description=raw["description"].strip(), kind=kind, runtime=rt, model=model, timeout=timeout, folder=folder, effort=effort,
                       isolated=isolated, is_editable=editable, hooks=hk, guide=guide.read_text().strip() if guide.is_file() else "",
                       output=output.resolve() if output.is_file() else None, **out)


def _inputs(name: str, raw: Any, required: bool) -> dict[str, dict[str, Any]]:
    """inputs：参数名 → JSON schema（type、description 必写；写了 default 的主模型可不传）。generator 必须至少一个。"""
    if raw is None and not required:
        return {}
    if not isinstance(raw, dict) or (required and not raw):
        raise _def_err(name, "inputs 要写成映射 {参数名: {type, description, default?, …}}" + ("（generator 至少一个参数）" if required else ""))
    from control_center.reader import FORBIDDEN_KEYS   # 网关按 reader.check 查参数：这些键名在那里算指挥字段

    out = {}
    for k, v in raw.items():
        at = f"inputs.{k}"
        if not (isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k)) or k in RESERVED_INPUTS or k in FORBIDDEN_KEYS:
            raise _def_err(name, f"{at}：参数名写小写字母、数字、下划线，不能是 {' / '.join([*RESERVED_INPUTS, *sorted(FORBIDDEN_KEYS)])}")
        if not isinstance(v, dict) or (extra := set(v) - INPUT_KEYS):
            raise _def_err(name, f"{at} 要写成 {{type, description, default?, …}}" + (f"（多了 {sorted(extra)}；能写 {', '.join(sorted(INPUT_KEYS))}）" if isinstance(v, dict) else ""))  # pyright: ignore[reportPossiblyUnboundVariable]
        if v.get("type") not in INPUT_TYPES:
            raise _def_err(name, f"{at}.type 写 {' / '.join(INPUT_TYPES)}")
        if not (isinstance(v.get("description"), str) and v["description"].strip()):
            raise _def_err(name, f"{at}.description 要写（给主模型看：这个参数是什么、怎么填）")
        if v["type"] == "array" and not (isinstance(v.get("items"), dict) and v["items"].get("type") in INPUT_TYPES):
            raise _def_err(name, f"{at}：数组要写 items: {{type: …}}")
        out[k] = dict(v)
    return out


def load_defs() -> list[SubagentDef]:
    """subagents/ 下全部子代理（stb subagents list、网页用）；有一份不合格就抛 SubagentError。"""
    return [load_def(n) for n in folder_names()]

