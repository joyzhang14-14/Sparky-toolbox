"""download 工具共用（MCP 服务器 server.py 与批准后的后台下载程序 download_watch.py）：来源探测、下载、格式标注、状态文件。都以 sparky 跑。

- 来源：网址（http / https），或 HuggingFace 仓库 {repo, revision, files?}（files 是 glob，不写 = 整个仓库）。
- 落在哪：统一的下载目录 /home/sparky/workspace/_download/（平台的 paths.DOWNLOADS；沙箱里外同一个路径），可以再分一层子目录（folder）；
  目标文件已存在就拒（不覆盖），只有同名 .part 是续传。下完挪到别处（~/models、~/apps/ComfyUI/models…）由模型用 shell 做；不自动清理。
- 登录才能下的：profile 写浏览器登录档案名，带档案的 cookie（data/tool_state/browser/profiles/<档案>.json），只给档案域名清单里的网址（_browser.profile_domains）。
- 下载：先写 <文件>.part，断了下次从 .part 的长度接着下（Range）；给了 sha256 就核对，HF 的 LFS 文件按 API 给的 sha256、普通文件按 git blob sha1 核对。
- 标注：下完按文件头（magic bytes，不信扩展名）识别格式，旁边写 <文件名>.meta.json {format, size, source, sha256, untrusted, downloaded_at}。
  untrusted：只有模型权重（WEIGHT_FORMATS：内容不会以任何形式进到模型）是 false，其余一律 true（文档、文本、图片、音视频、压缩包、不认识的）。压缩包不自动解压。
- 状态文件：下载目录的 STATUS/<id>.json（id 是平台一次性监听的登记 id）：{id, status: pending / downloading / done / failed, source_text, dest, total, done_bytes,
  files_done, meta, error}；total 是探测到的总字节数，探不准（没给大小、或服务器压缩传输：Content-Length 是压缩后的）就是 null，下完改成实际字节数；download_status 读它（等批 / 拒绝 / 取消看平台的登记，control_center/monitors.py）。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from control_center import tool_kit  # noqa: F401 — 装上程序错误标记
from control_center import paths  # noqa: E402 — paths.DOWNLOADS 调用时才读（测试换了数据目录也跟着走）

FREE_MARGIN = 1.05                                  # 硬盘至少留出这么多倍
CHUNK = 1 << 20
WEIGHT_FORMATS = ("safetensors", "gguf", "pytorch", "pytorch-pickle", "onnx")   # 模型权重：内容不会进到模型，untrusted: false
HF = "https://huggingface.co"
STATUS = ".status"                                  # 下载目录里放状态文件的子目录


class DownloadError(ValueError):
    pass


def dest_dir(folder: str = "") -> Path:
    """下载目录（DOWNLOADS）或它下面的一层子目录 folder（字母、数字、. _ -）。"""
    folder = str(folder or "").strip().strip("/")
    if folder and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", folder)):
        raise DownloadError(f"folder 只写一层子目录名（字母、数字、. _ -），如 qwen3-8b：{folder!r}")
    return paths.DOWNLOADS / folder if folder else paths.DOWNLOADS


def sandbox_path(p: Path) -> str:
    """下载目录里的路径给模型看的写法：沙箱里外同一个路径。"""
    return str(p)


def status_file(rid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", rid):
        raise DownloadError(f"下载 id 不对：{rid!r}（id 是 download_request 返回的 12 位十六进制，如 3f9a0c1b2d4e）")
    return paths.DOWNLOADS / STATUS / f"{rid}.json"


def save_status(st: dict[str, Any]) -> None:
    p = status_file(st["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def load_status(rid: str) -> dict[str, Any] | None:
    try:
        return json.loads(status_file(rid).read_text())
    except (OSError, ValueError):
        return None


# ---------- 来源与大小 ----------
def _http(method: str, url: str, headers: dict[str, str] | None = None, timeout: float = 60, stream: bool = False) -> Any:
    """网络请求的唯一出口（测试替换它）。要求不压缩传输（Accept-Encoding: identity）：Content-Length 才是文件本身的字节数。"""
    import httpx

    client = httpx.Client(follow_redirects=True, timeout=timeout, headers={"Accept-Encoding": "identity", **(headers or {})})
    if stream:
        return client, client.stream(method, url)
    try:
        return client.request(method, url)
    except httpx.HTTPError as e:
        raise DownloadError(f"连不上 {url}：{type(e).__name__}: {e}") from None
    finally:
        client.close()


def _body(resp: Any) -> str:
    """响应体前 300 字（报错用；流式响应先读完）。"""
    try:
        if callable(getattr(resp, "read", None)):
            resp.read()
        t = str(resp.text).strip()
    except Exception as e:  # noqa: BLE001
        return f"（响应体读不了：{type(e).__name__}）"
    return f"响应体：{t[:300]}" if t else "响应体为空"


def _name_from(url: str, resp: Any) -> str:
    cd = resp.headers.get("content-disposition", "") if resp is not None else ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    name = unquote(m.group(1)) if m else unquote(Path(urlsplit(url).path).name)
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", name).strip("._") or "download"
    return name[:200]


def _probe(url: str, headers: dict[str, str]) -> tuple[Any, str, str]:
    """探大小：先 HEAD；HEAD 被拒（有的服务器只拒 HEAD，如 modelscope 一律回 404）再用 GET 只读响应头、不读正文。
    返回（响应，失败时写进报错的请求说明，失败时的响应体）。"""
    import httpx

    r = _http("HEAD", url, headers=headers)
    if r.status_code < 400:
        return r, "", ""
    head = r.status_code
    client, cm = _http("GET", url, headers=headers, stream=True)
    try:
        with cm as g:
            return g, f"HEAD 请求 HTTP {head}，GET 请求 HTTP {g.status_code}", _body(g) if g.status_code >= 400 else ""
    except httpx.HTTPError as e:
        raise DownloadError(f"连不上 {url}：{type(e).__name__}: {e}") from None
    finally:
        client.close()


def plan_files(source: Any, cookies: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """来源 → 要下的文件 [{url, path（相对 dest）, size, sha256?, git_sha1?}]。网址用 _probe 探大小（没给、或仍是压缩传输的 size 为 None）；
    HF 仓库用 API（带每个文件的大小与校验）。"""
    if isinstance(source, str):
        if urlsplit(source).scheme not in ("http", "https"):
            raise DownloadError(f"source 写 http(s) 网址，或 HuggingFace 仓库 {{repo, revision, files}}：{source!r}")
        r, how, body = _probe(source, _cookie_header(cookies))
        if r.status_code >= 400:
            fix = {404: "网址不对或文件已不在：确认是文件的直链（点下载按钮后真正下载的地址），不是介绍页",
                   401: "要登录：申请时写 profile（登录档案名，会带档案的 cookie；网址要在档案的域名清单里）",
                   403: "没权限或要登录：申请时写 profile（登录档案名，会带档案的 cookie；网址要在档案的域名清单里）"}.get(
                r.status_code, "服务器拒绝或出错：稍后再试，或换个下载地址")
            raise DownloadError(f"探测 {source} 失败（{how}）：{body}\n改法：{fix}")
        plain = r.headers.get("content-encoding", "identity").strip().lower() in ("", "identity")
        size = int(r.headers["content-length"]) if plain and str(r.headers.get("content-length") or "").isdigit() else None
        return [{"url": source, "path": _name_from(source, r), "size": size}]
    if not isinstance(source, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", str(source.get("repo", ""))):
        raise DownloadError("HuggingFace 来源写 {repo: \"组织/仓库\", revision: \"main\"（或 commit）, files: \"*.safetensors\"（可选）}")
    repo, rev = source["repo"], str(source.get("revision") or "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", rev):
        raise DownloadError(f"revision 不合法：{rev!r}\n改法：写分支名（如 main）或 commit 哈希，只能有字母、数字和 . _ / -，最长 101 字符")
    r = _http("GET", f"{HF}/api/models/{repo}/revision/{rev}?blobs=true")
    if r.status_code >= 400:
        raise DownloadError(f"HuggingFace 上查不到 {repo}@{rev}（HTTP {r.status_code}；私有 / 要同意协议的仓库下不了）")
    pattern = str(source.get("files") or "*")
    out = []
    for s in r.json().get("siblings") or []:
        name = str(s.get("rfilename", ""))
        if not name or ".." in Path(name).parts or not fnmatch.fnmatch(name, pattern):
            continue
        lfs = s.get("lfs") or {}
        out.append({"url": f"{HF}/{repo}/resolve/{rev}/{name}", "path": name, "size": int(lfs.get("size") or s.get("size") or 0),
                    **({"sha256": lfs["sha256"]} if lfs.get("sha256") else {"git_sha1": s["blobId"]} if s.get("blobId") else {})})
    if not out:
        raise DownloadError(f"{repo}@{rev} 里没有匹配 {pattern} 的文件")
    return out


def _cookie_header(cookies: dict[str, str] | None) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}


def profile_cookies(profile: str, url: str) -> dict[str, str]:
    """浏览器登录档案（Playwright storageState）里能给这个网址用的 cookie：网址的域名要在档案的域名清单里，cookie 的域要对得上网址。"""
    import _browser

    try:
        domains = _browser.profile_domains(profile)
    except ValueError as e:
        raise DownloadError(str(e)) from None
    host = (urlsplit(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise DownloadError(f"{host} 不在登录档案 {profile} 允许的域名里（{domains}）")
    p = paths.TOOL_STATE / "browser" / "profiles" / f"{profile}.json"
    try:
        state = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise DownloadError(f"登录档案 {profile} 的 cookie 读不了（{p}：{type(e).__name__}: {e}），没下\n"
                            f"改法：先用子代理 isolated-browser 以档案 {profile} 登录这个网站一次（登录后会写回档案），再申请下载；档案文件坏了请 Joy 重新登录上传") from None
    out = {}
    for c in state.get("cookies") or []:
        d = str(c.get("domain", "")).lstrip(".").lower()
        if d and (host == d or host.endswith("." + d)):
            out[str(c.get("name"))] = str(c.get("value"))
    return out


# ---------- 下载 ----------
def total_of(files: list[dict[str, Any]]) -> int | None:
    """总字节数；有一个文件大小不知道就是 None。"""
    return None if any(f.get("size") is None for f in files) else sum(f["size"] for f in files)


def gb(n: int | None) -> str:
    return "大小未知" if n is None else f"{n / 1e9:.2f} GB"


def fetch(url: str, dest: Path, sha256: str | None = None, git_sha1: str | None = None, headers: dict[str, str] | None = None,
          progress: Any = None) -> int:
    """下到 dest：先写 dest.part，已有 .part 就从它的长度接着下（服务器不支持 Range 就从头）；校验对了再改名。返回字节数。"""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    hdr = {**(headers or {}), **({"Range": f"bytes={have}-"} if have else {})}
    client, cm = _http("GET", url, headers=hdr, timeout=60, stream=True)
    try:
        with cm as r:
            if r.status_code >= 400:
                raise DownloadError(f"下载 {url} 失败（HTTP {r.status_code}）：{_body(r)}")
            if have and r.status_code != 206:
                have = 0                                    # 不支持续传：从头下
            with part.open("ab" if have else "wb") as f:
                for chunk in r.iter_bytes(CHUNK):
                    f.write(chunk)
                    if progress:
                        progress(len(chunk))
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001 — 网络断了（httpx 的错误）、写盘出错
        raise DownloadError(f"下载 {url} 断了（{type(e).__name__}: {e}）：下次从 {part.name} 接着下") from None
    finally:
        client.close()
    n = part.stat().st_size
    if sha256 and (got := _hash(part, "sha256")) != sha256.lower():
        part.unlink(missing_ok=True)
        raise DownloadError(f"{dest.name} 的 sha256 是 {got}，应为 {sha256}（文件已删）")
    if git_sha1 and (got := _git_sha1(part)) != git_sha1:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{dest.name} 的 git blob sha1 是 {got}，HuggingFace 记的是 {git_sha1}（文件已删）")
    part.replace(dest)
    return n


def _hash(p: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with p.open("rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def _git_sha1(p: Path) -> str:
    h = hashlib.sha1(f"blob {p.stat().st_size}\0".encode())
    with p.open("rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


# ---------- 格式与标注 ----------
def sniff(p: Path) -> str:
    """按文件头认格式（不看扩展名）；认不出的返回 unknown。"""
    try:
        with p.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return "unknown"
    if len(head) >= 9 and head[8:9] == b"{" and 0 < int.from_bytes(head[:8], "little") < 100_000_000:
        return "safetensors"
    sigs = [(b"GGUF", "gguf"), (b"%PDF", "pdf"), (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"),
            (b"\x1a\x45\xdf\xa3", "matroska"), (b"OggS", "ogg"), (b"fLaC", "flac"), (b"ID3", "mp3"), (b"\x1f\x8b", "gzip"),
            (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"), (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!", "rar"), (b"\x80\x02", "pytorch-pickle"),
            (b"\x80\x03", "pytorch-pickle"), (b"\x80\x04", "pytorch-pickle")]
    for sig, fmt in sigs:
        if head.startswith(sig):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] in (b"WEBP", b"WAVE", b"AVI "):
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}[head[8:12]]
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3":
        return "mp3"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(p).namelist()
        except (zipfile.BadZipFile, OSError):
            return "zip"
        if any(n.endswith("data.pkl") for n in names):
            return "pytorch"
        for prefix, fmt in (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx")):
            if any(n.startswith(prefix) for n in names):
                return fmt
        return "zip"
    if head[:1] == b"\x08" and any(s in head for s in (b"onnx", b"pytorch", b"tf2onnx", b"skl2onnx")):
        return "onnx"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    low = text.lstrip().lower()
    if low.startswith(("<!doctype html", "<html")):
        return "html"
    if low.startswith(("{", "[")):
        return "json"
    return "text"


def untrusted(fmt: str) -> bool:
    """内容会不会以任何形式进到模型：只有模型权重不会（false），其余一律 true，不认识的也是 true。"""
    return fmt not in WEIGHT_FORMATS


def write_meta(p: Path, source: str) -> dict[str, Any]:
    """在文件旁写 <文件名>.meta.json：格式、大小、来源、sha256、untrusted、下载时间。"""
    fmt = sniff(p)
    meta = {"format": fmt, "size": p.stat().st_size, "source": source, "sha256": _hash(p, "sha256"), "untrusted": untrusted(fmt),
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    p.with_name(p.name + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def source_text(source: Any) -> str:
    return source if isinstance(source, str) else f"HuggingFace {source.get('repo')}@{source.get('revision') or 'main'}" + (
        f"（{source['files']}）" if source.get("files") else "")
