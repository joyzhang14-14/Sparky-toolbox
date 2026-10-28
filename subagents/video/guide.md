# video 用法

- 图写工作目录下的绝对路径：Joy 私信发来的在 in/（消息里有路径），之前生成的在 media/。
- 图生视频：prompt 里可用 <Picture 1> 指首帧、<Picture 2> 指尾帧。
- 参考图生视频：镜头和动作自由安排（如「<Picture 1> 里的女孩在 <Picture 2> 的街道上奔跑」）；做多段长视频时每段给同一组参考图，人物和场景就能一致。
- prompt 用一段话写清画面、动作、镜头运动和声音（对白、音效、音乐），可分镜头写（SHOT 1: … SHOT 2: …）。
- Joy 没要求高清长片就用默认参数；duration_s、megapixels 越大越慢。
- 要多久：卸下主模型、加载生成模型、再装回主模型来回约 5 分钟另算；默认档 480p 5 秒约 4.5 分钟（加速档 2 分钟）；768p 5 秒 7 / 17 分钟，768p 15 秒 37 / 90 分钟（加速 / 标准）。

结果：JSON（file 是原片；discord_file 是能发 Discord 的版本，原片超过 10 MiB 时是压缩版；另有 duration_s、width、height、fps、audio、seed、steps、seconds）和一张 4 帧拼图。
先看拼图，符合要求再 reply(text=..., files=[discord_file]) 发给 Joy；明显不符才重试（改写 prompt 或换 seed）。
