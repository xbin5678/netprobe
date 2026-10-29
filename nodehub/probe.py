"""订阅链接测速：拉订阅 → 解析节点 → 测可用性/延迟/下载速度 → 存历史。

和主流水线完全独立：
- 数据存在 `data/probe.db`，一个字节都不碰 `nodes.db`
- 有自己的任务锁 `data/probe.lock`，可以和定时任务同时跑
- 订阅地址是用户随手贴的，所以拉取前要做 SSRF 防护

支持的订阅格式（自动识别，不用用户选）：
clash/mihomo YAML、sing-box JSON、base64（v2rayN 那种）、明文 URI 列表、
Surge INI、Quantumult X INI。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
import yaml

from . import normalize, parser, tester, yamlutil
from .store import fingerprint, now

# 订阅文件一般几百 KB，给到 8MB 已经很宽松了
SUB_MAX = 8 << 20
UA = "clash-verge/v2.0.6"

# 测速用的测试文件。Cloudflare 的 __down 可以按字节数定制，最省事。
SPEED_URLS = {
    10: ["https://speed.cloudflare.com/__down?bytes=10000000",
         "https://cachefly.cachefly.net/10mb.test"],
    20: ["https://speed.cloudflare.com/__down?bytes=20000000"],
    50: ["https://speed.cloudflare.com/__down?bytes=50000000"],
}
SPEED_SIZES = (10, 20, 50)
# 测速至少要拿到这么多字节才算数：低于这个数的多半是连接「响了一下就断」，
# 算出来的速度没有意义，不能让它混进「有下载速度」那一档
MIN_BYTES = 128 << 10


class ProbeError(Exception):
    """给用户看的错误，直接显示在页面上。"""


# ---------------------------------------------------------------- 拉订阅

_BAD_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10",
)]


def _check_url(url: str) -> str:
    """只允许公网 http(s)，挡住拿订阅接口去打内网的玩法。"""
    url = (url or "").strip()
    if not url:
        raise ProbeError("订阅地址不能为空")
    if not re.match(r"^https?://", url, re.I):
        raise ProbeError("只支持 http:// 或 https:// 开头的订阅地址")
    host = (urlsplit(url).hostname or "").strip()
    if not host:
        raise ProbeError("订阅地址里没有主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise ProbeError(f"域名解析失败：{host}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        for net in _BAD_NETS:
            if ip in net:
                raise ProbeError(f"不允许访问内网地址：{ip}")
    return url


def fetch_sub(url: str, timeout: int = 25) -> tuple[str, str]:
    """返回 (正文, 最终地址)。

    跳转自己跟，不用 requests 的 allow_redirects：_check_url 只校验了第一次
    的地址，如果让它自动跟随，一个公网域名 302 到 127.0.0.1 / 169.254.169.254
    就绕过了整套内网拦截。每一跳都重新校验才安全。
    """
    url = _check_url(url)
    headers = {
        # 不带 UA 的话，不少机场会返回一个空订阅或者 403
        "User-Agent": UA,
        "Accept": "*/*",
    }
    for _hop in range(4):
        try:
            with requests.get(url, headers=headers, timeout=timeout,
                              stream=True, allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = (r.headers.get("Location") or "").strip()
                    if not loc:
                        raise ProbeError(f"订阅返回 {r.status_code} 但没给跳转地址")
                    url = _check_url(urljoin(url, loc))
                    continue
                if r.status_code != 200:
                    raise ProbeError(f"订阅返回 HTTP {r.status_code}")
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > SUB_MAX:
                        raise ProbeError(f"订阅内容超过 {SUB_MAX >> 20}MB，不像订阅文件")
                return bytes(buf).decode("utf-8", "replace"), url
        except ProbeError:
            raise
        except requests.exceptions.SSLError:
            raise ProbeError("订阅站点证书有问题（自签或过期）")
        except requests.exceptions.Timeout:
            raise ProbeError("拉取订阅超时，检查地址或网络")
        except requests.exceptions.ConnectionError:
            raise ProbeError("连不上订阅地址，可能被墙或域名写错")
        except Exception as e:
            raise ProbeError(f"拉取失败：{type(e).__name__}")
    raise ProbeError("订阅跳转次数太多（超过 4 次），检查地址")


# ---------------------------------------------------------------- 解析

def _b64_try(text: str) -> str | None:
    """尽量把 base64 解出来，不像 base64 就返回 None。"""
    s = re.sub(r"\s+", "", text)
    if len(s) < 32 or not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", s):
        return None
    s = s.replace("-", "+").replace("_", "/").rstrip("=")
    s += "=" * (-len(s) % 4)
    try:
        out = base64.b64decode(s, validate=False).decode("utf-8", "strict")
    except Exception:
        return None
    return out if "://" in out or "proxies:" in out else None


def _from_uris(text: str) -> tuple[list, int]:
    nodes, bad = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            continue
        p = parser.parse_uri(line)
        if not p:
            bad += 1
            continue
        p = normalize.sanitize(p)
        if not p:
            bad += 1
            continue
        p["_uri"] = line
        nodes.append(p)
    return nodes, bad


def _from_clash(doc: dict) -> tuple[list, int]:
    raw = doc.get("proxies")
    if not isinstance(raw, list):
        if doc.get("proxy-providers"):
            raise ProbeError("这是只含 proxy-providers 的配置，节点在别的文件里，换一份订阅")
        raise ProbeError("clash 配置里没有 proxies 段")
    nodes, bad = [], 0
    for d in raw:
        p = parser.normalize_clash_proxy(d)
        if not p:
            bad += 1
            continue
        p = normalize.sanitize(p)
        if not p:
            bad += 1
            continue
        nodes.append(p)
    return nodes, bad


_SB_TYPES = {"vmess", "vless", "trojan", "shadowsocks", "hysteria2", "tuic", "ss"}


def _from_singbox(doc: dict) -> tuple[list, int]:
    outs = doc.get("outbounds")
    if not isinstance(outs, list):
        raise ProbeError("sing-box 配置里没有 outbounds 段")
    nodes, bad = [], 0
    for o in outs:
        if not isinstance(o, dict):
            bad += 1
            continue
        t = str(o.get("type", "")).lower()
        if t not in _SB_TYPES:
            continue                      # direct/block/dns 这些不是节点，不算失败
        p = _sb_to_clash(o)
        if not p:
            bad += 1
            continue
        p = normalize.sanitize(p)
        if not p:
            bad += 1
            continue
        nodes.append(p)
    return nodes, bad


def _sb_to_clash(o: dict) -> dict | None:
    """sing-box outbound -> mihomo proxy，只覆盖常见字段。"""
    t = str(o.get("type", "")).lower()
    t = "ss" if t == "shadowsocks" else t
    server = str(o.get("server", "")).strip()
    port = o.get("server_port") or o.get("port")
    if not server or not port:
        return None
    try:
        port = int(port)
    except Exception:
        return None

    p: dict = {"name": str(o.get("tag") or f"{server}:{port}"),
               "type": t, "server": server, "port": port}

    if t in ("vmess", "vless"):
        p["uuid"] = o.get("uuid", "")
        if t == "vmess":
            p["cipher"] = "auto"
            p["alterId"] = int(o.get("alter_id") or 0)
        else:
            flow = str(o.get("flow") or "")
            if flow:
                p["flow"] = flow
    elif t in ("trojan", "hysteria2"):
        p["password"] = o.get("password", "")
    elif t == "ss":
        p["cipher"] = o.get("method", "")
        p["password"] = o.get("password", "")
    elif t == "tuic":
        p["uuid"] = o.get("uuid", "")
        p["password"] = o.get("password", "")

    tls = o.get("tls") or {}
    if isinstance(tls, dict) and tls.get("enabled"):
        p["tls"] = True
        sni = tls.get("server_name") or ""
        if sni:
            p["servername"] = sni
        alpn = tls.get("alpn")
        if isinstance(alpn, list) and alpn:
            p["alpn"] = alpn
        utls = tls.get("utls") or {}
        if isinstance(utls, dict) and utls.get("fingerprint"):
            p["client-fingerprint"] = utls["fingerprint"]
        reality = tls.get("reality") or {}
        if isinstance(reality, dict) and reality.get("enabled"):
            p["reality-opts"] = {
                "public-key": reality.get("public_key", ""),
                "short-id": reality.get("short_id", ""),
            }
    if o.get("tls") is True:
        p["tls"] = True

    tr = o.get("transport") or {}
    if isinstance(tr, dict):
        tt = str(tr.get("type", "")).lower()
        if tt in ("ws", "httpupgrade", "grpc", "http", "h2"):
            net = {"httpupgrade": "ws", "http": "http", "h2": "h2"}.get(tt, tt)
            p["network"] = net
            if net == "ws":
                p["ws-opts"] = {"path": tr.get("path") or "/"}
                hdrs = tr.get("headers") or {}
                if isinstance(hdrs, dict) and hdrs.get("Host"):
                    p["ws-opts"]["headers"] = {"Host": hdrs["Host"]}
            elif net == "grpc":
                p["grpc-opts"] = {"grpc-service-name": tr.get("service_name") or ""}
            elif net == "h2":
                p["h2-opts"] = {"path": tr.get("path") or "/"}
    if o.get("obfs") and isinstance(o.get("obfs"), dict):
        p["obfs"] = o["obfs"].get("type")
        p["obfs-password"] = o["obfs"].get("password")
    return p


_SURGE_TYPES = {"ss", "vmess", "trojan", "hysteria2", "snell", "socks5", "http"}
_QX_TYPES = {"shadowsocks", "vmess", "vless", "trojan", "hysteria2", "socks5", "http"}


def _kv(parts: list[str]) -> dict:
    d = {}
    for x in parts:
        k, sep, v = x.partition("=")
        if sep:
            d[k.strip().lower()] = v.strip()
    return d


def _from_ini(text: str) -> tuple[list, int]:
    """Surge / Quantumult X 那种 `key = value` 的行。

    两者靠左边判断：左边是协议名的是 QX（`vmess=host:port, ...`），
    左边是节点名的是 Surge（`香港01 = vmess, host, port, ...`）。
    """
    nodes, bad = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;/[" or "=" not in line:
            continue
        left, _, right = line.partition("=")
        raw_left = left.strip()          # 节点名要保留大小写
        left = raw_left.lower()
        parts = [x.strip() for x in right.split(",")]
        if not parts:
            continue
        try:
            if left in _QX_TYPES:
                p = _parse_qx(left, parts, line)
            elif parts[0].lower() in _SURGE_TYPES:
                p = _parse_surge(raw_left, parts, line)
            else:
                continue
        except Exception:
            bad += 1
            continue
        if not p:
            bad += 1
            continue
        p = normalize.sanitize(p)
        if not p:
            bad += 1
            continue
        nodes.append(p)
    return nodes, bad


def _split_hostport(s: str):
    s = s.strip()
    if s.startswith("["):                       # [v6]:port
        host, _, rest = s.partition("]")
        return host[1:], rest.lstrip(":")
    if s.count(":") == 1:
        h, _, p = s.partition(":")
        return h, p
    return s, ""


def _parse_qx(kind: str, parts: list[str], line: str) -> dict | None:
    host, port = _split_hostport(parts[0])
    if not host or not port.isdigit():
        return None
    kv = _kv(parts[1:])
    name = kv.get("tag") or f"{host}:{port}"
    t = "ss" if kind == "shadowsocks" else kind
    p: dict = {"name": name, "type": t, "server": host, "port": int(port)}
    if t == "ss":
        p["cipher"] = kv.get("method", "")
        p["password"] = kv.get("password", "")
    elif t in ("vmess", "vless"):
        p["uuid"] = kv.get("password") or kv.get("uuid") or ""
        p["cipher"] = "auto"
        if t == "vless":
            p["flow"] = kv.get("flow", "")
    else:
        p["password"] = kv.get("password", "")
    obfs = kv.get("obfs", "").lower()
    if obfs in ("ws", "wss"):
        p["network"] = "ws"
        p["ws-opts"] = {"path": kv.get("obfs-uri") or kv.get("obfs-path") or "/"}
        host_hdr = kv.get("obfs-host")
        if host_hdr:
            p["ws-opts"]["headers"] = {"Host": host_hdr}
    if (kv.get("tls") == "true" or kv.get("over-tls") == "true"
            or obfs == "wss" or kv.get("obfs") == "over-tls"):
        p["tls"] = True
        if kv.get("obfs-host"):
            p["servername"] = kv["obfs-host"]
    if kv.get("sni"):
        p["servername"] = kv["sni"]
    return p


def _parse_surge(name: str, parts: list[str], line: str) -> dict | None:
    kind = parts[0].lower()
    if len(parts) < 3:
        return None
    host, port = parts[1].strip(), parts[2].strip()
    if not host or not port.isdigit():
        return None
    kv = _kv(parts[3:])
    t = {"snell": "snell"}.get(kind, kind)
    p: dict = {"name": name or f"{host}:{port}", "type": t,
               "server": host, "port": int(port)}
    if t == "ss":
        p["cipher"] = kv.get("encrypt-method", "")
        p["password"] = kv.get("password", "")
    elif t == "vmess":
        p["uuid"] = kv.get("username", "")
        p["cipher"] = "auto"
    elif t in ("trojan", "hysteria2"):
        p["password"] = kv.get("password", "")
    else:
        p["password"] = kv.get("password", "")
    if kv.get("ws") == "true":
        p["network"] = "ws"
        p["ws-opts"] = {"path": kv.get("ws-path") or "/"}
        hdrs = kv.get("ws-headers", "")
        if hdrs:
            h, _, v = hdrs.partition(":")
            if h and v:
                p["ws-opts"]["headers"] = {h.strip(): v.strip()}
    if kv.get("tls") == "true":
        p["tls"] = True
        if kv.get("sni"):
            p["servername"] = kv["sni"]
    if kv.get("sni"):
        p["servername"] = kv["sni"]
    return p


def parse_sub(text: str) -> tuple[list[dict], str, int]:
    """返回 (节点列表, 格式名, 丢弃数)。"""
    raw = (text or "").strip()
    if not raw:
        raise ProbeError("订阅内容为空")

    # 1) sing-box：JSON
    if raw[0] in "{[":
        try:
            doc = json.loads(raw)
        except Exception:
            doc = None
        if isinstance(doc, dict) and "outbounds" in doc:
            nodes, bad = _from_singbox(doc)
            return nodes, "singbox", bad

    # 2) clash：YAML（先看关键字，省得对大文件做完整解析）
    head = raw[:40000]
    if "proxies:" in head or "proxy-groups:" in head:
        try:
            doc = yaml.safe_load(raw)
        except Exception:
            doc = None
        if isinstance(doc, dict) and "proxies" in doc:
            nodes, bad = _from_clash(doc)
            return nodes, "clash", bad

    # 3) base64 包一层（v2rayN / 小火箭 的通用订阅）
    decoded = _b64_try(raw)
    if decoded:
        if "proxies:" in decoded[:40000]:
            try:
                doc = yaml.safe_load(decoded)
            except Exception:
                doc = None
            if isinstance(doc, dict) and "proxies" in doc:
                nodes, bad = _from_clash(doc)
                return nodes, "clash(base64)", bad
        nodes, bad = _from_uris(decoded)
        if nodes:
            return nodes, "base64", bad

    # 4) 明文 URI 列表
    if "://" in raw:
        nodes, bad = _from_uris(raw)
        if nodes:
            return nodes, "plain", bad

    # 5) Surge / Quantumult X
    nodes, bad = _from_ini(raw)
    if nodes:
        return nodes, "surge/qx", bad

    raise ProbeError("认不出这个订阅的格式（支持 clash / sing-box / base64 / 明文 / Surge / 圈X）")


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    fmt        TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    total      INTEGER NOT NULL DEFAULT 0,
    tested     INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    size_mb    INTEGER NOT NULL DEFAULT 10,
    mode       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS pnodes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL,
    fp       TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    type     TEXT NOT NULL,
    server   TEXT NOT NULL,
    port     INTEGER NOT NULL,
    uri      TEXT NOT NULL DEFAULT '',
    clash    TEXT NOT NULL DEFAULT '',
    tcp_ok   INTEGER,
    tcp_ms   INTEGER,
    latency  INTEGER,
    ttfb     INTEGER,
    speed    REAL,
    ok       INTEGER NOT NULL DEFAULT 0,
    err      TEXT NOT NULL DEFAULT '',
    serr     TEXT NOT NULL DEFAULT '',
    tested_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pn_run ON pnodes(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pn_fp ON pnodes(run_id, fp);
"""


class ProbeDB:
    """订阅探测的历史库，独立文件，和主节点库互不干扰。"""

    def __init__(self, path: str):
        import sqlite3
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.sqlite3 = sqlite3
        with self._conn() as c:
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=10000")
            except Exception:
                pass
            c.executescript(SCHEMA)
            # 老库补列：测速失败的原因单独存，别覆盖真连测试的 err
            cols = {r[1] for r in c.execute("PRAGMA table_info(pnodes)")}
            if "serr" not in cols:
                c.execute("ALTER TABLE pnodes ADD COLUMN serr TEXT NOT NULL DEFAULT ''")

    def _conn(self):
        c = self.sqlite3.connect(self.path, timeout=15)
        c.row_factory = self.sqlite3.Row
        return c

    # ---- 记录
    def create_run(self, title: str, source: str, fmt: str,
                   nodes: list[dict]) -> int:
        ts = now()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs(title,source,fmt,created_at,updated_at,total,status)"
                " VALUES(?,?,?,?,?,?,'draft')",
                (title, source, fmt, ts, ts, len(nodes)))
            rid = int(cur.lastrowid)
            c.executemany(
                "INSERT OR IGNORE INTO pnodes(run_id,fp,name,type,server,port,uri,clash)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(rid, fingerprint(p), p.get("name", ""), p["type"], p["server"],
                  int(p["port"]),
                  p.get("_uri") or parser.proxy_to_uri(p),
                  json.dumps({k: v for k, v in p.items() if not k.startswith("_")},
                             ensure_ascii=False))
                 for p in nodes])
        return rid

    def runs(self) -> list[dict]:
        # 顺手带上「有几个测出了下载速度」——这是判断一条记录值不值得点开的关键指标
        sql = """
            SELECT r.*, COALESCE(s.spd, 0) AS spd
              FROM runs r
              LEFT JOIN (SELECT run_id, COUNT(*) AS spd FROM pnodes
                          WHERE speed > 0 GROUP BY run_id) s
                     ON s.run_id = r.id
             ORDER BY r.id DESC LIMIT 200
        """
        with self._conn() as c:
            rows = c.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def run(self, rid: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def nodes(self, rid: int, ids: list[int] | None = None) -> list[dict]:
        sql = "SELECT * FROM pnodes WHERE run_id=?"
        args: list = [rid]
        if ids:
            sql += " AND id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        sql += " ORDER BY id"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def node(self, nid: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM pnodes WHERE id=?", (nid,)).fetchone()
        return dict(r) if r else None

    # ---- 写结果
    def save_tcp(self, rid: int, result: dict[str, int | None]) -> None:
        """result: {fp: ms 或 None}"""
        ts = now()
        with self._conn() as c:
            for fp, ms in result.items():
                c.execute(
                    "UPDATE pnodes SET tcp_ok=?, tcp_ms=?, tested_at=? "
                    "WHERE run_id=? AND fp=?",
                    (1 if ms is not None else 0, ms, ts, rid, fp))
        self._touch(rid)

    def save_real(self, rid: int, result: dict[str, tuple]) -> None:
        """result: {fp: (ok, latency, err)}"""
        ts = now()
        with self._conn() as c:
            for fp, (ok, lat, err) in result.items():
                c.execute(
                    "UPDATE pnodes SET ok=?, latency=?, err=?, tested_at=? "
                    "WHERE run_id=? AND fp=?",
                    (1 if ok else 0, lat, (err or "")[:120], ts, rid, fp))
        self._touch(rid)

    def save_speed(self, nid: int, speed: float | None, ttfb: int | None,
                   err: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE pnodes SET speed=?, ttfb=?, serr=?, tested_at=? WHERE id=?",
                      (speed, ttfb, (err or "")[:120], now(), nid))

    def _touch(self, rid: int) -> None:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM pnodes WHERE run_id=?",
                              (rid,)).fetchone()["n"]
            ok = c.execute("SELECT COUNT(*) n FROM pnodes WHERE run_id=? AND ok=1",
                           (rid,)).fetchone()["n"]
            tested = c.execute(
                "SELECT COUNT(*) n FROM pnodes WHERE run_id=? AND tested_at IS NOT NULL",
                (rid,)).fetchone()["n"]
            c.execute("UPDATE runs SET total=?, ok=?, tested=?, updated_at=? WHERE id=?",
                      (total, ok, tested, now(), rid))

    def set_status(self, rid: int, status: str, mode: str = "",
                   size_mb: int | None = None) -> None:
        sets = ["status=?", "updated_at=?"]
        args: list = [status, now()]
        if mode:
            sets.append("mode=?")
            args.append(mode)
        if size_mb:
            sets.append("size_mb=?")
            args.append(size_mb)
        args.append(rid)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)

    # ---- 编辑
    def rename(self, rid: int, title: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET title=?, updated_at=? WHERE id=?",
                      ((title or "").strip()[:120], now(), rid))

    def delete_run(self, rid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pnodes WHERE run_id=?", (rid,))
            c.execute("DELETE FROM runs WHERE id=?", (rid,))

    def rename_node(self, nid: int, name: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE pnodes SET name=? WHERE id=?",
                      ((name or "").strip()[:80], nid))

    def delete_nodes(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._conn() as c:
            q = ",".join("?" * len(ids))
            cur = c.execute(f"DELETE FROM pnodes WHERE id IN ({q})", ids)
            return cur.rowcount


# ---------------------------------------------------------------- 测速

def _pick_proxies(rows: list[dict]) -> list[tuple[dict, dict]]:
    """把库里的行还原成 mihomo proxy，顺手再过一遍消毒。"""
    out = []
    for r in rows:
        try:
            p = json.loads(r["clash"] or "{}")
        except Exception:
            continue
        if not p:
            continue
        p = normalize.sanitize(p)
        if p is None:
            continue
        out.append((r, p))
    return out


def _select(port: int, name: str) -> bool:
    try:
        r = requests.put(f"http://127.0.0.1:{port}/proxies/PROBE",
                         json={"name": name}, timeout=5)
        return r.status_code in (200, 204)
    except Exception:
        return False


def _proxy_of(port: int) -> dict:
    u = f"http://127.0.0.1:{port}"
    return {"http": u, "https": u}


def _warmup(port: int, timeout: float = 5.0) -> tuple[int | None, str]:
    """先发一个 1 字节的请求，拿到真实首字节时间，也顺便把连接建起来。"""
    t0 = time.time()
    try:
        r = requests.get("https://speed.cloudflare.com/__down?bytes=1",
                         proxies=_proxy_of(port), timeout=(timeout, timeout),
                         headers={"User-Agent": UA, "Accept-Encoding": "identity"},
                         stream=True)
        try:
            for _ in r.iter_content(1):
                break
        finally:
            r.close()
        return int((time.time() - t0) * 1000), ""
    except Exception as e:
        return None, type(e).__name__


def _download(port: int, urls: list[str], limit: int,
              max_sec: float) -> tuple[float | None, str]:
    """返回 (字节/秒, 错误)。读到 limit 字节或超时为止。

    光「收到过字节」不算跑通：有的节点会先吐几百字节然后卡死，
    用 364 字节 / 1 秒 算出来的速度没有任何意义，还会混进「有下载速度」那一档里。
    所以要求至少拿到 MIN_BYTES 才认这个速度。
    """
    last = "timeout"
    for url in urls:
        t0 = time.time()
        got = 0
        try:
            with requests.get(url, proxies=_proxy_of(port), stream=True,
                              timeout=(6, max_sec),
                              headers={"User-Agent": UA,
                                       "Accept-Encoding": "identity"}) as r:
                if r.status_code >= 400:
                    last = f"HTTP {r.status_code}"
                    continue
                for chunk in r.iter_content(65536):
                    got += len(chunk)
                    if got >= limit or time.time() - t0 > max_sec:
                        break
        except Exception as e:
            last = type(e).__name__
            continue
        dt = time.time() - t0
        if got >= min(limit, MIN_BYTES) and dt > 0.05:
            return got / dt, ""
        last = (f"只收到 {got // 1024}KB" if got >= 1024
                else f"只收到 {got} 字节") if got else "no data"
    return None, last


def _key_of(row: dict):
    """测速结果的 key。测速库用 id，主库用 fp——两边共用同一套测速逻辑。"""
    v = row.get("id")
    return v if v is not None else row.get("fp")


def speed_chunk(rows: list[dict], cfg: dict, root: str, exe: str,
                size_mb: int, max_sec: float, on_one=None) -> dict:
    """一批节点在一个 mihomo 里依次测速，返回 {key: (speed, ttfb, err)}。

    key 由 `_key_of` 决定：测速库的行有 id，主库的行只有 fp。
    做法：把节点全灌进去，建一个 select 组，测之前用 API 把组切到目标节点，
    再通过 mixed-port 下载测试文件。这样一个进程就能测完所有节点，
    不用每个节点起一次内核（低配机上那个开销很要命）。
    """
    pairs = _pick_proxies(rows)
    if not pairs:
        return {}

    port = random.randint(20000, 45000)
    workdir = os.path.join(root, "data", "mihomo")
    os.makedirs(workdir, exist_ok=True)

    proxies, names = [], []
    for i, (_r, p) in enumerate(pairs):
        nm = f"n{i:05d}"
        q = {k: v for k, v in p.items() if k != "name"}
        q["name"] = nm
        proxies.append(q)
        names.append(nm)

    conf = {
        "mixed-port": port + 1,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{port}",
        "geo-auto-update": False,
        "geodata-mode": False,
        "proxies": proxies,
        "proxy-groups": [{"name": "PROBE", "type": "select", "proxies": names}],
        "rules": ["MATCH,PROBE"],
    }
    cfg_path = os.path.join(workdir, f"probe_{port}.yaml")
    yamlutil.dump_to(cfg_path, conf)

    limit = size_mb * 1000 * 1000
    urls = SPEED_URLS.get(size_mb) or [f"https://speed.cloudflare.com/__down?bytes={limit}"]
    out: dict = {}
    proc = None
    try:
        creation = 0x08000000 if sys.platform == "win32" else 0
        proc = subprocess.Popen([exe, "-d", workdir, "-f", cfg_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=creation)
        if not tester._wait_api(port, timeout=25):
            return {_key_of(r): (None, None, "内核启动失败") for r, _ in pairs}

        for i, (row, _p) in enumerate(pairs):
            k = _key_of(row)
            if not _select(port, names[i]):
                out[k] = (None, None, "选择节点失败")
                if on_one:
                    on_one(i + 1, len(pairs), out[k])
                continue
            ttfb, err = _warmup(port + 1)
            if err:
                out[k] = (None, None, err)
            else:
                sp, err2 = _download(port + 1, urls, limit, max_sec)
                out[k] = (sp, ttfb, err2)
            if on_one:
                on_one(i + 1, len(pairs), out[k])
        return out
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            os.remove(cfg_path)
        except Exception:
            pass


def speed_rows(rows: list[dict], cfg: dict, root: str, exe: str,
               size_mb: int = 10, max_sec: float = 12, chunk: int = 10,
               on_one=None) -> dict:
    """给一批节点分批测下载速度，返回 {key: (speed, ttfb, err)}。

    分批是为了兜底：mihomo 加载配置是全有或全无的，一批里混进一个坏节点
    会让整批都测不出来。10 个一批，坏的最多带走 9 个。
    主流水线的「最终筛查」和订阅测速页都走这里。
    """
    out: dict = {}
    total = len(rows)
    for start in range(0, total, max(1, chunk)):
        part = rows[start:start + chunk]

        def cb(i, _n, r, _o=start):
            if on_one:
                on_one(_o + i, total, r)

        out.update(speed_chunk(part, cfg, root, exe, size_mb, max_sec, on_one=cb))
    return out


# ---------------------------------------------------------------- 主流程

def _hr(title: str) -> None:
    print("\n" + "=" * 62)
    print(f"  {title}")
    print("=" * 62)


def run_probe(db: ProbeDB, rid: int, cfg: dict, root: str,
              mode: str = "auto", size_mb: int = 10,
              max_speed: int = 15, ids: list[int] | None = None) -> int:
    """跑一次探测。mode: fast | deep | speed | auto"""
    run = db.run(rid)
    if not run:
        print(f"找不到记录 #{rid}")
        return 2

    label = {"fast": "快速（只测 TCP）", "deep": "延迟（真实 HTTP）",
             "speed": "测速（下载）", "auto": "自动（TCP → 延迟 → 下载测速）"}
    _hr(f"订阅测速 —— {label.get(mode, mode)}")
    print(f"  订阅地址: {run['source'][:100]}")
    print(f"  格式: {run['fmt']}   节点: {run['total']} 个")
    if size_mb not in SPEED_SIZES:
        size_mb = 10
    if mode in ("speed", "auto"):
        print(f"  测速文件: {size_mb}MB / 最多测 {max_speed} 个")

    rows = db.nodes(rid, ids)
    if not rows:
        print("  这条记录里没有节点")
        db.set_status(rid, "empty", mode)
        return 2
    print(f"  本次检测: {len(rows)} 个节点")

    db.set_status(rid, "running", mode, size_mb)
    t0 = time.time()
    check_cfg = cfg.get("check") or {}
    tcp_cfg = check_cfg.get("tcp") or {}
    real_cfg = check_cfg.get("real") or {}
    steps = {"fast": 1, "deep": 2, "speed": 2, "auto": 3}.get(mode, 3)
    no = [0]

    def tag() -> str:
        no[0] += 1
        return f"[{no[0]}/{steps}]"
    try:
        # ---- 第一步：TCP 预筛（测速模式不需要，它只测已经可用的）
        if mode in ("fast", "deep", "auto"):
            nodes = [{"fp": r["fp"], "server": r["server"], "port": r["port"]}
                     for r in rows]
            ok_map = tester.tcp_precheck(
                nodes,
                timeout=float(tcp_cfg.get("timeout", 3.0)),
                threads=int(tcp_cfg.get("threads", 200)))
            db.save_tcp(rid, {r["fp"]: ok_map.get(r["fp"]) for r in rows})
            print(f"  {tag()} TCP 预筛: {len(ok_map)} 通过 / "
                  f"{len(rows) - len(ok_map)} 淘汰 ({time.time() - t0:.1f}s)")
            if mode == "fast":
                _finish(db, rid, t0, mode)
                return 0
            rows = [r for r in rows if r["fp"] in ok_map]
            if not rows:
                print("  没有节点通过 TCP 预筛")
                _finish(db, rid, t0, mode)
                return 0

        # ---- 第二步：真实 HTTP 延迟
        if mode in ("deep", "auto", "speed"):
            exe = tester.ensure_mihomo(check_cfg, root)
            if not exe:
                print("  拿不到 mihomo 内核，跳过真实连接测试")
                db.set_status(rid, "error", mode)
                return 2

            # 只做测速时，已经测过延迟的节点不用重测
            if mode == "speed":
                need = [r for r in rows if r.get("latency") is None]
            else:
                need = rows
            if need:
                step = tag()
                # chunk 交给 real_test 按配置来（低配机一次灌太多内核会顶内存）
                res = tester.real_test([dict(r) for r in need], real_cfg, root, exe)
                db.save_real(rid, res)
                ok_n = sum(1 for v in res.values() if v[0])
                print(f"  {step} 延迟测试: {ok_n} 可用 / {len(need) - ok_n} 淘汰")
            if mode == "deep":
                _finish(db, rid, t0, mode)
                return 0

            # ---- 第三步：下载测速
            rows = [r for r in db.nodes(rid, ids) if r["ok"] and r["latency"]]
            rows.sort(key=lambda r: r["latency"])
            rows = rows[:max(1, max_speed)]
            if not rows:
                print("  没有可用节点，跳过测速")
                _finish(db, rid, t0, mode)
                return 0

            step = tag()
            print(f"  {step} 下载测速 {len(rows)} 个（按延迟从快到慢，单文件 {size_mb}MB）")

            def on_one(i, total, res):
                sp, _ttfb, err = res
                if sp:
                    print(f"    测速 {i}/{total}  {sp / 1048576:6.2f} MB/s"
                          f"  ({sp * 8 / 1e6:6.1f} Mbps)")
                else:
                    print(f"    测速 {i}/{total}  失败 {err}")

            # 每 10 个一批，跑完一批就落库，中途崩了也不丢结果
            for start in range(0, len(rows), 10):
                part = rows[start:start + 10]
                got = speed_chunk(part, check_cfg, root, exe,
                                  size_mb, max_sec=10, on_one=on_one)
                for nid, (sp, ttfb, err) in got.items():
                    db.save_speed(nid, sp, ttfb, err)

        _finish(db, rid, t0, mode)
        return 0
    except KeyboardInterrupt:
        print("\n  已中断")
        db.set_status(rid, "stopped", mode)
        return 130
    except Exception as e:
        print(f"  出错: {type(e).__name__}: {e}")
        db.set_status(rid, "error", mode)
        return 2


def _finish(db: ProbeDB, rid: int, t0: float, mode: str = "auto") -> None:
    db._touch(rid)
    db.set_status(rid, "done")
    rows = db.nodes(rid)
    ok = [r for r in rows if r["ok"]]
    fast = [r for r in rows if r["speed"]]
    fast.sort(key=lambda r: -(r["speed"] or 0))
    tcp_ok = sum(1 for r in rows if r["tcp_ok"] == 1)
    print()
    if ok:
        print(f"完成: {len(ok)} 可用 / {len(rows)} 个节点")
    else:
        print(f"完成: 端口通 {tcp_ok} 个 / 共 {len(rows)} 个节点"
              + ("（快速检测不判断能否上网，要可用请跑深度或自动）" if mode == "fast" else ""))
    if fast:
        b = fast[0]
        print(f"  最快 {b['speed'] / 1048576:.2f} MB/s  "
              f"({b['speed'] * 8 / 1e6:.1f} Mbps)  {b['server']}:{b['port']}")
    print(f"总耗时 {time.time() - t0:.1f}s")
