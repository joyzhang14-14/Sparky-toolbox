"""skills 工具（手册 §4.10 第 4 步）：search_skills 在仓库 skills/ 里找（字面命中在前，bge-m3 语义相近的在后），load_skill 交回 SKILL.md 全文与文件清单（要的 Python 包还没装的先装上，不发卡：skills/ 里的都是 Joy 放的或批准导入的），
import_skill 把本机路径或网址的 skill 取到状态目录暂存、发通用审批卡，Joy 同意后请中枢 skill_install 以平台用户安装（control_center/skills.py），
装好后把要装的 Python 包装进沙箱共用的环境（control_center/sandbox_env.py），结果直接带上加载的内容。只交回文字，网关不参与。"""
import asyncio
import json
import os
import re
import secrets
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from control_center import tool_kit
from control_center import embed, hub_client, sandbox_env, skills

FLOOR, MARGIN = 0.45, 0.08             # 语义相近的取法（名字 + 说明的短文本；2026-10-16 用 23 个工具与子代理的说明实测：对的 0.5～0.75，不相关的也有 0.45～0.6）
INSTALL_TIMEOUT = 300                  # 等中枢拷进仓库、提交的秒数
SCRIPT_EXT = (".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".rb", ".pl")
GITHUB_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(tree|blob)/([^/]+)(?:/(.*))?)?/?$")

server = MCPServer("skills", instructions="找、加载、导入 skill（写好的做法，可带脚本）")


def _state() -> Path:
    return Path(os.environ["STB_STATE_DIR"])


def _literal(words: list[str], items: dict[str, dict[str, Any]]) -> list[str]:
    scored = [(-hits, n) for n, it in items.items() if (hits := sum(w in f"{n} {it['description']}".lower() for w in words))]
    return [n for _, n in sorted(scored)]


@server.tool(description="按要做的事或关键词找 skill")
def search_skills(query: str) -> str:
    words = [w for w in re.split(r"[\s,，、;；/|]+", query.lower()) if w]
    if not words:
        raise ToolError("query 写要做的事或关键词，如「PDF」「做表格」")
    items = skills.all_skills()
    if not items:
        return "[]\nskills/ 里还没有 skill：Joy 让你装 skill，或他给的附件、链接指向的仓库里有 SKILL.md 时，用 import_skill 导入"
    names = _literal(words, items)
    db = sqlite3.connect(_state() / "vectors.sqlite3")
    try:
        names += [n for n in embed.similar(query, {n: f"{n}：{it['description']}" for n, it in items.items()}, db, FLOOR, MARGIN) if n not in names]
    finally:
        db.close()
    if not names:
        return f"[]\n没有名字或说明里含「{query}」或意思相近的 skill：换个说法再搜"
    return json.dumps([{"name": n, "description": items[n]["description"]} for n in names], ensure_ascii=False)


def _loaded(name: str, head: str) -> str:
    folder = skills.SKILLS / name
    listing = "\n".join(f"- {rel}" for rel in skills.files(folder))
    return (f"{head}：照下面 SKILL.md 的做法做。文件在 {folder}/（沙箱里同一路径、只读）：参考文档用 read 读，脚本用 bash 跑。\n"
            f"文件：\n{listing}\n\n--- SKILL.md ---\n{(folder / 'SKILL.md').read_text(errors='replace')}")


async def _deps(name: str) -> str:
    """把这个 skill 要的 Python 包（skills.requirements）装进沙箱共用的环境：装过同一份清单的（状态目录 deps/<名>.json 记着）不再装，
    清单变了、环境没了才重装。返回给模型的说明（装不上的包；没有就是空）。"""
    pkgs = skills.requirements(skills.SKILLS / name)
    mark = _state() / "deps" / f"{name}.json"
    try:
        done = json.loads(mark.read_text())
    except (OSError, ValueError):
        done = {}
    if not pkgs:
        return ""
    if done.get("packages") != pkgs or not (sandbox_env.ENV / "bin" / "python").exists():
        try:
            done = {"packages": pkgs, "failed": (await asyncio.to_thread(sandbox_env.install, pkgs))[1]}
        except (RuntimeError, OSError) as e:
            return f"（依赖没装成：{e}）\n"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps(done, ensure_ascii=False))
    return f"（这些包没装上，用到它们的脚本跑不了：{'、'.join(done['failed'])}）\n" if done.get("failed") else ""


@server.tool(description="按 search_skills 给的 name 加载一个 skill：交回它的做法（SKILL.md）和文件清单")
async def load_skill(name: str) -> str:
    if name not in skills.all_skills():
        raise ToolError(f"没有叫 {name} 的 skill：先用 search_skills 找，name 照它返回的写")
    return await _deps(name) + _loaded(name, f"已加载 skill {name}")


def _download(url: str, dest: Path) -> Path:
    """下载到文件 dest（暂存区里，不进内存）：超过 MAX_BYTES 的 3 倍就停（GitHub 仓库的 zip 含整个仓库）。"""
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r, open(dest, "wb") as f:
        if r.status_code != 200:
            raise ToolError(f"下载 {url} 失败：HTTP {r.status_code}")
        for chunk in r.iter_bytes():
            f.write(chunk)
            if f.tell() > skills.MAX_BYTES * 3:
                raise ToolError(f"{url} 超过 {skills.MAX_BYTES * 3 // 2**20} MB，不像一个 skill")
    return dest


def _unzip(src: Path, dest: Path, bad: str = "不是 zip 文件") -> None:
    """解压到 dest：拒收绝对路径、..、链接。不是 zip 报 bad。"""
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ToolError(bad) from None
    for m in z.infolist():
        parts = Path(m.filename).parts
        if m.filename.startswith("/") or ".." in parts or (m.external_attr >> 16) & 0o170000 == 0o120000:
            raise ToolError(f"压缩包里有不安全的路径或链接：{m.filename}")
    z.extractall(dest)


def _fetch(source: str, raw: Path) -> Path:
    """source → raw 下的文件；返回 skill 所在的文件夹（含 SKILL.md）。"""
    sub = ""
    if source.startswith(("http://", "https://")):
        if m := GITHUB_RE.match(source):
            owner, repo, kind, ref, path = m.groups()
            sub = str(Path(path).parent) if kind == "blob" and path else (path or "")
            _unzip(_download(f"https://github.com/{owner}/{repo}/archive/{ref or 'HEAD'}.zip", raw.parent / "download.zip"), raw)
            tops = [p for p in raw.iterdir() if p.is_dir()]
            root = (tops[0] if len(tops) == 1 else raw) / sub
        else:
            _unzip(_download(source, raw.parent / "download.zip"), raw,
                   f"{source} 下下来不是 zip 文件：网址只认 GitHub 链接和 zip 的直链。\n"
                   "改法：单个 SKILL.md 或别的网站上的 skill，先用 download 工具的 download_request 下到本机，再把下好的本机路径（文件夹、.zip 或 SKILL.md）写进 source")
            root = raw
    else:
        p = Path(source)
        if not p.is_absolute() or not p.exists():
            raise ToolError(f"{source} 不存在：本机路径写绝对路径（如 /home/sparky/workspace/<任务>/in/x.zip），或写网址")
        if p.is_dir():
            shutil.copytree(p, raw / p.name, symlinks=True)
            root = raw / p.name
        elif p.name == "SKILL.md":
            (raw / "skill").mkdir()
            shutil.copy(p, raw / "skill" / "SKILL.md")
            root = raw / "skill"
        elif p.suffix in (".zip", ".skill"):
            _unzip(p, raw)
            root = raw
        else:
            raise ToolError(f"{source} 不是 skill 文件夹、.zip 或 SKILL.md")
    if not root.is_dir():
        raise ToolError(f"{source} 里没有 {sub or '这个文件夹'}")
    found = sorted({p.parent for p in root.rglob("SKILL.md")}, key=lambda d: len(d.parts))
    if not found:
        raise ToolError(f"{source} 里没有 SKILL.md")
    if len(found) > 1 and found[0] != root:
        rels = "、".join(str(d.relative_to(root)) for d in found[:10])
        raise ToolError(f"{source} 里有 {len(found)} 个 skill（{rels}）：指明要哪一个（本机路径写到那个文件夹，网址写到那个文件夹的 GitHub 链接）")
    return found[0]


def _detail(source: str, folder: Path) -> str:
    rels = skills.files(folder)
    size = sum((folder / r).stat().st_size for r in rels)
    lines = [f"来源：{source}", f"文件（{len(rels)} 个，共 {size / 1024:.1f} KB）："]
    lines += [f"  {r}  {(folder / r).stat().st_size / 1024:.1f} KB" for r in rels]
    if pkgs := skills.requirements(folder):
        lines += ["", "同意后联网安装这些 Python 包（requirements.txt 与代码里 import 的第三方模块；逐个装，装不上的会列出）：", "、".join(pkgs)]
    for r in rels:
        if r.suffix in SCRIPT_EXT or r.parts[0] == "scripts":
            head = (folder / r).read_text(errors="replace").splitlines()[:20]
            lines += ["", f"--- {r}（开头 20 行）", *head]
    return "\n".join(lines)


@server.tool(description="导入一个 skill：source 写本机路径（skill 文件夹、.zip 或 SKILL.md）或网址（GitHub 上 skill 文件夹的链接、.zip 链接）。"
             "发审批卡给 Joy，同意后装好并直接加载")
async def import_skill(source: str) -> str:
    stage = _state() / "staging" / secrets.token_hex(6)
    raw = stage / "raw"
    raw.mkdir(parents=True)
    try:
        found = _fetch(source.strip(), raw)
        try:
            meta = skills.check(found)
        except ValueError as e:
            raise ToolError(str(e)) from None
        name = meta["name"]
        staged = stage / name
        found.rename(staged)
        for f in [staged, *staged.rglob("*")]:                     # 中枢以平台用户读暂存区（sparkle 组）：拷来的 0600 文件也要读得到
            f.chmod(0o755 if f.is_dir() else 0o644)
        sha = skills.digest(staged)
        got, value = await tool_kit.ask_permission(skills.TOOL, f"导入 skill {name}：{meta['description'][:150]}", skills.key(name, sha), _detail(source, staged))
        if got == "failed":
            raise ToolError(f"审批卡没发出去：{value}")
        if got == "pending":
            return f"已发审批卡给 Joy，他还没处理，skill {name} 还没导入；不要重复导入"
        if got == "reject":
            return f"Joy 拒绝了导入 skill {name}" + (f"；他说：「{value[:1000]}」" if value else "")
        r = await hub_client.call("skill_install", timeout=INSTALL_TIMEOUT, run_ref=os.environ.get("STB_RUN_REF", ""), name=name, sha=sha, staged=str(staged)) or {}
        if not r.get("ok"):
            raise ToolError(f"Joy 同意了，但没装上：{r.get('error') or 'stbcc-hub 没有响应'}")
        return await _deps(name) + _loaded(name, f"已导入并加载 skill {name}（Joy 同意了，已提交 git）")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    server.run("stdio")
