"""transcribe 的转写脚本：用 ~/venvs/qwen-asr 的 python 跑（torch + qwen-asr + PyAV），由 transcribe 的 server.py 经 media.run_gpu 在一次性 stb-docker-base 容器里调用，跑完就退出、释放显存。
PyAV 把音频 / 视频的音轨解码成 16 kHz 单声道，Qwen3-ASR-1.7B 转写、Qwen3-ForcedAligner-0.6B 给逐词（中文逐字）时间戳；
长音频由 qwen-asr 自己在低音量处切块（带时间戳时每块 ≤ 180 秒）再合并。模型从 HF 缓存离线加载。结果 JSON 写到 <out.json>，stdout 最后一行打摘要。
  _transcribe_run.py <in> <out.json> <language 或 auto>
"""
import json
import sys
import time

src, out, language = sys.argv[1:4]
t0 = time.monotonic()

import av
import numpy as np
import torch
from qwen_asr import Qwen3ASRModel

SR = 16000
with av.open(src) as c:
    if not c.streams.audio:
        raise SystemExit("这个文件没有音轨")
    rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
    parts = [f.to_ndarray().reshape(-1) for fr in c.decode(audio=0) for f in rs.resample(fr)]
    parts += [f.to_ndarray().reshape(-1) for f in rs.resample(None)]
wav = np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
if len(wav) < SR // 2:
    raise SystemExit("音频不到 0.5 秒")
t1 = time.monotonic()
model = Qwen3ASRModel.from_pretrained("Qwen/Qwen3-ASR-1.7B", dtype=torch.bfloat16, device_map="cuda:0", max_inference_batch_size=8,
                                      max_new_tokens=4096, forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B",
                                      forced_aligner_kwargs=dict(dtype=torch.bfloat16, device_map="cuda:0"))
t2 = time.monotonic()
r = model.transcribe(audio=(wav, SR), language=None if language == "auto" else language, return_time_stamps=True)[0]
items = getattr(r.time_stamps, "items", None) or []
words = [{"text": it.text, "start": round(float(it.start_time), 2), "end": round(float(it.end_time), 2)} for it in items]
PUNCT = "，。！？、；：…,.!?;:\"'”’）)》」』"
pos = 0
for w in words:                          # 对齐结果不带标点：按顺序在全文里找到这个词，把紧跟的标点贴回去（分段、字幕要用）
    i = r.text.find(w["text"], pos)
    if i < 0 or i - pos > 20:
        continue
    j = k = i + len(w["text"])
    while k < len(r.text) and r.text[k] in PUNCT:
        k += 1
    w["text"] += r.text[j:k]
    pos = k
res = {"language": r.language, "text": r.text, "duration_s": round(len(wav) / SR, 2), "words": words,
       "decode_s": round(t1 - t0, 1), "load_s": round(t2 - t1, 1), "asr_s": round(time.monotonic() - t2, 1),
       "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)}
with open(out, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False)
print(json.dumps({k: v for k, v in res.items() if k not in ("text", "words")} | {"words": len(words)}))
