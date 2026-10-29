"""决定性测试：mihomo 的 trojan 到底把哪个字段当 SNI 发出去。

做法：在 VPS 本机起一个假 TLS 端口，只解析 ClientHello 里的 SNI 就关掉；
再让 mihomo 的 trojan 节点指向它，看实际发出的 SNI 是什么。

  F  只给 sni: wanted.example
  G  只给 servername: wanted.example
  H  两个都不给（对照，应该发 server 的值）
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = "/opt/nodehub"
EXE = os.path.join(ROOT, "bin", "mihomo")
WD = os.path.join(ROOT, "data", "sniprobe")
LISTEN = 38443
MIXED = 38444
API = 38445

seen = []


def parse_sni(data: bytes):
    """从 ClientHello 里抠 SNI。"""
    try:
        p = 43  # 跳过 record(5) + handshake(4) + version(2) + random(32)
        sid_len = data[p]
        p += 1 + sid_len
        cs_len = int.from_bytes(data[p:p + 2], "big")
        p += 2 + cs_len
        cm_len = data[p]
        p += 1 + cm_len
        ext_total = int.from_bytes(data[p:p + 2], "big")
        p += 2
        end = p + ext_total
        while p + 4 <= end:
            et = int.from_bytes(data[p:p + 2], "big")
            el = int.from_bytes(data[p + 2:p + 4], "big")
            p += 4
            if et == 0:  # server_name
                q = p + 2
                name_type = data[q]
                nlen = int.from_bytes(data[q + 1:q + 3], "big")
                if name_type == 0:
                    return data[q + 3:q + 3 + nlen].decode("ascii", "replace")
            p += el
    except Exception as e:
        return f"<parse-fail {e}>"
    return "<no-sni>"


def server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", LISTEN))
    s.listen(16)
    s.settimeout(60)
    while True:
        try:
            conn, _ = s.accept()
        except socket.timeout:
            return
        try:
            conn.settimeout(3)
            head = conn.recv(5)
            if len(head) < 5:
                conn.close()
                continue
            need = int.from_bytes(head[3:5], "big")
            buf = b""
            while len(buf) < need:
                chunk = conn.recv(need - len(buf))
                if not chunk:
                    break
                buf += chunk
            seen.append(parse_sni(head + buf))
        except Exception as e:
            seen.append(f"<err {e}>")
        finally:
            conn.close()


def run_case(label, extra):
    seen.clear()
    os.makedirs(WD, exist_ok=True)
    conf = {
        "mixed-port": MIXED,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "external-controller": f"127.0.0.1:{API}",
        "geo-auto-update": False,
        "proxies": [dict(type="trojan", name="t1", server="127.0.0.1",
                         port=LISTEN, password="x", skip_cert_verify=True,
                         **extra)],
        "proxy-groups": [{"name": "G", "type": "select", "proxies": ["t1"]}],
        "rules": ["MATCH,G"],
    }
    conf["proxies"][0] = {k.replace("skip_cert_verify", "skip-cert-verify"): v
                          for k, v in conf["proxies"][0].items()}
    cfg_path = os.path.join(WD, f"c_{label}.yaml")
    with open(cfg_path, "w") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

    proc = subprocess.Popen([EXE, "-d", WD, "-f", cfg_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        # 等 API 起来
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{API}/version",
                                       timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        # 通过代理发一个请求，逼它连 trojan
        try:
            urllib.request.urlopen("http://example.com/",
                                   timeout=6)  # noqa
        except Exception:
            pass
        subprocess.run(["curl", "-s", "-o", "/dev/null", "--max-time", "6",
                        "--proxy", f"http://127.0.0.1:{MIXED}",
                        "http://example.com/"], check=False)
        time.sleep(0.6)
        got = [s for s in seen if s and not s.startswith("<err")]
        print(f"{label:34} SNI -> {got[:3] if got else '（没收到握手）'}")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        time.sleep(0.4)


th = threading.Thread(target=server, daemon=True)
th.start()
time.sleep(0.5)

run_case("F 只给 sni=wanted.example", {"sni": "wanted.example"})
run_case("G 只给 servername=wanted.example", {"servername": "wanted.example"})
run_case("H 都不给（对照）", {})
