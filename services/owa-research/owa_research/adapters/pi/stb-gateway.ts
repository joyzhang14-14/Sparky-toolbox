// stb 的 pi 扩展（规格 D2、第 8 节）：沙箱里唯一的工具来源。
// 启动时从网关拉本次运行的工具列表并注册为 pi 工具；每次调用转发给网关（Bearer 令牌，一次运行一个）。
// 只读挂载到 /opt/stb/ext/stb-gateway.ts，pi 以 --no-builtin-tools 启动，本文件再把活动工具收窄成网关给的这几个。
import { Writable } from "node:stream";
import { Type, type TSchema } from "typebox";
import { isContextOverflow } from "@mariozechner/pi-ai";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const URL_ = process.env.STB_GATEWAY_URL || "";
const TOKEN = process.env.STB_GATEWAY_TOKEN || "";

function toTypebox(s: any): TSchema {
  const opts: Record<string, unknown> = {};
  if (s?.description) opts.description = s.description;
  if (s?.default !== undefined) opts.default = s.default;
  if (Array.isArray(s?.enum)) return Type.Union(s.enum.map((v: any) => Type.Literal(v)), opts);
  if (Array.isArray(s?.anyOf)) return Type.Union(s.anyOf.map(toTypebox), opts);
  switch (s?.type) {
    case "object": {
      const props: Record<string, TSchema> = {};
      const req = new Set<string>(s.required || []);
      for (const [k, v] of Object.entries(s.properties || {})) props[k] = req.has(k) ? toTypebox(v) : Type.Optional(toTypebox(v));
      const ap = s.additionalProperties;   // 字典参数（键随便写，如任务的 tools：工具名 → 工具项）；没写的按不许多键
      if (ap && typeof ap === "object" && !Object.keys(props).length) return Type.Record(Type.String(), toTypebox(ap), opts);
      return Type.Object(props, { ...opts, additionalProperties: ap === true });
    }
    case "array": return Type.Array(toTypebox(s.items || {}), opts);
    case "integer": return Type.Integer(opts);
    case "number": return Type.Number(opts);
    case "boolean": return Type.Boolean(opts);
    case "null": return Type.Null(opts);
    case undefined: return Type.Any(opts);   // 没写类型（如 list[Any] 的 items: {}）：什么都收，交给工具自己校验
    default: return Type.String(opts);
  }
}

// 校验前先修几种能确定怎么修的参数格式（pi 的 prepareArguments，在按 schema 校验之前跑）：
// 1. 要数组或对象（且不收字符串）的地方给了字符串，字符串能解析成要的类型就换成解析后的值（如 questions 写成了 "[{...}]"）；
//    字符串把同一层后面的字段也包了进去（"[{...}], \"why\": \"…\"}"）时，拆出来放回这一层（声明过、又没写的字段）；
// 2. 字段名首尾多了空白或粘了半截标签（"intro\n"、"intro\n</parameter"），去掉后是声明过的字段、参数里又没有这个字段时改回来；
//    空字段名（""）丢掉；
// 3. 要字符串的地方给了带 label 或 text 的对象（选项写成 {label, description}），取 label（没有就取 text）；
// 4. 列表里每项多写了一个自己没声明、外层对象声明了的文字字段（题目里写了 why）：外层没写就用它（几项不同的用「；」连起来），写了就丢掉。
// 其他情况原样交给 pi 校验。修过哪些记在 repairs（键 = 工具名 + 修后的参数），execute 时随 /call 带给网关记日志；
// pi 校验时若又转换了别的值，两边对不上，只是这次不记日志，修复照样生效。
const repairs = new Map<string, string[]>();

function types(s: any): string[] {
  const out: string[] = Array.isArray(s?.type) ? [...s.type] : s?.type ? [s.type] : [];
  for (const x of Array.isArray(s?.anyOf) ? s.anyOf : []) out.push(...types(x));
  return out;
}

// 字符串 → 解析后的值 + 字符串里多包进来的同一层字段（解析不了返回 undefined）
function parseLoose(v: string): { value: any; rest: Record<string, any> } | undefined {
  try {
    return { value: JSON.parse(v), rest: {} };
  } catch { /* 再试：后面跟着同一层的其他字段 */ }
  try {
    const { __v, ...rest } = JSON.parse(`{"__v":${v}`);
    return __v === undefined ? undefined : { value: __v, rest };
  } catch {
    return undefined;
  }
}

// outer：最近一层外面的对象（它声明的字段 props、下层交上来的字段 got）；inList = 当前值在它的某个列表里
type Outer = { props: Record<string, any>; got: Record<string, any[]>; inList?: boolean };

function repair(v: any, s: any, path: string, fixed: string[], outer?: Outer): any {
  if (!s || typeof s !== "object") return v;
  const ts = types(s);
  if (typeof v === "string" && !ts.includes("string") && (ts.includes("array") || ts.includes("object"))) {
    const p = parseLoose(v);
    const isObj = p && p.value !== null && typeof p.value === "object" && !Array.isArray(p.value);
    if (p && ((Array.isArray(p.value) && ts.includes("array")) || (isObj && ts.includes("object")))) {
      fixed.push(`${path || "参数"}：字符串换成${Array.isArray(p.value) ? "数组" : "对象"}`);
      v = p.value;
      const rest = Object.keys(p.rest);
      if (rest.length && outer) {
        fixed.push(`${path}：字符串里多包的 ${rest.join("、")} 拆回这一层`);
        for (const k of rest) (outer.got[k] ??= []).push(p.rest[k]);
      }
    }
  }
  if (ts.includes("string") && !ts.includes("object") && v && typeof v === "object" && !Array.isArray(v)) {
    const k = ["label", "text"].find((x) => typeof v[x] === "string");
    if (k) {
      fixed.push(`${path}：对象换成它的 ${k}`);
      return v[k];
    }
  }
  const alt = (k: string) => (Array.isArray(s.anyOf) ? s.anyOf : []).find((x: any) => x?.[k])?.[k];
  if (Array.isArray(v)) {
    const items = s.items ?? alt("items");
    return items ? v.map((x, i) => repair(x, items, `${path}[${i}]`, fixed, outer && { ...outer, inList: true })) : v;
  }
  if (v === null || typeof v !== "object") return v;
  const props: Record<string, any> = s.properties ?? alt("properties") ?? {};
  const ap = s.additionalProperties && typeof s.additionalProperties === "object" ? s.additionalProperties : undefined;
  if (!Object.keys(props).length && !ap) return v;
  const own: Outer = { props, got: {} };
  const out: Record<string, any> = {};
  for (const [k, x] of Object.entries(v)) {
    const at = path ? `${path}.${k}` : k;
    let key = k;
    const name = k.trim().split(/[\s<]/)[0];
    if (!ap && !name) {
      fixed.push(`丢掉空字段名 ${JSON.stringify(at)}`);
      continue;
    }
    if (name !== k && name in props && !(name in v)) {
      key = name;
      fixed.push(`字段名 ${JSON.stringify(at)} 改成 ${JSON.stringify(name)}`);
    }
    if (!(key in props) && !ap && outer?.inList && typeof x === "string" && key in outer.props) {
      (outer.got[key] ??= []).push(x);
      fixed.push(`${at}：挪到外层`);
      continue;
    }
    const sub = key in props ? props[key] : ap;
    out[key] = sub ? repair(x, sub, path ? `${path}.${key}` : key, fixed, own) : x;
  }
  for (const [k, xs] of Object.entries(own.got)) {
    if (k in out || !(k in props)) {
      fixed.push(`${path ? `${path}.` : ""}${k}：${k in out ? "已经写了" : "没声明"}，多出来的丢掉`);
      continue;
    }
    const strs = xs.every((x) => typeof x === "string");
    const val = strs ? [...new Set(xs as string[])].join("；") : xs[0];
    out[k] = repair(val, props[k], path ? `${path}.${k}` : k, fixed, own);
  }
  return out;
}

async function api(path: string, init: RequestInit = {}) {
  const res = await fetch(URL_ + path, { ...init, headers: { "content-type": "application/json", authorization: `Bearer ${TOKEN}`, ...(init.headers || {}) } });
  const text = await res.text();
  if (!res.ok) throw new Error(`网关 ${path} 返回 ${res.status}: ${text.slice(0, 200)}`);
  try {
    return JSON.parse(text);
  } catch (e: any) {
    throw new Error(`网关 ${path} 的回复不是合法 JSON（${e?.message}）：${text.slice(0, 300)}`);
  }
}

// 网关回 ok: false 时给模型的文字：报错原文（类别 code 只记在网关日志），缺 error 时贴原始回复
function errorText(body: any): string {
  const e = body?.error;
  if (!e || typeof e !== "object" || !e.message) return `网关回了 ok: false 但没带 error 说明，原始回复：${JSON.stringify(body).slice(0, 300)}`;
  return String(e.message);
}

// 旧结果收起（手册 §4.4）：工具结果带 stale（网关回复的 stale，tool_kit.result 给的）的，只有最近 STALE_KEEP 个原样给模型，
// 更早的在每次调用模型前（context 事件）换成 stale；会话里存的是原样的结果（details.stb_stale 记着替换文字），续跑照样收起
const STALE_KEEP = 2;
function collapse(ms: any[]): any[] {
  const idx = ms.flatMap((m: any, i: number) => (m?.role === "toolResult" && typeof m?.details?.stb_stale === "string" ? [i] : []));
  const old = new Set(idx.slice(0, Math.max(0, idx.length - STALE_KEEP)));
  return old.size ? ms.map((m: any, i: number) => (old.has(i) ? { ...m, content: [{ type: "text", text: m.details.stb_stale }] } : m)) : ms;
}

const PAUSE_EXIT_CODE = 75;   // 运行器据此把运行标为 paused（规格 9.1）
let pauseRequested = false;
let turn = 0;   // 第几轮（每次模型调用前加一）：随 /call 带给网关，同一条回答里的几次调用相同（用法规范按轮给）

// 上下文超长的恢复（pi 0.73 的 json 模式）：模型报上下文超长时，pi 在 agent_end 之后压缩、再自动重试（settings 里 compaction 开着），
// 但 json 模式把第一个 agent_end 当成这次提示结束：停写 stdout、开始收尾，压缩与重试在后台照跑，事件不再写出、会话也不再记。
// pi 收尾时先等扩展处理完 session_shutdown（之后才断开会话）：这里等重试跑完再放行，其间的事件照 json 模式的格式由本扩展写到 stdout
// （pi 已把 process.stdout.write 改道到 stderr，这里用流本身的 write）。压缩失败就没有重试、也没别的事要做，进程随之退出：
// 压缩没成功、重试后又超长，都补一条带 errorMessage 的 compaction_end（willRetry: false），平台据此报「上下文超长、压缩后仍失败」。
// 正常结束时也有同样的竞争：pi 先跑完扩展的处理器、再把事件写到 stdout，json 模式的收尾（停写 stdout）可能恰好插在两者之间，
// 最后的 agent_end（队列落后多时还有更早的事件）就丢了。所以转写放在最后注册的处理器里，先让一拍（setImmediate）：
// 停写与 session_shutdown 在同一串微任务里发生，这一拍之后还没 detached，pi 自己随后照写；已 detached，pi 那边已停写，由这里写。
// 这要求本扩展的处理器在所有处理器之后（pi_command 里 -e toollog.ts 在前、本文件在后；toollog 的处理器都是同步写文件）。
const FORWARD = ["agent_start", "turn_start", "message_start", "message_update", "message_end",
  "tool_execution_start", "tool_execution_update", "tool_execution_end", "turn_end", "agent_end"];
const emit = (e: unknown) => Writable.prototype.write.call(process.stdout, JSON.stringify(e) + "\n");
let detached = false;     // pi 已停写 stdout（session_shutdown 之后）：事件改由本扩展写
let running = 0;          // 已开始、扩展还没收到 agent_end 的段数（pi 的事件队列可能比收尾慢一步）
let recovering = false;   // 最近一次 agent_end 收在上下文超长上：pi 会压缩并重试
let attempted = false;    // 这次超长已压缩重试过一次（同 pi：重试后又超长就不再重试，报压缩失败）
let stopping = false;     // 收到 SIGTERM / SIGHUP：照 pi 原样收尾退出，不等
let wake: (() => void) | undefined;
for (const s of ["SIGTERM", "SIGHUP"] as const) process.on(s, () => { stopping = true; wake?.(); });
const giveUp = (why: string) => emit({ type: "compaction_end", reason: "overflow", aborted: false, willRetry: false, errorMessage: why });
process.on("exit", () => { if (detached && recovering && !stopping) giveUp("压缩没有成功（压缩出错或没有可压缩的内容），没有重试"); });

export default async function (pi: ExtensionAPI) {
  if (!URL_ || !TOKEN) throw new Error("缺少 STB_GATEWAY_URL / STB_GATEWAY_TOKEN");
  const { tools, builtins = [], loaded = [], loadable = [], restored: restoredAtStart = "" } = await api("/tools");
  // 压缩后补回（手册 §4.10 第 5 步）：压缩完告诉网关，网关交回压缩前加载过的工具与 skill 的原文（续跑时 /tools 的 restored 给），
  // 之后每次调用模型前插在压缩摘要后面；内容与位置每次一样，前缀缓存不破
  let restored = String(restoredAtStart || "");
  const names: string[] = [...builtins];   // pi 自带的函数（平台工具 workspace）：任务列了，网关才在 builtins 里给
  // 按需加载（load-tool）：已加载的（loaded）与还没加载的（loadable）开跑时就全部注册并打开——pi 的一次运行开跑时把工具表快照下来，
  // 运行中注册的用不上；还没加载就调用，网关回「先用 load_tool 加载」。发给模型的只留任务 yaml 列的（before_provider_request），
  // 列表固定不变、前缀缓存不破，模型从 load_tool 的结果里知道怎么调用
  const sent = new Set<string>([...builtins, ...tools.map((t: any) => t.name)]);
  const register = (t: any) => {
    names.push(t.name);
    pi.registerTool({
      name: t.name,
      label: t.name,
      description: t.description,
      promptSnippet: t.description.slice(0, 120),
      parameters: toTypebox(t.parameters) as any,
      prepareArguments(args: any) {
        const fixed: string[] = [];
        const out = repair(args, t.parameters, "", fixed);
        if (!fixed.length) return args;
        repairs.set(t.name + JSON.stringify(out), fixed);
        return out;
      },
      async execute(id, params, signal) {
        const key = t.name + JSON.stringify(params);
        const repaired = repairs.get(key);
        repairs.delete(key);
        const body = await api("/call", { method: "POST", body: JSON.stringify({ name: t.name, arguments: params, turn, call_id: id, ...(repaired ? { repaired } : {}) }), signal });
        if (body.pause) pauseRequested = true;
        // D36：Joy 在运行中又发的私信（网关在回复里带上）：原生 steer，这一轮工具跑完、下一次模型调用之前作为用户消息送进会话
        for (const m of Array.isArray(body.messages) ? body.messages : []) {
          const imgs = (Array.isArray(m.images) ? m.images : []).map((i: any) => ({ type: "image" as const, data: String(i.data), mimeType: String(i.mimeType) }));
          pi.sendUserMessage(imgs.length ? [{ type: "text" as const, text: String(m.text) }, ...imgs] : String(m.text), { deliverAs: "steer" });
        }
        if (!body.ok) {
          return { content: [{ type: "text" as const, text: errorText(body) }], details: body.error ?? {}, isError: true };
        }
        const text = typeof body.result === "string" ? body.result : JSON.stringify(body.result);
        // 工具结果里的图片（生成图的预览、视频的缩略拼图）：网关放在 images 里，原样作为图片块交给模型
        const images = (Array.isArray(body.images) ? body.images : [])
          .filter((i: any) => typeof i?.data === "string" && typeof i?.mimeType === "string")
          .map((i: any) => ({ type: "image" as const, data: i.data as string, mimeType: i.mimeType as string }));
        const details = typeof body.result === "object" ? body.result : {};
        return { content: [{ type: "text" as const, text }, ...images], details: typeof body.stale === "string" ? { ...details, stb_stale: body.stale } : details };
      },
    });
  };
  for (const t of [...tools, ...loaded, ...loadable]) register(t);
  pi.on("before_provider_request", (event: any) => {
    const p = event.payload;
    if (!Array.isArray(p?.tools) || p.tools.every((t: any) => sent.has(t?.function?.name ?? t?.name))) return undefined;
    return { ...p, tools: p.tools.filter((t: any) => sent.has(t?.function?.name ?? t?.name)) };
  });
  pi.on("agent_start", async () => { running += 1; recovering = false; });   // 重试开始了：压缩已成功
  pi.on("message_start", async (e: any) => { if (e.message?.role === "user") attempted = false; });
  pi.on("message_end", async (e: any) => { if (e.message?.role === "assistant" && e.message.stopReason !== "error") attempted = false; });
  pi.on("context", async (e: any) => {
    const raw: any[] = Array.isArray(e?.messages) ? e.messages : [];
    const ms = collapse(raw);
    const i = ms.map((m: any) => m?.role).lastIndexOf("compactionSummary");
    if (!restored || i < 0) return ms === raw ? undefined : { messages: ms };
    const add = { role: "custom", customType: "stb-restored", content: restored, display: false, timestamp: ms[i].timestamp };
    return { messages: [...ms.slice(0, i + 1), add, ...ms.slice(i + 1)] };
  });
  pi.on("session_compact", async (e: any) => {
    try {
      restored = String((await api("/compacted", { method: "POST", body: "{}" })).text || "");
    } catch { /* 网关一时连不上：沿用上次的 */ }
    const c = e.compactionEntry ?? {};
    if (detached) emit({ type: "compaction_end", reason: recovering ? "overflow" : "threshold", aborted: false, willRetry: recovering,
      result: { summary: c.summary, firstKeptEntryId: c.firstKeptEntryId, tokensBefore: c.tokensBefore, details: c.details } });
  });
  pi.on("session_shutdown", async () => {
    detached = true;
    while ((running > 0 || recovering) && !stopping) await new Promise<void>((r) => { wake = r; });
  });
  // 防御：不管 pi 怎么配置，活动工具只能是网关给的这几个（加上 builtins 里的 pi 自带函数）
  pi.on("session_start", async () => { pi.setActiveTools(names); });
  pi.on("before_agent_start", async () => { pi.setActiveTools(names); });
  // 撤工具即时生效：每轮模型调用前向网关要最新的工具表（运行中从任务 yaml 撤掉的工具网关不再列出），模型这一轮就看不到它
  pi.on("turn_start", async () => {
    turn += 1;
    try {
      const now = await api("/tools");
      const live = new Set([...(now.builtins || []), ...[...(now.tools || []), ...(now.loaded || []), ...(now.loadable || [])].map((t: any) => t.name)]);
      pi.setActiveTools(names.filter((n) => live.has(n)));
    } catch { /* 网关一时连不上：沿用当前工具表，调用时网关照样会拒 */ }
  });
  // 暂停只在边界发生：暂停标志随本轮工具调用的 /call 回复带回；轮次结束（工具结果已落盘）就以 75 退出。
  // 这里不再额外请求网关：最后一轮的处理器若做网络等待，pi 会在事件写出前退出，agent_end 就丢了。
  pi.on("turn_end", async () => {
    if (pauseRequested) {
      process.stderr.write("stb: pause requested by runner, exiting at turn boundary\n");
      setTimeout(() => process.exit(PAUSE_EXIT_CODE), 300);
      await new Promise((r) => setTimeout(r, 10_000));   // 等 exit；不再进入下一轮
    }
  });
  // 转写 pi 停写之后的事件（见文件头）：必须最后注册
  for (const type of FORWARD) pi.on(type as any, async (e: any, ctx: any) => {
    if (!detached) await new Promise((r) => setImmediate(r));
    if (detached) emit(e);
    if (type !== "agent_end") return;
    running -= 1;
    const last = [...(e.messages ?? [])].reverse().find((m: any) => m.role === "assistant");
    const overflow = !!last && isContextOverflow(last, ctx?.model?.contextWindow ?? 0);
    if (overflow && attempted && detached) giveUp("压缩后重试，上下文仍然超长");
    recovering = overflow && !attempted;
    attempted ||= recovering;
    wake?.();
  });
}
