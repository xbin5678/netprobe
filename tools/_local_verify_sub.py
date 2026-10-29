"""在用户本机加载 out/local-ok-clash.yaml，逐个节点测真实延迟。

这才是「导入自己电脑能不能用」的直接答案。
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "bin", "mihomo.exe")
SRC = os.path.join(ROOT, "out", "local-ok-clash.yaml")
WD = os.path.join(ROOT, ".tmp", "localtest")
MIXED, API = 38990, 38991

os.makedirs(WD, exist_ok=True)
cfg = yaml.safe_load(open(SRC, encoding="utf-8"))
cfg["mixed-port"] = MIXED
cfg["external-controller"] = f"127.0.0.1:{API}"
path = os.path.join(WD, "c.yaml")
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

names = [p["name"] for p in cfg["proxies"]]
print(f"配置里 {len(names)} 个节点，启动本机内核…")

proc = subprocess.Popen([EXE, "-d", WD, "-f", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000 if sys.platform == "win32" else 0)
try:
    up = False
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{API}/version",
                                   timeout=1).read()
            up = True
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.4)
    if not up:
        sys.exit("内核没起来，配置可能有问题")

    url = "http://www.gstatic.com/generate_204"
    ok = 0
    rows = []
    for nm in names:
        q = urllib.parse.quote(nm, safe="")
        u = (f"http://127.0.0.1:{API}/proxies/{q}/delay"
             f"?url={urllib.parse.quote(url, safe='')}&timeout=5000")
        try:
            d = json.loads(urllib.request.urlopen(u, timeout=8).read().decode())
            ms = d.get("delay")
        except Exception:
            ms = None
        if ms:
            ok += 1
        rows.append((nm, ms))
    print(f"\n真实可用 {ok}/{len(names)}")
    for nm, ms in sorted(rows, key=lambda x: (x[1] is None, x[1] or 0)):
        print(f"  {'✓' if ms else '✗'} {nm:28} {ms if ms else '-'} ms")
finally:
    proc.terminate()
    try:
        proc.wait(5)
    except Exception:
        proc.kill()
