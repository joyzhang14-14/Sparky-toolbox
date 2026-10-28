#!/bin/bash
# 启动 SearXNG 常驻容器：建 owa-net、检查 18080 占用、生成 secret、compose up。
set -euo pipefail
cd "$(dirname "$0")"
docker network inspect owa-net >/dev/null 2>&1 || docker network create owa-net
if [ ! -f secret.env ]; then
  echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > secret.env
  chmod 600 secret.env
fi
if ! docker ps --format '{{.Names}}' | grep -qx owa-searxng; then
  if ss -ltn | awk '{print $4}' | grep -qE '(^|:)18080$'; then
    echo "端口 18080 已被占用，不启动" >&2; exit 1
  fi
fi
docker compose up -d
echo "等待 SearXNG 就绪…"
for i in $(seq 1 30); do
  if curl -fs -m 3 'http://127.0.0.1:18080/healthz' >/dev/null 2>&1; then echo "SearXNG 就绪: http://127.0.0.1:18080"; exit 0; fi
  sleep 1
done
echo "SearXNG 30 秒内未就绪" >&2; docker logs --tail 30 owa-searxng >&2; exit 1
