# transcribe 用法

- file：工作目录下文件的绝对路径（私信发来的在 in/），音频 mp3 / wav / m4a… 或视频 mp4 / mov…。
- language 可选（Chinese、English、Japanese…），不写自动识别；中文含粤语和各地方言。
- 返回 JSON：language、duration_s、segments（按停顿分的段 [{start, end, text}]，秒）、transcript_file（含逐词时间戳，要更细的剪辑点就读它）、srt_file（字幕文件）。
- 剪视频按 segments 的时间定 edit-video_run 的 start / end；做字幕把 segments 填进 subtitles。
