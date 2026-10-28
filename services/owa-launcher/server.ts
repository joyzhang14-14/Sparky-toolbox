// open-web-access 宿主启动服务（规格 3、3.1、9）。
// owa 里唯一持有 docker 访问权的组件：只接受任务参数（task、context、budget…），自己拼固定的 docker 参数；
// 调用方不能传任意 docker 参数或镜像名。持有跨会话全局租约：同时运行的研究容器不超过 max_running（D21）。
// 服务设置（端口、并发、内存守卫、证据保留、研究容器的调参开关、容器默认沙箱 sandbox）只认 config/stbcc.yaml 的 launcher 段（STB_CONFIG 指别的设置目录时读那里的）；
// 研究时限按请求的 budget.max_seconds（isolated-web-search 子代理的程序总会带），没带用 DEFAULT_TIMEOUT，不另设上限。启动时用仓库 venv 的 python（没有就用 python3）读一次。
// 凭证（OWA_LAUNCHER_TOKEN：本服务的令牌）、个人标识（OWA_USER_AGENT）在 .env。
// 运行：node services/owa-launcher/server.ts（Node 24 原生剥离类型）。
import http from "node:http";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, statSync, rmSync, openSync, closeSync, realpathSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const HOME = process.env.OWA_HOME || join(dirname(fileURLToPath(import.meta.url)), "..", "..", "data", "tool_state", "owa");   // 令牌与研究证据（data/tool_state/owa）
const ARTIFACT_ROOT = process.env.OWA_ARTIFACT_ROOT || join(HOME, "artifacts");
// config/stbcc.yaml 的 launcher 段（没有这个文件或这一段时——比如测试把本文件单独拷走——全用默认值）
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STBCC_YAML = join(process.env.STB_CONFIG || join(REPO_ROOT, "config"), "stbcc.yaml");
const STBCC: { launcher?: Record<string, unknown>; tool_user?: string; shared_group?: string } = existsSync(STBCC_YAML)
  ? JSON.parse(execFileSync(existsSync(join(REPO_ROOT, ".venv", "bin", "python")) ? join(REPO_ROOT, ".venv", "bin", "python") : "python3", ["-c", "import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1])) or {}))", STBCC_YAML], { encoding: "utf8" }))
  : {};
const CFG = (STBCC.launcher && typeof STBCC.launcher === "object" ? STBCC.launcher : {}) as Record<string, unknown>;
const SANDBOX = (CFG.sandbox && typeof CFG.sandbox === "object" ? CFG.sandbox : {}) as Record<string, unknown>;
const setting = (v: unknown, dflt: string): string => (v !== undefined && v !== null ? String(v) : dflt);   // 只认 yaml：没写用默认
// 研究容器以工具用户（stbcc.yaml 的 tool_user，默认 sparky）跑（uid:gid），补上共用组（shared_group，默认 sparkle）：证据目录本服务（平台用户，同在组里）照样能读、能清
const SPARKY = ["-u", "-g"].map((f) => execFileSync("id", [f, setting(STBCC.tool_user, "sparky")], { encoding: "utf8" }).trim()).join(":");
const SPARKLE = execFileSync("getent", ["group", setting(STBCC.shared_group, "sparkle")], { encoding: "utf8" }).split(":")[2];
const PORT = Number(setting(CFG.port, "18081"));
const BIND = (Array.isArray(CFG.bind) ? CFG.bind.map(String) : setting(CFG.bind, "127.0.0.1,172.17.0.1").split(",")).map((s) => s.trim()).filter(Boolean);
const IMAGE = setting(SANDBOX.image, "owa-research:latest");
const MEMORY = setting(SANDBOX.memory, "6g");
const CPUS = setting(SANDBOX.cpus, "4");
const DEFAULT_TIMEOUT = 900;   // 请求没带 budget.max_seconds 时的研究时限（秒）
const ARTIFACT_TTL = Number(setting(CFG.artifact_ttl_seconds, "3600"));
const ARTIFACT_ROOT_MAX_BYTES = Number(setting(CFG.artifact_root_max_bytes, String(4 * 1024 ** 3)));
const MODEL_BASE_URL = setting(CFG.model_base_url, "http://host.docker.internal:8000/v1");
const SEARXNG_BASE_URL = setting(CFG.searxng_base_url, "http://searxng:8080");
const WORKER_ENV = (CFG.worker_env && typeof CFG.worker_env === "object" ? CFG.worker_env : {}) as Record<string, unknown>;
const MAX_QUEUE = 24;
// 研究请求的"今天"按本机时区算（D22 J6；UTC 傍晚以后就跨日了）：/etc/timezone，没有就看 /etc/localtime 指向的 zoneinfo，不受进程 TZ 影响
const LOCAL_TZ = (() => {
  try { const n = readFileSync("/etc/timezone", "utf8").trim(); if (n) return n; } catch {}
  try { const p = realpathSync("/etc/localtime"); if (p.includes("zoneinfo/")) return p.split("zoneinfo/")[1]; } catch {}
  return "UTC";
})();
const MAX_RUNNING = Math.max(1, Number(setting(CFG.max_running, "2")));   // 同时跑几个研究容器；每起一个都要过内存守卫
// 实验样本（D20）：只在请求带 _overrides.OWA_FIXTURE_URLS=1 时只读挂进容器 /fixtures，抓取器把 https://fixtures.invalid/<文件名> 映射到这里
const FIXTURE_DIR = process.env.OWA_FIXTURE_DIR || join(dirname(fileURLToPath(import.meta.url)), "..", "..", "tests", "fixtures", "real");
// bench/A-B 允许按任务覆盖的研究循环开关（其余 OWA_* 只能来自启动服务自身的环境）
const OVERRIDE_KEYS = new Set(["OWA_INCREMENTAL_REPORT", "OWA_PLANNING_THINKING", "OWA_MERGED_EXTRACT", "OWA_PARALLEL_ACTIONS", "OWA_SUBQUERY_DEFAULT", "OWA_RERANK_ENABLED", "OWA_EVIDENCE_EXTRACT", "OWA_SUBQUERY_MODE", "OWA_SOURCE_TIERS", "OWA_QUESTION_TRACKING", "OWA_FIXTURE_URLS"]);
// 透传给容器的 OWA_* 配置（白名单，不含 docker 相关）
// 研究容器的这几项按 yaml 的 launcher.worker_env → 仓库根目录 .env（不进 git，只取 PASS_ENV 几项，如带联系邮箱的 OWA_USER_AGENT；凭证不进本进程）取
const ENV_FILE = join(dirname(fileURLToPath(import.meta.url)), "..", "..", ".env");
const PASS_ENV = ["OWA_MODEL_ID", "OWA_RERANK_ENABLED", "OWA_RERANK_MODEL", "OWA_TABLE_MODEL", "OWA_SUBQUERY_DEFAULT", "OWA_EVIDENCE_EXTRACT", "OWA_SUBQUERY_MODE", "OWA_SOURCE_TIERS", "OWA_QUESTION_TRACKING", "OWA_PLANNING_THINKING", "OWA_READ_TOP_N_DEFAULT", "OWA_OUTPUT_CHAR_BUDGET", "OWA_HTTP_CONCURRENCY", "OWA_USER_AGENT", "OWA_MODEL_TIMEOUT_SECONDS"];
const FILE_ENV: Record<string, string> = (() => {
  const out: Record<string, string> = {};
  if (!existsSync(ENV_FILE)) return out;
  for (const line of readFileSync(ENV_FILE, "utf8").split("\n")) {
    const m = line.match(/^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && PASS_ENV.includes(m[1])) out[m[1]] = m[2].replace(/^(["'])(.*)\1$/, "$2");
  }
  return out;
})();

mkdirSync(HOME, { recursive: true, mode: 0o770 });          // data/tool_state/owa：整个 tool_state/ 对 sparky 读写，0700 会把默认 ACL 压掉
mkdirSync(ARTIFACT_ROOT, { recursive: true, mode: 0o770 });
// 令牌：仓库 .env 的 OWA_LAUNCHER_TOKEN（进程环境变量优先；平台与 isolated-web-search 子代理的程序都从 .env 读）。
const ENV_TOKEN = (() => {
  if (!existsSync(ENV_FILE)) return "";
  for (const line of readFileSync(ENV_FILE, "utf8").split("\n")) {
    const m = line.match(/^\s*(?:export\s+)?OWA_LAUNCHER_TOKEN\s*=\s*(.*?)\s*$/);
    if (m) return m[1].replace(/^(["'])(.*)\1$/, "$2");
  }
  return "";
})();
const TOKEN = process.env.OWA_LAUNCHER_TOKEN || ENV_TOKEN;
if (!TOKEN) throw new Error(".env 里没有 OWA_LAUNCHER_TOKEN（本服务的令牌）：写进仓库 .env 再重启 owa-launcher");

type Status = "queued" | "running" | "done" | "failed" | "cancelled" | "timeout";
interface Task {
  id: string; status: Status; request: unknown; timeoutSeconds: number;
  createdAt: number; startedAt?: number; endedAt?: number; containerStartSeconds?: number;
  result?: unknown; error?: { code: string; message: string };
  proc?: ChildProcess; waiters: Array<() => void>; stdout: string; killTimer?: NodeJS.Timeout; cancelRequested: boolean;
  overrides: Record<string, string>;
  modelBaseUrl: string; modelId?: string;   // 研究容器用的模型（D32）：请求没带就是本服务的配置
  memory: string; cpus: string;             // 研究容器的内存 / CPU：请求没带就是本服务的配置（stbcc.yaml 的 launcher.sandbox）
}
const tasks = new Map<string, Task>();
const queue: Task[] = [];
const running = new Set<Task>();

function newId(): string { return Date.now().toString(36) + "-" + randomBytes(4).toString("hex"); }
function notify(t: Task) { for (const w of t.waiters.splice(0)) w(); }
function finish(t: Task, status: Status, error?: Task["error"]) {
  if (t.status === "done" || t.status === "failed" || t.status === "cancelled" || t.status === "timeout") return;
  t.status = status; t.error = error; t.endedAt = Date.now();
  if (t.killTimer) clearTimeout(t.killTimer);
  notify(t);
  // 租约只在 docker 进程真正退出（release）后释放，保证同时跑的研究容器不超过 MAX_RUNNING
}
function release(t: Task) {
  if (running.delete(t)) setImmediate(pump);
}

const MEM_AVAIL_MIN_GB = Number(setting(CFG.host_mem_avail_min_gb, "6"));   // 低于此值不起新研究容器（驱动实测在 avail 2.3–3.6 GB 才失败）；现基线 vllm_node memory.high=80G 下 avail 常态 ≈ 12–13 GB，最多 2 个容器时每起一个都要过这道线
const MEM_WAIT_MAX_S = Number(setting(CFG.host_mem_wait_max_s, "600"));       // 最多等这么久，再失败为 RESOURCE_PRESSURE
let memWaitSince: number | null = null;

function hostMemAvailGb(): number | null {
  try {
    const m = /MemAvailable:\s+(\d+)\s+kB/.exec(readFileSync("/proc/meminfo", "utf8"));
    return m ? Number(m[1]) / 1024 / 1024 : null;
  } catch { return null; }
}

function pump() {
  if (running.size >= MAX_RUNNING || queue.length === 0) return;
  // 规格 §9 的"内存自适应暂停"在宿主这一层：vLLM 常驻在 121 GB 的机器上只剩几 GB 余量，
  // 再起一个 6 GB 上限的研究容器会把统一内存挤爆（2026-09-19 实测 GPU 驱动 NV_ERR_NO_MEMORY → vLLM 退出）。
  const avail = hostMemAvailGb();
  if (avail !== null && avail < MEM_AVAIL_MIN_GB) {
    memWaitSince ??= Date.now();
    const waited = (Date.now() - memWaitSince) / 1000;
    if (waited > MEM_WAIT_MAX_S) {
      const t = queue.shift()!;
      memWaitSince = null;
      finish(t, "failed", { code: "RESOURCE_PRESSURE", message: `宿主可用内存 ${avail.toFixed(1)} GB < ${MEM_AVAIL_MIN_GB} GB，等待 ${MEM_WAIT_MAX_S} s 未缓解` });
      return pump();
    }
    if (waited < 1) console.log(`memory guard: avail ${avail.toFixed(1)} GB < ${MEM_AVAIL_MIN_GB} GB, holding ${queue.length} queued task(s)`);
    setTimeout(pump, 30_000).unref();
    return;
  }
  memWaitSince = null;
  const t = queue.shift()!;
  if (t.cancelRequested) { finish(t, "cancelled", { code: "CANCELLED", message: "排队中被取消" }); return pump(); }
  running.add(t); t.status = "running"; t.startedAt = Date.now();
  const dir = join(ARTIFACT_ROOT, t.id);
  mkdirSync(dir, { recursive: true, mode: 0o770 });
  const logFd = openSync(join(dir, "worker.stderr.log"), "a");
  const prevMounts: string[] = [];
  for (const dirName of findArtifactDirs((t.request as any).evidence_request)) {
    if (dirName === t.id) continue;
    mkdirSync(join(dir, "prev", dirName), { recursive: true, mode: 0o770 });   // 挂载点先以本服务的用户建好：不让 docker 以 root 建（root 的目录本服务删不掉，过期清理就卡住）
    prevMounts.push("-v", `${join(ARTIFACT_ROOT, dirName)}:/artifacts/prev/${dirName}:ro`);
  }
  if (t.overrides.OWA_FIXTURE_URLS === "1" && existsSync(FIXTURE_DIR)) prevMounts.push("-v", `${FIXTURE_DIR}:/fixtures:ro`);
  const args = [
    "run", "--rm", "-i", "--name", `owa-research-${t.id}`, ...prevMounts,
    "--network", "owa-net", "--add-host", "host.docker.internal:host-gateway",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", SPARKY, "--group-add", SPARKLE, "--memory", t.memory, "--cpus", t.cpus, "--pids-limit", "1024",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=1g", "-v", `${dir}:/artifacts`,
    "-e", `OWA_MODEL_BASE_URL=${t.modelBaseUrl}`, "-e", `SEARXNG_BASE_URL=${SEARXNG_BASE_URL}`,
    "-e", "OWA_ARTIFACT_DIR=/artifacts", "-e", `OWA_RESEARCH_TIMEOUT_SECONDS=${t.timeoutSeconds}`,
  ];
  for (const k of PASS_ENV) { const v = k === "OWA_MODEL_ID" && t.modelId ? t.modelId : WORKER_ENV[k] !== undefined ? String(WORKER_ENV[k]) : FILE_ENV[k]; if (v !== undefined) args.push("-e", `${k}=${v}`); }
  for (const [k, v] of Object.entries(t.overrides)) args.push("-e", `${k}=${v}`);
  args.push(IMAGE, "research");
  const proc = spawn("docker", args, { stdio: ["pipe", "pipe", logFd] });
  closeSync(logFd);
  t.proc = proc; t.stdout = "";
  proc.stdout.on("data", (d) => { t.stdout += d.toString(); if (t.stdout.length > 8_000_000) proc.kill("SIGKILL"); });
  proc.on("error", (e) => { finish(t, "failed", { code: "LAUNCH_FAILED", message: String(e.message).slice(0, 300) }); release(t); });
  proc.on("close", (code) => {
    release(t);
    const lines = t.stdout.trim().split("\n");
    const last = lines[lines.length - 1] || "";
    let parsed: any = null;
    try { parsed = JSON.parse(last); } catch { /* 非 JSON */ }
    if (t.status === "cancelled" || t.status === "timeout") { if (parsed && parsed.status) t.result = parsed; return; }   // 取消/超时也保留阶段性结果
    if (parsed && parsed.error) return finish(t, "failed", { code: String(parsed.error.code || "WORKER_ERROR"), message: String(parsed.error.message || "").slice(0, 500) });
    if (parsed && parsed.status) { t.result = parsed; return finish(t, "done"); }
    finish(t, "failed", { code: "WORKER_ERROR", message: `研究容器退出码 ${code}，未产出结果` });
  });
  // 请求经 stdin 传入；日期由启动服务补齐（工具描述保持静态）
  const req = { ...(t.request as object), current_date: (t.request as any).current_date || localDate() };
  proc.stdin.end(JSON.stringify(req));
  // 还有名额就隔 20 秒再试一次起下一个（新任务提交与容器退出时会立刻再试，所以这不是硬性错峰；错峰靠的是每次起容器前的内存守卫）
  if (running.size < MAX_RUNNING && queue.length) setTimeout(pump, 20_000).unref();
  // 超时：先 SIGTERM（容器内转成 CANCELLED 并收尾），宽限 10 秒后 docker kill
  t.killTimer = setTimeout(() => killTask(t, "timeout", `超过 ${t.timeoutSeconds} 秒`), t.timeoutSeconds * 1000);
}

function localDate(now = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: LOCAL_TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);   // en-CA 就是 YYYY-MM-DD
}

function killTask(t: Task, status: Status, why: string) {
  if (t.status !== "running") { t.cancelRequested = true; if (t.status === "queued") finish(t, "cancelled", { code: "CANCELLED", message: why }); return; }
  const name = `owa-research-${t.id}`;
  spawn("docker", ["kill", "--signal", "TERM", name], { stdio: "ignore" });
  setTimeout(() => spawn("docker", ["kill", name], { stdio: "ignore" }), 10_000).unref();
  finish(t, status, { code: status === "timeout" ? "TIMEOUT" : "CANCELLED", message: why });
}

// evidence_request 里的 artifact_id 属于哪个历史任务目录：扫描各目录的 index.json
function findArtifactDirs(evidenceRequest: unknown): string[] {
  if (!Array.isArray(evidenceRequest) || evidenceRequest.length === 0) return [];
  const wanted = new Set(evidenceRequest.map((e: any) => String(e?.artifact_id || "")).filter(Boolean));
  const dirs = new Set<string>();
  try {
    for (const n of readdirSync(ARTIFACT_ROOT)) {
      const idx = join(ARTIFACT_ROOT, n, "index.json");
      if (!existsSync(idx)) continue;
      try {
        const keys = Object.keys(JSON.parse(readFileSync(idx, "utf8")));
        if (keys.some((k) => wanted.has(k))) dirs.add(n);
      } catch { /* 坏索引忽略 */ }
      if (dirs.size >= 4) break;
    }
  } catch { /* 无目录 */ }
  return [...dirs];
}

function sweepArtifacts() {
  try {
    const entries = readdirSync(ARTIFACT_ROOT).map((n) => ({ n, p: join(ARTIFACT_ROOT, n), st: statSync(join(ARTIFACT_ROOT, n)) }))
      .filter((e) => e.st.isDirectory()).sort((a, b) => a.st.mtimeMs - b.st.mtimeMs);
    const now = Date.now();
    let total = 0;
    const sizes = new Map<string, number>();
    for (const e of entries) { const s = dirSize(e.p); sizes.set(e.n, s); total += s; }
    for (const e of entries) {
      const runningNow = [...running].some((r) => r.id === e.n);
      if (runningNow) continue;
      if (now - e.st.mtimeMs > ARTIFACT_TTL * 1000 || total > ARTIFACT_ROOT_MAX_BYTES) {
        try { rmSync(e.p, { recursive: true, force: true }); total -= sizes.get(e.n) || 0; }
        catch (err) { console.log(`artifact sweep: ${e.n} 删不了，跳过：${(err as Error).message}`); }   // 一个删不了不挡住后面的（原来整轮中断）
      }
    }
  } catch { /* 忽略清理失败 */ }
}
function dirSize(p: string): number {
  let s = 0;
  try { for (const n of readdirSync(p)) { const st = statSync(join(p, n)); s += st.isDirectory() ? dirSize(join(p, n)) : st.size; } } catch {}
  return s;
}
setInterval(sweepArtifacts, 60_000).unref();

function authorized(req: http.IncomingMessage): boolean {
  const h = req.headers.authorization || "";
  const given = Buffer.from(h.replace(/^Bearer\s+/i, ""));
  const want = Buffer.from(TOKEN);
  return given.length === want.length && timingSafeEqual(given, want);
}
function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let b = ""; req.on("data", (c) => { b += c; if (b.length > 512_000) { reject(new Error("body too large")); req.destroy(); } });
    req.on("end", () => resolve(b)); req.on("error", reject);
  });
}
function view(t: Task) {
  return { task_id: t.id, status: t.status, result: t.result, error: t.error, seconds: ((t.endedAt || Date.now()) - (t.startedAt || t.createdAt)) / 1000,
    queue_position: t.status === "queued" ? queue.indexOf(t) + 1 : 0, container_start_seconds: t.containerStartSeconds ?? null,
    waiting_for: t.status === "queued" && memWaitSince !== null ? "host_memory" : null };
}

const server = http.createServer(async (req, res) => {
  const send = (code: number, obj: unknown) => { const s = JSON.stringify(obj); res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(s) }); res.end(s); };
  const url = new URL(req.url || "/", "http://x");
  if (url.pathname === "/health") return send(200, { ok: true, running: [...running].map((r) => r.id), max_running: MAX_RUNNING, queued: queue.length, image: IMAGE,
    host_mem_avail_gb: hostMemAvailGb(), mem_guard_min_gb: MEM_AVAIL_MIN_GB, holding_for_memory: memWaitSince !== null });
  if (!authorized(req)) return send(401, { error: "unauthorized" });
  try {
    if (req.method === "POST" && url.pathname === "/tasks") {
      const body = JSON.parse((await readBody(req)) || "{}");
      if (!body || typeof body.task !== "string" || !body.task.trim()) return send(400, { error: "task 必填" });
      if (queue.length >= MAX_QUEUE) return send(429, { error: "队列已满" });
      const requested = Number(body?.budget?.max_seconds || DEFAULT_TIMEOUT);
      const timeoutSeconds = Math.max(30, requested) + 30; // 容器内 budget 自行收尾；外层多给 30 秒
      // `_overrides`：只允许切换研究循环的开关（A/B 用），从请求体里剥离后再交给容器；其它键一律拒绝
      const overrides: Record<string, string> = {};
      if (body._overrides !== undefined) {
        if (typeof body._overrides !== "object" || body._overrides === null) return send(400, { error: "_overrides 必须是对象" });
        for (const [k, v] of Object.entries(body._overrides)) {
          if (!OVERRIDE_KEYS.has(k)) return send(400, { error: `不允许覆盖 ${k}` });
          if (!/^[A-Za-z0-9_.-]{1,64}$/.test(String(v))) return send(400, { error: `${k} 的值不合法` });
          overrides[k] = String(v);
        }
        delete body._overrides;
      }
      // 研究容器用哪个模型（D32：研究子代理可指定自己的档位）：只收宿主上的端点 http://host.docker.internal:<端口>/v1 与模型 id，
      // 从请求体里剥离后再交给容器；不传就用本服务的配置（stbcc.yaml 的 launcher.model_base_url，默认 :8000）
      const { model_base_url: mb, model_id: mi } = body;
      if (mb !== undefined && (typeof mb !== "string" || !/^http:\/\/host\.docker\.internal:\d{1,5}\/v1$/.test(mb))) return send(400, { error: "model_base_url 只能是 http://host.docker.internal:<端口>/v1" });
      if (mi !== undefined && (typeof mi !== "string" || !/^[A-Za-z0-9_.:\/-]{1,128}$/.test(mi))) return send(400, { error: "model_id 不合法" });
      delete body.model_base_url; delete body.model_id;
      // 研究容器的沙箱（请求带了的话）：镜像与网络只能是本服务固定的那两个（对不上就拒，免得配置写了却没生效），
      // 内存 / CPU 在上限内按请求；从请求体里剥离后再交给容器
      const sb = body.sandbox;
      let memory = MEMORY, cpus = CPUS;
      if (sb !== undefined) {
        if (typeof sb !== "object" || sb === null) return send(400, { error: "sandbox 必须是对象" });
        if (sb.image !== IMAGE || sb.network !== "owa-net") return send(400, { error: `sandbox 的 image / network 只能是 ${IMAGE} / owa-net（launcher 的配置）` });
        const m = /^(\d+)([mg])$/.exec(String(sb.memory));
        const c = Number(sb.cpus);
        if (!m || Number(m[1]) * (m[2] === "g" ? 1024 : 1) > 16 * 1024 || !(c > 0 && c <= 8)) return send(400, { error: "sandbox.memory 最多 16g、cpus 在 (0, 8] 内" });
        memory = String(sb.memory); cpus = String(c);
        delete body.sandbox;
      }
      const t: Task = { id: newId(), status: "queued", request: body, timeoutSeconds, createdAt: Date.now(), waiters: [], stdout: "", cancelRequested: false, overrides,
        modelBaseUrl: mb ?? MODEL_BASE_URL, modelId: mi, memory, cpus };
      tasks.set(t.id, t); queue.push(t); setImmediate(pump);
      return send(202, view(t));
    }
    // 研究选中的图（证据目录 <任务号>/<artifact_id>.png）：调用方（isolated-web-search 子代理的程序）经这里取
    const im = url.pathname.match(/^\/tasks\/([A-Za-z0-9-]+)\/images\/(imag_[0-9a-f]{12}_[0-9a-f]{4})\.png$/);
    if (im && req.method === "GET") {
      const f = join(ARTIFACT_ROOT, im[1], `${im[2]}.png`);
      if (!existsSync(f)) return send(404, { error: "没有这张图" });
      const buf = readFileSync(f);
      res.writeHead(200, { "content-type": "image/png", "content-length": buf.length });
      return res.end(buf);
    }
    const m = url.pathname.match(/^\/tasks\/([A-Za-z0-9-]+)$/);
    if (m) {
      const t = tasks.get(m[1]);
      if (!t) return send(404, { error: "未知任务" });
      if (req.method === "DELETE") { killTask(t, "cancelled", "调用方取消"); return send(200, view(t)); }
      if (req.method === "GET") {
        const wait = Math.min(Number(url.searchParams.get("wait") || 0), 60);
        if (wait > 0 && (t.status === "queued" || t.status === "running")) {
          await new Promise<void>((r) => { const timer = setTimeout(r, wait * 1000); t.waiters.push(() => { clearTimeout(timer); r(); }); });
        }
        return send(200, view(t));
      }
    }
    send(404, { error: "not found" });
  } catch (e: any) {
    send(500, { error: String(e?.message || e).slice(0, 300) });
  }
});
server.requestTimeout = 120_000;
server.headersTimeout = 65_000;
for (const host of BIND) {
  const s = host === BIND[0] ? server : http.createServer(server.listeners("request")[0] as any);
  s.listen(PORT, host, () => console.log(`owa-launcher listening on ${host}:${PORT} image=${IMAGE} artifacts=${ARTIFACT_ROOT}`));
  s.on("error", (e: any) => {
    console.error(`bind ${host}:${PORT} failed: ${e.code || e.message}`);
    // 开机时 docker0（172.17.0.1）可能比本服务晚起来：非首个地址报 EADDRNOTAVAIL 就每 30 秒重试，直到绑上
    if (host !== BIND[0] && e.code === "EADDRNOTAVAIL") setTimeout(() => s.listen(PORT, host), 30_000);
  });
}
