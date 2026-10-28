"""browser-script 的紧急任务：登记成平台一次性动态监听的监听程序（control_center/monitors.py 起、监督它：以 sparky 跑、宿主 listen_host.py、崩了按等待重起）。

watch(ctx, emit)：ctx["watch"] 是登记时给的（script、rel、sha、kind、at、interval、profile、domains，另有 id、name），ctx["listen"] 是监听实例名，
ctx["state_dir"] 是它的状态目录（结果 RESULT、开始实跑的标记 LIVE_MARK、截图 SHOTS 都在这里）。
- 定时（kind at）：等到执行时刻前 LEAD_S 秒（起来时已过执行时刻 MISSED_S 秒以上就记 missed、不补跑）→ emit fire（平台入队私信运行，主模型并行加载）
  → 开浏览器容器，脚本等到执行时刻整点实跑。
- 条件（kind watch）：开一个浏览器容器（以 browser-script 的名义、属于这个监听，请 stbcc-docker 代起），每 interval 秒跑一次 check()：
  为真 → emit fire → 回到 start_url 直接实跑；页面出错、容器退出就关掉容器、下次重开接着查，连续出错每 WATCH_ERRORS_NOTIFY 次私信 Joy 一次。
- 实跑：脚本内容哈希要和批准时一致（改过就不跑，告诉 Joy），只跑一次（先写 LIVE_MARK；中途被重起的不再重跑），final() 最多试 FINAL_TRIES 次 / FINAL_SECONDS 秒；
  结果写 RESULT（同 browser_script_run 的记录，不含页面正文）、截图写 SHOTS，最后 emit done（注销）并私信 Joy 一句结果。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _browser as bs  # noqa: E402 — 容器、档案同 browser / browser-script
import _script as sc  # noqa: E402


def watch(ctx: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    asyncio.run(run(ctx, emit))


async def run(ctx: dict[str, Any], emit: Callable[[dict[str, Any]], None], container: Any = None, sleep: Any = asyncio.sleep) -> None:
    """紧急任务的本体（container / sleep 给测试换假容器、假等待；不给用 bs.Container / asyncio.sleep）。"""
    m, sd = ctx["watch"], Path(ctx["state_dir"])
    sd.mkdir(parents=True, exist_ok=True)
    make = container or bs.Container
    name = m["name"]

    def done(result: dict[str, Any], note: str) -> None:
        (sd / sc.RESULT).write_text(json.dumps(result, ensure_ascii=False))
        emit({"done": True, "notify": note})

    def source() -> str:
        try:
            return Path(m["script"]).read_text()
        except OSError:
            return ""

    if (sd / sc.RESULT).exists():                     # 已经跑完（注销前被重起）：直接注销
        emit({"done": True})
        return
    if (sd / sc.LIVE_MARK).exists():                  # 实跑到一半被重起：只执行一次，不重跑
        done({"result": "error", "error": {"message": "上次实跑中途中断（监听程序被重起），没有重跑"}},
             f"⚠ 紧急任务 {name} 实跑中途中断了，没有重跑（只执行一次）；请看看网站上的状态")
        return
    changed = {"result": "script_changed", "error": {"message": "脚本在批准后被改过，没有执行"}}
    b: Any = None
    try:
        if m["kind"] == "at":
            at = float(m["at"])
            if time.time() > at + sc.MISSED_S:
                done({"result": "missed"}, f"⚠ 紧急任务 {name} 错过了执行时刻（{sc.when('at', at, None)}，当时监听没在跑），没有执行")
                return
            while (left := at - sc.LEAD_S - time.time()) > 0:
                await sleep(min(left, 30))
            if sc.digest(source()) != m["sha"]:
                done(changed, f"⚠ 紧急任务 {name} 的脚本在批准后被改过，没有执行；要继续请重新登记")
                return
            emit({"fire": True})
            b = make("browser-script", listen=ctx["listen"])
            await b.__aenter__()
            await _start(b, m, source())
        else:
            errors = 0
            while True:
                if sc.digest(source()) != m["sha"]:
                    done(changed, f"⚠ 紧急任务 {name} 的脚本在批准后被改过，已停止监控；要继续请重新登记")
                    return
                try:
                    if b is None:
                        b = make("browser-script", listen=ctx["listen"])
                        await b.__aenter__()
                        await _start(b, m, source())
                    r = await b.send({"op": "script", "source": source(), "mode": "check"}, timeout=max(60, int(m["interval"]) * 2))
                    if not r.get("ok") or r.get("result") == "error":
                        err = r.get("error")
                        raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err)[:200])
                    errors = 0
                    if r.get("result") == "true":
                        break
                except Exception as e:  # noqa: BLE001 — 页面出错、容器退出：换一个容器接着查
                    errors += 1
                    if b is not None:
                        await _close(b)
                        b = None
                    print(f"紧急任务 {name}：第 {errors} 次检查出错：{type(e).__name__}: {str(e)[:150]}", file=sys.stderr, flush=True)
                    if errors % sc.WATCH_ERRORS_NOTIFY == 0:
                        emit({"notify": f"⚠ 紧急任务 {name} 连续 {errors} 次检查出错（{type(e).__name__}: {str(e)[:150]}）：登录过期的话请让我重新登录；不需要了就让我取消 {name}"})
                await sleep(int(m["interval"]))
            emit({"fire": True})
            r = await b.send({"op": "goto", "url": sc.start_url(source())})
            if not r.get("ok"):
                raise RuntimeError(f"回到 start_url 失败：{str(r.get('error', ''))[:200]}")
        (sd / sc.LIVE_MARK).write_text(str(time.time()))
        r = await b.send({"op": "script", "source": source(), "mode": "live", "start_at": float(m["at"]) * 1000 if m["kind"] == "at" else None,
                          "final_tries": sc.FINAL_TRIES, "final_seconds": sc.FINAL_SECONDS},
                         timeout=max(0.0, float(m.get("at") or 0) - time.time()) + 600)
        if m.get("profile") and r.get("result") != "stopped":
            st = await b.send({"op": "state"})
            if st.get("ok") and st.get("storage_state"):
                bs.save_profile(m["profile"], st["storage_state"])
    except Exception as e:  # noqa: BLE001 — 实跑前后的意外：记结果、告诉 Joy（只执行一次，不重试）
        if b is not None:
            await _close(b)
            b = None
        done({"result": "error", "error": {"message": f"{type(e).__name__}: {str(e)[:200]}"}}, f"❌ 紧急任务 {name} 出错了：{type(e).__name__}: {str(e)[:150]}")
        return
    finally:
        if b is not None:
            await _close(b)
    out = sc.summarize(r, bs._safe) if r.get("ok") else {"result": "error", "error": {"message": str(r.get("error", ""))[:200]}}
    shots = []
    (sd / sc.SHOTS).mkdir(exist_ok=True)
    for i, s in enumerate(r.get("shots") or [], 1):
        (sd / sc.SHOTS / f"{i:02d}.jpg").write_bytes(base64.b64decode(s["data"]))
        shots.append({"why": s.get("why"), "file": f"{sc.SHOTS}/{i:02d}.jpg"})
    icon = "✅" if out["result"] == "done" else "⚠"
    done({**out, "shots": shots}, f"{icon} 紧急任务 {name} 实跑结果：{out['result']}" + (f"（{out['error']['message'][:100]}）" if out.get("error") else ""))


async def _start(b: Any, m: dict[str, Any], source: str) -> None:
    state = bs.load_profile(m["profile"]) if m.get("profile") else None
    r = await b.send({"op": "start", "start_url": sc.start_url(source), "nav_domains": m["domains"], "storage_state": state})
    if not r.get("ok"):
        raise RuntimeError(str(r.get("error", ""))[:200])


async def _close(b: Any) -> None:
    try:
        await b.__aexit__(None, None, None)
    except Exception as e:  # noqa: BLE001 — 关不掉也接着做（stbcc-docker 在连接断开时停容器）
        print(f"关浏览器容器出错：{e}", file=sys.stderr)
