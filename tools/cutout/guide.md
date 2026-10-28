# cutout 用法

- image：工作目录下文件的绝对路径（私信发来的图在 in/，生成的在 media/），png / jpg / webp / gif，≤ 20 MB。
- trim=true（默认）裁到主体范围（四周留 2% 边）；要和原图逐像素对齐（同尺寸叠回原图）写 trim=false。
- 返回 JSON（file 透明底 PNG、mask 蒙版（白 = 保留、黑 = 去掉）、width、height、source_size 原图尺寸、box 主体在原图里的 [左, 上, 右, 下]、foreground 前景占比）和预览图（透明处显示为灰色）。
- 换背景、做海报、做贴纸：用 render_png 写 HTML，把 file 当 <img> 叠在新背景上（纯色、渐变或子代理 image 生成的背景图）。
- 主体缺一块或背景没去干净就告诉 Joy，不要重试（同一张图结果一样）；彩色强光背景（霓虹等）下毛发边缘带背景色属正常。
