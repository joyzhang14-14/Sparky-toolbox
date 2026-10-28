#!/bin/bash
# 给 vLLM 容器的 cgroup 设 memory.high（软上限）：超过后内核主动回收（先文件页，再把冷 anon 换到 swap），不杀进程。
# 现行调用：~/scripts/vllm-eugr.sh、~/scripts/vllm-eugr-abl.sh 末尾后台 `--container vllm_node --wait 80G`（sudoers /etc/sudoers.d/vllm-memory-high 对本脚本免密），
# 日志 logs/vllm-memory-high-eugr.log。eugr 配方的 vllm_node 把 NVMe 上的 PLE 表 mmap 进来，cgroup 记账约 72 GB（file_mapped），
# 上限必须高于它：实测定 80G（≈ 稳态 72.5 + 7 GB 余量）；定得太低内核回收不掉、持续节流。容器每次重启都是新 cgroup，值会丢，所以每次起容器都要设。
#
# 用法（--container 必须写，或用环境变量 VLLM_CONTAINER）：
#   services/vllm/vllm-memory-high.sh --container vllm_node show          只读，不需要 root
#   services/vllm/vllm-memory-high.sh --container vllm_node 80G           立即设置（非 root 会自动 sudo）
#   services/vllm/vllm-memory-high.sh --container vllm_node --wait 80G    等 vLLM 就绪（127.0.0.1:8000/v1/models 返回 200）再设置：加载阶段不能提前限
#   services/vllm/vllm-memory-high.sh --container vllm_node max           撤销上限
set -euo pipefail

WAIT=0; CONTAINER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --wait) WAIT=1; shift ;;
    --container) CONTAINER="$2"; shift 2 ;;
    *) break ;;
  esac
done
ARG="${1:-show}"
NAME="${CONTAINER:-${VLLM_CONTAINER:-}}"
[ -n "$NAME" ] || { echo "要写 --container <容器名>（如 vllm_node）" >&2; exit 2; }

cg_dir() {
  local cid
  cid=$(docker inspect -f '{{.Id}}' "$NAME" 2>/dev/null) || { echo "容器 $NAME 不存在" >&2; exit 1; }
  echo "/sys/fs/cgroup/system.slice/docker-${cid}.scope"
}

gb() { awk -v b="$1" 'BEGIN{ if (b ~ /^[0-9]+$/) printf "%.2f", b/1073741824; else print b }'; }

show() {
  local d; d=$(cg_dir)
  local anon file; read -r anon file < <(awk '$1=="anon"{a=$2} $1=="file"{f=$2} END{print a, f}' "$d/memory.stat")
  local pswpin; pswpin=$(awk '$1=="pswpin"{print $2*4096}' "$d/memory.stat")
  printf "memory.high     = %s\n" "$(gb "$(cat "$d/memory.high")")"
  printf "memory.current  = %s GB  (anon %s, file %s)\n" "$(gb "$(cat "$d/memory.current")")" "$(gb "$anon")" "$(gb "$file")"
  printf "swap.current    = %s GB\n" "$(gb "$(cat "$d/memory.swap.current")")"
  printf "events.high     = %s   (超过上限触发回收的次数)\n" "$(awk '$1=="high"{print $2}' "$d/memory.events")"
  printf "pswpin 累计      = %s GB (换入总量，持续快速增长 = 抖动)\n" "$(gb "$pswpin")"
  printf "host MemAvail   = %s GB\n" "$(awk '/MemAvailable/{printf "%.2f", $2/1048576}' /proc/meminfo)"
}

if [ "$ARG" = "show" ]; then show; exit 0; fi

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -n "$0" --container "$NAME" $([ $WAIT -eq 1 ] && echo --wait) "$ARG"
fi

if [ $WAIT -eq 1 ]; then
  echo "等待 vLLM 就绪（最多 40 分钟）..."
  for _ in $(seq 1 240); do
    curl -sf -o /dev/null "http://127.0.0.1:8000/v1/models" && break
    sleep 10
  done
  curl -sf -o /dev/null "http://127.0.0.1:8000/v1/models" || { echo "vLLM 未就绪，放弃设置" >&2; exit 1; }
fi

D=$(cg_dir)
echo "设置前："; show
echo "$ARG" > "$D/memory.high"
echo; echo "已写入 memory.high = $ARG"; echo "设置后："; show
