"""卡片：要 Joy 点头或回答的事以带按钮的卡片发给他（各渠道显示，点击交中枢 stbcc-hub 的 card_act）。本模块只管卡片本身：

- 存卡：~/sparky-toolbox/data/hub/approvals/<id>.json；按钮 custom_id 不签名（"stb:<卡id>:<动作>"），服务端只认 owner 用户 ID 的点击。
- 按 kind 处理：处理器按 kind 登记（register）；点击按钮时由中枢（card_act）调 resolve → 处理器，处理器返回给 Joy 看的一句结果；处理完再调 ON_RESOLVED 的钩子。
  重复点击幂等：已处理的卡只把结果再显示一次，不重复执行。
- 密码只放内存：弹框里标了 secret 的字段的值只放进中枢进程的内存，等取一次（put_secret / take_secret）。
- 状态表情：卡片第一行带 ⏳ 等待中 / ✅ 已同意（已提交）/ ❌ 已拒绝 / ⚪ 作废或过期（status_emoji）。
- 过期：卡一律不过期（ttl_seconds 默认 None）；旧卡里写了 expires 的照旧到期作废（expired）。
运行中等卡（网关就地等、等不到让出、续跑时用掉已同意的卡）在 control_center/acquire_permission.py。
卡的结构：{id, kind, title, body, buttons: [{id, label, style, modal?}], select: {placeholder, options: [{value, label}], value} | None,
           modal: {title?, fields: [{id, label, default?, long?, secret?}]} | None, payload, file（附图 / 全文附件，hub.card_file）, created, expires,
           message: {channel_id, id} | None, resolved: {action, value, result, at} | None}
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from control_center import paths
from control_center.paths import APPROVALS

DEFAULT_TTL = None                # 卡不过期
Handler = Callable[[dict[str, Any], str, Any], str]
Review = Callable[[dict[str, Any]], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}
REVIEWS: dict[str, Review] = {}     # kind → 按 payload 生成整张卡（发 card 的参数）：调用方给的标题正文不用，payload 不合格抛 ValueError、不出卡
SPARKY_KINDS: set[str] = set()      # 以 sparky 跑的工具能经 hub.sock 发的卡（都要有 review：卡面由中枢生成）
ON_RESOLVED: list[Callable[[dict[str, Any], str, Any, str], None]] = []   # 卡处理完之后的钩子（card, action, value, result）


def register(kind: str, fn: Handler, *, review: Review | None = None, sparky: bool = False) -> None:
    """登记一种卡（中枢按登记表分发，不写死卡种类）：fn(card, action, value) 处理 Joy 的决定，返回给模型 / 运行的结果文字；
    review(payload) 写了 = 卡面由中枢按 payload 生成；sparky=True = 以 sparky 跑的工具可以发这种卡（必须有 review）。
    卡都由平台核心在各自模块里登记（工具不自带卡片处理器：以 sparky 跑的工具要 Joy 同意的事发 acquire_permission 的通用审批卡）。"""
    if sparky and review is None:
        raise ValueError(f"卡 {kind}：sparky 能发的卡要有 review（卡面由中枢按 payload 生成）")
    HANDLERS[kind] = fn
    if review is not None:
        REVIEWS[kind] = review
    if sparky:
        SPARKY_KINDS.add(kind)


def attach_dir(kind: str) -> Path:
    """卡片附件（内容太长时的全文）放这里：data/hub/cards/<kind>/（中枢只认这几处的附件，见 hub.card_file）。调用时才算。"""
    return paths.HUB / "cards" / kind


ENV_KIND = "env-credential"         # 向 Joy 要 .env 凭证的卡（builder 要凭证、网关缺凭证时发）：填的值由处理器直接写进 .env（control_center/builder）
SECRET_TTL_S = 900                  # 填了没人取的值（工具已经放弃等卡）最多留多久
_SECRETS: dict[str, tuple[str, float]] = {}   # 卡 id → (值, 填的时刻)：只在中枢 stbcc-hub 进程的内存里


def secret_fields(card: dict[str, Any]) -> list[str]:
    """卡的弹框里标了 secret 的字段（密码、凭证；id 是 password 的也算，部署前发的旧卡没标）：Joy 填的值只放中枢内存等取一次（put_secret），
    卡片记录、处理结果、日志、磁盘上都不留。"""
    return [str(f.get("id")) for f in ((card.get("modal") or {}).get("fields") or []) if isinstance(f, dict) and (f.get("secret") or f.get("id") == "password")]


def put_secret(card_id: str, value: str) -> None:
    """带 secret 字段的卡的值（Joy 在卡上填的）只放进本进程（stbcc-hub）内存，等取一次：.env 凭证卡由同进程的处理器取（builder._handle_env_card），
    通用审批卡的密码框由发卡的运行经中枢的 secret op 取（hub）。顺手丢掉放了超过 SECRET_TTL_S 没人取的（中枢每分钟也清一次：purge_secrets）。"""
    _path(card_id)                                  # 校验卡 id
    purge_secrets()
    _SECRETS[card_id] = (value, time.time())


def purge_secrets() -> int:
    """丢掉放了超过 SECRET_TTL_S 没人取的值（stbcc-hub 每分钟调一次，put_secret 也顺手调）。返回丢掉几个。"""
    now = time.time()
    old = [k for k, (_v, t) in _SECRETS.items() if now - t > SECRET_TTL_S]
    for k in old:
        del _SECRETS[k]
    return len(old)


def take_secret(card_id: str) -> str | None:
    """取走卡上填的值（只能取一次）；没有、已取过、过期了返回 None。"""
    v, t = _SECRETS.pop(card_id, (None, 0.0))
    return v if v is not None and time.time() - t <= SECRET_TTL_S else None


def status_emoji(card: dict[str, Any]) -> str:
    r = card.get("resolved")
    if not r:
        return "⚪" if expired(card) else "⏳"
    return "✅" if r.get("action") in ("approve", "submit", "continue") else "❌" if r.get("action") in ("reject", "decline") else "⚪"


def _path(card_id: str):
    if not card_id.isalnum():
        raise ValueError("非法的卡 id")
    return APPROVALS / f"{card_id}.json"


def create(kind: str, title: str, body: str, buttons: list[dict[str, Any]], *, ttl_seconds: int | None = DEFAULT_TTL,
           select: dict[str, Any] | None = None, modal: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    APPROVALS.mkdir(parents=True, exist_ok=True)
    now = time.time()
    card = {"id": secrets.token_hex(6), "kind": kind, "title": title, "body": body, "buttons": buttons, "select": select, "modal": modal,
            "payload": payload or {}, "created": now, "expires": now + ttl_seconds if ttl_seconds else None, "message": None, "resolved": None}
    save(card)
    return card


def save(card: dict[str, Any]) -> None:
    _path(card["id"]).write_text(json.dumps(card, ensure_ascii=False, indent=1))


def load(card_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_path(card_id).read_text())
    except (OSError, ValueError):
        return None


def pending() -> list[dict[str, Any]]:
    out = []
    for p in sorted(APPROVALS.glob("*.json")):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not c.get("resolved"):
            out.append(c)
    return out


def expired(card: dict[str, Any], now: float | None = None) -> bool:
    return bool(card.get("expires")) and float(card["expires"]) < (now or time.time())


def resolve(card: dict[str, Any], action: str, value: Any = None) -> str:
    """执行一次决定；已处理的卡直接返回上次结果（幂等）。返回给 Joy 看的结果文字。"""
    if card.get("resolved"):
        return str(card["resolved"].get("result", ""))
    if action == "expired":
        result = "已过期，未执行"
    else:
        fn = HANDLERS.get(card["kind"])
        if fn is None:
            result = f"没有 {card['kind']} 类卡的处理器"
        else:
            try:
                result = fn(card, action, value)
            except Exception as e:  # noqa: BLE001 — 处理器出错也要把卡收掉，不让它一直挂着
                result = f"处理失败：{type(e).__name__}: {str(e)[:200]}"
    card["resolved"] = {"action": action, "value": value, "result": result, "at": time.time()}
    save(card)
    for fn in ON_RESOLVED:
        try:
            fn(card, action, value, result)
        except Exception as e:  # noqa: BLE001 — 钩子出错不影响卡已处理
            print(f"卡 {card['id']} 的钩子出错：{type(e).__name__}: {e}", flush=True)
    return result
