// open-web-access 浏览器子服务（规格 5 render_page、8、9、10）。
// - 只读：导航、等待选择器、滚动、读 DOM 文本；不执行调用方给的任意 JavaScript。
// - 所有连接经 OWA_PROXY_URL 指定的本地 CONNECT 代理；bypass "<-loopback>" 强制 loopback 也走代理。
// - 导航 wait_until=commit + 约 5 秒宽限，避免在挑战页上挂到超时。
// - 一个浏览器实例、每请求新 context（不持久化 cookie）、并发由 OWA_BROWSER_CONCURRENCY 控制（默认 1）。
import http from "node:http";
import { chromium } from "patchright";

const HOST = "127.0.0.1";
const PORT = Number(process.env.OWA_BROWSER_PORT || 0);
const PROXY = process.env.OWA_PROXY_URL || "";
const CONCURRENCY = Math.max(1, Number(process.env.OWA_BROWSER_CONCURRENCY || 1));
const NAV_TIMEOUT_MS = Number(process.env.OWA_BROWSER_NAV_TIMEOUT_MS || 30000);
const GRACE_MS = 5000;
const MAX_HTML = 200_000;
const UA = process.env.OWA_USER_AGENT || "open-web-access-research-bot/0.1";

if (!PROXY) {
  console.error("OWA_PROXY_URL 未设置：浏览器子服务必须经本地出站代理运行");
  process.exit(2);
}

let browser = null;
async function getBrowser() {
  if (browser && browser.isConnected()) return browser;
  browser = await chromium.launch({
    headless: true,
    proxy: { server: PROXY, bypass: "<-loopback>" },
    // 不从环境或调用方接受 --proxy-server / --host-resolver-rules 等参数
    args: ["--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking", "--disable-sync"],
    ignoreDefaultArgs: ["--enable-automation"],
  });
  return browser;
}

// 简单信号量
let active = 0;
const waiters = [];
async function acquire() {
  if (active < CONCURRENCY) { active++; return; }
  await new Promise((r) => waiters.push(r));
  active++;
}
function release() { active--; const w = waiters.shift(); if (w) w(); }

// 在 DOM（含 shadow root）里找挑战特征；只识别不点击
const CHALLENGE_PROBE = `(() => {
  const sel = ['#cf-challenge-running', '#challenge-spinner', '#turnstile-wrapper', 'input[name=cf-turnstile-response]', '#challenge-form', 'iframe[src*="challenges.cloudflare.com"]'];
  const found = new Set();
  const walk = (root) => {
    for (const s of sel) { try { if (root.querySelector(s)) found.add(s); } catch {} }
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  const scripts = Array.from(document.scripts).map(s => s.textContent || '').join('\\n');
  const ctype = (scripts.match(/cType:\\s*'(non-interactive|managed|interactive)'/) || [])[1] || null;
  return { title: document.title, selectors: Array.from(found), cType: ctype, turnstile: /challenges\\.cloudflare\\.com\\/turnstile/.test(scripts) };
})()`;

async function render(params) {
  const { url, action = "navigate", wait_for_selector = null, scroll_to_bottom = false, max_chars = 16000 } = params;
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) throw new Error("url 必须是 http(s)");
  const b = await getBrowser();
  const context = await b.newContext({ userAgent: UA, javaScriptEnabled: true, viewport: { width: 1280, height: 900 }, locale: "en-US" });
  const page = await context.newPage();
  const t0 = Date.now();
  try {
    page.setDefaultTimeout(NAV_TIMEOUT_MS);
    let response = null;
    try {
      response = await page.goto(url, { waitUntil: "commit", timeout: NAV_TIMEOUT_MS });
    } catch (e) {
      const msg = String(e && e.message || e);
      if (/ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|403/.test(msg)) return { blocked: true, error: "egress-blocked", url };
      throw e;
    }
    await page.waitForLoadState("domcontentloaded", { timeout: GRACE_MS }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: GRACE_MS }).catch(() => {});
    if (wait_for_selector) await page.waitForSelector(String(wait_for_selector).slice(0, 200), { timeout: GRACE_MS }).catch(() => {});
    if (scroll_to_bottom || action === "scroll") {
      for (let i = 0; i < 8; i++) {
        await page.evaluate(() => window.scrollBy(0, window.innerHeight)).catch(() => {});
        await page.waitForTimeout(300);
      }
    }
    const status = response ? response.status() : null;
    const headers = response ? response.headers() : {};
    if (status === 403 && headers["x-owa-egress"] === "blocked") return { blocked: true, error: "egress-blocked", url };
    const probe = await page.evaluate(CHALLENGE_PROBE).catch(() => ({ title: "", selectors: [], cType: null, turnstile: false }));
    const finalUrl = page.url();
    const html = (await page.content().catch(() => "")).slice(0, MAX_HTML);
    const text = await page.evaluate(() => (document.body && document.body.innerText) || "").catch(() => "");
    return {
      blocked: false, url, final_url: finalUrl, status, headers, title: probe.title,
      challenge_probe: probe, html_head: html, text: text.slice(0, max_chars), text_length: text.length,
      elapsed_ms: Date.now() - t0,
    };
  } finally {
    await context.close().catch(() => {});
  }
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (c) => { body += c; if (body.length > 1_000_000) { reject(new Error("body too large")); req.destroy(); } });
    req.on("end", () => { try { resolve(body ? JSON.parse(body) : {}); } catch (e) { reject(e); } });
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => { const s = JSON.stringify(obj); res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(s) }); res.end(s); };
  try {
    if (req.method === "GET" && req.url === "/health") return send(200, { ok: true, browser: !!(browser && browser.isConnected()), active });
    if (req.method === "POST" && req.url === "/render") {
      const params = await readJson(req);
      await acquire();
      try { return send(200, await render(params)); } finally { release(); }
    }
    if (req.method === "POST" && req.url === "/shutdown") { send(200, { ok: true }); await shutdown(); return; }
    send(404, { error: "not found" });
  } catch (e) {
    send(500, { error: String(e && e.message || e).slice(0, 500) });
  }
});

async function shutdown() {
  try { if (browser) await browser.close(); } catch {}
  server.close();
  process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

server.listen(PORT, HOST, () => {
  const addr = server.address();
  // Python 侧从 stdout 第一行读取端口
  console.log(JSON.stringify({ port: addr.port, proxy: PROXY }));
});
