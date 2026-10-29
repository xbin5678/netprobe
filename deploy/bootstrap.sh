#!/usr/bin/env bash
# 数据采集流水线 · 一条命令安装
#
#   curl -fsSL https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh \
#     | bash -s -- --domain example.com
#
# 这个脚本只做三件事：装最小依赖 -> 把代码拉到 /opt/nodehub -> 交给 deploy/install.sh。
# 真正的安装逻辑都在 install.sh 里，所以本地装和远程装走的是同一条路径。
#
# 可用环境变量：
#   NODEPROBE_DIR     安装目录，默认 /opt/nodehub
#   NODEPROBE_BRANCH  分支，默认 main
#   NODEPROBE_REPO    owner/repo，默认 xbin5678/netprobe
#
# 幂等：重复跑就是升级代码，data/ 和 config.yaml 不会被覆盖。
set -euo pipefail

REPO="${NODEPROBE_REPO:-xbin5678/netprobe}"
BRANCH="${NODEPROBE_BRANCH:-main}"
DIR="${NODEPROBE_DIR:-/opt/nodehub}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  -h|--help)
    sed -n '2,17p' "$0" 2>/dev/null || true
    exit 0 ;;
esac

# ---------------------------------------------------------------- 0. 前置检查
[ "$(id -u)" -eq 0 ] || die "需要 root：请用 root 执行，或在命令前加 sudo"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  say "系统：${PRETTY_NAME:-unknown}"
fi

PKGS=""
command -v curl >/dev/null 2>&1 || PKGS="$PKGS curl"
command -v tar  >/dev/null 2>&1 || PKGS="$PKGS tar"
if [ -n "$PKGS" ]; then
  warn "缺依赖:$PKGS，先装"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y $PKGS
  elif command -v yum >/dev/null 2>&1; then
    yum install -y $PKGS
  else
    die "请手动安装$PKGS 后重跑"
  fi
fi

# ---------------------------------------------------------------- 1. 拉代码
# GitHub 在国内直连经常断，多准备几个镜像依次试；都不行再退回 git clone。
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

URLS=(
  "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
  "https://ghfast.top/https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  "https://gh-proxy.com/https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  "https://ghproxy.net/https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
)

say "下载代码（$REPO@$BRANCH）"
GOT=0
for u in "${URLS[@]}"; do
  host="$(printf '%s' "$u" | awk -F/ '{print $3}')"
  if curl -fsSL --connect-timeout 8 --max-time 90 -o "$TMP/src.tar.gz" "$u" 2>/dev/null \
      && [ "$(wc -c < "$TMP/src.tar.gz")" -gt 20000 ]; then
    ok "来自 $host"
    GOT=1
    break
  fi
  warn "$host 不通，换下一个"
done

if [ "$GOT" -eq 0 ]; then
  if command -v git >/dev/null 2>&1; then
    warn "镜像全不通，改用 git clone"
    git clone -q --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$TMP/src" \
      || die "git clone 也失败，检查服务器能否访问 GitHub"
  else
    die "镜像全不通，且没有 git。装一个 git 再重跑：apt-get install -y git"
  fi
else
  mkdir -p "$TMP/src"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1 \
    || die "解包失败，压缩包可能不完整"
fi

[ -f "$TMP/src/deploy/install.sh" ] || die "包内容不对，缺少 deploy/install.sh"
[ -f "$TMP/src/web.py" ]            || die "包内容不对，缺少 web.py"
ok "代码完整"

# ---------------------------------------------------------------- 2. 落到安装目录
mkdir -p "$DIR"

if [ -f "$DIR/config.yaml" ]; then
  say "覆盖代码到 $DIR（保留 data/ 与 config.yaml）"
else
  say "安装到 $DIR"
fi

# 用 tar 管道同步：运行期数据、虚拟环境、旧配置一律不碰
(
  cd "$TMP/src"
  tar cf - \
    --exclude=./data --exclude=./output --exclude=./out \
    --exclude=./.venv --exclude=./.tmp --exclude=./.git \
    --exclude=./config.yaml --exclude='./*.db' --exclude='./*.log' \
    .
) | ( cd "$DIR" && tar xf - )
ok "代码已就位"

# ---------------------------------------------------------------- 3. 交给 install.sh
say "开始安装"
echo
cd "$DIR"
exec bash deploy/install.sh "$@"
