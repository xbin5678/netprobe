---
name: 数据采集流水线
slug: netprobe
doc_type: install-guide
doc_version: 1.0.0
intended_reader: ai-agent
repo: https://github.com/xbin5678/netprobe
bootstrap_url: https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh
one_liner: curl -fsSL https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh | bash -s -- --domain <你的域名>
interactive: false
idempotent: true
requires:
  os: [debian, ubuntu, centos, fedora, rocky, almalinux]
  python: ">=3.9"
  privilege: root
  arch: [x86_64, aarch64]
  disk_mb: 300
  ram_mb: 512
ports:
  web_internal: 8787
  http: 80
  https: 443
default_install_dir: /opt/nodehub
service_name: nodehub-web
uninstall: bash /opt/nodehub/deploy/uninstall.sh
---

# 安装说明（给 AI 读）

这是一个可以**完全无人值守**安装的自托管服务。你只需要执行一条命令，不需要回答任何交互提问。

## 一、一条命令

```bash
curl -fsSL https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh | bash -s -- --domain example.com
```

把 `example.com` 换成目标域名。域名需要**已经解析到这台机器的公网 IP**，否则 HTTPS 证书申请会失败。

**没有域名**时（只在本机/内网用）：

```bash
curl -fsSL https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh | bash
```

装完访问 `http://<公网IP>:8787/?token=<令牌>`。

## 二、参数

全部参数原样透传给内部的 `deploy/install.sh`。

| 参数 | 说明 | 默认 |
|---|---|---|
| `--domain <域名>` | 启用 HTTPS 反代，服务只监听 127.0.0.1 | 不启用 |
| `--email <邮箱>` | Let's Encrypt 通知邮箱 | 无 |
| `--cert <路径>` | 自有证书 fullchain | 自动找 `/root/cert/<域名>/` |
| `--key <路径>` | 自有证书私钥 | 自动找 `/root/cert/<域名>/` |
| `--no-cert` | 忽略自有证书，强制 Let's Encrypt | — |
| `--port <端口>` | 网页端口 | `8787` |
| `--swap <MB>` | 顺手创建 swap | 不创建 |
| `--no-timer` | 不装每日自动更新 | 装 |
| `--no-caddy` | 不装反代，自己接 nginx | 装 |
| `--no-service` | 不装 systemd 服务（只铺代码） | 装 |
| `--force-config` | 覆盖已有 `config.yaml`（先备份） | 不覆盖 |

如果服务器访问 `raw.githubusercontent.com` 超时（国内线路常见），把命令里的地址换成镜像，其余不变：

```bash
curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh | bash -s -- --domain example.com
```

环境变量（写在命令前面）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `NODEPROBE_DIR` | 安装目录 | `/opt/nodehub` |
| `NODEPROBE_BRANCH` | 分支 | `main` |
| `NODEPROBE_REPO` | `owner/repo` | `xbin5678/netprobe` |

## 三、它会做什么

1. 检查 root 权限，缺 `curl`/`tar` 就用系统包管理器补上。
2. 下载代码压缩包，**依次尝试 5 个源**（GitHub 直连 + 4 个加速镜像），全失败则退回 `git clone`。
3. 解包到 `NODEPROBE_DIR`（默认 `/opt/nodehub`）。**已存在则只覆盖代码**，`data/`、`config.yaml`、`.venv/` 原样保留。
4. 建 Python 虚拟环境、装依赖（`PyYAML` + `requests`，默认源慢会自动换清华镜像）。
5. 首次安装时用 `config.vps.yaml` 生成 `config.yaml`；已有配置则沿用。
6. 下载 mihomo 内核到 `bin/`（约 15MB，按架构自动选）。
7. 装 systemd 服务 `nodehub-web` 并启动；装 `nodehub-update.timer` 每日 04:27 自动更新。
8. 有 `--domain` 时装/配置 Caddy，自动 HTTPS。
9. 打印访问地址、令牌、常用命令。

耗时：全新安装约 **1~3 分钟**（取决于网速，主要在装依赖和下载 Caddy）。
磁盘：约 **300MB**（含 venv、Caddy、内核）。
内存：峰值约 **130MB**，512MB 机器可跑。

## 四、怎么确认装成功

按顺序执行，三条都过才算成功：

```bash
# 1. 服务在跑（期望输出 active）
systemctl is-active nodehub-web

# 2. 本地端口通（期望输出 200）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/

# 3. 拿到访问令牌（期望输出一串字符）
cat /opt/nodehub/data/web_token.txt
```

有域名时再验证一次外网：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://example.com/
```

**注意**：第 2、3 条只看服务本身是否起来，不代表节点数据已就绪。首次安装后节点库是空的，需要等自动更新跑一次，或在页面上点「一键更新」。

## 五、无人值守调用的约定

给需要集成本项目的程序（比如 VPS 初始化脚本）参考：

```bash
install_netprobe() {
  local domain="${1:-}"
  local dir="${2:-/opt/nodehub}"

  local args=()
  [ -n "$domain" ] && args+=(--domain "$domain")

  curl -fsSL https://raw.githubusercontent.com/xbin5678/netprobe/main/deploy/bootstrap.sh \
    | NODEPROBE_DIR="$dir" bash -s -- "${args[@]}"
}

# 用法
# install_netprobe example.com
```

约定：

- **不读 stdin**，不会阻塞等待输入。
- **幂等**：重复执行 = 升级代码，数据和配置不丢。
- **退出码**：`0` 成功；非 `0` 失败（失败原因打印在 stderr，前缀 `[x]`）。
- **不会自动 reboot**，不改防火墙规则。
- 需要 root。非 root 直接退出，不会尝试 sudo。
- 端口冲突会让服务起不来，但安装脚本不会因此中断，需自己按第四节检查。

## 六、装完之后

| 动作 | 命令 |
|---|---|
| 看状态 | `systemctl status nodehub-web` |
| 看日志 | `journalctl -u nodehub-web -f` |
| 手动跑一次 | `cd /opt/nodehub && .venv/bin/python run.py all --config config.yaml` |
| 看定时任务 | `systemctl list-timers nodehub-update.timer` |
| 改端口 | `bash /opt/nodehub/deploy/install.sh --port 9000` |
| 换证书 | `bash /opt/nodehub/deploy/manage.sh cert reload` |
| 卸载 | `bash /opt/nodehub/deploy/uninstall.sh` |

卸载默认**保留** `data/` 目录，要连数据一起删加 `--purge`。

## 七、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `需要 root` | 非 root 执行 | 用 root，或命令前加 `sudo` |
| 镜像全不通 | 服务器无法访问 GitHub 及镜像 | `apt-get install -y git` 后重跑，会退回 git clone |
| `venv 建不出来` | 缺 `python3-venv` | `apt-get install -y python3-venv python3-pip` |
| 服务 active 但打不开 | 端口被占 / 防火墙 | `ss -lntp \| grep 8787`、检查安全组 |
| HTTPS 报证书错误 | 域名没解析到本机，或 80 端口不通 | `dig +short example.com` 核对；放行 80 端口 |
| 页面能开但没节点 | 还没跑过采集 | 点「一键更新」，或等每日定时任务 |
| 小内存机器 OOM | 用了桌面档并发 | `bash deploy/install.sh --force-config` 重新生成 VPS 配置 |

## 八、AI 快速判断清单

执行前确认：

- [ ] 有 root 权限
- [ ] 系统是 Debian/Ubuntu/CentOS/Fedora/Rocky/AlmaLinux 之一
- [ ] Python ≥ 3.9（脚本会检查，不够会报错退出）
- [ ] 若用 `--domain`：域名已解析到本机公网 IP，且 80/443 端口可达
- [ ] 磁盘剩余 ≥ 300MB

执行后确认：

- [ ] `systemctl is-active nodehub-web` 输出 `active`
- [ ] `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/` 输出 `200`
- [ ] `/opt/nodehub/data/web_token.txt` 存在且非空

三条全绿 = 安装成功。
