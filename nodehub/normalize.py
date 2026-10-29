"""节点消毒：把「乱七八糟」的节点修成 mihomo 一定认的样子。

为什么需要这一层
----------------
mihomo 加载配置是**全有或全无**的：`proxies` 里只要有一个字段不合法
（cipher 拼错、REALITY 的 short-id 不是 hex、flow 写了个废弃值……），
整个配置文件直接加载失败。

而检测是把 200 个节点塞进一个配置里跑真连测试的，所以**一个坏节点
会让那 200 个节点全部被判死**。实测 3.2 万节点的库里 164 批有 24 批
因此整批失败，约 4800 个节点被无辜淘汰。导出的订阅同理——用户导入
clash 配置时只要有一个坏节点，整份订阅都进不去。

所以这里做两件事：
1. 能修的修（server 带方括号、cipher 用了旧名字、h2-opts.path 类型错……）
2. 修不了的丢掉（REALITY 缺公钥、cipher 完全不存在）

调用点有三处，都在「数据即将被 mihomo 看到」之前：
- 入库时（Store.add）
- 真连检测前（tester.real_test）
- 导出时（exporter.build_proxies）
"""
from __future__ import annotations

import base64
import binascii
import re

# mihomo 支持的 shadowsocks 加密方式。
# 注意没有 chacha20-poly1305（那是 vmess 的叫法），ss 里叫 chacha20-ietf-poly1305。
SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-gcm-siv", "aes-256-gcm-siv",
    "rc4-md5", "chacha20", "chacha20-ietf", "xchacha20",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "none", "dummy",
}

# 常见的错写法 -> 正确写法。直接改名比丢节点划算。
CIPHER_ALIAS = {
    "chacha20-poly1305": "chacha20-ietf-poly1305",   # ss 里必须带 ietf
    "xchacha20-poly1305": "xchacha20-ietf-poly1305",
    "aes-256-gcm-siv-v2": "aes-256-gcm-siv",
    "rc4": "rc4-md5",
    "chacha20-ietf-poly1305-2022": "2022-blake3-chacha20-poly1305",
}

VMESS_CIPHERS = {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}

SSR_CIPHERS = SS_CIPHERS | {"rc4", "des-cfb", "bf-cfb", "cast5-cfb",
                            "salsa20", "chacha20-ietf"}
SSR_PROTOCOLS = {"origin", "auth_sha1_v4", "auth_aes128_md5", "auth_aes128_sha1",
                 "auth_chain_a", "auth_chain_b", "auth_chain_c", "auth_chain_d",
                 "auth_chain_e", "auth_chain_f"}
SSR_OBFS = {"plain", "http_simple", "http_post", "random_head",
            "tls1.2_ticket_auth", "tls1.2_ticket_fastauth"}

# mihomo 认的传输层。raw 是 xray 对 tcp 的叫法，映射过来。
#
# httpupgrade 这里**故意保持原样**，别再改回 "http"。原来看名字相近就映射成
# http，其实是两种完全不同的协议（http 是 HTTP 伪装隧道，httpupgrade 是
# WebSocket 的 Upgrade 变体），改过去等于凭空把节点变成另一种东西。
#
# 实测（本地裸 TCP 端口抓 mihomo 拨过来的前 120 字节，内核 v1.19.33）：
#   network=http         -> "GET /sniff HTTP/1.1"          真 HTTP 伪装
#   network=xhttp        -> "PRI * HTTP/2.0 ..."            真 HTTP/2
#   network=httpupgrade  -> \x00\x0b>\x0b...（vless 二进制头）  静默退回裸 tcp
# 也就是说这个内核压根没有 httpupgrade 实现（二进制里搜不到这个字符串），
# 配置能加载、不报错，但实际按 tcp 拨号，节点必然连不上。留着原值的好处是
# 不伪造数据：检测自然淘汰它，将来内核支持了也不用改库。
NET_ALIAS = {
    "raw": "tcp",
    "tcp": "tcp",
    "ws": "ws",
    "grpc": "grpc",
    "h2": "h2",
    "http": "http",
    "h2c": "h2",
    "xhttp": "xhttp",
    "splithttp": "xhttp",
    "httpupgrade": "httpupgrade",
}
# clash 内核压根不支持这些传输，留着只会报错
NET_DROP = {"kcp", "mkcp", "quic", "gun", "meek"}

# xtls 只有 vision 还在维护，origin/direct 早废了，mihomo 直接拒
FLOW_OK = {"", "xtls-rprx-vision"}
FLOW_ALIAS = {"xtls-rprx-vision-udp443": "xtls-rprx-vision"}

ALPN_OK = {"h2", "http/1.1", "h3"}

# uTLS 指纹白名单，跟内核认的那一套一致。库里混进来过 "unsafe"（257 个）、
# "chrom…" / "c..." 这种被截断的垃圾值，写进去内核只会打一行
# `wrong clientFingerprint` 然后忽略，等于没写——不如干脆不写，让它走默认。
FP_OK = {"chrome", "firefox", "safari", "ios", "android", "edge",
         "360", "qq", "random", "randomized"}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_HOST_OK = re.compile(r"^[a-z0-9._\-:]+$")


def _clean_host(v) -> str:
    """服务器地址：去空白、方括号、末尾点，统一小写。"""
    s = str(v or "").strip()
    s = s.strip("[]")                    # [2606:4700::1] -> 2606:4700::1
    s = s.rstrip(".")                    # FQDN 末尾的点会让解析变慢甚至失败
    s = s.strip().lower()
    return s


def _clean_port(v) -> int:
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return p if 1 <= p <= 65535 else 0


def _clean_uuid(v) -> str:
    """uuid：去空白、花括号，统一小写，32 位裸 hex 补上横线（去重靠它）。"""
    s = str(v or "").strip().strip("{}").lower()
    if len(s) == 32 and _HEX_RE.match(s):
        return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"
    return s


def _b64_len(s: str) -> int:
    """按 base64url 解码后的字节数，非法返回 -1。"""
    try:
        t = s.replace("-", "+").replace("_", "/")
        t += "=" * (-len(t) % 4)
        return len(base64.b64decode(t, validate=True))
    except (binascii.Error, ValueError):
        return -1


def _clean_reality(opts: dict) -> dict | None:
    """REALITY 参数。公钥不合法就整条丢掉——没有公钥一定连不上。"""
    if not isinstance(opts, dict):
        return None
    pbk = str(opts.get("public-key") or opts.get("publicKey") or "").strip()
    if not pbk or _b64_len(pbk) != 32:
        return None
    out = {"public-key": pbk}
    sid = str(opts.get("short-id") or opts.get("shortId") or "").strip().lower()
    # short-id 必须是偶数长度的 hex，最长 16；不合法就别写，写错会让整个配置失败
    if sid and len(sid) <= 16 and len(sid) % 2 == 0 and _HEX_RE.match(sid):
        out["short-id"] = sid
    return out


def _as_str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def sanitize(p: dict) -> dict | None:
    """校验 + 修正一条 clash proxy 字典。返回 None 表示这条救不回来。"""
    if not isinstance(p, dict):
        return None
    q = {k: v for k, v in p.items() if v is not None}

    t = str(q.get("type") or "").strip().lower()
    t = {"hy2": "hysteria2", "hysteria-2": "hysteria2"}.get(t, t)
    if t not in ("vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "tuic",
                 "http", "socks5"):
        return None

    server = _clean_host(q.get("server"))
    port = _clean_port(q.get("port"))
    if not server or not port or not _HOST_OK.match(server):
        return None

    out: dict = {"type": t, "server": server, "port": port,
                 "name": str(q.get("name") or f"{server}:{port}").strip()
                         or f"{server}:{port}"}

    # ---- 凭据
    if t in ("vmess", "vless"):
        uuid = _clean_uuid(q.get("uuid"))
        if not uuid:
            return None
        out["uuid"] = uuid
    if t in ("ss", "ssr", "trojan", "hysteria2"):
        pwd = str(q.get("password") or "")
        # 只去首尾空白：密码中间的空格是有效字符，动了就连不上
        pwd = pwd.strip()
        if not pwd:
            return None
        out["password"] = pwd
    if t == "tuic":
        uuid = _clean_uuid(q.get("uuid"))
        pwd = str(q.get("password") or "").strip()
        if not uuid and not pwd:
            return None
        if uuid:
            out["uuid"] = uuid
        if pwd:
            out["password"] = pwd
    if t in ("http", "socks5"):
        u = str(q.get("username") or "").strip()
        pw = str(q.get("password") or "").strip()
        if u:
            out["username"] = u
        if pw:
            out["password"] = pw

    # ---- 加密方式
    if t == "ss":
        c = str(q.get("cipher") or "").strip().lower()
        c = CIPHER_ALIAS.get(c, c)
        if c not in SS_CIPHERS:
            return None                      # 不认识的 cipher 一定跑不起来
        out["cipher"] = c
        if q.get("udp") is not None:
            out["udp"] = bool(q["udp"])
        if q.get("plugin"):
            out["plugin"] = q["plugin"]
            if q.get("plugin-opts"):
                out["plugin-opts"] = q["plugin-opts"]
    elif t == "vmess":
        c = str(q.get("cipher") or "auto").strip().lower()
        out["cipher"] = c if c in VMESS_CIPHERS else "auto"
        try:
            out["alterId"] = max(0, int(q.get("alterId") or 0))
        except (TypeError, ValueError):
            out["alterId"] = 0
    elif t == "ssr":
        c = str(q.get("cipher") or "").strip().lower()
        if c not in SSR_CIPHERS:
            return None
        out["cipher"] = c
        out["protocol"] = str(q.get("protocol") or "origin").strip()
        if out["protocol"] not in SSR_PROTOCOLS:
            return None
        out["obfs"] = str(q.get("obfs") or "plain").strip()
        if out["obfs"] not in SSR_OBFS:
            return None
        if q.get("protocol-param"):
            out["protocol-param"] = str(q["protocol-param"]).strip()
        if q.get("obfs-param"):
            out["obfs-param"] = str(q["obfs-param"]).strip()

    # ---- 传输层
    if t in ("vmess", "vless", "trojan"):
        net = str(q.get("network") or "tcp").strip().lower()
        if net in NET_DROP:
            return None                      # clash 内核不支持，测也测不了
        net = NET_ALIAS.get(net, net)
        if net not in ("tcp", "ws", "grpc", "h2", "http", "xhttp", "httpupgrade"):
            net = "tcp"
        out["network"] = net
        if net == "ws":
            o = q.get("ws-opts") if isinstance(q.get("ws-opts"), dict) else {}
            path = str(o.get("path") or "/").strip() or "/"
            wso: dict = {"path": path}
            hd = o.get("headers")
            if isinstance(hd, dict):
                clean = {str(k): str(v) for k, v in hd.items()
                         if str(k).strip() and str(v).strip()}
                if clean:
                    wso["headers"] = clean
            out["ws-opts"] = wso
        elif net == "grpc":
            o = q.get("grpc-opts") if isinstance(q.get("grpc-opts"), dict) else {}
            out["grpc-opts"] = {
                "grpc-service-name": str(o.get("grpc-service-name") or "").strip()
            }
        elif net == "h2":
            o = q.get("h2-opts") if isinstance(q.get("h2-opts"), dict) else {}
            # 坑：h2-opts.path 必须是字符串，写成数组 mihomo 直接报
            # `'h2-opts.path' expected type 'string'`
            pv = o.get("path")
            path = _as_str_list(pv)
            h2o: dict = {"path": path[0] if path else "/"}
            host = _as_str_list(o.get("host"))
            if host:
                h2o["host"] = host
            out["h2-opts"] = h2o
        elif net == "http":
            o = q.get("http-opts") if isinstance(q.get("http-opts"), dict) else {}
            path = _as_str_list(o.get("path")) or ["/"]
            hto: dict = {"path": path}
            hd = o.get("headers")
            if isinstance(hd, dict):
                clean = {str(k): _as_str_list(v) for k, v in hd.items() if v}
                clean = {k: v for k, v in clean.items() if v}
                if clean:
                    hto["headers"] = clean
            out["http-opts"] = hto
        elif net == "xhttp":
            # xhttp 必须带 opts：实测同一个节点，给 xhttp-opts 内核会正常拨号
            # 并发出 HTTP/2；一个字段都不给，连拨号都不会发起（监听端 10 秒
            # 收不到一个字节）。库里 111 个 xhttp 节点只有 39 个带 opts，
            # 以前这里整段丢掉，等于把这 111 个全部判死。
            # 值一律转成字符串：源里给数组/数字会让整份配置加载失败
            # （内核是全有或全无，一条坏就整批不加载）。
            o = q.get("xhttp-opts") if isinstance(q.get("xhttp-opts"), dict) else {}
            path = (_as_str_list(o.get("path")) or ["/"])[0]
            xo: dict = {"path": path if path.startswith("/") else "/" + path}
            host = str(o.get("host") or "").strip().lower()
            if host and _HOST_OK.match(host):
                xo["host"] = host
            mode = str(o.get("mode") or "").strip().lower()
            if mode in ("auto", "packet-up", "stream-up", "stream-one"):
                xo["mode"] = mode
            hd = o.get("headers")
            if isinstance(hd, dict):
                clean = {str(k): str(v) for k, v in hd.items()
                         if str(k).strip() and str(v).strip()}
                if clean:
                    xo["headers"] = clean
            out["xhttp-opts"] = xo

        # UDP 的封装方式，内核有 `packet-encoding` 这个字段（只认这两个值）。
        # 源里常写成 xudp，以前连它一起丢掉，xhttp/ws 上的 UDP 就走默认，
        # 这类节点测速和实际使用都受影响。
        if t in ("vmess", "vless"):
            pe = str(q.get("packet-encoding") or "").strip().lower()
            if pe in ("packetaddr", "xudp"):
                out["packet-encoding"] = pe

    # ---- TLS
    if t in ("vmess", "vless", "trojan", "hysteria2", "tuic"):
        if q.get("tls") is not None:
            out["tls"] = bool(q["tls"])
        sni = str(q.get("servername") or q.get("sni") or "").strip().rstrip(".").lower()
        if sni:
            # 坑：mihomo 里 SNI 的字段名**按协议不同**，写错不会报错，
            # 只会被静默忽略，然后 TLS 握手不带 SNI —— CDN 前置的节点
            # （server 是 IP、真实域名在 SNI 里）就会整批连不上。
            # 实测（假 TLS 端口抓 ClientHello）：
            #   vmess / vless -> 只认 servername，写 sni 无效
            #   trojan / hysteria2 / tuic -> 只认 sni，写 servername 无效
            if t in ("vmess", "vless"):
                out["servername"] = sni
            else:
                out["sni"] = sni
        if q.get("skip-cert-verify") is not None:
            out["skip-cert-verify"] = bool(q["skip-cert-verify"])
        alpn = [a for a in _as_str_list(q.get("alpn")) if a in ALPN_OK]
        if alpn:
            out["alpn"] = alpn
        fp = str(q.get("client-fingerprint") or "").strip().lower()
        if fp in FP_OK:
            out["client-fingerprint"] = fp

    if t == "vless":
        flow = str(q.get("flow") or "").strip().lower()
        flow = FLOW_ALIAS.get(flow, flow)
        if flow in FLOW_OK and flow:
            out["flow"] = flow
        # flow 不合法就当没有——留着会让整份配置加载失败
        ro = q.get("reality-opts")
        if isinstance(ro, dict) and ro:
            fixed = _clean_reality(ro)
            if fixed:
                out["reality-opts"] = fixed
                out["tls"] = True
            else:
                return None                  # REALITY 参数坏了，这条没救
        if q.get("encryption"):
            out["encryption"] = str(q["encryption"]).strip()

    if t == "hysteria2":
        if q.get("obfs"):
            out["obfs"] = str(q["obfs"]).strip()
            if q.get("obfs-password"):
                out["obfs-password"] = str(q["obfs-password"]).strip()
        for k in ("up", "down"):
            if q.get(k):
                out[k] = str(q[k]).strip()
    if t == "tuic":
        if q.get("congestion-controller"):
            out["congestion-controller"] = str(q["congestion-controller"]).strip()

    if q.get("udp") is not None and "udp" not in out:
        out["udp"] = bool(q["udp"])
    return out


def sanitize_many(items: list[dict]) -> tuple[list[dict], int]:
    """批量消毒，返回 (干净的, 被丢掉的数量)。"""
    out, dropped = [], 0
    for p in items:
        s = sanitize(p)
        if s:
            out.append(s)
        else:
            dropped += 1
    return out, dropped
