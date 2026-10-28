# stb-dev：builder 开发区与部署测试用的镜像（docs/builder-spec.md）：Ubuntu 24.04 的 python3.12 与宿主一致，
# 挂进来的仓库 venv（.venv/bin/python → /usr/bin/python3）和 sparky 的工具 venv 在容器里、宿主上都能直接用。
# 运行时 --network none、uid 1001（宿主 sparky），不烧任何凭证。构建：docker build -t stb-dev:latest services/stb-dev
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git ca-certificates ripgrep tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /
