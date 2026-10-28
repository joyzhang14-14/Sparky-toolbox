# pptx-deck 用法

坐标用虚拟画布像素（canvas 默认 1920x1080，16:9），照抄给 render_png 写 HTML 时的绝对定位数值；字号写 px，自动换成 pt（1920 画布：96 px = 48 pt）。

spec：canvas、slide_size（16:9 / 4:3）、title、slides；每页 background_color、background_image、elements。

elements 按顺序叠放，后面的压在前面上，三种：
- shape：kind 可选 rect、roundRect、parallelogram、ellipse、triangle、diamond、chevron、hexagon、pentagon、rightArrow、line（line 的 x,y 是起点，w,h 是终点增量）；fill 写颜色、"none" 或渐变；line 写 "none" 或描边；可带 rotation、radius、text。
- image：src 是工作目录下图片的绝对路径；fit 可选 stretch、contain、cover；crop 是 [左, 上, 右, 下]。
- text：字号 font_size_px；align 可选 left、center、right、justify；anchor 可选 top、middle、bottom；分段、分色用 paragraphs 和 runs 代替 text。

做法：先用 render_png 出一张不含文字的装饰背景图（警示条、网点、人物立绘都画在图里），再按同一套坐标叠文字。
字体只用：Arial Black（大标题）、Arial、Georgia、Impact、Courier New、Trebuchet MS，中文 "PingFang SC"。
生成后用 render_office(file) 看每页预览：文字换行压到别的元素、被图挡住、溢出文本框，warnings 看不出来（只按字数粗估）。有问题改 spec 再生成。
