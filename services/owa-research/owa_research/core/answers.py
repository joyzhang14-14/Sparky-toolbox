"""answers 的引文核对（研究容器的 build_answers 与 sparky-toolbox 隔离子代理的引文核对共用）。全部确定性，不调模型。"""
from __future__ import annotations

import re
import unicodedata

# 答案里要在引文中出现的"硬"内容：3 个字符以上、含数字的片段（F362、E257、2026、1,234、3.5%）。
# 1–2 位的裸数字不查：日期常换写法（"9 月 23 日" vs "September 23"），查它会大量误伤
_TOKEN = re.compile(r"[A-Za-z]*\d[\dA-Za-z]*(?:[.,:/-]\d+)*")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().casefold()


def quote_in(quote: str, text: str) -> bool:
    """引文逐字出现在原文里（忽略空白差异、NFKC、大小写）。太短的引文不算。"""
    q = norm(quote)
    return len(q) >= 4 and q in norm(text)


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """整句引文在原文里的位置（忽略空白差异与大小写）；整句找不到返回 None。"""
    words = quote.split()
    if not words or not quote_in(quote, text):
        return None
    m = re.search(r"\s+".join(map(re.escape, words)), text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def missing_tokens(answer: str, quote: str) -> list[str]:
    """答案里的编号 / 多位数字没有出现在引文里的那些（去掉千分位逗号后比较）。空列表 = 引文撑得住答案。"""
    q = norm(quote).replace(",", "")
    out = []
    for t in _TOKEN.findall(unicodedata.normalize("NFKC", answer)):
        t = t.replace(",", "").casefold()
        if len(t) >= 3 and t not in q:
            out.append(t)
    return out


# ---------- 词法核对 v2（D20 C1b，轮内核验 A2 与收尾核对共用；零依赖） ----------
_STOP = {"the", "and", "for", "with", "from", "this", "that", "what", "which", "who", "when", "where", "how", "does", "did", "are", "was", "were", "has", "have"}
_CAP_WORD = re.compile(r"\b[A-Z][A-Za-z'’\-]{2,}\b")
# 编号：E257、F362、C90、A3；允许中间有一个空格或连字符（E 205、E-205）；前后不能紧贴字母数字（但可以紧贴中文："摊位号为E205"）
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,2})[- ]?(\d{1,4})(?![0-9])")


def codes(text: str) -> list[str]:
    """文本里的编号，统一成大写、去掉中间的空格与连字符（'E-205' → 'E205'）。
    字母要大写（避免把 "at 7pm" 这种普通英文当编号）；整段就是一个编号且中间没有分隔时（答案 "e205"）不看大小写。"""
    t = unicodedata.normalize("NFKC", text)
    whole = re.fullmatch(r"\s*([A-Za-z]{1,2})([- ]?)(\d{1,4})\s*", t)
    if whole and (whole.group(1).isupper() or not whole.group(2)):
        return [whole.group(1).upper() + whole.group(3)]
    return list(dict.fromkeys(a.upper() + d for a, d in _CODE_RE.findall(t) if a.isupper()))


_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_MONTHS = {m: i for i, ms in enumerate(("jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august",
                                         "sep sept september", "oct october", "nov november", "dec december"), 1) for m in ms.split()}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))


def key_terms(question: str, others: list[str] = ()) -> list[str]:
    """题目里能拿去查的词：首字母大写的拉丁词（≥3 字符、去停用词）、编号、年份；几道题共有的词（如 UCI）去掉。小写返回。"""
    def raw(q: str) -> list[str]:
        out = [w for w in _CAP_WORD.findall(q) if w.casefold() not in _STOP] + codes(q) + _YEAR.findall(q)
        return list(dict.fromkeys(w.casefold() for w in out))
    shared = {t for o in others if o != question for t in raw(o)}
    return [t for t in raw(question) if t not in shared]


COMMON_RATIO = 0.2       # 已读文本里超过 20% 的行（且至少 10 行）都含的词不算题目主体（摊位表的 "uci" 在 134/277 行）
COMMON_MIN_LINES = 10


def need(terms: list[str]) -> int:
    """一行（或一格）要命中题目关键词的个数才算"关于这道题"：过半（3 个词要 2 个，5 个要 3 个）。"""
    return min(len(terms), max(1, -(-len(terms) * 3 // 5)))


_CODE_TERM = re.compile(r"[a-z]{1,2}\d{1,4}")


def term_hits(text: str, terms: list[str]) -> int:
    """命中的关键词个数。编号类的词（题面 "AI 2025"、"CS 161" 规整成 ai2025、cs161）按原文里的编号比，原文写法带不带空格都认。"""
    low = norm(text)
    in_codes: set[str] | None = None
    n = 0
    for t in terms:
        if re.search(rf"\b{re.escape(t)}\b", low):
            n += 1
        elif _CODE_TERM.fullmatch(t):
            in_codes = {c.casefold() for c in codes(text)} if in_codes is None else in_codes
            n += t in in_codes
    return n


def drop_common(terms: list[str], texts: list[str]) -> list[str]:
    """去掉在已读文本里到处都有的词（核对用）：它们命中了也说明不了"这一行说的是这道题"。"""
    lines = [norm(l) for t in texts for l in t.splitlines() if l.strip()]
    if not lines:
        return terms
    freq = {t: sum(1 for l in lines if re.search(rf"\b{re.escape(t)}\b", l)) for t in terms}
    keep = [t for t in terms if not (freq[t] >= COMMON_MIN_LINES and freq[t] / len(lines) >= COMMON_RATIO)]
    if terms and not keep:                  # 全剔光时主体检查与同行配对都会关掉：留下行频最低的那个
        keep = [min(terms, key=lambda t: freq[t])]
    return keep


def dates(text: str) -> set[tuple[int | None, int, int]]:
    """文本里的日期 → {(年或 None, 月, 日)}：2026-09-23、2026/9/23、9/23、10/01/2026（美式 MM/DD/YYYY）、9 月 23 日、2026 年 9 月 23 日、
    September 23(, 2026)、23 Sep 2026。前导零不计（09/23 = 9/23）。"""
    t = unicodedata.normalize("NFKC", text).replace("*", " ")     # Markdown 强调符（"**December 28,****2024**"）换成空格，年份才连得上
    out: set[tuple[int | None, int, int]] = set()
    for m, d, y in re.findall(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})\b", t):
        out.add((int(y), int(m), int(d)))
    for y, m, d in re.findall(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", t):
        out.add((int(y), int(m), int(d)))
    for m, d in re.findall(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", t):
        out.add((None, int(m), int(d)))
    for y, m, d in re.findall(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t):
        out.add((int(y) if y else None, int(m), int(d)))
    for mon, d, y in re.findall(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", t, re.I):
        out.add((int(y) if y else None, _MONTHS[mon.lower()], int(d)))
    for d, mon, y in re.findall(rf"\b(\d{{1,2}})\s+({_MON})\.?\b(?:,?\s+(\d{{4}}))?", t, re.I):
        out.add((int(y) if y else None, _MONTHS[mon.lower()], int(d)))
    return {x for x in out if 1 <= x[1] <= 12 and 1 <= x[2] <= 31}


_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d:])", re.I)
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}


_RANGE_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(?:-|–|—|~|to|至)\s*(\d{1,2})(?::\d{2})?\s*(am|pm|a\.m\.|p\.m\.)", re.I)


def times(text: str) -> dict[tuple[int, int], set[str]]:
    """文本里的钟点 → {(时, 分) 24 小时制: 写出来的数字}。只认带冒号或带 am/pm 的（19:00、7pm、7:00 PM），裸数字不算钟点。
    时间段只在末尾写 am/pm 时，起点跟着末尾（2–5 p.m. = 14:00–17:00；11–1 p.m. 的起点是上午 11 点）。"""
    t = unicodedata.normalize("NFKC", text)
    out: dict[tuple[int, int], set[str]] = {}

    def add(h: str, m: str, ap: str) -> None:
        hh, mm = int(h), int(m or 0)
        if ap:
            if not 1 <= hh <= 12:
                return
            hh = hh % 12 + (12 if ap.lower().startswith("p") else 0)
        if hh <= 23 and mm <= 59:
            out.setdefault((hh, mm), set()).update(x for x in (h, m) if x)

    for h, m, ap in _TIME_RE.findall(t):
        if m or ap:
            add(h, m, ap)
    for h1, m1, h2, ap in _RANGE_RE.findall(t):
        flip = int(h2) < int(h1) < 12                  # 起点比终点大：跨了正午（或午夜），起点取另一半天
        add(h1, m1, ("am" if ap.lower().startswith("p") else "pm") if flip else ap)
    return out


def _num(n: str) -> str:
    """数字的比较形式：去千分位，去掉末尾的 .00（$1,200.00 = $1,200）。"""
    n = n.replace(",", "")
    return re.sub(r"\.0+$", "", n)


def _date_in(a: tuple[int | None, int, int], pool: set[tuple[int | None, int, int]]) -> bool:
    return any(a[1:] == b[1:] and (a[0] is None or b[0] is None or a[0] == b[0]) for b in pool)


def same_line_pair(answer: str, text: str, terms: list[str]) -> list[str]:
    """答案里的编号必须和题目关键词（过半）出现在同一行；该行是 ` | ` 分格的表格行时，编号要在关键词所在格或紧邻的下一格。返回没配上的编号。
    答案是纯数字（"205"）、而原文里这个数字只作为某个编号的一部分出现时，按那些编号配对。"""
    if not terms:
        return []
    ans = unicodedata.normalize("NFKC", answer).strip()
    # 答案一侧也认小写编号（"e 205"、"booth e-205"），但只在原文里确有这个编号时才算，免得 "at 7pm" 被当成 AT7
    in_text_codes = codes(text)
    wanted = list(dict.fromkeys(codes(ans) + [c for c in codes(ans.upper()) if c in in_text_codes]))
    digits_only = re.fullmatch(r"\d{1,4}", ans) is not None
    if digits_only:
        in_text = [c for c in in_text_codes if re.sub(r"^[A-Z]+", "", c) == ans]
        if not in_text or re.search(rf"(?<![A-Za-z0-9]){ans}(?![0-9])", text):
            return []                              # 原文里这个数字单独出现过（不只是编号的一部分）：不按编号配对
        wanted = list(dict.fromkeys(in_text))
    if not wanted:
        return []

    k = need(terms)

    def has_term(t: str) -> bool:
        return term_hits(t, terms) >= k

    bad = []
    for code in wanted:
        ok = False
        for line in text.splitlines():
            if code not in codes(line) or not has_term(line):
                continue
            if " | " in line:
                cells = line.split(" | ")
                hits = [i for i, c in enumerate(cells) if has_term(c)]
                where = [i for i, c in enumerate(cells) if code in codes(c)]
                ok = any(w in (h, h + 1) for h in hits for w in where)
            else:
                ok = True
            if ok:
                break
        if not ok:
            bad.append(ans if digits_only else code)
    return bad


def spans_lines(quote: str, text: str) -> bool:
    """引文在原文里跨了行：整段找得到，但没有哪一行单独包含它（可能是两行拼成一句）。"""
    return quote_in(quote, text) and not any(quote_in(quote, line) for line in text.splitlines())


def unsupported_terms(answer: str, quote: str, question: str = "", kind: str = "quote", terms: list[str] | None = None,
                      context: str | None = None, source: str | None = None) -> list[str]:
    """引文撑不住答案的地方（空列表 = 撑得住）。只对 kind=quote 查：
    (1) 答案里的数字都要在引文里（日期按 (年, 月, 日) 比，"9 月 23 日" 与 "September 23" 算同一天；钟点 12/24 小时制互认，19:00 = 7pm；
        金额去千分位与末尾 .00）；
    (2) 答案里首字母大写的拉丁词要在引文里（日期对上时，月份名与星期名不查）；
    (3) 答案没有拉丁词时，题目关键词过半在 context（默认引文；邮件路径传整封邮件段，含 From / Subject）里；
    (4) 编号要与题目关键词同行（same_line_pair）；给了 source（引文所在的原文）时，带编号的答案（含只写数字部分的）的引文不能在原文里跨行。"""
    if kind != "quote":
        return []
    terms = key_terms(question) if terms is None else terms
    a = unicodedata.normalize("NFKC", answer)
    q = norm(quote)
    out: list[str] = []
    a_dates, q_dates = dates(a), dates(quote)
    covered: set[str] = set()          # 日期里被引文撑住的数字：月、日；年份只有引文里写了同一年才算
    for d in a_dates:
        for b in q_dates:
            if d[1:] == b[1:] and (d[0] is None or b[0] is None or d[0] == b[0]):
                covered |= {str(d[1]), f"{d[1]:02d}", str(d[2]), f"{d[2]:02d}"} | ({str(d[0])} if d[0] is not None and b[0] == d[0] else set())
    # 答案日期带年份、引文里同月日的日期没写这一年：年份不能从同一行别处的数字（别的日期列）借来
    no_year = {str(d[0]) for d in a_dates if d[0] is not None and not any(d[1:] == b[1:] and b[0] == d[0] for b in q_dates)}
    out += sorted(no_year)
    q_times = times(quote)
    for hm, written in times(a).items():               # 钟点对上了：答案里写钟点用的数字都算撑住
        if hm in q_times:
            covered |= written
    norm_nums = " ".join(_num(x) for x in _NUM.findall(q))
    for n in _NUM.findall(a):
        n = _num(n)
        if n in covered or n in no_year or re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", norm_nums):
            continue
        out.append(n)
    date_ok = any(_date_in(d, q_dates) for d in a_dates)
    for w in _CAP_WORD.findall(a):
        lw = w.casefold()
        if lw in _STOP or (date_ok and (lw.rstrip(".") in _MONTHS or lw in _WEEKDAYS)):
            continue
        if not re.search(rf"\b{re.escape(lw)}\b", q):
            out.append(w)
    if not re.search(r"[A-Za-z]{3,}", a) and terms and term_hits(quote if context is None else context, terms) < need(terms):
        out.append("主体:" + "/".join(terms[:3]))
    out += [f"配对:{c}" for c in same_line_pair(a, quote, terms)]
    if source is not None and spans_lines(quote, source):
        bare = a.strip()                    # 纯数字答案（"257"）在原文里只作为编号的一部分出现时也算编号，口径同 same_line_pair
        if codes(a) or set(codes(a.upper())) & set(codes(source)) or (
                re.fullmatch(r"\d{1,4}", bare) and any(re.sub(r"^[A-Z]+", "", c) == bare for c in codes(source))):
            out.append("跨行")
    return list(dict.fromkeys(out))


def support_line(answer: str, text: str, question: str = "", terms: list[str] | None = None) -> str | None:
    """已读文本里第一行能单独撑住答案的原文行（按 unsupported_terms 的规则）；没有返回 None。"""
    terms = key_terms(question) if terms is None else terms
    a = norm(answer)
    for line in text.splitlines():
        if len(line.strip()) >= 4 and (not a or any(tok in norm(line) for tok in a.split()[:3])) and not unsupported_terms(answer, line, question, "quote", terms):
            return line.strip()[:400]
    return None


# 答案像"没找到"（研究循环的轮内核验与 absent 判断共用）。中文前缀后面不要求词边界（"未找到2025年全年数据"）
_NF_EN = (r"not\s*found|none|null|n/?a|unknown|not\s+disclosed|not\s+available|not\s+stated|not\s+reported|not\s+present"
          r"|the\s+provided\s+sources\s+do\s+not")
_NF_ZH = "未找到|没找到|没有找到|无法找到|未披露|暂无|未提及|不详|无相关|无公开|未公布|尚未|未查到|不在"
NOT_FOUND = re.compile(rf"^\s*(?:(?:{_NF_EN})\b|{_NF_ZH}|没有|无)", re.I)
NOT_FOUND_ANY = re.compile(rf"(?<![A-Za-z])(?:{_NF_EN})\b|{_NF_ZH}", re.I)    # 出现在任何位置都算；单字"无""没有"只看开头
_YEAR_ONLY = re.compile(r"(?:19|20)\d{2}")


def absent_but_answered(answer: str) -> bool:
    """模型标 kind=absent 却写了答案时，只有答案含数字 / 日期（年份不算）、又没有"没找到"一类说法（开头或任何位置）才当真答案（D26、D28）。"""
    nums, ds = answer_facts(answer)
    nums = {n for n in nums if not _YEAR_ONLY.fullmatch(n)}
    return bool(nums or ds) and not NOT_FOUND.match(answer) and not NOT_FOUND_ANY.search(answer)


# ---------- 代码补引文（D26）：按答案里的数字回原文找行 ----------
CODE_QUOTE_MAX = 1500      # 候选行的长度上限（同引文上限）
GATE_ABOVE = 2             # 英文词闸看候选行本身和它上方几行（小节标题，如 "**Data Center**"）
# 元数据行不当候选：邮件头（含正文里转发、回复带出的头）、引用行、附件页脚，以及 services/owa-research/owa_research/worker.py 插入的表格标题行
_META_LINE = re.compile(r"^\s*(?:(?:From|Date|Sent|To|Cc|Subject)\s*:|On .+ wrote:\s*$|>|Page \d+ of \d+\s*$|## 表格 \d+(?:（第 \d+ 页）)?\s*$|# 解析出的表格)"
                        r"|Forwarded message", re.I)


def answer_facts(answer: str) -> tuple[set[str], set[tuple[int | None, int, int]]]:
    """答案里要回原文找的数字与日期。数字：≥2 位或带小数点，去 $ 与千分位、去末尾 .00；日期按 (年, 月, 日)，它用到的数字不再单独找。"""
    a = unicodedata.normalize("NFKC", answer)
    ds = dates(a)
    in_dates = {x for d in ds for x in (str(d[1]), f"{d[1]:02d}", str(d[2]), f"{d[2]:02d}", str(d[0]))}
    nums = {_num(n) for n in _NUM.findall(a) if len(re.sub(r"\D", "", n)) >= 2 or "." in n}
    return {n for n in nums if n not in in_dates}, ds


GENERIC = {"fiscal", "year", "years", "total", "end", "date", "full", "fy"}   # 泛词：单独出现不算指标（D27）
PHRASE_WINDOW = 12         # 正文里答案的数要是指标短语后 12 个词内出现的第一个数（D27）
_RUN = re.compile(r"[A-Za-z][A-Za-z&'’]*(?:[ \-]+[A-Za-z][A-Za-z&'’]*)*")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*\s*$)")
_UNIT = re.compile(r"\s*(billion|million|thousand|%|percent)", re.I)


def _norm_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("*", " ")


_FUNC = {"what", "was", "is", "are", "were", "of", "for", "in", "the", "a", "an", "did", "does", "do", "how", "much", "many", "by", "at",
         "on", "to", "and", "which", "when", "who", "during", "its", "their"}
_EDGE = _STOP | {"of", "at", "in", "on", "a", "an", "to"}
_PAREN = re.compile(r"[（(]([^（）()]*)[）)]")


def _notes(question: str) -> list[str]:
    """题面里注明原文用词的括号内容：全角括号"（Revenue）"，或紧跟在中文后面的半角括号；英文名字里自带的括号（"Beta Beta Beta (Tri Beta)"）不算。"""
    q = unicodedata.normalize("NFC", question)
    out = []
    for m in _PAREN.finditer(q):
        before = q[:m.start()].rstrip()
        if m.group(0)[0] == "（" or (before and re.match(r"[\u3400-\u9fff]", before[-1])):
            out.append(m.group(1))
    return out


def _runs(text: str) -> list[list[str]]:
    """连续英文词（带数字的词如 FY2025 当分隔），每段拆成词。"""
    t = re.sub(r"\b\w*\d\w*\b", " | ", _norm_text(text))
    return [[x for x in re.split(r"[ \-]+", m.group(0)) if x] for m in _RUN.finditer(t)]


def _is_entity(tok: str) -> bool:
    """公司名一类的专名词：全大写（NVIDIA、AMD）、带所有格（NVIDIA's）、或首字母大写（Nvidia、Microsoft）。"""
    return tok.isupper() or tok.endswith(("'s", "’s")) or tok[:1].isupper()


def _phrase(toks: list[str]) -> str | None:
    while toks and toks[0].casefold() in _EDGE:
        toks = toks[1:]
    while toks and toks[-1].casefold() in _EDGE:
        toks = toks[:-1]
    if not toks or all(x.casefold() in GENERIC | _STOP for x in toks) or (len(toks) == 1 and toks[0].isupper()):
        return None
    return " ".join(toks).casefold()


def _pure_english(q: str) -> bool:
    return not re.search(r"[\u3400-\u9fff]", q)


def metric_phrases(question: str, others: list[str] = ()) -> list[str]:
    """题面里的英文指标短语（D27、D28，闸），按短语整体匹配（operating income、data center）：
    - 题面有括号英文时，只用括号里的英文（括号外的 Nvidia、Advanced Micro Devices 这类专名不算）；
    - 纯英文题面按 what / was / is / of / for / in 这类词切开，去掉公司名（全大写、所有格、首字母大写）与泛词，只留指标名词短语；
    - 其余（中文题面、括号里没有英文）用题面里连续的英文词，照 D27：去两头停用词，全是泛词 / 停用词的、单个全大写词不算。
    几道题共有的短语不删（D28：同问几题 Revenue 时都要能补）；公司绑定由 question_entities 管。"""
    q = _norm_text(question)
    inside = [_phrase(t) for note in _notes(question) for t in _runs(note)]
    inside = [p for p in inside if p]
    if inside:
        return list(dict.fromkeys(inside))
    if _pure_english(q):
        out = []
        for toks in _runs(q):
            chunk: list[str] = []
            for tok in toks + ["what"]:                     # 末尾哨兵把最后一段收掉
                if tok.casefold() in _FUNC or _is_entity(tok) and tok is not toks[0] or tok.casefold() in GENERIC:
                    if (p := _phrase(chunk)):
                        out.append(p)
                    chunk = []
                elif not (tok is toks[0] and _is_entity(tok) and tok.casefold() not in _FUNC and len(toks) > 1 and tok.isupper()):
                    chunk.append(tok)
        return list(dict.fromkeys(out))
    return list(dict.fromkeys(p for t in _runs(q) if (p := _phrase(t))))


def question_entities(question: str) -> list[str]:
    """题面里要和原文绑定的公司名一类专名（D28）：有括号英文时是括号外的英文专名，纯英文题面是全大写 / 所有格 / 句中首字母大写的词；
    中文题面没有括号英文时不绑（那时英文短语本身就是对象，如摊位表的社团名）。泛词、带数字的词（FY2025）不算。"""
    q = _norm_text(question)
    notes = _notes(question)
    has_paren = any(_runs(n) for n in notes)
    if not has_paren and not _pure_english(q):
        return []
    outside = question
    for n in notes:
        outside = outside.replace(n, " ")
    outside = _norm_text(outside)
    out = []
    for toks in _runs(outside):
        group: list[str] = []
        for k, tok in enumerate(toks + ["what"]):                 # 相邻的专名词合成一个（Advanced Micro Devices）
            word = re.sub(r"['’]s$", "", tok)
            ent = (word.casefold() not in GENERIC | _STOP | _FUNC and len(word) >= 2
                   and (tok.isupper() or tok.endswith(("'s", "’s")) or (_is_entity(tok) and (has_paren or k > 0))))
            if ent:
                group.append(word.casefold())
            if not ent or tok.endswith(("'s", "’s")):
                if group:
                    out.append(" ".join(group))
                group = []
    return list(dict.fromkeys(out))


def _entity_ok(entities: list[str], lines: list[str], k: int, head: str) -> bool:
    """公司名出现在文档开头（标题或前 HEAD_LINES 个非空行），或候选行及其上方 GATE_ABOVE 行（D28）。"""
    if not entities:
        return True
    first = [x for x in lines if x.strip()][:HEAD_LINES]
    near = lines[max(0, k - GATE_ABOVE):k + 1]
    text = _norm_text(" ".join([head, *first, *near]))

    def found(e: str) -> bool:
        words = e.split()
        forms = [e] + (["".join(w[0] for w in words)] if len(words) > 1 else [])     # 多词公司名也认首字母缩写（AMD）
        return any(_phrase_re(f).search(text) for f in forms)
    return all(found(e) for e in entities)


HEAD_LINES = 5             # 公司绑定看文档开头几行（新闻稿首段、邮件头都会写公司名）


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z])" + r"[\s\-]+".join(map(re.escape, phrase.split())) + r"(?![A-Za-z])", re.I)


def _hits(text: str, phrases: list[str]) -> bool:
    t = _norm_text(text)
    return any(_phrase_re(p).search(t) for p in phrases)


def _has_answer(text: str, nums: set[str], ds: set) -> bool:
    have = {_num(n) for n in _NUM.findall(_norm_text(text))}
    return bool(nums & have) or any(_date_in(d, dates(text)) for d in ds)


def _unit_conflict(answer: str, line: str, nums: set[str]) -> bool:
    """答案里数后面的单位（billion / million / %）和原文里同一个数后面的单位对不上（原文不带单位时放行）。"""
    a, t = _norm_text(answer), _norm_text(line)
    for m in _NUM.finditer(a):
        ua = _UNIT.match(a, m.end())
        if not ua or _num(m.group(0)) not in nums:
            continue
        for n in _NUM.finditer(t):
            if _num(n.group(0)) == _num(m.group(0)) and (ul := _UNIT.match(t, n.end())):
                if ul.group(1).casefold().replace("percent", "%") != ua.group(1).casefold().replace("percent", "%"):
                    return True
    return False


def near_metric(lines: list[str], k: int, answer: str, phrases: list[str], other_phrases: list[str] = ()) -> bool:
    """D27：第 k 行里答案的数紧挨题目问的指标。
    表格行：行标签（第一个非空格）命中指标短语；或指标所在格就是数所在格、或紧挨在它前面（摊位表一行两个社团）；
            或上方 GATE_ABOVE 行内的表头行（不含金额的表格行，如 "Year Ended"）命中指标。
    正文：上方 GATE_ABOVE 行内有小节标题（Markdown 标题或 **…** 行）时，标题要命中指标；没有标题时，答案的数要是指标短语后
          PHRASE_WINDOW 个词内的第一个数，且短语前面紧挨着的不是 "segment" 或别的题的指标短语（那是细分指标）。
    单位冲突（% 对 billion、million 对 billion）时拒。"""
    if not phrases:
        return False
    line = lines[k]
    nums, ds = answer_facts(answer)
    if _unit_conflict(answer, line, nums):
        return False
    if " | " in line or line.lstrip().startswith("|"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        first = next((c for c in cells if c), "")
        if _hits(first, phrases):
            return True
        hit = [i for i, c in enumerate(cells) if _hits(c, phrases)]
        where = [i for i, c in enumerate(cells) if _has_answer(c, nums, ds)]
        if any(w in (h, h + 1) for h in hit for w in where):
            return True
        for j in range(k - 1, max(-1, k - 1 - GATE_ABOVE), -1):
            row = lines[j]
            if (" | " in row or row.lstrip().startswith("|")) and _hits(row, phrases) and not re.search(r"\d", re.sub(r"\b(?:19|20)\d{2}\b", "", row)):
                return True
        return False
    above = [lines[j] for j in range(max(0, k - GATE_ABOVE), k) if lines[j].strip()]
    if above and _HEADING.match(above[-1]):
        return _hits(above[-1], phrases)
    t = _norm_text(line)
    blockers = [_phrase_re(p) for p in other_phrases if p not in phrases] + [re.compile(r"(?<![A-Za-z])segment(?![A-Za-z])", re.I)]
    for p in phrases:
        if "segment" in p.split():
            blockers = blockers[:-1]
        for m in _phrase_re(p).finditer(t):
            before = " ".join(t[:m.start()].split()[-3:])
            if any(b.search(before) for b in blockers):
                continue
            window = " ".join(t[m.end():].split()[:PHRASE_WINDOW])
            if ds:
                if all(_date_in(d, dates(window)) for d in ds) and (not nums or _first_num_ok(window, nums)):
                    return True
            elif _first_num_ok(window, nums):
                return True
    return False


def _first_num_ok(window: str, nums: set[str]) -> bool:
    first = _NUM.search(window)
    return bool(first) and _num(first.group(0)) in nums


def quote_passes(answer: str, quote: str, question: str, others: list[str], text: str, terms: list[str], context: str | None = None,
                 head: str = "") -> bool:
    """模型自己抄的引文（已逐字在原文 text 里）过不过闸与核对：推断答案先试它，过了就不换成代码找的行（D26 复验第 4 步）。"""
    lines = text.splitlines()
    k = next((i for i, line in enumerate(lines) if quote_in(quote, line)), None)
    phrases = metric_phrases(question, others)
    others_p = [p for o in others if o != question for p in metric_phrases(o, [x for x in others if x != o])]
    return (k is not None and near_metric(lines, k, answer, phrases, others_p) and _entity_ok(question_entities(question), lines, k, head)
            and not unsupported_terms(answer, quote, question, "quote", terms, context=context, source=text))


def code_quotes(answer: str, question: str, others: list[str], sources: list[tuple[str, str]], terms: list[str],
                contexts: dict[str, str] | None = None, heads: dict[str, str] | None = None):
    """按答案里的数字 / 日期回本次已读原文逐行找引文（D26），按来源、文档顺序逐个给出 (source_id, 原文行)：
    该行含答案的全部数字与日期；答案的数紧挨题面的指标短语（near_metric，D27）；题面里的公司名出现在文档开头（heads 给的标题或前几行）
    或候选行附近（_entity_ok，D28）；unsupported_terms 对它返回 []。题面没有指标短语时不补。"""
    nums, ds = answer_facts(answer)
    phrases = metric_phrases(question, others)
    if (not nums and not ds) or not phrases:            # 题面没有指标短语：不补引文，维持未核实（D27）
        return
    others_p = [p for o in others if o != question for p in metric_phrases(o, [x for x in others if x != o])]
    entities = question_entities(question)
    for sid, text in sources:
        lines = text.splitlines()
        for k, line in enumerate(lines):
            if not 4 <= len(line.strip()) <= CODE_QUOTE_MAX or _META_LINE.search(line):
                continue
            have = {_num(n) for n in _NUM.findall(unicodedata.normalize("NFKC", line))}
            if not nums <= have or not all(_date_in(d, dates(line)) for d in ds):
                continue
            if not near_metric(lines, k, answer, phrases, others_p) or not _entity_ok(entities, lines, k, (heads or {}).get(sid, "")):
                continue
            if not unsupported_terms(answer, line, question, "quote", terms, context=(contexts or {}).get(sid), source=text):
                yield sid, line.strip()
