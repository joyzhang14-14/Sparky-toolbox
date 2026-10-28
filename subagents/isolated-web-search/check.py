"""isolated-web-search 的体检（stb doctor 的约定文件 check.py；本子代理 is_editable: false，以平台身份跑）：联网研究依赖的 owa-launcher 与 SearXNG
都要通（2026-09-20 重启后 SearXNG 没自动起来，研究全部空手而归）。没装联网研究（install.sh 不带 --owa，没有 owa-launcher 用户服务）时跳过、不算失败。
stdout 一行 JSON {ok, detail}。"""
import json

from control_center import systemd, tool_kit  # noqa: F401 — 装上程序错误标记
import _research  # noqa: E402
import httpx  # noqa: E402

SEARXNG = "http://127.0.0.1:18080"   # 自建 SearXNG（services/searxng/，研究容器经 owa-net 用它）


def launcher() -> dict:
    try:
        h = httpx.get(f"{_research.LAUNCHER}/health", timeout=5).json()
        return {"ok": bool(h.get("ok")), "detail": {k: h.get(k) for k in ("running", "queued", "host_mem_avail_gb", "holding_for_memory")}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200]}


def searxng() -> dict:
    try:
        r = httpx.get(f"{SEARXNG}/search", params={"q": "sparky doctor", "format": "json"}, timeout=20)
        n = len(r.json().get("results", [])) if r.status_code == 200 else 0
        return {"ok": n > 0, "detail": f"{n} hits" if r.status_code == 200 else f"http {r.status_code}（services/searxng/up.sh 启动）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200] + "（services/searxng/up.sh 启动）"}


def main() -> None:
    if not (systemd.UNIT_DIR / "owa-launcher.service").exists():
        print(json.dumps({"ok": True, "detail": "没装联网研究（install.sh --owa），跳过"}, ensure_ascii=False))
        return
    detail = {"owa_launcher": launcher(), "searxng": searxng()}
    print(json.dumps({"ok": all(v["ok"] for v in detail.values()), "detail": detail}, ensure_ascii=False))


if __name__ == "__main__":
    main()
