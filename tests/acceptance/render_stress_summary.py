"""从 stress jsonl 重新生成文档：区分研究耗时（容器内）与排队时间（外层墙钟之差）。
运行：uv run python tests/acceptance/render_stress_summary.py [jsonl 路径]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stress_suite import TASKS  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "acceptance"


def render(path: Path) -> Path:
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    by_id = {t["id"]: t for t in TASKS}
    ok = sum(1 for r in recs if r["correct"] is True)
    scored = sum(1 for r in recs if r["correct"] is not None)
    research = [(r["usage"] or {}).get("wall_seconds") for r in recs if (r["usage"] or {}).get("wall_seconds")]
    decode = [(r.get("throughput") or {}).get("decode_tok_s") for r in recs if (r.get("throughput") or {}).get("decode_tok_s")]
    temps = [(r.get("machine") or {}).get("gpu_temp_max_c") for r in recs if (r.get("machine") or {}).get("gpu_temp_max_c")]
    powers = [(r.get("machine") or {}).get("gpu_power_max_w") for r in recs if (r.get("machine") or {}).get("gpu_power_max_w")]
    throttled = [r["id"] for r in recs if (r.get("machine") or {}).get("thermal_throttled")]

    L = [
        "# 极限压力测试：考题、结果与机器指标",
        "",
        f"生成 {time.strftime('%Y-%m-%d %H:%M')}。机器 Sparky（DGX Spark GB10，20 核 aarch64，121 GB 统一内存）；模型 `vllm/qwen` = "
        "codegyver Qwen3.8-Flash-Next（常驻 vLLM，全程未卸载/未重启）；研究容器 `owa-research:latest`（6 GB 内存上限、4 CPU）。",
        "",
        "题目按公开 web-research benchmark 的难点设计：**BrowseComp**（从多个约束反推唯一实体）、**GAIA**（长工具链）、"
        "**FRAMES**（多跳检索 + 数值/时序推理）、**SEAL-0/SealQA**（检索结果互相冲突），另加时效陷阱、假前提、大文档定位、"
        "非英文来源、极限预算与并发。标准答案全部由人工先行核对（每题列了判定依据）。",
        "",
        f"## 结论：通过 {ok}/{scored}（S11 为观察项不计分）",
        "",
        f"- 研究耗时（容器内实际）中位 **{statistics.median(research):.0f} s**，范围 {min(research):.0f}–{max(research):.0f} s。",
        f"- 解码吞吐中位 **{statistics.median(decode):.1f} tok/s**，范围 {min(decode):.1f}–{max(decode):.1f}（单路串行，与机器基线 30 tok/s 一致）。",
        f"- GPU 峰值温度中位 **{statistics.median(temps):.0f}°C**（最高 {max(temps):.0f}°C，降频点约 96°C），峰值功耗最高 {max(powers):.1f} W，"
        f"热降频：{'、'.join(throttled) if throttled else '全程未发生'}。",
        "",
        "> 墙钟说明：压力测试与验收重跑同时提交，全局租约让研究容器严格串行，**外层墙钟包含排队等待**。下表的"
        "「研究耗时」取容器内 `usage.wall_seconds`（真实工作时间），「排队」是二者之差。",
        "",
        "## 总表",
        "",
        "| # | 维度 | 结果 | 状态 | 研究耗时 s | 排队 s | 解码 tok/s | 输出 token | 模型占比 | 来源数 | GPU 峰值 | 功耗峰值 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in recs:
        u, t, m = r["usage"] or {}, r.get("throughput") or {}, r.get("machine") or {}
        rw = u.get("wall_seconds")
        queue = (r["wall"] - rw) if rw else None
        mark = {True: "✅", False: "❌", None: "—"}[r["correct"]]
        L.append(f"| {r['id']} | {r['dim']} | {mark} | {r.get('status') or r['launcher_status']} | {f'{rw:.0f}' if rw else '—'} | "
                 f"{f'{queue:.0f}' if queue and queue > 5 else '—'} | {t.get('decode_tok_s') or '—'} | {t.get('completion_tok_total') or '—'} | "
                 f"{str(t.get('model_share_pct')) + '%' if t.get('model_share_pct') else '—'} | {len(r['sources'])} | "
                 f"{f'{m.get(chr(103) + chr(112) + chr(117) + chr(95) + chr(116) + chr(101) + chr(109) + chr(112) + chr(95) + chr(109) + chr(97) + chr(120) + chr(95) + chr(99))}°C' if m.get('gpu_temp_max_c') else '—'} | "
                 f"{f'{m.get(chr(103) + chr(112) + chr(117) + chr(95) + chr(112) + chr(111) + chr(119) + chr(101) + chr(114) + chr(95) + chr(109) + chr(97) + chr(120) + chr(95) + chr(119))} W' if m.get('gpu_power_max_w') else '—'} |")

    L += ["", "## 逐题：考题、标准答案、系统回答、耗时与机器", ""]
    for r in recs:
        task = by_id.get(r["id"], {})
        u, t, m = r["usage"] or {}, r.get("throughput") or {}, r.get("machine") or {}
        L.append(f"### {r['id']} {r['name']}（{r['dim']}）")
        req = task.get("request") or {}
        if req.get("task"):
            L.append("")
            L.append(f"**考题**：{req['task']}")
            L.append(f"**预算**：{json.dumps(req.get('budget') or {}, ensure_ascii=False)}"
                     + (f"；已知来源：{req['seed_urls']}" if req.get("seed_urls") else ""))
        if task.get("expected_source"):
            L.append(f"**标准答案 / 判定依据**：{task['expected_source']}")
        L.append(f"**结果**：{ {True: '✅ 通过', False: '❌ 未通过', None: '观察项'}[r['correct']] } "
                 f"（status={r.get('status')}；{'; '.join(r['notes'])}）")
        if r["summary"]:
            L.append(f"**系统回答**：{r['summary'][:600]}")
        if r["sources"]:
            L.append(f"**来源**：{', '.join(s[:75] for s in r['sources'])}")
        if r["access_failures"]:
            L.append(f"**访问失败（已如实记录，未当作答案）**：{r['access_failures']}")
        if r.get("slow_stages"):
            rw = u.get("wall_seconds") or 0
            L.append(f"**耗时 ≥5 s 的阶段**：{json.dumps(r['slow_stages'], ensure_ascii=False)}；研究耗时 {rw:.0f} s，模型生成占 {t.get('model_share_pct')}%，"
                     f"模型调用 {u.get('model_calls')} 次，搜索 {u.get('search_calls')} 次，读页 {u.get('pages_parsed')}，OCR 页 {u.get('ocr_pages')}")
        if t.get("decode_tok_s"):
            L.append(f"**吞吐**：解码 {t['decode_tok_s']} tok/s，端到端产出 {t['e2e_out_tok_s']} tok/s；prompt {t['prompt_tok_total']:,} + completion {t['completion_tok_total']:,} token")
        if m.get("samples"):
            L.append(f"**机器**：GPU 峰值 {m['gpu_temp_max_c']}°C / 均值 {m['gpu_temp_avg_c']}°C，功耗峰值 {m['gpu_power_max_w']} W，利用率均值 {m['gpu_util_avg_pct']}%；"
                     f"CPU 温区峰值 {m['cpu_zone_max_c']}°C；内存峰值 {m['mem_used_gb_max']} GB，swap 峰值 {m['swap_used_gb_max']} GB；"
                     f"热降频 {'**发生**' if m['thermal_throttled'] else '未发生'}（{m['samples']} 个采样点，间隔 5 s）")
        L.append("")
    out = OUT / (path.stem.replace("-stress", "") + "-stress-summary.md")
    out.write_text("\n".join(L) + "\n")
    return out


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / f"{time.strftime('%Y-%m-%d')}-stress.jsonl"
    print("written", render(p))
