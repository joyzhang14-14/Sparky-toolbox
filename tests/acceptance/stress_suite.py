"""极限压力测试（参考 BrowseComp / GAIA / FRAMES / SEAL-0 的难点设计）。

难点维度（每题标注）：
- inversion：从多个约束反推唯一实体（BrowseComp 的出题法），需要多次搜索交叉验证
- multihop：多跳检索 + 数值/时序推理（FRAMES）
- conflict：检索结果互相矛盾，正确行为是报告分歧而不是挑一个（SEAL-0）
- temporal：事件尚未发生 / 极新，正确行为是说"尚未公布"而不是编造
- false-premise：问题预设了不存在的事实，正确行为是指出前提错误
- needle：大文档里找一个具体数字 + 位置
- multilingual：答案只在非英文来源里
- completeness：一次要列全 10 项，考输出完整性
- budget-floor / budget-ceiling：极小 / 极大预算下的行为
- concurrency：并发提交，考全局租约（最多 OWA_MAX_RUNNING 个，D21）与容器回收

判定规则写在每题的 `check` 里：`must_any`（必须命中的字符串组）、`must_not`（不得出现）、
`must_behave`（行为判定函数名）。标准答案均由人工核对过（来源见 expected_source）。

运行：uv run python tests/acceptance/stress_suite.py [--only S1,S5] [--runs 1]
前提：owa-launcher 在 127.0.0.1:18081 运行。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import date
from pathlib import Path

import httpx

LAUNCHER = "http://127.0.0.1:18081"
from control_center import env  # noqa: E402
TOKEN = env.get("OWA_LAUNCHER_TOKEN")   # owa-launcher 的令牌在仓库 .env
OUT = Path(__file__).resolve().parents[2] / "docs" / "acceptance"

TASKS = [
    {
        "id": "S1", "dim": "inversion", "name": "多约束反推实体（Ig Nobel 土壤奖）",
        "request": {"task": "At the 2026 Ig Nobel Prize ceremony, one prize was awarded for research in which the authors buried about 1000 identical pairs of underpants in more than 25 countries and dug them up two months later. Which prize category was it, who were the winners, and which countries are they from?",
                    "required_outputs": ["prize category", "winner names", "countries"], "budget": {"max_searches": 6, "max_pages": 6, "max_seconds": 900}},
        "must_any": [["Soil Science", "soil science", "土壤"], ["Bender"], ["Heijden"]],
        "expected_source": "improbable.com/ig/winners/ — Soil Science Prize: Franz Bender, Marcel van der Heijden, Atlant Bieri, Pia Viviani (Switzerland/Netherlands/Germany)",
    },
    {
        "id": "S2", "dim": "temporal", "name": "尚未发生的事（2026 诺贝尔物理奖）",
        "request": {"task": "Who won the 2026 Nobel Prize in Physics, and for what discovery?",
                    "budget": {"max_searches": 5, "max_pages": 5, "max_seconds": 900}},
        "must_behave": "not_yet_announced",
        "must_not": [r"(?i)the 2026 Nobel Prize in Physics (was|is) awarded to [A-Z]"],
        "expected_source": "nobelprize.org — 2026 物理奖 10 月 6 日才公布；正确行为是说明尚未公布",
    },
    {
        "id": "S3", "dim": "false-premise", "name": "假前提（NVIDIA 收购 Arm 的附注）",
        "request": {"task": "In NVIDIA's fiscal year 2025 Form 10-K, what does the note on the completed acquisition of Arm Holdings say about the purchase price allocation?",
                    "budget": {"max_searches": 5, "max_pages": 5, "max_seconds": 900}},
        "must_behave": "false_premise_rejected",
        "must_not": [r"(?i)purchase price allocation for (the )?Arm (Holdings )?(acquisition|was)"],
        "expected_source": "NVIDIA 收购 Arm 于 2022 年 2 月终止，FY2025 10-K 无此附注；正确行为是指出前提不成立",
    },
    {
        "id": "S4", "dim": "multihop", "name": "多跳数值（研发费用占收入比对比）",
        "request": {"task": "Compare R&D expense as a percentage of revenue: NVIDIA in fiscal 2025 (ended January 26, 2025) versus Apple in fiscal 2024 (ended September 28, 2024). Which is higher, and by how many percentage points? Show the four underlying figures with units.",
                    "required_outputs": ["NVIDIA R&D and revenue", "Apple R&D and net sales", "two percentages", "difference in pp"], "budget": {"max_searches": 6, "max_pages": 6, "max_seconds": 900}},
        "must_any": [["12,914", "12914"], ["130,497", "130497"], ["31,370", "31370"], ["391,035", "391035"], ["NVIDIA", "NVDA"]],
        "expected_source": "NVDA FY2025 R&D 12,914 / rev 130,497 = 9.90%；AAPL FY2024 R&D 31,370 / sales 391,035 = 8.02%；NVIDIA 高约 1.9pp",
    },
    {
        "id": "S5", "dim": "conflict", "name": "来源冲突（土星卫星数）",
        "request": {"task": "How many confirmed moons does Saturn have as of September 2026? Different authorities report different numbers — report each number with its source and date, and explain why they differ.",
                    "required_outputs": ["each number", "source", "reason for difference"], "budget": {"max_searches": 6, "max_pages": 6, "max_seconds": 900}},
        "must_behave": "reports_disagreement",
        "must_any": [["274", "285", "293"]],
        "expected_source": "NASA 274 / IAU MPC 285（2026-03）/ Wikipedia 293（2026-06）：更新时点不同，非科学争议",
    },
    {
        "id": "S6", "dim": "needle", "name": "121 页 10-K 里找脚注数字",
        "request": {"task": "In Apple's fiscal 2024 Form 10-K PDF, find the total of future payments under unconditional purchase obligations with a remaining term in excess of one year as of September 28, 2024. Give the amount with unit, the note number, and the PDF page number.",
                    "seed_urls": ["https://s2.q4cdn.com/470004039/files/doc_earnings/2024/q4/filing/10-Q4-2024-As-Filed.pdf"],
                    "required_outputs": ["total amount", "note number", "PDF page"], "budget": {"max_searches": 3, "max_pages": 6, "max_documents": 2, "max_seconds": 1200}},
        "must_any": [["11,226", "11226"], ["Note 12", "note 12"]],
        "expected_source": "Apple FY2024 10-K，Note 12，PDF 第 48 页（印刷页 45）：Total $11,226 million",
    },
    {
        "id": "S7", "dim": "multilingual", "name": "非英文来源（日本人口推计）",
        "request": {"task": "日本の総務省統計局が公表している最新の人口推計によると、日本の総人口は何人か。公表年月と、その数字が載っている統計局のページの URL を示すこと。",
                    "required_outputs": ["総人口", "公表年月", "総務省統計局の URL"], "budget": {"max_searches": 6, "max_pages": 6, "max_seconds": 900}},
        "must_any": [["stat.go.jp"], ["万人", "million", "人口"]],
        "expected_source": "stat.go.jp 人口推計（月報）；数字随月份变化，判定看是否给出 stat.go.jp 来源与具体数字",
    },
    {
        "id": "S8", "dim": "budget-floor", "name": "极限预算下限（60 秒）",
        "request": {"task": "What is the capital of Australia?", "budget": {"max_seconds": 60, "max_searches": 1, "max_pages": 1}},
        "must_behave": "graceful_under_floor",
        "expected_source": "不求答对：必须返回结构化结果（complete/partial/blocked），不崩溃、不超时挂死",
    },
    {
        "id": "S9", "dim": "completeness", "name": "一次列全 10 项",
        "request": {"task": "List all ten 2026 Ig Nobel Prize categories and, for each, at least one winner's name. Output all ten.",
                    "required_outputs": ["10 categories", "one winner each"], "budget": {"max_searches": 5, "max_pages": 5, "max_seconds": 1200}},
        "must_any": [["Biomechanics"], ["Economics"], ["Chemistry"], ["Medicine"], ["Biology"], ["Physics"], ["Technology"], ["Olfaction"], ["Peace"], ["Soil"]],
        "partial_credit": True,
        "expected_source": "improbable.com/ig/winners/ — Biomechanics, Economics, Chemistry, Medicine, Biology, Physics, Technology, Olfaction, Peace, Soil Science",
    },
    {
        "id": "S10", "dim": "multihop", "name": "跨条目交叉（两个奖的共同国家）",
        "request": {"task": "Among the 2026 Ig Nobel Prizes, compare the author country lists of the Physics Prize (splash-free urinal) and the Technology Prize (mosquito proboscises as 3D printing nozzles). Which countries appear in both lists?",
                    "required_outputs": ["countries in both lists"], "budget": {"max_searches": 5, "max_pages": 5, "max_seconds": 900}},
        "must_any": [["Canada"], ["United States", "USA", "US"], ["China"]],
        "expected_source": "Physics: Canada, USA, China, Saudi Arabia；Technology: China, Canada, USA, Egypt → 交集 Canada, USA, China",
    },
    {
        "id": "S11", "dim": "budget-ceiling", "name": "超长预算下的开放题（30 分钟）",
        "request": {"task": "Survey the state of open-source self-hosted metasearch engines in 2026: name at least three actively maintained projects, their licenses, and one distinguishing feature each, with sources.",
                    "required_outputs": ["3 projects", "licenses", "distinguishing features"], "budget": {"max_searches": 12, "max_pages": 15, "max_seconds": 1800}},
        "must_any": [["SearXNG", "searxng"]],
        "expected_source": "不设唯一答案：看是否有效利用大预算（多来源、有引用），以及是否在答完后收尾而不是空转",
    },
]


def check(task: dict, res: dict) -> tuple[bool | None, list[str]]:
    text = (res.get("summary") or "") + " " + " ".join(c.get("text", "") for c in res.get("claims", [])) + " " + " ".join(res.get("missing_information") or [])
    notes: list[str] = []
    ok: bool | None = None

    if "must_any" in task:
        hits = [any(a.lower() in text.lower() for a in group) for group in task["must_any"]]
        n, total = sum(hits), len(hits)
        notes.append(f"关键值 {n}/{total}")
        ok = (n >= max(8, total - 2)) if task.get("partial_credit") else all(hits)
    for pat in task.get("must_not", []):
        if re.search(pat, text):
            notes.append(f"出现了不该有的断言: {pat}")
            ok = False
    behave = task.get("must_behave")
    if behave == "not_yet_announced":
        said = any(k in text.lower() for k in ("not yet", "has not been announced", "will be announced", "scheduled", "尚未", "未公布", "not been awarded"))
        named = bool(re.search(r"(?i)2026 Nobel Prize in Physics (was )?awarded to ([A-Z][a-z]+ [A-Z])", text))
        ok = said and not named
        notes.append(f"说明尚未公布={said}；编造得主={named}")
    elif behave == "false_premise_rejected":
        rejected = any(k in text.lower() for k in ("terminated", "did not complete", "was not completed", "no such", "not find", "abandoned", "终止", "未完成", "不存在", "no note", "does not contain"))
        ok = rejected
        notes.append(f"指出前提不成立={rejected}")
    elif behave == "reports_disagreement":
        nums = {n for n in re.findall(r"\b(274|285|293)\b", text)}
        has_dis = len(nums) >= 2 or bool(res.get("disagreements")) or any(k in text.lower() for k in ("differ", "disagree", "分歧", "不同"))
        ok = (ok is not False) and has_dis and bool(nums)
        notes.append(f"报告分歧={has_dis}，出现的数字={sorted(nums)}，disagreements 字段={len(res.get('disagreements') or [])}")
    elif behave == "graceful_under_floor":
        ok = res.get("status") in ("complete", "partial", "blocked") and isinstance(res.get("usage"), dict)
        notes.append(f"返回结构化结果={ok}，status={res.get('status')}")
    if task["id"] == "S11":
        u = res.get("usage") or {}
        notes.append(f"用了 {u.get('wall_seconds', 0):.0f}/1800 s，{len(res.get('sources') or [])} 个来源，{u.get('search_calls')} 次搜索")
    return ok, notes


async def submit(client: httpx.AsyncClient, request: dict) -> str:
    r = await client.post(f"{LAUNCHER}/tasks", json=request)
    r.raise_for_status()
    return r.json()["task_id"]


async def wait(client: httpx.AsyncClient, tid: str) -> dict:
    while True:
        v = (await client.get(f"{LAUNCHER}/tasks/{tid}", params={"wait": 60})).json()
        if v["status"] not in ("queued", "running"):
            return v


GPU_CSV = Path.home() / "monitor" / "gpu.csv"
SYS_CSV = Path.home() / "monitor" / "sys.csv"


def sample_machine(since_ts: float) -> dict:
    """从常驻 monitor.sh 的 CSV 里取本题运行期间的 GPU 温度/功耗/利用率与内存、CPU 温度峰值。"""
    def rows(path: Path, n: int) -> list[list[str]]:
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            return []
        out = []
        for line in reversed(lines[-4000:]):
            parts = line.split(",")
            try:
                ts = time.mktime(time.strptime(parts[0][:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, IndexError):
                continue
            if ts < since_ts:
                break
            if len(parts) >= n:
                out.append(parts)
        return out

    g = rows(GPU_CSV, 11)
    sy = rows(SYS_CSV, 12)
    num = lambda xs: [float(x) for x in xs if x not in ("", "N/A")]  # noqa: E731
    temps, powers, utils = num(r[1] for r in g), num(r[2] for r in g), num(r[10] for r in g)
    cpu_temps = num(max(r[1:8], key=lambda v: float(v or 0)) for r in sy) if sy else []
    throttled = any(r[6] != "Not Active" or r[7] != "Not Active" for r in g)
    return {
        "samples": len(g),
        "gpu_temp_max_c": max(temps) if temps else None, "gpu_temp_avg_c": round(sum(temps) / len(temps), 1) if temps else None,
        "gpu_power_max_w": max(powers) if powers else None, "gpu_util_avg_pct": round(sum(utils) / len(utils)) if utils else None,
        "cpu_zone_max_c": max(cpu_temps) if cpu_temps else None,
        "thermal_throttled": throttled,
        "mem_used_gb_max": max(num(r[9] for r in sy)) if sy else None, "swap_used_gb_max": max(num(r[11] for r in sy)) if sy else None,
    }


def throughput(u: dict) -> dict:
    """token 吞吐：生成速度（completion / 模型总时长）与端到端速度。"""
    st = u.get("stage_seconds") or {}
    tok = u.get("tokens") or {}
    model_s = st.get("model_total") or 0
    wall = u.get("wall_seconds") or 0
    comp, prompt = tok.get("completion") or 0, tok.get("prompt") or 0
    return {"decode_tok_s": round(comp / model_s, 1) if model_s else None,
            "e2e_out_tok_s": round(comp / wall, 1) if wall else None,
            "prompt_tok_total": prompt, "completion_tok_total": comp,
            "model_share_pct": round(model_s / wall * 100) if wall else None}


def slow_stages(u: dict, threshold: float = 5.0) -> dict:
    """只保留耗时 >= threshold 秒的阶段（几秒的噪音不记）。"""
    return {k: round(v, 1) for k, v in sorted((u.get("stage_seconds") or {}).items(), key=lambda kv: -kv[1]) if v >= threshold and k != "model_total"}


async def run_one(client: httpx.AsyncClient, task: dict, run: int) -> dict:
    t0 = time.time()
    view = await wait(client, await submit(client, task["request"]))
    res = view.get("result") or {}
    ok, notes = check(task, res)
    u = res.get("usage") or {}
    return {"id": task["id"], "dim": task["dim"], "name": task["name"], "run": run, "correct": ok, "status": res.get("status"),
            "launcher_status": view["status"], "wall": round(time.time() - t0, 1), "notes": notes, "error": view.get("error"),
            "usage": u, "slow_stages": slow_stages(u), "throughput": throughput(u), "machine": sample_machine(t0),
            "summary": (res.get("summary") or "")[:800], "sources": [s.get("url") for s in res.get("sources", [])][:8],
            "claims": len(res.get("claims") or []), "disagreements": len(res.get("disagreements") or []),
            "access_failures": [(f["url"][:60], f["error"]["code"]) for f in res.get("access_failures", [])][:5], "task_id": view.get("task_id")}


async def concurrency_probe(client: httpx.AsyncClient, n: int = 4) -> dict:
    """S12 并发压力：同时提交 n 个任务，检查同时运行的容器不超过 launcher 的上限（MAX_RUNNING，D21 起为 2）与容器回收。"""
    max_running = int((await client.get("/health")).json().get("max_running", 1))
    req = {"task": "What is the capital of France?", "budget": {"max_seconds": 120, "max_searches": 1, "max_pages": 1}}
    t0 = time.time()
    ids = await asyncio.gather(*(submit(client, req) for _ in range(n)))
    peak = 0
    done: list[dict] = []

    async def watch() -> None:
        nonlocal peak
        while len(done) < n:
            proc = await asyncio.create_subprocess_shell("docker ps --format '{{.Names}}' | grep -c owa-research || true",
                                                         stdout=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            peak = max(peak, int((out or b"0").decode().strip() or 0))
            await asyncio.sleep(2)

    watcher = asyncio.ensure_future(watch())
    for tid in ids:
        done.append(await wait(client, tid))
    watcher.cancel()
    proc = await asyncio.create_subprocess_shell("docker ps -a --format '{{.Names}}' | grep -c owa-research || true", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    leaked = int((out or b"0").decode().strip() or 0)
    return {"id": "S12", "dim": "concurrency", "name": f"并发提交 {n} 个任务", "run": 1,
            "correct": peak <= max_running and leaked == 0 and all(d["status"] == "done" for d in done),
            "notes": [f"同时运行的研究容器峰值={peak}（应 ≤ {max_running}）", f"残留容器={leaked}（应为 0）", f"全部完成={[d['status'] for d in done]}"],
            "wall": round(time.time() - t0, 1), "usage": {}, "slow_stages": {}, "throughput": {}, "machine": sample_machine(t0), "summary": "", "sources": [],
            "claims": 0, "disagreements": 0, "access_failures": [], "task_id": None, "status": None, "launcher_status": "done", "error": None}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--skip-concurrency", action="store_true")
    args = ap.parse_args()
    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    day = date.today().isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f"{day}-stress.jsonl"
    records: list[dict] = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as client:
        for task in TASKS:
            if only and task["id"] not in only:
                continue
            for i in range(args.runs):
                rec = await run_one(client, task, i + 1)
                records.append(rec)
                with log.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(json.dumps({k: rec[k] for k in ("id", "dim", "run", "correct", "status", "wall", "notes")}, ensure_ascii=False), flush=True)
        if not args.skip_concurrency and (not only or "S12" in only):
            rec = await concurrency_probe(client)
            records.append(rec)
            with log.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps({k: rec[k] for k in ("id", "correct", "wall", "notes")}, ensure_ascii=False), flush=True)
    write_summary(records, OUT / f"{day}-stress-summary.md")


def write_summary(records: list[dict], path: Path) -> None:
    ok = sum(1 for r in records if r["correct"] is True)
    scored = sum(1 for r in records if r["correct"] is not None)
    lines = ["# 极限压力测试：考题、结果与机器指标", "",
             f"生成 {time.strftime('%Y-%m-%d %H:%M')}；机器 Sparky（DGX Spark GB10，121 GB 统一内存）；模型 `vllm/qwen` = codegyver Qwen3.8-Flash-Next（常驻 vLLM，全程未重启）；",
             "题目设计参考 BrowseComp（多约束反推唯一实体）、GAIA（长工具链）、FRAMES（多跳数值/时序）、SEAL-0（检索结果互相冲突），另加时效陷阱、假前提、大文档定位、非英文来源、极限预算与并发。",
             "", f"**通过 {ok}/{scored}**（S11 为观察项不计分）。耗时只列 ≥5 秒的阶段；温度取该题运行期间 `~/monitor/gpu.csv` / `sys.csv` 的采样。", "",
             "## 总表", "",
             "| # | 维度 | 结果 | 状态 | 墙钟 s | 解码 tok/s | 输出 token | 模型占比 | GPU 峰值温度 | GPU 峰值功耗 | 降频 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        t, m = r.get("throughput") or {}, r.get("machine") or {}
        mark = {True: "✅", False: "❌", None: "—"}[r["correct"]]
        lines.append(f"| {r['id']} | {r['dim']} | {mark} | {r.get('status') or r['launcher_status']} | {r['wall']:.0f} | {t.get('decode_tok_s') or '—'} | "
                     f"{t.get('completion_tok_total') or '—'} | {str(t.get('model_share_pct') or '—') + '%'} | {m.get('gpu_temp_max_c') or '—'}°C | "
                     f"{m.get('gpu_power_max_w') or '—'} W | {'是' if m.get('thermal_throttled') else '否'} |")
    lines += ["", "## 逐题：考题、标准答案、结果、耗时构成", ""]
    by_id = {t["id"]: t for t in TASKS}
    for r in records:
        task = by_id.get(r["id"], {})
        m = r.get("machine") or {}
        t = r.get("throughput") or {}
        lines.append(f"### {r['id']} {r['name']}（{r['dim']}）")
        if task.get("request", {}).get("task"):
            lines.append(f"**考题**：{task['request']['task']}")
            b = task["request"].get("budget") or {}
            lines.append(f"**预算**：{json.dumps(b, ensure_ascii=False)}" + (f"；seed_urls：{task['request']['seed_urls']}" if task["request"].get("seed_urls") else ""))
        if task.get("expected_source"):
            lines.append(f"**标准答案/判定依据**：{task['expected_source']}")
        lines.append(f"**结果**：{ {True: '通过', False: '未通过', None: '观察项'}[r['correct']] }；status={r.get('status')}；判定 {'; '.join(r['notes'])}")
        if r["summary"]:
            lines.append(f"**系统回答**：{r['summary'][:500]}")
        if r["sources"]:
            lines.append(f"**来源**：{', '.join(u[:70] for u in r['sources'])}")
        if r["access_failures"]:
            lines.append(f"**访问失败**：{r['access_failures']}")
        u = r["usage"] or {}
        if r.get("slow_stages"):
            lines.append(f"**耗时 ≥5 s 的阶段**：{json.dumps(r['slow_stages'], ensure_ascii=False)}（总墙钟 {r['wall']:.0f} s，模型生成占 {t.get('model_share_pct')}%）")
        if t.get("decode_tok_s"):
            lines.append(f"**吞吐**：解码 {t['decode_tok_s']} tok/s；端到端产出 {t['e2e_out_tok_s']} tok/s；prompt {t['prompt_tok_total']} + completion {t['completion_tok_total']} token；模型调用 {u.get('model_calls')} 次")
        if m.get("samples"):
            lines.append(f"**机器**：GPU 温度 峰值 {m['gpu_temp_max_c']}°C / 均值 {m['gpu_temp_avg_c']}°C，功耗峰值 {m['gpu_power_max_w']} W，利用率均值 {m['gpu_util_avg_pct']}%，"
                         f"CPU 温区峰值 {m['cpu_zone_max_c']}°C，内存峰值 {m['mem_used_gb_max']} GB，swap 峰值 {m['swap_used_gb_max']} GB，热降频 {'是' if m['thermal_throttled'] else '否'}（{m['samples']} 个采样点）")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")
    print("written", path)


if __name__ == "__main__":
    asyncio.run(main())
