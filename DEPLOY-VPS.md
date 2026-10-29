# 在 VPS 上跑（带网页版）

> 一句话：**别用 Go 重写界面。** 保留 Python 内核，加一个零依赖的网页服务，
> 再用 Caddy 反代 + 自动 HTTPS。整套跑在 2 核 2G 上，峰值内存 130MB。

---

## 为什么不用 Go

真正的开销不在界面：

| 组件 | 内存占用 | 说明 |
|---|---|---|
| 网页服务（`web.py`，标准库） | **41 MB**（实测） | 就是个 HTTP 循环，只在有人开页面时干活 |
| Python 流水线（`run.py`） | 50–90 MB | 抓取 + 解析 + SQLite，跑完就退 |
| mihomo 内核 | 40–90 MB | 取决于一次灌多少节点（`chunk`） |
| Caddy（HTTPS 反代） | 54 MB | 只在开了域名时才有 |
| **流水线峰值（cgroup 实测）** | **131 MB** | 含 mihomo 子进程 |

Go 版本顶多把 41MB 压到 8MB，**省 30MB，却要重写 2800 行协议解析、去重、
TCP 预筛、mihomo 控制、导出**，再造一套进程通信。而真正吃资源的是并发探测
和 mihomo，不是界面。

正确做法是**让界面只做转发**：网页只管「转述状态 + 转发命令」，重活全在子进程，
网页崩了任务照跑，任务崩了网页还在。

---

## 架构

```
浏览器 ──HTTPS──> Caddy (80/443, 自动证书)
                    └──> web.py (127.0.0.1:8787, 标准库 ThreadingHTTPServer)
                           │  只读 SQLite、转发命令、SSE 推日志
                           └─ subprocess ──> run.py all ──> mihomo（测速子进程）
systemd timer ──每天 04:17──> run.py all（不经过网页，同样跑一遍）
```

- **零第三方依赖**：只用 `http.server` / `sqlite3` / `subprocess`，不装 Flask、uvicorn、Redis、Node。
- **SSE 而不是 WebSocket**：单向推日志，自带断线重连。
- **单进程、无守护进程**：挂了 systemd 拉起来，状态写 `data/web_state.json`。
- **鉴权**：一个令牌，Cookie 保存 30 天；`/healthz` 免鉴权给探针用。
- **命令白名单**：网页只能触发 `all/collect/check/verify/manage/stats` 和几个已知参数。
- **跨进程锁**：定时任务和网页按钮抢同一把 `data/run.lock`，不会重复跑。

---

## 一键部署

```bash
# 1. 把项目传到服务器（git clone / scp 都行）
cd /opt/nodehub

# 2. 一条命令装完：依赖 + venv + swap + systemd + HTTPS 反代
sudo bash deploy/install.sh --domain 你的域名.com --swap 2048
```

脚本会打印访问地址和令牌：

```
打开     https://你的域名.com/?token=xxxxxxxxxxxx
令牌     xxxxxxxxxxxx
```

打开一次后令牌写进 Cookie，以后直接访问域名即可。

### install.sh 的参数

| 参数 | 作用 |
|---|---|
| `--domain a.com` | 配 Caddy 反代 + HTTPS；`web.host` 自动改成 `127.0.0.1` |
| `--cert <路径> --key <路径>` | 用你自己的证书（不写也行，见下面「HTTPS 与证书」） |
| `--no-cert` | 忽略服务器上的自有证书，强制让 Caddy 申请 Let's Encrypt |
| `--email me@a.com` | Let's Encrypt 的到期通知邮箱（可选） |
| `--port 8787` | 网页端口 |
| `--swap 2048` | 加 2G swap（没有 swap 的机器建议加） |
| `--no-timer` | 不装每日定时任务 |
| `--no-caddy` | 不装反代（自己接 nginx 或走 SSH 隧道） |
| `--force-config` | 用 `config.vps.yaml` 覆盖 `config.yaml`（会先备份） |

**幂等**：可以重复跑，用来升级代码后的重装。已有 `config.yaml` 不会被覆盖。

前置条件（`--domain` 时）：域名 A 记录已指向本机，云安全组放行 **80 和 443**。
证书申请走 HTTP-01，需要 80 端口能从公网访问。

### 手动启动（不用 systemd）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python web.py --config config.yaml
```

### 首次跑一次

网页上点「一键更新」，或者命令行：

```bash
.venv/bin/python run.py all --config config.yaml
```

首次会自动下载**对应平台**的 mihomo 内核到 `bin/`（Linux 是 `.gz`，Windows 是 `.zip`，
arm64 自动选 arm64 包）。

---

## HTTPS 与证书

`--domain` 一开就是全程 HTTPS：80 端口只做 301 跳到 443，令牌和订阅内容都走加密通道，
应用本身在 `127.0.0.1:8787` 上根本不对外。

证书有两种来源，按这个顺序自动选：

1. **你自己的证书**：`/root/cert/<域名>/fullchain.pem` + `privkey.pem` 存在就直接用，
   也可以用 `--cert` / `--key` 指到别处。install.sh 会先校验**私钥是否配对**、
   **证书里的域名对不对得上**，不对直接报错退出，不会让你上线一个浏览器报错的站。
2. **Caddy 自动申请**：没有自有证书时，自动向 Let's Encrypt 申请并**自动续期**。

```bash
# 换成自己的证书（放到默认目录会被自动识别）
bash deploy/install.sh --domain your-domain.example
# 或者显式指定
bash deploy/install.sh --domain your-domain.example \
     --cert /root/cert/your-domain.example/fullchain.pem \
     --key  /root/cert/your-domain.example/privkey.pem
```

**自有证书不会自动续期**。续完之后不用重装，重载一下就行：

```bash
bash deploy/manage.sh cert          # 看当前用的哪张、有效期到哪天、对外提供的对不对
bash deploy/manage.sh cert reload   # 换完证书后重载（先校验配置，错了不会动线上）
```

`manage.sh cert` 会同时打印「证书文件里是什么」和「实际对外提供的是什么」，
两边不一致就说明 Caddy 还没重载。

---

## 网页界面

打开 `https://<域名>/?token=xxx`，顶上常驻四张统计卡（库内节点 / 当前可用 / 有效源 /
上次耗时）和一排操作按钮，下面是进度条和五个标签页。数据全靠 `/api/*` 拉，页面本身是
个几十 KB 的单文件，不引任何 CDN，手机上自动变窄屏：

| 标签 | 内容 |
| --- | --- |
| 运行日志 | 实时日志，SSE 推送；定时任务跑的也会回放 `data/last_run.log` |
| 数据源 | 每个订阅源的质量：抓到多少、活了多少，用来剔掉废源 |
| 可用节点 | 当前可用列表，可按协议、格式筛选，可勾选批量检测、可复制、可下载订阅 |
| **长期可用** | 连续存活 ≥ 1 / 3 / 5 / 7 天的节点，单独一页 |
| **订阅链接** | 按客户端分好的订阅地址，复制就能用 |

右上角还有入口通向**「订阅测速」独立页面**（`/probe`），用来测别人给的订阅链接，见下节。

### 检测：三档筛查

工具栏把「自动」和「手动」分开了。**一键更新**是完整流水线
（采集 → 深度检测 → 复核 → 最终筛查），每天定时任务跑的就是它，不用管；其余都是手动单步。

| 按钮 | 耗时（400 节点） | 做什么 |
| --- | --- | --- |
| 快速检测 | 约 5 秒 | 全库只做 TCP 握手，看端口还开不开 |
| 深度检测 | 约 40 秒 | 全库走真实 HTTP，通过才算可用 |
| 测下载速度 | 30 个约 60 秒 | 最终筛查，真跑 10MB 下载算吞吐 |
| 检测新节点 | 取决于新节点量 | 只测库里没测过的 |
| 复核可用 | 约 10 秒 | 把当前可用的再测一遍，剔掉侥幸通过的 |

**快速检测不改可用状态。** 结果单独写进 `tcp_ok` / `tcp_ms` 两列，「可用节点」表的
**TCP** 列就是它。原因很实在：端口开着不等于代理能用——很多节点服务器上跑着别的 Web
服务，443 永远通，但代理协议早死了。拿 TCP 结果覆盖可用状态会把一批死节点洗成「可用」。

**最终筛查只写 `speed`，同样不动 `alive`。** 测速超时可能只是网络抖动，
不代表节点死了；但它确实不该出现在「有速度」那一档里，所以 `speed` 归 NULL。

三档是漏斗关系，实测这台机器上：`tcp` 4160 → `alive` 722 → `speed` 18。
最后这 18 个才是真正能承载流量的。

**手动检测**在「可用节点」表里勾选（表头可全选本页），底下会浮出一条操作栏，
选快速或深度。勾选传到后端是节点指纹，走 `--fp` 白名单校验（`^[0-9a-f,]+$`），
不合法直接 400，不会拼进命令行。

### 最终筛查的流量账

只测「当前可用里延迟最好的前 30 个」，不是全测——10MB × 几万个是几个 TB。

```
最坏情况  count × size = 30 × 10MB = 300MB / 次
实测     30 个跑 60 秒、约 200MB（慢节点超时提前停）
每天一次  约 6GB / 月
```

嫌费流量就调 `check.speed.count`（比如 15），或者 `enabled: false` 关掉，
或者 `run.py all --no-speed`。关掉之后「有下载速度」那一档就是空的，订阅页会提示原因。

### 长期可用

「可用」只代表**这一轮测速过了**，可能一小时前刚活过来，也可能已经稳了一周。
翻墙最怕的是刚订阅完第二天就死，所以单独开了一页，按**连续存活时长**筛：

- 分段选择器切档：**≥1 天 / ≥3 天 / ≥5 天 / ≥7 天**，每个档位右上角带实时数量角标。
- 「连续存活」从 `alive_since` 算——节点**由死转活的那一刻**，中途测死一次就清零重算，
  不是 `first_seen`（否则一个早就入库、一直半死不活的节点也会被算成「长期可用」）。
- 额外要求 `ok_count >= 2`，也就是至少被测通过两次，避免只碰巧通了一次的节点混进来。
- 空档位不会给一张空白表，而是提示还差多少，例如：
  「还没有连续存活 ≥ 7 天的节点；目前最老的可用节点已连续存活 6 天 8 小时」。
- 复制和下载走同一套筛选，导出的 `count` 和订阅里实际行数一致。

接口层面就是给 `/api/nodes` 和 `/api/export` 加了个 `min_age`（单位：天），
表格和导出走的是同一个 `pick_nodes()`，所以「看到几行就导出几行」：

```bash
# 看 ≥3 天的有几个（rows 是明细，shown 是本次返回条数）
curl -s "https://<域名>/api/nodes?min_age=3&token=xxx" | jq '.shown, .alive_total'

# 直接导出一份 ≥7 天的 clash 订阅（返回的是文件内容，不是 JSON）
curl -s "https://<域名>/api/export?min_age=7&fmt=clash&token=xxx" -o long7.yaml
```

`/api/status` 里的 `stats.long` 就是页面角标的数据源，顺手能看离下一档还差多少：

```bash
curl -s "https://<域名>/api/status?token=xxx" | jq '.stats.long'
# {"1":108,"3":63,"5":27,"7":0,"oldest_hours":152.3}
```

> **新装的库前几档一定是空的。** 这个功能靠积累，不是靠算法——`alive_since` 从部署
> 那一刻才开始记。想有 ≥3 天的数据，就得让定时任务连着跑三天。库里节点最老的连续存活
> 时长会显示在空状态提示里，随时能看出离下一档还差多久。

### 订阅链接

「可用节点」页的复制/下载是一次性的；订阅链接是**给客户端长期用**的——粘一次，
之后客户端自己按间隔回来拉最新的，不用再管。

六个格式，一个格式一张卡片，各自一条链接：

| 格式 | 给谁用 | 节点数（500 个可用时） |
| --- | --- | --- |
| `clash` | Clash Verge Rev / Mihomo Party / FlClash / Stash / ClashX | 500 |
| `v2ray` | v2rayN / v2rayNG / 小火箭 / Karing / Hiddify / NekoBox（base64） | 500 |
| `singbox` | sing-box / Hiddify Next / SFA / SFI（原生 JSON） | 491 |
| `surge` | Surge Mac / iOS | 274 |
| `qx` | Quantumult X | 280 |
| `plain` | 明文链接，自己挑 | 500 |

**每个格式只给该客户端真认的协议。** 多塞一个它不支持的节点，很可能整份订阅导入失败，
而不是只坏那一个，所以 Surge 里没有 VLESS/SSR，圈 X 里没有 VLESS/Hysteria2，
sing-box 里没有 SSR。页面上会写「跳过 N 个」，不是漏了。

链接长这样，路径里的 `key` 就是凭证，不需要登录：

```
https://your-domain.example/sub/YOUR_SUB_KEY/clash
https://your-domain.example/sub/YOUR_SUB_KEY/surge
https://your-domain.example/sub/YOUR_SUB_KEY/clash?min_age=3      # 只要连续存活 ≥3 天的
https://your-domain.example/sub/YOUR_SUB_KEY/clash?tier=speed     # 只要有下载速度的
https://your-domain.example/sub/YOUR_SUB_KEY/clash?tier=speed&min_age=3   # 两个条件叠加
```

### 订阅哪一档，自己选

同一个 `key`，换 `?tier=` 就换一档，页面上点按钮自动带上，不用手拼：

| 档位 | 参数 | 条件 | 这台机器实测 |
| --- | --- | --- | --- |
| 能连接 | 不带 / `?tier=alive` | 深度筛查通过 | 722 个 |
| 有下载速度 | `?tier=speed` | 最终筛查跑出吞吐 | **18 个** |
| 只测了端口 | `?tier=tcp` | TCP 握手成功 | 4160 个 |

页面上三个按钮直接标着这三个数字——「有下载速度」那档少得刺眼，但那才是真能跑流量的。

`speed` 档导出的节点名会带实测吞吐，客户端列表里一眼看出快慢：

```
name: 'SS-001 59.7MB/s 18ms'
name: 'SS-002 57.9MB/s 15ms'
```

想让不同设备用不同档（手机拉 `speed` 保稳、电脑拉 `alive` 保多），
给每个客户端配各自的链接就行，互不影响。

`?min_age=N` 和「长期可用」是同一套筛选，可以跟 `tier` 叠加。

同一套 `tier` 也适用于节点接口和导出接口：

```bash
# 只看有下载速度的节点（rows 是明细，shown 是本次条数）
curl -s "https://<域名>/api/nodes?tier=speed&token=xxx" | jq '.shown, .alive_total'

# 直接导出一份「有速度 + 活了 3 天以上」的 clash 配置
curl -s "https://<域名>/api/export?tier=speed&min_age=3&fmt=clash&token=xxx" -o best.yaml

# 三档各多少（订阅页按钮角标的数据源）
curl -s "https://<域名>/api/status?token=xxx" | jq '.stats | {alive, speed_ok, tcp_ok}'
```

**安全**：链接泄露 = 节点泄露。所以

- 密钥存在 `data/sub_key.txt`（`chmod 600`），页面本身仍然要令牌才能打开；
- key 不对一律返回 404，不返回 401，免得别人靠状态码试探；
- 怀疑泄露了点页面上的「重置链接」，旧链接立刻全部失效（`POST /api/sub/reset`）。

**客户端自动更新**：响应头带 `profile-update-interval: 6`，Clash 系会按这个隔 6 小时
回来拉一次。其他客户端在它自己的设置里改更新间隔。

> 订阅内容有上限，默认 `export.top_n`（500）个。手机上导入几千个节点会卡，
> 所以截断是故意的——页面会写明「符合条件 517，订阅上限 500」。

---

## 订阅测速页（`/probe`）

上面那一整套是「自己的流水线」；这个页面是**测别人给的订阅**。贴一个订阅链接进去，
解析出节点，测可用性、延迟、**下载速度**，结果存成历史，下次接着用。
入口在主界面右上角，或者直接开 `https://<域名>/probe`。

### 它和主流水线是完全分开的

| | 主流水线 | 订阅测速 |
| --- | --- | --- |
| 数据库 | `data/nodes.db` | `data/probe.db` |
| 任务锁 | `data/run.lock` | `data/probe.lock` |
| 日志 | `data/last_run.log` | `data/probe_last.log` |
| 能不能同时跑 | — | **能**，两者互不阻塞 |

代价是两边同时跑会抢带宽，测出来的速度会偏低。真要测速就等定时任务跑完再点。

### 四种检测模式

| 按钮 | 做什么 | 500 节点实测 |
| --- | --- | --- |
| 自动检测 | TCP 预筛 → 真实 HTTP → 给最快的 N 个测下载速度 | TCP 3.5 秒，后面看节点数 |
| 快速 | 只连 TCP，只看端口开不开 | 3.5 秒 |
| 深度 | 真实 HTTP 请求，通过才算可用 | 约 30 秒 |
| 测下载速度 | 按延迟排序取前 N 个 | 每个节点 1-10 秒 |

测速文件 **10 / 20 / 50 MB** 三档，越小越快也越省流量。
**50MB × 15 个 ≈ 750MB**，流量紧张的机器用 10MB——10MB 已经足够算准 0.1~100 MB/s。

### 下载速度怎么测的

mihomo 的 `/proxies/{name}/delay` 只给延迟，没有吞吐量。这里的做法是：

1. 把要测的节点一次性灌进一个 mihomo，建一个 `select` 组；
2. 测之前 `PUT /proxies/PROBE` 把组切到目标节点；
3. 通过 mixed-port 下载 `speed.cloudflare.com/__down?bytes=...`；
4. 读到指定字节数或超时就停，算 `字节 / 耗时`。

**一个内核测完全部节点**，不用每个节点起一次进程。每 10 个一批，跑完一批立刻落库。
「首字节」列是单独发一个 1 字节请求量的，反映建连 + 首包时间。

实测参考：这台 VPS 直连 Cloudflare 是 **95 MB/s**，走香港节点测出 62 MB/s，
所以数字是可信的；如果你看到全部节点都卡在 1-2 MB/s，先怀疑 VPS 自己的带宽。

### 筛选：只有测出速度的才算真能用

一次实测：500 个节点，**37 个 TCP 通 + 真连通过，但只有 3 个跑出了下载速度**。
两者差别很大：

- TCP 通 / 延迟低 → 只证明端口开着、建连快；
- 有下载速度 → 真的把字节搬回来了，**这才是能承载流量的节点**。

节点卡片顶上有一排筛选：

| 控件 | 作用 |
| --- | --- |
| `全部` | 不筛 |
| `有下载速度 N` | 只留跑出速度的，**自动按快慢倒序**，绿色数字就是这批的数量 |
| `可用 N` | 真连测试通过的 |
| `测速失败` | 连得上但下载没跑通，排查用 |
| `速度 ≥ N MB/s` | 只留够快的，`5` 是能顺畅看 4K 的门槛 |
| 搜索框 | 名字 / 地址 / 协议 |
| `测这些` | 直接给当前筛选出来的这一批测速，不用逐个勾 |

筛选会同步影响计数、全选、批量检测和导出；被筛掉的节点自动退出选中集合，
不会出现「点了测速却测了一堆看不见的节点」。典型用法：
`可用` → `测这些` → 等测完切 `有下载速度` → 得到真正能用的那几条。

导出接口对应加了 `only_speed=1` 和 `min_speed=N` 两个参数，勾上
「只导出有下载速度的」就只导这一批（实测 37 → 3）。

### 订阅地址的安全处理

服务端要去访问用户随手贴的地址，所以做了这些：

- 只允许 `http(s)://`，`file://` 之类直接拒；
- 解析域名后检查 IP，**内网段一律拒绝**（127/10/172.16-31/192.168/169.254/100.64
  以及 IPv6 私有段），免得这个接口被当成内网扫描器；
- 单文件最多 8MB；证书错误 / 连不上 / 超时都有人话提示。

### 历史与编辑

- 每次拉取订阅自动存一条记录，之后可以回来接着测（已有结果不会白测）
- 记录：改名、删除（连节点一起删）
- 节点：改名、删除、勾选批量删除
- 勾选后可直接对选中的节点跑快速 / 深度 / 测速

### 命令行

```bash
# 直接给链接：自动建记录并跑完整流程
cd /opt/nodehub && .venv/bin/python run.py probe --url "https://xxx/sub?token=xxx" \
    --mode auto --size 10 --max-speed 15

# 对已有记录重测
.venv/bin/python run.py probe --run 3 --mode speed --size 20
```

### 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/probe` | 页面 |
| POST | `/api/probe/fetch` | 拉订阅 + 解析 + 建记录 |
| POST | `/api/probe/test` | 启动检测（`mode`/`size`/`max_speed`/`ids`） |
| POST | `/api/probe/stop` | 停止 |
| GET | `/api/probe/runs` | 历史列表 |
| GET | `/api/probe/run?id=N` | 单条记录 + 节点明细 |
| GET | `/api/probe/export?id=N&fmt=&only_ok=&only_speed=&min_speed=&sort=` | 导出 |
| POST | `/api/probe/rename` / `/api/probe/delete` | 记录改名 / 删除 |
| POST | `/api/probe/node/rename` / `/api/probe/node/delete` | 节点改名 / 删除 |
| GET | `/api/probe/stream` | SSE 进度与日志 |

所有参数走白名单：`--mode` 只认 `fast/deep/speed/auto`，`--ids` 只认数字和逗号，
`--size`/`--max-speed`/`--run` 只认整数，不合法直接 400。

---

## 日常运维

`deploy/manage.sh` 把常用操作收在一起，不用记 systemd 命令：

```bash
bash deploy/manage.sh status      # 服务 + 内存 + 节点库概况
bash deploy/manage.sh logs 100    # 跟网页服务日志
bash deploy/manage.sh run         # 立刻在前台跑一次流水线
bash deploy/manage.sh timer off   # 关掉每日自动更新
bash deploy/manage.sh url         # 打印带令牌的访问地址
bash deploy/manage.sh doctor      # 自检：venv/内核/磁盘/内存/进程
```

底层就是这些：

```bash
systemctl status nodehub-web          # 网页服务
journalctl -u nodehub-web -f          # 网页日志
journalctl -u nodehub-update -f       # 定时任务日志
systemctl list-timers nodehub-update.timer
```

**网页上看到的日志**：如果这次运行是定时任务跑的（不是网页点的），
网页会回放 `data/last_run.log`，也就是上一次运行的完整输出——所以「偶尔打开看一下」
也能知道昨晚跑得怎么样。

---

## 卸载

```bash
sudo bash deploy/uninstall.sh              # 拆服务 + 撤站点，数据保留
sudo bash deploy/uninstall.sh --purge      # 连节点库/导出/venv 一起删
sudo bash deploy/uninstall.sh --remove-caddy   # 连 Caddy 本体一起卸（释放 80/443）
```

默认只拆「装上去的东西」：停服务、删 systemd unit、删掉 Caddyfile 里
`# >>> nodehub` 到 `# <<< nodehub` 之间的配置段。**其他站点原样保留**，
节点库也不动（攒一次要十几分钟，误删很亏）。

---

## 调参

`config.vps.yaml` 是实测过的 2 核 2G 配置。核心就四个数：

| 参数 | 本机实测值 | 影响 |
|---|---|---|
| `check.tcp.threads` | **160** | 只做 TCP connect，不吃内存，可以开大 |
| `check.real.threads` | **32** | 真连并发，再高容易把慢节点误判成超时 |
| `check.real.chunk` | **200** | 一次灌给 mihomo 的节点数，**直接决定内核内存** |
| `fetch.concurrency` | **10** | 抓订阅源的并发 |

其他机器的参考值：

| 机器 | tcp.threads | real.threads | chunk | fetch.concurrency |
|---|---|---|---|---|
| 1 核 512MB | 40 | 12 | 60 | 3 |
| 1 核 1GB | 80 | 20 | 120 | 6 |
| **2 核 2GB（实测）** | **160** | **32** | **200** | **10** |
| 4 核 4GB | 300 | 48 | 300 | 16 |

**内存主要花在 `chunk`，不是总节点数** —— 3 万节点的库和 5 千节点的库，
峰值内存几乎一样，因为 mihomo 一次只加载 `chunk` 个。

还是被 OOM（`dmesg | grep -i oom`）就按顺序：加 swap → `chunk` 减半 →
`real.threads` 减半。

### 实测时间（2 核 2G，3.2 万节点的库）

| 场景 | 耗时 | CPU | 峰值内存 |
|---|---|---|---|
| 日常增量（没有新节点，只复核） | 0.9 分钟 | 7 秒 | 93 MB |
| `--full --limit 1200`（1198 个进检测） | 2.5 分钟 | 23 秒 | 131 MB |

日常每天一次完全够。想更彻底就 `--full --limit 3000`，约 6 分钟。

---

## 安全

网页暴露在公网时，至少做其中一件：

1. **Caddy 反代 + 自动 HTTPS**（`install.sh --domain` 已经帮你做好）
2. **只监听本地 + SSH 隧道**（零额外内存）
   ```bash
   # config.yaml: web.host: 127.0.0.1
   ssh -N -L 8787:127.0.0.1:8787 root@你的VPS
   # 然后本机开 http://127.0.0.1:8787
   ```
3. **Cloudflare Tunnel**：不开放任何端口，适合没有域名的机器。

`web.py` 自带令牌鉴权，但这不代替 HTTPS——明文 HTTP 下令牌和订阅内容都是裸奔的。

---

## 常见问题

**页面能开，日志不动**
反代把 SSE 缓冲了。Caddy 需要 `flush_interval -1`（模板里已经加了），
Nginx 需要 `proxy_buffering off; proxy_read_timeout 3600s;`。

**证书一直申请不下来**
80 端口没通。检查云安全组、`ss -tlnp | grep :80`、以及域名是否真的解析到本机
（`dig +short 你的域名`）。看日志：`journalctl -u caddy -n 50`。

**点「停止」后 mihomo 还在跑**
`web.py` 用进程组 kill（`start_new_session` + `killpg`），正常能连子进程一起收掉。
残留就 `pkill -f mihomo`。

**点按钮提示「定时任务正在运行」**
正常：定时任务和网页共用一把锁。等它跑完，或者 `systemctl stop nodehub-update.service`。

**下载内核失败**
服务器连不上 GitHub。手动把内核放到 `bin/mihomo` 并 `chmod +x`，
或者在 `config.yaml` 里写 `check.mihomo_path: /usr/local/bin/mihomo`。

**「长期可用」四个档位全是 0**
不是坏了，是还没攒够。连续存活从 `alive_since` 算，新部署的库从当天才开始记，
所以 ≥1 天那档也要等满 24 小时才会有数。页面空状态会告诉你「目前最老的可用节点已连续
存活 X 小时」，拿这个数对照就大概知道离下一档还差多久。另外节点中途被检出一次死就清零
重算，节点池本身在剧烈波动的话，天数会一直上不去——这时候更该关心「有效源」是不是掉了。

**客户端说订阅拉取失败 / 导入后一个节点都没有**
先看是不是格式不对：把链接粘进浏览器，能看到内容就说明服务端没问题。然后确认这个格式
支持你要的协议（Surge 没有 VLESS，圈 X 没有 VLESS 和 Hysteria2），页面上写了每个格式
跳过了多少。如果浏览器里也是 404，就是 key 过期了（点过「重置链接」），回页面重新复制。

**订阅链接能发给别人吗**
技术上能，谁拿到都能拉。但这台机器只有你一个人在用，发出去等于把节点送人，还占你的
带宽。真要分享就单独给他一个 key——目前不支持多 key，只能整个换。

**`database is locked`**
已经开了 WAL。还报就把 `store.prune_days` 调小，或者跑完再开网页。

**快速检测说端口通，深度检测却说不能用**
这是常态，不是 bug。很多节点的服务器上跑着别的 Web 服务，443 一直有人接，但代理协议
早挂了。快速检测只证明「有人接电话」，深度检测才验证「对面真的是代理」。所以 TCP 那列
只能用来初筛，判断能不能上网必须看「延迟」那列（它是深度检测的结果）。

**快速检测跑完，可用节点数量没变**
故意的。快速检测只写 `tcp_ok` / `tcp_ms`，不碰 `alive`。详见上面「检测：自动、手动、快、慢」。

**勾选节点检测没反应**
勾选状态只对**当前页**有效，翻页/换筛选条件会清掉。另外检测是单进程串行的，如果定时任务
正在跑会提示「定时任务正在运行」，等它结束再点。

**测速结果全是 0 或者全部失败**
先确认这个订阅本身是好的：点「快速」看 TCP 通过率，如果 TCP 大面积不通，是节点死了或者
你的 VPS 到那些地区不通，不是测速功能坏了。如果 TCP 通、深度全挂，多半是节点被墙或者
协议参数不对。另外别在定时任务跑的时候测速，两边抢带宽会把速度测低。

**测速很慢 / 跑一次要几分钟**
测速是一个节点一个节点串行下载的，这是为了数字准确。500 个节点里挑 15 个测，每个
1-10 秒，正常 1-2 分钟。嫌慢就把「最多测」调小，或者用 10MB 的文件。

**为什么 500 个节点里只有几个有下载速度**
这是正常的，也是重点：TCP 通、延迟低只说明端口开着，真正能跑流量的本来就只有一小撮。
这台机器实测：`tcp` 4160 → `alive` 722 → `speed` 18。**最后这 18 个才是能用的。**
如果「测速失败」很多而「可用」也很多，说明这些节点建连正常但落地被限速或已废。

**「有下载速度」那一档是空的**
两个原因：一是还没跑过最终筛查（`check.speed.enabled` 被关了，或者只跑过 `--no-speed`），
二是跑过但一个都没测出来。先看 `stats.speed_ok`：
`curl -s "https://<域名>/api/status?token=xxx" | jq '.stats.speed_ok'`。
是 0 就跑一次 `.venv/bin/python run.py speed`，日志里会写「测出速度 N / M」。

**最终筛查会不会把流量跑爆**
不会。默认只测延迟最好的 30 个 × 10MB = 最坏 300MB/次，慢节点超时提前停，实测约 200MB。
每天一次约 6GB/月。嫌多就调 `check.speed.count`，或者 `enabled: false` 关掉。

**订阅拉取失败，提示「不允许访问内网地址」**
订阅地址解析出来的 IP 落在内网段。这是故意的防护，避免这个接口被拿来扫内网。
换个公网地址，或者把订阅内容手动存下来用命令行导入。

**时间全是 UTC**
`sudo timedatectl set-timezone Asia/Shanghai`，然后 `systemctl restart nodehub-web`。

---

## 文件清单

```
web.py                     网页服务（标准库，无框架）
webui/index.html           主界面（无框架、无 CDN、带内联图标）
webui/probe.html           订阅测速页（独立页面，/probe）
config.vps.yaml            VPS 档配置（实测 2 核 2G）
requirements.txt           只两个依赖：PyYAML、requests

deploy/install.sh          一键安装（依赖/venv/swap/systemd/HTTPS 反代）
deploy/uninstall.sh        一键卸载（默认保留数据和其他站点）
deploy/manage.sh           日常运维（status/logs/run/timer/url/doctor）
deploy/Caddyfile           Caddy 站点块模板
deploy/nodehub-web.service 网页服务单元
deploy/nodehub-update.*    每日定时任务

nodehub/config.py          配置加载（run.py 和 web.py 共用）
nodehub/progress.py        进度解析（桌面版和网页版共用）
nodehub/lock.py            跨进程任务锁（Linux 用 flock，Windows 用 OpenProcess 探活）
nodehub/probe.py           订阅测速（拉订阅/识别格式/解析/延迟/下载测速/历史库）
nodehub/normalize.py       节点消毒（脏字段修正 + 不合格节点丢弃，防止整批检测失败）
nodehub/yamlutil.py        YAML 输出（强制字符串加引号，避免 short-id 被当成数字）
nodehub/exporter.py        导出 + 6 种订阅格式转换（clash/singbox/surge/qx/base64/明文）
nodehub/tester.py          TCP 预筛 + mihomo 真连测试（按平台选内核）

data/nodes.db              节点库（SQLite，WAL）
data/web_token.txt         网页访问令牌（chmod 600）
data/sub_key.txt           订阅链接密钥（chmod 600，泄露了就重置）
data/probe.db              订阅测速的历史库（记录 + 节点，和 nodes.db 分开）
data/probe_last.log        上一次测速的完整输出，测速页回放用
data/last_run.log          上一次运行的完整输出，网页回放用
```

桌面版 `gui.py` 一行没动，Windows 上照样双击 `启动界面.bat`。
