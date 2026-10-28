"""不加预算实验（规格 13 的性能上限）：把 max_seconds / max_model_tokens 提到配置上限，测研究循环的自然收敛点。

运行：uv run python tests/acceptance/unbounded_probe.py
结果写入 docs/acceptance/<日期>-unbounded.jsonl
"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import httpx
from stress_suite import TASKS, LAUNCHER, TOKEN, check, sample_machine, slow_stages, throughput

CASES = ["S3", "S5"]           # 修复前打满 900 s 的两题
BIG = {"max_seconds": 3600, "max_model_tokens": 400000, "max_searches": 12, "max_pages": 12, "max_documents": 6}

async def main():
    out = Path(__file__).resolve().parents[2] / "docs" / "acceptance" / (time.strftime("%Y-%m-%d") + "-unbounded.jsonl")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
        for tid in CASES:
            task = next(t for t in TASKS if t["id"] == tid)
            req = {**task["request"], "budget": BIG}
            t0 = time.time()
            r = await c.post(f"{LAUNCHER}/tasks", json=req); r.raise_for_status()
            jid = r.json()["task_id"]
            while True:
                v = (await c.get(f"{LAUNCHER}/tasks/{jid}", params={"wait": 60})).json()
                if v["status"] not in ("queued", "running"): break
            res = v.get("result") or {}
            ok, notes = check(task, res)
            u = res.get("usage") or {}
            rec = {"id": tid, "budget": BIG, "correct": ok, "notes": notes, "status": res.get("status"),
                   "outer_wall": round(time.time()-t0, 1), "usage": u, "slow_stages": slow_stages(u),
                   "throughput": throughput(u), "machine": sample_machine(t0), "task_id": jid,
                   "summary": (res.get("summary") or "")[:600], "claims": len(res.get("claims") or []),
                   "sources": len(res.get("sources") or []), "budget_exhausted": u.get("budget_exhausted")}
            with out.open("a") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps({k: rec[k] for k in ("id","correct","status","outer_wall","budget_exhausted")}, ensure_ascii=False), flush=True)
            print("  研究耗时", u.get("wall_seconds"), "s | 调用", u.get("model_calls"), "| token", u.get("tokens"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
