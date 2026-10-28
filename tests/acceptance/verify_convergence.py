"""收敛判据回归：同一批题跑一遍，看是否在合理轮数内收敛、claims 是否非空。
运行：uv run python tests/acceptance/verify_convergence.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from stress_suite import LAUNCHER, TASKS, TOKEN  # noqa: E402

CASES = ["S1", "S5", "S9"]


async def main() -> None:
    out = Path(__file__).resolve().parents[2] / "docs" / "acceptance" / f"{time.strftime('%Y-%m-%d')}-convergence.jsonl"
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
        for cid in CASES:
            task = next(t for t in TASKS if t["id"] == cid)
            r = await c.post(f"{LAUNCHER}/tasks", json=task["request"])
            r.raise_for_status()
            tid = r.json()["task_id"]
            while True:
                v = (await c.get(f"{LAUNCHER}/tasks/{tid}", params={"wait": 60})).json()
                if v["status"] not in ("queued", "running"):
                    break
            res = v.get("result") or {}
            u = res.get("usage") or {}
            trace = Path.home() / "sparky-toolbox" / "data" / "owa" / "artifacts" / tid / "trace.jsonl"
            conv = next((json.loads(l) for l in trace.read_text().splitlines() if '"converged"' in l), None) if trace.exists() else None
            rec = {"id": cid, "status": res.get("status"), "research_s": u.get("wall_seconds"), "model_calls": u.get("model_calls"),
                   "claims": len(res.get("claims") or []), "sources": len(res.get("sources") or []),
                   "located": sum(1 for x in res.get("claims") or [] if x.get("locators")),
                   "converged": bool(conv), "conv_detail": conv, "budget": u.get("budget_exhausted"), "task_id": tid}
            with out.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps({k: rec[k] for k in ("id", "status", "research_s", "model_calls", "claims", "located", "converged", "budget")}, ensure_ascii=False), flush=True)


asyncio.run(main())
