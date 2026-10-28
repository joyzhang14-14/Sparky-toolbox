"""stb schema（D1）：从字段表 control_center/schema/fields.yaml（control_center/scheme_check.py）生成 Markdown 字段表（字段、类型、默认、必填、允许的值、说明），手册里的表就是它的输出。

- task：类型、默认、取值来自 control_center/task.py 的 pydantic 模型 Task，说明来自字段表的 task
- tool-yaml：字段表的 tool（registry.TOOL_FIELDS）与 RUN_ENV（网关给每个工具的运行信息）
- subagents：字段表的 subagent（subagents.KIND_FIELDS；subagents/<名>/subagent.yaml，按种类 agent / generator / engine 分列）
- models：字段表的 models 与 models._settings
- stbcc：字段表的 stbcc（config/stbcc.yaml，带点的是那一段里的键）
`stb schema --write-manual` 把手册里成对标记 <!-- schema:<名>:begin --> … <!-- schema:<名>:end --> 之间的内容换成最新的表（只动标记之间）。
"""
from __future__ import annotations

import re
import typing
from pathlib import Path
from typing import Any

from pydantic import BaseModel

KINDS = ("task", "tool-yaml", "subagents", "models", "stbcc")
MANUAL = Path.home() / "Obsidian" / "Sparky" / "How-to-set-a-task-on-Sparky.md"
HEAD = "| 字段 | 类型 | 默认 | 必填 | 允许的值 | 说明 |\n| --- | --- | --- | --- | --- | --- |"


def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _row(*cells: Any) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _type(ann: Any) -> tuple[str, str]:
    """(类型, 允许的值)。"""
    names, allowed = [], []

    def walk(a: Any) -> None:
        origin = typing.get_origin(a)
        if origin is typing.Literal:
            allowed.extend(str(x) for x in typing.get_args(a))
            names.append("str")
        elif origin in (typing.Union, getattr(__import__("types"), "UnionType")):
            for x in typing.get_args(a):
                walk(x)
        elif a is type(None):
            return
        elif origin in (list, dict):
            names.append("list" if origin is list else "map")
        elif isinstance(a, type) and issubclass(a, BaseModel):
            names.append("map")
        else:
            names.append(getattr(a, "__name__", str(a)))
    walk(ann)
    return " / ".join(dict.fromkeys(names)), " / ".join(dict.fromkeys(allowed))


def _model_rows(model: type[BaseModel], prefix: str = "", effective: dict[str, Any] | None = None) -> list[str]:
    """effective：模型里默认是 None、实际由代码补默认值的字段 → 表里显示的默认值。说明来自字段表的 task（带前缀的是那一项映射里的键）。"""
    from control_center import scheme_check

    notes = {k[len(prefix):]: v.get("desc", "") for k, v in scheme_check.table("task").items() if k.startswith(prefix) and "." not in k[len(prefix):]}
    rows = []
    for name, f in model.model_fields.items():
        typ, allowed = _type(f.annotation)
        if f.is_required():
            default, req = "—", "是"
        else:
            d = (effective or {}).get(name, f.get_default(call_default_factory=True))
            default = ("—" if d is None or isinstance(d, BaseModel) else '""' if d == "" else "[]" if d == [] else
                       str(d).lower() if isinstance(d, bool) else str(d))
            req = "否"
        sub = typing.get_args(f.annotation) if typing.get_origin(f.annotation) else (f.annotation,)
        nested = next((a for a in sub if isinstance(a, type) and issubclass(a, BaseModel)), None)
        rows.append(_row(f"`{prefix}{name}`", typ, default, req, allowed, notes.get(name, "")))
        if nested is not None and prefix == "" and name != "tools":
            rows += _model_rows(nested, f"{name}.")
    return rows


def task_table() -> str:
    from control_center.task import Task

    return "\n".join([HEAD, *_model_rows(Task)])


def _req(d: str) -> tuple[str, str]:
    """(默认, 必填)：默认那一栏写着"必填…"的，必填栏照抄（如"当主模型的档位必填"），默认栏写 —。"""
    return ("—", "是" if d == "必填" else d) if "必填" in d else (d, "否")


def tool_yaml_table() -> str:
    from control_center.registry import RUN_ENV, TOOL_FIELDS

    rows = [_row(f"`{k}`", t, *_req(d), "", note) for k, (t, d, note) in TOOL_FIELDS.items()]
    env = ["| 环境变量（网关一律给，不用声明） | 说明 |", "| --- | --- |", *(_row(f"`{k}`", v) for k, v in RUN_ENV.items())]
    return "\n".join([HEAD, *rows, "", *env])


def subagents_table() -> str:
    """subagents/<名>/subagent.yaml 按种类的表（KIND_FIELDS）。"""
    from control_center.subagents import KIND_FIELDS, KINDS

    return "\n".join(["| 字段（subagents/<名>/subagent.yaml） | 类型 | " + " | ".join(KINDS) + " | 允许的值 | 说明 |",
                      "| --- | --- | " + " | ".join("---" for _ in KINDS) + " | --- | --- |",
                      *(_row(f"`{k}`", t, *(by[x] for x in KINDS), allowed, note) for k, (t, by, allowed, note) in KIND_FIELDS.items())])


def models_table() -> str:
    from control_center.models import PROFILE_FIELDS, SETTINGS_FIELDS

    rows = [_row(f"`{k}`", t, *_req(d), "", note) for k, (t, d, note) in PROFILE_FIELDS.items()]
    rows += [_row(f"`_settings.{k}`", t, d, "否", "", note) for k, (t, d, note) in SETTINGS_FIELDS.items()]
    return "\n".join([HEAD, *rows])


def stbcc_table() -> str:
    from control_center import scheme_check

    return "\n".join([HEAD, *(_row(f"`{k}`", v["type"], *_req(str(v["default"])), "", v["desc"]) for k, v in scheme_check.table("stbcc").items())])


def table(kind: str) -> str:
    return {"task": task_table, "tool-yaml": tool_yaml_table, "subagents": subagents_table, "models": models_table, "stbcc": stbcc_table}[kind]()


def _marker(kind: str) -> re.Pattern[str]:
    return re.compile(rf"(<!-- schema:{re.escape(kind)}:begin -->\n)(.*?)(<!-- schema:{re.escape(kind)}:end -->)", re.S)


def render_manual(text: str) -> str:
    """把手册里每一对标记之间换成最新的表；没有标记的种类不动。"""
    for kind in KINDS:
        text = _marker(kind).sub(lambda m: m.group(1) + table(kind) + "\n" + m.group(3), text)
    return text


def stale(text: str) -> list[str]:
    """手册里和代码对不上的表（种类名）；缺标记的种类也算。"""
    return [k for k in KINDS if not (m := _marker(k).search(text)) or m.group(2) != table(k) + "\n"]
