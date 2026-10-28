"""skills（手册 §4.10 第 4 步）：Agent Skills 标准的 skills/<名>/SKILL.md（开头 YAML 写 name、description），可带 scripts/、references/、requirements.txt。
放仓库 skills/（进 git，归平台、sparky 只读）；只经 skills 工具用（tools/skills/：search_skills、load_skill、import_skill），和工具分开。
导入：skills 工具把来源取到它的状态目录里暂存、发通用审批卡；Joy 同意后经中枢的 skill_install（以平台用户跑，sparky 写不了仓库）调 install：
核对卡、拷进 skills/<名>/、在仓库里 git 提交；之后 skills 工具把要装的 Python 包（requirements：requirements.txt + 脚本与文档里 Python 代码 import 的第三方模块）
装进沙箱共用的安装环境（control_center/sandbox_env.py，先 conda-forge 再 pip、逐个装）。"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import yaml

from control_center.paths import APPROVALS, REPO

SKILLS = REPO / "skills"
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_NAME = 64
MAX_BYTES = 1024 * 1024 * 1024
TOOL = "skills"
PIP_NAMES = {"PIL": "pillow", "fitz": "pymupdf", "cv2": "opencv-python", "yaml": "pyyaml", "docx": "python-docx", "pptx": "python-pptx",
             "bs4": "beautifulsoup4", "sklearn": "scikit-learn", "dateutil": "python-dateutil", "dotenv": "python-dotenv"}   # import 名和 pip 包名不一样的


def parse(folder: Path) -> dict[str, str]:
    """folder/SKILL.md 开头的 YAML → {name, description}；缺了、写错了报 ValueError（原文说缺什么）。"""
    p = folder / "SKILL.md"
    if not p.is_file():
        raise ValueError(f"{folder} 里没有 SKILL.md")
    text = p.read_text(errors="replace")
    m = re.match(r"---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    try:
        head = yaml.safe_load(m.group(1)) if m else None
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md 开头的 YAML 读不了：{e}") from None
    if not isinstance(head, dict):
        raise ValueError("SKILL.md 要以 --- 包起来的 YAML 开头，写 name 和 description")
    name, desc = str(head.get("name") or "").strip(), " ".join(str(head.get("description") or "").split())
    if not name or not desc:
        raise ValueError(f"SKILL.md 开头的 YAML 缺 {'、'.join(k for k, v in (('name', name), ('description', desc)) if not v)}")
    if len(name) > MAX_NAME or not NAME_RE.fullmatch(name):
        raise ValueError(f"skill 名 {name!r} 只能用小写字母、数字和连字符（不超过 {MAX_NAME} 个字）")
    return {"name": name, "description": desc}


def all_skills() -> dict[str, dict[str, Any]]:
    """skills/ 下的全部 skill → {名: {description, folder}}（文件夹名就是名字；SKILL.md 写坏了的、和文件夹名对不上的跳过）。"""
    out = {}
    for d in sorted(SKILLS.iterdir()) if SKILLS.is_dir() else []:
        try:
            meta = parse(d)
        except (ValueError, OSError):
            continue
        if meta["name"] == d.name:
            out[d.name] = {"description": meta["description"], "folder": d}
    return out


def files(folder: Path) -> list[Path]:
    """folder 里的全部文件（相对路径，排好序）；有链接或特殊文件报 ValueError。"""
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            raise ValueError(f"{p.relative_to(folder)} 是链接或特殊文件：skill 里只能放普通文件")
        if p.is_file():
            out.append(p.relative_to(folder))
    return out


def digest(folder: Path) -> str:
    """folder 全部内容（相对路径 + 字节）的 sha256：审批卡同意的就是这份内容。"""
    h = hashlib.sha256()
    for rel in files(folder):
        h.update(str(rel).encode() + b"\0" + (folder / rel).read_bytes() + b"\0")
    return h.hexdigest()


def check(folder: Path) -> dict[str, str]:
    """导入前的检查：SKILL.md 齐全、名字合法且没有重名、只有普通文件、总共不超过 MAX_BYTES。返回 {name, description}。"""
    meta = parse(folder)
    size = sum((folder / rel).stat().st_size for rel in files(folder))
    if size > MAX_BYTES:
        raise ValueError(f"skill 一共 {size / 1e6:.1f} MB，超过 {MAX_BYTES // 2**20} MB")
    if (SKILLS / meta["name"]).exists():
        raise ValueError(f"已经有叫 {meta['name']} 的 skill（{SKILLS / meta['name']}）：要换新版先让 Joy 删掉旧的")
    return meta


def _imports(code: str) -> set[str]:
    """一段 Python 代码 import 的顶层模块（相对导入不算）；语法不对的按行找 import / from。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", code, re.M))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            out.add(n.module.split(".")[0])
    return out


def requirements(folder: Path) -> list[str]:
    """要装的包：requirements.txt 的每一行，加上脚本（.py）与文档（.md 的 python 代码块）里 import 的第三方模块
    （标准库、skill 自己的模块除外；import 名换成 pip 包名，requirements.txt 写过的不重复）。"""
    req = folder / "requirements.txt"
    out = [ln.strip() for ln in req.read_text(errors="replace").splitlines() if ln.strip() and not ln.strip().startswith("#")] if req.is_file() else []
    own = {p.stem for p in folder.rglob("*.py")} | {p.name for p in folder.rglob("*") if p.is_dir()}
    mods: set[str] = set()
    for rel in files(folder):
        text = (folder / rel).read_text(errors="replace")
        for code in [text] if rel.suffix == ".py" else re.findall(r"```(?:python|py)\s*\n(.*?)```", text, re.S) if rel.suffix == ".md" else []:
            mods |= _imports(code)
    listed = {re.split(r"[<>=!~\[;\s]", r, maxsplit=1)[0].lower() for r in out}
    extra = sorted({PIP_NAMES.get(m, m) for m in mods - set(sys.stdlib_module_names) - own - {"__future__"}})
    return out + [p for p in extra if p.lower() not in listed]


def key(name: str, sha: str) -> str:
    """通用审批卡的 key：认「导入这一份内容」。"""
    return f"skill:{name}:{sha}"


def _approved_card(run: str, k: str) -> dict[str, Any] | None:
    """这次运行里 Joy 已同意（ask_permission 就地用掉了）、还没装过的导入卡。"""
    for p in sorted(APPROVALS.glob("*.json")):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        pl = c.get("payload") or {}
        if ((c.get("resolved") or {}).get("action") == "approve" and pl.get("run") == run and pl.get("tool") == TOOL and pl.get("key") == k
                and pl.get("used") and not pl.get("installed")):
            return c
    return None


def _git(*args: str) -> None:
    """在仓库里跑 git（cwd 是 skills/）；路径写相对 skills/ 的。"""
    r = subprocess.run(["git", "-C", str(SKILLS), *args], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[-500:]}")


def _unzip(data: bytes, dest: Path) -> None:
    """zip 的内容解到 dest：有绝对路径、..、链接的不解，解开超过 MAX_BYTES 的不解。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是 zip 文件") from None
    for m in z.infolist():
        if m.filename.startswith("/") or ".." in Path(m.filename).parts or (m.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"压缩包里有不安全的路径或链接：{m.filename}")
    if sum(m.file_size for m in z.infolist()) > MAX_BYTES:
        raise ValueError(f"压缩包解开超过 {MAX_BYTES // 2**20} MB")
    z.extractall(dest)


def import_upload(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Joy 在网页 Skills 页手动导入（Joy 本人上传，不发审批卡）：上传的 [(相对路径, 内容)]——一个 .zip / .skill、一个 SKILL.md 或整个 skill 文件夹
    → 最浅的那个 SKILL.md 所在的文件夹，check 后拷进 skills/<名>/、在仓库里 git 提交。返回 {name, requirements}；不合格报 ValueError，git 出错报 RuntimeError。"""
    if not files:
        raise ValueError("没有收到文件")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw"
        raw.mkdir()
        if len(files) == 1 and Path(files[0][0]).suffix in (".zip", ".skill"):
            _unzip(files[0][1], raw)
        else:
            for rel, data in files:
                parts = Path(rel).parts
                if not parts or rel.startswith("/") or ".." in parts:
                    raise ValueError(f"不安全的路径：{rel}")
                p = raw.joinpath(*parts)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        found = sorted({p.parent for p in raw.rglob("SKILL.md")}, key=lambda d: len(d.parts))
        if not found:
            raise ValueError("上传的内容里没有 SKILL.md")
        if len(found) > 1 and len(found[1].parts) == len(found[0].parts):
            raise ValueError(f"上传的内容里有好几个 skill（{'、'.join(str(d.relative_to(raw)) for d in found[:10])}）：一次只传一个")
        name = check(found[0])["name"]
        SKILLS.mkdir(exist_ok=True)
        shutil.copytree(found[0], SKILLS / name)
    _git("add", "--", name)
    _git("commit", "-q", "-m", f"feat(skills): import {name}", "--", name)
    return {"name": name, "requirements": requirements(SKILLS / name)}


def install(run: str, name: str, sha: str, staged: str) -> dict[str, Any]:
    """中枢的 skill_install：核对这次运行里 Joy 同意了导入 staged 这份内容（key 里的 sha256），先拷到平台自己的临时目录再核一次指纹，
    拷进 skills/<名>/、在仓库里 git 提交。返回 {ok, folder} 或 {ok: False, error}。（要装的包由 skills 工具接着以 sparky 装进沙箱共用的环境。）"""
    card = _approved_card(run, key(name, sha))
    if card is None:
        return {"ok": False, "error": f"这次运行里没有 Joy 已同意、还没装过的导入 {name} 的审批卡（内容在发卡之后变了也算没有）"}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / name
        try:
            shutil.copytree(staged, copy, symlinks=True)
            if digest(copy) != sha:
                return {"ok": False, "error": "暂存的内容和 Joy 同意的那份对不上（发卡之后被改过），没有安装"}
            meta = check(copy)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        if meta["name"] != name:
            return {"ok": False, "error": f"SKILL.md 里的名字是 {meta['name']}，不是 {name}"}
        dest = SKILLS / name
        SKILLS.mkdir(exist_ok=True)
        shutil.copytree(copy, dest)
    card["payload"]["installed"] = time.time()
    from control_center import cards

    cards.save(card)
    try:
        _git("add", "--", name)
        _git("commit", "-q", "-m", f"feat(skills): import {name}", "--", name)
    except RuntimeError as e:
        return {"ok": False, "error": f"已拷进 {dest}，但 {e}"}
    return {"ok": True, "folder": str(dest)}
