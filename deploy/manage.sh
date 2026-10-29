#!/usr/bin/env bash
# 数据采集流水线 · 日常运维（不想记 systemd 命令就用这个）
#
#   bash deploy/manage.sh status      # 服务 + 端口 + 节点库概况
#   bash deploy/manage.sh start|stop|restart
#   bash deploy/manage.sh logs [行数]  # 跟网页服务日志
#   bash deploy/manage.sh run [参数…]  # 立刻在前台跑一次流水线
#   bash deploy/manage.sh timer on|off # 定时任务总开关（systemd timer）
#   bash deploy/manage.sh schedule     # 看当前排期（每天几点 / 每几小时）
#   bash deploy/manage.sh schedule 04:30        # 改成每天 04:30
#   bash deploy/manage.sh schedule every 6      # 改成每 6 小时
#   bash deploy/manage.sh schedule off          # 暂停自动跑（timer 还开着，只是到点不跑）
#   bash deploy/manage.sh url          # 打印访问地址（含令牌）
#   bash deploy/manage.sh doctor       # 自检：依赖/端口/内存/磁盘
#   bash deploy/manage.sh cert         # 看当前用的证书和有效期
#   bash deploy/manage.sh cert reload  # 换完证书后重载（校验通过才 reload）
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
SERVICE="nodehub-web"
PY=".venv/bin/python"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  x\033[0m %s\n' "$*"; }

cmd="${1:-status}"; shift || true

case "$cmd" in
  status)
    echo "── 服务 ─────────────────────────────"
    systemctl is-active --quiet "$SERVICE" \
      && ok "$SERVICE 运行中" || bad "$SERVICE 未运行"
    systemctl is-active --quiet caddy 2>/dev/null \
      && ok "caddy 运行中" || true
    echo
    echo "── 内存 ─────────────────────────────"
    # 进程名是 python（venv 里），不是 python3，所以按命令行匹配
    ps -eo pid=,rss=,etime=,args= | awk '/[w]eb\.py|[r]un\.py/ {
        printf "  PID %-7s %5dMB  %-9s %s\n", $1, $2/1024, $3, substr($0, index($0,$4)) }' \
      | head -5 || true
    free -m | awk 'NR<=2{printf "  %-6s %6s %6s %6s\n",$1,$2,$3,$4}'
    echo
    echo "── 节点库 ───────────────────────────"
    [ -x "$PY" ] && "$PY" run.py stats --config config.yaml 2>/dev/null | sed 's/^/  /' || echo "  还没装好"
    ;;

  start|stop|restart)
    systemctl "$cmd" "$SERVICE"
    sleep 1
    systemctl is-active "$SERVICE" && ok "$SERVICE 已 $cmd"
    ;;

  logs)
    n="${1:-60}"
    journalctl -u "$SERVICE" -n "$n" --no-pager
    ;;

  run)
    say "前台跑一次流水线（Ctrl-C 可中断）"
    exec "$PY" run.py "${1:-all}" "${@:2}" --config config.yaml
    ;;

  timer)
    case "${1:-on}" in
      on)  systemctl enable --now nodehub-update.timer >/dev/null 2>&1; ok "定时检查已开启（每 10 分钟问一次，到点才跑）" ;;
      off) systemctl disable --now nodehub-update.timer >/dev/null 2>&1; ok "定时检查已关闭（排期保留）" ;;
      *)   echo "用法: manage.sh timer on|off"; exit 2 ;;
    esac
    systemctl list-timers nodehub-update.timer --no-pager --no-legend 2>/dev/null | sed 's/^/  /'
    ;;

  schedule)
    case "${1:-show}" in
      show|"")
        "$PY" - <<'PYCODE'
from nodehub import schedule as s
d = s.summary(".")
print(f"  排期：{d['desc']}")
print(f"  上次：{d['last_text']}    下次：{d['next_text']}")
PYCODE
        ;;
      off)   "$PY" -c 'from nodehub import schedule as s; s.save(".", {"enabled": False}); print("  已暂停自动运行")' ;;
      on)    "$PY" -c 'from nodehub import schedule as s; s.save(".", {"enabled": True}); print("  已启用自动运行")' ;;
      every)
        "$PY" -c "from nodehub import schedule as s; d=s.save('.', {'enabled': True, 'mode': 'interval', 'every_hours': ${2:-6}}); print('  已改成：' + s.describe(d))"
        ;;
      [0-9]*:[0-9]*)
        "$PY" -c "from nodehub import schedule as s; d=s.save('.', {'enabled': True, 'mode': 'daily', 'at': '$1'}); print('  已改成：' + s.describe(d))"
        ;;
      *)
        echo "用法: manage.sh schedule [show|on|off|04:30|every 6]"; exit 2
        ;;
    esac
    ;;

  url)
    TOKEN="$(cat data/web_token.txt 2>/dev/null || echo '')"
    DOMAIN="$(grep -oP '^\S+(?= \{)' /etc/caddy/Caddyfile 2>/dev/null | head -1 || true)"
    if [ -n "$DOMAIN" ]; then
      echo "https://$DOMAIN/?token=$TOKEN"
    else
      PORT="$("$PY" -c 'import yaml;print((yaml.safe_load(open("config.yaml"))["web"] or {}).get("port",8787))' 2>/dev/null || echo 8787)"
      echo "http://$(hostname -I | awk '{print $1}'):$PORT/?token=$TOKEN"
    fi
    ;;

  doctor)
    say "自检"
    [ -x "$PY" ] && ok "venv 存在" || bad "venv 缺失，跑 deploy/install.sh"
    [ -f config.yaml ] && ok "config.yaml 存在" || bad "config.yaml 缺失"
    [ -f bin/mihomo ] && ok "mihomo 内核已就绪" || echo "  · mihomo 内核还没下载（首次跑流水线时自动下）"
    echo "  · 磁盘：$(df -h "$APP_DIR" | awk 'NR==2{print $4" 可用 / "$2}')"
    echo "  · 内存：$(free -m | awk 'NR==2{print $3"MB 已用 / "$2"MB"}')  swap：$(free -m | awk 'NR==3{print $2"MB"}')"
    echo "  · 进程：$([ -n "$(pgrep -f 'web\.py' || true)" ] && echo 网页在跑 || echo 网页没跑)"
    LOCK="$APP_DIR/data/run.lock"
    if [ -f "$LOCK" ]; then
      PID="$(cat "$LOCK" 2>/dev/null || echo 0)"
      [ -n "$PID" ] && [ "$PID" != "0" ] && kill -0 "$PID" 2>/dev/null \
        && echo "  · 流水线：正在跑（PID $PID）" || echo "  · 流水线：空闲"
    fi
    ;;

  cert)
    CF=/etc/caddy/Caddyfile
    DOMAIN="$(grep -oP '^\S+(?= \{)' "$CF" 2>/dev/null | head -1 || true)"
    FILES="$(grep -oP '(?<=^\ttls ).*' "$CF" 2>/dev/null | head -1 || true)"
    echo "── 站点 ─────────────────────────────"
    echo "  域名    ${DOMAIN:-（没配反代）}"
    if [ -n "$FILES" ]; then
      echo "  来源    自有证书：$FILES"
    else
      echo "  来源    Caddy 自动申请（Let's Encrypt）"
    fi
    echo
    if [ -n "$FILES" ]; then
      echo "── 证书文件 ─────────────────────────"
      CRTF="$(echo "$FILES" | awk '{print $1}')"
      if [ -f "$CRTF" ]; then
        openssl x509 -in "$CRTF" -noout -subject -issuer -dates 2>/dev/null | sed 's/^/  /'
      else
        bad "证书文件不见了：$CRTF"
      fi
      echo
    fi
    echo "── 实际对外提供 ─────────────────────"
    if [ -n "$DOMAIN" ]; then
      echo | openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null \
        | openssl x509 -noout -subject -issuer -dates 2>/dev/null | sed 's/^/  /'
    fi
    if [ "${1:-}" = "reload" ]; then
      echo
      say "重载 Caddy"
      if caddy validate --config "$CF" --adapter caddyfile >/dev/null 2>&1; then
        ok "配置校验通过"
        systemctl reload caddy 2>/dev/null || systemctl restart caddy
        ok "已重载，新证书立即生效"
      else
        bad "配置有错，没敢 reload"
        exit 1
      fi
    fi
    ;;

  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
