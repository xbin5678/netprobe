"""确认导出的整份配置能被 mihomo 正常加载（不因 sni 字段报错）。"""
import json
import os
import subprocess
import time
import urllib.request

import yaml

ROOT = "/opt/nodehub"
EXE = os.path.join(ROOT, "bin", "mihomo")
WD = os.path.join(ROOT, "data", "loadtest")
API = 38791

os.makedirs(WD, exist_ok=True)
cfg = yaml.safe_load(open("/tmp/exp_alive.yaml", encoding="utf-8"))
cfg["mixed-port"] = 38790
cfg["external-controller"] = f"127.0.0.1:{API}"
path = os.path.join(WD, "c.yaml")
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

n = len(cfg.get("proxies") or [])
print(f"配置里 {n} 个节点，启动内核…")
proc = subprocess.Popen([EXE, "-d", WD, "-f", path],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)
up = False
for _ in range(40):
    try:
        v = urllib.request.urlopen(f"http://127.0.0.1:{API}/version",
                                   timeout=1).read().decode()
        print("内核已加载：", v)
        up = True
        break
    except Exception:
        if proc.poll() is not None:
            break
        time.sleep(0.5)

if up:
    d = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{API}/proxies", timeout=3).read().decode())
    prox = [k for k, v in d["proxies"].items()
            if v.get("type") not in ("Selector", "URLTest", "Direct", "Reject",
                                     "Compatible", "Pass", "RejectDrop")]
    print(f"内核里实际生效的节点: {len(prox)}")
else:
    print("加载失败，内核输出：")
    proc.terminate()
    try:
        out = proc.communicate(timeout=5)[0]
    except Exception:
        proc.kill()
        out = ""
    print("\n".join(out.splitlines()[:12]))

proc.terminate()
try:
    proc.wait(5)
except Exception:
    proc.kill()
