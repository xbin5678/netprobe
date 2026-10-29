"""对照实验：本机 mihomo 能不能正常启动、DIRECT 能不能连通。

如果 DIRECT 有延迟、而节点全 timeout，说明测速链路本身是好的，
是「本机到这些节点」不通，不是导出或内核的问题。
"""
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from nodehub import tester, yamlutil  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "bin", "mihomo.exe")
WORK = os.path.join(ROOT, "data", "mihomo")
os.makedirs(WORK, exist_ok=True)

port = random.randint(20000, 45000)
conf = {
    "mixed-port": port + 1,
    "allow-lan": False,
    "mode": "rule",
    "log-level": "silent",
    "ipv6": False,
    "external-controller": f"127.0.0.1:{port}",
    "geo-auto-update": False,
    "geodata-mode": False,
    "proxies": [],
    "rules": ["MATCH,DIRECT"],
}
cfg_path = os.path.join(WORK, f"ctl_{port}.yaml")
yamlutil.dump_to(cfg_path, conf)

proc = subprocess.Popen([EXE, "-d", WORK, "-f", cfg_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=0x08000000 if sys.platform == "win32" else 0)
try:
    print("内核启动:", tester._wait_api(port, timeout=20))
    for url in ("https://www.gstatic.com/generate_204",
                "https://cp.cloudflare.com/generate_204",
                "http://www.baidu.com"):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/proxies/DIRECT/delay",
                             params={"timeout": 5000, "url": url}, timeout=10)
            print(f"  DIRECT {url:<42} -> {r.status_code} {r.text[:80]}")
        except Exception as e:
            print(f"  DIRECT {url:<42} -> 异常 {type(e).__name__}")
    # 再看一眼本机直连同一个地址（不经过内核），判断是不是网络本身就不通
    for url in ("https://www.gstatic.com/generate_204",
                "https://cp.cloudflare.com/generate_204"):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=6)
            print(f"  裸连   {url:<42} -> {r.status_code} {int((time.time()-t0)*1000)}ms")
        except Exception as e:
            print(f"  裸连   {url:<42} -> 异常 {type(e).__name__}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    try:
        os.remove(cfg_path)
    except Exception:
        pass
