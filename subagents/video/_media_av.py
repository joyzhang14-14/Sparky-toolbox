"""video 子代理的视频辅助脚本：用 ComfyUI venv 的 python 跑（有 PyAV；stb 的 venv 没有 av），由同文件夹的 server.py 以子进程调用。
结果 JSON 打在 stdout 最后一行。
  sheet <video> <out.jpg>               取首、1/3、2/3、尾 4 帧拼成 2x2 预览图（长边 ≤ 1024），输出时长 / 尺寸 / fps / 帧数 / 有无音轨
  shrink <in.mp4> <out.mp4> <max_bytes>  按码率重编码成 H.264 + AAC（尺寸、帧率、声道不变），超了就降码率再来，最多 3 次
"""
import json
import os
import sys

import av
from PIL import Image

SHEET_EDGE = 1024
GAP = 4
AUDIO_BPS = 128_000


def _count(src: str) -> int:
    with av.open(src) as c:
        return sum(1 for _ in c.decode(video=0))


def sheet(src: str, dst: str) -> dict:
    with av.open(src) as c:
        vs = c.streams.video[0]
        n = vs.frames or _count(src)
        want = sorted({round(i * (n - 1) / 3) for i in range(4)})
        frames = []
        for i, f in enumerate(c.decode(vs)):
            if i in want:
                frames.append(f.to_image())
                if len(frames) == len(want):
                    break
        fps = float(vs.average_rate or 24)
        info = {"width": vs.codec_context.width, "height": vs.codec_context.height, "fps": round(fps, 3), "frames": n,
                "duration_s": round(n / fps, 2), "audio": bool(c.streams.audio)}
    w, h = frames[0].size
    s = min((SHEET_EDGE - GAP) / 2 / w, (SHEET_EDGE - GAP) / 2 / h)
    cw, ch = max(1, round(w * s)), max(1, round(h * s))
    out = Image.new("RGB", (cw * 2 + GAP, ch * 2 + GAP), "white")
    for k, im in enumerate(frames):
        out.paste(im.convert("RGB").resize((cw, ch)), ((k % 2) * (cw + GAP), (k // 2) * (ch + GAP)))
    out.save(dst, "JPEG", quality=85)
    return info


def _encode(src: str, dst: str, vbr: int) -> None:
    with av.open(src) as inp, av.open(dst, "w", format="mp4") as out:
        vin = inp.streams.video[0]
        ain = inp.streams.audio[0] if inp.streams.audio else None
        vout = out.add_stream("libx264", rate=vin.average_rate or 24)
        vout.width, vout.height, vout.pix_fmt = vin.codec_context.width, vin.codec_context.height, "yuv420p"
        vout.bit_rate = vbr
        vout.options = {"preset": "medium", "maxrate": str(vbr * 3 // 2), "bufsize": str(vbr * 2)}
        aout = None
        if ain is not None:
            aout = out.add_stream("aac", rate=ain.rate, layout=ain.layout.name)
            aout.bit_rate = AUDIO_BPS
        for pkt in inp.demux(*[s for s in (vin, ain) if s is not None]):
            for frame in pkt.decode():
                o = vout if pkt.stream.type == "video" else aout
                out.mux(o.encode(frame))
        out.mux(vout.encode(None))
        if aout is not None:
            out.mux(aout.encode(None))


def shrink(src: str, dst: str, max_bytes: int) -> dict:
    with av.open(src) as c:
        vs = c.streams.video[0]
        n = vs.frames or _count(src)
        duration = n / float(vs.average_rate or 24)
        audio = bool(c.streams.audio)
    # 按时长分配码率：总量留 8% 给容器开销，扣掉音频
    vbr = max(200_000, int(max_bytes * 8 * 0.92 / duration) - (AUDIO_BPS if audio else 0))
    for _ in range(3):
        _encode(src, dst, vbr)
        if os.path.getsize(dst) <= max_bytes:
            break
        vbr = max(100_000, vbr * 3 // 4)
    return {"bytes": os.path.getsize(dst), "video_bps": vbr}


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "sheet":
        res = sheet(args[0], args[1])
    elif cmd == "shrink":
        res = shrink(args[0], args[1], int(args[2]))
    else:
        raise SystemExit(f"未知子命令：{cmd}")
    print(json.dumps(res))
