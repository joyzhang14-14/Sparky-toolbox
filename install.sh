#!/usr/bin/env bash
# install.sh：在一台装好 Docker 的 Linux 上装 sparky-toolbox（stb）。以平台用户（普通用户、在 docker 组）在仓库里跑：
#   ./install.sh [--yes] [--no-cleanup] [--discord] [--docker] [--builder] [--owa] [--comfyui]
# 步骤：1 检查前提 → 2 需要 root 的系统设置（先列出要做的事，确认后用 sudo 执行；--yes 跳过确认）→ 3 uv sync → 4 .env →
#      5 config/models.yaml → 6 建 stb-agent 镜像 → 7 装并启动 stbcc-runner / stbcc-hub / stbcc-web / stbcc-cleanup（及选上的可选组件）→ 8 stb doctor --smoke。
# stbcc-cleanup 每天清一次过期的运行记录、私信附件、审批卡、媒体成品；--no-cleanup 不装它。
# 可选组件（默认不装）：
#   --discord  Discord 渠道 stbcc-discord：私信与审批卡走 Discord（.env 要填 STB_DISCORD_TOKEN、STB_OWNER_DISCORD_ID）
#   --docker   容器启动器 stbcc-docker：tool.yaml 写了 docker 的工具（render、cutout 等）经它起容器
#   --builder  工具构建 stbcc-builder：子代理 builder 开发、试跑、部署新工具（连带装 --docker）
#   --owa      联网研究：研究镜像 owa-research、搜索引擎 SearXNG、owa-launcher（要 /usr/bin/node；.env 要填 OWA_LAUNCHER_TOKEN、OWA_USER_AGENT）
#   --comfyui  建 stb-docker-base 镜像（cutout、transcribe 在里面跑宿主的 venv）；ComfyUI 本身另装，装好后在 config/models.yaml 加 runtime: comfyui 的档位
# 工具用户与共用组的名字读 config/stbcc.yaml 的 tool_user / shared_group（默认 sparky / sparkle）。可以重复跑：做过的步骤跳过。
set -euo pipefail
cd "$(dirname "$0")"
REPO=$PWD
YES=0; OPT=" "
for a in "$@"; do
  case $a in
    --yes) YES=1 ;;
    --no-cleanup|--discord|--docker|--builder|--owa|--comfyui) OPT+="${a#--} " ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$a（--help 看用法）" >&2; exit 2 ;;
  esac
done
[[ $OPT == *" builder "* ]] && OPT+="docker "
has() { [[ $OPT == *" $1 "* ]]; }
log() { printf '\n== %s\n' "$*"; }
die() { echo "错误：$*" >&2; exit 1; }
cfg() { sed -n "s/^$1:[[:space:]]*\([^[:space:]#]*\).*/\1/p" config/stbcc.yaml | tr -d "\"'" | head -1; }   # stbcc.yaml 顶层的一个标量
in_group() { id -nG "$1" | tr ' ' '\n' | grep -qx "$2"; }

ME=$(id -un)
TOOL=$(cfg tool_user); TOOL=${TOOL:-sparky}
GROUP=$(cfg shared_group); GROUP=${GROUP:-sparkle}

log "1/8 检查前提"
[ "$(uname -s)" = Linux ] || die "只支持 Linux"
[ "$(id -u)" -ne 0 ] || die "不要用 root 跑：以平台用户（普通用户）跑，要 root 的步骤脚本会用 sudo"
[ "$ME" != "$TOOL" ] || die "平台用户不能是工具用户 $TOOL"
for c in docker sudo python3.12 uv setfacl bc git; do command -v "$c" >/dev/null || die "缺 $c"; done
in_group "$ME" docker || die "$ME 不在 docker 组：sudo usermod -aG docker $ME，重新登录后再跑"
docker info >/dev/null 2>&1 || die "连不上 Docker（docker info 失败）：Docker 没在跑，或本次登录还没拿到 docker 组"
systemctl --user show-environment >/dev/null 2>&1 || die "连不上 systemd 用户服务（systemctl --user）：要用 systemd 的发行版，且以登录会话跑（ssh 或本机登录，不要 su）"
ABI=$(python3.12 -c 'import ctypes; print(max(ctypes.CDLL(None).syscall(444, None, 0, 1), 0))')
[ "$ABI" -ge 4 ] || die "内核 Landlock ABI 是 $ABI，至少要 4（Linux 6.7+，且 lsm 里开了 landlock）：工具禁联网靠它"
[ "$ABI" -ge 6 ] || echo "提示：Landlock ABI $ABI < 6（Linux 6.12+），shell 工具的只读通行用不了，别的不受影响"
if has owa; then [ -x /usr/bin/node ] || die "--owa 要 /usr/bin/node（owa-launcher 直接跑 .ts，要 Node 22.18+）"; fi
echo "平台用户 $ME，工具用户 $TOOL，共用组 $GROUP，仓库 $REPO，Landlock ABI $ABI"

log "2/8 需要 root 的系统设置"
THOME=$(getent passwd "$TOOL" | cut -d: -f6 || true); THOME=${THOME:-/home/$TOOL}
WS=$(cfg workspace); WS=${WS:-$THOME/workspace}
SUDOERS=/etc/sudoers.d/stb-${ME//./_}   # 文件名带点的 sudo 不读
DESC=(); CMDS=()
add() { DESC+=("$1"); CMDS+=("$2"); }
getent group "$GROUP" >/dev/null || add "建共用组 $GROUP" "groupadd $GROUP"
id "$TOOL" >/dev/null 2>&1 || add "建工具用户 $TOOL（家目录 $THOME）：工具、监听、workflow 步骤以它的身份跑" "useradd -m -d $THOME -s /bin/bash -c 'stb tool user' $TOOL"
{ id "$TOOL" >/dev/null 2>&1 && in_group "$TOOL" "$GROUP"; } || add "把 $TOOL 加进 $GROUP 组" "usermod -aG $GROUP $TOOL"
in_group "$ME" "$GROUP" || add "把 $ME 加进 $GROUP 组" "usermod -aG $GROUP $ME"
{ id "$TOOL" >/dev/null 2>&1 && sudo -k -n -u "$TOOL" true 2>/dev/null; } ||
  add "加 sudoers 规则 $SUDOERS：$ME ALL=($TOOL) NOPASSWD: ALL（平台以 $TOOL 身份起工具，不要密码）" \
      "echo '$ME ALL=($TOOL) NOPASSWD: ALL' > $SUDOERS.new && visudo -cqf $SUDOERS.new && chmod 440 $SUDOERS.new && mv $SUDOERS.new $SUDOERS"
[ "$(stat -c %U:%G:%a "$THOME" 2>/dev/null)" = "$TOOL:$GROUP:2770" ] && [ -d "$WS" ] ||
  add "$THOME 与工作区 $WS 归 $TOOL:$GROUP、组读写、目录 setgid、组默认可写（ACL 默认项 g::rwx）" \
      "install -d -o $TOOL -g $GROUP $WS && chgrp -R -h $GROUP $THOME && chmod -R g+rwX $THOME && find $THOME -xdev -type d -exec chmod g+s {} + -exec setfacl -d -m g::rwx {} + && chmod 2770 $THOME"
[ "$(stat -c %G:%a "$HOME")" = "$GROUP:2750" ] ||
  add "$HOME（只这一层，不递归）归 $GROUP 组、2750：$TOOL 要穿过它读仓库" "chgrp $GROUP $HOME && chmod 2750 $HOME"
getent group "$GROUP" >/dev/null && [ -z "$(find "$REPO" -xdev \( -path "$REPO/data" -o -path "$REPO/logs" \) -prune -o \( ! -group "$GROUP" -o -type d ! -perm -2050 -o -perm -0020 \) -print -quit)" ] ||
  add "仓库 $REPO（data/、logs/ 除外，平台运行时自己设）归 $GROUP 组、组只读、目录 setgid" \
      "find $REPO -xdev \\( -path $REPO/data -o -path $REPO/logs \\) -prune -o \\( -exec chgrp -h $GROUP {} + -exec chmod g+rX,g-w {} + \\) && find $REPO -xdev \\( -path $REPO/data -o -path $REPO/logs \\) -prune -o -type d -exec chmod g+s {} +"
[ -e "/var/lib/systemd/linger/$ME" ] ||
  add "loginctl enable-linger $ME：没登录时 systemd 用户服务也常驻" "loginctl enable-linger $ME"
if [ ${#CMDS[@]} -eq 0 ]; then
  echo "都已设好"
else
  echo "要用 sudo 做这些事："
  for i in "${!DESC[@]}"; do printf '  %d. %s\n     # %s\n' $((i + 1)) "${DESC[$i]}" "${CMDS[$i]}"; done
  if [ $YES -eq 0 ]; then   # 提示自己打印：read -p 在 stdin 不是终端（管道输入）时不显示提示
    printf '执行？[y/N] '; ok=; read -r ok || true; [ -t 0 ] || echo "$ok"
    [[ $ok == [yY]* ]] || die "没有确认，停在这里（要 root 的步骤也可以照上面的命令自己跑）"
  fi
  for c in "${CMDS[@]}"; do echo "+ sudo $c"; sudo bash -c "$c"; done
fi
MGR=$(systemctl show "user@$(id -u).service" -p MainPID --value)
for g in "$GROUP" docker; do   # 新加的组要重新登录才生效；systemd 用户服务（linger 后常驻）要重启才拿到
  GID=$(getent group "$g" | cut -d: -f3)
  id -nG | tr ' ' '\n' | grep -qx "$g" && grep '^Groups:' "/proc/$MGR/status" | tr -s ' \t' '\n' | grep -qx "$GID" && continue
  echo "$g 组还没在本次登录或 systemd 用户服务里生效：退出所有登录会话后执行 sudo systemctl restart user@$(id -u).service（或重启机器），重新登录再跑一次 ./install.sh"
  exit 0
done

log "3/8 uv sync（Python 依赖装到 .venv）"
uv sync

log "4/8 .env（凭证与个人标识，不进 git）"
if [ -f .env ]; then echo ".env 已有，不动"; else
  cp .env.example .env && chgrp "$GROUP" .env && chmod 640 .env
  echo "已从 .env.example 生成 .env（640、组 $GROUP）：用到哪个组件就填哪几项，模型档位写了 api_key 的也在这里填它的值"
fi

log "5/8 config/models.yaml（模型档位）"
if [ ! -f config/models.yaml ]; then
  cp config/models.example.yaml config/models.yaml
  echo "已从 config/models.example.yaml 生成 config/models.yaml：改成你自己的模型服务"
elif git diff --quiet HEAD -- config/models.yaml 2>/dev/null; then
  echo "config/models.yaml 还是仓库自带的那份：照 config/models.example.yaml 改成你自己的模型服务"
else
  echo "config/models.yaml 已改过，不动"
fi

log "6/8 镜像"
image() { if docker image inspect "$1:latest" >/dev/null 2>&1; then echo "$1:latest 已有"; else "${@:2}"; fi; }
image stb-agent services/stb-agent/build.sh
if has comfyui; then image stb-docker-base docker build -t stb-docker-base:latest services/stb-docker-base; fi
if has owa; then image owa-research services/owa-research/build.sh; services/searxng/up.sh; fi

log "7/8 systemd 用户服务"
STB=$REPO/.venv/bin/stb
for s in runner hub web; do "$STB" "$s" --install; done
has no-cleanup || "$STB" cleanup --install
has discord && "$STB" discord --install
has docker && "$STB" docker --install
has builder && "$STB" builder --install
has owa && "$STB" launcher --install
has comfyui && echo "ComfyUI 另装：装好后在 config/models.yaml 加 runtime: comfyui 的档位（endpoint、home、start、stop，见 config/models.example.yaml 的说明）"

log "8/8 stb doctor --smoke"
"$STB" doctor --smoke || echo "体检有没过的项，看上面的输出（模型档位只查在跑的；改完 .env / models.yaml 再跑 stb doctor --smoke）"
echo
echo "装好了。先 export PATH=\"$REPO/.venv/bin:\$PATH\"，照 README 的 Quickstart 写第一个任务 tasks/hello/task.yaml，再跑："
echo "  stb task check tasks/hello/task.yaml && stb task add tasks/hello/task.yaml && stb run hello --now --wait && stb logs hello"
