"""算力 API：用本机一个指定的模型档位，像 OpenAI / Anthropic 的 API key 一样从别的设备调用（手册「算力 API」一节）。

- 一个 API = 一个任务 api-<名>（控制台「API」页建，create）+ 一个端口 + 一把密钥：
  任务 trigger: listen、listen: api（平台自带的监听：没有 listen.py，只由这里的门口触发）、workflow 只有一步 [1, hold.py, true, {runtime, model}]、
  priority 建时填；hold.py 只调 control_center.api_session.hold（会话）。端口、对外方式、密钥的 sha256 存 data/hub/api/<名>.json，不进 task.yaml。
- 门口（Door，在 stbcc-hub 里跑）：每 RECONCILE_S 秒按 data/hub/api/*.json 开关端口。本地 = 绑本机的 Tailscale 地址；
  公网 = 另绑 127.0.0.1，由 Tailscale Funnel 的路径 /<名> 转进来（建 / 删时 funnel 命令挂上 / 撤掉）。每条请求：
  1. 认密钥：Authorization: Bearer、x-api-key，或地址路径 /k/<密钥>/（只能填一个地址的客户端用，如酒馆的 ComfyUI）；同一来源连续 LOCK_FAILS 次错 → LOCK_S 秒 429。
  2. GET /v1/models 直接按配置答（只列这个 API 的模型，id = 档位的 model_path），不起会话。
  3. 会话在跑（runner 现在跑的是这个任务、hold 写了就绪）就转发；不在跑就让任务入队（照常排队、抢占、切模型），最多等 WAIT_S 秒，
     还没起来回 503 + Retry-After，错误文字写明在等什么；上次会话失败不到 FAIL_PAUSE_S 秒的不再入队，回失败原因。
  4. 档位的端点答 /models（OpenAI 兼容）：只放 OPENAI_POST 两个接口；请求里的 model 要等于 model_path（换成调用名再转），流式照转，从 usage 记 token。
     不答 /models 的（ComfyUI 这类宿主程序）：按 COMFY_GET / COMFY_POST 原样转（含 /ws 的 websocket）。别的路径 404。
  5. 记账：data/hub/api/usage/<日期>.jsonl 每条一行（不记内容；ComfyUI 的 GET 轮询不记）；最后一次请求的时刻与在答的路数写进 live/<名>.json 给会话看。
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from control_center import runner
from control_center.api_session import IDLE_S, SESSION_FILE, api_dir, live_file, read_json, usage_file, usage_rows, write_json
from control_center.models import _entry, _models, exclusive, load_models, profiles
from control_center import paths
from control_center.paths import CONTROL, RUNNER_STATE, RUNS, STEPS, TASKS, task_file
from control_center.task import TaskError, load_task

PREFIX = "api-"                          # API 的任务名都以它开头
LISTEN_NAME = "api"                      # 平台自带的监听名（task.BUILTIN_LISTEN）
PORTS = range(18200, 18300)              # 分给 API 的端口
WAIT_S = 30                              # 会话没在跑：请求最多等它起来多久，再回 503
RETRY_AFTER_S = 15
FAIL_PAUSE_S = 300                       # 上次会话失败（比如模型加载不了）后多久内不再自动入队
LOCK_FAILS, LOCK_S = 5, 600              # 同一来源连续几次密钥错就封多久
BODY_MAX = 20 * 2 ** 20                  # 请求体上限（带图的对话够用）
READ_TIMEOUT_S = 600                     # 等模型服务回一段数据的上限
RECONCILE_S = 2
OPENAI_POST = ("/v1/chat/completions", "/v1/completions")
COMFY_GET = re.compile(r"/(?:api/)?(?:system_stats|object_info(?:/[\w.-]+)?|history(?:/[\w.-]+)?|view|queue|prompt|embeddings|ws)")
COMFY_POST = re.compile(r"/(?:api/)?(?:prompt|interrupt|upload/image|upload/mask|queue|history)")
HOP = {"host", "authorization", "x-api-key", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authorization", "te", "trailer", "accept-encoding"}
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,35}")
API_KEY = web.AppKey("api", str)        # 端口的 app 上记着它是哪个 API


class ApiError(Exception):
    pass


# ---------- 配置与状态（门口、控制台共用）----------
def config_file(name: str) -> Path:
    return api_dir() / f"{name}.json"


def configs() -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted(api_dir().glob("*.json")) if api_dir().is_dir() else []:
        c = read_json(f)
        if c.get("name") == f.stem and isinstance(c.get("port"), int):
            out[f.stem] = c
    return out


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "sk-spk-" + secrets.token_urlsafe(32)


def tier_of(task: Any) -> tuple[str, dict[str, Any]]:
    """API 任务绑的档位：(档位名, 档位)。"""
    models = load_models()
    name = str(task.step_profile(models, 1))
    return name, profiles(models)[name]


def current() -> dict[str, Any]:
    try:
        return json.loads(RUNNER_STATE.read_text()).get("current") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def session(name: str) -> dict[str, Any] | None:
    """这个 API 的会话在跑、就绪：runner 现在跑的是它，hold 写了 ready。返回 {run_id, …}，否则 None。"""
    cur = current()
    if cur.get("task") != name or not cur.get("run_id"):
        return None
    s = read_json(RUNS / name / str(cur["run_id"]) / runner.STEPS_DIR / SESSION_FILE)
    return {"run_id": cur["run_id"], **s} if s.get("ready") else None


def last_run(name: str) -> dict[str, Any]:
    for f in sorted((RUNS / name).glob("*/run.json"), reverse=True):
        if info := read_json(f):
            return {"id": f.parent.name, **info}
    return {}


def _ended(info: dict[str, Any]) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(info.get("ended"))).timestamp()
    except ValueError:
        return 0.0


def waiting_text(name: str) -> str:
    """会话没起来时告诉客户端在等什么。"""
    cur = current()
    if cur.get("task") == name:
        return "正在加载这个 API 的模型（换模型约 4–5 分钟）"
    rows = sorted((runner._queue_entries()), key=lambda e: (-int(e[1].get("priority", 5)), float(e[1].get("enqueued_at", 0))))
    names = [e.get("task") for _, e in rows]
    ahead = names[:names.index(name)] if name in names else names
    now = f"现在在跑 {cur['task']}（优先级 {cur.get('priority')}）" if cur.get("task") else "Sparky 正要开始"
    return f"排队中：{now}" + (f"，前面还有 {len(ahead)} 个（{'、'.join(map(str, ahead[:3]))}{'…' if len(ahead) > 3 else ''}）" if ahead else "")


def pending(name: str) -> bool:
    """任务在排队或在跑（runner.task_pending，加上刚触发、还在中枢 steps/ 里等开跑的新运行）。"""
    return runner.task_pending(name) or any(read_json(p).get("task") == name for p in STEPS.glob("*.json"))


def trigger(name: str) -> str | None:
    """会话没在跑：让任务入队（已在队列 / 在跑就不重复）。不入队、要直接回给客户端的原因返回出来。"""
    if pending(name):
        return None
    last = last_run(name)
    if last.get("status") == "failed" and time.time() - _ended(last) < FAIL_PAUSE_S:
        return f"上次会话失败（{str(last.get('error') or '')[:600]}），{FAIL_PAUSE_S // 60} 分钟内不再自动重试"
    if last.get("status") == "paused" and last.get("pause_reason") == "manual":
        return f"这个 API 的会话被手动暂停了：stb resume {name} 恢复"
    runner.enqueue(name, trigger="listen", prompt="", source={"event": LISTEN_NAME})
    return None


def record(row: dict[str, Any]) -> None:
    f = usage_file(time.strftime("%Y-%m-%d"))
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as out:
        out.write(json.dumps({"ts": round(time.time(), 3), **row}, ensure_ascii=False) + "\n")


def _usage(u: Any) -> dict[str, int]:
    if not isinstance(u, dict):
        return {}
    return {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0),
            "cached": int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)}


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers, dumps=lambda o: json.dumps(o, ensure_ascii=False))   # 中文原样给（curl 直接能读）


def _err(status: int, msg: str, kind: str, headers: dict[str, str] | None = None) -> web.Response:
    return _json({"error": {"message": msg, "type": kind, "code": status}}, status, headers)


# ---------- 门口 ----------
class Door:
    def __init__(self, bind_ip: str) -> None:
        self.bind_ip = bind_ip                     # 本地：本机的 Tailscale 地址
        self.sites: dict[str, tuple[tuple[Any, ...], web.AppRunner]] = {}   # API 名 → ((端口, 绑的地址…), 在听的 runner)
        self.apis: dict[str, dict[str, Any]] = {}  # API 名 → {cfg, task, tier, profile, error}
        self.fails: dict[tuple[str, str], tuple[int, float]] = {}
        self.inflight: dict[str, int] = {}
        self.written: dict[str, float] = {}
        self.kinds: dict[str, tuple[str, str | None]] = {}   # 运行 id → ("openai", 调用名) / ("native", None)
        self.client: ClientSession | None = None

    async def run(self) -> None:
        self.client = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10, sock_read=READ_TIMEOUT_S), auto_decompress=False)
        while True:
            try:
                await self.reconcile()
            except Exception as e:  # noqa: BLE001
                print(f"API 门口出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            await asyncio.sleep(RECONCILE_S)

    async def reconcile(self) -> None:
        """按 data/hub/api/*.json 开关端口，重读各 API 的任务与档位。"""
        cfgs = await asyncio.to_thread(configs)
        for name in [n for n in self.sites if n not in cfgs]:
            await self._close(name)
            self.apis.pop(name, None)
        for name, cfg in cfgs.items():
            self.apis[name] = await asyncio.to_thread(self._load, cfg)
            want = (cfg["port"], *dict.fromkeys([self.bind_ip, *(["127.0.0.1"] if cfg.get("public") else [])]))   # 没有 Tailscale 时本地也是 127.0.0.1：只绑一次
            if name in self.sites and self.sites[name][0] == want:
                continue
            await self._close(name)
            app = web.Application(client_max_size=BODY_MAX)
            app[API_KEY] = name
            app.router.add_route("*", "/{tail:.*}", self.handle)
            r = web.AppRunner(app, access_log=None)
            await r.setup()
            try:
                for host in want[1:]:
                    await web.TCPSite(r, str(host), int(cfg["port"])).start()
            except OSError as e:
                await r.cleanup()
                print(f"API {name}：端口 {cfg['port']} 开不了：{e}", flush=True)
                continue
            self.sites[name] = (want, r)
            print(f"API {name}：在听 {'、'.join(f'{h}:{want[0]}' for h in want[1:])}", flush=True)

    @staticmethod
    def _load(cfg: dict[str, Any]) -> dict[str, Any]:
        try:
            task = load_task(task_file(cfg["name"]))
            tier, prof = tier_of(task)
            return {"cfg": cfg, "task": task, "tier": tier, "profile": prof, "error": None}
        except Exception as e:  # noqa: BLE001 — 任务定义、档位表读不了：请求回原因
            return {"cfg": cfg, "task": None, "tier": None, "profile": None, "error": f"{type(e).__name__}: {str(e)[:500]}"}

    async def _close(self, name: str) -> None:
        if got := self.sites.pop(name, None):
            await got[1].cleanup()

    async def close(self) -> None:
        for name in list(self.sites):
            await self._close(name)
        if self.client:
            await self.client.close()

    # ---- 一条请求 ----
    async def handle(self, req: web.Request) -> web.StreamResponse:
        name = req.app[API_KEY]
        a = self.apis.get(name)
        if a is None:
            return _err(404, "没有这个 API", "not_found")
        src = (req.headers.get("X-Forwarded-For", "").split(",")[0].strip() or req.remote or "?") if req.remote in ("127.0.0.1", "::1") else (req.remote or "?")
        n, until = self.fails.get((name, src), (0, 0.0))
        if until > time.time():
            return _err(429, f"密钥连续错了 {LOCK_FAILS} 次，{int(until - time.time())} 秒后再试", "rate_limited")
        key, path = self._key(req)
        if not key or not hmac.compare_digest(key_hash(key), str(a["cfg"].get("key_sha256") or "")):
            n += 1
            self.fails[(name, src)] = (0, time.time() + LOCK_S) if n >= LOCK_FAILS else (n, 0.0)
            return _err(401, "密钥不对", "invalid_api_key")
        self.fails.pop((name, src), None)
        if a["error"]:
            return _err(503, f"这个 API 的任务读不了：{a['error']}", "unavailable")
        if req.method == "GET" and path == "/v1/models":
            return _json({"object": "list", "data": [{"id": a["profile"]["model_path"], "object": "model",
                                                                  "created": int(a["cfg"].get("created") or 0), "owned_by": "sparky"}]})
        t0 = time.monotonic()
        data = None
        if req.method == "POST" and path in OPENAI_POST:     # 模型名不对直接回，不起会话
            try:
                data = json.loads(await req.read())
                assert isinstance(data, dict)
            except (ValueError, AssertionError):
                return _err(400, "请求体要是 JSON 对象", "invalid_request_error")
            if data.get("model") not in (None, "", want := a["profile"]["model_path"]):
                self._log(name, req, path, 400, t0)
                return _err(400, f"这个 API 只能用 {want}（请求写的是 {data.get('model')}）", "model_not_found")
        if not a["task"].enabled:
            return _err(503, f"{name} 已停用（控制台的任务页启用后再用）", "unavailable")
        self._touch(name)
        sess = await self._wait_session(name)
        if isinstance(sess, web.Response):
            self._log(name, req, path, sess.status, t0)
            return sess
        kind = await self._kind(sess["run_id"], a["profile"])
        if kind[0] == "missing":
            resp = _err(503, f"本机现在跑的不是 {a['profile']['model_path']}（档位 {a['tier']}）", "model_not_loaded")
            self._log(name, req, path, 503, t0)
            return resp
        self._inc(name, 1)
        try:
            if session(name) is None:                # 刚好在收尾：不转了，让客户端重试
                resp = _err(503, "会话正在结束，稍后重试", "unavailable", {"Retry-After": str(RETRY_AFTER_S)})
                self._log(name, req, path, 503, t0)
                return resp
            if kind[0] == "openai":
                return await self._openai(req, name, a, path, kind[1], t0, data)
            return await self._native(req, name, a, path, t0)
        finally:
            self._inc(name, -1)

    @staticmethod
    def _key(req: web.Request) -> tuple[str, str]:
        """(密钥, 去掉 /k/<密钥> 之后的路径)。"""
        m = re.fullmatch(r"/k/([^/]+)(/.*)?", req.path)
        if m:
            return m.group(1), m.group(2) or "/"
        auth = req.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else req.headers.get("x-api-key", "").strip()
        return key, req.path

    async def _wait_session(self, name: str) -> dict[str, Any] | web.Response:
        deadline = time.monotonic() + WAIT_S
        while True:
            if (s := session(name)) is not None:
                return s
            try:
                why = await asyncio.to_thread(trigger, name)
            except (TaskError, OSError, ValueError) as e:
                why = f"入队失败：{e}"
            if why:
                return _err(503, why, "unavailable")
            if time.monotonic() > deadline:
                return _err(503, await asyncio.to_thread(waiting_text, name) + f"。{RETRY_AFTER_S} 秒后重试", "unavailable",
                            {"Retry-After": str(RETRY_AFTER_S)})
            await asyncio.sleep(1)

    async def _kind(self, run_id: str, prof: dict[str, Any]) -> tuple[str, str | None]:
        """这次会话的接口：端点答 /models 的是 openai（调用名取 root 等于 model_path 的那一条）；答了但没有这一条是 missing；不答的是 native。"""
        if run_id not in self.kinds:
            listed = await asyncio.to_thread(_models, prof, 5)
            entry = _entry(prof, listed) if listed is not None else None
            self.kinds[run_id] = ("native", None) if listed is None else ("openai", str(entry["id"])) if entry else ("missing", None)
        return self.kinds[run_id]

    def _touch(self, name: str) -> None:
        self._live(name, force=False)

    def _inc(self, name: str, d: int) -> None:
        before = self.inflight.get(name, 0)
        self.inflight[name] = max(0, before + d)
        self._live(name, force=(before == 0) != (self.inflight[name] == 0))

    def _live(self, name: str, force: bool) -> None:
        """最后一次请求的时刻、在答的路数写给会话看：在答的从 0 变非 0（或反过来）马上写，别的最多每秒写一次。"""
        now = time.time()
        if force or now - self.written.get(name, 0) >= 1:
            self.written[name] = now
            write_json(live_file(name), {"last_request": now, "inflight": self.inflight.get(name, 0)})

    def _log(self, name: str, req: web.Request, path: str, status: int, t0: float, usage: dict[str, int] | None = None) -> None:
        record({"api": name, "method": req.method, "path": path, "status": status, "ms": int((time.monotonic() - t0) * 1000), **(usage or {})})

    async def _openai(self, req: web.Request, name: str, a: dict[str, Any], path: str, call: str | None, t0: float,
                      data: dict[str, Any] | None) -> web.StreamResponse:
        if data is None:
            self._log(name, req, path, 404, t0)
            return _err(404, f"这个 API 只有 GET /v1/models、POST {'、'.join(OPENAI_POST)}", "not_found")
        data["model"] = call
        stream = bool(data.get("stream"))
        asked = bool((data.get("stream_options") or {}).get("include_usage")) if stream else True
        if stream and not asked:
            data["stream_options"] = {**(data.get("stream_options") or {}), "include_usage": True}
        url = str(a["profile"]["endpoint"]).rstrip("/") + path[len("/v1"):]
        assert self.client is not None
        async with self.client.post(url, json=data) as up:
            if not stream or up.status != 200:
                raw = await up.read()
                try:
                    usage = _usage(json.loads(raw).get("usage"))
                except (ValueError, AttributeError):
                    usage = {}
                self._log(name, req, path, up.status, t0, usage)
                return web.Response(body=raw, status=up.status, content_type=up.content_type)
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
            await resp.prepare(req)
            buf, usage = b"", {}
            try:
                async for chunk in up.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        ev, buf = buf.split(b"\n\n", 1)
                        out, u = _sse_event(ev, asked)
                        usage = u or usage
                        if out is not None:
                            await resp.write(out + b"\n\n")
                if buf:
                    await resp.write(buf)
                await resp.write_eof()
            finally:
                self._log(name, req, path, 200, t0, usage)
            return resp

    async def _native(self, req: web.Request, name: str, a: dict[str, Any], path: str, t0: float) -> web.StreamResponse:
        ok = (COMFY_GET if req.method == "GET" else COMFY_POST if req.method == "POST" else None)
        if ok is None or not ok.fullmatch(path):
            self._log(name, req, path, 404, t0)
            return _err(404, f"这个 API 不转 {req.method} {path}", "not_found")
        base = str(a["profile"]["endpoint"]).rstrip("/")
        assert self.client is not None
        if req.method == "GET" and path.endswith("/ws") and req.headers.get("Upgrade", "").lower() == "websocket":
            return await self._ws(req, base.replace("http", "ws", 1) + path)
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
        async with self.client.request(req.method, base + path, params=req.query, data=await req.read() if req.can_read_body else None,
                                       headers=headers) as up:
            resp = web.StreamResponse(status=up.status, headers={k: v for k, v in up.headers.items() if k.lower() not in HOP})
            await resp.prepare(req)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
        if req.method != "GET":
            self._log(name, req, path, up.status, t0)
        return resp

    async def _ws(self, req: web.Request, url: str) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        assert self.client is not None
        async with self.client.ws_connect(url, params=req.query) as up:
            async def pump(src: Any, dst: Any) -> None:
                async for m in src:
                    if m.type == WSMsgType.TEXT:
                        await dst.send_str(m.data)
                    elif m.type == WSMsgType.BINARY:
                        await dst.send_bytes(m.data)
                    else:
                        break
            tasks = [asyncio.create_task(pump(ws, up)), asyncio.create_task(pump(up, ws))]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        await ws.close()
        return ws


def _sse_event(ev: bytes, asked: bool) -> tuple[bytes | None, dict[str, int]]:
    """一段 SSE：取出 usage；客户端没要 usage（我们替它加的 include_usage）时，只带 usage 的最后一段不转给它。"""
    line = next((l for l in ev.split(b"\n") if l.startswith(b"data:")), b"")
    body = line[5:].strip()
    if not body or body == b"[DONE]":
        return ev, {}
    try:
        d = json.loads(body)
    except ValueError:
        return ev, {}
    u = _usage(d.get("usage")) if isinstance(d, dict) else {}
    if not asked and u and not d.get("choices"):
        return None, u
    return ev, u


# ---------- 控制台：建、换密钥、删、列 ----------
@functools.lru_cache(maxsize=1)
def tailscale_ip() -> str:
    return paths.tailscale_ip()


@functools.lru_cache(maxsize=1)
def funnel_host() -> str:
    """公网地址的主机名（Tailscale 的 MagicDNS 名，如 sparky.xxx.ts.net）。"""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10).stdout
        return str(json.loads(out)["Self"]["DNSName"]).rstrip(".")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return "（Tailscale 没开）"


def funnel(name: str, port: int | None) -> None:
    """公网：Funnel 的 443 挂上路径 /<名> → 127.0.0.1:<端口>（port 为 None 时撤掉）。joyzhang14 是 Tailscale 的 operator，不用 sudo。"""
    cmd = ["tailscale", "funnel", "--bg", "--yes", f"--set-path=/{name}", f"http://127.0.0.1:{port}"] if port else \
          ["tailscale", "funnel", "--yes", "--https=443", f"--set-path=/{name}", "off"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ApiError(f"Funnel {'挂路径' if port else '撤路径'}失败：{(r.stderr or r.stdout).strip()[:500]}")


def urls(cfg: dict[str, Any]) -> dict[str, str]:
    local = f"http://{tailscale_ip()}:{cfg['port']}"
    return {"local": local, **({"public": f"https://{funnel_host()}/{cfg['name']}"} if cfg.get("public") else {})}


def _bindable(port: int) -> bool:
    for host in ("127.0.0.1", tailscale_ip()):
        with socket.socket() as s:
            try:
                s.bind((host, port))
            except OSError:
                return False
    return True


def free_port() -> int:
    used = {c["port"] for c in configs().values()}
    for p in PORTS:
        if p not in used and _bindable(p):
            return p
    raise ApiError(f"端口 {PORTS.start}–{PORTS.stop - 1} 都用完了")


def models_menu() -> list[dict[str, Any]]:
    """建 API 的模型下拉菜单：models.yaml 里有服务的档位（独占档位没有服务，不列）。"""
    return [{"tier": n, "runtime": p["runtime"], "model": p["model_path"], "vision": bool(p.get("vision"))}
            for n, p in profiles(load_models()).items() if not exclusive(p)]


def _git(*a: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(TASKS), *a], capture_output=True, text=True)


def _commit(name: str, msg: str) -> None:
    """仓库 main 上提交这个任务文件夹的增删（main 不留未提交改动，builder 部署才合得进）；不在 git 仓库、不在 main 不提交。"""
    if not TASKS.exists() or _git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
        return
    _git("add", "-A", "--", name)
    if _git("status", "--porcelain", "--", name).stdout.strip():
        _git("-c", "user.name=stb-web", "-c", "user.email=web@sparky", "commit", "-q", "-m", msg, "--", name)


TASK_YAML = """\
# 算力 API：{model}（档位 {tier}）。控制台的「API」页建的：端口、对外方式与密钥在 data/hub/api/{name}.json；
# 改优先级在任务页，换模型或对外方式在「API」页删了重建（手册「算力 API」，control_center/api_door.py）
name: {name}
description: 算力 API：会话占着机器、把请求转给本机的 {model}，{idle} 分钟没有请求就结束
enabled: true
trigger: listen
listen: {listen}
priority: {priority}
timeout: none
steps:
  - [1, hold.py, true, {{runtime: {runtime_q}, model: {model_q}}}]
"""
HOLD_PY = """\
# 算力 API 的会话：占着机器，门口把请求转给模型；{idle} 分钟没有请求就结束（control_center/api_session.py）
from control_center.api_session import hold

hold()
"""


def create(suffix: str, tier: str, priority: Any, public: bool) -> dict[str, Any]:
    """建一个 API：任务 api-<suffix>（写文件夹、校验、登记、提交进仓库）、分端口、生成密钥（只回这一次）、公网的挂 Funnel 路径。"""
    from control_center.runner import mark_registered

    name = PREFIX + str(suffix or "").strip()
    if not NAME_RE.fullmatch(name[len(PREFIX):]) or len(name) > 40:
        raise ApiError("名称只能用小写字母、数字和 -，以字母或数字开头（api- 后面最多 36 个字）")
    if task_file(name).exists() or config_file(name).exists():
        raise ApiError(f"已经有 {name} 了")
    prof = profiles(load_models()).get(str(tier))
    if prof is None or exclusive(prof):
        raise ApiError(f"本机没有模型档位 {tier}（models.yaml 里有服务的档位才能选）")
    try:
        prio = int(str(priority))
        assert 1 <= prio <= 9
    except (ValueError, AssertionError):
        raise ApiError("优先级要写 1–9 的整数") from None
    port = free_port()
    d = task_file(name).parent
    d.mkdir(parents=True)
    try:
        (d / "task.yaml").write_text(TASK_YAML.format(model=prof["model_path"], tier=tier, name=name, idle=IDLE_S // 60, listen=LISTEN_NAME,
                                                      priority=prio, runtime_q=json.dumps(prof["runtime"]), model_q=json.dumps(prof["model_path"])))
        (d / "hold.py").write_text(HOLD_PY.format(idle=IDLE_S // 60))
        load_task(task_file(name), load_models())
        if public:
            funnel(name, port)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise ApiError(str(e)) from None
    key = new_key()
    cfg = {"name": name, "port": port, "public": bool(public), "key_sha256": key_hash(key), "created": time.time()}
    write_json(config_file(name), cfg)
    mark_registered(name)
    _commit(name, f"feat(tasks): 网页建算力 API {name}")
    return {"name": name, "key": key, "port": port, "urls": urls(cfg), "runtime": prof["runtime"]}


def rekey(name: str) -> dict[str, Any]:
    cfg = configs().get(name)
    if cfg is None:
        raise ApiError(f"没有 API {name}")
    key = new_key()
    write_json(config_file(name), {**cfg, "key_sha256": key_hash(key)})
    return {"name": name, "key": key, "urls": urls(cfg), "runtime": tier_of(load_task(task_file(name)))[1]["runtime"]}


def stop(name: str) -> dict[str, Any]:
    """结束会话：同 /stop（运行器在下一个边界取走停止标记，会话收到 SIGTERM）。"""
    if current().get("task") != name:
        raise ApiError(f"{name} 没有在跑的会话")
    CONTROL.mkdir(parents=True, exist_ok=True)
    (CONTROL / f"stop-{name}").write_text(str(time.time()))
    return {"ok": True}


def delete(name: str) -> dict[str, Any]:
    """删 API：撤 Funnel 路径、删配置、删任务（定义与工作目录；运行记录留着）、提交进仓库。会话在跑 / 在排队的先结束。"""
    from control_center.cli import remove_task

    cfg = configs().get(name)
    if cfg is None:
        raise ApiError(f"没有 API {name}")
    if pending(name):
        raise ApiError(f"{name} 的会话在跑或在排队：先结束会话再删")
    if cfg.get("public"):
        funnel(name, None)
    config_file(name).unlink(missing_ok=True)
    live_file(name).unlink(missing_ok=True)
    remove_task(name, keep_runs=True)
    _commit(name, f"chore(tasks): 网页删算力 API {name}")
    return {"ok": True}


def overview(days: int = 30) -> dict[str, Any]:
    """「API」页要的：每个 API 的状态、今天的用量，近 days 天每天的 token，最近 50 次请求，模型菜单。"""
    now = time.time()
    cur, queued = current(), {e.get("task") for _, e in runner._queue_entries()}
    rows, since = [], now - days * 86400
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    for name, cfg in configs().items():
        try:
            task = load_task(task_file(name))
            tier, prof = tier_of(task)
            info = {"tier": tier, "model": prof["model_path"], "priority": task.priority, "enabled": task.enabled}
        except Exception as e:  # noqa: BLE001
            info = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        s = session(name)
        state = "running" if s else "loading" if cur.get("task") == name else "queued" if name in queued or runner.task_pending(name) else "idle"
        today = usage_rows(name, midnight, now)
        rows.append({"name": name, "port": cfg["port"], "public": bool(cfg.get("public")), "created": cfg.get("created"), "urls": urls(cfg),
                     "state": state, "session": {"run_id": s["run_id"], "started": s.get("started"), "inflight": read_json(live_file(name)).get("inflight", 0)} if s else None,
                     "last_request": read_json(live_file(name)).get("last_request"), "waiting": waiting_text(name) if state == "queued" else None,
                     "today": {"requests": len(today), "input": sum(int(r.get("input") or 0) for r in today), "output": sum(int(r.get("output") or 0) for r in today)},
                     "last_run": {k: v for k, v in last_run(name).items() if k in ("id", "status", "error")}, **info})
    hist = sorted((r for c in configs() for r in usage_rows(c, since, now)), key=lambda r: r["ts"])
    daily: dict[str, dict[str, int]] = {}
    for r in hist:
        k = time.strftime("%Y-%m-%d", time.localtime(r["ts"])) + "|" + str(r.get("api"))
        d = daily.setdefault(k, {"requests": 0, "tokens": 0})
        d["requests"] += 1
        d["tokens"] += int(r.get("input") or 0) + int(r.get("output") or 0)
    return {"apis": rows, "models": models_menu(),
            "daily": [{"day": k.split("|")[0], "api": k.split("|")[1], **v} for k, v in sorted(daily.items())],
            "recent": hist[-50:][::-1], "prefix": PREFIX, "idle_minutes": IDLE_S // 60}
