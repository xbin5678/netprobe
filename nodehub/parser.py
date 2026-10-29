"""协议解析：把各种订阅格式统一成 mihomo/clash 的 proxy 字典。"""
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------- 工具


def b64d(s: str) -> str:
    s = (s or "").strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", "ignore")
    except Exception:
        return ""


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _q(query: dict, *keys, default="") -> str:
    for k in keys:
        if k in query and query[k]:
            return query[k][0]
    return default


def _split_uri(uri: str):
    """返回 (head, fragment_name)"""
    frag = ""
    if "#" in uri:
        uri, frag = uri.split("#", 1)
        frag = unquote(frag).strip()
    return uri, frag


# ---------------------------------------------------------------- vmess


def parse_vmess(uri: str):
    body = uri[len("vmess://"):]
    raw = b64d(body)
    if not raw.strip().startswith("{"):
        # 少数 v2rayN 变体：vmess://base64uuid@host:port?...
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None

    server = str(j.get("add", "")).strip()
    port = _int(j.get("port"))
    uuid = str(j.get("id", "")).strip()
    if not server or not port or not uuid:
        return None

    net = str(j.get("net", "tcp") or "tcp").lower()
    header_type = str(j.get("type", "none") or "none").lower()
    tls = str(j.get("tls", "") or "").lower() in ("tls", "reality", "xtls")
    host = str(j.get("host", "") or "")
    path = str(j.get("path", "") or "")
    sni = str(j.get("sni", "") or "") or host

    if net in ("kcp", "quic", "mkcp"):
        return None  # clash 系不支持

    p = {
        "name": str(j.get("ps") or f"{server}:{port}"),
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": uuid,
        "alterId": _int(j.get("aid", 0)),
        "cipher": "auto",
        "udp": True,
        "skip-cert-verify": True,
    }
    if net == "ws":
        p["network"] = "ws"
        opts = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        p["ws-opts"] = opts
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": path or host}
    elif net == "h2" or net == "http":
        p["network"] = "h2" if net == "h2" else "http"
        p["h2-opts" if net == "h2" else "http-opts"] = {"path": [path or "/"], "host": [host] if host else []}
    else:
        p["network"] = "tcp"
        if header_type == "http":
            p["network"] = "http"
            p["http-opts"] = {"path": [path or "/"], "headers": {"Host": [host]} if host else {}}

    if tls:
        p["tls"] = True
        if sni:
            p["servername"] = sni
    alpn = str(j.get("alpn", "") or "")
    if alpn:
        p["alpn"] = [a for a in alpn.split(",") if a]
    fp = str(j.get("fp", "") or "")
    if fp:
        p["client-fingerprint"] = fp
    return p


# ---------------------------------------------------------------- vless


def parse_vless(uri: str):
    body, name = _split_uri(uri)
    u = urlparse(body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    server = u.hostname
    port = _int(u.port)
    uuid = unquote(u.username or "")
    if not port or not uuid:
        return None

    security = _q(q, "security").lower()
    net = _q(q, "type", "network", default="tcp").lower()
    sni = _q(q, "sni", "peer", "servername")
    host = _q(q, "host")
    path = _q(q, "path")
    service = _q(q, "serviceName", "servicename")
    flow = _q(q, "flow")

    p = {
        "name": name or f"{server}:{port}",
        "type": "vless",
        "server": server,
        "port": port,
        "uuid": uuid,
        "udp": True,
        "skip-cert-verify": True,
    }
    if flow:
        p["flow"] = flow

    if net == "ws":
        p["network"] = "ws"
        opts = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        p["ws-opts"] = opts
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": service or path or ""}
    elif net in ("tcp", ""):
        p["network"] = "tcp"
        if host and host != server:
            p["servername"] = sni or host
    elif net == "h2":
        p["network"] = "h2"
        p["h2-opts"] = {"path": path or "/", "host": [host] if host else []}
    elif net in ("http", "xhttp", "splithttp"):
        p["network"] = "http"
        p["http-opts"] = {"path": [path or "/"], "headers": {"Host": [host]} if host else {}}
    else:
        return None

    if security in ("tls", "xtls"):
        p["tls"] = True
        if sni:
            p["servername"] = sni
    elif security == "reality":
        p["tls"] = True
        p["servername"] = sni
        pbk = _q(q, "pbk", "publicKey")
        sid = _q(q, "sid", "shortId")
        if not pbk:
            return None
        p["reality-opts"] = {"public-key": pbk}
        if sid:
            p["reality-opts"]["short-id"] = sid
        p["client-fingerprint"] = _q(q, "fp", default="chrome")

    fp = _q(q, "fp")
    if fp and "client-fingerprint" not in p:
        p["client-fingerprint"] = fp
    alpn = _q(q, "alpn")
    if alpn:
        p["alpn"] = [a for a in unquote(alpn).split(",") if a]
    return p


# ---------------------------------------------------------------- trojan


def parse_trojan(uri: str):
    body, name = _split_uri(uri)
    u = urlparse(body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    server = u.hostname
    port = _int(u.port)
    pwd = unquote(u.username or "")
    if not port or not pwd:
        return None

    net = _q(q, "type", "network", default="tcp").lower()
    sni = _q(q, "sni", "peer", "servername")
    host = _q(q, "host")
    path = _q(q, "path")

    p = {
        "name": name or f"{server}:{port}",
        "type": "trojan",
        "server": server,
        "port": port,
        "password": pwd,
        "udp": True,
        "skip-cert-verify": True,
    }
    if sni:
        p["sni"] = sni
    if net == "ws":
        p["network"] = "ws"
        opts = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        p["ws-opts"] = opts
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": _q(q, "serviceName") or path or ""}
    alpn = _q(q, "alpn")
    if alpn:
        p["alpn"] = [a for a in unquote(alpn).split(",") if a]
    return p


# ---------------------------------------------------------------- ss


def _ss_plugin(plugin_str: str):
    """SIP003 plugin 字符串 -> (clash plugin, opts)"""
    parts = [x for x in plugin_str.split(";") if x]
    if not parts:
        return None, None
    kind = parts[0].strip()
    kv = {}
    for item in parts[1:]:
        if "=" in item:
            k, v = item.split("=", 1)
            kv[k.strip()] = v.strip()
    if kind in ("obfs-local", "simple-obfs", "obfs"):
        opts = {"mode": kv.get("obfs", "http")}
        if "obfs-host" in kv:
            opts["host"] = kv["obfs-host"]
        return "obfs", opts
    if kind == "v2ray-plugin":
        opts = {"mode": kv.get("mode", "websocket"), "tls": "tls" in kv}
        if "host" in kv:
            opts["host"] = kv["host"]
        if "path" in kv:
            opts["path"] = kv["path"]
        return "v2ray-plugin", opts
    return None, None


def parse_ss(uri: str):
    body, name = _split_uri(uri)
    body = body[len("ss://"):]

    method = pwd = server = ""
    port = 0
    plugin = ""

    if "@" in body:
        # SIP002: ss://base64(method:pass)@host:port/?plugin=...
        userinfo, hostpart = body.rsplit("@", 1)
        dec = b64d(userinfo)
        if ":" in dec:
            method, pwd = dec.split(":", 1)
        else:
            method, pwd = unquote(userinfo).split(":", 1) if ":" in unquote(userinfo) else ("", "")
        if "/" in hostpart:
            hostpart, qs = hostpart.split("/", 1)
            qs = qs.lstrip("?")
            plugin = parse_qs(qs).get("plugin", [""])[0]
        if hostpart.startswith("["):
            server, port_s = hostpart.rsplit("]:", 1) if "]:" in hostpart else (hostpart, "0")
            server = server.strip("[]")
            port = _int(port_s)
        else:
            if ":" not in hostpart:
                return None
            server, port_s = hostpart.rsplit(":", 1)
            port = _int(port_s)
    else:
        # 旧格式: ss://base64(method:pass@host:port)
        dec = b64d(body)
        if "@" not in dec:
            return None
        userinfo, hostpart = dec.rsplit("@", 1)
        if ":" not in userinfo:
            return None
        method, pwd = userinfo.split(":", 1)
        if ":" not in hostpart:
            return None
        server, port_s = hostpart.rsplit(":", 1)
        port = _int(port_s)

    if not server or not port or not method:
        return None

    p = {
        "name": name or f"{server}:{port}",
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": pwd,
        "udp": True,
    }
    if plugin:
        kind, opts = _ss_plugin(plugin)
        if kind:
            p["plugin"] = kind
            p["plugin-opts"] = opts
    return p


# ---------------------------------------------------------------- ssr


def parse_ssr(uri: str):
    raw = b64d(uri[len("ssr://"):])
    if "/" not in raw:
        return None
    main, _, qs = raw.partition("/?")
    parts = main.split(":")
    if len(parts) < 6:
        return None
    server, port, proto, method, obfs, pwd_b64 = parts[-6:]
    q = parse_qs(qs)
    try:
        pwd = b64d(pwd_b64)
    except Exception:
        return None
    name = b64d(q.get("remarks", [""])[0]) or f"{server}:{port}"
    p = {
        "name": name,
        "type": "ssr",
        "server": server,
        "port": _int(port),
        "cipher": method,
        "password": pwd,
        "protocol": proto,
        "obfs": obfs,
        "udp": True,
    }
    if q.get("protoparam"):
        p["protocol-param"] = b64d(q["protoparam"][0])
    if q.get("obfsparam"):
        p["obfs-param"] = b64d(q["obfsparam"][0])
    if not p["port"]:
        return None
    return p


# ---------------------------------------------------------------- hysteria2 / tuic


def parse_hysteria2(uri: str):
    body, name = _split_uri(uri)
    u = urlparse(body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    port = _int(u.port)
    if not port:
        return None
    pwd = unquote(u.username or "") or _q(q, "auth", "password")
    p = {
        "name": name or f"{u.hostname}:{port}",
        "type": "hysteria2",
        "server": u.hostname,
        "port": port,
        "password": pwd,
        "skip-cert-verify": True,
        "udp": True,
    }
    sni = _q(q, "sni", "peer")
    if sni:
        p["sni"] = sni
    obfs = _q(q, "obfs")
    if obfs and obfs != "none":
        p["obfs"] = obfs
        p["obfs-password"] = _q(q, "obfs-password", "obfsParam")
    return p


def parse_tuic(uri: str):
    body, name = _split_uri(uri)
    u = urlparse(body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    port = _int(u.port)
    if not port:
        return None
    p = {
        "name": name or f"{u.hostname}:{port}",
        "type": "tuic",
        "server": u.hostname,
        "port": port,
        "uuid": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "skip-cert-verify": True,
        "udp-relay-mode": _q(q, "udp_relay_mode", "udp-relay-mode", default="native"),
        "congestion-controller": _q(q, "congestion_control", "congestion-controller", default="bbr"),
    }
    sni = _q(q, "sni", "peer")
    if sni:
        p["sni"] = sni
    return p


# ---------------------------------------------------------------- 分发

PARSERS = {
    "vmess://": parse_vmess,
    "vless://": parse_vless,
    "trojan://": parse_trojan,
    "ss://": parse_ss,
    "ssr://": parse_ssr,
    "hysteria2://": parse_hysteria2,
    "hy2://": parse_hysteria2,
    "tuic://": parse_tuic,
}

SUPPORTED_TYPES = {"vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "tuic"}


def parse_uri(uri: str):
    uri = uri.strip()
    for prefix, fn in PARSERS.items():
        if uri.lower().startswith(prefix):
            try:
                p = fn(uri)
            except Exception:
                return None
            if p and p.get("server") and p.get("port"):
                p["name"] = (p.get("name") or "").strip() or f"{p['server']}:{p['port']}"
                return p
            return None
    return None


# ---------------------------------------------------------------- clash yaml -> proxy


def normalize_clash_proxy(d: dict):
    """校验并补全一条 clash proxies 条目。"""
    if not isinstance(d, dict):
        return None
    t = str(d.get("type", "")).lower()
    t = {"hy2": "hysteria2", "hysteria-2": "hysteria2", "socks5": "socks5"}.get(t, t)
    if t not in SUPPORTED_TYPES:
        return None
    server = str(d.get("server", "")).strip()
    port = _int(d.get("port"))
    if not server or not port:
        return None
    p = dict(d)
    p["type"] = t
    p["server"] = server
    p["port"] = port
    p.setdefault("name", f"{server}:{port}")
    p["name"] = str(p["name"]).strip() or f"{server}:{port}"
    # mihomo 里这些字段必须存在，否则某些节点会报错
    if t in ("vmess", "vless"):
        p.setdefault("udp", True)
        p.setdefault("cipher", "auto") if t == "vmess" else None
        if not p.get("uuid"):
            return None
    if t in ("ss", "trojan", "hysteria2") and not p.get("password"):
        return None
    p.setdefault("skip-cert-verify", True)
    return p


# ---------------------------------------------------------------- clash proxy -> uri（导出用）


def proxy_to_uri(p: dict) -> str:
    t = p.get("type")
    name = str(p.get("name", ""))
    server, port = p.get("server"), p.get("port")
    if not server or not port:
        return ""
    if t == "vmess":
        j = {
            "v": "2", "ps": name, "add": server, "port": str(port),
            "id": p.get("uuid", ""), "aid": str(p.get("alterId", 0)),
            "scy": p.get("cipher", "auto"), "net": p.get("network", "tcp"),
            "type": "none", "host": "", "path": "", "tls": "tls" if p.get("tls") else "",
            "sni": p.get("servername", ""),
        }
        if p.get("network") == "ws":
            o = p.get("ws-opts", {}) or {}
            j["path"] = o.get("path", "/")
            j["host"] = (o.get("headers", {}) or {}).get("Host", "")
        elif p.get("network") == "grpc":
            j["path"] = (p.get("grpc-opts", {}) or {}).get("grpc-service-name", "")
        return "vmess://" + base64.b64encode(
            json.dumps(j, ensure_ascii=False).encode()
        ).decode()

    if t == "vless":
        from urllib.parse import urlencode
        q = {"encryption": "none", "type": p.get("network", "tcp")}
        if p.get("tls"):
            q["security"] = "reality" if p.get("reality-opts") else "tls"
        else:
            q["security"] = "none"
        if p.get("servername"):
            q["sni"] = p["servername"]
        if p.get("flow"):
            q["flow"] = p["flow"]
        if p.get("client-fingerprint"):
            q["fp"] = p["client-fingerprint"]
        ro = p.get("reality-opts") or {}
        if ro.get("public-key"):
            q["pbk"] = ro["public-key"]
            if ro.get("short-id"):
                q["sid"] = ro["short-id"]
        if p.get("network") == "ws":
            o = p.get("ws-opts", {}) or {}
            q["path"] = o.get("path", "/")
            h = (o.get("headers", {}) or {}).get("Host")
            if h:
                q["host"] = h
        elif p.get("network") == "grpc":
            q["serviceName"] = (p.get("grpc-opts", {}) or {}).get("grpc-service-name", "")
        return f"vless://{p.get('uuid','')}@{server}:{port}?{urlencode(q)}#{name}"

    if t == "trojan":
        from urllib.parse import urlencode
        q = {}
        if p.get("sni"):
            q["sni"] = p["sni"]
        if p.get("network") and p["network"] != "tcp":
            q["type"] = p["network"]
        from urllib.parse import quote
        pwd = quote(str(p.get("password", "")), safe="")
        return f"trojan://{pwd}@{server}:{port}?{urlencode(q)}#{name}"

    if t == "ss":
        userinfo = base64.b64encode(
            f"{p.get('cipher','')}:{p.get('password','')}".encode()
        ).decode().rstrip("=")
        return f"ss://{userinfo}@{server}:{port}#{name}"

    if t == "ssr":
        from urllib.parse import urlencode

        def _e(x: str) -> str:
            # SSR 用 urlsafe base64 且不带 padding
            return base64.urlsafe_b64encode(str(x).encode()).decode().rstrip("=")

        main = ":".join([
            str(server), str(port),
            str(p.get("protocol") or "origin"),
            str(p.get("cipher") or "none"),
            str(p.get("obfs") or "plain"),
            _e(p.get("password", "")),
        ])
        q = {}
        if p.get("obfs-param"):
            q["obfsparam"] = _e(p["obfs-param"])
        if p.get("protocol-param"):
            q["protoparam"] = _e(p["protocol-param"])
        q["remarks"] = _e(name)
        return "ssr://" + _e(main + "/?" + urlencode(q))

    if t == "hysteria2":
        from urllib.parse import urlencode
        q = {}
        if p.get("sni"):
            q["sni"] = p["sni"]
        q["insecure"] = "1"
        # 密码里可能有 :// 这类字符，不转义会把 URI 结构撑坏
        from urllib.parse import quote
        pwd = quote(str(p.get("password", "")), safe="")
        return f"hysteria2://{pwd}@{server}:{port}?{urlencode(q)}#{name}"

    return ""
