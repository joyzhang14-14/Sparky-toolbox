# stb-agent：agent 任务的一次性沙箱镜像（规格第 7 节）：pi + uv + git，无浏览器，以 sparky 跑（uid 1001 与宿主 sparky 一致；平台起容器时另给 --user 与 sparkle 组）。
# 扩展（toollog.ts、stb-gateway.ts）与 models.json 运行时只读挂入，不烧进镜像。构建：services/stb-agent/build.sh
FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-venv curl ca-certificates jq ripgrep procps \
    && rm -rf /var/lib/apt/lists/*
RUN userdel -r node 2>/dev/null; useradd -m -u 1001 -s /bin/bash agent
USER agent
ENV HOME=/home/agent PATH=/home/agent/.local/bin:/home/agent/.npm-global/bin:$PATH
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mkdir -p ~/.npm-global ~/.pi/agent && npm config set prefix ~/.npm-global \
    && npm install -g @mariozechner/pi-coding-agent@0.73.1
WORKDIR /home/agent
# umask 002：写进挂载目录的文件组可写（平台用户同在 sparkle 组，要读、要删）
ENTRYPOINT ["/bin/sh", "-c", "umask 002 && exec pi \"$@\"", "pi"]
