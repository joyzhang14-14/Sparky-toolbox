# open-web-access 一次性研究容器镜像（规格 3、9、11）。
# 基础镜像自带 Node + Playwright 1.63 Chromium（Linux ARM64），与宿主 1.63 对齐；Python 3.12 来自 Ubuntu noble。
# 构建：services/owa-research/build.sh（上下文是仓库根：docker build -f services/owa-research/Dockerfile -t owa-research:latest .）
FROM mcr.microsoft.com/playwright:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# LibreOffice 只装在研究容器（旧 DOC/XLS/PPT 转换），宿主不装；fonts-dejavu 保证转换/渲染有字体
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates \
      libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /usr/local/bin/

# 非 root；与宿主 sparky（uid 1001）一致（基础镜像自带的 ubuntu 1000、pwuser 1001 先删掉），bind mount 的证据目录属主一致（owa-launcher 另给 --user 与 sparkle 组）
RUN (userdel -r ubuntu 2>/dev/null || true) && (userdel -r pwuser 2>/dev/null || true) && useradd -m -u 1001 -s /bin/bash owa && mkdir -p /opt/owa /artifacts && chown -R owa:owa /opt/owa /artifacts
USER owa
WORKDIR /opt/owa
ENV UV_PROJECT_ENVIRONMENT=/opt/owa/.venv UV_LINK_MODE=copy UV_PYTHON=/usr/bin/python3 HOME=/home/owa

# Node 浏览器子服务依赖（patchright 1.63，复用镜像内 Chromium）
COPY --chown=owa:owa services/owa-research/browser/package.json services/owa-research/browser/package-lock.json ./services/owa-research/browser/
RUN cd services/owa-research/browser && npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# Python 依赖（锁文件，不装 dev）
COPY --chown=owa:owa pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 重排/embedding 小模型（候选对比后选定，见 docs/candidates.md）预置进镜像，运行时不下载
ARG OWA_RERANK_MODEL=BAAI/bge-small-en-v1.5
ENV FASTEMBED_CACHE_PATH=/opt/owa/models OWA_RERANK_MODEL=${OWA_RERANK_MODEL}
RUN mkdir -p /opt/owa/models && /opt/owa/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('${OWA_RERANK_MODEL}', threads=2)"

COPY --chown=owa:owa services/owa-research/owa_research ./services/owa-research/owa_research
COPY --chown=owa:owa services/owa-research/browser/server.mjs ./services/owa-research/browser/server.mjs
COPY --chown=owa:owa services/owa-research/browser/agent.mjs ./services/owa-research/browser/agent.mjs
# 以图搜图服务插件（一个服务一个文件，reverse_image_search 运行时扫描；见 owa_research/core/image_engines.py）
COPY --chown=owa:owa subagents/isolated-web-search/image_engines ./subagents/isolated-web-search/image_engines
RUN uv sync --frozen --no-dev

ENV PATH=/opt/owa/.venv/bin:$PATH OWA_ARTIFACT_DIR=/artifacts OWA_BROWSER_SERVER=/opt/owa/services/owa-research/browser/server.mjs
ENTRYPOINT ["python", "-m", "owa_research.worker"]
CMD ["research"]
