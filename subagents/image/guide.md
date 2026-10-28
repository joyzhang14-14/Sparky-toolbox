# image 用法

- 图写工作目录下的绝对路径：Joy 私信发来的在 in/（消息里有路径），之前生成的在 media/。
- 改图例：把 <image2> 里的外套穿到 <image1> 的人身上，人物、姿势和背景不变。
- prompt 写具体画面：主体、动作、构图、风格、光线、镜头，中英文都行。
- 要多久：调用前后要卸下主模型、加载生成模型、再装回主模型，来回约 5 分钟另算；出一张图约 1 分钟。

结果：JSON（file 是成品路径，另有 width、height、seed、steps、seconds）和一张预览图。
先看预览，符合要求再 reply(text=..., files=[file]) 发给 Joy；明显不符才重试（改写 prompt 或换 seed）。
