"""stbcc-discord：Discord 入口的适配器，常驻 Gateway 长连接（规格 11.7、9.2、D13）。卡片、系统通知、私信入队与命令、定时触发与任务监听
都在聊天中枢 stbcc-hub（control_center/hub.py）；本服务只把 Discord 上的私信、按钮、弹框转给中枢，再按中枢的指令发消息、显示卡片、换表情与状态栏。
本服务掉线不影响中枢和网页入口。

- 只认 owner 用户 ID 的私信（owner = .env 的 STB_OWNER_DISCORD_ID）；服务器消息、其他用户一律忽略。
- 收到私信：附件先放进本渠道的暂存目录（channels.staging），发布来源 message 的事件交中枢（publish：交给订阅它的任务的 listen.py，接不接、命令、对话记录都归任务；
  带消息标记 ref = 这条私信的位置），交完删掉暂存的附件。打断/排队/进度都只用状态栏和表情表示，不发提示文字。
- 斜杠命令：按命令表 data/messages/commands.json（私信任务各自登记的，channels.commands）注册，启动时同步（改了命令表重启 stbcc-discord 生效）；
  选了斜杠命令就像打了「/命令 参数」这句一样交中枢（publish），回话作为这次交互的回复发出。
- 进行中标记：DM 收到先加 🕒（排队），轮到它但要先加载任务指定的模型时（及子代理换模型期间）换成 ⏳（D32），开跑换成 🤔（模型思考），工具调用期间换成 🛠️，调用隔离子代理（联网研究、读邮件…）期间换成 📡，生图期间 🎨、做视频期间 🎞️，
  结束换成 ✅ / ❌ / ⏸️（仿 openclaw）；定时任务只改状态栏，不发临时消息。
- 状态栏文字由运行器算："正在：A（优先级）；打断：B（优先级），C（优先级）"，空闲时"空闲"。
- 本地接口 ~/sparky-toolbox/data/sockets/channels/discord.sock（启动时开 = 向中枢报到；一行 JSON，只有中枢调，接口说明见 hub.py）：
  presence / loading / progress_start / activity / progress_end / send / card / card_refresh / reply / status。
- 回复（reply）：中枢转来的模型回复经 discord_out 发到 owner 的私信（分块、清洗、nonce 幂等、零提及；role=cron 的带 CRON_NONCE），
- 审批卡（11.7 P3、11.8）：中枢建好卡（~/sparky-toolbox/data/hub/approvals/）后经 `card` 让本服务发到 DM；on_interaction 只认 owner 的点击，弹框在这里弹，决定交中枢（card_act），
  卡不过期（旧卡写了 ttl 的到期按钮变灰），卡片第一行是状态 emoji，重复点击幂等；带 modal 的按钮弹输入框，带 select 的卡用下拉改。问题卡（ask-user，D35）用 Components V2 卡（LayoutView，这种消息不带 content）一次只显示一题，同一条消息原地改，
  字母按钮选、「上一题」「下一题」翻页，最后一题之后是回答总览（「上一题」「提交」），进度存在卡里，提交才交处理器。
- 对话记录归任务（私信任务的 listen.py 记 Joy 的私信、run_end hook 记回复），本服务不记。系统通知的 nonce 带 stbsys 前缀，role=cron 的回复带 stbcron 前缀。
- 在线状态只对和 bot 有共同服务器的用户可见（Discord 按服务器分发 presence）：要看到绿点，需要一个只有 Joy 和 bot 的私有服务器；
  本服务仍然只处理私信，服务器消息一律忽略。
- intents 最小化：只有 dm_messages（私信内容对 bot 免特权 intent）。用 discord.py，不手写网关。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
import httpx

import inspect

from control_center import cards, acquire_permission, builder, hub_client, hub, runner  # noqa: F401
from channels.discord import out as discord_out
from control_center import env
from control_center.hub import card_file  # noqa: F401 — 网页聊天与测试从这里取
from control_center.paths import ensure_dirs
from channels import IMAGE_EXT, MAX_ATTACHMENT_BYTES, MESSAGE, commands, doc_name, staging  # noqa: F401 — 各渠道共用的附件规则、命令表

# 附件（11.7）：只从 Discord CDN 下载，图片和其他任何类型的文件都收，单个 ≤ 10 MB，30 秒超时（_save_attachments）
CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")
THINKING = "🤔"
QUEUED = "🕒"          # 私信收到、还没轮到跑
LOADING = "⏳"         # 轮到它了，但先要加载任务指定的模型（D32）
ASKING = "❓"          # 等 Joy 回答问题卡（D35）：就地等与挂起都是它
ACTIVITY = {"think": "🤔", "tool": "🛠️", "untrusted": "📡", "loading": LOADING, "ask": ASKING}   # 模型思考 / 普通工具调用 / 不可信来源（搜索、读邮件）/ 子代理换模型（D32）/ 等回答
SUBAGENT_EMOJI = {"image": "🎨", "video": "🎞️", "model3d": "🧱"}   # 本入口按子代理名换的表情（生图 / 做视频 / 做 3D 模型）；中枢的 activity 只带名字、kind、isolated，没列的按 state
TOOL_EMOJI = {"speak": "🎤"}   # 本入口按工具名换的表情（说话）；中枢的 activity 带工具名，没列的按 state
DONE = {"complete": "✅", "failed": "❌", "timeout": "❌", "paused": "⏸️", "partial": "🟡", "waiting": ASKING, "stopped": "⏹️"}   # partial：主模型时间用完已收尾，可续跑（D34）；waiting：挂起等回答（D35）
BUTTON_STYLE = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger, "primary": discord.ButtonStyle.primary,
                "secondary": discord.ButtonStyle.secondary}
SYSTEM_NONCE = "stbsys"     # 系统通知的 nonce 前缀
CRON_NONCE = "stbcron"     # role=cron 的回复（不是渠道消息触发的运行发的成品，control_center/reply.role）的 nonce 前缀
SLASH_NAME = re.compile(r"^[-_a-z0-9]{1,32}$")   # Discord 斜杠命令名的格式（命令表里不合格的不注册）
HUB_DOWN = "聊天中枢 stbcc-hub 没有响应"


def card_text(card: dict[str, Any]) -> str:
    lines = [f"{cards.status_emoji(card)} **{card['title']}**", card["body"]]   # ⏳ 等待中 / ✅ 已同意 / ❌ 已拒绝 / ⚪ 作废
    if card["kind"] == acquire_permission.KIND:
        lines.append(acquire_permission.render(card))
    if card.get("resolved"):
        lines.append(f"→ {card['resolved']['result']}")
    elif cards.expired(card):
        lines.append("→ 已过期")
    return "\n".join(lines)[:1900]


def card_view(card: dict[str, Any]) -> discord.ui.View:
    """卡的按钮；custom_id = stb:<卡id>:<动作>，不签名（只认 owner 的点击）。发出去后 view.stop()，点击走 on_interaction。
    处理过或过期的卡按钮留着但变灰：不收掉按钮行，消息高度基本不变，下面那张卡的按钮不会挪到手指底下被误点。"""
    done = bool(card.get("resolved")) or cards.expired(card)
    view = discord.ui.View(timeout=None)
    sel = card.get("select")
    if sel:
        opts = [discord.SelectOption(label=o["label"][:100], value=o["value"][:100], default=o["value"] == sel.get("value")) for o in sel["options"][:25]]
        view.add_item(discord.ui.Select(custom_id=f"stb:{card['id']}:select", placeholder=sel.get("placeholder", ""), options=opts, row=0, disabled=done))
    for b in card["buttons"]:
        view.add_item(discord.ui.Button(custom_id=f"stb:{card['id']}:{b['id']}", label=b["label"][:80], style=BUTTON_STYLE.get(b.get("style", "secondary"), discord.ButtonStyle.secondary),
                                        row=1, disabled=done))
    return view


def ask_layout(card: dict[str, Any]) -> discord.ui.LayoutView:
    """问题卡（D35）：一张 Components V2 卡。题目页：文字（第 i/n 题 · 单选/多选、题面、A. … 选项与 ✅、别的题的摘要）+ 两行按钮：
    每个字母一个（动作 L<序号>；最后一个是「其他…」，动作 other，弹框），「上一题」「下一题」。最后一题点下一题进回答总览：
    各题回答 +「上一题」「提交」。交掉以后只剩文字。
    填空题：文字（题面、提示、现在填的）+ 一行按钮「填写」（动作 other，弹框一个文本框，预填现在的字）「上一题」「下一题」；备注页（最后一页）的「下一题」换成「提交」。"""
    p, cid = card["payload"], card["id"]
    items: list[Any] = [discord.ui.TextDisplay(card_text(card)[:3900])]
    if not card.get("resolved") and p["current"] >= len(p["questions"]):
        items.append(discord.ui.ActionRow(
            discord.ui.Button(custom_id=f"stb:{cid}:prev", label="上一题"),
            discord.ui.Button(custom_id=f"stb:{cid}:submit", label="提交", style=discord.ButtonStyle.primary)))
    elif not card.get("resolved") and p["questions"][p["current"]]["type"] == "fill":
        i = p["current"]
        q, filled = p["questions"][i], bool(p["answers"][i].get("text"))
        items.append(discord.ui.ActionRow(
            discord.ui.Button(custom_id=f"stb:{cid}:other", label=("修改" if filled else "填写") + ("备注" if q.get("notes") else ""),
                              style=discord.ButtonStyle.success if filled else discord.ButtonStyle.secondary),
            discord.ui.Button(custom_id=f"stb:{cid}:prev", label="上一题", disabled=i == 0),
            discord.ui.Button(custom_id=f"stb:{cid}:submit", label="提交", style=discord.ButtonStyle.primary) if q.get("notes")
            else discord.ui.Button(custom_id=f"stb:{cid}:next", label="下一题")))
    elif not card.get("resolved"):
        i = p["current"]
        q, a = p["questions"][i], p["answers"][i]
        pick = lambda on: discord.ButtonStyle.success if on else discord.ButtonStyle.secondary  # noqa: E731
        letters = [discord.ui.Button(custom_id=f"stb:{cid}:L{j}", label=acquire_permission.LETTERS[j], style=pick(j in a["picked"])) for j in range(len(q["options"]))]
        letters.append(discord.ui.Button(custom_id=f"stb:{cid}:other", label=f"{acquire_permission.LETTERS[len(q['options'])]} {acquire_permission.OTHER}", style=pick(bool(a["other"]))))
        items.append(discord.ui.ActionRow(*letters))
        items.append(discord.ui.ActionRow(
            discord.ui.Button(custom_id=f"stb:{cid}:prev", label="上一题", disabled=i == 0),
            discord.ui.Button(custom_id=f"stb:{cid}:next", label="下一题")))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items))
    return view


def card_message(card: dict[str, Any]) -> dict[str, Any]:
    """发 / 改卡的消息参数：问题卡是 V2 卡（content 必须为空），其余是文字 + 按钮。"""
    if card["kind"] == acquire_permission.KIND:
        return {"content": None, "view": ask_layout(card)}
    return {"content": card_text(card), "view": card_view(card)}


def no_taker(r: dict[str, Any]) -> str:
    """中枢交了、但没有任务接这条也没回话时给 Joy 的一句。"""
    return f"没有任务接这条消息（{'、'.join(r.get('tasks') or {}) or '没有启用的任务订阅 message'}）"


def owner_id() -> int:
    """owner = .env 的 STB_OWNER_DISCORD_ID（Joy 的 Discord 用户 ID）：只认他的私信和按钮点击。"""
    try:
        return int(env.require("STB_OWNER_DISCORD_ID"))
    except (env.EnvError, ValueError) as e:
        raise SystemExit(f"STB_OWNER_DISCORD_ID 要是 Joy 的 Discord 用户 ID（数字）：{e}") from None


DM_FILE_BYTES = 10 * 1024 * 1024     # Discord 私信单文件上限（没有服务器加成）：更大的附件只在网页上有

class Service(discord.Client):
    def __init__(self, owner: int) -> None:
        intents = discord.Intents.none()
        intents.dm_messages = True        # 私信内容不需要特权 intent（MESSAGE_CONTENT 对 DM 豁免），所以不申请它
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)       # 私信命令也注册成斜杠命令：输入 / 时菜单里能选到
        self._add_slash_commands()
        self.owner = owner
        self.progress: dict[str, discord.Message] = {}
        self.marker: dict[str, str] = {}          # react id → 当前挂着的表情
        self.presence_text = "空闲"                # 最近一次的状态栏文字：断线重连（on_ready 会再触发）后原样恢复
        self._react_lock = asyncio.Lock()         # 换表情要串行：pi 并行执行同一轮的多个工具，请求会交错
        self._dm: discord.DMChannel | None = None

    async def _save_attachments(self, message: discord.Message) -> tuple[list[str], list[str]]:
        """附件只从 Discord CDN 下载，单个 ≤ 10 MB，30 秒超时，放进本渠道的暂存目录（channels.staging）。返回 (图片, 文档) 的宿主路径：
        图片名 <消息id>-<序号><扩展名>，其他文件名 channels.doc_name；中枢入队时拷进接下它的任务（图片随提示给主模型看，音视频进任务工作目录的 in/，其余类型交隔离子代理读（D17））。
        不合规的附件跳过并回一句。"""
        images: list[str] = []
        docs: list[str] = []
        skipped: list[str] = []
        d = staging("discord")
        for i, a in enumerate(message.attachments):
            ext = IMAGE_EXT.get((a.content_type or "").split(";")[0].strip())
            doc = None if ext else doc_name(message.id, i, a.filename)
            host = (a.url.split("//", 1)[-1].split("/", 1)[0]).lower()
            if not (ext or doc) or host not in CDN_HOSTS or a.size > MAX_ATTACHMENT_BYTES:
                skipped.append(a.filename)
                continue
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
                    r = await c.get(a.url)
                if r.status_code != 200 or len(r.content) > MAX_ATTACHMENT_BYTES:
                    skipped.append(a.filename)
                    continue
            except httpx.HTTPError:
                skipped.append(a.filename)
                continue
            f = d / (f"{message.id}-{i}{ext}" if ext else str(doc))
            f.write_bytes(r.content)
            (images if ext else docs).append(str(f))
        if skipped:
            await self.send_system(f"跳过了附件 {', '.join(skipped)}：只收 Discord CDN 上 10 MB 以内的文件")
        return images, docs

    async def _message(self, ref: dict[str, Any]) -> discord.Message | None:
        try:
            ch = self.get_channel(int(ref["channel_id"])) or await self.fetch_channel(int(ref["channel_id"]))
            return await ch.fetch_message(int(ref["id"]))
        except (discord.HTTPException, KeyError, ValueError):
            return None

    async def dm(self) -> discord.DMChannel:
        if self._dm is None:
            user = await self.fetch_user(self.owner)
            self._dm = user.dm_channel or await user.create_dm()
        return self._dm

    def _add_slash_commands(self) -> None:
        """命令表（channels.commands）里的命令注册成 Discord 斜杠命令：只在和 bot 的私信里可用、只认 owner，效果同直接发「/命令 参数」这句（_slash）。
        参数按命令表的 args（每个参数一个字符串选项，有 choices 就是下拉；required 的必填）。回复作为这次交互的回复发出。"""
        for name, spec in commands().items():
            cmd = name.lstrip("/")
            if not SLASH_NAME.fullmatch(cmd):
                print(f"斜杠命令 {name} 的名字不合 Discord 的格式，没注册", flush=True)
                continue
            self.tree.add_command(self._slash_command(cmd, spec))

    def _slash_command(self, cmd: str, spec: dict[str, Any]) -> app_commands.Command:
        args = [a for a in spec.get("args") or [] if isinstance(a, dict) and SLASH_NAME.fullmatch(str(a.get("name") or ""))]

        async def callback(inter: discord.Interaction, **kw: Any) -> None:
            text = " ".join([f"/{cmd}", *(str(kw[a["name"]]) for a in args if kw.get(a["name"]))])
            await self._slash(inter, text)

        params = [inspect.Parameter("inter", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=discord.Interaction)]
        params += [inspect.Parameter(str(a["name"]), inspect.Parameter.KEYWORD_ONLY, annotation=str if a.get("required") else (str | None),
                                     default=inspect.Parameter.empty if a.get("required") else None) for a in args]
        callback.__signature__ = inspect.Signature(params)       # type: ignore[attr-defined] — discord.py 按签名生成选项
        callback.__annotations__ = {}
        command = app_commands.Command(name=cmd, description=str(spec.get("description") or cmd)[:100], callback=callback)
        for p in command._params.values():
            a = next(x for x in args if x["name"] == p.name)
            p.description = str(a.get("description") or p.name)[:100]
            p.choices = [app_commands.Choice(name=str(c["name"])[:100], value=str(c["value"])) for c in a.get("choices") or []][:25]
        command.allowed_contexts = app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False)
        return command

    async def _slash(self, inter: discord.Interaction, text: str) -> None:
        """斜杠命令：当作 Joy 发了 text 这句交中枢（publish），回话作为交互的回复。"""
        if inter.user.id != self.owner:
            await inter.response.send_message("只认 owner", ephemeral=True)
            return
        await inter.response.defer(thinking=True)
        r = await hub_client.call("publish", timeout=hub_client.PUBLISH_TIMEOUT, source=MESSAGE, channel="discord", ts=time.time(),
                                 event={"text": text, "msg_id": f"slash{inter.id}"})
        if r is None:
            reply = f"{HUB_DOWN}，命令没有执行"
        elif r.get("task"):                       # 有任务接下、入队了
            reply = f"已交给 {r.get('task')}"
        else:
            reply = str(r.get("reply") or r.get("error") or no_taker(r))
        await inter.followup.send(reply[:1900], suppress_embeds=True)

    async def setup_hook(self) -> None:
        try:
            await self.tree.sync()                      # 全局同步：改了命令重启 stbcc-discord 生效
        except discord.HTTPException as e:
            print(f"斜杠命令同步失败：{e}", flush=True)

    async def on_ready(self) -> None:
        if hello := await hub_client.call("hello"):            # 重启上线：状态栏取中枢记着的那句
            self.presence_text = str(hello.get("presence") or self.presence_text)
        await self.set_presence(self.presence_text)
        await self.dm()
        print(f"stbcc-discord 在线：{self.user}（owner {self.owner}，{len(self.guilds)} 个服务器）", flush=True)

    async def set_presence(self, text: str) -> None:
        # 自定义状态：只显示文字，不带"正在玩/观看"前缀；在线状态只对有共同服务器的用户可见
        self.presence_text = text
        await self.change_presence(activity=discord.CustomActivity(name=text), status=discord.Status.online)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None:
            return
        if self.user and message.author.id == self.user.id:
            return
        if message.author.id != self.owner or message.author.bot:
            return
        text = (message.content or "").strip()
        attachments, docs = await self._save_attachments(message) if message.attachments else ([], [])
        if not text and not attachments and not docs:
            return
        # 交中枢：交给订阅 message 的任务的 listen.py，接下的入队（附件拷进它）；命令、不接的回一句话。交完删掉暂存的附件
        try:
            r = await hub_client.call("publish", timeout=hub_client.PUBLISH_TIMEOUT, source=MESSAGE, channel="discord", ts=message.created_at.timestamp(),
                                     ref={"channel_id": str(message.channel.id), "id": str(message.id)}, images=attachments, docs=docs,
                                     event={"text": text, "msg_id": str(message.id)})
        finally:
            for f in [*attachments, *docs]:
                Path(f).unlink(missing_ok=True)
        if r is None or not r.get("ok") or not r.get("task"):        # 没有任务接下：回话、出错原因或「没有任务接」
            await self.send_system((r or {}).get("reply") or (r or {}).get("error") or (no_taker(r) if r else f"{HUB_DOWN}，这条私信没有处理"))
            return
        try:
            await message.add_reaction(QUEUED)
        except discord.HTTPException:
            pass

    async def send_system(self, text: str) -> list[discord.Message]:
        """系统通知：nonce 带 SYSTEM_NONCE 前缀。返回发出的消息。"""
        ch = await self.dm()
        sent = []
        for part in discord_out.chunk(discord_out.sanitize(text)):
            if part.strip():
                sent.append(await ch.send(part, nonce=f"{SYSTEM_NONCE}{secrets.token_hex(6)}", allowed_mentions=discord.AllowedMentions.none(),
                                            suppress_embeds=True))
        return sent

    # ---- 审批卡 ----
    async def show_card(self, card_id: str) -> None:
        """中枢建好的卡发到 DM，记下消息位置（之后按它改卡）。发出去之前已在别的入口处理了的，再改一次成现状。"""
        card = cards.load(card_id)
        if card is None:
            return
        kw = card_message(card)
        view = kw["view"]
        f = card_file(card.get("file"))
        files = {"files": [discord.File(str(f))]} if f else {}
        msg = await (await self.dm()).send(**kw, allowed_mentions=discord.AllowedMentions.none(),   # V2 卡没有正文，不设预览标志
                                            suppress_embeds=kw["content"] is not None, **files)
        view.stop()
        fresh = cards.load(card_id) or card
        fresh["message"] = {"channel_id": str(msg.channel.id), "id": str(msg.id)}
        cards.save(fresh)
        if fresh.get("resolved") != card.get("resolved") or fresh.get("payload") != card.get("payload"):
            await self._refresh_card(fresh)

    async def _refresh_card(self, card: dict[str, Any]) -> None:
        m = await self._message(card["message"]) if card.get("message") else None
        if m:
            try:
                await m.edit(**card_message(card))
            except discord.HTTPException as e:
                print(f"改卡 {card['id']} 失败：{e}", flush=True)

    async def on_interaction(self, inter: discord.Interaction) -> None:
        data = inter.data or {}
        cid = str(data.get("custom_id", ""))
        if not cid.startswith("stb:") or inter.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return
        if inter.user.id != self.owner:                           # 只认 owner；按钮 ID 不签名
            return
        parts = cid.split(":", 2)
        if len(parts) != 3:
            return
        _, card_id, action = parts
        card = cards.load(card_id)
        if card is None:
            await inter.response.send_message("这张卡已不存在", ephemeral=True)
            return
        live = not card.get("resolved") and not cards.expired(card)
        if live and inter.type == discord.InteractionType.component:     # 要先弹框的（问题卡的「其他…」、带 modal 的按钮）：弹框在这里，提交后再交中枢
            if card["kind"] == acquire_permission.KIND and action == "other":
                m = acquire_permission.other_modal(card)
                modal = discord.ui.Modal(title=m["title"][:45], custom_id=cid, timeout=None)
                modal.add_item(discord.ui.TextInput(label=m["label"][:45], custom_id="text", default=m["default"][:4000] or None,
                                                    placeholder=(m.get("placeholder") or "")[:100] or None,   # 填空题的填写提示
                                                    style=discord.TextStyle.paragraph, required=False, max_length=acquire_permission.MAX_TEXT))
                await inter.response.send_modal(modal)
                return
            btn = next((b for b in card["buttons"] if b["id"] == action), None)
            if card["kind"] != acquire_permission.KIND and btn and btn.get("modal") and card.get("modal"):
                modal = discord.ui.Modal(title=card["modal"].get("title", card["title"])[:45], custom_id=cid, timeout=None)
                for f in card["modal"]["fields"][:5]:
                    modal.add_item(discord.ui.TextInput(label=f["label"][:45], custom_id=f["id"], default=str(f.get("default", ""))[:4000],
                                                        style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short,
                                                        required=bool(f.get("secret")), max_length=4000))   # 都可以留空；密码、凭证这类 secret 字段除外
                await inter.response.send_modal(modal)
                return
        value: Any = None
        if inter.type == discord.InteractionType.modal_submit:     # 弹框的值（密码、凭证也在这里）只经本机 socket 交中枢，由中枢放内存、取一次
            fields = {c["components"][0]["custom_id"]: c["components"][0].get("value", "") for c in data.get("components", []) if c.get("components")}
            value = next(iter(fields.values()), "") if card["kind"] == acquire_permission.KIND else fields
        elif action == "select":
            value = str((data.get("values") or [(card.get("select") or {}).get("value")])[0])
        r = await hub_client.call("card_act", origin="discord", id=card_id, action=action, value=value)
        if r is None or not r.get("ok") or r.get("note"):
            await inter.response.send_message((r or {}).get("note") or (r or {}).get("error") or f"{HUB_DOWN}，稍后再点", ephemeral=True)
            return
        await inter.response.edit_message(**card_message(r["card"]))

    # ---- 本地接口 ----
    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "status":
            return {"ok": True, "ready": self.is_ready(), "latency": self.latency, "guilds": len(self.guilds)}
        if not self.is_ready():
            return {"ok": False, "error": "not ready"}
        if op == "presence":
            await self.set_presence(str(req.get("text", "空闲")))
            return {"ok": True}
        if op == "loading":                               # D32：运行器确认要切换模型：🕒 → ⏳（开跑时 progress_start 换成 🤔）
            m = await self._message(req.get("ref") or {})
            if not m:
                return {"ok": True, "id": ""}
            async with self._react_lock:
                await m.add_reaction(LOADING)
                self.progress[f"react:{m.id}"] = m
                self.marker[f"react:{m.id}"] = LOADING
                try:
                    await m.remove_reaction(QUEUED, self.user)
                except discord.HTTPException:
                    pass
            return {"ok": True, "id": f"react:{m.id}"}
        if op == "progress_start":                        # 状态栏文字由运行器统一算（含被打断的任务），这里只管表情
            ref = req.get("ref")
            if ref:                                           # 由私信触发的运行：在 Joy 的那条消息上加 🤔
                m = await self._message(ref)
                if m:
                    async with self._react_lock:
                        await m.add_reaction(THINKING)
                        self.progress[f"react:{m.id}"] = m
                        self.marker[f"react:{m.id}"] = THINKING
                        for old in (QUEUED, LOADING, ASKING):             # 轮到它了：🕒 / ⏳ / ❓（答后续跑）→ 🤔
                            try:
                                await m.remove_reaction(old, self.user)
                            except discord.HTTPException:
                                pass
                    return {"ok": True, "id": f"react:{m.id}"}
                return {"ok": True, "id": ""}
            return {"ok": True, "id": "presence"}          # 定时任务：只有状态栏，不发临时消息
        if op == "activity":                                  # 工具调用期间换表情：🤔 ↔ 🛠️ / 📡
            key = str(req.get("id"))
            m = self.progress.get(key)
            new = SUBAGENT_EMOJI.get(str(req.get("subagent") or "")) or TOOL_EMOJI.get(str(req.get("tool") or "")) or ACTIVITY.get(str(req.get("state")), THINKING)
            async with self._react_lock:
                old = self.marker.get(key, THINKING)
                if m and old != new:
                    try:
                        await m.add_reaction(new)
                        self.marker[key] = new
                        await m.remove_reaction(old, self.user)
                    except discord.HTTPException as e:
                        print(f"activity {old}->{new} 失败：{e}", flush=True)
            return {"ok": True}
        if op == "progress_end":
            ref = req.get("ref") or {}
            key = str(req.get("id") or (f"react:{ref.get('id')}" if ref else ""))   # id 为空：按 ref 认那条私信的进度记录
            # D32：没有进度记录（加载还没开始就失败、本服务重启过）但带了 ref：按它找那条私信，把 🕒 / ⏳ 换成结果
            found = await self._message(ref) if key not in self.progress and ref else None
            async with self._react_lock:
                m = self.progress.pop(key, None)
                olds = [self.marker.pop(key, THINKING)] if m else [QUEUED, LOADING]
                m = m or found
                if m:
                    try:
                        await m.add_reaction(DONE.get(str(req.get("status")), DONE["failed"]))
                        for old in olds:
                            await m.remove_reaction(old, self.user)
                    except discord.HTTPException as e:
                        print(f"progress_end {olds} 失败：{e}", flush=True)
            return {"ok": True}
        if op == "send":                                  # 系统通知
            await self.send_system(str(req.get("text", "")))
            return {"ok": True}
        if op == "reply":                                 # 模型的回复：本入口来的私信、定时任务的成品（中枢决定发给谁）
            text = str(req.get("text", ""))
            files = [p for p in (Path(str(f)) for f in req.get("host_files") or []) if not (p.is_file() and p.stat().st_size > DM_FILE_BYTES)]   # 发不出的大文件跳过，不报错（网页照常有）
            ch = await self.dm()
            ids = await asyncio.to_thread(discord_out.send, str(ch.id), text, self.token, files=files,
                                          nonce_prefix=CRON_NONCE if req.get("role") == "cron" else "")
            return {"ok": True, "result": f"已发送 {len(ids)} 条消息（{len(text)} 字符）" + (f"，附件 {len(files)} 个" if files else "")}
        if op == "card":                                  # 中枢建好的卡：发到 DM
            await self.show_card(str(req.get("id", "")))
            return {"ok": True}
        if op == "card_refresh":                          # 卡被别处处理了（网页、作废、过期）：改写私信里的卡片、按钮变灰
            card = cards.load(str(req.get("id", "")))
            if card:
                await self._refresh_card(card)
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op}"}

    async def serve_socket(self) -> None:
        path = hub.adapter_socket("discord")           # 报到：data/sockets/channels/discord.sock
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        path.unlink(missing_ok=True)

        async def conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(r.readline(), 10)
                resp = await self.handle(json.loads(line or b"{}"))
            except Exception as e:  # noqa: BLE001
                resp = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            w.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            await w.drain()
            w.close()

        srv = await asyncio.start_unix_server(conn, str(path))
        os.chmod(path, 0o600)
        async with srv:
            await srv.serve_forever()


async def main() -> None:
    ensure_dirs()
    token = env.require("STB_DISCORD_TOKEN")
    svc = Service(owner_id())
    svc.token = token                                    # reply 经 discord_out 发（REST，和 discord.py 共用同一个 bot token）
    async with svc:
        asyncio.create_task(svc.serve_socket())
        await svc.start(token)


if __name__ == "__main__":
    asyncio.run(main())
