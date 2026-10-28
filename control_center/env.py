"""本机的私人配置：凭证与个人标识都在仓库根目录的 .env（不进 git；属主是平台用户，sparkle 组只读，其他人无权限）。

- 格式：一行一个 KEY=VALUE，# 开头是注释，值两边的引号会去掉；
- 取值时进程环境变量优先（测试与 systemd 可覆盖），其次 .env；
- 任务 YAML 的 tools 下的配置值里写 ${KEY}，load_task 按这里展开（expand_tree）；只展开个人标识：
  工具代码里 credential("KEY") 读的键都是凭证（registry.credential_keys 静态扫出来），写成 ${凭证} 直接报错（凭证只给工具进程自己读）。system_prompt、注释里的 ${} 不展开。
路径可用 STB_ENV_FILE 改（测试用）。不引入 python-dotenv，自己解析。
- 工具读凭证：control_center/credential.py 的 credential("KEY")（.env 对 sparky 只读），缺了网关给 Joy 发凭证卡；
- 凭证值扫描（find_value / check_no_values）：要写进任务 / 工具文件的内容里原样出现 .env 的值就拒，防模型把值写死（代码里该写 credential("键名")）；
  报错只报文件、行号、键名，绝不带值。builder 部署检查、网页保存任务、stb task add / check 三处用。
- .env 旁边的 .env.keys 只列键名（没有值，权限 644）：sparky 用户（shell、builder 工具）查有哪些凭证、缺哪个读它，不读 .env（sparky 读得到，但值会进对话、会话文件、日志）。
  写入 .env（update）时重写；Joy 手改 .env 的，stbcc-hub 每分钟按修改时间补写（write_keys）。
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SCAN_MIN = 8                                                   # 短于 8 个字符的值不比（true、端口号之类，误报多）
_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s@]+")              # 不含 @ 的 URL；带 user:pass@ 的照样比


class EnvError(Exception):
    pass


def path() -> Path:
    return Path(os.environ.get("STB_ENV_FILE", REPO / ".env"))


def _parse(p: Path) -> dict[str, str]:
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def values() -> dict[str, str]:
    p = path()
    if not p.exists():
        return {}
    st = p.stat()
    if st.st_mode & 0o007 or st.st_uid != p.parent.stat().st_uid:   # 组位不管：sparkle 组（sparky 在里面）只读
        raise EnvError(f"{p} 权限不对：属主必须是平台用户（仓库目录的属主），其他人（other）不能有任何权限"
                       f"（chmod o= {p}；属主不对用 chown 改回）；给 sparky 只读：chgrp sparkle {p} 且 chmod 640 {p}")
    try:
        return _parse(p)
    except PermissionError:          # 读不到 .env（不在 sparkle 组或组不可读）：只看环境变量
        return {}


def get(key: str) -> str | None:
    return os.environ.get(key) or values().get(key) or None


def require(key: str) -> str:
    v = get(key)
    if not v:
        raise EnvError(f"缺少 {key}：写进 {path()}（权限 600），见 .env.example")
    return v


def expand_tree(v: object, credentials: frozenset[str] | set[str] = frozenset()) -> object:
    """把 v（任务 yaml 的 tools 部分）里字符串值中的 ${KEY} 换成 .env 里的值；缺哪个就报哪个，credentials 里的键（凭证）直接拒。
    列表里单独一项正好是 ${KEY} 时，值按逗号分成多项。"""
    def sub(s: str) -> str:
        def one(m: re.Match[str]) -> str:
            if m.group(1) in credentials:
                raise EnvError(f"${{{m.group(1)}}} 是凭证，只能给工具用（工具进程自己从 .env 读），不能写进任务 yaml；"
                               f"改法：从任务 yaml 删掉这一项，工具用到时在代码里 credential(\"{m.group(1)}\") 自己读")
            return require(m.group(1))
        return _VAR.sub(one, s)

    if isinstance(v, dict):
        return {k: expand_tree(x, credentials) for k, x in v.items()}
    if isinstance(v, list):
        out: list[object] = []
        for x in v:
            if isinstance(x, str) and _VAR.fullmatch(x.strip()):
                out += [p.strip() for p in sub(x.strip()).split(",") if p.strip()]
            else:
                out.append(expand_tree(x, credentials))
        return out
    return sub(v) if isinstance(v, str) else v


def _scanned(v: str) -> bool:
    """这个值要不要比：明显不是机密的不比——短于 SCAN_MIN、纯数字（Discord ID 之类）、邮箱或「名字 <邮箱>」、不含 @ 的 URL。
    这些是个人标识或公开地址，任务 / 工具里出现是正常的（任务 yaml 用 ${STB_OWNER_DISCORD_ID}、工具常写服务地址）。其余一律当可能的凭证比。"""
    v = v.strip()
    if len(v) < SCAN_MIN or v.isdigit() or _URL.fullmatch(v) or _EMAIL.fullmatch(v):
        return False
    m = re.fullmatch(r"[^<>@]*<([^<>]+)>", v)
    return not (m and _EMAIL.fullmatch(m.group(1)))


def find_value(text: str) -> tuple[int, str] | None:
    """text 里第一处原样出现的 .env 值：(行号, 键名)；没有返回 None。读不到 .env（如 .env 不是 sparkle 组可读时以 sparky 跑）就不比。"""
    vals = {k: v.strip() for k, v in values().items() if _scanned(v)}
    for n, line in enumerate(text.splitlines(), 1):
        for k, v in vals.items():
            if v in line:
                return n, k
    return None


def check_no_values(text: str, where: str) -> None:
    """要写入的内容里有 .env 的值就抛 EnvError：只报文件、行号、键名，不带值。"""
    if hit := find_value(text):
        raise EnvError(f"{where} 第 {hit[0]} 行出现了 {hit[1]} 的值，请改成在代码里用 credential(\"{hit[1]}\") 读")


def update(new: dict[str, str]) -> Path:
    """写入或更新几项（保留其余行与注释）。写临时文件再改名（原子）：已有 .env 的权限位与属组（sparkle 组只读）原样带过去，新建的才是 600。"""
    p = path()
    lines = p.read_text().splitlines() if p.exists() else []
    left = dict(new)
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in left:
            lines[i] = f"{k}={left.pop(k)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)   # 新建即 600
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            if p.exists():
                st = p.stat()
                os.chown(tmp, -1, st.st_gid)
                os.chmod(tmp, st.st_mode & 0o777)
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    write_keys()
    return p


def keys_path() -> Path:
    return path().with_name(path().name + ".keys")


def write_keys() -> Path | None:
    """.env.keys：.env 的键名，一行一个（没有值）；内容没变就不写。.env 不存在返回 None。"""
    p = path()
    if not p.exists():
        return None
    out = keys_path()
    text = "# .env 里的键名（没有值），平台按 .env 自动生成，不要手改\n" + "".join(f"{k}\n" for k in _parse(p))
    if not out.exists() or out.read_text() != text:
        out.write_text(text)
    out.chmod(0o644)
    return out
