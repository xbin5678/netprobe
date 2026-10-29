#!/usr/bin/env bash
# 数据采集流水线 · 一键安装
#
#   bash deploy/install.sh                                  # 只装本机（127.0.0.1:8787）
#   bash deploy/install.sh --domain your-domain.example           # 装 + HTTPS 反代（推荐）
#   bash deploy/install.sh --domain a.com --email me@a.com  # 用 Let's Encrypt，指定通知邮箱
#   bash deploy/install.sh --domain a.com \
#        --cert /root/cert/a.com/fullchain.pem --key /root/cert/a.com/privkey.pem
#                                                           # 用自有证书（放 /root/cert/<域名>/ 会自动认）
#   bash deploy/install.sh --domain a.com --no-cert     # 忽略自有证书，强制用 Let's Encrypt
#   bash deploy/install.sh --swap 2048                      # 顺手加 2G swap
#   bash deploy/install.sh --no-timer                       # 不装每日定时任务
#   bash deploy/install.sh --no-caddy                       # 不装反代（自己接 nginx）
#   bash deploy/install.sh --force-config                   # 覆盖已有 config.yaml（先备份）
#
# 幂等：可以重复跑，用来升级代码后的重装。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="nodehub-web"
TIMER="nodehub-update.timer"
WEB_PORT="8787"
DOMAIN=""
ACME_EMAIL=""
CERT=""
KEY=""
NO_CERT=0
DO_SERVICE=1
DO_TIMER=1
DO_CADDY=1
FORCE_CONFIG=0
SWAP_MB=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) shift; DOMAIN="${1:-}" ;;
    --email) shift; ACME_EMAIL="${1:-}" ;;
    --cert) shift; CERT="${1:-}" ;;
    --key) shift; KEY="${1:-}" ;;
    --no-cert) NO_CERT=1 ;;
    --port) shift; WEB_PORT="${1:-8787}" ;;
    --swap) shift; SWAP_MB="${1:-2048}" ;;
    --no-service) DO_SERVICE=0 ;;
    --no-timer) DO_TIMER=0 ;;
    --no-caddy) DO_CADDY=0 ;;
    --force-config) FORCE_CONFIG=1 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）"; exit 1 ;;
  esac
  shift
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*"; exit 1; }

[ -f "$APP_DIR/run.py" ] || die "找不到 run.py，脚本必须放在项目的 deploy/ 目录里"
[ "$(id -u)" -eq 0 ] || warn "不是 root：venv 和配置会装好，但 systemd/反代会跳过"

cd "$APP_DIR"

# ---------------------------------------------------------------- 1. 系统依赖
say "检查系统依赖"
PKGS=""
command -v python3 >/dev/null 2>&1 || PKGS="$PKGS python3"
# 注意：`import venv` 在精简系统上能过，但 ensurepip 缺了照样建不出 venv。
# Debian 上真正要装的是 python3-venv，它才带 ensurepip。
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
  PKGS="$PKGS python3-venv python3-pip"
fi
command -v curl >/dev/null 2>&1 || PKGS="$PKGS curl"
command -v tar >/dev/null 2>&1 || PKGS="$PKGS tar"

if [ -n "$PKGS" ]; then
  warn "缺依赖:$PKGS"
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
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "需要 Python 3.9+，当前 $(python3 -V 2>&1)"
ok "Python $(python3 -V 2>&1 | awk '{print $2}')"

# ---------------------------------------------------------------- 2. swap
if [ "$SWAP_MB" -gt 0 ]; then
  if swapon --show 2>/dev/null | grep -q .; then
    ok "已有 swap，跳过"
  else
    say "创建 ${SWAP_MB}MB swap"
    fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -q vm.swappiness=20 || true
    ok "swap 已挂载"
  fi
fi

# ---------------------------------------------------------------- 3. venv
say "准备 Python 虚拟环境"
# 不能只看 .venv/bin/python 在不在：上次建坏的 venv 会留下解释器但没有 pip
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import pip" >/dev/null 2>&1; then
  rm -rf .venv
  if ! python3 -m venv .venv 2>/dev/null; then
    warn "建 venv 失败（多半缺 ensurepip），补装后重试"
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip
    fi
    rm -rf .venv
    python3 -m venv .venv || die "venv 建不出来，请手动装 python3-venv"
  fi
fi
.venv/bin/python -m pip install --upgrade pip -q 2>/dev/null || true
if ! .venv/bin/python -m pip install -q -r requirements.txt 2>/dev/null; then
  warn "默认源慢，换清华镜像"
  .venv/bin/python -m pip install -q -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
ok "依赖就绪：$(.venv/bin/python -c 'import yaml,requests;print("PyYAML",yaml.__version__,"requests",requests.__version__)')"

mkdir -p data output bin

# ---------------------------------------------------------------- 4. 配置
say "生成配置"
if [ ! -f config.yaml ] || [ "$FORCE_CONFIG" -eq 1 ]; then
  if [ -f config.yaml ]; then
    BAK="config.yaml.bak.$(date +%Y%m%d%H%M%S)"
    cp config.yaml "$BAK"; warn "旧配置已备份到 $BAK"
  fi
  cp config.vps.yaml config.yaml
  ok "config.yaml ← config.vps.yaml"
else
  ok "沿用已有 config.yaml"
fi

# 反代模式下只监听回环，8787 不暴露到公网
BIND_HOST="0.0.0.0"
if [ -n "$DOMAIN" ] && [ "$DO_CADDY" -eq 1 ]; then BIND_HOST="127.0.0.1"; fi

.venv/bin/python - "$WEB_PORT" "$BIND_HOST" <<'PY'
import sys
import pathlib
import yaml

port, host = int(sys.argv[1]), sys.argv[2]
p = pathlib.Path("config.yaml")
cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
web = cfg.setdefault("web", {})
web["host"] = host
web["port"] = port
p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"    web.host={host}  web.port={port}")

# 低配机的常见坑：直接抄了桌面版 config.yaml（threads=400），上机就 OOM
tcp = ((cfg.get("check") or {}).get("tcp") or {})
real = ((cfg.get("check") or {}).get("real") or {})
if int(tcp.get("threads") or 0) > 128 or int(real.get("threads") or 0) > 32 \
        or int(real.get("chunk") or 0) > 200:
    print("    [!] 这份配置是桌面档的并发（threads/chunk 偏大），小内存机器容易 OOM。")
    print("        建议改成 tcp.threads=48~64 / real.threads=12~16 / real.chunk=80~120，")
    print("        或者直接重跑：bash deploy/install.sh --force-config")
PY

# ---------------------------------------------------------------- 5. systemd
if [ "$DO_SERVICE" -eq 1 ]; then
  if [ "$(id -u)" -ne 0 ]; then
    warn "非 root，跳过 systemd。手动启动："
    echo "    cd $APP_DIR && .venv/bin/python web.py --config config.yaml"
  else
    say "安装 systemd 服务"
    sed "s#/opt/nodehub#$APP_DIR#g" deploy/nodehub-web.service \
      > "/etc/systemd/system/$SERVICE.service"
    sed "s#/opt/nodehub#$APP_DIR#g" deploy/nodehub-update.service \
      > "/etc/systemd/system/nodehub-update.service"
    sed "s#/opt/nodehub#$APP_DIR#g" deploy/nodehub-update.timer \
      > "/etc/systemd/system/nodehub-update.timer"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE"
    sleep 2
    if systemctl is-active --quiet "$SERVICE"; then
      ok "网页服务已启动（$SERVICE）"
    else
      warn "服务没起来，看日志：journalctl -u $SERVICE -n 40 --no-pager"
    fi

    if [ "$DO_TIMER" -eq 1 ]; then
      systemctl enable --now "$TIMER" >/dev/null 2>&1 || true
      ok "每日自动更新已开启（$(systemctl list-timers "$TIMER" --no-pager --no-legend 2>/dev/null | awk '{print $1" "$2" "$3}' | head -1)）"
    else
      systemctl disable --now "$TIMER" >/dev/null 2>&1 || true
      ok "未开启每日自动更新"
    fi
  fi
fi

# ---------------------------------------------------------------- 6. Caddy 反代
CADDY_OK=0
if [ -n "$DOMAIN" ] && [ "$DO_CADDY" -eq 1 ]; then
  if [ "$(id -u)" -ne 0 ]; then
    warn "非 root，跳过 Caddy"
  else
    say "配置 HTTPS 反代（$DOMAIN → 127.0.0.1:$WEB_PORT）"

    # 证书来源：显式 --cert/--key > 默认目录 /root/cert/<域名>/ > 让 Caddy 自己申请
    TLS_LINE=""
    if [ "$NO_CERT" -eq 0 ] && [ -z "$CERT" ] && [ -f "/root/cert/$DOMAIN/fullchain.pem" ] \
        && [ -f "/root/cert/$DOMAIN/privkey.pem" ]; then
      CERT="/root/cert/$DOMAIN/fullchain.pem"
      KEY="/root/cert/$DOMAIN/privkey.pem"
      ok "发现已有证书：/root/cert/$DOMAIN/"
    fi
    if [ -n "$CERT" ]; then
      [ -f "$CERT" ] || die "证书不存在：$CERT"
      [ -n "$KEY" ] || KEY="$(dirname "$CERT")/privkey.pem"
      [ -f "$KEY" ] || die "私钥不存在：$KEY"
      if ! openssl x509 -in "$CERT" -noout -checkhost "$DOMAIN" 2>/dev/null | grep -q "does match"; then
        warn "证书里的域名跟 $DOMAIN 对不上，客户端会报证书错误，请确认"
      fi
      if [ "$(openssl x509 -in "$CERT" -noout -pubkey 2>/dev/null | openssl md5)" \
           != "$(openssl pkey -in "$KEY" -pubout 2>/dev/null | openssl md5)" ]; then
        die "证书和私钥不匹配（$CERT / $KEY）"
      fi
      TLS_LINE="$(printf '\ttls %s %s\n' "$CERT" "$KEY")"
      ok "使用自有证书：$CERT（到期 $(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)）"
      warn "自有证书不会自动续期，换了证书后跑：bash deploy/manage.sh cert reload"
    else
      ok "未提供证书，由 Caddy 自动申请 Let's Encrypt（需 80 端口可达）"
    fi

    if ! command -v caddy >/dev/null 2>&1; then
      say "下载 Caddy（约 50MB，一次性）"
      curl -fsSL -m 180 -o /usr/local/bin/caddy \
        "https://caddyserver.com/api/download?os=linux&arch=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')" \
        || die "Caddy 下载失败，检查网络或用 --no-caddy 自己接 nginx"
      chmod +x /usr/local/bin/caddy
      ok "caddy $(/usr/local/bin/caddy version | awk '{print $1}')"
    else
      ok "已有 caddy $(caddy version | awk '{print $1}')"
    fi

    mkdir -p /etc/caddy /var/lib/caddy
    # 只取站点块本身：去掉模板里的说明性注释，保留两行标记
    SITE="$(sed -e "s#__DOMAIN__#$DOMAIN#g" -e "s#__PORT__#$WEB_PORT#g" deploy/Caddyfile \
            | awk -v tls="$TLS_LINE" '/^__TLS__$/{ if (tls != "") print tls; next } { print }' \
            | sed '/^#/!b; /^# >>> nodehub$/b; /^# <<< nodehub$/b; d')"
    CF=/etc/caddy/Caddyfile
    if [ ! -s "$CF" ]; then
      # 全新文件：先写全局块（邮箱只在给了的时候写），再写站点块
      {
        echo "# 由 deploy/install.sh 生成，手改前先备份"
        if [ -n "$ACME_EMAIL" ]; then
          printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
        fi
        printf '%s\n' "$SITE"
      } > "$CF"
    else
      # 已有 Caddyfile（可能还有别的站点）：只替换我们这段，别动别人的
      cp "$CF" "$CF.bak.$(date +%Y%m%d%H%M%S)"
      sed -i '/^# >>> nodehub$/,/^# <<< nodehub$/d' "$CF"
      printf '\n%s\n' "$SITE" >> "$CF"
    fi

    cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy（公开节点面板 HTTPS 反代）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl restart caddy
    sleep 3
    if systemctl is-active --quiet caddy; then
      if [ -n "$TLS_LINE" ]; then
        ok "反代已启动，正在用你的证书"
      else
        ok "反代已启动，证书在后台自动申请（约 10-60 秒）"
      fi
      CADDY_OK=1
    else
      warn "caddy 没起来：journalctl -u caddy -n 40 --no-pager"
    fi
  fi
fi

# ---------------------------------------------------------------- 7. 收尾
TOKEN="$(cat data/web_token.txt 2>/dev/null || true)"
[ -n "$TOKEN" ] || TOKEN="（服务首次启动后写入 data/web_token.txt）"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${IP:-}" ] || IP="<服务器IP>"

if [ -n "$DOMAIN" ] && [ "$CADDY_OK" -eq 1 ]; then
  URL="https://$DOMAIN/?token=$TOKEN"
else
  URL="http://$IP:$WEB_PORT/?token=$TOKEN"
fi

cat <<EOF

────────────────────────────────────────────────────────
 装好了
   打开     $URL
   令牌     $TOKEN
   目录     $APP_DIR
   配置     $APP_DIR/config.yaml

 常用命令（也可以直接用 deploy/manage.sh）
   状态     systemctl status $SERVICE
   日志     journalctl -u $SERVICE -f
   手动跑   .venv/bin/python run.py all --config config.yaml
   定时     systemctl list-timers nodehub-update.timer
   改端口   bash deploy/install.sh --port 9000$( [ -n "$DOMAIN" ] && echo " --domain $DOMAIN" )

 卸载     bash deploy/uninstall.sh
────────────────────────────────────────────────────────
EOF
