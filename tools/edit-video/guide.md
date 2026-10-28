# edit-video 用法

- 素材写工作目录下文件的绝对路径：私信发来的在 in/，生成的在 media/。
- 先用 edit-video_info 看素材：在 start–end 之间均匀抽 count 帧拼图（默认整段 4 帧，最多 9 帧；frame_times 是每格的秒数），定剪辑点时缩小 start / end 细看。要按台词剪，先 transcribe 拿每句的时间。
- edit-video_run 返回 JSON（file 成品、duration_s、width、height、bytes；超过 10 MiB 另有 discord_file 压缩版）和成片的 4 帧拼图。看拼图检查画面、字幕位置，有问题改剪辑单重剪。发给 Joy：reply(files=[discord_file 或 file])。
