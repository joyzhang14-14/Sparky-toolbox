"""pptx-deck 工具：把一份 JSON 版面规格生成 .pptx（演示文稿），文字是原生文本框，PowerPoint / Keynote 里能直接点选编辑。

坐标一律是虚拟画布像素（默认 1920x1080），按幻灯片尺寸线性换算成 EMU / 磅，可以直接照抄 render 那套 HTML 绝对定位的数值：
1920x1080 画布 → 12192000x6858000 EMU（13.333x7.5 英寸，16:9），1 px = 6350 EMU，96 px 字号 = 48 pt。
推荐做法：装饰（警示条、网点、扫描线、人物立绘）交给 render_png 出一张不含文字的 PNG 当整页背景，文字用本工具的原生文本框叠上去——
既有设计感，Joy 又能改字。纯用 shape + text 也能搭出整页（色块、条形、标签）。
只读工作目录下 in / media / out 里的文件（沙箱里外同一个路径），成品只写本次运行的 media 目录（media/<运行 id>/pptx-N.pptx）。
形状一律去掉模板自带的 <p:style>（主题的线、阴影、字体引用），出来只按规格里写的样式渲染，结果可预期。
环境变量（网关给）：STB_WORK_ROOT，STB_MEDIA_DIR（没给就用 STB_WORK_ROOT/media/STB_RUN_ID）。不需要凭证，不联网。
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image, UnidentifiedImageError
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

server = MCPServer("pptx-deck", instructions="JSON 版面规格 → .pptx：像素坐标、可编辑文字；背景图配 render_png，文字用文本框")

SLIDE_EMU = {"16:9": (12192000, 6858000), "4:3": (9144000, 6858000)}     # 幻灯片物理尺寸（EMU）
SLIDE_IN = {"16:9": (13.3333, 7.5), "4:3": (10.0, 7.5)}                  # 同上，英寸（算 px→pt 用）
DEFAULT_CANVAS = (1920, 1080)
NATIVE = ("PNG", "JPEG", "GIF", "BMP", "TIFF")                           # python-pptx 认的图片格式，其余先用 PIL 转 PNG
READABLE = ("in", "media", "out")                                        # 本工具只读工作目录的这几个子目录
SHAPES = {"rect": MSO_SHAPE.RECTANGLE, "roundRect": MSO_SHAPE.ROUNDED_RECTANGLE, "parallelogram": MSO_SHAPE.PARALLELOGRAM,
          "ellipse": MSO_SHAPE.OVAL, "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE, "diamond": MSO_SHAPE.DIAMOND,
          "chevron": MSO_SHAPE.CHEVRON, "hexagon": MSO_SHAPE.HEXAGON, "pentagon": MSO_SHAPE.PENTAGON,
          "rightArrow": MSO_SHAPE.RIGHT_ARROW, "line": None}
ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}
ANCHOR = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}
EA_SUCC = ("a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst")     # a:ea 之后才允许出现的兄弟标签
TEXT_KEYS = ("font_size_px", "font_family", "bold", "italic", "color", "align", "anchor", "wrap", "line_spacing",
             "letter_spacing_px", "caps", "space_before_px", "space_after_px")
MAX_SLIDES, MAX_ELEMENTS, MAX_CHARS = 80, 400, 20000


class Ctx:
    """一份 deck 的换算与目录：像素 → EMU / 磅的比例、画布尺寸。"""

    def __init__(self, spec: dict[str, Any], warns: list[str]) -> None:
        c = spec.get("canvas") or DEFAULT_CANVAS
        if not (isinstance(c, (list, tuple)) and len(c) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in c)):
            raise ToolError(f"canvas 要写成 [宽, 高] 像素：{c!r}")
        self.cw, self.ch = float(c[0]), float(c[1])
        ratio = spec.get("slide_size", "16:9")
        if ratio not in SLIDE_EMU:
            raise ToolError(f"slide_size 只能是 {' / '.join(SLIDE_EMU)}")
        self.w_emu, self.h_emu = SLIDE_EMU[ratio]
        w_in, h_in = SLIDE_IN[ratio]
        self.ex, self.ey = self.w_emu / self.cw, self.h_emu / self.ch          # px → EMU
        self.px, self.py = w_in * 72 / self.cw, h_in * 72 / self.ch            # px → pt
        self.font_pt = self.px
        if abs(self.px - self.py) > 0.01 * self.px:
            warns.append(f"画布 {self.cw:g}x{self.ch:g} 与幻灯片 {ratio} 比例不一致：坐标按 x / y 分别拉伸，字号按宽度换算")

    def box(self, e: dict[str, Any], warns: list[str], what: str, allow_zero: bool = False) -> tuple[int, int, int, int]:
        x, y, w, h = _num(e, "x"), _num(e, "y"), _num(e, "w"), _num(e, "h")
        bad = (w == 0 and h == 0) if allow_zero else (w <= 0 or h <= 0)     # 直线允许 w 或 h 为 0（水平 / 垂直线）
        if w < 0 or h < 0 or bad:
            raise ToolError(f"{what} 要写非负数 w / h（像素），且不能两个都是 0")
        if x + w > self.cw + 1 or y + h > self.ch + 1 or x < -1 or y < -1:
            warns.append(f"{what} 超出画布：({x:g},{y:g}) {w:g}x{h:g}（画布 {self.cw:g}x{self.ch:g}）")
        return (Emu(int(round(x * self.ex))), Emu(int(round(y * self.ey))),
                Emu(int(round(w * self.ex))), Emu(int(round(h * self.ey))))


def _num(d: Any, key: str, default: float = 0.0) -> float:
    v = d.get(key, default) if isinstance(d, dict) else default
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ToolError(f"{key} 要写成数字（像素）：{v!r}")
    return float(v)


def _hex(v: Any, what: str = "颜色") -> str:
    s = str(v).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        raise ToolError(f"{what} 要写成 6 位十六进制（如 \"E11D2E\" 或 \"#E11D2E\"）：{v!r}")
    return s.upper()


def work_root() -> Path:
    if not os.environ.get("STB_WORK_ROOT"):
        raise ToolError("本任务没有工作目录（STB_WORK_ROOT 未设置）")
    return Path(os.environ["STB_WORK_ROOT"]).resolve()


def media_dir() -> Path:
    """本次运行的成品目录：网关给的 STB_MEDIA_DIR，没给就用 work_root/media/<STB_RUN_ID>（测试环境）。"""
    if os.environ.get("STB_MEDIA_DIR"):
        d = Path(os.environ["STB_MEDIA_DIR"]).resolve()
    else:
        rid = os.environ.get("STB_RUN_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", rid):
            raise ToolError("没给 STB_MEDIA_DIR，也没有合法的 STB_RUN_ID，无处放成品")
        d = work_root() / "media" / rid
    if work_root() not in d.parents:
        raise ToolError("成品目录越界，只能写在工作目录下")
    d.mkdir(parents=True, exist_ok=True)
    return d


def host_path(p: Any) -> Path:
    """模型写的路径（沙箱里外同一个路径）→ realpath；必须在工作目录里，且只在 in / media / out 下。"""
    root = work_root()
    if not isinstance(p, str) or not p.startswith("/"):
        raise ToolError(f"路径要写工作目录下文件的绝对路径：{p!r}")
    h = Path(p).resolve()
    if root not in h.parents:
        raise ToolError(f"路径不在工作目录 {root} 下：{p}")
    if h.relative_to(root).parts[0] not in READABLE:
        raise ToolError(f"只能读工作目录里 {'、'.join(READABLE)} 下的文件：{p}")
    return h


def sandbox_path(host: Path) -> str:
    """给模型看的路径：沙箱里外同一个路径，就是 realpath。"""
    return str(host.resolve())


def next_name(d: Path, base: str) -> str:
    n = 1
    while any(d.glob(f"{base}-{n}.*")):
        n += 1
    return f"{base}-{n}"


def _read_spec(spec: Any) -> dict[str, Any]:
    if isinstance(spec, dict):
        return spec
    if not isinstance(spec, str) or not spec.strip():
        raise ToolError("spec 要写成 JSON 字符串，或工作目录下 JSON 文件的绝对路径")
    s = spec.strip()
    if s.startswith("{"):
        try:
            return json.loads(s)
        except ValueError as e:
            raise ToolError(f"spec 不是合法 JSON：{e}") from None
    f = host_path(s)
    if not f.is_file():
        raise ToolError(f"没有这个 spec 文件：{s}")
    try:
        return json.loads(f.read_text())
    except ValueError as e:
        raise ToolError(f"{s} 不是合法 JSON：{e}") from None


# ---------- 图片 ----------

def _open(path: Path) -> tuple[str, int, int]:
    try:
        with Image.open(path) as im:
            return (im.format or "").upper(), im.width, im.height
    except (OSError, ValueError, UnidentifiedImageError) as e:
        raise ToolError(f"读不了图片 {sandbox_path(path)}：{e}") from None


def _native(p: Path, media: Path) -> Path:
    """python-pptx 只吃 PNG / JPEG / GIF / BMP / TIFF；webp 等先用 PIL 转成 PNG 放进成品目录。"""
    fmt, _, _ = _open(p)
    if fmt in NATIVE:
        return p
    out = media / f"{next_name(media, 'asset')}.png"
    with Image.open(p) as im:
        im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB").save(out, "PNG")
    return out


def _put_image(slide: Any, e: dict[str, Any], ctx: Ctx, media: Path, warns: list[str]) -> None:
    src = host_path(e.get("src"))
    if not src.is_file():
        raise ToolError(f"图片不存在：{e.get('src')}")
    path = _native(src, media)
    _, iw, ih = _open(path)
    x, y, w, h = ctx.box(e, warns, f"图片 {e.get('src')}")
    fit = e.get("fit", "stretch")
    if fit not in ("stretch", "contain", "cover"):
        raise ToolError(f"fit 只能是 stretch / contain / cover：{fit}")
    if fit == "contain":                                          # 等比缩放到框内、居中（不裁不变形）
        s = min(w / iw, h / ih)
        nw, nh = max(int(round(iw * s)), 1), max(int(round(ih * s)), 1)
        slide.shapes.add_picture(str(path), x + (w - nw) // 2, y + (h - nh) // 2, nw, nh)
        return
    pic = slide.shapes.add_picture(str(path), x, y, w, h)
    if e.get("crop"):
        c = e["crop"]
        if not (isinstance(c, (list, tuple)) and len(c) == 4):
            raise ToolError(f"crop 要写成 [左, 上, 右, 下] 四个 0~1 的比例：{c!r}")
        pic.crop_left, pic.crop_top, pic.crop_right, pic.crop_bottom = (max(0.0, min(float(v), 0.99)) for v in c)
    elif fit == "cover":                                          # 等比放大到铺满框，多出来的按比例裁掉
        s = max(w / iw, h / ih)
        cx = max(0.0, (1 - (w / s) / iw) / 2)
        cy = max(0.0, (1 - (h / s) / ih) / 2)
        pic.crop_left = pic.crop_right = cx
        pic.crop_top = pic.crop_bottom = cy


# ---------- 形状 ----------

def _alpha(clr: Any, pct: Any) -> None:
    v = max(0.0, min(float(pct), 100.0))                          # pct = 不透明度百分数
    clr.append(clr.makeelement(qn("a:alpha"), {"val": str(int(round(v * 1000)))}))


def _strip_style(shape: Any) -> None:
    """去掉模板给 autoshape 加的 <p:style>：否则线、阴影、字体都从主题继承，结果不可预期。"""
    el = shape._element.find(qn("p:style"))
    if el is not None:
        shape._element.remove(el)


def _fill(shape: Any, e: dict[str, Any], warns: list[str]) -> None:
    f = e.get("fill", "none")
    if f in (None, "", "none"):
        shape.fill.background()
        return
    if isinstance(f, dict):
        cols = f.get("gradient")
        if not (isinstance(cols, list) and cols):
            raise ToolError("fill 写成颜色，或 {\"gradient\": [颜色…], \"angle\": 90, \"alpha\": 80}")
        shape.fill.gradient()
        grad = shape.fill._xPr.find(qn("a:gradFill"))
        gslst = grad.find(qn("a:gsLst")) if grad is not None else None
        if gslst is None:
            raise ToolError("渐变填充没生成（模板里没有 gsLst）：这个元素先改用纯色 fill")
        for gs in list(gslst):
            gslst.remove(gs)
        pos = f.get("positions") or []
        if pos and len(pos) != len(cols):
            warns.append(f"fill.positions 有 {len(pos)} 个、渐变色有 {len(cols)} 个，按默认均分")
            pos = []
        for i, c in enumerate(cols):
            p = float(pos[i]) if pos else (100.0 * i / max(len(cols) - 1, 1))
            gs = gslst.makeelement(qn("a:gs"), {"pos": str(int(round(max(0.0, min(p, 100.0)) * 1000)))})
            clr = gs.makeelement(qn("a:srgbClr"), {"val": _hex(c, "渐变色")})
            if f.get("alpha") is not None:
                _alpha(clr, f["alpha"])
            gs.append(clr)
            gslst.append(gs)
        if f.get("angle") is not None:                            # 线性渐变角度（度）：0 = 左→右，90 = 下→上
            lin = grad.find(qn("a:lin"))
            if lin is None:
                warns.append("渐变没有 a:lin，角度没写上")
            else:
                lin.set("ang", str(int(round(float(f["angle"]) * 60000)) % 21600000))
        return
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(_hex(f))
    if e.get("alpha") is not None:
        solid = shape.fill._xPr.find(qn("a:solidFill"))
        clr = solid.find(qn("a:srgbClr")) if solid is not None else None
        if clr is not None:
            _alpha(clr, e["alpha"])


def _stroke(shape: Any, e: dict[str, Any]) -> None:
    ln = e.get("line", "none")
    if ln in (None, "", "none"):
        shape.line.fill.background()
        return
    if not isinstance(ln, dict):
        raise ToolError("line 写成 \"none\"，或 {\"color\": …, \"width_px\": 2, \"alpha\": 60}")
    shape.line.color.rgb = RGBColor.from_string(_hex(ln.get("color", "FFFFFF"), "线色"))
    shape.line.width = Pt(max(_num(ln, "width_px", 2.0) * 0.5, 0.25))
    if ln.get("alpha") is not None:
        solid = shape.line._get_or_add_ln().find(qn("a:solidFill"))
        clr = solid.find(qn("a:srgbClr")) if solid is not None else None
        if clr is not None:
            _alpha(clr, ln["alpha"])


def _put_shape(slide: Any, e: dict[str, Any], ctx: Ctx, warns: list[str]) -> None:
    kind = e.get("kind", "rect")
    if kind not in SHAPES:
        raise ToolError(f"kind 只能是 {' / '.join(SHAPES)}：{kind}")
    x, y, w, h = ctx.box(e, warns, f"形状 {kind}", allow_zero=(kind == "line"))
    if kind == "line":                                            # 直线：从 (x,y) 到 (x+w, y+h)
        sp = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x, y, x + w, y + h)
        _stroke(sp, {**e, "line": e.get("line") or {"color": "FFFFFF", "width_px": 2}})
        if e.get("rotation") is not None:
            sp.rotation = float(e["rotation"])
        return
    sp = slide.shapes.add_shape(SHAPES[kind], x, y, w, h)
    _strip_style(sp)
    _fill(sp, e, warns)
    _stroke(sp, e)
    if e.get("rotation") is not None:
        sp.rotation = float(e["rotation"])
    if kind == "roundRect" and e.get("radius") is not None:
        sp.adjustments[0] = max(0.0, min(float(e["radius"]), 0.5))
    if e.get("text") not in (None, "", []):                       # 形状自带的文字（标签条省事）
        base = {k: e[k] for k in TEXT_KEYS if k in e}
        _write_text(sp.text_frame, {"paragraphs": e["text"], **base}, ctx, warns, f"形状 {kind} 的文字", box=None)


# ---------- 文字 ----------

def _paras(v: Any) -> list[Any]:
    if isinstance(v, str):
        return [{"text": t} for t in v.split("\n")]
    if isinstance(v, list):
        return [{"text": p} if isinstance(p, str) else p for p in v]
    raise ToolError(f"文字要写成字符串或段落列表：{v!r}")


def _runs(p: dict[str, Any]) -> list[dict[str, Any]]:
    r = p.get("runs")
    if r is None:
        return [p]                                              # 段落级样式（color / bold …）由这唯一的 run 继承
    if not (isinstance(r, list) and r):
        raise ToolError(f"runs 要写成非空列表：{r!r}")
    return [{"text": str(x)} if isinstance(x, str) else x for x in r]


def _apply_font(run: Any, style: dict[str, Any], base: dict[str, Any], ctx: Ctx) -> None:
    f = run.font
    px = _num(style, "font_size_px", _num(base, "font_size_px", 32.0))
    f.size = Pt(round(max(px * ctx.font_pt, 0.5), 1))
    fam = style.get("font_family") or base.get("font_family") or "Arial"
    f.name = str(fam)
    rpr = f._rPr
    if fam and fam.lower() != "inherit":                           # 中日韩字形也指定同一字体，否则中文回落到主题字体
        ea = rpr.find(qn("a:ea"))
        if ea is None:
            ea = rpr.makeelement(qn("a:ea"), {})
            rpr.insert_element_before(ea, *EA_SUCC)
        ea.set("typeface", str(fam))
    b, i = style.get("bold", base.get("bold")), style.get("italic", base.get("italic"))
    if b is not None:
        f.bold = bool(b)
    if i is not None:
        f.italic = bool(i)
    f.color.rgb = RGBColor.from_string(_hex(style.get("color") or base.get("color") or "000000", "文字颜色"))
    ls = _num(style, "letter_spacing_px", _num(base, "letter_spacing_px", 0.0))
    if ls:
        rpr.set("spc", str(int(round(ls * ctx.px * 100))))          # 字距，单位 1/100 磅
    if style.get("caps", base.get("caps")):
        rpr.set("cap", "all")


def _est_height(paras: list[dict[str, Any]], base: dict[str, Any], ctx: Ctx, box_w_pt: float) -> float:
    """粗估文字块高度（磅）：CJK 按 1 个字宽、其余按 0.55，只用来提醒可能撑破文本框，不是精确排版。"""
    total = 0.0
    for p in paras:
        size = _num(p, "font_size_px", _num(base, "font_size_px", 32.0)) * ctx.font_pt
        sp = _num(p, "space_before_px", _num(base, "space_before_px", 0.0)) * ctx.py
        ls = float(p.get("line_spacing", base.get("line_spacing", 1.0)) or 1.0)
        text = "".join(str(r.get("text", "")) for r in _runs(p))
        per = max(box_w_pt / max(size, 0.5), 1.0)                   # 一行放得下多少个半角字宽
        units = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in text)
        total += sp + max(1, math.ceil(units / per)) * size * ls if text else sp
    return total


def _write_text(tf: Any, e: dict[str, Any], ctx: Ctx, warns: list[str], what: str, box: tuple[int, int] | None) -> None:
    base = {k: e[k] for k in TEXT_KEYS if k in e}
    paras = _paras(e.get("paragraphs", e.get("text", "")))
    if sum(len(str(r.get("text", ""))) for p in paras for r in _runs(p)) > MAX_CHARS:
        raise ToolError(f"{what} 超过 {MAX_CHARS} 字，拆成多个文本框")
    anchor = e.get("anchor", "top")
    if anchor not in ANCHOR:
        raise ToolError(f"anchor 只能是 top / middle / bottom：{anchor}")
    tf.word_wrap = bool(e.get("wrap", True))
    tf.vertical_anchor = ANCHOR[anchor]
    for m in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, m, 0)
    for idx, p in enumerate(paras):
        if not isinstance(p, dict):
            raise ToolError(f"段落要写成字符串或对象：{p!r}")
        para = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        a = str(p.get("align", base.get("align") or ""))
        if a:
            if a not in ALIGN:
                raise ToolError(f"align 只能是 {' / '.join(ALIGN)}：{a}")
            para.alignment = ALIGN[a]
        ls = p.get("line_spacing", base.get("line_spacing"))
        if ls is not None:
            v = float(ls)
            para.line_spacing = v if v <= 6 else Pt(v * ctx.py)      # <=6 当倍数，否则按像素
        sb, sa = _num(p, "space_before_px", _num(base, "space_before_px", 0.0)), _num(p, "space_after_px", _num(base, "space_after_px", 0.0))
        if sb:
            para.space_before = Pt(max(sb * ctx.py, 0.1))
        if sa:
            para.space_after = Pt(max(sa * ctx.py, 0.1))
        for r in _runs(p):
            if not isinstance(r, dict):
                raise ToolError(f"run 要写成字符串或对象：{r!r}")
            run = para.add_run()
            run.text = str(r.get("text", ""))
            _apply_font(run, r, base, ctx)
    if box:
        bw, bh = box
        est = _est_height(paras, base, ctx, max(bw, 1.0))
        if bh and est > bh * 1.08:
            first = str(paras[0].get("text") or _runs(paras[0])[0].get("text") or "") if paras else ""
            warns.append(f"{what} 文字可能超出文本框（粗估 {int(est)}pt / 框 {int(bh)}pt）：{first[:24]!r}")


def _put_text(slide: Any, e: dict[str, Any], ctx: Ctx, warns: list[str]) -> None:
    x, y, w, h = ctx.box(e, warns, "文本框")
    box = slide.shapes.add_textbox(x, y, w, h)
    _write_text(box.text_frame, e, ctx, warns, "文本框", box=(_num(e, "w") * ctx.px, _num(e, "h") * ctx.py))


# ---------- deck ----------

def _slide(prs: Any, s: dict[str, Any], ctx: Ctx, media: Path, warns: list[str], n: int) -> int:
    if not isinstance(s, dict):
        raise ToolError(f"第 {n} 页要写成对象")
    slide = prs.slides.add_slide(prs.slide_layouts[6])              # 空白版式，什么都不预设
    bg = s.get("background_color")
    if bg:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(_hex(bg, "background_color"))
    if s.get("background_image"):
        _put_image(slide, {"src": s["background_image"], "x": 0, "y": 0, "w": ctx.cw, "h": ctx.ch, "fit": "stretch"}, ctx, media, warns)
    els = s.get("elements") or []
    if not isinstance(els, list):
        raise ToolError(f"第 {n} 页的 elements 要是列表")
    if len(els) > MAX_ELEMENTS:
        raise ToolError(f"第 {n} 页有 {len(els)} 个元素，超过 {MAX_ELEMENTS}")
    for i, e in enumerate(els, 1):
        if not isinstance(e, dict):
            raise ToolError(f"第 {n} 页第 {i} 个元素要写成对象")
        kind = e.get("type")
        try:
            if kind == "text":
                _put_text(slide, e, ctx, warns)
            elif kind == "shape":
                _put_shape(slide, e, ctx, warns)
            elif kind == "image":
                _put_image(slide, e, ctx, media, warns)
            else:
                raise ToolError(f"type 只能是 text / shape / image：{kind!r}")
        except ToolError as err:
            raise ToolError(f"第 {n} 页第 {i} 个元素：{err}") from None
    return len(els)


# spec 写法、推荐做法、字体与预览检查在 guide.md（第一次调用时给）。字体只用 Joy 的 Mac（Keynote / PowerPoint）上有的；
# 坐标换算：1920 画布 1 px = 6350 EMU。
@server.tool(name="pptx-deck_build", description=(
    "把 JSON 版面规格生成 .pptx，文字是原生文本框，PowerPoint / Keynote 里能直接改。"
    "spec 传 JSON 字符串或工作目录下 JSON 文件的绝对路径。"
    "返回 file（成品路径，可直接 reply(files=[…])）、slides、warnings（元素超出画布、文字可能撑破文本框等粗估提醒）。"))
def pptx_deck_build(spec: str, out_name: str = "") -> str:
    warns: list[str] = []
    s = _read_spec(spec)
    if not isinstance(s, dict) or not isinstance(s.get("slides"), list) or not s["slides"]:
        raise ToolError("spec 要写成 {\"slides\": [ {\"elements\": […]} , … ]}，至少一页")
    if len(s["slides"]) > MAX_SLIDES:
        raise ToolError(f"{len(s['slides'])} 页，超过 {MAX_SLIDES}")
    ctx = Ctx(s, warns)
    media = media_dir()
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(ctx.w_emu), Emu(ctx.h_emu)
    if s.get("title"):
        prs.core_properties.title = str(s["title"])[:200]
    fonts: set[str] = set()
    total = 0
    for n, sl in enumerate(s["slides"], 1):
        total += _slide(prs, sl, ctx, media, warns, n)
        for e in (sl.get("elements") or [] if isinstance(sl, dict) else []):
            if isinstance(e, dict) and e.get("font_family"):
                fonts.add(str(e["font_family"]))
    if out_name:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", out_name):
            raise ToolError(f"out_name 只能是文件名（不含路径），如 claret-deck：{out_name!r}")
        stem = out_name[:-5] if out_name.endswith(".pptx") else out_name
        dst = media / f"{stem}.pptx"
        if dst.exists():                                        # 指定了名字就尽量用原名，重名再加序号
            dst = media / f"{next_name(media, stem)}.pptx"
    else:
        dst = media / f"{next_name(media, 'deck')}.pptx"
    prs.save(str(dst))
    return json.dumps({"file": sandbox_path(dst), "slides": len(s["slides"]), "elements": total,
                       "width_in": round(ctx.w_emu / 914400, 3), "height_in": round(ctx.h_emu / 914400, 3),
                       "px_to_pt": round(ctx.font_pt, 4), "fonts": sorted(fonts), "warnings": warns,
                       "bytes": dst.stat().st_size}, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
