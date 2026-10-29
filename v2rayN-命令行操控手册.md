# v2rayN 命令行操控手册

> 面向 AI 的操作说明。命令已在本机（**Windows 11 + v2rayN 7.25.6 + Xray 26.10.21**）逐条验证语法与实际行为；
> 涉及 GUI 启动的步骤按源码流程描述（见附录 B 的来源清单）。
> 目的：让 AI 在不点鼠标的前提下，完成 v2rayN 的启动、停止、开关代理、切换节点、验证连通。

---

## 0. 先记住三件事

**1）v2rayN 没有真正的命令行接口。**
它是个 WPF/Avalonia 图形程序。没有 `v2rayN.exe --start`、没有 `--stop`、没有 `--switch-node`。
传入任何未知参数，它只会**忽略参数并正常启动 GUI**——不会报错，也不会执行你以为的操作。

**2）官方唯一的命令行参数是 `rebootas`（7.x 才有）。**

```bat
v2rayN.exe rebootas
```

作用只有一个：**跳过单实例检查，以新进程身份接管**。v2rayN 用它做「以管理员身份重启」和自更新后的重启。
6.16 及更早的 6.x 版本连这个参数都不解析，`OnStartup` 里根本没读 `e.Args`。

**3）所以「命令行操控」= 绕过 GUI，从系统层下手。**
按可靠性从高到低，有四条路：

| 路线 | 手段 | 能力 | 适用 |
|---|---|---|---|
| A. 进程级 | `Start-Process` / `taskkill` | 启动、停止、判断存活 | 任何版本，首选 |
| B. 注册表级 | `HKCU\...\Internet Settings` | 开关 Windows 系统代理 | 任何版本，独立于 v2rayN |
| C. 配置级 | 改 `guiNConfig.json` | 换节点、换端口、换代理模式、开 TUN | 需先退出 v2rayN |
| D. 直驱核心 | 直接跑 `xray.exe run -c config.json` | 完全可控，不经过 GUI | 想要确定性时 |

> 另有一条「D+」路线：v2rayN 官方还发布了一个 **WebAPI 无头版本**，提供完整 REST 接口（`/api/core/start` 等）。见第 10 节。普通 GUI 版**没有**这个接口。

---

## 1. 事实基线（先对齐坐标）

### 1.1 本机实际情况

| 项 | 值 |
|---|---|
| 安装目录 | `D:\Program Files\v2rayN-windows-64` |
| 主程序 | `v2rayN.exe`（约 102 MB，单文件） |
| 版本 | 7.25.6 |
| 当前核心 | Xray（`bin\xray\xray.exe`，26.10.21） |
| 配置文件 | `guiConfigs\guiNConfig.json` |
| 节点库 | `guiConfigs\guiNDB.db`（SQLite） |
| 本地代理端口 | **10808**（SOCKS + HTTP 混合，见 1.3） |
| 系统代理 | 注册表 `ProxyServer = 127.0.0.1:10808` |
| 当前状态 | 未运行（`ProxyEnable = 0`） |

### 1.2 目录结构

```
v2rayN-windows-64\
├─ v2rayN.exe              主程序（GUI）
├─ AmazTool.exe            升级/重启辅助工具
├─ bin\
│  ├─ xray\xray.exe        核心（本机默认）
│  ├─ sing_box\sing-box.exe
│  └─ mihomo\mihomo.exe
├─ binConfigs\             v2rayN 生成的核心配置（运行时产物，config.json）
├─ guiConfigs\
│  ├─ guiNConfig.json      ★ 主配置（端口、代理模式、当前节点 ID）
│  ├─ guiNDB.db            ★ 节点/订阅数据库
│  └─ pac.txt
├─ guiLogs\                运行日志
└─ guiTemps\               临时文件
```

**数据目录可被重定向**：若环境变量 `V2RAYN_LOCAL_APPLICATION_DATA_V2=1`，配置改到 `%LOCALAPPDATA%\v2rayN`。
AI 不要假设配置一定在 exe 旁边，先按第 2 节的脚本探测。

### 1.3 端口：一个端口，两种协议（重要）

v2rayN 默认只建一个入站，`protocol = socks`，端口 10808。但 **Xray 的 socks 入站在同一端口上同时接受 SOCKS5 和 HTTP 代理请求**。实测日志：

```
accepted tcp:www.baidu.com:80        [socks >> direct]
accepted http://www.baidu.com/       [socks >> direct]
```

这意味着：

- 系统代理指向 `127.0.0.1:10808` 是**正确**的，不是配错；
- 用 curl 测试时，`-x http://127.0.0.1:10808` 和 `-x socks5h://127.0.0.1:10808` **两种写法都通**；
- 不需要额外开一个 10809 的 http 端口。

> 常见误解：网上很多教程说「10808 是 socks、10809 是 http」。那是 v2rayN 5.x 的老配置。7.x 默认只有一个入站。
> 端口基准值 = `guiNConfig.json → Inbound[0].LocalPort`，其余端口由基准值偏移得出（+1 socks2、+3 pac、+4 api、+6 mixed），不要去猜。

---

## 2. 每次动手前：环境探测

AI 每次操作前**必须先跑这一段**，不要假设路径、端口、运行状态。

```powershell
$ErrorActionPreference = 'SilentlyContinue'

# --- 1) 找安装目录 ---
$dir = @(
  'D:\Program Files\v2rayN-windows-64',
  'C:\Program Files\v2rayN-windows-64',
  "$env:USERPROFILE\Desktop\v2rayN-windows-64",
  "$env:USERPROFILE\Downloads\v2rayN-windows-64"
) | Where-Object { Test-Path "$_\v2rayN.exe" } | Select-Object -First 1

if (-not $dir) {
  # 兜底：从开始菜单快捷方式二进制里提取路径
  $lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\v2rayN.lnk"
  if (Test-Path $lnk) {
    $a = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($lnk))
    $m = [regex]::Match($a, '[A-Za-z]:\\[^\x00<>|]{2,120}v2rayN\.exe')
    if ($m.Success) { $dir = Split-Path $m.Value }
  }
}
"安装目录: $dir"

# --- 2) 版本 ---
"版本: " + (Get-Item "$dir\v2rayN.exe").VersionInfo.FileVersion

# --- 3) 是否在运行 ---
$p = Get-Process -Name v2rayN -ErrorAction SilentlyContinue
if ($p) { "状态: 运行中 (PID $($p.Id))" } else { "状态: 未运行" }

# --- 4) 代理端口 ---
$cfgPath = if ($env:V2RAYN_LOCAL_APPLICATION_DATA_V2 -eq '1') { "$env:LOCALAPPDATA\v2rayN\guiConfigs\guiNConfig.json" } else { "$dir\guiConfigs\guiNConfig.json" }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$port = $cfg.Inbound[0].LocalPort
"代理端口: $port"

# --- 5) 端口是否在监听 ---
$l = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($l) { "端口 ${port}: 监听中 (PID $((@($l))[0].OwningProcess))" } else { "端口 ${port}: 未监听" }

# --- 6) 系统代理状态 ---
$reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
"系统代理: ProxyEnable=$($reg.ProxyEnable)  ProxyServer=$($reg.ProxyServer)"

# --- 7) 当前节点 ---
$curId = $cfg.IndexId
"当前节点 IndexId: $curId"
```

**判读规则：**

| 现象 | 含义 |
|---|---|
| 进程在 + 端口监听 + `ProxyEnable=1` | 完全就绪 |
| 进程在 + 端口监听 + `ProxyEnable=0` | 核心在跑，但系统代理关着 |
| 进程在 + 端口不监听 | 核心没起来（节点无效/端口冲突/正在启动），查 `guiLogs` |
| 进程不在 + `ProxyEnable=1` | **危险状态**：上次被强杀，系统代理残留，本机基本断网 |
| 进程不在 + `ProxyEnable=0` | 干净关闭 |

---

## 3. 启动

### 3.1 普通启动（推荐）

```powershell
$dir = 'D:\Program Files\v2rayN-windows-64'
Start-Process -FilePath "$dir\v2rayN.exe" -WorkingDirectory $dir
```

`Start-Process` 立即返回，不阻塞。GUI 窗口会出现（除非配置了 `AutoHideStartup`）。

**cmd 等价写法**（注意 `start` 的第一个引号是窗口标题占位，不能省）：

```bat
start "" "D:\Program Files\v2rayN-windows-64\v2rayN.exe"
```

### 3.2 启动会连带发生什么（必须知道）

v2rayN 启动流程 = 读配置 → 起核心 → **按 `SystemProxyItem.SysProxyType` 自动设置系统代理**。

也就是说：

- 如果上次是「设置系统代理」（`SysProxyType = 1`），**启动即自动接管系统代理**，不需要额外操作；
- 如果节点列表为空或当前节点无效，核心不会启动，端口不会监听，但 GUI 照样打开。

### 3.3 等待就绪（不要用固定 sleep）

```powershell
$dir = 'D:\Program Files\v2rayN-windows-64'
# 端口从配置读，不要写死（用户可能改过）
$port = (Get-Content "$dir\guiConfigs\guiNConfig.json" -Raw | ConvertFrom-Json).Inbound[0].LocalPort
Start-Process -FilePath "$dir\v2rayN.exe" -WorkingDirectory $dir

$deadline = (Get-Date).AddSeconds(30)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        $ready = $true; break
    }
    Start-Sleep -Milliseconds 500
}
if ($ready) { "就绪，端口 $port 已监听" } else { "超时：30 秒内核心未监听 $port" }
```

典型就绪时间 2–5 秒。超过 30 秒基本可以判定失败。

### 3.4 静默启动（不弹窗）

没有命令行开关。只能改配置，让它在启动后自己隐藏到托盘：

```json
// guiNConfig.json → UiItem
"AutoHideStartup": true
```

改完需重启 v2rayN 生效（见第 6 节的「改配置流程」）。

### 3.5 以管理员身份启动

TUN 模式、修改系统级路由才需要。注意 **UAC 会弹窗，无人值守场景会卡住**：

```powershell
Start-Process -FilePath "$dir\v2rayN.exe" -WorkingDirectory $dir -Verb RunAs
```

### 3.6 单实例机制（坑）

v2rayN 用命名 EventWaitHandle 做单实例，key = exe 完整路径的 MD5。

- **第二次启动会静默退出**（退出码 0），并把已运行实例的窗口唤到前台；
- 所以「启动」命令重复执行是安全的、幂等的，不会出现两个实例；
- 但如果 AI 启动后马上判断「进程数 > 1」来确认成功，会误判。用端口监听判断，不要用进程数。

---

## 4. 停止

### 4.1 正确顺序（务必按这个来）

```
① 先清系统代理  →  ② 再杀进程  →  ③ 验证
```

原因：v2rayN 正常退出时会自己清系统代理；但 `taskkill /F` 是**强杀**，跳过退出流程，系统代理会残留，导致本机断网。

### 4.2 一步到位的停止脚本（推荐直接用）

```powershell
$ErrorActionPreference = 'SilentlyContinue'
$dir = 'D:\Program Files\v2rayN-windows-64'          # 换成第 2 节探测到的真实路径
$regKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'

# ① 清系统代理
Set-ItemProperty -Path $regKey -Name ProxyEnable  -Value 0 -Type DWord
Set-ItemProperty -Path $regKey -Name ProxyServer  -Value '' -Type String
Set-ItemProperty -Path $regKey -Name AutoConfigURL -Value '' -Type String

# ② 结束进程（/F 强制；不加 /F 只会发关闭消息，可能只是最小化到托盘）
taskkill /F /IM v2rayN.exe 2>$null | Out-Null
Start-Sleep -Milliseconds 800

# ③ 验证
$p = Get-Process -Name v2rayN -ErrorAction SilentlyContinue
$port = (Get-Content "$dir\guiConfigs\guiNConfig.json" -Raw | ConvertFrom-Json).Inbound[0].LocalPort
$l = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $p -and -not $l) { "已停止，系统代理已清" } else { "未完全停止：进程=$([bool]$p) 端口=$([bool]$l)" }
```

### 4.3 为什么必须 `/F`

`taskkill /IM v2rayN.exe`（不带 `/F`）发的是 `WM_CLOSE`。而 v2rayN 有个选项 `UiItem.Hide2TrayWhenClose`：

- `false`（默认）→ 真的退出；
- `true` → **只是隐藏到托盘，进程继续跑**。

AI 无法预知该配置，所以一律用 `/F`。

### 4.4 强杀不会留下僵尸核心

v2rayN 用 Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）托管核心进程。v2rayN 一死，xray/sing-box 会被系统连带终止。**不需要**再单独 `taskkill /F /IM xray.exe`。

如果发现核心确实残留（异常情况），再补一刀：

```powershell
taskkill /F /IM xray.exe 2>$null
taskkill /F /IM sing-box.exe 2>$null
```

### 4.5 正常退出（会自己清代理）的替代方案

如果想让 v2rayN 走完整退出流程（清代理 + 保存配置 + 停核心），可以先关系统代理再发关闭消息：

```powershell
# 先手动清代理，避免它把代理状态"记住"
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0 -Type DWord
# 再温和关闭；若 3 秒内没退出，补 /F
(Get-Process v2rayN).CloseMainWindow() | Out-Null
Start-Sleep 3
if (Get-Process v2rayN -ErrorAction SilentlyContinue) { taskkill /F /IM v2rayN.exe }
```

---

## 5. 开关 Windows 系统代理（不依赖 v2rayN）

这是「让流量真的走代理」的开关。改的是 WinINET 注册表，**Chrome / Edge / IE / 大部分桌面程序都读这里**。

### 5.1 开启

```powershell
$k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
Set-ItemProperty -Path $k -Name ProxyEnable  -Value 1 -Type DWord
Set-ItemProperty -Path $k -Name ProxyServer  -Value '127.0.0.1:10808' -Type String
Set-ItemProperty -Path $k -Name ProxyOverride -Value '<local>;localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;192.168.*' -Type String
Set-ItemProperty -Path $k -Name AutoConfigURL -Value '' -Type String
```

### 5.2 关闭

```powershell
$k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
Set-ItemProperty -Path $k -Name ProxyEnable  -Value 0 -Type DWord
Set-ItemProperty -Path $k -Name ProxyServer  -Value '' -Type String
Set-ItemProperty -Path $k -Name AutoConfigURL -Value '' -Type String
```

### 5.3 查看

```powershell
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' |
  Select-Object ProxyEnable, ProxyServer, ProxyOverride, AutoConfigURL
```

### 5.4 一个诚实的限制

直接写注册表**不会主动通知系统**。v2rayN 自己是用 `InternetSetOption(SETTINGS_CHANGED / REFRESH)` 刷新的，那需要 P/Invoke 编译一段 C#（`Add-Type`），在部分受限环境会被安全策略拦截。

实际影响：

- **新启动的进程**（新开的浏览器、新跑的 curl）会立即读到新值 —— 通常这就够了；
- **已在运行的程序**可能仍用旧设置，直到它自己重新读取（浏览器一般几十秒内会同步，或重启后生效）。

如果确实需要立刻全局生效，且环境允许 `Add-Type`：

```powershell
Add-Type -Namespace W -Name INet -MemberDefinition @'
[DllImport("wininet.dll", SetLastError=true, CharSet=CharSet.Auto)]
public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
'@
[W.INet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null  # SETTINGS_CHANGED
[W.INet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null  # REFRESH
```

被拦就用不了 —— 那就退回「改注册表 + 让目标程序新起进程」的策略，别硬刚。

---

## 6. 改配置（换节点 / 换端口 / 换代理模式）

### 6.1 铁律：v2rayN 运行时不要改配置文件

v2rayN 在**退出时会保存配置**，运行时也会周期性写回。你在它运行期间改 `guiNConfig.json`，会被它覆盖掉。

正确流程：

```
退出 v2rayN（第 4 节） → 备份 → 改 guiNConfig.json → 启动（第 3 节） → 验证
```

### 6.2 关键字段速查

`guiConfigs\guiNConfig.json` 里 AI 真正会用到的字段：

| 字段 | 类型 | 作用 | 常用值 |
|---|---|---|---|
| `IndexId` | string | **当前使用的节点 ID** | 对应 `guiNDB.db` 的 `ProfileItem.IndexId` |
| `SubIndexId` | string | 当前订阅分组 ID | 可为空 |
| `Inbound[0].LocalPort` | int | 本地代理端口 | 默认 `10808` |
| `Inbound[0].Protocol` | string | 入站协议 | `"socks"`（同时兼容 HTTP） |
| `Inbound[0].AllowLANConn` | bool | 允许局域网连接 | `true` / `false` |
| `SystemProxyItem.SysProxyType` | int | **启动时的代理行为** | 见 6.3 |
| `TunModeItem.EnableTun` | bool | TUN 模式（需管理员） | `false` |
| `UiItem.AutoHideStartup` | bool | 启动后隐藏主窗口 | `true` |
| `UiItem.Hide2TrayWhenClose` | bool | 点关闭时只缩到托盘 | `false` |
| `GuiItem.AutoRun` | bool | 开机自启 | `false` |

### 6.3 `SysProxyType` 取值（源码枚举 `ESysProxyType`）

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | ForcedClear | 强制清除系统代理（不接管） |
| 1 | ForcedChange | **强制设置系统代理**（默认，最常用） |
| 2 | Unchanged | 不改变系统代理（手动管理时用） |
| 3 | Pac | 用 PAC 脚本 |

> 注意：退出时只要不是 `Unchanged`，v2rayN 都会清掉系统代理。想让它「别动我的代理设置」，把值设成 `2`。

### 6.4 切换节点（最常用的操作）

```powershell
$ErrorActionPreference = 'Stop'
$dir = 'D:\Program Files\v2rayN-windows-64'
$cfgPath = "$dir\guiConfigs\guiNConfig.json"
$dbPath  = "$dir\guiConfigs\guiNDB.db"

# 1) 先停
taskkill /F /IM v2rayN.exe 2>$null | Out-Null
Start-Sleep -Milliseconds 800
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0 -Type DWord

# 2) 备份
Copy-Item $cfgPath "$cfgPath.bak" -Force

# 3) 查候选节点（只读 SQLite，见第 7 节）
#    这里假设已经选定 $targetId
$targetId = '5306166478686822931'

# 4) 改 IndexId —— 文本替换，只动这一处，其余字节原样保留
$raw = Get-Content $cfgPath -Raw
$new = $raw -replace '"IndexId"\s*:\s*"[^"]*"', ('"IndexId": "' + $targetId + '"')
# 必须无 BOM 写回（原文件就是无 BOM 的 CRLF）
[IO.File]::WriteAllText($cfgPath, $new, (New-Object Text.UTF8Encoding $false))

# 5) 启动 + 等待就绪（第 3.3 节）
Start-Process -FilePath "$dir\v2rayN.exe" -WorkingDirectory $dir
```

> **别用 `Set-Content -Encoding UTF8`。** PowerShell 5.1 的这个写法会写入 UTF-8 BOM（`EF BB BF`），
> 而 v2rayN 的配置文件原本是无 BOM 的 CRLF。BOM 有可能让它的 JSON 解析报「加载 GUI 配置文件异常」。
> 上面用 `WriteAllText` + `UTF8Encoding($false)` 写回，实测首字节仍是 `{`、换行仍是 CRLF。

> **为什么用文本替换而不是 `ConvertFrom-Json` → `ConvertTo-Json`？**
> 实测后者能保住所有字段（数组仍是数组、`null` 仍是 `null`），但会把 8455 字节的配置重写成 18405 字节，缩进和对齐全变。功能没问题，只是文件脏了、diff 难看。
> 文本替换实测只改动 1 处（`"IndexId"` 在整个文件里只出现一次），长度 8455 → 8447，其余字节零变化。
> 如果目标 ID 里有正则特殊字符，先 `[regex]::Escape($targetId)`。

### 6.5 换端口

改 `Inbound[0].LocalPort`（如 10808 → 20808）。改完记得同步系统代理的 `ProxyServer` 值，否则代理指向空气。

### 6.6 开 TUN

```json
"TunModeItem": { "EnableTun": true, ... }
```

前置条件：**必须以管理员运行**，且 TUN 虚拟网卡驱动已安装。AI 无人值守时不要贸然开 TUN —— UAC 会卡住。

---

## 7. 节点库（guiNDB.db）只读查询

SQLite 库，v2rayN 运行中会被锁定/覆盖。**默认只读，不要写。**

### 7.1 表结构（节选）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `ProfileItem` | 节点 | `IndexId`、`Remarks`、`Address`、`Port`、`ConfigType`、`Subid` |
| `ProfileExItem` | 节点测速结果 | `IndexId`、`Delay`、`Speed`、`IpInfo` |
| `SubItem` | 订阅 | `Id`、`Remarks`、`Url`、`Enabled`、`UpdateTime` |
| `RoutingItem` | 路由方案 | `Id`、`Remarks`、`IsActive` |
| `ServerStatItem` | 流量统计 | `IndexId`、`TotalUp`、`TotalDown` |

`ProfileItem.ConfigType` 是 `EConfigType` 枚举的数值（7.25.6 实测）：

| 值 | 协议 | 值 | 协议 |
|---|---|---|---|
| 1 | VMess | 8 | TUIC |
| 2 | Custom | 9 | WireGuard |
| 3 | Shadowsocks | 10 | HTTP |
| 4 | SOCKS | 11 | Anytls |
| 5 | VLESS | 12 | Naive |
| 6 | Trojan | 13 | Outbound |
| 7 | Hysteria2 | 14 | MASQUE |

> 101 = 策略组，102 = 代理链。这套数值随版本变过（比如 7.x 新增了 MASQUE），
> 跨版本时不要硬编码判断协议 —— 判断「能不能用」看 `ProfileExItem.Delay`，比看协议名可靠。

### 7.2 查当前用的是哪个节点

```powershell
$dir = 'D:\Program Files\v2rayN-windows-64'
$cfg = Get-Content "$dir\guiConfigs\guiNConfig.json" -Raw | ConvertFrom-Json
$cfg.IndexId
```

再用 Python 读库（PowerShell 没有内置 SQLite）：

```python
import sqlite3, json
db = r"D:\Program Files\v2rayN-windows-64\guiConfigs\guiNDB.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)   # 只读打开，防止误写
cur = con.cursor()

# 当前节点
cur.execute("""SELECT p.IndexId, p.Remarks, p.Address, p.Port, e.Delay
               FROM ProfileItem p LEFT JOIN ProfileExItem e ON p.IndexId = e.IndexId
               WHERE p.IndexId = ?""", ("5306166478686822931",))
print(cur.fetchone())

# 延迟最低的 10 个
for row in cur.execute("""SELECT p.IndexId, p.Remarks, p.Address, e.Delay
                          FROM ProfileItem p JOIN ProfileExItem e ON p.IndexId = e.IndexId
                          WHERE e.Delay > 0 ORDER BY e.Delay LIMIT 10"""):
    print(row)
```

### 7.3 写库的风险

- v2rayN 启动时会把内存状态写回，**运行中写库会被覆盖**；
- 直接改 `ProfileItem` 而不改 `ProfileExItem`，界面会出现「节点存在但无测速数据」的错位；
- 节点字段有几十个（`StreamSecurity`/`Flow`/`PublicKey`/`ShortId`…），手写极易漏字段导致节点不可用。

**结论：加节点、删节点、改节点，交给 v2rayN 自己做（GUI 或 WebAPI）。AI 只读库。**

---

## 8. 验证代理是否真的能用

不要用「端口在监听」当成功标准 —— 那只能说明核心起来了，不代表能出网。

```bash
# 通过代理访问（返回 204 即成功）
curl -x http://127.0.0.1:10808 --max-time 8 -sS -o /dev/null -w "%{http_code}\n" https://www.gstatic.com/generate_204

# 等价写法（socks5h 让代理解析 DNS，更贴近真实行为）
curl -x socks5h://127.0.0.1:10808 --max-time 8 -sS -o /dev/null -w "%{http_code}\n" https://www.gstatic.com/generate_204

# 看真实出口 IP（确认流量确实走了代理）
curl -x http://127.0.0.1:10808 --max-time 10 -s https://api.ipify.org
```

### 退出码对照（实测）

| curl 退出码 | HTTP | 含义 | 处置 |
|---|---|---|---|
| 0 | 204 / 200 | 代理可用 | 成功 |
| 7 | 000 | 连不上代理端口：核心没起 / 端口错 / 端口被占 | 查进程、查端口 |
| 28 | 000 | 超时：节点活着但走不通（被墙/节点失效） | 换节点 |
| 52 / 56 | 000 | 代理接受了连接但没回数据：协议不匹配或节点挂了 | 换节点 |
| 35 | — | TLS 握手失败 | 节点证书/SNI 问题，换节点 |

判断「是代理坏了还是网络坏了」，跑一次直连对比：

```bash
curl --noproxy '*' --max-time 8 -sS -o /dev/null -w "%{http_code}\n" https://www.gstatic.com/generate_204
```

---

## 9. 绕开 GUI，直接驱动核心

想要**确定性**（AI 不想依赖 GUI 的隐式行为）时用这条路。

### 9.1 用 v2rayN 生成的配置直接跑

v2rayN 每次启动核心时会把最终配置写到 `binConfigs\config.json`。可以直接复用：

```powershell
$dir = 'D:\Program Files\v2rayN-windows-64'
Start-Process -FilePath "$dir\bin\xray\xray.exe" `
  -ArgumentList 'run','-c',"$dir\binConfigs\config.json" `
  -WorkingDirectory "$dir\binConfigs" -WindowStyle Hidden
```

Xray 的启动参数就是 `run -c <配置文件>`；sing-box 是 `run -c <配置文件> --disable-color`。

> 前提：`binConfigs\config.json` 得先存在。它只在 v2rayN 起过核心之后才有。如果目录是空的，先按第 3 节启动一次 v2rayN。

### 9.2 自己写最小配置（完全自主）

```json
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "in-socks",
      "port": 10808,
      "listen": "127.0.0.1",
      "protocol": "socks",
      "settings": { "auth": "noauth", "udp": true }
    }
  ],
  "outbounds": [
    {
      "tag": "proxy",
      "protocol": "vless",
      "settings": {
        "vnext": [{
          "address": "你的服务器地址",
          "port": 443,
          "users": [{ "id": "你的UUID", "encryption": "none", "flow": "" }]
        }]
      },
      "streamSettings": { "network": "tcp", "security": "tls", "tlsSettings": { "serverName": "你的域名" } }
    }
  ]
}
```

启动：

```powershell
& "D:\Program Files\v2rayN-windows-64\bin\xray\xray.exe" run -c "C:\path\to\my.json"
```

### 9.3 注意事项

- **端口冲突**：直驱核心时，v2rayN 必须没在跑，否则 10808 被占，核心启动即退出；
- **系统代理要自己设**：直驱核心不会动注册表，得用第 5 节的命令；
- **退出要自己管**：直接 kill xray 进程即可（`taskkill /F /IM xray.exe`）；
- 这条路完全绕开 v2rayN 的节点库、路由规则、订阅，适合「只要一个能用的代理通道」的场景。

---

## 10. 进阶：v2rayN WebAPI（真正的 REST 接口）

**注意：普通 GUI 版没有这个能力。** 需要单独下载 `v2rayN-windows-64-WebAPI.zip`（官方 Release 里的独立包），它是无头 ASP.NET Core 服务，跑起来不弹 GUI。

### 10.1 启动

```powershell
cd "C:\path\to\v2rayN-windows-64-WebAPI"
.\v2rayN.WebAPI.exe --foreground --no-open
```

默认监听 `http://127.0.0.1:5080`。

启动参数：

| 参数 | 作用 |
|---|---|
| `--foreground` | 前台运行，Ctrl+C 停止 |
| `--stop` | 停止同数据目录下正在运行的实例 |
| `--no-open` | 不自动打开浏览器 |
| `--help` | 打印全部选项 |

### 10.2 认证（两步）

```bash
# 1) 用 Management Key 换 session token
curl -s -X POST http://127.0.0.1:5080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"key":"你的V2RAYN_WEB_API_KEY"}'
# → {"success":true,"data":{"token":"...","expiresAt":"..."}}

# 2) 后续请求带 Bearer
curl -s http://127.0.0.1:5080/api/status -H "Authorization: Bearer <token>"
```

Management Key 来自 `.env` 里的 `V2RAYN_WEB_API_KEY`。纯本机回环部署时可以留空走首次初始化。

### 10.3 核心端点（AI 最常用的部分）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查（无需认证） |
| GET | `/api/status` | 运行状态、版本、能力 |
| POST | `/api/core/start` | **启动核心** |
| POST | `/api/core/stop` | **停止核心** |
| POST | `/api/core/restart` | **重启核心** |
| GET | `/api/profiles` | 节点列表（支持 `?filter=`） |
| POST | `/api/profiles/{id}/select` | **切换节点** |
| POST | `/api/profiles/{id}/latency` | 测单节点延迟 |
| POST | `/api/profiles/import` | 导入节点 |
| POST | `/api/subscriptions/update` | 更新订阅 |
| GET | `/api/settings` | 读设置 |
| PUT | `/api/settings/inbound` | 改本地端口 |
| PUT | `/api/settings/application` | 改应用设置（含系统代理） |
| GET | `/api/logs?limit=200` | 读日志 |
| GET | `/api/events` | SSE 事件流（需 sse-ticket） |

### 10.4 用它 vs 用 GUI 版

| | GUI 版（进程+注册表+配置） | WebAPI 版 |
|---|---|---|
| 启动/停止 | 杀进程 | `POST /api/core/stop`，干净 |
| 切节点 | 改 json + 重启整个程序 | 一次 POST，核心热重载 |
| 系统代理 | 自己写注册表 | 走设置接口 |
| 状态查询 | 猜（端口/进程） | `GET /api/status` 精确 |
| 依赖 | 无 | 需换安装包 + 管理 Key |

**如果用户的机器上装的是普通 GUI 版，不要试图调这些接口 —— 5080 端口根本不会开。**

---

## 11. 故障排查

> **本机突然上不了网？先跑这一条急救**（清代理 + 杀进程，见 4.2 节）：
> `Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0 -Type DWord; taskkill /F /IM v2rayN.exe`
> 九成的「装完代理后全网打不开」都是系统代理指向了一个已经死掉的端口。

| 现象 | 原因 | 处置 |
|---|---|---|
| 浏览器全打不开，但 `ping baidu.com` 通 | 系统代理残留，指向已死的 10808 | 清 `ProxyEnable=0`（5.2 节） |
| 启动后端口一直不监听 | 当前节点无效 / 端口被占 / 核心文件缺失 | 看 `guiLogs\*.log`；`netstat -ano \| findstr 10808` 查占用 |
| 端口监听但 curl 退出码 28 | 节点失效或超时 | 换节点 |
| 双击没反应 / 一闪而过 | 已有实例在跑（单实例机制） | 查进程，或直接看托盘 |
| 提示「加载 GUI 配置文件异常」 | `guiNConfig.json` 被改坏（JSON 语法错） | 还原 `.bak` |
| 改了配置重启后又被改回去 | v2rayN 运行时写入覆盖 | 先退出再改 |
| 系统代理开了但 Chrome 不走代理 | Chrome 用了自己的代理设置 / 扩展 | 检查 Chrome 设置，或改注册表后重启 Chrome |
| 需要管理员但没权限 | TUN 模式、系统级路由 | `-Verb RunAs` 启动（会弹 UAC） |
| 日志乱码 | Windows 控制台编码 | `chcp 65001` 或改用 PowerShell 原生命令 |

日志位置：`guiLogs\` 目录下按日期命名的 `.txt`。启动失败时先看这里，比猜快。

---

## 12. 给 AI 的操作守则

**做：**

1. **先探测再动手**。每次都跑第 2 节的脚本，不要复用上一次的路径/端口。
2. **幂等**。启动前查是否已在运行；停止前查是否已停。
3. **改配置前先备份** `Copy-Item guiNConfig.json guiNConfig.json.bak`。
4. **改配置前先退出** v2rayN，改完再启动。
5. **停止时先清代理再杀进程**，顺序不能反。
6. **用端口监听 + curl 204 双重验证**，不要只看进程存在。
7. **报告要说清三件事**：当前状态（进程/端口/系统代理）、执行了什么、验证结果。

**不做：**

1. ❌ 不要给 `v2rayN.exe` 传 `--start` / `--stop` / `--proxy` 之类的参数 —— 它不认，只会打开 GUI。
2. ❌ 不要假设端口一定是 10808 —— 读 `Inbound[0].LocalPort`。
3. ❌ 不要假设安装路径 —— 先探测。
4. ❌ 不要写 `guiNDB.db`。
5. ❌ 不要在 v2rayN 运行时改 `guiNConfig.json`。
6. ❌ 不要用 `taskkill` 不带 `/F` 来「确保退出」—— 可能只是缩到托盘。
7. ❌ 不要无人值守时用 `-Verb RunAs` 或开 TUN —— UAC 会挂起。
8. ❌ 不要在没有可用节点的情况下反复重启，先确认 `ProfileItem` 表非空。

---

## 附录 A：速查表

```powershell
# 路径
$dir = 'D:\Program Files\v2rayN-windows-64'
$exe = "$dir\v2rayN.exe"
$cfg = "$dir\guiConfigs\guiNConfig.json"

# 状态
Get-Process v2rayN -ErrorAction SilentlyContinue                    # 是否运行
Get-NetTCPConnection -LocalPort 10808 -State Listen                 # 端口是否监听（10808 是默认值，实际读 Inbound[0].LocalPort）
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' | Select ProxyEnable,ProxyServer

# 启动 / 停止
Start-Process $exe -WorkingDirectory $dir                            # 启动
taskkill /F /IM v2rayN.exe                                           # 强杀
Start-Process $exe -WorkingDirectory $dir -Verb RunAs                # 管理员启动
& $exe rebootas                                                      # ⚠ 仅在旧实例已退出时用；运行中执行会产生第二个实例

# 代理开关
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 1 -Type DWord   # 开
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0 -Type DWord   # 关

# 读配置
(Get-Content $cfg -Raw | ConvertFrom-Json).IndexId                   # 当前节点
(Get-Content $cfg -Raw | ConvertFrom-Json).Inbound[0].LocalPort      # 代理端口
(Get-Content $cfg -Raw | ConvertFrom-Json).SystemProxyItem.SysProxyType

# 验证
curl -x http://127.0.0.1:10808 --max-time 8 -o NUL -s -w "%{http_code}" https://www.gstatic.com/generate_204
curl -x http://127.0.0.1:10808 --max-time 10 -s https://api.ipify.org   # 出口 IP
```

| 命令 | 一句话 |
|---|---|
| `v2rayN.exe` | 启动 GUI（唯一的启动方式） |
| `v2rayN.exe rebootas` | 跳过单实例检查重启（7.x 唯一参数） |
| `AmazTool.exe rebootas` | 升级辅助工具的重启命令 |
| `AmazTool.exe help` | 打印升级工具帮助 |
| `taskkill /F /IM v2rayN.exe` | 强制结束（推荐） |
| `Start-Process ... -Verb RunAs` | 管理员启动（弹 UAC） |

---

## 附录 B：事实来源

| 结论 | 依据 |
|---|---|
| 唯一参数是 `rebootas` | 源码 `Global.RebootAs = "rebootas"`，`App.xaml.cs` 里 `e.Args.Any(t => t == Global.RebootAs)` |
| 6.x 不解析任何参数 | `6.16` 版 `App.xaml.cs` 的 `OnStartup` 完全没读 `e.Args` |
| 单实例按 exe 路径 MD5 | `Utils.GetMd5(Utils.GetExePath())` + `EventWaitHandle` |
| 强杀不留核心 | `WindowsJobService` 使用 `LimitFlags = 0x2000`（KILL_ON_JOB_CLOSE） |
| 退出会清系统代理 | `AppManager.AppExitAsync` → `UpdateSysProxy(config, true)` |
| 10808 同时支持 HTTP | 本机 xray 实测，日志 `accepted http://www.baidu.com/ [socks >> direct]` |
| 配置路径 | `Utils.StartupPath()` + `guiConfigs`；可用 `V2RAYN_LOCAL_APPLICATION_DATA_V2=1` 重定向 |
| 端口偏移规则 | `AppManager.GetLocalPort()` 返回 `基准端口 + (int)协议枚举` |
| WebAPI 端点 | `v2rayN.WebAPI/Api/WebApiEndpoints.cs`、`WebAuthEndpoints.cs` |

---

*文档基于 v2rayN 7.25.6 / Xray 26.10.21 / Windows 11 编写。版本升级后行为可能有变，执行前先用第 2 节脚本复核。*
