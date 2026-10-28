# photocraft 用法

分工（Joy 2026-10-20 定）：**photocraft 管调色 / 滤镜 / 裁剪缩放 / 合成 / 图层 / 文字 / 形状 / PSD·PCRAFT；
精细抠图（头发、胡须）仍用 cutout；生成新内容仍用 image 子代理**。三者都保留、不替换——别拿 photocraft 的图层蒙版去做精细抠图，也别在它里面"画"出新内容。

- 引擎是 photocraft（纯 Rust 的 Photoshop 重实现）官方 aarch64 包的 headless MCP（`photocraft-cli mcp`，装在 /home/joyzhang14/apps/photocraft/photocraft-current）：不联网、不要 GPU、不起容器。
  子进程第一次调用才起，**同一次运行里跨工具调用文档保持打开、改动保留**（不确定就 photocraft_session_list 看一眼）；运行结束随工具进程退，跨运行不保留文档。
  子进程崩溃或被杀后下一次调用会自动重建会话，返回里带 session_rebuilt 说明——这时文档是空的，先重新 doc_open / doc_new，别当文档还在。
- 路径（引擎起子进程时就定死，调用前先校验，报错会写清）：**读**只认本次运行工作目录下的绝对路径；**写**（doc_save、预览落盘）只认本次运行的 media 目录（它在工作目录下，所以存出去的成品也能再 doc_open 打开）。
- 参数与返回都是 photocraft 自己的 JSON，原样进原样出；引擎报错原文交回。

## 流程

1. photocraft_doc_open（工作目录里的图）或 photocraft_doc_new 新建；多文件合成用 photocraft_doc_place（Smart Object 图层）。
2. **改图前先 photocraft_command_list(filter=关键词) 查准确命令 id 和参数文档**（注册表实测 918 条，id 和参数都别凭记忆猜）。
   常用的有 image.autoTone / image.autoContrast、image.adjustments.brightnessContrast / levels / curves、layer.newAdjustmentLayer.*、layer.new.layer、layer.flattenImage、text.*、edit.crop、image.imageSize 等。
   注意：引擎自动化模式下 file.*（file.open 等）是禁用的，开文件一律走 doc_open / doc_place / doc_new。
3. photocraft_command_run(id, params) 或 photocraft_command_batch([[id,params],…]) 连续跑；photocraft_doc_inspect 看图层树 / history / 选区。
   command_batch 返回按步的 {completed, failed, results:[{ok,result|error}]}——某步失败先看那步的 error，别整批重跑。
4. photocraft_render_preview 出扁平化 PNG 预览（落 media/、附图给你看）——每改几步看一眼，别闷头连改十几步。
5. photocraft_doc_save 保存：**.pcraft 是无损原生格式**（还要继续改就存这个）；png / psd / jpg / tif / webp 等按扩展名导出。成品路径都写 media/。

## 参数坑（实测）

- **等比缩放**：image.imageSize 同时给 width 和 height 会拉成那个尺寸（1280x1014 给 400x400 就变 400x400 的扁图）。
  等比缩到宽 400 的正确写法：`photocraft_command_run("image.imageSize", {"width": 400, "scaleWidth": true, "constraintProportional": true, "constraintRatio": false})`（1280x1014 → 400x317）。
- **Smart Object 图层不能直接调色**：doc_place 进来的层是 Smart Object，对它跑 image.autoTone 会报 "the smart object is not directly editable"；要先 layer.flattenImage（或 layer.layerStyle 类命令）再调。
- 一切命令的参数以 photocraft_command_list 返回的 params 文档为准（它写明取值范围与默认值）；查不到再试相近关键词。

## 资源（2026-10-20 GB10 实测）

- 引擎子进程有 **8 GB 内存上限**（RLIMIT_AS）：大文档 / 大图超出会本次调用失败并交回原因（报错带 "memory allocation … failed"），工具与宿主没事——遇到就换小图、减步骤或改走别的工具。
- 实测内存：空载（起了引擎没开文档）RSS ≈ 11 MB；打开 1280x1014 JPG ≈ 23 MB；巨图（60000x60000 跑 autoTone）撞上限被杀前 RSS ≈ 6.2 GB。日常修图在几十 MB 量级，放心用。
- 快：开图 + 三条命令 + 存盘 全链路秒级以内（1304x602 JPG 三条命令流水线 0.15s 量级）。
