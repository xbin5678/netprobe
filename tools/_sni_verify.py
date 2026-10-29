"""端到端验收：拿**线上真实导出的**配置，验证 trojan 现在真的把 SNI 发出去了。

做法：从导出的 yaml 里挑 trojan 节点，把 server/port 改指向本机假 TLS 端口，
其余字段原样保留，看 mihomo 发出的 ClientHello 里 SNI 是不是节点自带的那个域名。
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

import yaml

ROOT = "/opt/nodehub"
EXE = os.path.join(ROOT, "bin", "mihomo")
WD = os.path.join(ROOT, "data", "sniverify")
LISTEN = 38643
MIXED = 38644
API = 38645

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
    except Exception:
        return "<fail>"
    return "<no-sni>"


def server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", LISTEN))
    s.listen(16)
    s.settimeout(90)
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


exp = yaml.safe_load(open("/tmp/exp_alive.yaml", encoding="utf-8"))
tro = [p for p in exp["proxies"] if p["type"] == "trojan" and p.get("sni")]
print(f"导出配置里带 sni 的 trojan: {len(tro)} 个")
pick = tro[:4]

threading.Thread(target=server, daemon=True).start()
time.sleep(0.5)

for p in pick:
    want = p["sni"]
    seen.clear()
    os.makedirs(WD, exist_ok=True)
    q = dict(p)
    q["name"] = "t1"
    q["server"] = "127.0.0.1"
    q["port"] = LISTEN
    conf = {
        "mixed-port": MIXED, "allow-lan": False, "mode": "rule",
        "log-level": "silent", "external-controller": f"127.0.0.1:{API}",
        "geo-auto-update": False,
        "proxies": [q],
        "proxy-groups": [{"name": "G", "type": "select", "proxies": ["t1"]}],
        "rules": ["MATCH,G"],
    }
    path = os.path.join(WD, "v.yaml")
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
        ok = "✓" if got and got[0] == want else "✗"
        print(f"  {ok} 期望 {want:32} 实发 {got[0] if got else '（没握手）'}")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        time.sleep(0.3)
