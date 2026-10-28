"""browser：操作一次性无头浏览器的一组动作（读外部内容：只给子代理用，是 subagents/isolated-browser/ 的工具；主模型拿不到它的结果）。

本进程以 sparky 跑（联网按 tool.yaml 的 network）：容器请 stbcc-docker 代起（_browser.Container）；发卡、取密码、标记用掉的卡都经 stbcc-hub（hub.sock）的 op，不直接写平台数据。
进程只活一次子代理调用（网关在这次调用里第一次用到时起、调用结束关，control_center/gateway.py 的 _run_callee）：本进程里的状态（S）就是这一次调用的浏览器——
browser_open 第一次调用时开容器，之后各动作都在这个容器里做；进程退出（调用结束、超时、出错）时 stbcc-docker 随连接断开停掉容器，下一次子代理调用是全新的浏览器。
- 每个动作返回：第一行「第 N 步 动作 → 页面变了什么」（子代理的对话压缩成摘要时靠它）、网址 / 状态 / 标题 / 被拦下的请求、
  截图路径（工作目录 media/<运行 id>/browser-<k>/step-NN.jpg）、元素表（[编号] 标签 名称）、正文节选，另附一张标了编号的截图（交给能看图的子代理模型）。
  全文用 browser_read（一次 READ_CHARS 字，start 接着读），在全文里查用 browser_find。这些文字就是隔离核对的原文：子代理答案的引文要从这里逐字复制（control_center/subagent_run.py）。
- 每个结果带 stale（第一行 + 网址，tool_kit.result）：子代理上下文里只有最近 2 步原样留着，更早的换成它（手册 §4.4「旧结果收起」）。
- 网址核对：browser_goto 要去的网址，主域名得在这次打开过的网址、看过的页面（网址、链接、正文里的网址）里出现过（S.hosts），不然不去、说明可能记错或是编的；
  browser_open 不核对（任务书给的网址用它打开）。
- 登录档案（_browser）：browser_open 的 profile 写档案名才带 Joy 的登录状态，主页面只能去档案的域名清单里的网站；不写可去任意公网。
  每个动作做完，cookie 等有变化就写回档案（调用随时可能被结束，不等到最后）。
- 要先问 Joy 的事（两张清单在子代理的 system_prompt）：都发平台的通用审批卡（tool_kit.ask_permission，卡面一句话 + 当前一屏截图）。
  browser_submit 先发卡，Joy 同意才点那一次；不写 ref 是申请发出个人信息，批一次这次调用都算；browser_upload 也先发卡（发出信息批过就不再发）；
  browser_ask_password 发带密码框的卡，Joy 亲自输入密码，程序直接填进密码框，模型看不到（密码只交回这次调用）。
  就地等 tool_kit.APPROVAL_WAIT_S 秒（运行要暂停就提前停）；没等到：这次不提交，网关随后让出运行（卡还在等）；Joy 之后同意了，续跑时走到同一页点同一个按钮直接提交（不再发卡）。
  密码框只能经 browser_ask_password 填（容器拒绝普通输入填密码框）。
- 加域名：用登录档案时被域名清单拦下、而那个网站是同一网站登录 / 内容流程的一部分，browser_request_domain 发通用审批卡（主域名按网址算，_browser.registrable_domain）；
  Joy 同意后本进程把主域名追加进清单（_browser.add_profile_domain），关掉按旧清单开的浏览器，子代理重新 browser_open 就能去。
- 每个动作的结果在结构化信息里给 source（当前页网址）和 summary（第一行：这一步做了什么、页面变了什么）：网关记进子代理调用日志，网页按它显示每一步；
  browser_record 把这次做成的动作写成 browser-script 的脚本草稿。
"""
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _browser as bs  # noqa: E402
from control_center import tool_kit
from _browser import BrowserError, platform_fault  # noqa: E402

STUCK_WARN, STUCK_STOP = 5, 10                   # 页面连续几步一点没变：提醒换办法 / 劝它别再做
FAIL_WARN, FAIL_STOP = 3, 10                     # 连续几步动作失败（点不到、超时）：同上
LOOP_WARN, LOOP_STOP = 3, 10                     # 同一个页面状态第几次出现（绕圈）：同上
EXCERPT = 3000                                   # 动作结果里的正文节选字数（全文 browser_read）
READ_CHARS = 10_000                              # browser_read 一次给的字数（start 接着读）
FIND_HITS, FIND_AROUND = 15, 200                 # browser_find 最多列几处命中、每处前后各给几个字
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]]+")   # 正文里的网址（网址核对用：页面上出现过的域名）
STALE_NOTE = "（这一步的页面已收起，只留上面这些）"
BATCHABLE = ("click", "click_at", "type", "select", "press", "scroll", "wait", "hover", "drag")   # browser_batch 里能放的动作
CMD_KEYS = ("ref", "text", "enter", "value", "key", "dy", "url", "x", "y", "seconds", "to", "to_x", "to_y")
Out = CallToolResult                             # 每个动作的结果（tool_kit.result：内容块 + 结构化的 source = 当前页网址）
DOMAIN_HINT = "；它确实是这个已登录网站的登录或内容流程的一部分（如它的 CDN、文件存储、单点登录）就用 browser_request_domain 申请加进清单，无关的网站不要申请"

server = MCPServer("browser", instructions="操作一次性无头浏览器：打开网页、点、输入、翻页；每步返回页面变化、元素表和截图")


class Session:
    """这一次子代理调用的浏览器（进程只活一次调用）。"""

    def __init__(self) -> None:
        self.c: Any = None                       # 开着的容器（_browser.Container）；None = 还没开或已坏
        self.profile, self.domains, self.start_url = "", ["*"], ""
        self.snap: dict[str, Any] = {}           # 最近一次看到的页面
        self.steps = 0
        self.record: list[dict[str, Any]] = []   # 做成了的动作和它操作的元素（browser_record 写脚本草稿）
        self.stuck, self.last_sig, self.fails, self.empty = 0, None, 0, 0
        self.seen: Counter = Counter()
        self.send_ok = False                     # 这次调用里 Joy 已同意"发出信息"（批一次）
        self.state_hash = ""                     # 上次写回登录档案的内容（没变就不写）
        self.n = 0                               # 截图、动作记录的目录编号 browser-<n>（同一次运行里第几次开浏览器）
        self.hosts: set[str] = set()             # 这次打开过的网址、看过的页面里出现过的主域名（browser_goto 的网址核对）


S = Session()


def _dirs() -> tuple[Path | None, str]:
    """(截图目录, 给模型看的截图路径前缀，沙箱里外同一个路径)：第一次用时按这次运行 media 目录里已有的 browser-* 编号。"""
    work, run_id = os.environ.get("STB_WORK_ROOT"), os.environ.get("STB_RUN_ID")
    base = Path(work or ".") / "media" / str(run_id)
    if not S.n:
        S.n = 1 + len(list(base.glob("browser-*"))) if base.is_dir() else 1
    name = f"browser-{S.n}"
    return (base / name if work and run_id else None), str(base / name)


def _need() -> Any:
    if S.c is None:
        raise BrowserError("浏览器还没打开：先 browser_open(url, profile?)")
    return S.c


async def _send(cmd: dict[str, Any], timeout: float = 90) -> dict[str, Any]:
    """发一条命令；容器坏了（平台故障）就记成没开，下次 browser_open 重开。"""
    try:
        return await _need().send(cmd, timeout)
    except ToolError:
        S.c = None
        raise


def _elements(snap: dict[str, Any]) -> str:
    lines = []
    for e in snap.get("elements") or []:
        extra = "".join(f" {k}={e[k]!r}" for k in ("type", "value", "selected", "href") if e.get(k) not in (None, ""))
        extra += {True: " ✓已勾选", False: " ☐未勾选"}.get(e.get("checked"), "")
        lines.append(f"[{e['ref']}] {e['tag']} {e.get('name', '')!r}{extra}{' （iframe 里）' if e.get('frame') else ''}{' （屏幕外）' if e.get('offscreen') else ''}")
    return "\n".join(lines) or "（没有可交互元素）"


def said(word: str, name: str) -> bool:
    """name 里有没有 word：英文按整词（State 不算在 Statement 里），中文按子串。"""
    w, n = word.strip().lower(), name.lower()
    if not w:
        return False
    if w.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", n) is not None
    return w in n


def target_mismatch(act: dict[str, Any], snap: dict[str, Any]) -> str | None:
    """模型说要点「Done」、给的编号却是别的元素（想的和写的对不上）：名字对不上、而别的元素名字对得上时拦下，报出对得上的编号。"""
    tgt, ref = str(act.get("target") or "").strip(), str(act.get("ref") or "")
    if not tgt or not ref:
        return None
    els = snap.get("elements") or []
    el = next((e for e in els if e.get("ref") == ref), None)
    name = str((el or {}).get("name") or "")
    if el is None or (name and (said(tgt, name) or said(name, tgt))):
        return None                                  # 编号不存在交给浏览器报错；名字对得上就放行
    hits = [e for e in els if said(tgt, str(e.get("name") or ""))]
    if not hits:
        return None                                  # 页面上没有叫这个的（图标按钮等）：核对不了，放行
    return (f"没执行：你说要操作「{tgt[:40]}」，但 {ref} 号是 {el.get('tag')} {name[:40] or '（没有名字）'!r}；名字对得上的是 "
            + "、".join(f"[{e['ref']}] {e.get('tag')} {str(e.get('name', ''))[:30]!r}" for e in hits[:5]) + "。确认编号再做")


def fix_ref(v: Any) -> str:
    """模型常把编号写成 [10]、#10 或数字：取出数字；取不出就原样交给浏览器报错。"""
    s = str(v)
    m = re.search(r"\d+", s)
    return s if s.isdigit() else (m.group() if m else s)


def _js(s: Any) -> str:
    return json.dumps(str(s), ensure_ascii=False)


def locator(el: dict[str, Any] | None) -> str | None:
    """元素 → 稳定的 Playwright 定位（不用会变的编号）：label → placeholder → role + 名字 → id → 文字。"""
    if not el:
        return None
    tag, typ, name = el.get("tag", ""), el.get("type", ""), str(el.get("name") or "")
    base = "page"
    if el.get("label"):
        return f"{base}.getByLabel({_js(el['label'])})"
    if el.get("ph"):
        return f"{base}.getByPlaceholder({_js(el['ph'])})"
    role = el.get("role") or {"a": "link", "button": "button", "select": "combobox", "textarea": "textbox"}.get(tag) or (
        {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button"}.get(typ, "textbox") if tag == "input" else None)
    if role and name:
        return f"{base}.getByRole({_js(role)}, {{ name: {_js(name[:60])}, exact: true }})"
    if el.get("id"):
        return f"{base}.locator({_js('#' + el['id'])})"
    if name:
        return f"{base}.getByText({_js(name[:60])}, {{ exact: true }})"
    return None


def script_draft(start_url: str, steps: list[dict[str, Any]]) -> str:
    """把这次浏览做成的动作写成 browser-script 的脚本草稿：提交包进 final()，密码不写，定位不出来的标 TODO。"""
    lines = ["// 浏览器子代理录制的草稿：检查每一步、补上等待和成功判据，再用 browser_script_run mode=dry 演练",
             f"export const start_url = {_js(start_url)};", "", "export async function run({ page, final, log }) {"]
    for st in steps:
        a, el, loc = st.get("a"), st.get("el"), locator(st.get("el"))
        frame = "  // 这个元素在 iframe 里：要用 page.frameLocator(...) 定位\n" if el and el.get("frame") else ""
        miss = f"  // TODO：定位不出来（{(el or {}).get('tag', '?')} {str((el or {}).get('name', ''))[:30]!r}），看截图换一种写法\n"
        if a in ("click", "hover", "select", "type", "submit", "drag") and not loc:
            lines.append(miss.rstrip("\n"))
            continue
        if a == "click":
            lines.append(f"{frame}  await {loc}.first().click();")
        elif a == "hover":
            lines.append(f"{frame}  await {loc}.first().hover();")
        elif a == "type" and st.get("secret"):
            lines.append("  // TODO：这里是密码 / PIN，不写进脚本；用登录档案保持登录，或到时找 Joy")
        elif a == "type":
            lines.append(f"{frame}  await {loc}.first().fill({_js(st.get('text', ''))});")
        elif a == "select":
            lines.append(f"{frame}  await {loc}.first().selectOption({_js(st.get('value', ''))});")
        elif a == "drag" and locator(st.get("to_el")):
            lines.append(f"  await {loc}.first().dragTo({locator(st.get('to_el'))}.first());")
        elif a == "press":
            lines.append(f"  await page.keyboard.press({_js(st.get('key', 'Enter'))});")
        elif a == "goto":
            lines.append(f"  await page.goto({_js(st.get('url', ''))});")
        elif a == "wait" and st.get("text"):
            lines.append(f"  await page.getByText({_js(st['text'])}).first().waitFor();")
        elif a == "click_at":
            lines.append(f"  await page.mouse.click({st.get('x', 0)}, {st.get('y', 0)});   // TODO：按坐标点的，最好换成定位")
        elif a == "submit":
            lines += ["  await final(async () => {", f"    await {loc}.first().click();",
                      "    // TODO：等提交成功后出现的文字，例如 await page.getByText(\"…\").waitFor({ timeout: 5000 });",
                      "    return true;", "  });"]
    lines += ["}", ""]
    return "\n".join(lines)


def form_state(snap: dict[str, Any]) -> str:
    """页面上输入框、下拉框、勾选框当前的状态（程序读的，正文里没有）：接在 browser_read 的正文后面。"""
    lines = []
    for e in snap.get("elements") or []:
        if "checked" in e:
            lines.append(f"{e.get('type') or e.get('tag')} {str(e.get('name', ''))[:60]!r}：{'已勾选' if e['checked'] else '未勾选'}")
        elif "selected" in e:
            lines.append(f"下拉框 {str(e.get('name', ''))[:40]!r}：选中 {e['selected']!r}")
        elif "value" in e and e.get("type") != "password" and (e.get("name") or e["value"]):   # 没名字又空着的框说明不了什么，只会干扰
            lines.append(f"{e.get('type') or e.get('tag')} {str(e.get('name', ''))[:60]!r}：值 {str(e['value'])[:80]!r}")
    return ("\n\n[表单当前状态（程序读取）]\n" + "\n".join(lines)) if lines else ""


def page_text(snap: dict[str, Any]) -> str:
    """一页的全文（browser_read）：页面标题（<title>）与各级标题（h1–h3）写在正文前面（正文 innerText 里看不出哪行是 h1），后面接表单当前状态。"""
    head = f"页面标题（title）：{snap.get('title')}\n" if str(snap.get("title") or "").strip() else ""
    hs = [h for h in snap.get("headings") or [] if isinstance(h, dict) and str(h.get("text") or "").strip()]
    if hs:
        head += "页面里的标题：\n" + "\n".join(f"{h.get('tag', 'h')}：{str(h['text']).strip()[:200]}" for h in hs[:40]) + "\n"
    return (head + "\n" if head else "") + str(snap.get("text", "")) + form_state(snap)


def changes(before: dict[str, Any], after: dict[str, Any]) -> str:
    """这一步前后页面变了什么（一行）：网址、输入框的值、勾选状态、新出现 / 消失的元素、正文新出现的行。编号每次重排，按 标签+名称 对齐。"""
    def fields(snap: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
        seen: Counter = Counter()
        got = {}
        for e in snap.get("elements") or []:
            if "value" in e or "checked" in e:
                k = (e.get("tag"), e.get("type"), e.get("name"))
                seen[k] += 1
                got[(*k, seen[k])] = e
        return got

    def label(e: dict[str, Any]) -> str:
        return f"{e.get('tag')} {str(e.get('name', ''))[:40]!r}"

    out = []
    if before.get("url") != after.get("url"):
        out.append(f"网址变成 {str(after.get('url', ''))[:120]}")
    fb, fa = fields(before), fields(after)
    for k, e in fa.items():
        o = fb.get(k)
        if o is None:
            continue
        if o.get("selected") != e.get("selected"):
            out.append(f"{label(e)} 选中 {str(o.get('selected', ''))[:40]!r} → {str(e.get('selected', ''))[:40]!r}")
        elif o.get("value") != e.get("value"):
            out.append(f"{label(e)} 的值 {str(o.get('value', ''))[:40]!r} → {str(e.get('value', ''))[:40]!r}")
        if o.get("checked") != e.get("checked"):
            out.append(f"{label(e)} {'已勾选' if e.get('checked') else '取消勾选'}")
    cb = Counter(label(e) for e in before.get("elements") or [])
    ca = Counter(label(e) for e in after.get("elements") or [])
    new, gone = ca - cb, cb - ca
    if new:
        out.append("新出现：" + "、".join(f"{k}×{n}" if n > 1 else k for k, n in list(new.items())[:5]) + ("…" if len(new) > 5 else ""))
    if gone:
        out.append("消失：" + "、".join(f"{k}×{n}" if n > 1 else k for k, n in list(gone.items())[:5]) + ("…" if len(gone) > 5 else ""))
    old_lines = set(str(before.get("text", "")).splitlines())
    lines = [ln.strip()[:80] for ln in str(after.get("text", "")).splitlines() if ln.strip() and ln not in old_lines]
    if lines:
        out.append("正文新出现：" + " / ".join(lines[:3]) + ("…" if len(lines) > 3 else ""))
    if not out and [label(e) for e in before.get("elements") or []] != [label(e) for e in after.get("elements") or []]:
        out.append("元素顺序变了，现在是：" + "、".join(label(e) for e in (after.get("elements") or [])[:6]))   # 拖动排序、交换位置
    elif not out and before.get("text") != after.get("text"):
        out.append("正文有变化（内容一样、顺序或位置变了）")
    return "；".join(out) or "页面没有任何变化"


def keep(snap: dict[str, Any]) -> str | None:
    """记下新看到的页面：卡住 / 绕圈 / 空页的计数，截图存成 step-NN.jpg；返回截图的路径（没截图 None）。"""
    sig = hash((snap.get("url"), snap.get("text"), json.dumps(snap.get("elements"), sort_keys=True, ensure_ascii=False)))
    S.stuck, S.last_sig = (S.stuck + 1 if sig == S.last_sig else 0), sig
    if S.stuck == 0:                                 # 进入这个状态才算一次（原地不动归 stuck 管）
        S.seen[sig] += 1
    S.empty = S.empty + 1 if not str(snap.get("text", "")).strip() and not snap.get("elements") else 0
    snap["empty_streak"] = S.empty
    S.snap = snap
    saw(snap)
    shots, rel = _dirs()
    if shots is None or not snap.get("screenshot"):
        return None
    shots.mkdir(parents=True, exist_ok=True)
    name = f"step-{S.steps:02d}.jpg"
    (shots / name).write_bytes(base64.b64decode(snap["screenshot"]))
    return f"{rel}/{name}"


def _domain(url: str) -> str:
    """网址或主机名的主域名（files.canvas.uci.edu → uci.edu）；IP、localhost 等算不出主域名的用主机名本身。"""
    try:
        return bs.registrable_domain(url)
    except ValueError:
        return (urlsplit(url if "://" in url else f"http://{url}").hostname or "").lower()


def saw(snap: dict[str, Any]) -> None:
    """记下这一页出现过的主域名：页面网址、链接（相对链接按页面网址补全）、正文里的网址。"""
    base = str(snap.get("url") or "")
    urls = [base, *(urljoin(base, str(e["href"])) for e in snap.get("elements") or [] if e.get("href")), *URL_RE.findall(str(snap.get("text") or ""))]
    S.hosts |= {d for u in urls if u and (d := _domain(u))}


def _src() -> str | None:
    """这一步结果的来源：浏览器当前页的网址（还没打开页面为 None）。"""
    return str(S.snap.get("url") or "") or None


def _say(text: str) -> Out:
    """只有一段文字的结果（失败、被核对拦下、在等 Joy…）：来源同样是当前页网址，摘要是第一行。"""
    return tool_kit.result(text, source=_src(), summary=text.splitlines()[0][:200] if text else None)


def observe(head: str, shot: str | None) -> Out:
    """动作的结果：第一行 head（这一步做了什么、页面变了什么），然后是当前页（网址、状态、标题、截图路径、元素表、正文节选）和标了编号的截图。"""
    s = S.snap
    text = str(s.get("text") or "")
    blocked = "；".join(f"{b.get('kind')} {b.get('method', '')} {b.get('url', '')}".strip() for b in s.get("blocked") or []) or "无"
    lines = [head, f"网址：{s.get('url', '')}", f"HTTP 状态：{s.get('status') or '未知'}"]
    if s.get("nav_error"):
        lines.append(f"打开网址出错：{s['nav_error']}（常见原因：域名不存在、网址写错了，或网站连不上；先核对网址，别当成被拦截）")
    if s.get("empty_streak"):
        lines.append(f"页面是空的（没有正文也没有可交互元素），已连续 {s['empty_streak']} 次为空：还在加载就 browser_wait，加载出错就 browser_goto 重开或 browser_back")
    lines += [f"标题：{s.get('title', '')}", f"刚才被拦下的请求：{blocked}"]
    if S.profile and any(b.get("kind") == "nav" for b in s.get("blocked") or []):
        lines.append(f"（nav 是不在登录档案 {S.profile} 域名清单里的网址{DOMAIN_HINT}）")
    if shot:
        lines.append(f"截图：{shot}")
    lines += ["### 可交互元素（[编号] 标签 名称；截图上红框左上角的数字就是编号）", _elements(s),
              f"### 正文节选（前 {EXCERPT} 字" + (f"，共 {len(text)} 字，全文用 browser_read）" if len(text) > EXCERPT else "）"), text[:EXCERPT]]
    out: list[TextContent | ImageContent] = [TextContent(type="text", text="\n".join(lines))]
    pic = s.get("marked") or s.get("screenshot")      # 标了编号的那张：红框 + 左上角的数字就是元素表里的编号
    if pic:
        out.append(ImageContent(type="image", data=pic, mimeType="image/jpeg"))
    return tool_kit.result(out, source=_src(), summary=head[:200],     # 摘要：这一步做了什么、页面变了什么（网页 / 日志显示子代理过程用）
                           stale=f"{head[:400]}\n网址：{s.get('url', '')}\n{STALE_NOTE}")


def remember(x: dict[str, Any], on: dict[str, Any], secret: bool = False) -> None:
    """做成了的动作记进 S.record（用动作之前那一屏的元素，编号对得上）：browser_record 写脚本草稿用。"""
    els = {e.get("ref"): e for e in on.get("elements") or []}
    step = {"a": str(x.get("action") or x.get("op") or ""), **{k: x[k] for k in ("text", "value", "key", "url", "x", "y", "seconds", "dy") if k in x},
            "el": els.get(str(x.get("ref", ""))), "to_el": els.get(str(x.get("to", ""))), "page": on.get("url")}
    if secret:
        step.pop("text", None)
        step["secret"] = True
    S.record.append(step)


async def save_state() -> None:
    """用登录档案时：cookie 等有变化就写回档案（每步之后都查：子代理调用随时可能被结束）。"""
    if not S.profile or S.c is None:
        return
    st = await _send({"op": "state"})
    if st.get("ok") and st.get("storage_state"):
        h = json.dumps(st["storage_state"], sort_keys=True)
        if h != S.state_hash:
            bs.save_profile(S.profile, st["storage_state"])
            S.state_hash = h


def _label(action: str, **kw: Any) -> str:
    S.steps += 1
    args = " ".join(f"{k}={str(v)[:60]!r}" for k, v in kw.items() if v not in (None, "", False))
    return f"第 {S.steps} 步 {action}" + (f" {args}" if args else "")


def _warn() -> str:
    """卡住、绕圈、连续失败的提醒（接在这一步结果后面）。"""
    if S.stuck >= STUCK_WARN:
        return (f"（页面已经 {S.stuck} 步没有变化：别再重复，换个办法：看看别的动作（browser_scroll 指定区域、browser_hover、browser_wait、browser_click_at 按坐标）能不能用"
                + ("；再试也没用了，别再操作这个页面，交回结果）" if S.stuck >= STUCK_STOP else "）"))
    if S.seen[S.last_sig] >= LOOP_WARN:
        return (f"（这个页面状态已经第 {S.seen[S.last_sig]} 次出现：你在绕圈。回头看前几步的结果，已经做成的别再做，接着做下一件事"
                + ("；绕不出来就交回结果）" if S.seen[S.last_sig] >= LOOP_STOP else "）"))
    return ""


async def _act(label: str, cmd: dict[str, Any], *, submit: bool = False) -> Out:
    """在浏览器里做一个动作（或一串 batch）：成功就记录、比对前后页面、写回登录档案，返回新页面；失败返回原因（页面没变，不另附页面）。"""
    before = S.snap
    res = await _send(cmd, timeout=240 if cmd["op"] == "batch" else 90)
    ok = bool(res.get("ok"))
    if ok:
        if submit:
            remember({"action": "submit", "ref": cmd["ref"]}, before)
        else:
            done = int(res.get("batch_done") or 0) if cmd["op"] == "batch" else 1
            for x in (cmd["cmds"][:done] if cmd["op"] == "batch" else [cmd]):
                remember(x, before, secret=bool(x.get("allow_password")))
    if cmd["op"] == "batch" and ok:
        label += f"（一串 {len(cmd['cmds'])} 个做了 {res.get('batch_done')} 个" + (f"，{res['batch_error']}" if res.get("batch_error") else "") + "）"
    S.fails = 0 if ok else S.fails + 1
    if ok and "url" in res:
        shot = keep(res)
        now = next((e for e in res.get("elements") or [] if e.get("ref") == cmd.get("ref")), {}) if cmd["op"] == "type" and not cmd.get("allow_password") else {}
        miss = (f"；注意：输入框现在的值是 {str(now['value'])[:40]!r}，没填进去（可能被别的东西挡住或被页面清掉了；框在一个带滚动条的小区域里时，先 browser_scroll(ref=这个框, dy=100) 把它滚出来再填）"
                if "value" in now and now.get("type") != "password" and now["value"] != str(cmd.get("text", "")) else "")
        head = f"{label} → {str(res.get('title', ''))[:60]!r}；{changes(before, res)}{miss}{_warn()}"
        await save_state()
        return observe(head, shot)
    head = f"{label} → " + ("做了，页面没有回快照" if ok else f"失败：{str(res.get('error', ''))[:200]}") + (
        f"（已经连续 {S.fails} 步失败：别再做同样的动作，换个办法，比如先 browser_wait 等页面加载、看截图换编号或 browser_click_at"
        + ("；再试也没用了，交回结果）" if S.fails >= FAIL_STOP else "）") if S.fails >= FAIL_WARN else "")
    return _say(head)


def _checked(act: dict[str, Any]) -> str | None:
    """带 target 的动作核对编号（target_mismatch）：对不上返回说明（这步不做，算一次失败）。"""
    wrong = target_mismatch(act, S.snap)
    if wrong:
        S.fails += 1
    return wrong


# ---------- 审批卡（平台的通用审批卡，tool_kit.ask_permission：卡面文字、附图、密码框都由这里给） ----------
SEND_INFO = "（发出信息）"           # 不写 ref 的 submit（申请发出个人信息）卡上写这个


def card_shot(prefix: str = "submit") -> str | None:
    """审批卡附当前这一屏（提交、上传、登录共用）：存成 <prefix>-NN.jpg，返回宿主路径（卡片按它附图）。"""
    shots, _ = _dirs()
    if shots is None or not S.snap.get("screenshot"):
        return None
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / f"{prefix}-{S.steps:02d}.jpg"
    shot.write_bytes(base64.b64decode(S.snap["screenshot"]))
    return str(shot)


async def ask_approval(ref: str, summary: str) -> bool | str | None:
    """发审批卡并就地等 Joy 点（最多 tool_kit.APPROVAL_WAIT_S 秒，运行要暂停就提前停）；同意 True，没处理 None（卡还在等），
    「拒绝，你应该…」返回 Joy 写的话（str），卡发不出去 False。卡上一句话 + 当前一屏截图。
    同一次运行里 Joy 之前已经同意过同一页、同一个按钮的卡（挂起后续跑）：直接放行，不再发卡（卡的 key 是网址 + 按钮名）。"""
    el = next((e for e in S.snap.get("elements") or [] if e.get("ref") == ref), {})
    url = str(S.snap.get("url", ""))[:500]
    button = str(el.get("name") or el.get("tag") or ref)[:80] if ref else SEND_INFO
    host = urlsplit(url).hostname or url[:60]
    what = "发出信息" if button == SEND_INFO else f"点「{button}」提交"
    key = hashlib.sha256(f"{url}\0{button}".encode()).hexdigest()
    got, value = await tool_kit.ask_permission("browser", f"在 {host} {what}：{summary[:200] or '（没写要提交什么）'}——看截图，没问题就同意", key,
                                              f"网址：{url}", image=card_shot())
    return True if got == "approve" else None if got == "pending" else False if got == "failed" else (value or False)


async def ask_secret(summary: str) -> str | None:
    """要密码：通用审批卡带密码框（一句话 + 截图 +「输入密码」/「拒绝」）。就地等：返回密码（只在内存里）、""（拒绝或发不出卡）或 None（没等到）。
    密码只在 stbcc-hub 的内存里：经中枢的 secret op 取一次（只给发卡的这次运行），不落盘；卡片记录、处理结果、日志里都没有它。"""
    host = urlsplit(str(S.snap.get("url", ""))).hostname or "这个网站"
    got, value = await tool_kit.ask_permission("browser", f"在 {host} 登录：{summary[:200] or '需要密码'}", secrets.token_hex(8),
                                              image=card_shot("login"), password=f"{host} 的密码"[:45])
    return (value or "") if got == "approve" else None if got == "pending" else ""


# ---------- 函数 ----------
@server.tool(name="browser_open", description=(
    "打开网页（第一次调用时开浏览器）。profile 写登录档案名才带 Joy 的登录状态，这时只能去档案域名清单里的网站；不写是全新的无登录浏览器，可去任意公网。"
    "档案相同就在当前浏览器里打开；换档案会关掉当前的、开新的"))
async def browser_open(url: str, profile: str = "") -> Out:
    profile = profile.strip()
    domains = bs.nav_domains(profile)
    if not bs.host_allowed(url, domains):
        raise BrowserError(f"网址的域名不在允许范围（{urlsplit(url).hostname or url[:100]} 不在 {domains} 里"
                           f"{f'，这是登录档案 {profile} 允许的域名' if profile else ''}；只能是 http / https 网址）；换成允许域名里的网址，或换用对应的登录档案"
                           f"{DOMAIN_HINT if profile else ''}")
    S.hosts.add(_domain(url))
    if S.c is not None and S.profile == profile:
        return await _goto(url)
    if S.c is not None:                               # 换档案：当前的关掉（登录档案每步之后已写回）
        c, S.c = S.c, None
        await c.__aexit__(None, None, None)
    S.c = await bs.Container().__aenter__()
    first = not S.start_url
    S.profile, S.domains, S.start_url, S.state_hash = profile, domains, S.start_url or url, ""
    label = _label("open", url=url, profile=profile)
    snap = await _send({"op": "start", "start_url": url, "nav_domains": domains, "storage_state": bs.load_profile(profile)})
    if not snap.get("ok"):
        S.c = None
        raise platform_fault(f"浏览器没起来：{snap.get('error', '')}")
    shot = keep(snap)
    if not first:                                     # 换档案重开：脚本草稿里记成 goto（第一次打开的网址是草稿的 start_url）
        S.record.append({"a": "goto", "url": url, "el": None, "to_el": None, "page": url})
    head = f"{label} → {str(snap.get('title', ''))[:60]!r}"
    await save_state()
    return observe(head, shot)


@server.tool(name="browser_goto", description=(
    "在当前浏览器里打开网址（只能去允许的域名）：网址要来自任务书或这次看过的页面（链接、正文里写着的），域名没出现过的不去；任务书给的网址用 browser_open"))
async def browser_goto(url: str) -> Out:
    _need()
    if not bs.host_allowed(url, S.domains):
        host = urlsplit(url).hostname or url[:100]
        raise BrowserError(f"拒绝：域名不在允许范围（{host} 不在允许的域名里：{', '.join(S.domains)}；只能是 http / https 网址）。"
                           "改法：只在这些域名里导航；确实要去别的网站就交回结果，写明需要的网址" + (DOMAIN_HINT if S.profile else ""))
    if _domain(url) not in S.hosts:
        host = urlsplit(url).hostname or url[:100]
        return _say(f"没去 {url[:200]}：{host} 没在这次打开过的网址和看过的页面（网址、链接、正文里的网址）里出现过，网址可能记错了或是编的。"
                    "要去页面上的链接就点那个链接；任务书里给的网址用 browser_open 打开")
    return await _goto(url)


async def _goto(url: str) -> Out:
    return await _act(_label("goto", url=url), {"op": "goto", "url": url})


@server.tool(name="browser_back", description="后退一页")
async def browser_back() -> Out:
    return await _act(_label("back"), {"op": "back"})


@server.tool(name="browser_click", description=(
    "点元素：ref 是元素表里的编号；target 写它在页面上叫什么（按钮上的字），程序核对编号，对不上就不点、告诉你对得上的编号。"
    "会改变网站数据或把东西发给别人的按钮（提交、发送、报名、付款、删除…）用 browser_submit"))
async def browser_click(ref: str | int, target: str = "") -> Out:
    act = {"action": "click", "ref": fix_ref(ref), "target": target}
    if wrong := _checked(act):
        return _say(wrong)
    return await _act(_label("click", ref=act["ref"], target=target), {"op": "click", "ref": act["ref"]})


@server.tool(name="browser_click_at", description="按截图上的坐标点（截图 1280×860，左上角 0,0）：要点的东西在截图里看得到、元素表里却找不到时用")
async def browser_click_at(x: int, y: int) -> Out:
    return await _act(_label("click_at", x=x, y=y), {"op": "click_at", "x": x, "y": y})


@server.tool(name="browser_type", description=(
    "往输入框填字（enter=true 顺带回车）；target 同 browser_click。密码框填不了：密码用 browser_ask_password"))
async def browser_type(ref: str | int, text: str, enter: bool = False, target: str = "") -> Out:
    act = {"action": "type", "ref": fix_ref(ref), "target": target}
    if wrong := _checked(act):
        return _say(wrong)
    return await _act(_label("type", ref=act["ref"], text=text, enter=enter, target=target), {"op": "type", "ref": act["ref"], "text": text, "enter": enter})


@server.tool(name="browser_select", description="下拉框选一项（value 写选项的文字或值）；target 同 browser_click")
async def browser_select(ref: str | int, value: str, target: str = "") -> Out:
    act = {"action": "select", "ref": fix_ref(ref), "target": target}
    if wrong := _checked(act):
        return _say(wrong)
    return await _act(_label("select", ref=act["ref"], value=value, target=target), {"op": "select", "ref": act["ref"], "value": value})


@server.tool(name="browser_press", description="按键（如 Enter、Escape、Tab、ArrowDown）。在填好的表单里按回车等于提交：那种情况用 browser_submit")
async def browser_press(key: str) -> Out:
    return await _act(_label("press", key=key), {"op": "press", "key": key})


@server.tool(name="browser_scroll", description="滚动（dy 正数往下，约 700 一屏）；写 ref 就在那个元素所在的区域里滚（自带滚动条的小框、弹窗、下拉列表），不写滚整页")
async def browser_scroll(dy: int = 700, ref: str | int = "") -> Out:
    cmd: dict[str, Any] = {"op": "scroll", "dy": dy, **({"ref": fix_ref(ref)} if str(ref).strip() else {})}
    return await _act(_label("scroll", dy=dy, ref=cmd.get("ref")), cmd)


@server.tool(name="browser_wait", description=(
    "等页面自己变：写 text 就等到页面上新出现这段文字（原本就有的不算，最多 60 秒，默认最多等 30 秒），不写就等 seconds 秒（默认 5）。"
    "加载中、进度条、「Just a moment…」这类页面用它；等到之后要马上做的（到 75% 立刻点 Stop）放进 browser_batch 一串做"))
async def browser_wait(text: str = "", seconds: int = 0) -> Out:
    cmd: dict[str, Any] = {"op": "wait", **({"text": text} if text else {}), **({"seconds": seconds} if seconds else {})}
    return await _act(_label("wait", text=text, seconds=seconds), cmd)


@server.tool(name="browser_hover", description="把鼠标移上去（鼠标移上去才出现的菜单、说明）：写 ref（元素编号）或 x、y（截图坐标）")
async def browser_hover(ref: str | int = "", x: int | None = None, y: int | None = None, target: str = "") -> Out:
    act = {"action": "hover", "ref": fix_ref(ref) if str(ref).strip() else "", "target": target}
    if act["ref"] and (wrong := _checked(act)):
        return _say(wrong)
    cmd: dict[str, Any] = {"op": "hover", **({"ref": act["ref"]} if act["ref"] else {}), **({"x": x, "y": y} if x is not None and y is not None else {})}
    return await _act(_label("hover", ref=act["ref"], x=x, y=y), cmd)


@server.tool(name="browser_drag", description="拖动：把元素 ref 拖到元素 to 上松开；或从 ref（或 x、y）拖到截图坐标 to_x、to_y（滑块、排序、地图）")
async def browser_drag(ref: str | int = "", to: str | int = "", x: int | None = None, y: int | None = None,
                       to_x: int | None = None, to_y: int | None = None) -> Out:
    cmd: dict[str, Any] = {"op": "drag", **({"ref": fix_ref(ref)} if str(ref).strip() else {}), **({"to": fix_ref(to)} if str(to).strip() else {}),
                           **{k: v for k, v in (("x", x), ("y", y), ("to_x", to_x), ("to_y", to_y)) if v is not None}}
    return await _act(_label("drag", ref=cmd.get("ref"), to=cmd.get("to"), x=x, y=y, to_x=to_x, to_y=to_y), cmd)


@server.tool(name="browser_batch", description=(
    "接下来几步不用看结果就知道怎么做时一串做（连着填好几个输入框、点 Start 后等到 75% 再点 Stop）：steps 是动作列表，每项 {action, …参数}，"
    "action 只能是 click / click_at / type / select / press / scroll / wait / hover / drag，参数同对应的函数（编号用这一次元素表里的）。"
    "中途有一个失败或页面跳到别的网址，后面的就不做，结果写做到了第几个"))
async def browser_batch(steps: list[dict[str, Any]]) -> Out:
    _need()
    if not steps or not all(isinstance(x, dict) for x in steps):
        raise BrowserError("steps 要是动作列表：[{\"action\": \"type\", \"ref\": \"3\", \"text\": \"…\"}, {\"action\": \"click\", \"ref\": \"5\"}]")
    if bad := [str(x.get("action")) for x in steps if x.get("action") not in BATCHABLE]:
        raise BrowserError(f"steps 里只能放 {', '.join(BATCHABLE)}（有 {', '.join(bad)}）；提交、上传、密码要单独用对应的函数")
    for x in steps:
        for k in ("ref", "to"):
            if x.get(k) is not None:
                x[k] = fix_ref(x[k])
        if wrong := _checked(x):
            return _say(wrong)
    cmds = [{"op": str(x["action"]), **{k: x[k] for k in CMD_KEYS if k in x}} for x in steps]
    label = _label("batch") + " " + "、".join(f"{x['action']}" + (f" {x.get('ref')}" if x.get("ref") else "") for x in steps)
    return await _act(label, {"op": "batch", "cmds": cmds})


async def _snapshot() -> dict[str, Any]:
    """当前页的最新快照（不算一步、不存截图）：只更新当前页、记下出现过的域名。"""
    snap = await _send({"op": "snapshot"})
    if not snap.get("ok"):
        raise BrowserError(f"读不了当前页：{str(snap.get('error', ''))[:200]}")
    S.snap = {**snap, "empty_streak": S.empty}
    saw(snap)
    return snap


@server.tool(name="browser_read", description=(
    f"读当前页的全文（页面标题、各级标题、正文、表单当前状态），一次 {READ_CHARS} 字，start 写从第几个字接着读：动作结果里只有正文节选，答案要的原文不在节选里就用它；"
    "长页面里找某个词用 browser_find"))
async def browser_read(start: int = 0) -> Out:
    snap = await _snapshot()
    full, url = page_text(snap), str(snap.get("url", ""))
    start = max(0, min(int(start), len(full)))
    end = min(len(full), start + READ_CHARS)
    where = f"第 {start + 1}–{end} 字，共 {len(full)} 字"
    more = f"\n\n（{where}；接着读 browser_read(start={end})）" if end < len(full) else (f"\n\n（{where}，读完了）" if start else "")
    text = f"当前页全文{f'（{where}）' if start or end < len(full) else ''}\n网址：{url}\n{full[start:end]}{more}"
    return tool_kit.result(text, source=_src(), summary=f"读全文 {where}", stale=f"读了当前页全文（{where}）\n网址：{url}\n{STALE_NOTE}")


@server.tool(name="browser_find", description=(
    "在当前页全文里查一个词或一句话（不分大小写）：回每处命中和前后文、在全文里的位置。长页面里找东西用它，别一屏屏滚动；"
    "要看更多前后文用 browser_read(start=位置)"))
async def browser_find(text: str) -> Out:
    q = str(text).strip()
    if not q:
        raise BrowserError("text 写要查的词或一句话")
    snap = await _snapshot()
    full, url = page_text(snap), str(snap.get("url", ""))
    low, ql = full.lower(), q.lower()
    hits, i = [], low.find(ql)
    while i >= 0:
        hits.append(i)
        i = low.find(ql, i + len(ql))
    if not hits:
        body = f"当前页没有「{q}」（全文 {len(full)} 字）\n网址：{url}"
    else:
        body = "\n".join([f"在当前页查「{q}」：共 {len(hits)} 处" + (f"，只列前 {FIND_HITS} 处（换更具体的词）" if len(hits) > FIND_HITS else ""), f"网址：{url}"]
                         + [f"--- 第 {p + 1} 字处\n{full[max(0, p - FIND_AROUND):p + len(q) + FIND_AROUND]}" for p in hits[:FIND_HITS]])
    return tool_kit.result(body, source=_src(), summary=f"查 {q[:60]!r}：{len(hits)} 处",
                           stale=f"在当前页查了「{q[:100]}」：{len(hits)} 处\n网址：{url}\n{STALE_NOTE}")


@server.tool(name="browser_submit", description=(
    "点「要先问 Joy」那类按钮（会改变网站数据或把东西发给别人，见你的行为规则的两张清单）：Joy 批准才点那一次；summary 一句话写清要提交什么。"
    "不能用 browser_click 或在填好的表单里按回车代替。"
    "不写 ref：往网站发 Joy 的个人信息前先申请一次，summary 列出要发的全部信息；批准后这次调用里可以填写、发送，最后的提交按钮仍要 browser_submit(ref) 再批一次"))
async def browser_submit(summary: str, ref: str | int = "", target: str = "") -> Out:
    _need()
    ref = fix_ref(ref) if str(ref).strip() else ""
    if not ref and S.send_ok:
        return _say("这次已获准发出信息，不用再申请，直接填写、发送")
    if ref and (wrong := _checked({"action": "submit", "ref": ref, "target": target})):
        return _say(wrong)
    ok = await ask_approval(ref, summary)
    label = _label("submit", ref=ref, summary=summary)
    if ok is None:                                    # 卡还在等 Joy：这次不提交（网关随后让出运行；他同意后续跑时走到同一页直接提交）
        return _say(f"{label} → 审批卡还在等 Joy 处理，这次先不提交：别再提交，交回结果（写明在等 Joy 审批）")
    if isinstance(ok, str):
        return _say(f"{label} → Joy 拒绝了，他说：「{ok[:500]}」。照他说的调整后继续；需要提交时重新 browser_submit")
    if ok is not True:
        return _say(f"{label} → Joy 没有同意，不要再提交；用已有内容交回结果")
    if not ref:                                       # 只申请发信息：记下同意，接着由子代理自己填、发
        S.send_ok = True
        return _say(f"{label} → Joy 同意发出这些信息，可以接着填写、发送；最后的提交按钮仍要 browser_submit(ref) 再批一次")
    return await _act(label + "（Joy 已同意）", {"op": "click", "ref": ref}, submit=True)


@server.tool(name="browser_upload", description=(
    "往文件选择框（input type=file）里放工作目录的文件：path 写工作目录下文件的绝对路径（最大 10 MB）；要 Joy 批准（这次调用里发信息已批过就不用再批）"))
async def browser_upload(ref: str | int, path: str, summary: str) -> Out:
    _need()
    try:
        f = bs.work_file(path)
    except ValueError as e:
        raise BrowserError(str(e)) from None
    label = _label("upload", ref=ref, path=path)
    if not S.send_ok:
        ok = await ask_approval("", f"上传文件 {f.name}：{summary[:60]}")
        if ok is None:
            return _say(f"{label} → 审批卡还在等 Joy 处理，这次先不上传：交回结果（写明在等 Joy 审批）")
        if ok is not True:
            return _say(f"{label} → " + (f"Joy 拒绝了，他说：「{ok[:500]}」" if isinstance(ok, str) else "Joy 没有同意，不要上传"))
        S.send_ok = True
    return await _act(label, {"op": "upload", "ref": fix_ref(ref), "name": f.name, "mime": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                              "data": base64.b64encode(f.read_bytes()).decode()})


@server.tool(name="browser_ask_password", description="需要输入密码时用")
async def browser_ask_password(ref: str | int, summary: str) -> Out:
    _need()
    ref = fix_ref(ref)
    el = next((e for e in S.snap.get("elements") or [] if e.get("ref") == ref), {})
    if el.get("type") != "password":
        raise BrowserError(f"ref 要指向密码框（type=password），{ref} 号是 {el.get('tag', '?')} {str(el.get('name', ''))[:40]!r}")
    label = _label("ask_password", ref=ref)
    got = await ask_secret(summary)
    if got is None:
        return _say(f"{label} → 审批卡还在等 Joy 输入密码，这次先停下：交回结果（写明在等 Joy 登录）")
    if got == "":
        return _say(f"{label} → Joy 选择不登录；用已有内容交回结果")
    try:
        return await _act(label + "（Joy 已输入密码）", {"op": "type", "ref": ref, "text": got, "allow_password": True})
    finally:
        got = None                                     # 密码只在这一次 send 里出现，不进历史、截图、日志、脚本草稿


@server.tool(name="browser_request_domain", description=(
    "用登录档案时网址被域名清单拦下，而那个网站确实是这个已登录网站的登录或内容流程的一部分（如它的 CDN、文件存储、单点登录）："
    "申请把它的主域名（含子域）加进档案的清单。url 写被拦下的网址，reason 一句话写清它和这个网站的关系、为什么要去；profile 不写 = 当前浏览器的档案。"
    "Joy 批准后重新 browser_open 就能去；和登录的网站无关的域名不要申请"))
async def browser_request_domain(url: str, reason: str, profile: str = "") -> Out:
    profile = profile.strip() or S.profile
    if not profile:
        raise BrowserError("没用登录档案：不带档案的浏览器本来就能去任意公网，不用申请")
    try:
        domain = bs.registrable_domain(url)
        have = bs.profile_domains(profile)
    except ValueError as e:
        raise BrowserError(f"申请不了：{e}") from None
    label = _label("request_domain", url=url, profile=profile)

    def done(text: str) -> Out:
        return _say(f"{label} → {text}")

    if not any(domain == d or domain.endswith("." + d) for d in have):
        got, value = await tool_kit.ask_permission(
            "browser", f"给登录档案 {profile} 加主域名 {domain}（含子域）", f"domain:{profile}:{domain}",
            f"被拦下的网址：{url.strip()[:300]}\n理由（模型写的）：{reason.strip()[:300]}\n档案现有的域名：{', '.join(have)}", image=card_shot())
        if got == "failed":
            return done(f"申请没发出去（{value}）：用已有内容交回结果")
        if got == "pending":                          # 卡还在等 Joy：网关随后让出运行，他同意后续跑时再申请就直接加
            return done(f"加域名 {domain} 的审批卡还在等 Joy 处理：别再申请，交回结果（写明在等 Joy 同意加域名 {domain}）")
        if got == "reject":
            return done(f"Joy 拒绝了，他说：「{value[:500]}」，照他说的改" if value else f"Joy 没同意加 {domain}：不要再去这个网站，用已有内容交回结果")
        try:
            bs.add_profile_domain(profile, domain)
        except (ValueError, OSError) as e:
            return done(f"Joy 同意了，但没加上：{type(e).__name__}: {str(e)[:300]}；用已有内容交回结果")
    if S.c is not None and S.profile == profile and not bs.host_allowed(f"https://{domain}/", S.domains):   # 开着的浏览器还按旧清单拦：关掉（登录状态每步已写回档案）
        c, S.c = S.c, None
        await c.__aexit__(None, None, None)
    return done(f"{domain} 已在登录档案 {profile} 的域名清单里（含子域）：用 browser_open 带 profile={profile!r} 重新打开 {url.strip()[:300]}")


@server.tool(name="browser_record", description=(
    "把这次调用里做成的动作存成 browser-script 的脚本草稿（工作目录 scripts/<name>.mjs）（提交那步包进 final()，密码不写）：要掐时间的事（抢课）先走一遍流程、停在最后提交前，再存草稿"))
async def browser_record(name: str) -> Out:
    if not S.record:
        raise BrowserError("这次还没有做成的动作，没有可录的：先 browser_open 走一遍流程")
    if not os.environ.get("STB_WORK_ROOT"):
        raise BrowserError("不在任务运行里（没有工作目录），存不了草稿")
    stem = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")[:40] or "draft"
    d = Path(os.environ["STB_WORK_ROOT"]) / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    f, n = d / f"{stem}.mjs", 2
    while f.exists():                                  # 不覆盖已有的脚本
        f, n = d / f"{stem}-{n}.mjs", n + 1
    f.write_text(script_draft(S.start_url, S.record))
    return _say(f"脚本草稿已存：{f}（{len(S.record)} 个动作）")


if __name__ == "__main__":
    server.run("stdio")
