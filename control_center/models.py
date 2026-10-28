"""模型档位（规格第 8 节、D7）。文件 ~/sparky-toolbox/config/models.yaml（进 git；缺失直接报错）。

endpoint 是宿主机视角的地址（网关跑 agent 子代理时直接用）；沙箱内的地址由网关的模型转发口决定（gateway.py）。
档位分两种，只看写了哪些字段，不看推理底座是什么：没有 endpoint 的是独占档位（没有服务）；有 endpoint 的是服务档位。
服务档位在不在跑问 endpoint 的 /models（OpenAI 兼容）：答了就看有没有 root（条目没有 root 时看 id）等于 model_path 的条目，有就在跑，调用名（请求里的 model）取那一条的 id（call_name）；
不答 /models、但 endpoint 本身回 HTTP 200 也算在跑（宿主程序，如 ComfyUI）。
服务档位没写 start 也没写 stop 的是常驻接口（resident：Ollama、云端）：平台不起不停，换到它只看它在不在跑，从它换走不停它、不等它。沙箱里 pi 的 models.json 由平台按调用名生成（provider 固定 PI_PROVIDER）。
服务档位也用来给 workflow 任务的步骤起它要连的服务（步骤以 sparky 跑、不能调 docker）：步骤的第 4 项 runtime / model 指向档位，运行器以平台身份按 start 起好、再跑这一步。
"""
from __future__ import annotations

import subprocess
from typing import Any

import yaml

from control_center import scheme_check
from control_center.paths import MODELS_FILE

# 档位字段（来自字段表 control_center/schema/fields.yaml；load_models 按它校验，写错的键直接报错；stb schema models 按它生成手册的字段表）：字段 → (类型, 默认, 说明)
PROFILE_FIELDS: dict[str, tuple[str, str, str]] = {k: (v["type"], v["default"], v["desc"]) for k, v in scheme_check.table("models").items()}
START_TIMEOUT_S = 600            # 起命令之后等档位就绪的上限（秒），所有档位一样；加载中认不出是哪个档位时等它能答 /models 也用它
SETTINGS_FIELDS: dict[str, tuple[str, str, str]] = {k: (v["type"], v["default"], v["desc"]) for k, v in scheme_check.table("models._settings").items()}


def exclusive(p: dict[str, Any]) -> bool:
    """独占档位：没有 endpoint（没有服务，如训练），只让机器空出来；只按 runtime 匹配。"""
    return not p.get("endpoint")


def resident(p: dict[str, Any]) -> bool:
    """常驻接口：服务档位没写 start 也没写 stop（如 Ollama、云端）。平台不起不停：换到它只看 /models 有没有这个模型，从它换走不停它、不等它消失。"""
    return not exclusive(p) and not p.get("start") and not p.get("stop")


def required_fields(p: dict[str, Any]) -> tuple[str, ...]:
    """档位的必填字段：runtime 都要；独占档位（没有 endpoint）只要它；服务档位再要 model_path、endpoint。
    任务拿它当主模型时还要 context_window（main_missing，stb task check 查）。"""
    if exclusive(p):
        return ("runtime",)
    return ("runtime", "model_path", "endpoint")


MAIN_FIELDS = ("context_window",)     # agent 任务拿档位当主模型时要有的字段（pi 的上下文长度）


def main_missing(p: dict[str, Any]) -> list[str]:
    """档位当 agent 任务的主模型还缺哪些：独占档位不能当主模型（返回 endpoint），其余看 MAIN_FIELDS。"""
    if exclusive(p):
        return ["endpoint"]
    return [k for k in MAIN_FIELDS if p.get(k) in (None, "")]


def load_models() -> dict[str, dict[str, Any]]:
    """返回档位表；保留键 `_settings` 也在里面（调用方按需读取）。文件缺失、字段名写错、缺必填字段（required_fields）都直接报错（不自动写默认内容）。"""
    if not MODELS_FILE.exists():
        raise ValueError(f"{MODELS_FILE} 不存在：模型档位表在仓库 config/models.yaml（进 git），从 git 恢复；字段见手册 §5 或 stb schema models")
    return check_models(yaml.safe_load(MODELS_FILE.read_text()) or {})


def check_models(data: Any) -> dict[str, dict[str, Any]]:
    """档位表的校验（load_models 与 builder 计划里的新档位共用）：字段名、必填字段、context_window 的取值、runtime + model_path 不重复、容器名 + model_path 不重复。"""
    if not isinstance(data, dict):
        raise ValueError(f"{MODELS_FILE} 顶层必须是映射")
    if bad := sorted(set(data.get("_settings") or {}) - set(SETTINGS_FIELDS)):
        raise ValueError(f"{MODELS_FILE}：_settings 里的 {bad} 不认识（可用：{', '.join(SETTINGS_FIELDS)}）")
    seen: dict[tuple[str, str], str] = {}
    pairs: dict[tuple[str, str], str] = {}
    for name, p in profiles(data).items():
        if bad := sorted(set(p) - set(PROFILE_FIELDS)):
            raise ValueError(f"{MODELS_FILE}：档位 {name} 的 {bad} 是未知字段（可用：{', '.join(PROFILE_FIELDS)}；字段表见手册 How-to-set-a-task-on-Sparky.md，或 stb schema models）")
        if missing := [k for k in required_fields(p) if p.get(k) in (None, "")]:
            kind = "独占档位" if exclusive(p) else "服务档位"
            raise ValueError(f"{MODELS_FILE}：档位 {name} 缺必填字段 {'、'.join(missing)}（{kind}要写 {'、'.join(required_fields(p))}；字段见 stb schema models）")
        if "context_window" in p and (isinstance(p["context_window"], bool) or not isinstance(p["context_window"], int) or p["context_window"] <= 0):
            raise ValueError(f"{MODELS_FILE}：档位 {name} 的 context_window 要写正整数（模型的上下文长度，token），现在是 {p['context_window']!r}")
        if p.get("runtime"):
            pair = (str(p["runtime"]), "*" if exclusive(p) else str(p.get("model_path") or ""))
            if pair in pairs:
                raise ValueError(f"{MODELS_FILE}：档位 {pairs[pair]} 与 {name} 的 runtime + model_path 相同（{pair[0]} + {pair[1]}），任务分不清指的是哪个")
            pairs[pair] = name
        if p.get("container"):
            key = (str(p["container"]), str(p.get("model_path") or ""))
            if key in seen:
                raise ValueError(f"{MODELS_FILE}：档位 {seen[key]} 与 {name} 容器名都是 {key[0]}，model_path 必须不同（否则分不清在跑的是哪个）")
            seen[key] = name
    return data


def profile_for(models: dict[str, Any], runtime: str, model: str) -> str | None:
    """任务写的 (runtime, model) 对应的档位名：档位的 runtime 相同、model_path 等于 model；独占档位（没有 endpoint）只看 runtime，model 只作记录。"""
    for name, p in profiles(models).items():
        if str(p.get("runtime") or "") == runtime and (bool(model) if exclusive(p) else str(p.get("model_path") or "") == model):
            return name
    return None


def profiles(models: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in models.items() if not k.startswith("_") and isinstance(v, dict)}


def _container_running(name: str) -> bool:
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() == "true"


def api_key(profile: dict[str, Any]) -> str:
    """请求模型时带的密钥：档位写了 api_key（.env 里的键名）取那个键的值（credential，缺了抛 MissingCredential），没写是 "local"。"""
    from control_center.credential import credential

    return credential(str(profile["api_key"])) if profile.get("api_key") else "local"


def _models(profile: dict[str, Any], timeout: float) -> list[dict[str, Any]] | None:
    """端点 /models（OpenAI 兼容，{"data": [...]}）列出的模型；端点不答、或答的不是这种格式（ComfyUI 这类宿主程序）返回 None。
    档位写了 api_key 才带 Authorization: Bearer <值>（取不到值也是 None）。"""
    import httpx

    try:
        auth: dict[str, Any] = {"headers": {"Authorization": f"Bearer {api_key(profile)}"}} if profile.get("api_key") else {}
        r = httpx.get(str(profile["endpoint"]).rstrip("/") + "/models", timeout=timeout, **auth)
        data = r.json().get("data") if r.status_code == 200 else None
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def _entry(profile: dict[str, Any], listed: list[dict[str, Any]]) -> dict[str, Any] | None:
    """端点列出的模型里属于这个档位的那一条：root 等于 model_path；条目没有 root（不是 vLLM 的 OpenAI 兼容服务）时比 id。"""
    path = str(profile.get("model_path") or "")
    return next((m for m in listed if path and str(m.get("root") or m.get("id") or "") == path), None)


def call_name(profile: dict[str, Any], timeout: float = 5) -> str | None:
    """调用这个档位的模型时请求里写的 model：端点 /models 里 root 等于 model_path 的那一条的 id；端点不答 /models 或没有这一条返回 None。"""
    listed = _models(profile, timeout) if not exclusive(profile) else None
    entry = _entry(profile, listed) if listed is not None else None
    return str(entry["id"]) if entry and entry.get("id") else None


def _endpoint_ok(url: str, timeout: float) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def state(profile: dict[str, Any], timeout: float = 5) -> str:
    """档位此刻的状态：up（在跑、就绪）/ loading（登记了容器、容器在跑、端点还不答 /models：加载中，认不出是哪个档位）/ down。
    独占档位没有服务，总是 down（就绪与否由运行器看内存）；登记了 container 的要容器在跑；
    端点答 /models：有 root 等于 model_path 的条目才是 up；不答 /models：登记了容器算加载中，没登记容器的看 endpoint 回不回 HTTP 200（宿主程序，如 ComfyUI）。"""
    if exclusive(profile):
        return "down"
    if profile.get("container") and not _container_running(str(profile["container"])):
        return "down"
    listed = _models(profile, timeout)
    if listed is not None:
        return "up" if _entry(profile, listed) else "down"
    if profile.get("container"):
        return "loading"
    return "up" if _endpoint_ok(str(profile["endpoint"]), timeout) else "down"


def is_up(profile: dict[str, Any], timeout: float = 5) -> bool:
    """档位在跑、就绪（state 是 up）。"""
    return state(profile, timeout) == "up"


def running(models: dict[str, Any]) -> list[str]:
    """正在运行的已登记档位名：就绪的（up），加上加载中的（loading：已经占着内存；同一个容器的档位认不出是谁，都算）。
    容器在跑、端点能答、但模型不是任何登记档位的不算（当未登记容器：不停，内存闸兜底）。"""
    return [n for n, p in profiles(models).items() if state(p, timeout=2) != "down"]


def loading(models: dict[str, Any]) -> list[str]:
    """加载中、还认不出是哪个档位的容器名（state 是 loading 的档位的 container，去重）。"""
    return sorted({str(p["container"]) for p in profiles(models).values() if state(p, timeout=2) == "loading"})


PI_PROVIDER = "stb"          # 沙箱里 pi 的 models.json 的 provider 名（平台固定；模型 id 是档位的调用名，pi 的 --model 写 PI_PROVIDER/调用名）


def pi_models_json(profile: dict[str, Any], base_url: str, model_id: str, effort: str | None = None) -> dict[str, Any]:
    """生成沙箱里 pi 用的 ~/.pi/agent/models.json（一个 provider、一个模型，模型 id = 档位的调用名 call_name）。
    effort（任务 / 子代理的推理档位）：写了才打开 supportsReasoningEffort，并把这一级登记进 thinkingLevelMap（pi 只发登记过的 xhigh），
    配合 pi 的 --thinking，请求里带 reasoning_effort；没写就关着：pi 不发档位（它默认的 medium 也不发），模板用自己的默认。"""
    model: dict[str, Any] = {"id": model_id, "name": model_id, "reasoning": True,
                             "input": ["text", "image"] if profile.get("vision") else ["text"],
                             "contextWindow": int(profile["context_window"]),
                             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    if effort:
        model["thinkingLevelMap"] = {effort: effort}
    return {"providers": {PI_PROVIDER: {
        "baseUrl": base_url, "api": "openai-completions", "apiKey": api_key(profile),
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": bool(effort)},
        "models": [model]}}}
