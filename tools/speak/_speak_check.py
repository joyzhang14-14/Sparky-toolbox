"""speak 的停顿检查与拼接：用 ~/venvs/qwen-asr 的 python 跑（torch + qwen-asr + soundfile + librosa），由 speak 的 server.py 经 media.run_gpu 在一次性 stb-docker-base 容器里调用。
  _speak_check.py <job.json> <out.json>
job：{"chunks": [{"text": 这一块的原文, "takes": [wav…]}], "checked": {wav: 已查过的结果}, "gap_s": 块间静音秒数, "final_wav": 成品 wav, "final_ogg": 成品 ogg}
每块的每条录音（没查过的）和原文对齐（Qwen3-ForcedAligner-0.6B，中文逐字时间），记两种问题：原文没标点却停了 ≥ WRONG_S 秒（停错），句末标点后停顿不到 END_S 秒（句末没停）。
含英文字母、数字或不到 MIN_CHARS 个字的块不查（对齐在这些地方会错位、误报）。每块挑问题最少的一条（一样多取靠前的），按顺序拼起来，块间留 gap_s 秒静音；
写成品 wav 和 ogg（Opus，Discord 能直接播），out.json 写每条录音查出的问题、每块挑了哪条、成品时长。"""
import json
import re
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForcedAligner

WRONG_S, END_S, MIN_CHARS = 0.25, 0.15, 6
END, MID = set("。！？!?…"), set("，、；：,;:")
PUNCT = END | MID | set("“”‘’「」『』《》〈〉\"' 　\n—～~·")
OPUS_SR = (8000, 12000, 16000, 24000, 48000)

job = json.loads(Path(sys.argv[1]).read_text())
checked = dict(job.get("checked") or {})


def checkable(text: str) -> bool:
    return not re.search(r"[A-Za-z0-9０-９]", text) and len([c for c in text if c not in PUNCT]) >= MIN_CHARS


def after_marks(text: str) -> list:
    """原文去掉标点后每个字后面跟的标点种类：end / mid / None。"""
    after = []
    for ch in text:
        if ch in PUNCT:
            if after:
                after[-1] = "end" if ch in END else ("mid" if ch in MID and after[-1] != "end" else after[-1])
        else:
            after.append(None)
    return after


aligner = None
for c in job["chunks"]:
    for w in c["takes"]:
        if w in checked:
            continue
        if not checkable(c["text"]):
            checked[w] = {"checked": False, "issues": []}
            continue
        if aligner is None:
            aligner = Qwen3ForcedAligner.from_pretrained("Qwen/Qwen3-ForcedAligner-0.6B", dtype=torch.bfloat16, device_map="cuda:0")
        y, sr = sf.read(w, dtype="float32")
        r = aligner.align(audio=[(librosa.resample(y, orig_sr=sr, target_sr=16000), 16000)], text=[c["text"]], language=["Chinese"])[0]
        items = [(it.text, float(it.start_time), float(it.end_time)) for it in r]
        after, k, issues = after_marks(c["text"]), 0, []
        for j in range(len(items) - 1):
            k += len(items[j][0])
            kind = after[k - 1] if 0 < k <= len(after) else None
            gap = round(items[j + 1][1] - items[j][2], 2)
            at = "".join(x[0] for x in items[max(0, j - 3):j + 1]) + " | " + "".join(x[0] for x in items[j + 1:j + 4])
            if kind is None and gap >= WRONG_S:
                issues.append({"kind": "停错", "at": at, "gap_s": gap})
            elif kind == "end" and gap < END_S:
                issues.append({"kind": "句末没停", "at": at, "gap_s": gap})
        checked[w] = {"checked": True, "issues": issues}

chosen, audio, sr = [], [], None
for c in job["chunks"]:
    best = min(c["takes"], key=lambda w: len(checked[w]["issues"]))
    chosen.append(best)
    y, s = sf.read(best, dtype="float32")
    if sr is None:
        sr = s
    audio += [y, np.zeros(int(job["gap_s"] * sr), np.float32)]
y = np.concatenate(audio[:-1])
sf.write(job["final_wav"], y, sr)
o_sr = sr if sr in OPUS_SR else 48000
sf.write(job["final_ogg"], y if o_sr == sr else librosa.resample(y, orig_sr=sr, target_sr=o_sr), o_sr, format="OGG", subtype="OPUS")
out = {"checked": checked, "chosen": chosen, "duration_s": round(len(y) / sr, 2)}
Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False))
print(json.dumps({"duration_s": out["duration_s"], "chunks": len(chosen)}))
