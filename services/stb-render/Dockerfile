# stb-render：render 工具的一次性渲染容器（tools/render/server.py）：无头 Chromium 把任务工作目录里的 HTML 截成 PNG 或导出 PDF。
# 运行时 --network none、只读根、以 sparky 身份跑，按原路径只读挂任务工作目录、只写本次运行的 media 目录；字体全部烧在镜像里（断网加载不了网上字体）。
# 字体：Google Fonts 全库（ofl / apache / ufl）+ Noto CJK（思源黑体 / 宋体）+ Noto Color Emoji + 霞鹜文楷 + 得意黑。
# render_office：LibreOffice（Impress / Writer / Calc）把 pptx / docx / xlsx 转 PDF 看预览；Liberation / Carlito / Caladea 与 Arial / Calibri / Cambria 同字宽，换行位置接近 Office。
# 构建：docker build -t stb-render:latest services/stb-render
FROM mcr.microsoft.com/playwright:v1.63.0-noble

RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji poppler-utils curl unzip ca-certificates \
      libreoffice-impress libreoffice-writer libreoffice-calc fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/render
RUN npm init -y >/dev/null && npm install --omit=dev playwright-core@1.63.0 && npm cache clean --force

# Google Fonts 全库：只留字体文件和 METADATA.pb（分类表从它生成）
RUN mkdir -p /usr/share/fonts/google /opt/render/gf-meta \
    && curl -fsSL https://github.com/google/fonts/archive/refs/heads/main.tar.gz \
       | tar -xz -C /tmp --wildcards 'fonts-main/ofl/*' 'fonts-main/apache/*' 'fonts-main/ufl/*' \
    && for d in /tmp/fonts-main/*/*/; do \
         n=$(basename "$d"); \
         ls "$d"*.[to]tf >/dev/null 2>&1 || continue; \
         mkdir -p "/usr/share/fonts/google/$n"; cp "$d"*.[to]tf "/usr/share/fonts/google/$n/"; \
         if [ -f "$d/METADATA.pb" ]; then cp "$d/METADATA.pb" "/opt/render/gf-meta/$n.pb"; fi; \
       done \
    && ls /usr/share/fonts/google | wc -l \
    && rm -rf /tmp/fonts-main

ARG WENKAI=v1.522
ARG SMILEY=v2.0.1
RUN mkdir -p /usr/share/fonts/extra && cd /usr/share/fonts/extra \
    && for w in Light Regular Medium; do \
         curl -fsSLO "https://github.com/lxgw/LxgwWenKai/releases/download/$WENKAI/LXGWWenKai-$w.ttf"; done \
    && curl -fsSL -o /tmp/smiley.zip "https://github.com/atelier-anchor/smiley-sans/releases/download/$SMILEY/smiley-sans-$SMILEY.zip" \
    && unzip -q /tmp/smiley.zip -d /tmp/smiley \
    && find /tmp/smiley -name '*.ttf' -exec cp {} /usr/share/fonts/extra/ \; \
    && ls /usr/share/fonts/extra/*mileySans* \
    && rm -rf /tmp/smiley.zip /tmp/smiley

COPY --chmod=644 render.mjs catalog.py /opt/render/
COPY --chmod=644 fonts-alias.conf /etc/fonts/conf.d/60-stb-alias.conf
RUN fc-cache -f >/dev/null && python3 /opt/render/catalog.py > /opt/render/catalog.json

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp
ENTRYPOINT ["node", "/opt/render/render.mjs"]
