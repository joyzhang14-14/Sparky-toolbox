"""transcribe 工具：语音转文字。Qwen3-ASR-1.7B 转写 + Qwen3-ForcedAligner-0.6B 逐词时间戳，给剪视频、做字幕用。
每次调用请 stbcc-docker 起一个一次性 stb-docker-base 容器（~/venvs/qwen-asr 的 python 跑 _transcribe_run.py，media.run_gpu），跑完就退、释放显存；
不换模型，和主模型同时跑（约 5 GB）。
开跑前查可用内存，不够（或跑的时候显存不足）就报平台故障，写明可用与需要的内存，由主模型决定。模型在 HF 缓存里，离线加载。
时限默认 TIMEOUT_S，主模型可以按调用给 timeout（不设上限）。
完整结果（逐词时间戳）存工作目录 media/<运行 id>/transcript-N.json，另存同名 .srt 字幕；返回给主模型的是分好段的文字和时间。
环境变量：STB_WORK_ROOT、STB_RUN_ID、STB_RUN_REF（网关一律给；容器属于这次运行，运行结束就停）。
"""
import json
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from control_center.tool_kit import DISK_DOC, REASON_DOC, TIMEOUT_DOC, call_timeout, platform_fault, timeout_note, tool_timeout
from control_center import media

HELPER = Path(__file__).resolve().with_name("_transcribe_run.py")
PYTHON = Path("~/venvs/qwen-asr/bin/python").expanduser()
VENV = PYTHON.parents[1]                                 # 整个 venv 只读挂进容器（python 软链到 /usr/bin/python3，镜像里有同版本的）
TIMEOUT_S = 30 * 60
MIN_AVAIL_GB = 10                    # 开跑前宿主可用内存下限（两个模型 bf16 约 5 GB + 推理）
# 转写容器的环境：只挂 HF 缓存的 hub/（只读），写只能写 /tmp（内存盘）与 media；
# 模型离线加载、缓存与编译产物放 /tmp、令牌文件指到不存在的路径（hub/ 外的 token 没挂进去）；
# TORCH_DISABLE_NATIVE_JIT：torch 2.14 把部分算子（如 rope 里的 bmm）路由到 Triton 现编的核，Triton 要现场调 C 编译器，
# stb-docker-base 里没有编译器（/tmp 也不许执行），关掉后走 torch 自带的实现
HF_HOME = Path("~/.cache/huggingface").expanduser()
HF_HUB = HF_HOME / "hub"
HF_ENV = {"HF_HOME": str(HF_HOME), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
          "HF_HUB_DISABLE_TELEMETRY": "1", "HF_TOKEN_PATH": "/tmp/stb-no-hf-token", "XDG_CACHE_HOME": "/tmp/stb-transcribe-cache",
          "TRITON_CACHE_DIR": "/tmp/stb-transcribe-cache/triton", "TORCHINDUCTOR_CACHE_DIR": "/tmp/stb-transcribe-cache/inductor",
          "TORCH_DISABLE_NATIVE_JIT": "1"}
MEDIA_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
GAP_S, SEG_MAX_S = 0.8, 12.0         # 分段：停顿超过 GAP_S 或一段超过 SEG_MAX_S 就另起一段
SENT_END, CLAUSE_END, CLAUSE_MIN_S = tuple("。！？.!?…"), tuple("，、；：,;:"), 5.0   # 句末标点处断段；逗号处在这一段已满 CLAUSE_MIN_S 秒时断
MAX_RETURN_CHARS = 20000             # 返回给主模型的分段文字上限；超了只返回前面的，全文看 transcript_file

server = MCPServer("transcribe", instructions="Qwen3-ASR 语音转文字，带逐词时间戳")


def _avail_gb() -> float:
    return media.avail_gb()


def _join(a: str, b: str) -> str:
    """中文逐字直接连；两边都是拉丁字母 / 数字的词之间补空格。"""
    return a + (" " if a and a[-1].isascii() and not a[-1].isspace() and b[:1].isascii() and b[:1].isalnum() else "") + b


def segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in words:
        prev = out[-1] if out else None
        cut = prev is None or prev["text"].endswith(SENT_END) or (prev["text"].endswith(CLAUSE_END) and prev["end"] - prev["start"] >= CLAUSE_MIN_S)
        if not cut and w["start"] - prev["end"] <= GAP_S and w["end"] - prev["start"] <= SEG_MAX_S:
            out[-1]["text"] = _join(out[-1]["text"], w["text"])
            out[-1]["end"] = w["end"]
        else:
            out.append({"start": w["start"], "end": w["end"], "text": w["text"]})
    return out


def _srt(segs: list[dict[str, Any]]) -> str:
    def t(s: float) -> str:
        ms = round(s * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "".join(f"{i}\n{t(s['start'])} --> {t(s['end'])}\n{s['text']}\n\n" for i, s in enumerate(segs, 1))


@server.tool(name="transcribe_run", description=(
    "语音转文字：音频或视频里的说话 / 唱歌转成带时间的分段文字、逐词时间戳和 SRT 字幕，支持中英文等 30 种语言"))
def transcribe_run(file: str, language: str = "", timeout: Annotated[int | str, Field(description=TIMEOUT_DOC.format(default="30 分钟"))] = 0, disk: Annotated[str, Field(description=DISK_DOC)] = "", reason: Annotated[str, Field(description=REASON_DOC)] = "") -> str:
    limit = call_timeout(timeout, TIMEOUT_S)
    host = media.host_path(file)
    if host.suffix.lower() not in MEDIA_EXT:
        raise media.MediaError(f"只收音频 / 视频文件（{' / '.join(e[1:] for e in MEDIA_EXT)}）：{file}")
    if not host.is_file():
        raise media.MediaError(f"文件不存在：{file}")
    if language and not language.replace(" ", "").isalpha():
        raise media.MediaError(f"language 写语种英文名，如 Chinese、English（你给的是 {language!r}），或不写让它自动识别")
    if not PYTHON.is_file():
        raise platform_fault(f"语音识别环境没装：{PYTHON}（要 Joy 装好；这次先用别的办法，或用问题卡告诉 Joy）")
    avail = _avail_gb()
    if avail < MIN_AVAIL_GB:
        raise platform_fault(media.low_memory("转写没开跑", avail, MIN_AVAIL_GB))
    d = media.media_dir()
    name = media.next_name("transcript")
    out, srt = d / f"{name}.json", d / f"{name}.srt"
    t0 = time.monotonic()
    try:
        r = media.run_gpu("transcribe", [str(PYTHON), str(HELPER), str(host), str(out), language or "auto"],
                           read=[media.work_root(), VENV, HF_HUB, HELPER], write=[d], timeout=limit, need_gb=MIN_AVAIL_GB, env=HF_ENV,
                           disk=disk, reason=reason)
    except subprocess.TimeoutExpired:
        raise tool_timeout(timeout_note("转写", time.monotonic() - t0, limit, bool(timeout))) from None
    except OSError as e:
        raise platform_fault(f"转写容器没起来（stbcc-docker：{e}）") from None
    if r.returncode != 0 or not out.is_file():
        err = (r.stderr.strip().splitlines() or ["（没有 stderr）"])[-1][:300]
        if "没有音轨" in r.stderr or "不到 0.5 秒" in r.stderr or "InvalidDataError" in r.stderr:
            raise media.MediaError(f"读不了这个文件的声音：{err}")
        raise platform_fault(f"转写进程没有正常结束（退出码 {r.returncode}）：{err}")
    res = json.loads(out.read_text())
    segs = segments(res["words"])
    srt.write_text(_srt(segs), encoding="utf-8")
    shown, n = [], 0
    for s in segs:
        n += len(s["text"]) + 20
        if n > MAX_RETURN_CHARS:
            break
        shown.append(s)
    info = {"file": media.sandbox_path(host), "language": res["language"], "duration_s": res["duration_s"], "segments": shown,
            "transcript_file": media.sandbox_path(out), "srt_file": media.sandbox_path(srt), "seconds": round(time.monotonic() - t0, 1)}
    if len(shown) < len(segs):
        info["note"] = f"共 {len(segs)} 段，只返回了前 {len(shown)} 段；后面的用 workspace 读 transcript_file"
    return json.dumps(info, ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")
