"""speak 工具：用 Sparky 的声音（「安静的少女」，Breeze TTS 2 克隆）把中文文字念成语音，成品给 reply 当附件发。
每次调用请 stbcc-docker 起一次性 stb-docker-base 容器接力跑，跑完就退、释放显存；不换模型，和主模型同时跑（念的时候整机多占约 6 GB）：
  1. 生成（Breeze 环境的 python 跑 _speak_gen.py）：按句末标点切成每块 ≤ LIMIT 字（Breeze 一次只念得了 2 分钟左右，再长会跳句）；
     没语气的块用长参考录音克隆，有语气的块用短参考录音 + 语气指令；segments 给了就每段各用自己的语气；
  2. 停顿检查与拼接（~/venvs/qwen-asr 的 python 跑 _speak_check.py）：每块和原文对齐，查「没标点却停了」「句末没停」；
  3. 有问题的块重念一次（步骤 1 只念这些块），再查一次，每块挑问题少的那条拼起来。
声音配置（参考录音和文字稿、参数、语气指令）在 VOICE/voice.json，2026-10 Joy 逐轮听过定下的；念之前去掉括号里的舞台说明（如「（笑）」会被念出来）和表情符号；
方括号的声音标记（[笑] [叹气] 等）留给 Breeze 发声，停顿检查时从原文去掉。开跑前查宿主可用内存，不够就报平台故障，由主模型决定。
成品存工作目录 media/<运行 id>/speech-N.ogg（Opus）与同名 .wav，各块的录音在 speech-N-parts/。
环境变量：STB_WORK_ROOT、STB_RUN_ID、STB_RUN_REF（网关一律给；容器属于这次运行，运行结束就停）。
"""
import json
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from control_center.tool_kit import call_timeout, platform_fault, timeout_note, tool_timeout
from control_center import media

HERE = Path(__file__).resolve().parent
GEN, CHECK = HERE / "_speak_gen.py", HERE / "_speak_check.py"
TTS_REPO = Path("~/apps/breeze-tts").expanduser()        # Breeze 推理代码（github breezeblue-ai/breeze-tts，commit 6eba901）
TTS_PY = TTS_REPO / ".venv/bin/python"
TTS_VENV = TTS_PY.parents[1]                             # uv 建的 venv（qwen-tts 0.1.1 + transformers 4.57.3 + torch 2.11 cu130）
TTS_CKPT = Path("~/models/breeze-tts-2").expanduser()   # HF BreezeBlue/Breeze-TTS-2
ASR_PY = Path("~/venvs/qwen-asr/bin/python").expanduser()
ASR_VENV = ASR_PY.parents[1]                             # python 软链到 /usr/bin/python3，镜像里有同版本的
ASR_HUB = Path("~/.cache/huggingface/hub").expanduser()
VOICE = Path("~/research/voicestudio/sparky-voice").expanduser()
TIMEOUT_S = 30 * 60
GEN_NEED_GB, CHECK_NEED_GB = 8, 4    # 开跑前宿主可用内存下限：Breeze 念的时候整机多占约 6.2 GB（10-25 主模型在线实测）；对齐模型约 2 GB
LIMIT, GAP_S = 200, 0.15             # 每块最多 LIMIT 字（10-25 实测：千字长文切块全念到，一口气念会跳句）；块间静音
MOOD_MAX = 100                       # 自己写的语气描述最多几个字
CACHE = {"XDG_CACHE_HOME": "/tmp/stb-speak-cache", "TRITON_CACHE_DIR": "/tmp/stb-speak-cache/triton",
         "TORCHINDUCTOR_CACHE_DIR": "/tmp/stb-speak-cache/inductor", "HOME": "/tmp"}
GEN_ENV = {"HF_HOME": "/tmp/stb-speak-hf", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
           "HF_TOKEN_PATH": "/tmp/stb-no-hf-token", **CACHE}
CHECK_ENV = {"HF_HOME": str(ASR_HUB.parent), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
             "HF_TOKEN_PATH": "/tmp/stb-no-hf-token", **CACHE}
BRACKETS = re.compile(r"[（(][^（）()]*[）)]")
TAG = re.compile(r"\[[^\[\]]*\]\s*")
EXC = re.compile(r"^[\w.]*(Error|Exception)\b")

server = MCPServer("speak", instructions="用 Sparky 的声音把中文文字念成语音")


def _avail_gb() -> float:
    return media.avail_gb()


def _gen_env() -> dict:
    """生成容器的环境：LD_LIBRARY_PATH 指到 venv 自带的 CUDA 13 运行库（nvidia/cu13/lib）。声码器的 Snake 激活要 NVRTC 现编内核，
    NVRTC 要找 libnvrtc-builtins.so.13.0；宿主上靠系统的 /usr/local/cuda 找到，容器里没有它。"""
    lib = next(TTS_VENV.glob("lib/python3*/site-packages/nvidia/cu13/lib"), None)
    return {**GEN_ENV, **({"LD_LIBRARY_PATH": str(lib)} if lib else {})}


def _py_base() -> Path:
    """venv 的 python 软链指向的 Python 安装目录，照软链里写的路径挂进容器：uv 的 cpython-3.11-… 本身也是指向带小版本号目录的软链，
    只挂真实目录的话，容器里没有软链经过的这条路径，python 起不来（退出码 127）。"""
    return Path(os.readlink(TTS_PY)).parents[1] if TTS_PY.is_symlink() else TTS_PY.parents[1]


def clean(text: str) -> str:
    """去掉括号里的舞台说明、剩下的括号、表情符号和多余空白。"""
    t = re.sub(r"[()（）]", "", BRACKETS.sub("", text))
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("So", "Cs", "Co", "Cf"))
    return re.sub(r"\s+", " ", t).strip()


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """按句末标点切成每块 ≤ limit 字；一句就超过 limit 的，在逗号等处再切，没有就硬切。"""
    sents = []
    for s in (x for x in re.split(r"(?<=[。！？!?…])", text) if x.strip()):
        while len(s) > limit:
            cut = max(s.rfind(c, 0, limit) for c in "，,、；;：:")
            cut = cut + 1 if cut > 0 else limit
            sents.append(s[:cut])
            s = s[cut:]
        sents.append(s)
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) > limit:
            out.append(cur)
            cur = ""
        cur += s
    return out + ([cur] if cur else [])


def _voice() -> dict:
    try:
        return json.loads((VOICE / "voice.json").read_text())
    except (OSError, ValueError) as e:
        raise platform_fault(f"Sparky 的声音配置读不了：{VOICE / 'voice.json'}（{e}；要 Joy 处理，这次先用文字回复）") from None


def _run(what: str, cmd: list[str], read: list[Path], write: list[Path], env: dict, need: float, log: Path, deadline: float, given: bool,
         t0: float, disk: str, reason: str) -> subprocess.CompletedProcess:
    avail = _avail_gb()
    if avail < need:
        raise platform_fault(media.low_memory(f"{what}没开跑", avail, need))
    left = deadline - time.monotonic()
    if left <= 0:
        raise tool_timeout(timeout_note("念", time.monotonic() - t0, deadline - t0, given))
    try:
        r = media.run_gpu("speak", cmd, read=read, write=write, timeout=left, need_gb=need, env=env, disk=disk, reason=reason)
    except subprocess.TimeoutExpired:
        raise tool_timeout(timeout_note("念", time.monotonic() - t0, deadline - t0, given)) from None
    except OSError as e:
        raise platform_fault(f"{what}容器没起来（stbcc-docker：{e}）") from None
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines() or ["（没有 stderr）"]
        err = next((x for x in reversed(lines) if EXC.match(x)), lines[-1])[:300]   # PyTorch 的 CUDA 报错后面还跟几行调试提示，取异常那一行
        log.write_text(r.stderr)
        raise platform_fault(f"{what}进程没有正常结束（退出码 {r.returncode}）：{err}（完整输出在 {media.sandbox_path(log)}）")
    return r


class Segment(BaseModel):
    text: str = Field(description="这一段要念的中文")
    mood: str = Field("平静", description="这一段的语气，写法同 speak_run 的 mood")


def _instruct(mood: str, moods: dict) -> str | None:
    """mood 是 voice.json 里的名字就用 Joy 调过的那句指令（平静 = 不带指令）；否则把这句描述直接当指令。"""
    m = mood.strip()
    if m in moods:
        return moods[m]
    if not m or len(m) > MOOD_MAX:
        raise ToolError(f"mood 写 {' / '.join(moods)} 之一，或用一句话（{MOOD_MAX} 字以内）描述语气，比如「压低声音说悄悄话」（你给的是 {mood!r}）")
    return m


@server.tool(name="speak_run", description=(
    "用 Sparky 的声音把中文文字念成 ogg 录音"))
def speak_run(text: str = "", mood: str = "平静", segments: Annotated[list[Segment], Field(max_length=50, description=(
        "一篇里分段换语气时用：[{text, mood}, …]，按顺序念完拼成一条录音；给了 segments 就不写 text、mood"))] = [],
        timeout: int | str = 0, disk: str = "", reason: str = "") -> str:
    limit = call_timeout(timeout, TIMEOUT_S)
    moods = _voice().get("instructions") or {}
    if segments and text.strip():
        raise ToolError("text 和 segments 只写一个：整篇一个语气写 text + mood，分段换语气写 segments")
    segs = [(g.text, g.mood) for g in map(Segment.model_validate, segments)] or [(text, mood)]
    plan = [(clean(x), _instruct(md, moods)) for x, md in segs]
    if not any(x for x, _ in plan):
        raise ToolError("去掉括号里的舞台说明和表情符号以后，没有能念的文字")
    for p, what in ((TTS_PY, "Breeze TTS 环境"), (ASR_PY, "停顿检查用的语音识别环境")):
        if not p.exists():
            raise platform_fault(f"{what}没装：{p}（要 Joy 装好；这次先用文字回复）")
    if not (TTS_CKPT / "config.json").is_file():
        raise platform_fault(f"Breeze TTS 的权重不在：{TTS_CKPT}（要 Joy 处理；这次先用文字回复）")
    t0 = time.monotonic()
    deadline = t0 + limit
    d = media.media_dir()
    name = media.next_name("speech")
    parts_dir = d / f"{name}-parts"
    parts_dir.mkdir()
    texts, instr = [], []
    for x, ins in plan:
        for c in chunks(x) if x else []:
            texts.append(c)
            instr.append(ins)
    gen_read = [TTS_VENV, _py_base(), TTS_REPO, TTS_CKPT, VOICE, GEN]
    run = dict(given=bool(timeout), t0=t0, disk=disk, reason=reason, deadline=deadline)

    def gen(idx: list[int], take: int) -> None:
        job = parts_dir / f"gen-{take}.json"
        job.write_text(json.dumps({"ckpt": str(TTS_CKPT), "repo": str(TTS_REPO), "voice_dir": str(VOICE), "items": [
            {"out": str(parts_dir / f"c{i + 1}-{take}.wav"), "text": texts[i], "instruct": instr[i]} for i in idx]}, ensure_ascii=False))
        _run("生成", [str(TTS_PY), str(GEN), str(job)], gen_read, [d], _gen_env(), GEN_NEED_GB, parts_dir / f"gen-{take}.stderr.log", **run)

    def check(takes: list[list[str]], checked: dict, n: int) -> dict:
        job, out = parts_dir / f"check-{n}.json", parts_dir / f"check-{n}-result.json"
        job.write_text(json.dumps({"chunks": [{"text": TAG.sub("", x), "takes": tk} for x, tk in zip(texts, takes)], "checked": checked, "gap_s": GAP_S,
                                   "final_wav": str(d / f"{name}.wav"), "final_ogg": str(d / f"{name}.ogg")}, ensure_ascii=False))
        _run("停顿检查", [str(ASR_PY), str(CHECK), str(job), str(out)], [ASR_VENV, ASR_HUB, CHECK], [d], CHECK_ENV, CHECK_NEED_GB,
             parts_dir / f"check-{n}.stderr.log", **run)
        return json.loads(out.read_text())

    gen(list(range(len(texts))), 1)
    takes = [[str(parts_dir / f"c{i + 1}-1.wav")] for i in range(len(texts))]
    rep = check(takes, {}, 1)
    bad = [i for i, w in enumerate(rep["chosen"]) if rep["checked"][w]["issues"]]
    if bad:
        gen(bad, 2)
        for i in bad:
            takes[i].append(str(parts_dir / f"c{i + 1}-2.wav"))
        rep = check(takes, rep["checked"], 2)
    left = [{"chunk": i + 1, **x} for i, w in enumerate(rep["chosen"]) for x in rep["checked"][w]["issues"]]
    info = {"file": media.sandbox_path(d / f"{name}.ogg"), "wav_file": media.sandbox_path(d / f"{name}.wav"), "duration_s": rep["duration_s"],
            "chunks": len(texts), "redone_chunks": [i + 1 for i in bad], "pause_issues": left, "seconds": round(time.monotonic() - t0, 1)}
    if [x for x, _ in plan] != [x.strip() for x, _ in segs]:
        info["spoken_text"] = "".join(x for x, _ in plan)
    return json.dumps(info, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
