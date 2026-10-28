# render 用法

HTML（render_png、render_pdf 通用）：

- 先把 HTML 写进工作目录的 out/，html 参数写它的绝对路径；图片也写绝对路径（工作目录 media/ 或 in/ 下的文件）。
- 断网：只能用工作目录里的文件和已装字体，网上的字体、图片、脚本都加载不了。
- 中文字体：'Noto Sans CJK SC'（黑体）、'Noto Serif CJK SC'（宋体）、'LXGW WenKai'（楷体手写）、'Smiley Sans'（得意黑，斜体标题）、
  'ZCOOL KuaiLe'、'ZCOOL QingKe HuangYou'、'ZCOOL XiaoWei'、'Ma Shan Zheng'（毛笔）、'Zhi Mang Xing'（行书）、'Long Cang'、'Liu Jian Mao Cao'；
  英文等有整个 Google Fonts 字体库（Inter、Roboto、Playfair Display、Montserrat…），用 render_fonts 查。

render_png(html, width, height, scale, transparent, full_page)：

- width / height 是画布像素（竖版海报 1080×1350、横幅 1920×1080），body 设成同样大小、margin 0；scale=2 出两倍像素。
- transparent=true 背景透明（html、body 不设背景色），做透明底贴纸；full_page=true 按内容高度截整页。
- 返回 JSON（file、width、height、problems：加载失败的图片 / 字体、内容超出画布等）和预览图。看预览和 problems，有问题改 HTML 再渲染（一般 1–3 轮）。

render_pdf(html, width, height, preview_pages)：

- 幻灯片：每页一个 section，宽高和 @page 都等于 width × height（默认 1280×720），每页后分页，body 无边距。
- 返回 JSON（file、pages 总页数、problems）和 preview_pages 所选页的预览（最多 4 页，默认前 4 页）。逐页看文字有没有溢出、重叠、看不清。

render_office(file, preview_pages)：

- file 是工作目录里的 .pptx / .docx / .xlsx（也收 ppt / doc / xls / odp / odt / ods），转成 PDF；返回 JSON（file 转出的 PDF、pages）和所选页的预览。
- 做完 pptx-deck 之类的文件先用它看：文字溢出、换行压到别的元素、被图挡住、看不清，改了再生成、再看。
- 排版和 PowerPoint / Keynote 略有差别（没装的字体会被替换），重叠、遮挡、溢出都看得出来。

满意了用 reply(files=[file]) 发给 Joy（PDF 也行）。

render_fonts(category, zh, query)：font-family 写返回的 family。category：SANS_SERIF / SERIF / DISPLAY（标题装饰）/ HANDWRITING / MONOSPACE，空 = 全部；
zh=true 只看含中文的；query 按名字包含查。最多返回 200 个。
