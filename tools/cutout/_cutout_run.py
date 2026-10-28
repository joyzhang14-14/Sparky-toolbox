"""cutout 的抠图脚本：用 ComfyUI venv 的 python 跑（torch + ComfyUI 自带的 BiRefNet 实现），由 cutout 的 server.py 经 media.run_gpu 在一次性 stb-docker-base 容器里调用，跑完就退出、释放显存。
不起 ComfyUI 服务（切模型系统靠 ComfyUI 端口判断档位，不能碰）。结果 JSON 打在 stdout 最后一行。
  _cutout_run.py <ComfyUI 目录> <model> <in> <out_rgba.png> <out_mask.png> <trim 0|1>
  trim=1：两张图都裁到主体范围（alpha > 8 的外接框，四周留 2% 边）
"""
import json
import sys
import time

comfy_home, model, src, out_rgba, out_mask, trim = sys.argv[1:7]
sys.path.insert(0, comfy_home)
t0 = time.monotonic()

import numpy as np
import torch
from PIL import Image, ImageOps

import comfy.bg_removal_model

with Image.open(src) as im:
    im = ImageOps.exif_transpose(im).convert("RGB")
bg = comfy.bg_removal_model.load(model)
if bg is None:
    raise SystemExit("模型文件不是 BiRefNet")
t1 = time.monotonic()
x = torch.from_numpy(np.asarray(im, dtype=np.float32) / 255.0)[None]      # ComfyUI 的 IMAGE：(B, H, W, C)，0–1
with torch.inference_mode():
    mask = bg.encode_image(x)[0].float().clamp(0, 1).cpu().numpy()
alpha = Image.fromarray((mask * 255).round().astype(np.uint8), "L")
rgba = im.copy()
rgba.putalpha(alpha)
box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
if trim == "1" and box:
    pad = round(0.02 * max(im.size))
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
    rgba, alpha = rgba.crop(box), alpha.crop(box)
alpha.save(out_mask)
rgba.save(out_rgba)
print(json.dumps({"width": rgba.width, "height": rgba.height, "source_size": [im.width, im.height], "box": list(box) if box else None,
                  "foreground": round(float(mask.mean()), 4),
                  "load_s": round(t1 - t0, 1), "infer_s": round(time.monotonic() - t1, 1)}))
