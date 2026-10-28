#!/usr/bin/env bash
# 构建 stb-agent:latest（沙箱 agent 镜像）。带内存守卫：可用内存不足时不构建（实测构建约占 0.3 GB，门槛留余量）。
set -euo pipefail
cd "$(dirname "$0")/../.."
MIN_GB=${STB_AGENT_BUILD_MEM_MIN_GB:-2}
AVAIL=$(awk '/MemAvailable/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
if (( $(echo "$AVAIL < $MIN_GB" | bc -l) )); then echo "可用内存 ${AVAIL} GB < ${MIN_GB} GB，不构建（门槛可用 STB_AGENT_BUILD_MEM_MIN_GB 调）"; exit 1; fi
docker build -t stb-agent:latest -f services/stb-agent/Dockerfile services/stb-agent
docker image inspect stb-agent:latest --format 'stb-agent:latest {{.Size}} bytes'
