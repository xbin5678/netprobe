"""确认 mihomo 认不认 xhttp-opts（path / host / mode）。

做法：本地裸 TCP 监听，抓 mihomo 拨过来的前 400 字节，看里面有没有
我们设置的 path。顺便对比「不给 xhttp-opts」时的默认值。
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

EXE = "/opt/nodehub/bin/mihomo"
WD = "/opt/nodehub/data/xhttptest"
UUID = "0b3e0b1e-1111-2222-3333-444455556666"


def sniff(box, lport):
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
            while len(data) < 400:
                b = c.recv(400 - len(data))
                if not b:
                    break
                data += b
        except socket.timeout:
            pass
        box["data"] = data
        c.close()
    except Exception as e:
        box["err"] = str(e)
    finally:
        s.close()


def probe(idx, label, opts):
    lport, mixed, api = 39100 + idx * 3, 39101 + idx * 3, 39102 + idx * 3
    p = {"type": "vless", "name": "t1", "server": "127.0.0.1", "port": lport,
         "uuid": UUID, "network": "xhttp", "tls": False}
    if opts is not None:
        p["xhttp-opts"] = opts
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
                urllib.request.urlopen(f"http://127.0.0.1:{api}/version",
                                       timeout=1).read()
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
    txt = data.decode("latin-1")
    hit = [w for w in ("/deep/path", "/api/v1/upload", "fitindoor") if w in txt]
    print(f"  {label:34} 内核{'起来了' if up else '没起来'} "
          f"收到 {len(data)} 字节  命中={hit or '无'}")
    if not data:
        errs = [l.strip() for l in open(logp, errors="replace") if "error" in l.lower()]
        print(f"       {errs[-1][-100:] if errs else ''}")
    else:
        print(f"       {txt[:200]!r}")


probe(0, "不给 xhttp-opts", None)
probe(1, "只给 path=/", {"path": "/"})
probe(2, "只给 mode=auto", {"mode": "auto"})
probe(3, "path+host", {"path": "/api/v1/upload", "host": "fitindoor.net",
                       "mode": "stream-one"})
