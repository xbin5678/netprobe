"""体检：节点库里的数据到底脏在哪。"""
import collections
import json
import re
import sqlite3
import sys

sys.path.insert(0, ".")
from nodehub.parser import SUPPORTED_TYPES

c = sqlite3.connect("data/nodes.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("SELECT * FROM nodes")]
print("总节点:", len(rows))

# mihomo 支持的 ss cipher（不支持的话整个配置文件会被拒）
SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "aes-128-cfb", "aes-192-cfb",
    "aes-256-cfb", "aes-128-ctr", "aes-192-ctr", "aes-256-ctr", "rc4-md5",
    "chacha20-ietf", "xchacha20", "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305",
    "none", "dummy", "aes-128-gcm-siv", "aes-256-gcm-siv",
}
VMESS_CIPHERS = {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}
SSR_CIPHERS = SS_CIPHERS | {"rc4", "des-cfb", "bf-cfb", "cast5-cfb", "rc4-md5",
                            "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
                            "chacha20", "chacha20-ietf", "salsa20"}
NETS = {"tcp", "ws", "grpc", "h2", "http", "xhttp", "splithttp", "quic", "kcp", "mkcp"}

bad = collections.Counter()
samples = collections.defaultdict(list)


def note(kind, n, detail):
    bad[kind] += 1
    if len(samples[kind]) < 3:
        samples[kind].append((n["server"], n["port"], detail))


dup = collections.Counter()
for n in rows:
    try:
        p = json.loads(n["clash"])
    except Exception as e:
        note("clash 不是合法 JSON", n, str(e)[:40])
        continue
    t = p.get("type")
    dup[(t, str(p.get("server", "")).lower(), str(p.get("port")))] += 1

    # 端口
    try:
        port = int(p.get("port"))
        if not (1 <= port <= 65535):
            note("端口越界", n, port)
    except Exception:
        note("端口不是数字", n, p.get("port"))

    # 服务器名
    s = str(p.get("server", ""))
    if not s:
        note("没有 server", n, "")
    elif s != s.strip():
        note("server 有空白", n, repr(s))
    elif s != s.lower():
        note("server 有大写", n, s)
    elif s.startswith("[") or s.endswith("]"):
        note("server 带方括号", n, s)
    elif s.endswith("."):
        note("server 末尾有点", n, s)

    # cipher
    if t == "ss" and p.get("cipher") not in SS_CIPHERS:
        note("ss cipher 不支持", n, p.get("cipher"))
    if t == "vmess" and p.get("cipher") not in VMESS_CIPHERS:
        note("vmess cipher 不支持", n, p.get("cipher"))
    if t == "ssr" and p.get("cipher") not in SSR_CIPHERS:
        note("ssr cipher 不支持", n, p.get("cipher"))

    # 传输层
    net = p.get("network")
    if net and net not in NETS:
        note("network 未知", n, net)
    if net in ("kcp", "quic", "mkcp"):
        note("mihomo 不支持的传输", n, net)

    # 凭据
    if t in ("vmess", "vless"):
        u = str(p.get("uuid") or "")
        if not u:
            note("缺 uuid", n, "")
        elif u != u.strip():
            note("uuid 有空白", n, repr(u))
        elif u != u.lower():
            note("uuid 有大写", n, u)
        elif not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", u):
            note("uuid 格式异常", n, u)
    if t in ("ss", "trojan", "hysteria2") and not p.get("password"):
        note("缺密码", n, "")

    # 名字
    nm = str(p.get("name") or "")
    if re.search(r"[\u200b-\u200f\u202a-\u202e\ufeff]", nm):
        note("名字含零宽字符", n, repr(nm[:20]))
    if len(nm) > 60:
        note("名字过长", n, len(nm))
    if not nm:
        note("名字为空", n, "")

# 同地址同端口重复（不同 fp，说明凭据或协议不同）
for k, v in dup.items():
    if v > 1:
        bad["同地址同端口重复"] += v - 1
        if len(samples["同地址同端口重复"]) < 3:
            samples["同地址同端口重复"].append(k)

print("\n=== 问题分布 ===")
for k, v in bad.most_common():
    print(f"  {k:<22} {v:>6}   例: {samples[k][:2] if k in samples else ''}")
if not bad:
    print("  没有发现问题")

print("\n=== 协议分布 ===")
for k, v in collections.Counter(json.loads(n["clash"]).get("type") for n in rows).most_common():
    print(f"  {k:<12} {v}")
