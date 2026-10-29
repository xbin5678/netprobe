"""SNI 字段名矩阵：vmess / vless / trojan 各自认 sni 还是 servername？

复用 _sni_sniff 的假 TLS 监听 + ClientHello 解析，逐个协议试。
hysteria2 / tuic 走 QUIC，这个 TCP 监听抓不到，按文档用 sni。
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

ROOT = "/opt/nodehub"
EXE = os.path.join(ROOT, "bin", "mihomo")
WD = os.path.join(ROOT, "data", "snimatrix")
LISTEN = 38543
MIXED = 38544
API = 38545
WANT = "wanted.example"

seen = []


def parse_sni(data: bytes):
    try:
        p = 43
        p += 1 + data[p]
        p += 2 + int.from_bytes(data[p:p + 2], "big")
        p += 1 + data[p]
        end = p + 2 + int.from_bytes(data[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            et = int.from_bytes(data[p:p + 2], "big")
            el = int.from_bytes(data[p + 2:p + 4], "big")
            p += 4
            if et == 0:
                q = p + 2
                if data[q] == 0:
                    n = int.from_bytes(data[q + 1:q + 3], "big")
                    return data[q + 3:q + 3 + n].decode("ascii", "replace")
            p += el
    except Exception as e:
        return f"<fail {e}>"
    return "<no-sni>"


def server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", LISTEN))
    s.listen(32)
    s.settimeout(120)
    while True:
        try:
            conn, _ = s.accept()
        except socket.timeout:
            return
        try:
            conn.settimeout(3)
            head = conn.recv(5)
            if len(head) < 5:
                continue
            need = int.from_bytes(head[3:5], "big")
            buf = b""
            while len(buf) < need:
                ch = conn.recv(need - len(buf))
                if not ch:
                    break
                buf += ch
            seen.append(parse_sni(head + buf))
        except Exception:
            pass
        finally:
            conn.close()


BASE = {
    "vmess": dict(type="vmess", server="127.0.0.1", port=LISTEN, uuid="0b3e0b1e-1111-2222-3333-444455556666", alterId=0, cipher="auto", tls=True, udp=True),
    "vless": dict(type="vless", server="127.0.0.1", port=LISTEN, uuid="0b3e0b1e-1111-2222-3333-444455556666", tls=True, udp=True),
    "trojan": dict(type="trojan", server="127.0.0.1", port=LISTEN, password="x", udp=True),
}


def run_case(proto, field):
    seen.clear()
    os.makedirs(WD, exist_ok=True)
    p = dict(BASE[proto], name="t1")
    p["skip-cert-verify"] = True
    if field:
        p[field] = WANT
    conf = {
        "mixed-port": MIXED, "allow-lan": False, "mode": "rule",
        "log-level": "silent", "external-controller": f"127.0.0.1:{API}",
        "geo-auto-update": False,
        "proxies": [p],
        "proxy-groups": [{"name": "G", "type": "select", "proxies": ["t1"]}],
        "rules": ["MATCH,G"],
    }
    path = os.path.join(WD, f"{proto}_{field or 'none'}.yaml")
    with open(path, "w") as f:
        json.dump(conf, f, indent=2)
    proc = subprocess.Popen([EXE, "-d", WD, "-f", path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{API}/version",
                                       timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        subprocess.run(["curl", "-s", "-o", "/dev/null", "--max-time", "5",
                        "--proxy", f"http://127.0.0.1:{MIXED}",
                        "http://example.com/"], check=False)
        time.sleep(0.5)
        got = [s for s in seen if s and not s.startswith("<")]
        print(f"  {proto:7} {field or '(不填)':13} -> {got[0] if got else '（没握手）'}")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        time.sleep(0.3)


threading.Thread(target=server, daemon=True).start()
time.sleep(0.5)
for proto in ("vmess", "vless", "trojan"):
    print(f"[{proto}]")
    for field in (None, "sni", "servername"):
        run_case(proto, field)
