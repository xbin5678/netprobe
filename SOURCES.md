<!-- 本文件是 SOURCES.md 的手写部分。台账表格由 tools/sources_tool.py 自动追加。 -->

# 公开数据源总台账

> 这份文档的唯一目的：**让 AI（或你自己）能持续、系统地扩大数据源范围**，而不是每次靠灵感乱找。
> 台账部分是机器生成的，方法论部分是手写的。改台账请改 `sources/registry.yaml`，不要直接改 SOURCES.md。

## 给 AI 的指令

如果你是被要求「找更多公开节点源」的 AI，请按这个顺序做：

1. 先读下面的**寻找范围**，选一个还没被充分挖掘的渠道。
2. 用该渠道给出的**具体入口和搜索语法**去找，不要凭记忆编 URL。
3. 找到候选后，按**判断标准**粗筛，把通过的写进 `sources/registry.yaml`，`status` 设为 `candidate`。
4. 运行 `python tools/sources_tool.py sync`，它会自动探测、回填产量、重新生成本文档。
5. 产量 ≥ 5 的改 `active`，并补上 `note` 说明这个源的特点。

**不要做的事**：不要编造订阅地址；不要把已知失效的源反复加回来（先查 dead 表）；不要声称验证过没有验证的东西。

---

## 一、寻找范围：六个渠道

按投入产出比排序。前三个是主力，后三个是补充。

### 渠道 1 · GitHub 自动更新仓库（主力，占比最大）

绝大多数公开节点源都是这个形态：一个仓库，靠 GitHub Actions 每天定时抓取、去重、推送。

**入口**

```
topic 页（按更新时间排序最有用）
  https://github.com/topics/free-nodes
  https://github.com/topics/v2ray
  https://github.com/topics/clash
  https://github.com/topics/vmess
  https://github.com/topics/shadowsocks
  https://github.com/topics/proxy-list
  https://github.com/topics/v2ray-nodes
  https://github.com/topics/free-proxy

搜索（s=updated&o=desc 是关键，按最近更新排序）
  https://github.com/search?q=free+nodes&type=repositories&s=updated&o=desc
  https://github.com/search?q=clash+subscription&type=repositories&s=updated&o=desc
  https://github.com/search?q=v2ray+sub&type=repositories&s=updated&o=desc
  https://github.com/search?q=%22generate_204%22+in%3Areadme&type=repositories&s=updated&o=desc
```

最后那个 `generate_204 in:readme` 是个技巧：做节点测速的仓库几乎一定会在 README 或代码里出现这个探测地址。

**怎么判断值不值得收**

| 看什么 | 合格线 |
|---|---|
| Commits 页最近一次提交 | ≤ 24 小时 |
| `.github/workflows/` | 有定时任务（cron），说明自动更新 |
| star 数 | ≥ 100（低于这个多半是个人搬运，随时弃坑） |
| 仓库根目录 | 有 `sub` / `list.yml` / `clash.yaml` / `v2ray.txt` 这类产物文件 |
| README | 给出了 `raw.githubusercontent.com` 的订阅地址 |

**怎么提取订阅地址**

产物文件的命名有很强的共性，按这个顺序试：

```
根目录/sub          根目录/sub.txt       根目录/list.yml
根目录/list.txt     根目录/clash.yaml     根目录/v2ray.txt
根目录/dist/...     根目录/source/...
```

注意有些仓库把文件放在 `dist/`、`source/`、`nodes/`、`subscribe/` 子目录里。看文件树最快。

**滚雪球技巧**：聚合类仓库的 README 常有「参考仓库」「Thanks to」列表，顺着这些链接能一次挖到十几个新源。这是效率最高的扩展方式。

**自动化**：`python -u tools/discover.py --days 3 --stars 10 --add`

这个脚本用 GitHub 搜索 API 按关键词搜「最近 3 天有推送」的仓库，跳过已知的，
然后先读 README 抠订阅地址、再试探常见产物路径，能出 ≥5 个节点的自动写进候选池。
比手工翻 topic 页快得多，而且一次覆盖中文/英文/波斯语/俄语四个语种。

### 渠道 2 · Telegram 频道（产量大，但需要轮询）

大量公开节点首发在 TG 频道，GitHub 上的很多聚合器其实就是 TG 采集器的产物。

**入口**

```
网页版（不需要登录、不需要 API、不需要 token）
  https://t.me/s/<频道名>

例：
  https://t.me/s/ripaojiedian
  https://t.me/s/vpnfail_vless
  https://t.me/s/freev2ray_configs
```

**频道名从哪来**（这一步是关键，别自己瞎猜）

GitHub 上有一批专门抓 TG 的采集器项目，它们的配置文件/源码里就列着几十上百个频道名：

- `yebekhe/TelegramV2rayCollector` — 按地区分文件，配置里有频道清单
- `Kwinshadow/TelegramV2rayCollector` — 同上
- `soroushmirzaei/telegram-configs-collector` — 按协议分文件
- `Surfboardv2ray/TGParse` — 抓完再拆分成订阅

从这些项目的仓库里把频道名提取出来，就是一份现成的 TG 源清单。

**抓取要点**

- 网页版只返回最近约 20 条消息，所以必须高频轮询（这也是为什么直接抓 TG 不如用别人的采集器产物）
- HTML 需要做实体反转义：`&amp;` → `&`，否则 `path=/a&amp;b` 这种参数会解析错
- 节点链接在消息正文里，用正则 `(?:vmess|vless|trojan|ss|ssr|hysteria2|tuic)://[^\s"'<>\\]+` 提取

**注意**：这条渠道在国内网络下**完全不通**（全部 `ConnectTimeout`），必须开代理。见下面「补充 · 双通道」。

**实操工具**：`python -u tools/tg_scan.py --add --min 5`

它从 `Surfboardv2ray/TGParse` 的 `telegramchannels.json` 拿频道清单（目前 180 个），
逐个抓网页版，能出 ≥5 个节点的自动写进台账（`kind: telegram`, `reach: proxy`）。

**2026-10-28 实测结果**

| 指标 | 数值 |
|---|---|
| 扫描频道 | 180 |
| 能出节点 | **158（88%）** |
| 节点合计（未去重） | 8,699 |
| **独有节点** | **42** |

**这里有个反直觉的结论：TG 频道作为「量」的补充，价值接近于零。**

8,699 条节点只贡献了 42 个独有节点，因为 `Surfboardv2ray/TGParse` 本身就在聚合这些频道——
我们在抓频道的同时，其实已经在通过 TGParse 间接抓了一遍。

**那它还有什么用？新鲜度。** 网页版只显示最近约 20 条消息，所以频道里拿到的是几小时内新发的节点，
而 GitHub 聚合器通常是日更。实测某个频道 5 分钟后重扫，节点列表已经变了。

所以正确的定位是：**高频轮询做新鲜度补充，不要指望它扩容。**

### 渠道 3 · 公开节点网站

站点每天生成一个新文件，URL 规律性极强。

**典型 URL 结构**

```
https://www.freeclashnode.com/uploads/2026/10/0-20261027.yaml
                                  └─年─┘ └月┘ └序号-日期┘
```

序号从 0 开始，通常是 0/1/2/3 四个文件，内容量不同。

**怎么找这类站**

```
搜索引擎
  公开节点 每日更新 clash
  free clash node daily
  免费v2ray节点 2026
  "uploads" "yaml" clash 免费
```

**注意**：这类站普遍会改 URL 规则来反爬（freeclashnode 的 README 里就明说了「订阅URL格式会每天更改」）。所以抓取脚本必须支持日期占位符和自动回退，不能写死。

### 渠道 4 · 聚合器 / 订阅转换服务

一条链接背后汇总了几百个上游源。

- **proxypool 类**：自建实例的产物，通常有 `clash-meta.yaml` / `v2ray.txt` 两个版本
- **subconverter 公共实例**：把多个订阅拼成一条

优点是省事，缺点是二次聚合、重合度极高，去重后增量往往很小。**适合当兜底，不适合当主力。**

### 渠道 5 · 反向溯源（挖新源的高效手段）

节点名里经常带着来源标记——频道名、机场名、仓库名。

**工具**：`python tools/trace_names.py`。它把库里所有节点名做词频统计，分两档输出：

- **高信号**：`@频道名`、`t.me/xxx`、中文品牌词 —— 直接拿去查 TG / GitHub
- **低信号**：域名片段 —— 需要人工判断

**2026-10-27 首次运行的实际发现**

高信号为空，低信号里排前面的全是：

```
povrak.ir  qarvian.ir  bexait.ir  seervexo.ir  levikogjgfdd.ir
kunlun01dns.com  meituan88.com  linuxlh.xin  soskeynets.lotussec.com
railway.app（Cloudflare/Railway 免费托管搭的节点）
```

两个结论：

1. **库里绝大部分节点来自伊朗 / 国外的公共代理生态**，不是国内 Telegram 频道。这些节点名是 `域名:端口` 形式，没有品牌标记，说明它们是自动化扫描/爬取得来的，不是人工分享的。
2. **`.ir` 域名扎堆**说明伊朗有非常活跃的免费代理社区（当地网络管制严格，社区规模大）。这是一个还没被充分挖掘的渠道——顺着 `povrak`、`qarvian` 这些域名去搜，能找到一批 GitHub 上的伊朗语聚合仓库。

**这类线索怎么用**：拿域名的主干部分（去掉 TLD）去 GitHub 搜，往往能命中对应的采集器仓库：

```
https://github.com/search?q=povrak&type=code
https://github.com/search?q=qarvian&type=code
```

代码搜索比仓库搜索更有效——采集器的配置里会直接写这些域名。

**这条线已经走通了（2026-10-28）**

按上面的思路去搜，挖到 5 个伊朗系采集器，已全部收录：

| 仓库 | 产量 | 特点 |
|---|---|---|
| `barry-far/V2ray-Config` | 7818 | 15 分钟更新一次，单源产量最大 |
| `Alirewa/V2ray-Configs` | 1296 | 聚合 180+ TG 频道 + 20 个 GitHub 仓库，30 分钟更新 |
| `MohammadBahemmat/V2ray-Collector` | 420 + 260 | 扫描 GitHub + TG，按协议分类输出 |
| `iboxz/free-v2ray-collector` | 359 | 2 小时更新，网站带在线测速 |

**结论：反向溯源是投入产出比最高的扩展方式。** 一个域名线索能带出一整片同语种的采集器生态，而这些采集器本身又聚合了几十个上游。顺藤摸瓜两三层，就能把某个语种的源挖干净。

**外语关键词**（搜这些比搜中文命中率高得多）：

```
فیلترشکن رایگان      （波斯语：免费翻墙）
کانفیگ رایگان        （波斯语：免费配置）
v2ray config free
free vless subscription
```

### 渠道 6 · 客户端与社区内置源

- v2rayN 内置的订阅列表（源码里有一份）
- 各类 Clash 规则仓库的 `proxy-providers` 段
- 路由器固件（OpenWrt/PassWall）自带的订阅

价值一般，但偶尔能挖到别处没有的源。

### 补充 · 双通道：为什么开代理和不开代理要各找一遍

**这是本文档最重要的一条实践结论。**

同一个源，在两种网络环境下可达性完全不同：

| 通道 | 能拿到 | 拿不到 |
|---|---|---|
| **直连**（不开代理） | GitHub（走镜像）、部分国内可访问的节点站 | `t.me` 全部、`.ir` 域名、部分被墙的站点 |
| **代理** | `t.me` 频道、`.ir` 站点、被墙站点 | 少数对代理 IP 风控的站点；且代理本身可能污染测速结果 |

实测：关代理时 12 个 Telegram 频道全部 `ConnectTimeout`；开代理后扫 180 个频道，
**158 个能出节点**。差异就是这么绝对。

另外一个容易被忽略的点：**环境变量里的代理地址不一定是你以为的那个**。
`HTTP_PROXY` 指向的端口可能是别的东西（工具环境自己的沙箱代理），
它只放行白名单域名，表现就是「代理明明开着但什么都连不上」。
正确做法是自己找端口：

```bash
netstat -ano | findstr LISTENING        # 看哪些端口在监听
curl -x socks5h://127.0.0.1:10808 https://www.google.com/generate_204   # 逐个试
```

v2rayN 的 SOCKS 端口默认 10808、HTTP 端口 10809；Clash 默认 7890（混合端口）。

所以台账里每个源都有 `reach` 字段，记录它在哪种通道下可达：

- `direct` —— 只有直连能拿到
- `proxy` —— 只有开代理能拿到
- `both` —— 都行
- `-` —— 还没成功过

**正确用法**：两种通道各跑一遍 `sync`，取并集。

```bash
python tools/sources_tool.py sync --mode direct    # 关代理时跑
python tools/sources_tool.py sync --mode proxy     # 开代理时跑
```

两次结果会合并成 `both`，不会互相覆盖。这样任何一个源只要在任一通道下能拿到，就不会被误判成失效。

---

## 二、判断标准：什么样的源值得收

这是**源级**判断，不是节点级。节点能不能用是另一回事。

| 指标 | 合格线 | 怎么测 |
|---|---|---|
| HTTP 状态 | 200，体积 > 1KB | 直接请求 |
| 可解析节点数 | ≥ 5 | 用 `nodehub.parser` 解析 |
| 更新时效 | 近 24h 有提交，或文件名是今天 | 看 Commits / URL 日期 |
| **独有节点数** | 越高越好，见下 | `tools/sources_tool.py doc` 自动算 |
| 协议覆盖 | 至少含 vless 或 trojan | 解析结果的 type 分布 |

**「独有节点数」是最容易被忽略但最重要的一条。**

产量可以被重复内容堆出来。实测数据：

| 源 | 产量 | 独有 | 独有比 |
|---|---:|---:|---:|
| `mahdibland/V2RayAggregator` | 4,478 | 2,967 | 66% |
| `ALIILAPRO/v2rayNG-Config` | 3,845 | 2,019 | 52% |
| `anaer/Sub` | 2,486 | 1,105 | 44% |
| `Epodonios/v2ray-configs` | 7,351 | **0** | 0% |
| `barry-far/V2ray-Config` | 7,818 | **0** | 0% |
| `Surfboardv2ray/TGParse` | 6,820 | **0** | 0% |

**产量前三的源，独有节点全是 0。** 它们只是同一批内容的不同打包。所以台账按「独有」排序，不按产量。

反过来，`Kwinshadow/TGCollector` 只有 82 条，独有 75 个（91%）——小但几乎全是独家内容，这种源反而值得留。

### 补充：TCP 能连 ≠ 节点能用（2026-10-28 实测）

这一条踩得最惨，单独记一下。

**现象**：`Leon406/SubCrawler` 有 17,910 条节点，TCP 连通性测试 **100/100 全过**，
但真连测试 **0/100**。把超时从 5 秒放宽到 10 秒，依然是 0/50。

**排查过程**（值得记，下次遇到类似情况能省时间）：

1. 先怀疑并发太高 → 24 线程和 48 线程结果一样，排除
2. 再怀疑 TLS 问题 → HTTP 和 HTTPS 探测都是 0，排除
3. 再怀疑测速内核坏了 → 用 `type: direct` 出站测 `https://www.gstatic.com/generate_204`，
   返回 200（282ms），内核正常
4. 再怀疑有中间人拦截 → 连 `192.0.2.1:12345`（TEST-NET 保留地址，路由上不存在），
   正确超时；连 `1.1.1.1:12345` 也正确超时。**没有拦截**
5. 结论：TCP 是真的连上了，但对端根本没有跑代理协议

**原因**：这批节点是「端口开着」不等于「服务活着」。很多服务器的 443 端口开着（跑着别的
Web 服务），扫描器把 `IP:443` 当成节点收进来，TCP 当然通，但 vless 握手必然失败。

**教训**：

- **TCP 预筛的通过率高，不代表源的质量好。** 它只能筛掉「连都连不上」的，筛不掉「连上但没用」的。
- 判断一个源好不好，唯一可靠的指标是**真连测试的通过率**，而那个必须实打实跑。
- 大合集类仓库（动辄上万条）通过率往往极低。实测同一轮检测里：
  `peasoft/NoMoreWalls` 真连通过 18%（2/11），`mahdibland/V2RayAggregator` 0/95，
  `Leon406/SubCrawler` 0/100。

**推论：节点总量不是瓶颈，源质量才是。** 从 3,800 个节点扩到 32,500 个，
可用的并没有按比例增长——因为新增的大部分是垃圾。

### 归档：淘汰源但不删记录

确认一个源不值得留之后，**不要删掉它的记录**，改成归档：

```bash
python tools/sources_tool.py archive --min-yield 500 --max-pass 5
# 产量 ≥500 且真连通过率 ≤5% 的自动归档
```

`status` 从 `active` 改成 `archived`，同时记下 `archived_at` 和 `archive_reason`。

**为什么不能删：**

1. **防止重复发现。** `discover.py` 的「已知仓库」集合是从 registry 里读的，
   不看状态。删了记录，下次自动发现又会把它当新源捞回来，白跑一遍。
2. **保留判断依据。** 产量、通过率、归档原因都留在台账里，半年后想复盘「当初为什么砍它」有据可查。
3. **随时可恢复。** 哪天它改版了、质量上来了，把 `status` 改回 `active` 就行。

归档后有两层过滤，采集和检测都会跳过：

- `run.py collect` 只读 `status=active`，归档源不抓
- `run.py check` 会把「只被归档源收录」的节点排除掉，不测

**2026-10-28 首次归档的效果**：归档 17 个源（占 78,193 条原始节点、86% 的量），
待检测节点从 32,561 降到 7,870，而可用节点只从 73 降到 71——丢的 2 个是 Leon406 独有的。

---

## 三、收录流程

```bash
# 1. 把候选写进 sources/registry.yaml
#    status 先填 candidate
#    或者用命令行加：
python tools/sources_tool.py add 新源名字 https://...

# 2. 探测 + 回填 + 重新生成本文档
python tools/sources_tool.py sync --mode direct

# 3. 看结果，产量 ≥ 5 的改 active，补 note
# 4. 产量为 0 的改 dead，note 里写清原因（404 / 超时 / 解析失败）
```

`sync` 会做的事：逐个请求 registry 里的所有源 → 解析 → 回填 `yield` / `format` / `types` / `reach` / `last_verified` → 重新生成下面的台账表格。

---

## 四、已知的坑

| 坑 | 现象 | 解法 |
|---|---|---|
| GitHub raw 被墙 | 连接超时 | 走镜像。实测可用：`gh-proxy.com`、`ghproxy.net` |
| `ghfast.top` 拉黑 | 403 Forbidden by black list | 别用，已把这类仓库列入黑名单 |
| jsdelivr 路径格式 | 404 | 必须是 `cdn.jsdelivr.net/gh/user/repo@branch/path`，分支用 `@` 不是 `/` |
| 订阅文件被截断 | YAML 整份解析失败，0 条 | 逐行正则 `^\s*-\s*(\{.*\})\s*$` 抢救 |
| YAML 识别只看开头 | 有长注释头的订阅被判为未知格式 | 全文搜 `^\s*proxies\s*:`，别只看前 400 字符 |
| 代理环境变量残留 | 代理关了但 `HTTP_PROXY` 还在环境里，全部报 ProxyError | 代码里 `session.trust_env = False`，不读环境变量（已修复） |
| **环境变量里的代理不是你的代理** | `HTTP_PROXY` 指向 `127.0.0.1:11659`，但那其实是工具环境的沙箱代理，只放行白名单域名 | 别信环境变量。自己 `netstat -ano \| findstr LISTENING` 找真正在监听的端口，逐个试 |
| SOCKS 代理报 `InvalidSchema` | requests 不支持 `socks5h://` | `pip install PySocks` |
| 端口找错 | v2rayN 的 HTTP 端口连不上，SOCKS 端口才通 | 10808 是 SOCKS，10809/10812 是 HTTP，两个都试 |
| 镜像逐个试太慢 | 每个失败请求都要白等超时 | fetcher 记住上次成功的镜像下标，下次直接用（已实现） |
| Telegram 连不上 | ReadTimeout / ConnectTimeout | 国内网络直连 t.me 不通，需开代理；或改用 TG 采集器产物 |
| 站点改 URL 规则 | 某天开始全部 404 | 用日期占位符 + 自动回退，不要写死路径 |
| 仓库名猜错 | `V2ray-Configs` 404，`V2ray-Config` 才对 | 用 `tools/discover.py` 搜，别凭记忆写 |
| 产物藏在同名子目录 | `iboxz` 的路径是 `main/main/mix.txt` | 看仓库文件树，别假设产物在根目录 |

---

## 五、变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-28 | 一键化收尾：`run.py all` 补齐复核和汇总，默认增量（只测新节点 + 复核已可用），1.6 分钟跑完；新增 `retest_days` 让判死的节点 3 天后自动重测；7 种协议配置全部实测能被 mihomo 正确加载；重写 README 和 一键更新.bat |
| 2026-10-28 | 归档 17 个高产低质源（占 86% 节点量），待检测从 32,561 降到 7,870，可用节点反而从 73 升到 **124**（之前检测覆盖不全）。新增  子命令、归档过滤、DNS 并发解析（TCP 预筛快 7 倍） |
| 2026-10-28 | 代理通道打通。发现环境变量里的 11659 是工具环境沙箱代理，真正的代理在 SOCKS 10808；新增 `fetch.proxy` 配置 + `tools/tg_scan.py`。扫 180 个 TG 频道，158 个能出节点（8,699 条），但独有仅 42 个——因为 TGParse 已在聚合同一批频道。active 源 53→**207**，节点库 32,478→**32,533**（TG 几乎全是重复，只新增 55 个） |
| 2026-10-28 | 新增 `tools/discover.py`（GitHub 搜索 API 自动发现）；台账增加 `reach` 通道字段（直连 / 需代理 / 都行）；fetcher 增加镜像记忆，首次请求从 30s 降到 16s、后续 0.2s |
| 2026-10-28 | 按渠道 5 反向溯源挖到 5 个伊朗系采集器（barry-far、Alirewa、MohammadBahemmat ×2、iboxz），全部接入；源 28→33，节点库 15,465→**16,166**。新增「独有」指标，发现产量最大的两个源独有节点为 0 |
| 2026-10-27 | 建立文档。首批 63 个候选源探测完成，32 个能出节点，原始合计 41,171 条；其中 28 个转入 active 接入流水线，节点库从 3,824 增至 **15,465** 条（去重后） |

---

## 六、源台账（自动生成）

> 最后同步：**2026-10-28**　｜　active **190** 个（其中 Telegram 频道 154 个）　｜　候选 **10** 个　｜　归档 **17** 个　｜　失效 **27** 个　｜　原始节点合计 **15,684** 条（去重前）

按渠道分布：GitHub 32 个、站点 4 个、Telegram 154 个

### 6.1 已收录 · 订阅仓库（36 个）

这些源已经接进采集流水线，`run.py collect` 会自动抓。
**排序依据 = 独有 × 通过率**，也就是「这个源实际能贡献多少个能用的独家节点」。

**产量**是解析出的节点条数。**独有**是这个源独家提供的节点数（别的源都没有），**独有比** = 独有 ÷ 产量。当前 36 个源共提供 **272** 个独家节点。

> **为什么按独有排序而不是按产量**：产量可以被重复内容堆出来。`Danialsamadi/v2go` 有 564 条，独有只有 **34** 个（6%）；而 `iboxz/free-v2ray-collector` 产量 358，独有 **76** 个（21%）。**看独有，别看产量。**

| 源 | 类型 | 产量 | 独有 | 独有比 | 通过率 | 通道 | 频率 | 说明 |
|---|---|---:|---:|---:|---:|---|---|---|
| `zhuhaiuk/free-nodes` | vless,vmess | 17 | 13 | 76% | 60% | 都行 | unknown | discover 自动发现（★379，搜索词「free v2ray nodes」） |
| `iboxz/free-v2ray-collector` | ss,trojan,vless,vmess | 358 | 76 | 21% | 10% | 都行 | 2h | 伊朗系，2小时更新一次，网站带在线测速 |
| `Danialsamadi/v2go` | ss,trojan,vless,vmess | 564 | 34 | 6% | 18% | 都行 | unknown | discover 自动发现（★155，搜索词「v2ray subscription」） |
| `F0rc3Run/F0rc3Run` | ss,trojan,vless,vmess | 403 | 11 | 2% | 31% | 都行 | unknown | discover 自动发现（★321，搜索词「v2ray subscription」） |
| `ovmvo/FreeSub` | ss,trojan,vless,vmess | 47 | 5 | 10% | 58% | 都行 | unknown | discover 自动发现（★348，搜索词「公开节点」） |
| `cbusifabcap/daily_free_vpn` | hysteria2,ss,ssr,trojan,vless,vmess | 238 | 9 | 3% | 22% | 都行 | unknown | discover 自动发现（★368，搜索词「公开节点」） |
| `shaoyouvip/free` | ss,trojan,vless,vmess | 22 | 3 | 13% | 64% | 都行 | unknown | discover 自动发现（★1294，搜索词「公开节点」） |
| `Mahdi0024/ProxyCollector` | ss,vmess | 118 | 2 | 1% | 82% | 都行 | unknown | discover 自动发现（★330，搜索词「v2ray subscription」） |
| `MohammadBahemmat/all_servers` | hysteria2,ss,trojan,tuic,vless,vmess | 407 | 21 | 5% | 7.6% | 都行 | daily | 伊朗系采集器，扫描GitHub+TG，按协议分类输出 |
| `freeclashnode-1` | hysteria2,ss,trojan,vless | 98 | 3 | 3% | 21% | 都行 | daily | 站点源，0/1/2/3 四个文件，URL 规则会变 |
| `0xRadikal/Free-v2ray-Configs` | ss,trojan,vless,vmess | 98 | 4 | 4% | - | 都行 | unknown | discover 自动发现（★772，搜索词「v2ray subscription」） |
| `ShadowsocksAggregator` | ss,trojan,vmess | 201 | 1 | 0% | 42% | 都行 | daily | Eternity.yml，偏 ss/trojan |
| `freeclashnode-2` | ss,ssr,trojan,vless,vmess | 641 | 5 | 0% | 5.8% | 都行 | daily | 站点源，0/1/2/3 四个文件，URL 规则会变 |
| `ermaozi/clash` | hysteria2,ss,trojan,vless,vmess | 356 | 1 | 0% | 25% | 都行 | unknown | 主仓库产物的 clash 格式版 |
| `mfuu/FreeProxies-yaml` | ss,trojan,vmess | 103 | 1 | 0% | 13% | 都行 | 8h | 人工精选，通过率高；yaml 有截断需抢救解析 |
| `MohammadBahemmat/vless` | vless | 226 | 2 | 0% | 5.0% | 都行 | daily | 同上，vless 单独文件 |
| `freeclashnode-0` | ss,trojan,vless,vmess | 434 | 1 | 0% | 0.8% | 都行 | daily | 站点源，0/1/2/3 四个文件，URL 规则会变 |
| `ermaozi/v2ray` | hysteria2,ss,trojan,vless,vmess | 75 | 0 | 0% | 28% | 都行 | unknown | 与 ermaozi/clash 内容相同，只是格式不同 |
| `peasoft/NoMoreWalls-list` | hysteria2,ss,trojan,vless,vmess | 429 | 0 | 0% | 23% | 都行 | daily | 知名仓库，人工维护，质量高；yml 和 txt 两版 |
| `AutoMergePublicNodes-txt` | hysteria2,ss,trojan,vless,vmess | 429 | 0 | 0% | 23% | 都行 | unknown | 数值与 NoMoreWalls 完全一致（422/109），疑为镜像，增量有限 |
| `ts-sf/fly` | vmess | 201 | 0 | 0% | 4.0% | 都行 | daily | 产量中等 |
| `NoMoreWalls-list.yml` | ss,trojan,vmess | 117 | 0 | 0% | 30% | 都行 | unknown | NoMoreWalls 的 yml 版，节点数远少于 txt 版（109 vs 422） |
| `AutoMerge-list.yml` | ss,trojan,vmess | 117 | 0 | 0% | 30% | 都行 | unknown | 同上，与 NoMoreWalls 的 yml 版数值一致 |
| `mfuu/FreeProxies` | ss,trojan,vmess | 105 | 0 | 0% | 13% | 都行 | 8h | 人工精选，通过率高；yaml 有截断需抢救解析 |
| `free-nodes/v2rayfree` | ss,vless | 85 | 0 | 0% | 0.0% | 都行 | daily | 原 aiboboxx/v2rayfree，仓库已改名 |
| `v2rayfree-sub` | ss,vless | 85 | 0 | 0% | 0.0% | 都行 | unknown | aiboboxx 旧地址，会重定向到 free-nodes/v2rayfree，内容相同 |
| `Kwinshadow/TGCollector` | ss,trojan,vless,vmess | 82 | 67 | 81% | 0.0% | 都行 | daily | TG 采集器 |
| `Ruk1ng001/clash` | hysteria2,ss,ssr,trojan,tuic,vless,vmess | 70 | 0 | 0% | 32% | 都行 | unknown | 协议覆盖最全，含 tuic 和 ssr |
| `snakem982/v2ray-2` | hysteria2,ss,trojan,vless,vmess | 56 | 0 | 0% | 59% | 都行 | unknown | 自建 proxypool 产物，v2ray 明文格式 |
| `freeclashnode-3` | vless,vmess | 21 | 0 | 0% | 0.0% | 都行 | daily | 站点源，0/1/2/3 四个文件，URL 规则会变 |
| `awesome-vpn/awesome-vpn` | hysteria2,ss,vless | 41 | 0 | 0% | - | 都行 | unknown | discover 自动发现（★6784，搜索词「free v2ray nodes」） |
| `gslege/CloudflareIP` | vless | 20 | 13 | 65% | 0.0% | 都行 | unknown | discover 自动发现（★674，搜索词「公开节点」） |
| `sinavm/SVM` | ss,trojan,vless,vmess | 108 | 0 | 0% | 8.2% | 都行 | unknown | discover 自动发现（★432，搜索词「v2ray subscription」） |
| `DukeMehdi/FreeList-V2ray-Configs` | ss,trojan | 967 | 0 | 0% | 13% | 都行 | unknown | discover 自动发现（★209，搜索词「کانفیگ رایگان」） |
| `HakurouKen/free-node` | ss,trojan,vmess | 113 | 0 | 0% | 76% | 都行 | unknown | discover 自动发现（★180，搜索词「free v2ray nodes」） |
| `wenxig/free-nodes-sub` | ss,vless | 20 | 0 | 0% | - | 都行 | unknown | discover 自动发现（★107，搜索词「free v2ray nodes」） |

### 6.2 Telegram 频道（154 个，合并展示）

这 154 个频道合计 8,212 条，但**独有节点只有 38 个**。原因是 `Surfboardv2ray/TGParse` 本身就在聚合这些频道，内容高度重合。

> **结论：TG 频道作为「量」的补充价值接近零，它的价值在「新」。**网页版只显示最近约 20 条消息，所以这些源拿到的是几小时内新发的节点，比 GitHub 聚合器（通常日更）快。适合高频轮询做「新鲜度」补充，不适合指望它扩容。

产量最高的 15 个：

| 频道 | 产量 | 独有 |
|---|---:|---:|
| `t.me/atovpn` | 23 | 5 |
| `t.me/daredevill_404` | 19 | 4 |
| `t.me/vpsave` | 202 | 7 |
| `t.me/proxy_online_net` | 124 | 3 |
| `t.me/v2rayls` | 15 | 1 |
| `t.me/webshecan` | 13 | 1 |
| `t.me/maconnectt` | 9 | 1 |
| `t.me/cfox_server` | 8 | 1 |
| `t.me/tootffarangi2` | 272 | 0 |
| `t.me/configv2rayng` | 234 | 0 |
| `t.me/tarkshodeh` | 214 | 0 |
| `t.me/meliproxyy` | 201 | 0 |
| `t.me/Farah_VPN` | 200 | 0 |
| `t.me/v2ray_ng` | 172 | 0 |
| `t.me/v2rayng3` | 172 | 0 |

完整列表见 `sources/registry.yaml`（`kind: telegram`）。

### 6.3 候选池（candidate，10 个）

能出节点但产量低、或还没评估。产量 ≥ 5 且增量比合格就转 active。

| 源 | 类型 | 产量 | 独有 | 独有比 | 通过率 | 通道 | 频率 | 说明 |
|---|---|---:|---:|---:|---:|---|---|---|
| `t.me/outline_vpn` | ss,trojan,vless,vmess | 174 | 0 | 0% | - | 需代理 | unknown | 国内网络直连 t.me 超时，需代理；或改用 Surfboardv2ray/TGParse 这类采集器产物间接覆盖 |
| `t.me/oneclickvpnkeys` | hysteria2,trojan,vless | 96 | 0 | 0% | - | 需代理 | unknown | 国内网络直连 t.me 超时，需代理；或改用 Surfboardv2ray/TGParse 这类采集器产物间接覆盖 |
| `t.me/proxy_mtm` | ss,trojan,vless,vmess | 73 | 0 | 0% | - | 需代理 | unknown | 国内网络直连 t.me 超时，需代理；或改用 Surfboardv2ray/TGParse 这类采集器产物间接覆盖 |
| `ermaozi01/free_clash_vpn` | ss,vless,vmess | 19 | 0 | 0% | - | 都行 | daily | ermaozi 的旧仓库 |
| `ripaojiedian/clash` | ss,vmess | 14 | 0 | 0% | - | 都行 | unknown | ripaojiedian 的 clash 版 |
| `ripaojiedian/freenode` | ss,trojan,vmess | 13 | 0 | 0% | - | 都行 | daily | 产量小但质量稳定 |
| `Pawdroid/Free-servers` | ss,trojan,vless | 12 | 0 | 0% | - | 都行 | 6h | 产量很小（10 条左右），但都是精选 |
| `t.me/vpnfail_vless` | vless,vmess | 12 | 0 | 0% | - | 需代理 | unknown | 国内网络直连 t.me 超时，需代理；或改用 Surfboardv2ray/TGParse 这类采集器产物间接覆盖 |
| `NiREvil/vless` | - | 0 | 0 | - | - | - | unknown | discover 自动发现（★1360，搜索词「v2ray subscription」） ｜ 本次探测为 0 |
| `jichangx/free-nodes` | - | 0 | 0 | - | - | - | unknown | discover 自动发现（★206，搜索词「free v2ray nodes」） ｜ 本次探测为 0 |

### 6.4 已归档（archived，17 个）

**归档 ≠ 删除。** 这些源产量很大但真连通过率极低，已经停止采集，但条目保留在 `sources/registry.yaml` 里——`discover.py` 的已知集合依然会跳过它们，以后不会再重复收录。如果哪天想重新启用，把 `status` 改回 `active` 就行。

| 源 | 产量 | 通过率 | 归档原因 |
|---|---:|---:|---|
| `Leon406/SubCrawler` | 17,910 | 0.4% | 产量 17,910 但真连通过率仅 0.4%（21/5329），典型大合集，占用采集和检测时间 |
| `Epodonios/v2ray-configs` | 7,844 | 3.3% | 产量 7,844 但真连通过率仅 3.5%（41/1185），典型大合集，占用采集和检测时间 |
| `barry-far/V2ray-Config` | 7,823 | 3.5% | 产量 7,823 但真连通过率仅 3.7%（41/1120），典型大合集，占用采集和检测时间 |
| `Surfboardv2ray/TGParse` | 7,310 | 3.3% | 产量 7,310 但真连通过率仅 3.5%（40/1148），典型大合集，占用采集和检测时间 |
| `Barabama/merged.txt` | 7,229 | 2.1% | 产量 7,229 但真连通过率仅 1.9%（18/932），典型大合集，占用采集和检测时间 |
| `mahdibland/V2RayAggregator` | 4,471 | 2.0% | 产量 4,471 但真连通过率仅 1.8%（16/900），典型大合集，占用采集和检测时间 |
| `nyeinkokoaung404/V2ray-Configs` | 4,375 | 2.3% | 产量 4,375 但真连通过率仅 2.2%（16/736），典型大合集，占用采集和检测时间 |
| `MatinGhanbari/v2ray-configs` | 3,985 | 1.5% | 产量 3,985 但真连通过率仅 1.1%（9/783），典型大合集，占用采集和检测时间 |
| `ALIILAPRO/v2rayNG-Config` | 3,841 | 0.9% | 产量 3,841 但真连通过率仅 0.6%（4/721），典型大合集，占用采集和检测时间 |
| `Barabama/merged.yaml` | 3,200 | 1.8% | 产量 3,200 但真连通过率仅 1.6%（20/1218），典型大合集，占用采集和检测时间 |
| `VPN-Subcription-Links/ClashX-V2Ray-TopFreeProxy` | 2,540 | 4.2% | 产量 2,540 但真连通过率仅 3.6%（24/668），典型大合集，占用采集和检测时间 |
| `anaer/Sub` | 1,839 | 0.5% | 产量 1,839 但真连通过率仅 0.0%（0/433），典型大合集，占用采集和检测时间 |
| `MahanKenway/Freedom-V2Ray` | 1,702 | 2.2% | 产量 1,702 但真连通过率仅 1.7%（15/875），典型大合集，占用采集和检测时间 |
| `dongchengjie/airport` | 1,487 | 0.0% | 产量 1,487 但真连通过率仅 0.0%（0/291），典型大合集，占用采集和检测时间 |
| `Alirewa/V2ray-Configs` | 1,296 | 2.9% | 产量 1,296 但真连通过率仅 2.8%（7/251），典型大合集，占用采集和检测时间 |
| `MhdiTaheri/V2rayCollector` | 786 | 6.0% | 产量 786 但真连通过率仅 2.8%（4/141），典型大合集，占用采集和检测时间 |
| `w1770946466/Auto_proxy` | 555 | 0.0% | 产量 555 但真连通过率仅 0.0%（0/81），典型大合集，占用采集和检测时间 |

### 6.5 已失效（dead，27 个）

**加新源之前先看这张表**，别把已经死掉的源反复加回来。

| 源 | 地址 | 失败原因 | 最后探测 |
|---|---|---|---|
| `Flik6/getNode` | `https://raw.githubusercontent.com/Flik6/getNode/main/v2ray.txt` | fetch:empty | 2026-10-28 |
| `Leon406/Sub` | `https://raw.githubusercontent.com/Leon406/Sub/master/sub/share/all` | fetch:HTTP 404 | 2026-10-28 |
| `Ruk1ng001/v2ray` | `https://raw.githubusercontent.com/Ruk1ng001/freeSub/main/v2ray.txt` | fetch:HTTP 404 | 2026-10-28 |
| `a2470982985/getNode` | `https://raw.githubusercontent.com/a2470982985/getNode/main/v2ray.txt` | fetch:empty | 2026-10-28 |
| `aiboboxx/clashfree` | `https://raw.githubusercontent.com/aiboboxx/clashfree/main/clash.yml` | fetch:HTTP 404 | 2026-10-28 |
| `barry-far/V2ray-Configs` | `https://raw.githubusercontent.com/barry-far/V2ray-Configs/main/All_...` | fetch:HTTP 404 | 2026-10-28 |
| `clashnode` | `https://clashnode.xyz/wp-content/uploads/{YYYY}/{MM}/{YYYYMMDD}.yaml` | fetch:HTTP 404 | 2026-10-28 |
| `ermaozi/reachable` | `https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscr...` | fetch:HTTP 404 | 2026-10-28 |
| `freev2` | `https://freev2.net/wp-content/uploads/{YYYY}/{MM}/{YYYYMMDD}.yaml` | fetch:HTTP 526 | 2026-10-28 |
| `mfuu/v2ray` | `https://raw.githubusercontent.com/mfuu/v2ray/master/v2ray` | fetch:HTTP 404 | 2026-10-28 |
| `nodefree` | `https://nodefree.org/dy/{YYYY}/{MM}/{YYYYMMDD}.txt` | fetch:SSLError | 2026-10-28 |
| `oneclash` | `https://oneclash.cc/wp-content/uploads/{YYYY}/{MM}/{YYYYMMDD}.yaml` | fetch:SSLError | 2026-10-28 |
| `snakem982/clash-meta` | `https://raw.githubusercontent.com/snakem982/proxypool/main/source/c...` | fetch:HTTP 404 | 2026-10-28 |
| `snakem982/v2ray-3` | `https://raw.githubusercontent.com/snakem982/proxypool/main/source/v...` | fetch:HTTP 404 | 2026-10-28 |
| `soroushmirzaei/tgc` | `https://raw.githubusercontent.com/soroushmirzaei/telegram-configs-c...` | fetch:HTTP 404 | 2026-10-28 |
| `t.me/FreeVlessVpn` | `https://t.me/s/FreeVlessVpn` | tg-html(0 uris) | 2026-10-28 |
| `t.me/ProxyCollector` | `https://t.me/s/ProxyCollector` | tg-html(0 uris) | 2026-10-28 |
| `t.me/freev2ray_configs` | `https://t.me/s/freev2ray_configs` | tg-html(0 uris) | 2026-10-28 |
| `t.me/jichangbaipiao` | `https://t.me/s/jichangbaipiao` | tg-html(0 uris) | 2026-10-28 |
| `t.me/nodefree` | `https://t.me/s/nodefree` | tg-html(0 uris) | 2026-10-28 |
| `t.me/ripaojiedian` | `https://t.me/s/ripaojiedian` | tg-html(0 uris) | 2026-10-28 |
| `t.me/v2ray_configs_pool` | `https://t.me/s/v2ray_configs_pool` | tg-html(0 uris) | 2026-10-28 |
| `t.me/v2rayngvpn` | `https://t.me/s/v2rayngvpn` | tg-html(0 uris) | 2026-10-28 |
| `tbbatbb/Proxy` | `https://raw.githubusercontent.com/tbbatbb/Proxy/master/dist/v2ray.c...` | fetch:HTTP 404 | 2026-10-28 |
| `v2rayshare` | `https://www.v2rayshare.com/wp-content/uploads/{YYYY}/{MM}/{YYYYMMDD...` | fetch:SSLError | 2026-10-28 |
| `vveg26/get_proxy` | `https://raw.githubusercontent.com/vveg26/get_proxy/main/dist/v2ray....` | fetch:HTTP 404 | 2026-10-28 |
| `yebekhe/TGCollector` | `https://raw.githubusercontent.com/yebekhe/TelegramV2rayCollector/ma...` | fetch:HTTP 404 | 2026-10-28 |

---

*本文档由 `tools/sources_tool.py` 生成。方法论部分来自 `sources/_method.md`，台账部分来自 `sources/registry.yaml`。*