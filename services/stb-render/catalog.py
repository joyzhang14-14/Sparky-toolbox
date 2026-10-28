"""构建镜像时生成字体目录 catalog.json：[{family, category, zh}]，render 工具的 render_fonts 按它查。
category：Google Fonts 的分类（SANS_SERIF / SERIF / DISPLAY / HANDWRITING / MONOSPACE），不在 Google Fonts 里的按名字补；zh = 含中文字形（fontconfig 认 lang=zh，或 Google Fonts 标了中文子集）。"""
import json
import re
import subprocess
from pathlib import Path

EXTRA = {"LXGW WenKai": "HANDWRITING", "LXGW WenKai Light": "HANDWRITING", "LXGW WenKai Medium": "HANDWRITING",
         "Smiley Sans": "DISPLAY", "Noto Color Emoji": "DISPLAY"}


def families(*args: str) -> set[str]:
    out = subprocess.run(["fc-list", *args, "family"], capture_output=True, text=True, check=True).stdout
    return {f.strip() for line in out.splitlines() for f in line.split(",")[:1] if f.strip()}


cat: dict[str, str] = {}
gf_zh: set[str] = set()                   # Google Fonts 元数据标了中文子集的（常用字不全的风格字体 fontconfig 不认 lang=zh）
for pb in Path("/opt/render/gf-meta").glob("*.pb"):
    t = pb.read_text(errors="ignore")
    name, c = re.search(r'^name: "([^"]+)"', t, re.M), re.search(r'^category: "([^"]+)"', t, re.M)
    if name and c:
        cat[name.group(1)] = c.group(1)
        if re.search(r'^subsets: "chinese-', t, re.M):
            gf_zh.add(name.group(1))
zh = families(":lang=zh") | gf_zh
rows = []
for f in sorted(families(":")):
    c = cat.get(f) or EXTRA.get(f) or ("MONOSPACE" if "Mono" in f else "SERIF" if "Serif" in f or "Ming" in f else "SANS_SERIF")
    rows.append({"family": f, "category": c, "zh": f in zh})
print(json.dumps(rows, ensure_ascii=False))
