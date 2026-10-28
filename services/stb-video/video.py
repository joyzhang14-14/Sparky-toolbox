"""stb-video 容器入口：stdin 一行 JSON 任务，stdout 最后一行 JSON 结果。参数已由宿主上的 edit-video 工具（tools/edit-video/server.py）校验过（路径都在任务工作目录下，容器里外同一个路径）。
  {mode: "info", file, sheet, count, start, end}          探测时长 / 尺寸 / 帧率 / 音轨，在 [start, end] 里均匀取 count 帧拼成预览图
  {mode: "edit", plan, out, sheet, discord_out, max_bytes} 按剪辑单出 mp4（H.264 + AAC），再出 4 帧预览；超过 max_bytes 另出压缩版
命令一律按参数列表调用（不经 shell）；字幕文字写进 /tmp 的 SRT 文件，不拼进滤镜字符串。
"""
import json
import math
import os
import subprocess
import sys

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
SUB_FONT = "Noto Sans CJK SC"


def ff(*args):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"ffmpeg 出错：{r.stderr.strip()[-600:]}")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate",
                        "-of", "json", path], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"读不了这个文件：{r.stderr.strip()[-300:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    num, _, den = (v or {}).get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    return {"duration_s": round(float(d.get("format", {}).get("duration") or 0), 3), "width": (v or {}).get("width"),
            "height": (v or {}).get("height"), "fps": round(fps, 3), "audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def sheet(src, dst, count=4, start=0.0, end=None, dur=None):
    """在 [start, end] 里均匀取 count 帧，缩放后按 2 列（count > 4 时 3 列）拼成一张 JPEG，长边约 1024。"""
    dur = dur if dur is not None else probe(src)["duration_s"]
    end = min(end if end is not None else dur, dur)
    span = max(end - start, 0.04)
    cols = 2 if count <= 4 else 3
    rows = math.ceil(count / cols)
    cell = 1024 // cols // 2 * 2                             # yuv420p 要求宽高是偶数
    ins, lab = [], ""
    for i in range(count):
        t = start + span * (i + 0.5) / count
        ins += ["-ss", f"{t:.3f}", "-i", src]
        lab += f"[{i}:v]scale={cell}:{cell}:force_original_aspect_ratio=decrease,pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2:white,setsar=1[f{i}];"
    for i in range(count, rows * cols):                      # 补白格凑满网格
        lab += f"color=white:s={cell}x{cell}:d=1[f{i}];"
    layout = "|".join(f"{(i % cols) * cell}_{(i // cols) * cell}" for i in range(rows * cols))
    lab += "".join(f"[f{i}]" for i in range(rows * cols)) + f"xstack=inputs={rows * cols}:layout={layout}[out]"
    ff(*ins, "-filter_complex", lab, "-map", "[out]", "-frames:v", "1", "-q:v", "3", dst)
    return [round(start + span * (i + 0.5) / count, 2) for i in range(count)]


def atempo(speed):
    """atempo 每级只能 0.5–2 倍，超出的拆成几级相乘。"""
    parts = []
    while speed > 2:
        parts.append(2.0); speed /= 2
    while speed < 0.5:
        parts.append(0.5); speed /= 0.5
    return ",".join(f"atempo={p:.4f}" for p in parts + [speed])


def srt_time(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def edit(p, out):
    clips = p["clips"]
    infos = [None if c["file"].lower().endswith(IMAGE_EXT) else probe(c["file"]) for c in clips]
    first = next((i for i in infos if i and i["width"]), None)
    W = p.get("width") or (first or {}).get("width") or 1280
    H = p.get("height") or (first or {}).get("height") or 720
    W, H = W // 2 * 2, H // 2 * 2
    fps = p.get("fps") or 30
    fit = "increase" if p.get("fit") == "cover" else "decrease"
    ins, graph, total = [], "", 0.0
    for i, (c, info) in enumerate(zip(clips, infos)):
        speed = float(c.get("speed") or 1)
        if info is None:                                         # 图片：定格 duration 秒
            d = float(c.get("duration") or 3)
            ins += ["-loop", "1", "-t", f"{d:.3f}", "-i", c["file"]]
            vtrim, atrim, length, has_audio = "", "", d, False
        else:
            s = float(c.get("start") or 0)
            e = min(float(c["end"]) if c.get("end") is not None else info["duration_s"], info["duration_s"])
            if e - s < 0.05:
                raise SystemExit(f"第 {i + 1} 段的 start / end 不对：{s}–{e}（素材长 {info['duration_s']} 秒）")
            ins += ["-i", c["file"]]
            vtrim, atrim = f"trim=start={s:.3f}:end={e:.3f},", f"atrim=start={s:.3f}:end={e:.3f},"
            length, has_audio = (e - s) / speed, info["audio"]
        post = (f"crop={W}:{H}" if fit == "increase" else f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
        graph += (f"[{i}:v]{vtrim}setpts=(PTS-STARTPTS)/{speed:.4f},scale={W}:{H}:force_original_aspect_ratio={fit},{post},"
                  f"fps={fps},format=yuv420p,setsar=1[v{i}];")
        vol = float(c.get("volume", 1))
        if has_audio and not p.get("mute"):
            graph += (f"[{i}:a]{atrim}asetpts=PTS-STARTPTS,{atempo(speed)},"
                      f"volume={vol:.3f},aresample=48000,aformat=channel_layouts=stereo[a{i}];")
        else:
            graph += f"anullsrc=r=48000:cl=stereo,atrim=0:{length:.3f}[a{i}];"
        total += length
    n = len(clips)
    graph += "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac];"
    vlast, alast = "[vc]", "[ac]"
    fade = float(p.get("fade") or 0)
    if fade > 0:
        graph += f"{vlast}fade=t=in:d={fade:.3f},fade=t=out:st={max(total - fade, 0):.3f}:d={fade:.3f}[vf];"
        graph += f"{alast}afade=t=in:d={fade:.3f},afade=t=out:st={max(total - fade, 0):.3f}:d={fade:.3f}[af];"
        vlast, alast = "[vf]", "[af]"
    subs = p.get("subtitles") or []
    if subs:
        with open("/tmp/subs.srt", "w", encoding="utf-8") as f:
            for k, s in enumerate(subs, 1):
                f.write(f"{k}\n{srt_time(float(s['start']))} --> {srt_time(float(s['end']))}\n{s['text'].strip()}\n\n")
        size = int(p.get("subtitle_size") or max(min(W, H) // 18, 16))      # 像素
        u = 288 / H                                                           # SRT 按 libass 默认画布高 288 排版：像素换算成它的单位
        style = (f"FontName={SUB_FONT},FontSize={size * u:.1f},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 f"Outline={max(size / 12, 1) * u:.2f},Shadow=0,MarginV={H / 14 * u:.1f},MarginL={W / 20 * u:.1f},MarginR={W / 20 * u:.1f}")
        graph += f"{vlast}subtitles=/tmp/subs.srt:force_style='{style}'[vs];"
        vlast = "[vs]"
    m = p.get("music")
    if m:
        ins += ["-stream_loop", "-1", "-ss", f"{float(m.get('start') or 0):.3f}", "-i", m["file"]]
        graph += (f"[{n}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={float(m.get('volume', 0.5)):.3f},aresample=48000,"
                  f"aformat=channel_layouts=stereo[mu];{alast}[mu]amix=inputs=2:duration=first:normalize=0[am];")
        alast = "[am]"
    ff(*ins, "-filter_complex", graph.rstrip(";"), "-map", vlast, "-map", alast, "-t", f"{total:.3f}",
       "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out)
    return total


def shrink(src, dst, max_bytes, dur):
    """按目标码率重编码成小于 max_bytes 的版本（Discord 附件上限），超了降码率再来，最多 3 次。"""
    target = max_bytes * 0.92
    for _ in range(3):
        vbps = max(int(target * 8 / max(dur, 0.1) - 128_000), 150_000)
        ff("-i", src, "-c:v", "libx264", "-preset", "medium", "-b:v", str(vbps), "-maxrate", str(vbps), "-bufsize", str(vbps * 2),
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst)
        if os.path.getsize(dst) <= max_bytes:
            return True
        target *= 0.8
    return False


def main():
    job = json.loads(sys.stdin.read())
    if job["mode"] == "info":
        info = probe(job["file"])
        if not info["width"]:
            raise SystemExit("这个文件没有画面（只有音频）")
        info["frame_times"] = sheet(job["file"], job["sheet"], job["count"], job.get("start") or 0.0, job.get("end"), info["duration_s"])
        print(json.dumps(info))
        return
    total = edit(job["plan"], job["out"])
    info = probe(job["out"])
    info["frame_times"] = sheet(job["out"], job["sheet"], 4, 0.0, None, info["duration_s"])
    info["bytes"] = os.path.getsize(job["out"])
    if info["bytes"] > job["max_bytes"]:
        info["discord_ok"] = shrink(job["out"], job["discord_out"], job["max_bytes"], total)
    print(json.dumps(info))


if __name__ == "__main__":
    main()
