"""内核到底怎么处理 network: tcp/http/httpupgrade/xhttp？

「配置能加载」不等于「内核真的会发这种协议」——未知的 network 会被静默
当成 tcp，配置照样加载成功。所以这里不看加载结果，看**实际发出去的字节**：
本地起一个裸 TCP 监听，把 mihomo 拨过来的头 120 字节抓下来。
  - 真 tcp  -> 首字节是 vless 版本号 0x00 + uuid，二进制
  - 真 http -> "GET /sniff HTTP/1.1" 明文
  - 静默退回 tcp -> 同样是 0x00 开头（跟 tcp 一模一样）
每个 network 用独立的一套端口，跑完等进程彻底退出再进下一个。
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
WD = os.path.join(ROOT, "data", "transniff")
UUID = "0b3e0b1e-1111-2222-3333-444455556666"


def sniff(box: dict, lport: int) -> None:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", lport))
        s.listen(4)
        s.settimeout(10)
        c, _ = s.accept()
        c.settimeout(3)
        data = b""
        try:
            while len(data) < 120:
                b = c.recv(120 - len(data))
                if not b:
                    break
                data += b
        except socket.timeout:
            pass
        box["data"] = data
        try:
            c.close()
        except Exception:
            pass
    except Exception as e:
        box["err"] = f"{type(e).__name__}: {e}"
    finally:
        s.close()


def probe(idx: int, net: str) -> None:
    lport, mixed, api = 38900 + idx * 3, 38901 + idx * 3, 38902 + idx * 3
    p = {"type": "vless", "name": "t1", "server": "127.0.0.1", "port": lport,
         "uuid": UUID, "network": net, "tls": False}
    if net in ("ws", "httpupgrade", "xhttp"):
        p["ws-opts"] = {"path": "/sniff"}
    if net == "http":
        p["http-opts"] = {"path": ["/sniff"]}
    conf = {"mixed-port": mixed, "allow-lan": False, "mode": "rule",
            "log-level": "debug", "external-controller": f"127.0.0.1:{api}",
            "proxies": [p], "rules": ["MATCH,t1"]}
    os.makedirs(WD, exist_ok=True)
    path = os.path.join(WD, "c.yaml")
    json.dump(conf, open(path, "w"))
    logp = os.path.join(WD, f"m{idx}.log")

    box = {}
    th = threading.Thread(target=sniff, args=(box, lport), daemon=True)
    th.start()
    time.sleep(0.3)

    with open(logp, "w") as log:
        proc = subprocess.Popen([EXE, "-d", WD, "-f", path],
                                stdout=log, stderr=subprocess.STDOUT)
        up = False
        for _ in range(30):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{api}/version", timeout=1).read()
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
        if up:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{mixed}"}))
            try:
                op.open("http://sniff.test/x", timeout=5)
            except Exception:
                pass
        th.join(11)
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
            proc.wait(5)
    time.sleep(0.4)

    data = box.get("data") or b""
    tag = f"  {net:12}"
    if not up:
        tail = [l.strip() for l in open(logp, errors="replace")
                if "level=error" in l or "level=fatal" in l]
        print(f"{tag} 内核没起来  {tail[-1][-110:] if tail else ''}")
        return
    if not data:
        tail = [l.strip() for l in open(logp, errors="replace")
                if "error" in l.lower() or "dial" in l.lower()]
        print(f"{tag} 一个字节都没发  {tail[-1][-110:] if tail else '(无报错)'}")
        return
    head = data[:70]
    if head[:1] == b"\x00":
        kind = "二进制 0x00 开头 = 裸 tcp（或静默退回 tcp）"
    elif head[:3] in (b"GET", b"POS", b"PUT", b"HEA"):
        kind = "明文 HTTP 请求 = 真的在发 HTTP 层协议"
    else:
        kind = "其他"
    print(f"{tag} {kind}\n               {head!r}")


for i, n in enumerate(("tcp", "http", "ws", "httpupgrade", "xhttp")):
    probe(i, n)
