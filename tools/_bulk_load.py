"""用真实节点库做一次「整份配置能否加载」的回归。

mihomo 是配置全有或全无的：proxies 里有一条字段不合法，整份配置都加载
不起来。所以这里把所有非主流传输（xhttp / httpupgrade / h2 / grpc / http）
的节点全塞进同一份配置，再补足到 5000 条，看内核能不能起来、能不能把
每条都注册成出站。
"""
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/nodehub")
from nodehub import normalize  # noqa: E402

EXE = "/opt/nodehub/bin/mihomo"
WD = "/opt/nodehub/data/bulkload"
MIXED, API = 39201, 39202
SPECIAL = ("xhttp", "httpupgrade", "h2", "grpc", "http")

c = sqlite3.connect("/opt/nodehub/data/nodes.db")
rows = list(c.execute("select type, clash from nodes"))
random.seed(7)
random.shuffle(rows)

special, other, dropped = [], [], 0
seen = set()
for typ, clash in rows:
    try:
        p = json.loads(clash or "{}")
    except Exception:
        continue
    net = str(p.get("network") or "tcp").lower()
    s = normalize.sanitize(dict(p, name=f"{typ}-{len(seen)}"))
    if not s:
        dropped += 1
        continue
    fp = json.dumps(s, sort_keys=True)
    if fp in seen:
        continue
    seen.add(fp)
    (special if net in SPECIAL else other).append(s)

proxies = special + other[:max(0, 5000 - len(special))]
names = {p["name"] for p in proxies}
print(f"库里 {len(rows)} 条，消毒丢弃 {dropped} 条")
print(f"非主流传输 {len(special)} 条，总共塞 {len(proxies)} 条")

os.makedirs(WD, exist_ok=True)
conf = {"mixed-port": MIXED, "allow-lan": False, "mode": "rule",
        "log-level": "warning", "external-controller": f"127.0.0.1:{API}",
        "proxies": proxies, "rules": ["MATCH,DIRECT"]}
path = os.path.join(WD, "c.yaml")
# 必须 ensure_ascii=False：ensure_ascii=True 会把 emoji 写成 \ud83d 这种
# 代理对转义，YAML 不认，内核直接 fatal（线上走 yamlutil，没这问题）
json.dump(conf, open(path, "w", encoding="utf-8"), ensure_ascii=False)

with open(os.path.join(WD, "m.log"), "w") as log:
    proc = subprocess.Popen([EXE, "-d", WD, "-f", path],
                            stdout=log, stderr=subprocess.STDOUT)
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
            time.sleep(0.5)
    got = 0
    if up:
        try:
            d = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{API}/proxies", timeout=5))
            got = len([k for k in d.get("proxies", {}) if k in names])
        except Exception as e:
            print("查 /proxies 失败", e)
    proc.terminate()
    try:
        proc.wait(8)
    except Exception:
        proc.kill()

print(f"内核{'加载成功' if up else '加载失败'}，注册出站 {got}/{len(proxies)}")
if not up:
    tail = [l.strip() for l in open(os.path.join(WD, "m.log"), errors="replace")][-6:]
    print("\n".join(tail))
