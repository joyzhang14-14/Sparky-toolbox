"""stbcc-hub：聊天中枢。卡片、系统通知、事件发布、任务监听、状态栏与运行进度都在这里；各聊天入口（Discord、网页…）是适配器（渠道）。

- 本机接口 ~/sparky-toolbox/data/sockets/hub.sock（660、归 sparkle 组，一行 JSON 请求、一行 JSON 回复；组里的 sparky 能连，sparky 只能调 reply、登记了 sparky=True 的卡与下面的运行内 op，见 caller_denied）：
  平台各处经 hub_client.call(op, …) 调：status、presence、loading、progress_start、activity、progress_end、send、card、card_refresh；
  适配器调：publish（Joy 发来的一条消息：来源 message 的事件，带来源渠道、消息标记 ref、ts、附件，见下面的「事件」）、
  card_act（处理一张卡）、hello（适配器上线时取当前状态栏文字）；
  reply 工具（tools/reply/server.py）调：reply（模型给 Joy 的回复，见下面的「回复」）；
  以 sparky 跑的工具在运行里要改平台数据时调（run_op，都带 run_ref，只认在跑的运行、只动这次运行的东西）：secret（取走自己发的带密码框的卡里 Joy 填的值，一次）、
  card_use（把这次运行已同意的卡标成用掉；运行挂起（waiting）时也认：Joy 同意后平台替那次调用执行）、
  card_expire（发卡的调用不再等自己发的带密码框的卡：作废它，Joy 填不了）、
  monitor（一次性动态监听的登记与取消，control_center/monitors.hub_op）、
  skill_install（skills 工具：Joy 在审批卡上同意导入后，以平台用户装进仓库 skills/，control_center/skills.install）；
  别的平台组件经 hub_client.call("publish", source=<来源名>, event={…}) 发布事件（见下面的「事件」）。
- 入口列表：config/stbcc.yaml 的 channels（如 [discord, web]），只是过滤：已报到、又写在列表里的入口才收中枢往外发的
  （系统通知、卡片、状态栏、进度）；没有这个文件或写坏了 = 全部已报到的入口。每次往外发都现读，增删即时生效，不用重启。入口发来的事件照常处理。
- 某个适配器不在（socket 连不上）或超时只是跳过它，不影响中枢和其他入口。
- 卡：card 建卡（data/hub/approvals/）再让各入口显示；card_act 判定并执行一次决定，
  卡上弹框里标了 secret 的字段（密码、凭证）的值只放本进程内存等取一次（cards.put_secret；通用审批卡的密码框由发卡的运行经 secret op 取），
  卡片、处理结果、日志、磁盘上都不留；处理后通知其余入口刷新同一张卡。
- 定时触发：每分钟（整分后 SCHEDULE_OFFSET_S 秒）按任务 yaml 的 schedule 入队，停机期间错过的补最近一次（control_center/schedule.py）。
- 每分钟：到期的卡作废并让各入口刷新。每小时看一次家目录所在分区的剩余空间，低于 DISK_FREE_MIN 私信 Joy 一次
  （附最近 24 小时写入最多的几个目录）；回到阈值以上之后再低于才再发（标记 data/runtime/control/disk-low）。
- 任务自带监听（任务文件夹里的 listen.py，control_center/listen.py）：另一个循环每秒看一次哪些监听到了检查间隔（30 秒），调 check(ctx)，fire 就入队；
  listen.py 写了 watch(ctx, emit) 的是常驻监听，同一个循环监督它（起、停、崩了按等待重起）。一次性动态监听（control_center/monitors.py）也在这个循环里：
  每秒看登记的审批卡（monitors.tick），生效的作为登记它的任务的常驻监听交给循环监督（monitors.watchers）。
- 算力 API 的门口（control_center/api_door.py）：每个 API 一个端口，认密钥、转发给 API 绑的模型；会话没在跑就让它的任务入队（平台自带的监听 api）。
- 事件：任何来源以来源名发布事件（publish op），交给 listen_events 订阅了这个来源的任务：调它的 on_event(event, ctx)，fire 就入队；同一来源的事件按到达顺序逐条交。
  来源只是名字，平台不列白名单；每个订阅者各自决定接不接（渠道发布的消息也一样：交给每个订阅者，fire 的各自入队，附件各拷一份）。
  一次性监听回调（control_center/monitors.py）也是事件：来源是登记它的工具名，只交给登记它的任务（publish 的 task）。
  on_event 的返回值还可以请平台对本任务做通用动作（actions：stop 停本任务的运行、撤本任务排队的同来源条目；clear_runs 删本任务某时刻起的运行记录，listen.run_actions），
  平台只对发请求的任务自己执行，执行结果接在它的 reply 后面回给发布方。

适配器接口（写新入口——Telegram、iMessage…——照这个做，中枢代码不用改）
- 报到：适配器启动时在 ~/sparky-toolbox/data/sockets/channels/<名字>.sock 开本机 unix socket（先删掉旧文件再 bind，权限 600），
  socket 文件在 = 已报到；退出时不用删（连不上只是被跳过）。名字就是入口名：发布事件时 channel 写它，回复按它找回来。
- 发布消息：publish {source: "message", event: {text, msg_id, origin?}, channel, ref?（消息标记，渠道自己的格式，之后的进度 op 原样带回）, ts, images?, docs?}；
  附件先放进渠道自己的暂存目录（data/hub/ 下），images / docs 写宿主路径，中枢交完（publish 返回）后渠道删掉暂存的文件。
- 格式：中枢连上后写一行 JSON 请求 {"op": …, …}，适配器回一行 JSON（至少 {"ok": true/false}，出错带 "error"），然后关连接。
  一般的 op 中枢等 ADAPTER_TIMEOUT 秒，reply 等 REPLY_TIMEOUT 秒；不认识的 op 回 {"ok": false, "error": "unknown op …"}。
- 中枢发给适配器的 op（都要回，用不上的直接回 {"ok": true}）：
  · status：{"ok": true, "ready": 是否就绪, …}（stbcc-hub 的 status 汇总各入口的）
  · presence {text}：状态栏文字（"正在：…"/"空闲"）
  · loading {task, ref?} / progress_start {task, started, ref?}：一次运行要换模型 / 开跑（ref：触发它的那条消息的标记）。回复里可带 "id"（适配器自己的进度标记），
    之后 activity / progress_end 原样带回；第一个回了 id 的适配器的 id 被采用（现在只有 Discord 给）
  · activity {id, state, subagent?, kind?, isolated?}：运行中的状态（think / tool / untrusted / loading / ask）；子代理调用期间带它的名字、kind、isolated，入口自己挑表情
  · progress_end {id, status, ref?}：运行结束（complete / failed / timeout / paused / partial / waiting / stopped）；id 为空时按 ref 找那条消息
  · send {text, task?}：系统通知（带 task 的是任务通知）
  · card {id} / card_refresh {id}：显示 / 刷新 data/hub/approvals/<id>.json 这张卡（适配器自己读卡文件，按自己的方式显示与收点击，决定经 card_act 交中枢）
  · reply {channel, text, files, host_files, run_ref, role}：模型的回复。files 是模型写的附件路径（沙箱里外同一个路径），host_files 是校验过的宿主路径
    （reply 工具已校验）；role = bot（渠道消息触发的运行的回复）/ cron（别的运行的成品）。成功回 {"ok": true, "result": "给模型看的一句话"}；
    不显示这条（不归它管）回 {"ok": true, "skipped": true}。对话记录归任务（任务自己的 hook 写），中枢与适配器都不记。
- 回复（reply）往哪发：按这次运行 run.json 的 channel——是某个已报到的入口就只发给它（不看入口列表；它不在 = 回复失败，模型会收到错误）；
  没有 channel（定时任务、监听触发的运行）就发给开着的各入口，由适配器决定显示与否（Discord 显示；网页对定时任务的成品 role cron 回收到但不存，它从任务消息库 tasks.sqlite3 读来显示——
  任务的 post_call hook 在 reply 成功后才记库，网页回收到让 Discord 不在时这条 reply 也算送达）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from control_center import cards, acquire_permission, builder, hub_client, env, listen, monitors, registry, runner, schedule, scheme_check  # noqa: F401 — builder / monitors / runner 导入时登记卡处理器
from control_center import paths, reply, skills
from control_center.paths import CONTROL, HUB, INBOX, MEDIA_EXT, REPO, RUNS, WORK, ensure_dirs, run_work_dir, sparky_home, task_file
from control_center.task import NAME_RE, load_task, subscribers

SWEEP_S = 60
SCHEDULE_OFFSET_S = 1                        # 定时触发在整分之后多少秒检查（原定时刻那一分钟里）
SCHEMA_ALERT_DELAY_S = 15                    # 字段表不通过时，启动后等这么久（入口上线）再私信 Joy
DISK_CHECK_S = 3600                          # 多久看一次家目录所在分区的剩余空间
DISK_FREE_MIN = 200 * 2 ** 30                # 剩余低于这么多就私信 Joy
DISK_SCAN_S = 30                             # 找最近写入多的目录：最多扫这么久（只在要报警时扫一次）
ADAPTER_TIMEOUT = 10
REPLY_TIMEOUT = 180                          # 回复可能带 10 个附件、要重试限流：等久一点
RUN_REF_RE = re.compile(r"[\w][\w.-]*/[\w][\w.-]*")   # <任务>/<运行 id>
SPARKY = paths.TOOL_USER                     # 以工具用户（stbcc.yaml 的 tool_user，默认 sparky） + Landlock 跑的工具：socket 归 sparkle 组、组可读写，但它只能调下面这几个 op
RUN_OPS = ("secret", "card_use", "card_expire", "monitor", "skill_install")   # 运行内 op（run_op）：带 run_ref，只认在跑的运行
SPARKY_OPS = {"reply": None, "card": cards.SPARKY_KINDS, "card_use": cards.SPARKY_KINDS,
              **{op: None for op in RUN_OPS if op != "card_use"}}   # op → 允许的卡种类（None = 不看）：reply 工具发回复；卡只能发 / 用登记时写了 sparky=True 的（通用审批卡 permission…）
HUB_OPS = ("status", "hello", "presence", "loading", "progress_start", "activity", "progress_end", "card_refresh", "send", "card", "card_act",
           "reply", "publish", *RUN_OPS)   # handle 认的 op（报错时列出）


def adapters_dir() -> Path:
    return paths.CHANNEL_SOCKETS


def adapter_socket(name: str) -> Path:
    """渠道报到的 socket：data/sockets/channels/<名字>.sock。"""
    return adapters_dir() / f"{name}.sock"


def registered() -> list[str]:
    """已报到的入口：data/sockets/channels/ 下的 <名字>.sock。"""
    return sorted(p.stem for p in adapters_dir().glob("*.sock")) if adapters_dir().is_dir() else []


def entries() -> list[str]:
    """开着的入口：已报到、又写在 config/stbcc.yaml 的 channels 里的（没报到的名字跳过）；没写或读不了就是全部已报到的入口。"""
    reg = registered()
    try:
        names = paths.settings().get("channels")
    except (OSError, ValueError):
        return reg
    return [n for n in names if n in reg] if isinstance(names, list) else reg


def run_info(ref: Any) -> dict[str, Any]:
    """<任务>/<运行 id> 的 run.json（回复按它的 channel 找入口）；格式不对或读不了返回空。"""
    if not RUN_REF_RE.fullmatch(str(ref or "")):
        return {}
    try:
        info = json.loads((RUNS / str(ref) / "run.json").read_text())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def disk_low_text(root: Path | None = None) -> str | None:
    """家目录所在分区剩余低于 DISK_FREE_MIN、而且上次报过之后回到过阈值以上（或没报过）：返回给 Joy 的通知；否则 None。"""
    root = root or Path.home()
    marker = CONTROL / "disk-low"
    free = shutil.disk_usage(root).free
    if free >= DISK_FREE_MIN:
        marker.unlink(missing_ok=True)
        return None
    if marker.exists():
        return None
    CONTROL.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()))
    top, partial = recent_writes(root)
    rows = "\n".join(f"- {d}：{n / 2 ** 30:.1f} GB" for d, n in top) or "（没找到）"
    return (f"⚠ 磁盘：{root} 所在分区只剩 {free / 2 ** 30:.0f} GB（低于 {DISK_FREE_MIN // 2 ** 30} GB）。最近 24 小时写入最多的目录"
            + ("（扫描到时限，只统计了一部分）" if partial else "") + f"：\n{rows}")


def recent_writes(root: Path, since_s: float = 86400, depth: int = 2, top: int = 5) -> tuple[list[tuple[str, int]], bool]:
    """root 下最近 since_s 秒改过的文件按前 depth 级目录合计大小，取最大的 top 个；不跟符号链接、不跨文件系统，最多扫 DISK_SCAN_S 秒
    （到点就停，第二个返回值为 True）。"""
    since, deadline = time.time() - since_s, time.monotonic() + DISK_SCAN_S
    dev = root.stat().st_dev
    sums: dict[str, int] = {}
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            return sorted(sums.items(), key=lambda x: -x[1])[:top], True
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_dir(follow_symlinks=False):
                            if st.st_dev == dev:
                                stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False) and st.st_mtime >= since:
                            key = str(root.joinpath(*Path(e.path).relative_to(root).parts[:-1][:depth]))   # 文件所在目录的前 depth 级
                            sums[key] = sums.get(key, 0) + st.st_size
                    except (OSError, ValueError):
                        pass
        except OSError:
            pass
    return sorted(sums.items(), key=lambda x: -x[1])[:top], False


def card_file(path: Any) -> Path | None:
    """卡片能附的文件只有两处：卡内容太长时的全文（cards.attach_dir：data/hub/cards/<kind>/），
    工具给的附图（如截图：工作目录 <workspace>/<任务>/media/）。"""
    if not path:
        return None
    f = Path(str(path)).resolve()
    work = WORK.resolve()
    ok = (f.is_relative_to(cards.attach_dir("").resolve())
          or (f.is_relative_to(work) and f.relative_to(work).parts[1:2] == ("media",)))
    return f if ok and f.is_file() else None




def _events_of(name: str) -> list[str]:
    """任务 name 订阅的事件来源（yaml 的 listen_events）；读不了是空。"""
    try:
        return load_task(task_file(name), refresh=False).listen_events
    except Exception:  # noqa: BLE001
        return []


def _file_ref(f: Path, name: str) -> str:
    """图片以外的附件给任务 name 的模型的写法：音视频（MEDIA_EXT）给工作目录里的路径 <workspace>/<任务>/in/<名>（沙箱里外同一个路径），
    其余类型只给文件名（交隔离子代理读）。"""
    return str(WORK / name / "in" / f.name) if f.suffix.lower() in MEDIA_EXT else f.name


def _take_attachments(name: str, images: list[Path], docs: list[Path]) -> tuple[list[str], list[str]]:
    """事件的附件拷进接下它的任务 name：图片进工作目录 <workspace>/<任务>/in/（返回这个路径，随 prompt 给模型看）；
    其他文件里音视频（MEDIA_EXT）进工作目录 in/（返回路径），其余类型进 data/hub/inbox/<任务>/（沙箱看不到，返回文件名，运行开始时拷进该次运行的 in/）。
    每个接下的任务各拷一份。"""
    atts, files = [], []
    for f in images:
        d = WORK / name / "in"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, d / f.name)
        atts.append(str(d / f.name))
    for f in docs:
        ref = _file_ref(f, name)
        if ref == f.name:
            d = INBOX / name
            d.mkdir(parents=True, exist_ok=True, mode=0o750)
        else:
            d = WORK / name / "in"
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, d / f.name)
        files.append(ref)
    return atts, files


class Hub:
    def __init__(self) -> None:
        self.presence = "空闲"                     # 最近一次的状态栏文字：适配器重启上线时经 hello 取回
        self._bg: set[asyncio.Task[Any]] = set()
        self._disk_checked = 0.0                    # 上次看磁盘剩余空间的时刻（monotonic）
        self._locks: dict[str, asyncio.Lock] = {}   # 事件来源 → 锁：同一来源的事件按到达顺序逐条交（publish）

    # ---- 往各入口发 ----
    async def fanout(self, op: str, exclude: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        """把一个 op 发给开着的各入口（exclude 除外），返回回了话的适配器的回复。适配器不在 / 超时只是跳过。"""
        names = [n for n in entries() if n != exclude]
        rs = await asyncio.gather(*(hub_client.request(adapter_socket(n), {"op": op, **kw}, timeout=ADAPTER_TIMEOUT) for n in names))
        return [r for r in rs if r]

    async def reply(self, kw: dict[str, Any]) -> dict[str, Any]:
        """模型的回复：回到这次运行的入口（run.json 的 channel，已报到就只发它）；没有入口的发给开着的各入口，由适配器决定显示与否。
        至少一个适配器显示了才算发出；都没有就回错误（reply 工具把它报给模型）。"""
        info = run_info(kw.get("run_ref"))
        channel = info.get("channel")
        req = {"op": "reply", "channel": channel, **{k: kw.get(k) for k in ("text", "files", "host_files", "run_ref", "role")}}
        names = [channel] if channel in registered() else entries()
        got = await asyncio.gather(*(hub_client.request_detail(adapter_socket(n), req, timeout=REPLY_TIMEOUT) for n in names))
        rs = [r for r, _ in got]
        done = [(n, r) for n, r in zip(names, rs) if r and r.get("ok") and not r.get("skipped")]
        if done:
            return {"ok": True, "entries": [n for n, _ in done], "result": "；".join(str(r.get("result") or f"已发到 {n}") for n, r in done)}
        errs = [f"{n}：没有响应（{why or '回复没写 ok'}）" if not (r and r.get("error")) else f"{n}：{r['error']}"
                for n, (r, why) in zip(names, got) if not (r and r.get("ok"))]
        skipped = [n for n, r in zip(names, rs) if r and r.get("ok") and r.get("skipped")]
        why = "；".join(errs) or (f"{'、'.join(skipped)} 没有显示这条回复（这次运行不是从它来的）" if skipped
                                  else f"没有开着的入口（{adapters_dir()} 下没有报到的适配器 socket）")
        return {"ok": False, "error": f"回复没有发出去：{why}。不要重试这条回复：把要说的内容写进这次运行的最终结果（最后的回答）"}

    def background(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---- 事件 ----
    async def publish(self, source: str, event: dict[str, Any], channel: str | None = None, ref: dict[str, Any] | None = None, ts: float | None = None,
                      images: list[str] | None = None, docs: list[str] | None = None, task: str | None = None,
                      priority: int | None = None, watch: str | None = None) -> dict[str, Any]:
        """来源 source 的一个事件 → 交给每个订阅了它、启用的任务（task：只交给这一个任务——一次性监听回调交给登记它的任务；
        它没订阅这个来源就直接入队，用 event 的 prompt）。同一来源的事件按到达顺序逐条处理（前一条交完才交下一条）。
        订阅者：调 on_event(event, ctx)，fire 就入队（trigger listen，source.event 记来源名；带 steer 的可插进它正在跑的对话），actions 照做（只对它自己）。
        渠道发布时带 channel（来源渠道）、ref（消息标记）、ts、images / docs（附件在渠道暂存目录里的宿主路径）：event 里另给 channel、ts、
        images、docs（图片 / 图片以外的附件的文件名）；附件在入队时拷进每个接下它的任务（给模型的是拷进去以后的路径）
        （图片和音视频进工作目录 in/，其余进 data/hub/inbox/<任务>/）。
        priority、watch 原样写进入队的条目（一次性监听回调的优先级与状态栏标签）。
        返回 {ok, source, tasks: {任务: queued / skipped / error: …}}，有任务入队另带 queued（第一个的队列文件名）、task；没有入队、有 reply 带 reply；
        都没有、有出错的 ok 为 false、error 是原因。"""
        lock = self._locks.setdefault(source, asyncio.Lock())
        async with lock:
            return await self._deliver(source, event, channel, ref, ts, [Path(str(x)) for x in images or []], [Path(str(x)) for x in docs or []],
                                       task, priority, watch)

    async def _deliver(self, source: str, event: dict[str, Any], channel: str | None, ref: dict[str, Any] | None, ts: float | None,
                       images: list[Path], docs: list[Path], task: str | None, priority: int | None, watch: str | None) -> dict[str, Any]:
        ts = float(ts or time.time())
        if bad := [str(f) for f in [*images, *docs] if not f.resolve().is_relative_to(HUB.resolve()) or not f.is_file()]:
            return {"ok": False, "error": f"附件要是 data/hub/ 下渠道暂存目录里的文件：{'、'.join(bad)}"}
        event = {**event, "source": source, "ts": ts, **({"channel": channel} if channel else {})}
        if channel:
            event.setdefault("msg_id", f"{channel}{int(ts * 1000)}")
        if images or docs:
            event.update(images=[f.name for f in images], docs=[f.name for f in docs])
        entry = {"channel": channel, "ref": ref, "priority": priority, "watch": watch,
                 "source": {"event": source, **({"origin": "cli"} if event.get("origin") == "cli" else {})}}
        names = [task] if task else subscribers(source, enabled_only=True)
        if task and source not in _events_of(task):          # 指定的任务没订阅这个来源：直接入队，用事件给的 prompt
            got = [(task, {"fire": True, "prompt": str(event.get("prompt") or "")}, None)]
        else:
            got = await asyncio.gather(*(self._on_event(n, event, names) for n in names))
        out: dict[str, Any] = {} if task else {n: "skipped: 已停用" for n in subscribers(source) if n not in names}
        queued, replies, errs = [], [], []
        for name, res, err in got:
            if err is not None:
                out[name] = f"error: {err}"
                errs.append(err)
                continue
            notes = await self.run_actions(name, res.get("actions") or [], source)
            if res.get("reply") is not None or notes:
                replies.append(str(res.get("reply") or "") + "".join(notes))
            if not res["fire"]:
                out[name] = "skipped"
                continue
            try:
                atts, files = _take_attachments(name, images, docs)
                q = runner.enqueue(name, trigger="listen", prompt=res["prompt"], steer=res.get("steer"), loads_since=res.get("loads_since"), attachments=atts, files=files,
                                   **{k: v for k, v in entry.items() if v})
                out[name] = "queued"
                queued.append((name, getattr(q, "name", "")))
            except Exception as e:  # noqa: BLE001
                out[name] = f"error: 入队失败：{type(e).__name__}: {str(e)[:200]}"
                errs.append(f"{name} 入队失败：{type(e).__name__}: {str(e)[:200]}")
        if len(queued) > 1:
            print(f"事件 {source}：{'、'.join(n for n, _ in queued)} 都接了，各自入队", flush=True)
        if queued:
            return {"ok": True, "source": source, "tasks": out, "queued": queued[0][1], "task": queued[0][0]}
        if replies:
            return {"ok": True, "source": source, "tasks": out, "reply": "\n".join(replies)}
        if errs:
            return {"ok": False, "source": source, "tasks": out, "error": "；".join(errs) + "，没有入队"}
        return {"ok": True, "source": source, "tasks": out}

    async def _on_event(self, name: str, event: dict[str, Any], names: list[str]) -> tuple[str, Any, str | None]:
        """调 name 的 on_event（ctx 的 tasks 是这个事件交给的全部任务 names）：返回 (任务名, 结果, 出错原因)。"""
        source = event.get("source")
        try:
            task = load_task(task_file(name))
        except Exception as e:  # noqa: BLE001 — 任务定义读不了
            why = f"{type(e).__name__}: {str(e)[:300]}"
            print(f"事件 {source}：{name} 的任务定义读不了：{why}", flush=True)
            return name, None, f"{name} 的任务定义读不了：{why}"
        try:
            return name, await listen.call(task, "on_event", "event", event, tasks=names), None
        except Exception as e:  # noqa: BLE001 — 监听程序起不来、报错、超时、格式不对
            why = listen.failure_why(task, e, "on_event")
            print(f"事件 {source}：{name} 的 listen.py 处理失败：{why}", flush=True)
            return name, None, f"{name} 的监听程序（{listen.rel_file(task)}）处理这个事件失败：{why}"

    async def run_actions(self, name: str, actions: list[dict[str, Any]], source: str) -> list[str]:
        """on_event 请平台对任务 name 自己做的通用动作（listen.run_actions，source 是请求它的事件的来源）；撤掉的条目带消息标记的，各渠道把它标成已停。
        返回接在 reply 后面的执行结果。"""
        if not actions:
            return []
        notes, dropped = await asyncio.to_thread(listen.run_actions, name, actions, source)
        for e in dropped:
            if ref := e.get("ref"):
                await self.fanout("progress_end", id="", status="stopped", ref=ref)
        return notes

    # ---- 卡 ----
    async def card(self, req: dict[str, Any]) -> dict[str, Any]:
        card = cards.create(req["kind"], req["title"], req["body"], req["buttons"], ttl_seconds=req.get("ttl_seconds", cards.DEFAULT_TTL),
                                select=req.get("select"), modal=req.get("modal"), payload=req.get("payload"))
        f = card_file(req.get("file"))
        card["file"] = str(f) if f else None      # 卡片附的截图 / 全文：各入口都能显示
        cards.save(card)
        await self.fanout("card", id=card["id"])
        return {"ok": True, "id": card["id"]}

    def act(self, card_id: str, action: str, value: Any) -> dict[str, Any]:
        """一次卡片操作（Discord 的按钮 / 下拉 / 弹框，网页的卡片组件）。value：弹框字段 {id: 值}、下拉选中的值、问题卡「其他」的文字。
        已处理的卡幂等（只返回现状）；过期的作废；问题卡交 acquire_permission.interact；弹框里标了 secret 的字段只放本进程内存（cards.put_secret）。"""
        card = cards.load(card_id)
        if card is None:
            return {"ok": False, "error": "这张卡已不存在"}
        note = None
        if not card.get("resolved"):
            if cards.expired(card):
                cards.resolve(card, "expired")
            elif card["kind"] == acquire_permission.KIND:
                note = acquire_permission.interact(card, action, value)
            elif action == "select":
                card["select"]["value"] = str(value)
                cards.save(card)
            elif action not in {b["id"] for b in card.get("buttons") or []}:
                ids = [str(b["id"]) for b in card.get("buttons") or []]
                return {"ok": False, "error": f"这张卡没有「{action}」按钮（只认：{'、'.join(ids) or '没有按钮'}），可能是旧卡：看最新的那张卡再点"}
            elif secret := cards.secret_fields(card):     # 密码 / 凭证（弹框里标了 secret 的字段）：只放本进程内存等取一次，卡片、处理结果、日志里都不留
                fields = value if isinstance(value, dict) else {}
                if secret[0] in fields:
                    cards.put_secret(card["id"], str(fields[secret[0]]))
                cards.resolve(card, action, {k: v for k, v in fields.items() if k not in secret} or None)
            else:
                cards.resolve(card, action, value if isinstance(value, dict) else None)
        return {"ok": True, "card": cards.load(card["id"]) or card, "note": note}

    # ---- 运行内 op（以 sparky 跑的工具在运行里要改平台数据的事）----
    async def run_op(self, op: str, kw: dict[str, Any]) -> dict[str, Any]:
        """secret / card_use / monitor：run_ref 要是在跑的运行，只动这次运行的卡、这个任务的一次性监听。"""
        ref = str(kw.get("run_ref") or "")
        live = ("running", "waiting") if op in ("card_use", "card_expire", "skill_install") else ("running",)   # card_use：挂起的运行里 Joy 同意了，平台当场替那次调用执行（acquire_permission 的批了就执行）
        if not RUN_REF_RE.fullmatch(ref) or ".." in ref.split("/") or run_info(ref).get("status") not in live:
            return {"ok": False, "error": f"{op}：运行 {ref!r} 不在跑（run_ref 要写 <任务>/<运行 id>，且 run.json 是 running）"}
        if op == "secret":                                # 只给发卡的那次运行，取一次；回复里的值不进任何日志
            card = cards.load(str(kw.get("id", ""))) or {}
            if not cards.secret_fields(card) or (card.get("payload") or {}).get("run") != ref:
                return {"ok": False, "error": "不是这次运行发的带密码框的卡"}
            if (card.get("resolved") or {}).get("action") != "approve":
                return {"ok": False, "error": "Joy 还没在这张卡上输入密码（或拒绝了）"}
            return {"ok": True, "value": cards.take_secret(card["id"]) or ""}
        if op == "card_expire":                           # 发卡的调用不再等这张带密码框的卡：作废，各入口刷新（按钮变灰）
            card = cards.load(str(kw.get("id", ""))) or {}
            if not cards.secret_fields(card) or (card.get("payload") or {}).get("run") != ref:
                return {"ok": False, "error": "不是这次运行发的带密码框的卡"}
            if not card.get("resolved"):
                cards.resolve(card, "expired")
                await self.fanout("card_refresh", id=card["id"])
            return {"ok": True}
        if op == "card_use":
            match = kw.get("match") or {}
            if not isinstance(match, dict) or not all(isinstance(v, str) for v in match.values()):
                return {"ok": False, "error": "match 要写 {payload 键: 字符串}"}
            return {"ok": True, "used": acquire_permission.take_approved(str(kw.get("kind", "")), ref, card_id=str(kw["id"]) if kw.get("id") else None, **match)}
        if op == "skill_install":                         # skills 工具：Joy 在审批卡上同意导入后，以平台用户拷进仓库 skills/、提交、建依赖环境（sparky 写不了仓库）
            return await asyncio.to_thread(skills.install, ref, str(kw.get("name", "")), str(kw.get("sha", "")), str(kw.get("staged", "")))
        return await monitors.hub_op(ref, kw)

    # ---- 本机接口 ----
    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        kw = {k: v for k, v in req.items() if k != "op"}
        if op == "status":
            rs = {n: await hub_client.request(adapter_socket(n), {"op": "status"}, timeout=ADAPTER_TIMEOUT) for n in registered()}
            return {"ok": True, "ready": True, "entries": entries(), "adapters": rs}
        if op == "hello":
            return {"ok": True, "presence": self.presence}
        if op == "presence":
            self.presence = str(kw.get("text", "空闲"))
            await self.fanout("presence", text=self.presence)
            return {"ok": True}
        if op in ("loading", "progress_start"):          # 回复里的 id 是适配器的（Discord：react:<消息 id>），之后 activity / progress_end 原样带回
            rs = await self.fanout(op, **kw)
            got = next((r for r in rs if r.get("id")), None)
            return got or {"ok": True, "id": "presence" if op == "progress_start" and not kw.get("ref") else ""}
        if op in ("activity", "progress_end", "card_refresh"):
            await self.fanout(op, **kw)
            return {"ok": True}
        if op == "send":                                  # 系统通知；带 task 的是任务通知（结果、失败、部分完成）；entries：回了 ok 的入口（一个都没有就是空列表）
            names = entries()
            rs = await asyncio.gather(*(hub_client.request(adapter_socket(n), {"op": "send", "text": str(kw.get("text", "")), "task": kw.get("task")},
                                                           timeout=ADAPTER_TIMEOUT) for n in names))
            return {"ok": True, "entries": [n for n, r in zip(names, rs) if r and r.get("ok")]}
        if op == "card":                                  # 登记了 review 的卡（通用审批卡 permission）按 payload 重新生成，调用方给的标题正文不用
            if review := cards.REVIEWS.get(str(kw.get("kind"))):
                kw = review(kw.get("payload") or {})
            return await self.card(kw)
        if op == "card_act":                              # 处理后其余入口跟着改（后台发，发起的入口不等）
            r = self.act(str(kw.get("id", "")), str(kw.get("action", "")), kw.get("value"))
            if r.get("ok"):
                self.background(self.fanout("card_refresh", exclude=kw.get("origin"), id=r["card"]["id"]))
            return r
        if op in RUN_OPS:
            return await self.run_op(op, kw)
        if op == "reply":
            return await self.reply(kw)
        if op == "publish":
            source, event = str(kw.get("source") or ""), kw.get("event") or {}
            if not NAME_RE.fullmatch(source) or not isinstance(event, dict):
                return {"ok": False, "error": "publish 要 source（来源名：小写字母、数字、- _）和 event（映射）"}
            return await self.publish(source, event, **{k: kw[k] for k in ("channel", "ref", "ts", "images", "docs", "task", "priority", "watch") if kw.get(k) is not None})
        return {"ok": False, "error": f"unknown op {op!r}（可用：{', '.join(HUB_OPS)}）"}

    async def sweep_loop(self) -> None:
        """每分钟：ttl 到期的卡作废、各入口按钮变灰；每小时看一次磁盘剩余空间（disk_low_text）。"""
        while True:
            try:
                for card in cards.pending():
                    if cards.expired(card):
                        cards.resolve(card, "expired")
                        await self.fanout("card_refresh", id=card["id"])
                env.write_keys()                  # Joy 手改 .env 后补写 .env.keys（键名，给 sparky 看）
                cards.purge_secrets()             # 填了没人取的密码 / 凭证值：过期就丢
                if time.monotonic() - self._disk_checked >= DISK_CHECK_S or not self._disk_checked:
                    self._disk_checked = time.monotonic()
                    if text := await asyncio.to_thread(disk_low_text):
                        await self.fanout("send", text=text)
            except Exception as e:  # noqa: BLE001
                print(f"sweep 出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            await asyncio.sleep(SWEEP_S)

    async def schema_alert(self, probs: list[str]) -> None:
        """字段表不通过：等入口上线后私信 Joy 一次（SCHEMA_ALERT_DELAY_S 秒后）。"""
        await asyncio.sleep(SCHEMA_ALERT_DELAY_S)
        await self.fanout("send", text="⚠ stbcc-hub 因为字段表不通过而不触发任务（定时、监听都停着）：\n" + "\n".join(f"- {p}" for p in probs[:20]))

    async def schedule_loop(self) -> None:
        """定时触发（trigger: schedule）：启动时和之后每分钟（整分后 SCHEDULE_OFFSET_S 秒）按任务 yaml 的 schedule 入队（control_center/schedule.py）。"""
        while True:
            try:
                for name, slot in await asyncio.to_thread(schedule.tick, time.time()):
                    print(f"定时触发：{name}（原定 {time.strftime('%m-%d %H:%M', time.localtime(slot))}）已入队", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"定时触发出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            await asyncio.sleep(60 - time.time() % 60 + SCHEDULE_OFFSET_S)

    async def listen_loop(self) -> None:
        """写了 listen 的任务：到了检查间隔（30 秒）就在后台调一次 listen.py 的 check（不阻塞中枢），常驻的（watch）由同一个检查循环监督；
        一次性动态监听：每次循环在后台跑一遍 monitors.tick（上一遍没完就不叠），生效的经 monitors.watchers 交给检查循环。连续失败的通知经 send 发给各入口。"""
        async def notify(text: str) -> None:
            await self.fanout("send", text=text)

        listen.clear_live()                               # 上一个实例的常驻监听随它停了
        checker = listen.Listener(notify, extra=monitors.watchers)
        timed: asyncio.Task[Any] | None = None
        while True:
            try:
                checker.tick()
            except Exception as e:  # noqa: BLE001
                print(f"监听循环出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            if timed is None or timed.done():
                if timed is not None and not timed.cancelled() and (e := timed.exception()) is not None:
                    print(f"一次性监听循环出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
                timed = asyncio.create_task(monitors.tick())
            await asyncio.sleep(listen.TICK_S)

    async def workflow_loop(self) -> None:
        """workflow 任务的旁路（control_center/workflow.py）：每 TICK_S 秒起 data/runtime/steps/ 里的步骤（不要资源的，不进 runner 的队列）、收尾结束了的旁路单元、处理停止标记。"""
        from control_center import runner, workflow

        while True:
            try:
                notes, stale = await asyncio.to_thread(workflow.dispatch)
                for text in notes:
                    await self.fanout("send", text=text)
                for d in stale:
                    await runner.notify_stale(d)
            except Exception as e:  # noqa: BLE001
                print(f"workflow 旁路出错：{type(e).__name__}: {str(e)[:200]}", flush=True)
            await asyncio.sleep(workflow.TICK_S)

    async def serve_socket(self) -> None:
        path = hub_client.socket_path()
        path.unlink(missing_ok=True)

        async def conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(r.readline(), 10)
                req = json.loads(line or b"{}")
                resp = denied if (denied := caller_denied(req, peer_uid(w.get_extra_info("socket")))) else await self.handle(req)
            except Exception as e:  # noqa: BLE001 — 只回错误类型与消息，不回显请求（凭证卡的值经这里传）
                resp = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            try:
                w.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
                await w.drain()
                w.close()
            except OSError:
                pass

        srv = await asyncio.start_unix_server(conn, str(path), limit=2 ** 22)
        os.chmod(path, 0o600)
        paths.share_socket(path)
        self.background(asyncio.to_thread(runner.recover_approved))   # 上次退出时已同意、还没执行完的调用：补上（socket 已开，工具能经它用掉卡）
        async with srv:
            await srv.serve_forever()


def peer_uid(sock: Any) -> int | None:
    """unix socket 对端的 uid（SO_PEERCRED）；拿不到返回 None。"""
    import socket
    import struct

    try:
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
    except (OSError, AttributeError):
        return None


def caller_denied(req: dict[str, Any], uid: int | None) -> dict[str, Any] | None:
    """平台用户（本服务自己的 uid）什么都能调；sparky（以 sparky 跑的工具、监听程序）只能调 SPARKY_OPS；别人一律拒。"""
    if uid == os.getuid():
        return None
    try:
        import pwd

        sparky = pwd.getpwnam(SPARKY).pw_uid
    except KeyError:
        sparky = None
    op = req.get("op")
    if uid is None or uid != sparky:
        return {"ok": False, "error": f"调用方（uid {uid}）不能调 {op!r}：" + ("拿不到对端 uid（SO_PEERCRED）" if uid is None else f"只有平台用户和 {SPARKY} 能连中枢")}
    if op not in SPARKY_OPS:
        return {"ok": False, "error": f"调用方（{SPARKY}）不能调 {op!r}：{SPARKY} 只能调 {'、'.join(SPARKY_OPS)}"}
    if SPARKY_OPS[op] is not None and req.get("kind") not in SPARKY_OPS[op]:
        return {"ok": False, "error": f"调用方（{SPARKY}）不能调 {op!r} 发 kind {req.get('kind')!r} 的卡：{SPARKY} 只能发 {'、'.join(sorted(SPARKY_OPS[op]))} 这几种卡"}
    if op == "reply":
        return sparky_reply_denied(req)
    return None


def sparky_reply_denied(req: dict[str, Any]) -> dict[str, Any] | None:
    """sparky 发来的 reply 不信它自己报的宿主路径：按 files（模型写的路径）重新算 host_files（规则同 reply 工具，control_center/reply.host_files：sparky 家目录或这次运行工作目录下的文件），
    算出来的写回 req；run_ref 要是在跑的运行，role 只能是 reply.ROLES。不合规则返回拒绝。"""
    def no(why: str) -> dict[str, Any]:
        return {"ok": False, "error": f"回复被拒：{why}"}

    ref = str(req.get("run_ref") or "")
    if not RUN_REF_RE.fullmatch(ref) or ".." in ref.split("/"):
        return no("run_ref 要写 <任务>/<运行 id>")
    if (status := run_info(ref).get("status")) != "running":
        return no(f"运行 {ref} 不在跑（run.json 的 status 是 {status!r}）；不要重试这条回复，把要说的写进最终结果")
    if req.get("role") not in reply.ROLES:
        return no(f"role 只能是 {'、'.join(reply.ROLES)}")
    files, hosts = req.get("files") or [], req.get("host_files") or []
    if not isinstance(files, list) or not isinstance(hosts, list) or len(files) != len(hosts):
        return no("files 与 host_files 要一一对应")
    try:
        got = reply.host_files([sparky_home(), run_work_dir(RUNS / ref)], files)
    except reply.FileError as e:
        return no(str(e))
    for f, h, host in zip(files, hosts, got):
        if Path(str(h)).resolve() != host:
            return no(f"附件的宿主路径与模型写的路径对不上：{f}")
    req["host_files"] = [str(h) for h in got]
    return None


async def main() -> None:
    ensure_dirs()
    adapters_dir().mkdir(mode=0o700, exist_ok=True)
    hub = Hub()
    print(f"stbcc-hub 在线：已报到的入口 {', '.join(registered()) or '（还没有）'}，开着的 {', '.join(entries()) or '（没有）'}", flush=True)
    asyncio.create_task(hub.sweep_loop())
    if probs := scheme_check.problems():               # 字段表锁对不上：只收发消息，不触发任务（不跑定时触发与监听），私信 Joy
        print("字段表不通过，不触发任务：" + "；".join(probs), flush=True)
        asyncio.create_task(hub.schema_alert(probs))
    else:
        asyncio.create_task(hub.schedule_loop())
        asyncio.create_task(hub.listen_loop())
        asyncio.create_task(hub.workflow_loop())
        from control_center import api_door

        asyncio.create_task(api_door.Door(api_door.tailscale_ip()).run())   # 算力 API 的门口：每个 API 一个端口，会话没在跑就让任务入队
    await hub.serve_socket()


if __name__ == "__main__":
    asyncio.run(main())
