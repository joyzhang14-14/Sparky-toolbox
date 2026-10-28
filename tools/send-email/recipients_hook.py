"""send-email 的 pre_call hook（tool.yaml 的 hooks，send-email 是 is_editable: false，以平台身份跑）。输入是网关从 stdin 给的 JSON
（{event, task, run_id, run_dir, tool, function, args}，见 control_center/hooks.py）：
1. 收件人：to / cc 一律改写成同文件夹 recipients.txt 里的地址（「to: 地址」「cc: 地址」一行一个，# 注释），模型给的不算；
   stdout 输出 {"args": {...}}，网关用它替换这次调用的参数。recipients.txt 的模板是 recipients.example.txt：
   文件不在、或没有 to: 行 → 退出码 2 拦下（stderr 说明，原样交给模型）。
2. 正文链接（send-email 的固定行为）：参数里所有字符串中的 http(s) 链接，规范化后（小写、去掉 # 之后的部分、去掉结尾的 / 和 .,;）
   必须在本次运行的研究来源里，否则退出码 2 拦下，stderr 列出前 5 个，每个附上来源里最接近的网址（有才附，如模型把 /2026/10/19/ 改写成了 /2026-10-19/）。
   只差 - 与 / 的（把 - 都当 / 后一样）、且来源里只对上一个的，直接换成来源原网址（参数里一并改写），不拦；换了哪些写进 stderr（hooks.jsonl 记着）。研究来源 = 运行目录 run_dir 里平台记的子代理调用结果
   （subagent_results.jsonl，一行一次调用）中，隔离子代理（isolated）成功交回的结果的 links（{来源编号: [网址…]}，联网研究 isolated-web-search 交的）。
"""
import difflib
import json
import re
import sys
from pathlib import Path

RECIPIENTS = Path(__file__).resolve().parent / "recipients.txt"
LINK = re.compile(r"https?://[^\s\"'<>)\]\\]+")


def recipients(text: str) -> tuple[list[str], list[str]]:
    """recipients.txt → (to, cc)；不认识的行（不是 to: / cc: 开头）报 ValueError。"""
    to, cc = [], []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, addr = line.partition(":")
        if key.strip().lower() not in ("to", "cc") or not addr.strip():
            raise ValueError(f"recipients.txt 第 {n} 行不认识：要写成「to: 地址」或「cc: 地址」")
        (to if key.strip().lower() == "to" else cc).append(addr.strip())
    return to, cc


def norm(u: str) -> str:
    """网址规范化：去掉结尾的 .,;、# 之后的部分、结尾的 /，小写。"""
    u = u.strip().rstrip(".,;")
    u = re.sub(r"#.*$", "", u)
    return u.rstrip("/").lower()


RESULTS = "subagent_results.jsonl"   # 平台记子代理调用结果的文件（control_center/gateway.py SUBAGENT_RESULTS）


def sources(run_dir) -> dict[str, str]:
    """本次运行的研究来源 {规范化后: 原网址}：隔离子代理成功交回的结果里 links 的网址。没有运行目录或文件就是空。"""
    out: dict[str, str] = {}
    if not run_dir:
        return out
    try:
        lines = (Path(str(run_dir)) / RESULTS).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        res = r.get("result") if isinstance(r, dict) and r.get("isolated") and r.get("ok") else None
        links = res.get("links") if isinstance(res, dict) else None
        if isinstance(links, dict):
            out.update((norm(u), u) for urls in links.values() if isinstance(urls, list) for u in urls if isinstance(u, str))
    return out


def loose(u: str) -> str:
    """只差 - 与 / 的网址看成一样：规范化后把 - 都当 /。"""
    return norm(u).replace("-", "/")


def rewrite(x, fix: dict[str, str]):
    """参数里所有字符串中的链接按 fix 换掉（逐个链接换，不做子串替换）。"""
    if isinstance(x, str):
        return LINK.sub(lambda m: fix.get(m.group(0), m.group(0)), x)
    if isinstance(x, dict):
        return {k: rewrite(v, fix) for k, v in x.items()}
    if isinstance(x, list):
        return [rewrite(v, fix) for v in x]
    return x


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    args = dict(data.get("args") or {})
    if not RECIPIENTS.is_file():
        print("tools/send-email/recipients.txt 不在（按同文件夹的 recipients.example.txt 建），邮件没发；请 Joy 建好这个文件", file=sys.stderr)
        return 2
    try:
        to, cc = recipients(RECIPIENTS.read_text())
    except ValueError as e:
        print(f"{e}（请 Joy 改 tools/send-email/recipients.txt），邮件没发", file=sys.stderr)
        return 2
    if not to:
        print("tools/send-email/recipients.txt 里还没有收件人（to: 行），邮件没发；请 Joy 补上", file=sys.stderr)
        return 2
    known = sources(data.get("run_dir"))
    args.pop("to", None)
    args.pop("cc", None)
    bad = sorted({u for s in strings(args) for u in LINK.findall(s) if norm(u) not in known})
    same: dict[str, set[str]] = {}
    for k, u in known.items():
        same.setdefault(k.replace("-", "/"), set()).add(u)
    fix = {u: next(iter(m)) for u in bad if len(m := same.get(loose(u), set())) == 1}
    if fix:
        args = rewrite(args, fix)
        bad = [u for u in bad if u not in fix]
        print("链接换成了来源原网址：" + "；".join(f"{u} → {v}" for u, v in fix.items()), file=sys.stderr)
    if bad:
        hint = {u: difflib.get_close_matches(norm(u), list(known), n=1, cutoff=0.9) for u in bad[:5]}
        print("正文里有不在本次研究来源里的链接，不能发：" + "；".join(u + (f"（来源里最接近的：{known[m[0]]}）" if (m := hint[u]) else "") for u in bad[:5])
              + "\n改法：把这几个链接换成本次研究来源里的网址（括号里的是同一篇就逐字照抄它），或者删掉这几条，再用同一个函数重发", file=sys.stderr)
        return 2
    print(json.dumps({"args": {**args, "to": to, "cc": cc}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
