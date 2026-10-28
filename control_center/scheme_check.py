"""字段表锁：control_center/schema/fields.yaml 是所有 yaml 字段（任务、tool.yaml、subagent.yaml、models.yaml、stbcc.yaml）的唯一来源（table），
代码按它校验、stb schema 按它生成手册的表；control_center/schema/fields.lock 是 Joy 批准过的字段表指纹（FIELDS_FILE 的 sha256 + 当时各种类的字段名）。

- problems()：字段表和 lock 对不上，或仓库里的 yaml 写了表外的字段（yaml_problems）。
- require(服务)：有 problems 就私信 Joy（经 stbcc-hub，不在就算了）并退出——stbcc-runner / stbcc-builder / stbcc-docker 启动时调，不一致就不启动；
  stbcc-hub 启动时看到 problems 只收发消息、不触发任务（不跑定时触发与监听），同样私信 Joy。
- approve()：`stb schema approve`——发平台的通用审批卡（列出和 lock 比加了 / 去了哪些字段），就地等 Joy；同意了才写 fields.lock。
改字段表、加字段必须先问 Joy（仓库根的 CLAUDE.md、AGENTS.md 写明）。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import yaml

from control_center.paths import REPO

FIELDS_FILE = REPO / "control_center" / "schema" / "fields.yaml"
LOCK_FILE = REPO / "control_center" / "schema" / "fields.lock"
KINDS = ("task", "tool", "subagent", "models", "models._settings", "stbcc")
FREE_MAPS = ("launcher.worker_env",)            # stbcc.yaml 里键由别的程序定的映射：不逐个查下面的键
APPROVE_WAIT_S = 1800                           # stb schema approve 等 Joy 处理审批卡最多多久
APPROVE_RUN = "cli/schema"                      # 审批卡 payload 的 run（不是任务运行）：卡按它和字段表指纹认，Joy 后来同意的下次 approve 直接用掉
_cache: dict[str, Any] = {}


def _fields() -> dict[str, dict[str, dict[str, Any]]]:
    """fields.yaml 的内容（按修改时间缓存）；写坏了抛 ValueError。"""
    st = FIELDS_FILE.stat()
    if _cache.get("key") != (st.st_mtime_ns, st.st_size):
        raw = yaml.safe_load(FIELDS_FILE.read_text())
        if not isinstance(raw, dict) or set(raw) != set(KINDS) or not all(isinstance(v, dict) for v in raw.values()):
            raise ValueError(f"{FIELDS_FILE} 要写成 {{种类: {{字段: {{…}}}}}}，种类正好是 {', '.join(KINDS)}")
        _cache.update(key=(st.st_mtime_ns, st.st_size), fields=raw)
    return _cache["fields"]


def table(kind: str) -> dict[str, dict[str, Any]]:
    """一种 yaml 的字段 → 定义（字段名带点的是那一项映射里的键）。"""
    return _fields()[kind]


def top(kind: str) -> list[str]:
    """一种 yaml 顶层能写的字段名。"""
    return [k for k in table(kind) if "." not in k]


def fingerprint() -> str:
    return hashlib.sha256(FIELDS_FILE.read_bytes()).hexdigest()


def _names() -> dict[str, list[str]]:
    return {k: list(table(k)) for k in KINDS}


def load_lock() -> dict[str, Any]:
    try:
        return json.loads(LOCK_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_lock() -> None:
    LOCK_FILE.write_text(json.dumps({"sha256": fingerprint(), "fields": _names()}, ensure_ascii=False, indent=1) + "\n")


def lock_ok() -> bool:
    return load_lock().get("sha256") == fingerprint()


def diff_text() -> str:
    """和 lock 比，各种类加了 / 去了哪些字段；名字都没变就说只改了说明 / 类型这些。"""
    old = load_lock().get("fields") or {}
    lines = []
    for k, names in _names().items():
        before = old.get(k) or []
        if added := [n for n in names if n not in before]:
            lines.append(f"➕ {k}：{'、'.join(added)}")
        if gone := [n for n in before if n not in names]:
            lines.append(f"➖ {k}：{'、'.join(gone)}")
    return "\n".join(lines) or "字段名没变（改了说明、类型、默认值这些）"


def _keys(raw: dict[str, Any], prefix: str = "") -> list[str]:
    """映射里所有的键路径（a、a.b…）；FREE_MAPS 下面的不展开。"""
    out = []
    for k, v in raw.items():
        path = f"{prefix}{k}"
        out.append(path)
        if isinstance(v, dict) and path not in FREE_MAPS:
            out += _keys(v, f"{path}.")
    return out


def _load(p: Path) -> Any:
    try:
        return yaml.safe_load(p.read_text())
    except (OSError, yaml.YAMLError):
        return None                                 # 读不了、写坏了：由各自的加载报错，这里只查字段


def yaml_problems() -> list[str]:
    """仓库里的 yaml 写了字段表以外的字段：任务、工具、子代理只查顶层，models.yaml 查档位与 _settings，stbcc.yaml 查到每一层。"""
    from control_center.paths import MODELS_FILE, STBCC_FILE, task_files

    out = []
    groups = (("task", list(task_files())), ("tool", sorted(REPO.glob("tools/*/tool.yaml"))), ("subagent", sorted(REPO.glob("subagents/*/subagent.yaml"))))
    for kind, files in groups:
        allowed = set(top(kind))
        for f in files:
            raw = _load(f)
            if isinstance(raw, dict) and (bad := sorted(set(raw) - allowed)):
                out.append(f"{f.relative_to(REPO) if f.is_relative_to(REPO) else f}：{bad} 不在字段表的 {kind} 里")
    raw = _load(MODELS_FILE)
    for name, prof in (raw.items() if isinstance(raw, dict) else []):
        kind = "models._settings" if name == "_settings" else "models"
        if isinstance(prof, dict) and (bad := sorted(set(prof) - set(top(kind)))):
            out.append(f"{MODELS_FILE.name} 的 {name}：{bad} 不在字段表的 {kind} 里")
    raw = _load(STBCC_FILE)
    if isinstance(raw, dict) and (bad := sorted(set(_keys(raw)) - set(table("stbcc")))):
        out.append(f"{STBCC_FILE.name}：{bad} 不在字段表的 stbcc 里")
    return out


def problems() -> list[str]:
    try:
        out = [] if lock_ok() else [f"字段表 {FIELDS_FILE.relative_to(REPO)} 和 Joy 批准过的 {LOCK_FILE.relative_to(REPO)} 对不上（{diff_text()}）："
                                    "改字段表要先问 Joy，他同意后 `stb schema approve`"]
        return out + yaml_problems()
    except (OSError, ValueError) as e:
        return [f"字段表读不了：{e}"]


def notify(service: str, probs: list[str]) -> None:
    """私信 Joy（经 stbcc-hub；它不在就算了）。"""
    import asyncio

    from control_center import hub_client

    text = f"⚠ {service} 因为字段表不通过而没启动 / 不触发任务：\n" + "\n".join(f"- {p}" for p in probs[:20])
    try:
        asyncio.run(hub_client.call("send", text=text))
    except Exception:  # noqa: BLE001 — 中枢不在：日志里有
        pass


def require(service: str) -> None:
    """服务启动前：有 problems 就私信 Joy 并退出（不启动）。"""
    if probs := problems():
        print(f"{service} 不启动：" + "；".join(probs), flush=True)
        notify(service, probs)
        raise SystemExit(1)


async def approve() -> str:
    """stb schema approve：字段表和 lock 对不上时发通用审批卡（加了 / 去了哪些字段），就地等 Joy（最多 APPROVE_WAIT_S 秒）；同意了写 fields.lock。"""
    from control_center import acquire_permission, cards, hub_client

    if lock_ok():
        return "字段表和 fields.lock 一致，不用批准"
    fp = fingerprint()
    if acquire_permission.take_approved(acquire_permission.PERMISSION_KIND, APPROVE_RUN, key=fp[:64]):   # 上次发的卡 Joy 后来同意了：直接用掉
        write_lock()
        return "Joy 已在审批卡上同意，已更新 control_center/schema/fields.lock"
    pl = {"run": APPROVE_RUN, "tool": "stb schema", "action": "更新字段表锁 control_center/schema/fields.lock（字段表改了）", "key": fp[:64], "detail": diff_text()}
    r = await hub_client.call("card", kind=acquire_permission.PERMISSION_KIND, title="", body="", buttons=[], payload=pl)
    card_id = (r or {}).get("id")
    if not card_id:
        return f"审批卡没发出去：{(r or {}).get('error') or 'stbcc-hub 没有响应'}"
    deadline = time.time() + APPROVE_WAIT_S
    while time.time() < deadline:
        got = (cards.load(card_id) or {}).get("resolved")
        if got:
            if got.get("action") != "approve":
                return "Joy 拒绝了，fields.lock 没改"
            if fingerprint() != fp:
                return "批准期间字段表又改了，fields.lock 没改：重新 stb schema approve"
            acquire_permission.take_approved(acquire_permission.PERMISSION_KIND, APPROVE_RUN, card_id=card_id)   # 用掉这张卡
            write_lock()
            return "Joy 同意了，已更新 control_center/schema/fields.lock"
        await __import__("asyncio").sleep(3)
    return "Joy 还没处理审批卡，fields.lock 没改（卡还在：他同意后再 stb schema approve 就直接更新）"
