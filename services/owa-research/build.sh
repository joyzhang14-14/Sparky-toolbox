#!/bin/bash
# 重建研究镜像，但先确认：没有研究容器在跑、宿主可用内存足够。
# 2026-09-19 教训：vLLM 常驻后机器只剩几 GB 余量，构建/测试叠加会把统一内存挤爆。
set -euo pipefail
cd "$(dirname "$0")/../.."   # 构建上下文是仓库根
MIN_GB=${OWA_HOST_MEM_AVAIL_MIN_GB:-6}  # 与 launcher 守卫同值（config/stbcc.yaml 的 launcher.host_mem_avail_min_gb）
if docker ps --format '{{.Names}}' | grep -q '^owa-research-'; then
  echo "有研究容器在跑，先等它结束再构建" >&2; exit 1
fi
avail=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [ "$avail" -lt "$MIN_GB" ]; then
  echo "宿主可用内存 ${avail} GB < ${MIN_GB} GB，不构建" >&2; exit 1
fi
exec docker build -f services/owa-research/Dockerfile -t "${OWA_IMAGE_TAG:-owa-research:latest}" .
