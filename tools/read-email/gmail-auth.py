#!/usr/bin/env python3
"""一次性拿 Gmail 只读 refresh token，写进仓库根目录 .env 的 STB_GMAIL_CLIENT_ID / STB_GMAIL_CLIENT_SECRET / STB_GMAIL_REFRESH_TOKEN（经 env.update：原子写，保留 .env 原有的权限与属组）。

前提：Google Cloud 里建一个 OAuth 客户端（类型"桌面应用"），开启 Gmail API，下载 client_secret_*.json。
用法：uv run python tools/read-email/gmail-auth.py ~/Downloads/client_secret_xxx.json
远程机器：先 ssh -L 8765:127.0.0.1:8765 sparky，再在本机浏览器打开脚本打印的链接。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control_center import env  # noqa: E402

PORT = int(os.environ.get("STB_OAUTH_PORT", "8765"))
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raw = json.loads(Path(sys.argv[1]).read_text())
    client = raw.get("installed") or raw.get("web") or raw
    client_id, client_secret = client["client_id"], client["client_secret"]
    redirect = f"http://127.0.0.1:{PORT}/"
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = parse_qs(urlsplit(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                body = b"OK, back to the terminal."
            else:
                body = b"missing code/state"
            self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state})
    print("在浏览器打开：\n", url, "\n等待授权回调…", flush=True)
    while "code" not in got:
        threading.Event().wait(0.5)
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    r.raise_for_status()
    tok = r.json()
    if "refresh_token" not in tok:
        raise SystemExit("没拿到 refresh_token：去 https://myaccount.google.com/permissions 撤销该应用后重跑")
    out = env.update({"STB_GMAIL_CLIENT_ID": client_id, "STB_GMAIL_CLIENT_SECRET": client_secret, "STB_GMAIL_REFRESH_TOKEN": tok["refresh_token"]})
    prof = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30).json()
    print(f"已写入 {out}（账号 {prof.get('emailAddress')}，scope 只读）")


if __name__ == "__main__":
    main()
