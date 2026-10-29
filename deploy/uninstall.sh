#!/usr/bin/env bash
# 数据采集流水线 · 一键卸载
#
#   bash deploy/uninstall.sh                # 停服务 + 删 systemd + 撤掉本站点反代（数据保留）
#   bash deploy/uninstall.sh --purge        # 再删节点库/导出/venv（不可恢复）
#   bash deploy/uninstall.sh --remove-caddy # 连 Caddy 本体一起卸（端口 80/443 释放）
#   bash deploy/uninstall.sh --yes          # 不交互确认
#
# 默认只拆「我们装的东西」，不碰数据、不碰别人的站点。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="nodehub-web"
PURGE=0
REMOVE_CADDY=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --remove-caddy) REMOVE_CADDY=1 ;;
    --keep-caddy) REMOVE_CADDY=0 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）"; exit 1 ;;
  esac
  shift
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "卸载要 root：sudo bash $0"; exit 1; }

echo
echo "即将卸载：$APP_DIR"
echo "  · 停止并删除 systemd 服务（$SERVICE / nodehub-update）"
echo "  · 撤掉 /etc/caddy/Caddyfile 里本站点的配置"
if [ "$REMOVE_CADDY" -eq 1 ]; then
  echo "  · 卸载 Caddy 本体"
fi
if [ "$PURGE" -eq 1 ]; then
  echo "  ⚠️  删除 data/ output/ .venv/ bin/ config.yaml（节点库和导出不可恢复）"
else
  echo "  · 数据保留（data/ output/）"
fi
echo

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "确认继续？[y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "已取消"; exit 0 ;; esac
fi

# ---------------------------------------------------------------- 服务
say "停止并删除服务"
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
systemctl stop nodehub-update.timer 2>/dev/null || true
systemctl disable nodehub-update.timer 2>/dev/null || true
systemctl stop nodehub-update.service 2>/dev/null || true

for u in "$SERVICE.service" nodehub-update.service nodehub-update.timer; do
  rm -f "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
ok "服务已移除"

# ---------------------------------------------------------------- 反代
if [ "$REMOVE_CADDY" -eq 1 ]; then
  if [ -x /usr/local/bin/caddy ]; then
    say "卸载 Caddy 本体"
    systemctl stop caddy 2>/dev/null || true
    systemctl disable caddy 2>/dev/null || true
    rm -f /etc/systemd/system/caddy.service
    rm -f /usr/local/bin/caddy
    rm -rf /etc/caddy /var/lib/caddy
    systemctl daemon-reload
    ok "Caddy 已卸载（80/443 释放）"
  else
    warn "没装 Caddy，跳过"
  fi
elif [ -f /etc/caddy/Caddyfile ] && grep -q '^# >>> nodehub$' /etc/caddy/Caddyfile 2>/dev/null; then
  say "撤掉本站点反代配置"
  # 只删 install.sh 写的标记段，其他站点原样保留
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
  sed -i '/^# >>> nodehub$/,/^# <<< nodehub$/d' /etc/caddy/Caddyfile
  # 去掉尾部空行
  sed -i -e :a -e '/^\n*$/{$d;N;};/\n$/ba' /etc/caddy/Caddyfile 2>/dev/null || true
  if grep -qE '^[^#[:space:]].*\{' /etc/caddy/Caddyfile 2>/dev/null; then
    systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
    ok "已撤掉本站点，Caddy 继续为其他站点服务"
  else
    systemctl stop caddy 2>/dev/null || true
    ok "Caddyfile 已空，Caddy 已停（要彻底删掉加 --remove-caddy）"
  fi
else
  ok "没有本站点的反代配置，跳过"
fi

# ---------------------------------------------------------------- 数据
if [ "$PURGE" -eq 1 ]; then
  say "删除数据"
  rm -rf "$APP_DIR/data" "$APP_DIR/output" "$APP_DIR/bin" "$APP_DIR/.venv"
  rm -f "$APP_DIR/config.yaml"
  ok "数据已清空"
else
  ok "数据保留：$APP_DIR/data 与 $APP_DIR/output"
fi

echo
echo "──────────────────────────────────────────────"
echo " 卸载完成。代码目录还在：$APP_DIR"
[ "$PURGE" -eq 1 ] && echo " 想彻底删掉目录：rm -rf '$APP_DIR'"
echo "──────────────────────────────────────────────"
