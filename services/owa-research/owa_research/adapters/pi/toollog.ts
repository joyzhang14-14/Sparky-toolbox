// 工具调用日志（来自 harness，原样复用）：每次工具调用/每轮回复/整段结束各记一行 JSONL
// 输出路径由环境变量 HARNESS_LOG 指定（默认 ./harness-toollog.jsonl）
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export default function (pi: any) {
  const path = process.env.HARNESS_LOG || "./harness-toollog.jsonl";
  mkdirSync(dirname(path), { recursive: true });
  const t0 = Date.now();
  const started = new Map<string, number>();
  const write = (rec: Record<string, unknown>) =>
    appendFileSync(path, JSON.stringify({ t: (Date.now() - t0) / 1000, ...rec }) + "\n");
  const clip = (v: unknown, n = 4000) => { const s = typeof v === "string" ? v : JSON.stringify(v); return s && s.length > n ? s.slice(0, n) + `…[+${s.length - n}]` : s; };

  pi.on("session_start", async () => write({ ev: "session_start", pid: process.pid }));
  pi.on("tool_execution_start", async (e: any) => {
    started.set(e.toolCallId, Date.now());
    write({ ev: "tool_start", id: e.toolCallId, tool: e.toolName, args: e.args });
  });
  pi.on("tool_execution_end", async (e: any) => {
    const ms = started.has(e.toolCallId) ? Date.now() - started.get(e.toolCallId)! : null;
    const text = (e.result?.content ?? []).map((c: any) => c.text ?? "").join("\n");
    write({ ev: "tool_end", id: e.toolCallId, tool: e.toolName, ms, error: !!e.isError, result: clip(text) });
  });
  pi.on("message_end", async (e: any) => {
    if (e.message?.role !== "assistant") return;
    const u = e.message.usage || {};
    const text = (e.message.content ?? []).filter((c: any) => c.type === "text").map((c: any) => c.text).join("\n");
    const calls = (e.message.content ?? []).filter((c: any) => c.type === "toolCall").map((c: any) => c.name);
    write({ ev: "assistant", input: u.input, output: u.output, calls, text: clip(text, 2000) });
  });
  pi.on("turn_end", async (e: any) => write({ ev: "turn_end", turn: e.turnIndex }));
  pi.on("agent_end", async () => write({ ev: "agent_end" }));
}
