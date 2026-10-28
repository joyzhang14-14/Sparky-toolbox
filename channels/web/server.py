"""stb 网页服务外壳：只在 Tailscale 网卡上监听（同一 tailnet 的设备才能访问），标准库 HTTP，无依赖。
页面 frontend/index.html 是外壳加聊天；控制台（任务、运行记录、日志、工具、子代理及其操作）的接口和前端在 control_center/console/，这里挂上它的路由；
聊天的接口在 chat.py（/api/chat/…）。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from channels.web import chat as webchat
from control_center.console import api as console
from control_center.paths import ensure_dirs, settings, tailscale_ip
from control_center.task import TaskError

TAILNET = ("100.64.0.0/10",)          # Tailscale 分配的地址段（CGNAT 段）；来源不在这里面一律 403


def web_settings() -> dict[str, Any]:
    """config/stbcc.yaml 的 web 段：port（端口）、bind（tailscale 或具体 IP）。"""
    web = settings().get("web") or {}
    if extra := sorted(set(web) - {"port", "bind"}):
        raise SystemExit(f"stbcc.yaml 的 web 段有未知字段 {extra}（可用：port、bind）")
    return {"port": int(web.get("port", 18090)), "bind": str(web.get("bind", "tailscale"))}


def _allowed_client(ip: str) -> bool:
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or any(a in ipaddress.ip_network(n) for n in TAILNET)
INDEX = Path(__file__).parent / "frontend" / "index.html"
FRONTEND = INDEX.parent.resolve()   # 聊天页按需加载的前端模块（3D 查看器 viewer3d.js 与它用的 vendor/three/）经 /web/<路径> 取，只给这里的 .js


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *a: Any) -> None:  # 安静
        pass

    def parse_request(self) -> bool:  # 第二道：来源 IP 必须是 tailnet 或本机
        ok = super().parse_request()
        if ok and not _allowed_client(self.client_address[0]):
            self.send_error(403, "tailnet only")
            return False
        return ok

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, p: Path, download: bool = False) -> None:
        import mimetypes
        from urllib.parse import quote

        # 附件：支持 Range（视频要按片段取才能播放、拖动）、ETag / 304（看过的不再下载），分块发，不整个读进内存
        st = p.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        size, start, end = st.st_size, 0, st.st_size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if m and (m.group(1) or m.group(2)) else 200)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"):
            self.send_header("Content-Security-Policy", "sandbox")    # 模型给的 HTML / SVG：隔离打开，里面的脚本碰不到本站接口
        if download:                                   # 浏览器直接存进「下载」文件夹；中文文件名走 filename*
            self.send_header("Content-Disposition", f"attachment; filename=\"{p.name.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{quote(p.name)}")
        if m and (m.group(1) or m.group(2)):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        if self.command == "HEAD":
            return
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := f.read(min(256 * 1024, left))):
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()                                   # _send_file 看到 HEAD 只发头；其余接口照常（浏览器只对附件发 HEAD）

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, INDEX.read_text(), "text/html")
            if (r := console.get(u.path, q)) is not None:     # 控制台（control_center/console）
                return self._send(*r)
            if u.path.startswith("/web/"):
                f = (FRONTEND / u.path[len("/web/"):]).resolve()
                return self._send_file(f) if f.suffix == ".js" and FRONTEND in f.parents and f.is_file() else self._send(404, {"error": "not found"})
            if u.path == "/api/chat/history":
                return self._send(200, webchat.history(int(q.get("limit", ["20"])[0]), q.get("before", [None])[0], q.get("have", [""])[0]))
            if u.path == "/api/chat/turn":
                d = webchat.turn_events(q.get("run", [""])[0])
                return self._send(200, d) if d else self._send(404, {"error": "没有这一轮"})
            if u.path == "/api/chat/card":
                d = webchat.card_full(q.get("id", [""])[0])
                return self._send(200, d) if d else self._send(404, {"error": "没有这张卡"})
            if u.path == "/api/chat/call":
                d = webchat.call_detail(q.get("run", [""])[0], q.get("id", [""])[0])
                return self._send(200, d) if d else self._send(404, {"error": "没有这次调用"})
            if u.path == "/api/chat/status":
                return self._send(200, webchat.status())
            if u.path == "/api/chat/file":
                f = webchat.file_path(q.get("p", [""])[0])
                w = q.get("w", [""])[0]
                return self._send_file(webchat.thumb(f, int(w)) if w.isdigit() else f, download=q.get("dl", [""])[0] == "1")
            self._send(404, {"error": "not found"})
        except TaskError as e:
            self._send(404, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or 0) or b"{}")
        try:
            if (r := console.post(u.path, body)) is not None:   # 控制台（control_center/console）
                return self._send(200, r)
            if u.path == "/api/chat/send":
                return self._send(200, webchat.send(str(body.get("text") or ""), body.get("files") or []))
            m = re.fullmatch(r"/api/chat/cards/([0-9a-f]+)", u.path)
            if m:
                return self._send(200, webchat.card_act(m.group(1), str(body.get("action") or ""), body.get("value")))
            self._send(404, {"error": "not found"})
        except TaskError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main() -> None:
    ensure_dirs()
    cfg = web_settings()
    bind = tailscale_ip(cfg["bind"])
    srv = ThreadingHTTPServer((bind, cfg["port"]), Handler)
    webchat.serve_adapter()                     # 向聊天中枢报到：data/sockets/channels/web.sock
    print(f"stb web: http://{bind}:{cfg['port']}（只在 Tailscale 网卡监听）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
