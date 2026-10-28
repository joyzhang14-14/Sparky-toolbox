// 浏览器容器内的驱动（sparky-toolbox 的 browser 工具，tools/browser/server.py）。
// stdin 每行一个 JSON 命令，stdout 每行一个 JSON 回复；由宿主上的 browser 工具逐步驱动，本进程不做任何判断。
// - 所有连接经 OWA_PROXY_URL（同容器里的出站代理：只连公网 IP、只 80/443）。
// - 网站自己的请求（含 POST）照常放行，像人用浏览器一样；提交类点击由宿主要求子代理用 submit 动作、先过 Joy 的审批卡。
// - 主框架导航只允许 nav_domains 里的域名（"*" = 任意公网域名），防止把页面内容拼进网址带出去。
// - 不往密码框输入；不接受下载；弹窗（alert / confirm）一律取消；新开的标签页接管为当前页。
import readline from "node:readline";
import { chromium } from "patchright";

const PROXY = process.env.OWA_PROXY_URL || "";
const MAX_ELEMENTS = 200;
const MAX_TEXT = 8000;

if (!PROXY) {
  process.stdout.write(
    JSON.stringify({ ok: false, error: "OWA_PROXY_URL 未设置" }) + "\n",
  );
  process.exit(2);
}

let browser, context, page;
let navDomains = [];
let mainStatus = null;   // 主框架最近一次文档响应的 HTTP 状态码
let refFrame = new Map(); // 元素编号 → 它所在的框架（主页面或 iframe）
const BATCHABLE = new Set(["click", "click_at", "type", "select", "press", "scroll", "wait", "hover", "drag"]);   // 能放进一串（batch）的动作
const KEY_ALIAS = { Right: "ArrowRight", Left: "ArrowLeft", Up: "ArrowUp", Down: "ArrowDown", Esc: "Escape", Return: "Enter" };
const blocked = [];
let writes = [];          // 写入型请求（POST / PUT / PATCH / DELETE）：脚本演练时列给 Joy 看，只记不拦
let busy = false;
let navError = null;     // 最近一次打开网址失败的原因（域名解析不了、连不上）：报给子代理，别让它当成"页面空白"去猜         // 正在执行脚本：宿主断开（/stop 杀掉工具进程）时立刻退出，脚本不再往下跑
class DryStop extends Error {}                   // 演练走到 final()：到此为止
class FinalFailed extends Error {}               // final() 次数用完还没成功
const firstLine = (e) => String((e && e.message) || e).split("\n")[0].slice(0, 200);

const hostOf = (url) => { try { return new URL(url).hostname || String(url).slice(0, 100); } catch { return String(url).slice(0, 100); } };

function hostAllowed(url) {
  let host;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (url.startsWith("about:") || url.startsWith("data:")) return true;
  return navDomains.some(
    (d) => d === "*" || host === d || host.endsWith("." + d),
  );
}

// 请求所在框架是不是某个标签页的主框架；新开标签页的第一个导航请求发出时框架还没建好（frame() 会抛错），按主框架算
function mainFrameOf(req) {
  let frame = null;
  try { frame = req.frame(); } catch { return true; }
  return frame.parentFrame() === null;
}

async function route(r) {
  const req = r.request();
  const u = new URL(req.url());
  if (req.isNavigationRequest() && mainFrameOf(req) && !hostAllowed(req.url())) {
    blocked.push({ kind: "nav", url: u.origin + u.pathname });
    return r.abort("blockedbyclient");
  }
  return r.continue();
}

function adopt(p) {
  page = p;
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
  page.on("response", (r) => {
    try { if (r.request().isNavigationRequest() && r.frame() === page.mainFrame()) mainStatus = r.status(); } catch {}
  });
  page.on("popup", (np) => adopt(np));
}

async function settle() {
  await page
    .waitForLoadState("domcontentloaded", { timeout: 15000 })
    .catch(() => {});
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
}

// 给可见的可交互元素编号（data-stb-ref），返回元素表和页面正文
// 在当前框架里给视口内有编号的元素画框、标编号（截完图再删掉）
const MARK_JS = `(() => {
  const box = document.createElement('div');
  box.id = '__stb_marks';
  box.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
  for (const el of document.querySelectorAll('[data-stb-ref]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const f = document.createElement('div');
    f.style.cssText = 'position:fixed;border:2px solid #ff2d55;box-sizing:border-box;left:' + r.left + 'px;top:' + r.top + 'px;width:' + r.width + 'px;height:' + r.height + 'px';
    const t = document.createElement('div');
    t.textContent = el.getAttribute('data-stb-ref');
    t.style.cssText = 'position:fixed;background:#ff2d55;color:#fff;font:bold 11px/13px monospace;padding:0 2px;left:' + Math.max(0, r.left) + 'px;top:' + Math.max(0, r.top - 13) + 'px';
    box.append(f, t);
  }
  (document.body || document.documentElement).appendChild(box);
})()`;
const UNMARK_JS = `(() => { const b = document.getElementById('__stb_marks'); if (b) b.remove(); })()`;

const SNAPSHOT_JS = `((max, maxText, start) => {
  document.querySelectorAll('[data-stb-ref]').forEach(e => e.removeAttribute('data-stb-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[onclick],[contenteditable=true],[draggable=true]';
  const out = [];
  // 用脚本做的"按钮"（div / span 绑了点击事件，鼠标移上去是手指）：取最外层那个，已在标准可点元素里面的不重复收
  const pointer = [...(document.body ? document.body.querySelectorAll('div,span,li,label,img,svg,p,td,i') : [])].filter(el =>
    !el.closest(sel) && getComputedStyle(el).cursor === 'pointer' && !(el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer'));
  // 不在链接、按钮里的大图（头像、商品图）：悬停、点图片时要用到
  const imgs = [...document.querySelectorAll('img')].filter(el => !el.closest(sel) && !pointer.some(p => p.contains(el)) && el.width >= 40 && el.height >= 40);
  const all = [...document.querySelectorAll(sel), ...pointer, ...imgs].sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
  // 超过上限时先保留屏幕上看得见的（日历、长列表会把后面的按钮挤掉），剩下的名额按页面顺序给屏幕外的；编号仍按页面顺序
  const cand = [];
  for (const el of all) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    cand.push([el, r]);
  }
  // 真看得见：中心点在视口里、而且那一点最上层的就是它（被带滚动条的弹窗裁掉、被遮罩盖住的都不算）
  const seen = ([el, r]) => {
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
    const hit = document.elementFromPoint(x, y);
    return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
  };
  const outside = ([, r]) => r.bottom < 0 || r.top > innerHeight;   // 在视口外、滚一下能看到
  const keep = new Set();
  for (const pick of [seen, outside, () => true]) {                  // 名额先给看得见的，再给视口外的，最后给被裁掉、被盖住的
    for (const c of cand) { if (keep.size >= max) break; if (!keep.has(c[0]) && pick(c)) keep.add(c[0]); }
  }
  for (const [el, r] of cand) {
    if (!keep.has(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    // 复选框、单选框常常没有自己的名字：用它的 <label> 或紧跟着的文字
    const near = (el.labels && el.labels[0] && el.labels[0].innerText) || (tag === 'input' && el.nextSibling && el.nextSibling.textContent) || '';
    const label = (tag === 'input' || tag === 'select' || tag === 'textarea') && el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const btnText = tag === 'input' && ['submit', 'button', 'reset'].includes(type) ? el.value : '';   // <input type=submit> 的字在 value 里
    const name = (el.getAttribute('aria-label') || label || btnText || el.innerText || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || near || el.getAttribute('name') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    const ref = String(start + out.length + 1);
    el.setAttribute('data-stb-ref', ref);
    const item = { ref, tag, name };
    if (type) item.type = type;
    // 录制脚本草稿用：标签、占位文字、role、id（给模型看的元素表里不显示）
    if (label.trim()) item.label = label.replace(/\\s+/g, ' ').trim().slice(0, 80);
    if (el.getAttribute('placeholder')) item.ph = el.getAttribute('placeholder').slice(0, 80);
    if (el.getAttribute('role')) item.role = el.getAttribute('role');
    if (el.id) item.id = el.id.slice(0, 80);
    if (tag === 'a') item.href = (el.getAttribute('href') || '').slice(0, 160);
    if (tag === 'input' || tag === 'textarea' || tag === 'select') item.value = type === 'password' ? '***' : String(el.value || '').slice(0, 80);
    if (tag === 'select' && el.selectedIndex >= 0) item.selected = (el.options[el.selectedIndex].text || '').trim().slice(0, 80);
    if (type === 'checkbox' || type === 'radio') item.checked = el.checked;
    else if (el.hasAttribute('aria-checked')) item.checked = el.getAttribute('aria-checked') === 'true';
    if (!seen([el, r])) item.offscreen = true;                    // 现在看不见（视口外、被裁掉或被盖住）
    out.push(item);
  }
  // 各级标题（h1–h3，看得见的）：交给读者时写在正文前面（innerText 里看不出哪行是标题）
  const headings = [...document.querySelectorAll('h1,h2,h3')].filter(h => h.offsetParent !== null || getComputedStyle(h).position === 'fixed')
    .map(h => ({ tag: h.tagName.toLowerCase(), text: (h.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200) })).filter(h => h.text).slice(0, 40);
  return { elements: out, text: (document.body ? document.body.innerText : '').slice(0, maxText), headings };
})`;

async function snapshot() {
  await settle();
  if (page.url().startsWith("chrome-error://")) {
    // 被拦的导航停在错误页：退回上一页
    await page.goBack({ timeout: 15000 }).catch(() => {});
    await settle();
  }
  // 主页面和每个看得见的 iframe（嵌在网页里的另一个网页：申请表、支付框、编辑器常在里面）都编号，编号接着往下排
  refFrame = new Map();
  const snap = { elements: [], text: "", headings: [] };
  for (const f of page.frames()) {
    if (snap.elements.length >= MAX_ELEMENTS) break;
    const main = f === page.mainFrame();
    if (!main) {
      const box = await f.frameElement().then((h) => h.boundingBox()).catch(() => null);
      if (!box || box.width < 2 || box.height < 2) continue;
    }
    const r = await f
      .evaluate(`${SNAPSHOT_JS}(${MAX_ELEMENTS - snap.elements.length}, ${main ? MAX_TEXT : 3000}, ${snap.elements.length})`)
      .catch(() => null);
    if (!r) continue;
    for (const e of r.elements) {
      if (!main) e.frame = true;
      refFrame.set(e.ref, f);
      snap.elements.push(e);
    }
    if (main) snap.headings = r.headings || [];
    const t = (r.text || "").trim();
    if (t) snap.text += main ? t : `\n\n[iframe ${f.url().slice(0, 120)}]\n${t}`;
  }
  const shot = await page
    .screenshot({ type: "jpeg", quality: 60 })
    .catch(() => null);
  const frames = page.frames();
  for (const f of frames) await f.evaluate(MARK_JS).catch(() => {});
  const marked = await page.screenshot({ type: "jpeg", quality: 60 }).catch(() => null);
  for (const f of frames) await f.evaluate(UNMARK_JS).catch(() => {});
  const b = blocked.splice(0);
  const ne = navError;
  navError = null;
  return {
    url: page.url(),
    title: await page.title().catch(() => ""),
    status: mainStatus,
    ...snap,
    blocked: b,
    nav_error: ne,
    screenshot: shot ? shot.toString("base64") : null,
    marked: marked ? marked.toString("base64") : null,
  };
}

function el(ref) {
  if (!/^\d+$/.test(String(ref))) throw new Error("ref 要是元素编号");
  return (refFrame.get(String(ref)) || page.mainFrame()).locator(`[data-stb-ref="${ref}"]`).first();
}

// 一串动作（batch）里的单个动作不快照：编号沿用上一次快照，做完整串再快照一次
async function done(c) {
  if (!c.nosnap) return snapshot();
  await page.waitForLoadState("domcontentloaded", { timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(400);
  return {};
}

const OPS = {
  async start(c) {
    navDomains = (c.nav_domains || []).map((d) => String(d).toLowerCase());
    browser = await chromium.launch({
      headless: true,
      proxy: { server: PROXY, bypass: "<-loopback>" },
      args: [
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--disable-background-networking",
        "--disable-sync",
      ],
      ignoreDefaultArgs: ["--enable-automation"],
    });
    context = await browser.newContext({
      storageState: c.storage_state || undefined,
      acceptDownloads: false,
      viewport: { width: 1280, height: 860 },
      locale: "en-US",
      timezoneId: process.env.TZ || "America/Los_Angeles",
    });
    await context.route("**/*", route);
    context.on("request", (req) => {
      const m = req.method();
      if (!["GET", "HEAD", "OPTIONS"].includes(m)) writes.push({ method: m, url: req.url().slice(0, 300) });
    });
    adopt(await context.newPage());
    if (!hostAllowed(c.start_url))
      throw new Error(`start_url 不在允许的域名里：${hostOf(c.start_url)} 不在 ${navDomains.join("、") || "（空）"} 里；换成允许域名里的网址`);
    await page
      .goto(c.start_url, { waitUntil: "domcontentloaded", timeout: 30000 })
      .catch((e) => { navError = firstLine(e); });
    return snapshot();
  },
  snapshot,
  async click(c) {
    // 链接可能在新标签页打开（target=_blank）：点完等一下 popup，来了就切过去
    const popup = page.waitForEvent("popup", { timeout: 3000 }).catch(() => null);
    await el(c.ref).click({ timeout: 8000 });
    const np = await popup;
    if (np && np !== page) adopt(np);
    return done(c);
  },
  async type(c) {
    const e = el(c.ref);
    if ((await e.getAttribute("type").catch(() => "")) === "password" && !c.allow_password)
      throw new Error("不能往密码框输入（只能填 Joy 在问题卡里给的内容）");
    // 先把框滚到它所在区域（整页、带滚动条的小框）的正中间：贴边时常被别的东西挡住，挡住的框有的页面会清空
    await e.evaluate((n) => n.scrollIntoView({ block: "center", inline: "nearest" })).catch(() => {});
    await e.fill(String(c.text ?? ""), { timeout: 8000 });
    if (c.enter) await e.press("Enter");
    return done(c);
  },
  async select(c) {
    await el(c.ref).selectOption(String(c.value ?? ""), { timeout: 8000 });
    return done(c);
  },
  async press(c) {
    const key = String(c.key || "Enter");
    await page.keyboard.press(KEY_ALIAS[key] || key);
    return done(c);
  },
  async click_at(c) {
    // 按截图坐标点（截图就是视口，1280×860，左上角是 0,0）：元素表里找不到要点的东西时用
    const popup = page.waitForEvent("popup", { timeout: 3000 }).catch(() => null);
    await page.mouse.click(Number(c.x) || 0, Number(c.y) || 0);
    const np = await popup;
    if (np && np !== page) adopt(np);
    return done(c);
  },
  async scroll(c) {
    // 写 ref 就滚那个元素所在的可滚动区域（页面里带滚动条的小框、弹窗、下拉列表）：直接找最近的可滚动祖先，元素被挡住也滚得动
    if (c.ref) {
      await el(c.ref).evaluate((e, dy) => {
        let p = e.parentElement;
        while (p && !(p.scrollHeight > p.clientHeight && /(auto|scroll)/.test(getComputedStyle(p).overflowY))) p = p.parentElement;
        (p || document.scrollingElement).scrollBy(0, dy);
      }, Number(c.dy) || 700, { timeout: 8000 });
    } else await page.mouse.wheel(0, Number(c.dy) || 700);
    return done(c);
  },
  async wait(c) {
    // 等页面自己变：写 text 就等到页面上出现这段文字，否则干等 seconds 秒（1–60，默认 5；写了 text 默认最多等 30 秒）
    const ms = Math.min(Math.max(Number(c.seconds) || (c.text ? 30 : 5), 1), 60) * 1000;
    if (c.text) {
      // 等这段文字"新出现"：页面上原本就有的（比如说明里的 "reach 75%"）不算，出现次数比开始时多才算等到；0.1 秒查一次
      const n0 = await page.evaluate((w) => (document.body ? document.body.innerText.split(w).length - 1 : 0), String(c.text)).catch(() => 0);
      const seen = await page
        .waitForFunction(([w, n]) => (document.body ? document.body.innerText.split(w).length - 1 : 0) > n, [String(c.text), n0], { timeout: ms, polling: 100 })
        .then(() => true, () => false);
      // 一串动作里没等到就算失败，后面的不做（比如没等到 75% 就别点 Stop）；单独用时照常返回当前页
      if (!seen && c.nosnap) throw new Error(`等了 ${ms / 1000} 秒没出现「${c.text}」`);
    } else await page.waitForTimeout(ms);
    return done(c);
  },
  async hover(c) {
    // 鼠标移到元素（ref）或坐标（x, y）上：鼠标移上去才出现的菜单、说明
    if (c.ref) await el(c.ref).hover({ timeout: 8000 });
    else await page.mouse.move(Number(c.x) || 0, Number(c.y) || 0);
    return done(c);
  },
  async drag(c) {
    // 按住拖到另一个元素（to）或坐标（to_x, to_y）上松开
    if (c.ref && c.to) {
      await el(c.ref).dragTo(el(c.to), { timeout: 8000 });
      return done(c);
    }
    if (c.to_x === undefined || c.to_y === undefined) throw new Error("drag 要写拖到哪：to（目标元素编号）或 to_x、to_y（截图坐标）");
    const from = c.ref ? await el(c.ref).boundingBox() : { x: Number(c.x) || 0, y: Number(c.y) || 0, width: 0, height: 0 };
    if (!from) throw new Error("拖不动：元素不在页面上（没有位置，可能在视口外或被隐藏）；先 scroll 让它出现，或改用截图坐标 x、y 拖");
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(Number(c.to_x) || 0, Number(c.to_y) || 0, { steps: 12 });
    await page.mouse.up();
    return done(c);
  },
  async upload(c) {
    // 文件由宿主从工作目录读好（base64）传进来，塞进文件选择框
    await el(c.ref).setInputFiles(
      { name: String(c.name), mimeType: String(c.mime || "application/octet-stream"), buffer: Buffer.from(String(c.data || ""), "base64") },
      { timeout: 8000 },
    );
    return snapshot();
  },
  async batch(c) {
    // 一次做一串动作：某个失败、或页面跳到别的网址就停，剩下的不做
    const url0 = page.url();
    let n = 0;
    let error = null;
    for (const cmd of c.cmds || []) {
      if (!BATCHABLE.has(cmd.op)) {
        error = `第 ${n + 1} 个动作 ${cmd.op} 不能放进一串`;
        break;
      }
      try {
        await OPS[cmd.op]({ ...cmd, nosnap: true });
      } catch (e) {
        error = `第 ${n + 1} 个动作 ${cmd.op} 失败：${String((e && e.message) || e).slice(0, 200)}`;
        break;
      }
      n += 1;
      if (page.url() !== url0) break;
    }
    return { ...(await snapshot()), batch_done: n, batch_error: error };
  },
  async script(c) {
    // 执行 Playwright 脚本（.mjs 源码）：dry 走到 final() 停下；live 由 final() 按次数反复提交、成功一次就停；check 调 check() 返回真假
    const fs = await import("node:fs/promises");
    const file = `/tmp/stb-script-${Date.now()}-${Math.random().toString(16).slice(2)}.mjs`;
    await fs.writeFile(file, String(c.source || ""));
    const t0 = Date.now();
    const at = () => Date.now() - t0;
    const events = [];
    const shots = [];
    const shot = async (why) => {
      if (shots.length >= 12) return;
      const b = await page.screenshot({ type: "jpeg", quality: 60 }).catch(() => null);
      if (b) shots.push({ why, at: at(), data: b.toString("base64") });
    };
    const p0 = page;
    p0.setDefaultTimeout(10000);                   // 脚本里的点击、填写默认最多等 10 秒（Playwright 默认 30 秒，final 反复重试时会拖很久）
    const onNav = (f) => { if (f === p0.mainFrame()) events.push({ at: at(), kind: "nav", url: f.url().slice(0, 300) }); };
    p0.on("framenavigated", onNav);
    writes = [];
    let used = false;
    let tries = 0;
    const final = async (fn) => {
      if (c.mode !== "live") {
        await shot("final 之前");
        throw new DryStop();
      }
      if (used) throw new Error("final() 只能成功一次，已经成功过");
      const max = Number(c.final_tries) || 40;
      const every = Number(c.final_every_ms) || 500;
      const deadline = Date.now() + (Number(c.final_seconds) || 30) * 1000;   // 次数和总时长哪个先到都停
      for (tries = 1; tries <= max && Date.now() < deadline; tries++) {
        let ok = false;
        try {
          ok = (await fn()) === true;
        } catch (e) {
          events.push({ at: at(), kind: "final_error", msg: firstLine(e) });
        }
        events.push({ at: at(), kind: "final_try", n: tries, ok });
        if (ok) {
          used = true;
          await shot("final 成功");
          return true;
        }
        await p0.waitForTimeout(every);
      }
      await shot("final 次数用完");
      throw new FinalFailed();
    };
    const log = (msg) => events.push({ at: at(), kind: "log", msg: String(msg).slice(0, 300) });
    let result = "done";
    let error = null;
    busy = true;
    try {
      const mod = await import(`file://${file}`);
      if (c.start_at) {
        const wait = Number(c.start_at) - Date.now();
        if (wait > 0) await new Promise((r) => setTimeout(r, wait));
        events.push({ at: at(), kind: "start_at" });
      }
      if (c.mode === "check") {
        if (typeof mod.check !== "function") throw new Error("脚本没有导出 check()");
        result = (await mod.check({ page: p0, log })) === true ? "true" : "false";
      } else {
        if (typeof mod.run !== "function") throw new Error("脚本没有导出 run()");
        await mod.run({ page: p0, final, log });
        if (c.mode === "live" && !used) result = "done_without_final";
      }
    } catch (e) {
      if (e instanceof DryStop) result = "stopped_at_final";
      else if (e instanceof FinalFailed) result = "final_failed";
      else {
        result = "error";
        // 出错那一行：只取脚本文件里的行号（报错信息只留第一行，后面常带页面内容）
        const m = String((e && e.stack) || "").match(new RegExp(file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ":(\\d+)"));
        error = { message: firstLine(e), line: m ? Number(m[1]) : null };
      }
    } finally {
      busy = false;
      p0.off("framenavigated", onNav);
      p0.setDefaultTimeout(30000);                  // 还原 Playwright 默认
      if (result !== "stopped_at_final") await shot("结束");
      fs.unlink(file).catch(() => {});
    }
    return { result, error, events: events.slice(-300), writes: writes.slice(0, 200), shots, final_tries: tries, url: p0.url(), seconds: at() / 1000 };
  },
  async goto(c) {
    if (!hostAllowed(c.url)) throw new Error(`这个网址的域名不在允许范围里：${hostOf(c.url)} 不在 ${navDomains.join("、") || "（空）"} 里；只在这些域名里导航`);
    await page
      .goto(String(c.url), { waitUntil: "domcontentloaded", timeout: 30000 })
      .catch((e) => { navError = firstLine(e); });
    return snapshot();
  },
  async back() {
    await page.goBack({ timeout: 15000 }).catch(() => {});
    return snapshot();
  },
  async state() {
    return { storage_state: await context.storageState() };
  },
  async close() {
    await browser?.close().catch(() => {});
    return {};
  },
};

const rl = readline.createInterface({ input: process.stdin });
rl.on("close", () => { if (busy) process.exit(0); });   // 脚本执行中宿主断开（/stop）：马上退出，不让脚本接着提交
for await (const line of rl) {
  if (!line.trim()) continue;
  let reply;
  try {
    const c = JSON.parse(line);
    const fn = OPS[c.op];
    if (!fn) throw new Error(`未知命令 ${c.op}`);
    reply = { ok: true, ...(await fn(c)) };
    if (c.op === "close") {
      process.stdout.write(JSON.stringify(reply) + "\n");
      break;
    }
  } catch (e) {
    reply = { ok: false, error: String((e && e.message) || e).slice(0, 300) };
  }
  process.stdout.write(JSON.stringify(reply) + "\n");
}
process.exit(0);
