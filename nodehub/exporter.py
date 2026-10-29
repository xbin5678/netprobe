"""导出：订阅文件、clash 配置、统计报告。"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import quote


from . import normalize, yamlutil
from .parser import proxy_to_uri
from .store import Store


def _friendly(p: dict, idx: int, latency: int | None) -> str:
    t = p.get("type", "?")
    lat = f"{latency}ms" if latency else "--"
    return f"{t.upper()}-{idx:03d} {lat}"


def build_proxies(nodes: list[dict], name_fn=None) -> list[dict]:
    """name_fn(p, idx, node) 可以自定义节点名；不给就用默认的 TYPE-001 延迟ms。"""
    out = []
    for n in nodes:
        # 导出前再消毒一次：老库里可能还有消毒前入库的脏节点，
        # 而用户客户端加载配置同样是全有或全无，一个坏节点整份订阅都进不去。
        p = normalize.sanitize(json.loads(n["clash"]))
        if not p:
            continue
        if name_fn:
            try:
                p["name"] = str(name_fn(p, len(out) + 1, n))
            except Exception:
                p["name"] = _friendly(p, len(out) + 1, n.get("latency"))
        else:
            p["name"] = _friendly(p, len(out) + 1, n.get("latency"))
        p["_fp"] = n.get("fp", "")
        out.append(p)
    return out


def build_clash_config(nodes: list[dict], name_fn=None) -> dict:
    """把节点列表组装成一份可直接导入的 clash/mihomo 配置。

    界面里的「复制 Clash 配置」和 export_all 都用这个，避免两处逻辑跑偏。
    """
    proxies = build_proxies(nodes, name_fn)
    names = [p["name"] for p in proxies]
    clash_proxies = [{k: v for k, v in p.items() if not k.startswith("_")} for p in proxies]
    # 一个节点都没有时不能留空的 url-test 组，mihomo 会报
    # 「proxy group 没有可用节点」直接拒绝加载。给个只剩 DIRECT 的最小配置，
    # 客户端能正常导入、只是列表是空的，比报错好排查。
    if names:
        groups: list[dict] = [
            {"name": "🚀 节点选择", "type": "select", "proxies": ["♻️ 自动选择", "DIRECT"] + names},
            {
                "name": "♻️ 自动选择",
                "type": "url-test",
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 50,
                "proxies": names,
            },
        ]
    else:
        groups = [{"name": "🚀 节点选择", "type": "select", "proxies": ["DIRECT"]}]
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "proxies": clash_proxies,
        "proxy-groups": groups,
        # 刻意不用 GEOIP 规则：那会让内核去下载 GeoIP 数据库，
        # 在没有代理的环境下会直接卡住启动。局域网直连用 IP-CIDR 就够。
        "rules": [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
            "MATCH,🚀 节点选择",
        ],
    }


def nodes_to_uris(nodes: list[dict], name_fn=None) -> list[str]:
    """节点列表 -> URI 列表。优先用原始 uri，没有就按 clash 字典反推。"""
    out = []
    for n in nodes:
        p = json.loads(n["clash"]) if isinstance(n.get("clash"), str) else (n.get("clash") or {})
        u = n.get("uri") or proxy_to_uri(p)
        if not u:
            continue
        if name_fn:
            try:
                nm = str(name_fn(p, len(out) + 1, n))
                u = u.partition("#")[0] + "#" + quote(nm, safe="")
            except Exception:
                pass
        out.append(u)
    return out


# =====================================================================
# 订阅：一份节点，按不同客户端的能力分别生成
#
# 原则是「宁可少给」：多塞一个客户端不认的节点，很可能整份订阅导入失败，
# 而不是只坏那一个。所以每种格式只导出该客户端确实支持的协议，
# 页面和订阅响应里都会带上「跳过了多少个」。
# =====================================================================

FMT_SUPPORT: dict[str, set[str] | None] = {
    "clash": {"ss", "ssr", "vmess", "vless", "trojan", "hysteria2", "tuic"},
    "v2ray": None,      # base64 订阅，客户端自己解析，全给
    "plain": None,
    "singbox": {"ss", "vmess", "vless", "trojan", "hysteria2", "tuic"},   # sing-box 无 SSR 出站
    "surge": {"ss", "vmess", "trojan", "hysteria2"},                      # Surge 不支持 VLESS / SSR
    "qx": {"ss", "ssr", "vmess", "trojan"},                               # 圈 X 不支持 VLESS / Hy2
}

# 订阅页面的卡片清单，顺序就是页面上的顺序
FORMATS: list[dict] = [
    {
        "id": "clash", "name": "Clash / mihomo", "ext": "yaml",
        "ctype": "text/yaml; charset=utf-8",
        "apps": "Clash Verge Rev · Mihomo Party · FlClash · Stash · ClashX",
        "desc": "完整配置，自带分流规则和「自动选择」测速组，导进去就能用。",
    },
    {
        "id": "v2ray", "name": "v2rayN / 小火箭（通用）", "ext": "txt",
        "ctype": "text/plain; charset=utf-8",
        "apps": "v2rayN · v2rayNG · Shadowrocket · Karing · Hiddify · NekoBox",
        "desc": "base64 订阅，通用性最好，认不出格式的客户端基本都吃这一份。",
    },
    {
        "id": "singbox", "name": "sing-box", "ext": "json",
        "ctype": "application/json; charset=utf-8",
        "apps": "sing-box · Hiddify Next · Karing · SFA / SFI",
        "desc": "sing-box 原生 JSON，含 selector + urltest，跳过 SSR 节点。",
    },
    {
        "id": "surge", "name": "Surge", "ext": "conf",
        "ctype": "text/plain; charset=utf-8",
        "apps": "Surge Mac · Surge iOS",
        "desc": "Surge 不认 VLESS 和 SSR，这两类节点会自动跳过。",
    },
    {
        "id": "qx", "name": "Quantumult X", "ext": "conf",
        "ctype": "text/plain; charset=utf-8",
        "apps": "Quantumult X（圈 X）",
        "desc": "圈 X 不认 VLESS 和 Hysteria2，这两类节点会自动跳过。",
    },
    {
        "id": "plain", "name": "明文链接", "ext": "txt",
        "ctype": "text/plain; charset=utf-8",
        "apps": "任何支持「从剪贴板导入」的客户端",
        "desc": "一行一个节点链接，方便自己挑几个手动加。",
    },
]

# sing-box 能表达的传输层；xhttp / kcp / quic 这些没法转，直接跳过
_SB_NET = {"tcp", "ws", "grpc", "h2", "http"}

# sing-box 的 uTLS 指纹白名单，mihomo 那边偶尔会写别的值
_SB_FP = {"chrome", "firefox", "edge", "safari", "360", "qq", "ios",
          "android", "random", "randomized"}

# 合法的 ALPN。库里混进来过 "v2rayNplus--v2rayNplus--..." 这种垃圾值，
# 原样写进 sing-box 会让 TLS 握手直接失败。
_ALPN_OK = {"h2", "http/1.1", "h3"}


def _clash_of(n: dict) -> dict:
    c = n.get("clash")
    if isinstance(c, str):
        try:
            return json.loads(c)
        except ValueError:
            return {}
    return c or {}


def fmt_nodes(nodes: list[dict], fmt: str) -> tuple[list[dict], int]:
    """按客户端能力筛一遍，返回 (可用节点, 被跳过的数量)。"""
    allow = FMT_SUPPORT.get(fmt)
    out: list[dict] = []
    skipped = 0
    for n in nodes:
        p = _clash_of(n)
        t = (p.get("type") or "").lower()
        if allow is not None and t not in allow:
            skipped += 1
            continue
        if fmt == "singbox" and (p.get("network") or "tcp").lower() not in _SB_NET:
            skipped += 1
            continue
        out.append(n)
    return out, skipped


# ---- sing-box -------------------------------------------------------
def _sb_tls(p: dict) -> dict | None:
    t = (p.get("type") or "").lower()
    need = bool(p.get("tls")) or bool(p.get("reality-opts")) or \
        t in ("trojan", "hysteria2", "tuic")
    if not need:
        return None
    tls: dict = {"enabled": True}
    sni = p.get("servername") or p.get("sni")
    if sni:
        tls["server_name"] = sni
    if p.get("skip-cert-verify"):
        tls["insecure"] = True
    alpn = [a for a in (p.get("alpn") or []) if a in _ALPN_OK]
    if alpn:
        tls["alpn"] = alpn
    fp = str(p.get("client-fingerprint") or "").lower()
    ro = p.get("reality-opts") or {}
    if ro.get("public-key"):
        tls["reality"] = {
            "enabled": True,
            "public_key": ro.get("public-key"),
            "short_id": ro.get("short-id") or "",
        }
        tls["utls"] = {"enabled": True, "fingerprint": fp if fp in _SB_FP else "chrome"}
    elif fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp if fp in _SB_FP else "chrome"}
    return tls


def _sb_transport(p: dict) -> dict | None:
    net = (p.get("network") or "tcp").lower()
    if net == "ws":
        o = p.get("ws-opts") or {}
        tr: dict = {"type": "ws", "path": o.get("path") or "/"}
        hd = {str(k): str(v) for k, v in (o.get("headers") or {}).items()}
        if hd:
            tr["headers"] = hd
        return tr
    if net == "grpc":
        o = p.get("grpc-opts") or {}
        return {"type": "grpc", "service_name": o.get("grpc-service-name") or ""}
    if net in ("h2", "http"):
        o = p.get("h2-opts") or {}
        tr = {"type": "http", "path": o.get("path") or "/"}
        host = o.get("host")
        if host:
            tr["host"] = list(host) if isinstance(host, list) else [str(host)]
        return tr
    return None


# sing-box 的 outbound type 名字跟 clash 不完全一样，最容易踩的就是这个
_SB_TYPE = {"ss": "shadowsocks"}


def build_singbox_config(nodes: list[dict], name_fn=None) -> dict:
    proxies = build_proxies(nodes, name_fn)
    outbounds: list[dict] = []
    tags: list[str] = []
    for p in proxies:
        t = (p.get("type") or "").lower()
        o: dict = {
            "type": _SB_TYPE.get(t, t),
            "tag": p["name"],
            "server": p.get("server"),
            "server_port": int(p.get("port") or 0),
        }
        if t == "vless":
            o["uuid"] = p.get("uuid") or ""
            if p.get("flow"):
                o["flow"] = p["flow"]
        elif t == "vmess":
            o["uuid"] = p.get("uuid") or ""
            o["security"] = p.get("cipher") or "auto"
            o["alter_id"] = int(p.get("alterId") or 0)
        elif t == "trojan":
            o["password"] = p.get("password") or ""
        elif t == "ss":
            o["method"] = p.get("cipher") or "aes-256-gcm"
            o["password"] = p.get("password") or ""
        elif t == "hysteria2":
            o["password"] = p.get("password") or p.get("auth") or ""
        elif t == "tuic":
            o["uuid"] = p.get("uuid") or ""
            o["password"] = p.get("password") or ""
        else:
            continue
        if t != "ss":
            tls = _sb_tls(p)
            if tls:
                o["tls"] = tls
        tr = _sb_transport(p)
        if tr:
            o["transport"] = tr
        if t in ("vless", "vmess") and p.get("packet-encoding"):
            o["packet_encoding"] = p["packet-encoding"]
        outbounds.append(o)
        tags.append(o["tag"])

    groups: list[dict] = []
    if tags:
        groups = [
            {"type": "selector", "tag": "proxy", "outbounds": ["auto", "direct"],
             "default": "auto"},
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": "http://www.gstatic.com/generate_204",
             "interval": "5m", "tolerance": 50},
        ]
    final = "proxy" if tags else "direct"
    return {
        "log": {"level": "info", "timestamp": True},
        # 刻意不写 dns 段：1.12 起旧写法被废弃、1.14 直接移除，写了就得赌用户
        # 装的是哪个版本。不写就用内核默认解析，1.8 到 1.14 都能跑。
        "inbounds": [
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080}
        ],
        "outbounds": groups + outbounds + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [{"ip_is_private": True, "outbound": "direct"}],
            "final": final,
            "auto_detect_interface": True,
        },
    }


# ---- Surge ----------------------------------------------------------
def _surge_line(p: dict) -> str | None:
    t = (p.get("type") or "").lower()
    name = str(p.get("name") or "").replace(",", "-")
    srv, port = p.get("server"), p.get("port")
    net = (p.get("network") or "tcp").lower()
    ws = net == "ws"
    wo = p.get("ws-opts") or {}
    sni = p.get("servername") or p.get("sni") or ""
    if t == "ss":
        parts = [f"encrypt-method={p.get('cipher')}", f"password={p.get('password')}"]
        if p.get("udp"):
            parts.append("udp-relay=true")
        return f"{name} = ss, {srv}, {port}, " + ", ".join(parts)
    if t == "vmess":
        parts = [f"username={p.get('uuid')}", "vmess-aead=true"]
        if ws:
            parts += ["ws=true", f"ws-path={wo.get('path') or '/'}"]
            host = (wo.get("headers") or {}).get("Host")
            if host:
                parts.append(f"ws-headers=Host:{host}")
        if p.get("tls") or sni:
            parts.append("tls=true")
            if sni:
                parts.append(f"sni={sni}")
            if p.get("skip-cert-verify"):
                parts.append("skip-cert-verify=true")
        return f"{name} = vmess, {srv}, {port}, " + ", ".join(parts)
    if t == "trojan":
        parts = [f"password={p.get('password')}"]
        if sni:
            parts.append(f"sni={sni}")
        if p.get("skip-cert-verify"):
            parts.append("skip-cert-verify=true")
        if p.get("udp"):
            parts.append("udp-relay=true")
        return f"{name} = trojan, {srv}, {port}, " + ", ".join(parts)
    if t == "hysteria2":
        parts = [f"password={p.get('password') or p.get('auth')}"]
        if sni:
            parts.append(f"sni={sni}")
        if p.get("skip-cert-verify"):
            parts.append("skip-cert-verify=true")
        return f"{name} = hysteria2, {srv}, {port}, " + ", ".join(parts)
    return None


def build_surge_config(nodes: list[dict], name_fn=None) -> str:
    proxies = build_proxies(nodes, name_fn)
    lines: list[str] = [
        "[General]",
        "loglevel = notify",
        "dns-server = 223.5.5.5, 119.29.29.29",
        "skip-proxy = 127.0.0.1, 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12, "
        "localhost, *.local",
        "bypass-system = true",
        "",
        "[Proxy]",
    ]
    names: list[str] = []
    for p in proxies:
        line = _surge_line(p)
        # 逗号会把 Surge 的字段切碎，宁可丢掉这个节点也不能让整份配置解析失败
        if line and "," not in str(p.get("password") or ""):
            lines.append(line)
            names.append(str(p["name"]).replace(",", "-"))
    lines += ["", "[Proxy Group]"]
    if names:
        lines.append("🚀 节点选择 = select, ♻️ 自动选择, DIRECT, " + ", ".join(names))
        lines.append(
            "♻️ 自动选择 = url-test, " + ", ".join(names)
            + ", url = http://www.gstatic.com/generate_204, interval = 300, tolerance = 50"
        )
    else:
        lines.append("🚀 节点选择 = select, DIRECT")
    lines += [
        "",
        "[Rule]",
        "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
        "FINAL,🚀 节点选择",
        "",
    ]
    return "\n".join(lines)


# ---- Quantumult X ---------------------------------------------------
def _qx_tag(p: dict) -> str:
    """圈 X 的 tag 用逗号分字段，空格也别留，统一成短横线。"""
    t = str(p.get("name") or "node")
    for ch in (" ", ",", "="):
        t = t.replace(ch, "-")
    return t.replace("--", "-")


def _qx_line(p: dict) -> str | None:
    t = (p.get("type") or "").lower()
    tag = _qx_tag(p)
    host, port = p.get("server"), p.get("port")
    net = (p.get("network") or "tcp").lower()
    wo = p.get("ws-opts") or {}
    sni = p.get("servername") or p.get("sni") or ""
    if t == "ss":
        return (f"shadowsocks={host}:{port}, method={p.get('cipher')}, "
                f"password={p.get('password')}, tag={tag}")
    if t == "ssr":
        parts = [f"method={p.get('cipher')}", f"password={p.get('password')}",
                 f"ssr-protocol={p.get('protocol') or 'origin'}"]
        if p.get("protocol-param"):
            parts.append(f"ssr-protocol-param={p['protocol-param']}")
        parts.append(f"obfs={p.get('obfs') or 'plain'}")
        if p.get("obfs-param"):
            parts.append(f"obfs-host={p['obfs-param']}")
        return f"ssr={host}:{port}, " + ", ".join(parts) + f", tag={tag}"
    if t == "vmess":
        # 圈 X 的 vmess 只有 ws / tcp 两种，grpc 之类转不过去
        if net not in ("ws", "tcp"):
            return None
        parts = ["method=chacha20-poly1305", f"password={p.get('uuid')}"]
        if net == "ws":
            tls_on = bool(p.get("tls"))
            parts.append("obfs=wss" if tls_on else "obfs=ws")
            # obfs-host 要用 WebSocket 的 Host 头，没有才退回 SNI / 服务器地址
            wsh = (wo.get("headers") or {}).get("Host")
            parts.append(f"obfs-host={wsh or sni or host}")
            parts.append(f"obfs-uri={wo.get('path') or '/'}")
        if p.get("tls") or sni:
            parts.append("tls-verification=false" if p.get("skip-cert-verify")
                         else "tls-verification=true")
        return f"vmess={host}:{port}, " + ", ".join(parts) + f", tag={tag}"
    if t == "trojan":
        parts = [f"password={p.get('password')}", "over-tls=true"]
        if sni:
            parts.append(f"tls-host={sni}")
        parts.append("tls-verification=false" if p.get("skip-cert-verify")
                     else "tls-verification=true")
        return f"trojan={host}:{port}, " + ", ".join(parts) + f", tag={tag}"
    return None


def build_qx_config(nodes: list[dict], name_fn=None) -> str:
    proxies = build_proxies(nodes, name_fn)
    lines: list[str] = [
        "[general]",
        "server_check_url=http://www.gstatic.com/generate_204",
        "network_check_url=http://www.baidu.com",
        "",
        "[dns]",
        "server=223.5.5.5",
        "server=119.29.29.29",
        "",
        "[policy]",
    ]
    servers: list[str] = []
    for p in proxies:
        line = _qx_line(p)
        if line and "," not in str(p.get("password") or ""):
            servers.append(line)
    lines.append("static=🚀 节点选择, ♻️ 自动选择, direct")
    if servers:
        lines.append("url-latency-benchmark=♻️ 自动选择, server-tag-regex=.*, "
                     "check-interval=300, tolerance=0")
    lines += ["", "[server_local]"] + servers
    lines += [
        "",
        "[filter_local]",
        "ip-cidr, 127.0.0.0/8, direct",
        "ip-cidr, 192.168.0.0/16, direct",
        "ip-cidr, 10.0.0.0/8, direct",
        "ip-cidr, 172.16.0.0/12, direct",
        "final, 🚀 节点选择",
        "",
    ]
    return "\n".join(lines)


# ---- 统一入口 -------------------------------------------------------
def build_sub(fmt: str, nodes: list[dict],
              name_fn=None) -> tuple[str, str, str, int, int]:
    """生成订阅内容。

    返回 (内容, 扩展名, Content-Type, 实际节点数, 跳过数)。
    """
    usable, skipped = fmt_nodes(nodes, fmt)
    meta = next((f for f in FORMATS if f["id"] == fmt), None)
    ext = meta["ext"] if meta else "txt"
    ctype = meta["ctype"] if meta else "text/plain; charset=utf-8"

    if fmt == "clash":
        conf = build_clash_config(usable, name_fn)
        return (yamlutil.dump(conf), ext, ctype, len(usable), skipped)
    if fmt == "singbox":
        return (json.dumps(build_singbox_config(usable, name_fn), ensure_ascii=False,
                           indent=2), ext, ctype, len(usable), skipped)
    if fmt == "surge":
        text = build_surge_config(usable, name_fn)
        return text, ext, ctype, _count_lines(text, " = ", "[Proxy]"), skipped
    if fmt == "qx":
        text = build_qx_config(usable, name_fn)
        return text, ext, ctype, _count_lines(text, "=", "[server_local]"), skipped
    uris = nodes_to_uris(usable, name_fn)
    if fmt == "v2ray":
        return (base64.b64encode("\n".join(uris).encode()).decode(),
                ext, ctype, len(uris), skipped)
    return "\n".join(uris), ext, ctype, len(uris), skipped


def _count_lines(text: str, sep: str, sect: str | None = None) -> int:
    """数一下真正写出去了多少节点（有些节点在拼装时被丢掉了）。"""
    n = 0
    on = sect is None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            on = sect is None or s == sect
            continue
        if on and sep in s and not s.startswith("#"):
            n += 1
    return n


def export_all(store: Store, cfg: dict, root: str) -> dict:
    out_dir = os.path.join(root, cfg.get("dir", "output"))
    os.makedirs(out_dir, exist_ok=True)

    nodes = store.alive_nodes(int(cfg.get("max_latency", 0)))
    top_n = int(cfg.get("top_n", 0) or 0)
    if top_n:
        nodes = nodes[:top_n]

    # ---- 1) clash / mihomo 配置
    conf = build_clash_config(nodes)
    clash_path = os.path.join(out_dir, "alive_clash.yaml")
    yamlutil.dump_to(clash_path, conf)

    # ---- 2) 明文 URI 订阅
    uris = nodes_to_uris(nodes)
    plain_path = os.path.join(out_dir, "alive.txt")
    with open(plain_path, "w", encoding="utf-8") as f:
        f.write("\n".join(uris))

    # ---- 3) base64 订阅（v2rayN / v2rayNG 直接导入）
    sub_path = os.path.join(out_dir, "alive_sub.txt")
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(uris).encode()).decode())

    # ---- 4) 报告
    st = store.stats()
    lines = [
        "# 公开节点采集报告",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 总览",
        "",
        f"- 节点库总量：**{st['total']}**",
        f"- 当前可用：**{st['alive']}**",
        f"- 本次导出：**{len(nodes)}**",
    ]
    if st["min_latency"]:
        lines.append(f"- 最低延迟：{st['min_latency']} ms，平均延迟：{int(st['avg_latency'] or 0)} ms")
    lines += ["", "## 可用节点按协议分布", ""]
    for k, v in (st["alive_by_type"] or {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## 采集源状态", "", "| 源 | 抓到节点 | 状态 | 时间 |", "|---|---|---|---|"]
    for s in st["sources"]:
        lines.append(
            f"| {s['name']} | {s['last_count']} | {s['last_status']} | {s['last_fetch'] or '-'} |"
        )
    lines += ["", "## 最快 20 个节点", "", "| # | 节点 | 类型 | 地址 | 延迟 |", "|---|---|---|---|---|"]
    quick = build_proxies(nodes[:20])
    for i, (n, p) in enumerate(zip(nodes[:20], quick), 1):
        lines.append(
            f"| {i} | {p['name']} | {p.get('type')} | {p.get('server')}:{p.get('port')} | {n.get('latency')} ms |"
        )
    lines += [
        "",
        "## 使用方式",
        "",
        "- `alive_clash.yaml` → Clash Verge / Mihomo Party / FlClash 直接当配置文件导入",
        "- `alive_sub.txt` → v2rayN / v2rayNG / Karing「从剪贴板导入」或作为订阅文件",
        "- `alive.txt` → 明文节点链接，可自行拆分",
        "",
    ]
    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {
        "count": len(nodes),
        "clash": clash_path,
        "sub": sub_path,
        "plain": plain_path,
        "report": report_path,
    }
