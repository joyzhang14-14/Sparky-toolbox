"""speak 的生成脚本：用 Breeze TTS 2 环境的 python 跑（~/apps/breeze-tts/.venv，torch 2.11 cu130），由 speak 的 server.py 经 media.run_gpu 在一次性 stb-docker-base 容器里调用，跑完就退出、释放显存。
  _speak_gen.py <job.json>
job：{"ckpt": 权重目录, "repo": Breeze 推理代码目录, "voice_dir": 声音配置目录, "items": [{"out": 输出 wav, "text": 一块文字, "instruct": 语气指令或 null}]}
没有语气指令：长参考录音 + 它的文字稿纯克隆；有语气指令：短参考录音 + 文字稿 + 指令。引导强度读 voice.json。走官方 infer.py 同一条路（eager，长度上限用官方的）。
stdout 最后一行打 JSON 摘要：load_s、sr、每块的用时和时长。"""
import json
import sys
import time
from pathlib import Path

import torch

torch.zeros(1, device="cuda")  # 先初始化 CUDA 再建模型：主模型在线时先在 CPU 建模型再碰 GPU 会 OOM（10-19 VoxCPM2 排查）

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

job = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, job["repo"])
from breeze_infer.runtime import load_runtime, set_all_seeds, update_generation_config_for_breeze  # noqa: E402
from breeze_infer.templates import get_template, prepare_inputs, select_template_name  # noqa: E402
from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig  # noqa: E402

V = Path(job["voice_dir"])
cfg = json.loads((V / "voice.json").read_text())
REF = {"short": (str(V / cfg["reference"]), cfg["reference_text"]),
       "long": (str(V / cfg["long_text"]["reference"]), (V / Path(cfg["long_text"]["reference"]).with_suffix(".txt")).read_text().strip())}
P = cfg["params"]
t0 = time.monotonic()
tokenizer, model, codec = load_runtime(Path(job["ckpt"]), device="cuda:0", attn_implementation="eager")
update_generation_config_for_breeze(model)
runtime = FastBreezeStreamingRuntime(model, codec, FastStreamingConfig(max_new_tokens=1500, max_seq_len=2048, fast_all=False,
                                                                       repetition_penalty=1.1), tokenizer=tokenizer)
sr = runtime.sample_rate
res = {"load_s": round(time.monotonic() - t0, 1), "sr": sr, "items": []}
for n, it in enumerate(job["items"]):
    t = time.monotonic()
    ref, ref_text = REF["short" if it.get("instruct") else "long"]
    req = {"id": f"c{n}", "text": it["text"], "speaker": "S0", "ref_audio_path": ref, "ref_text": ref_text}
    if it.get("instruct"):
        req["instruction"] = it["instruct"]
    seed = int(time.time_ns() % 2**31)  # 重念要换一次抽样
    set_all_seeds(seed)
    inputs = prepare_inputs(tokenizer, codec, model, [req], get_template(select_template_name(req)),
                            guidance_scale=P["mood_cfg" if it.get("instruct") else "clone_cfg"], guidance_scale_ref=None, guidance_scale_ins=None)
    wav = np.concatenate([c.audio for c in runtime.iter_audio_chunks(inputs, request_id=req["id"], seed=seed)]).reshape(-1)
    wav = (wav / 32768.0 if wav.dtype == np.int16 else wav).astype(np.float32)
    sf.write(it["out"], wav, sr)
    res["items"].append({"out": it["out"], "seconds": round(time.monotonic() - t, 1), "audio_s": round(len(wav) / sr, 2)})
print(json.dumps(res, ensure_ascii=False))
