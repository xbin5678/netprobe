"""验证消毒层：修了多少、丢了多少、修完还有多少批会让 mihomo 失败。"""
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter

import yaml

sys.path.insert(0, ".")
from nodehub import normalize, yamlutil

CHUNK = 200
exe = os.path.join("bin", "mihomo.exe")
work = os.path.join("data", "_mh2")
os.makedirs(work, exist_ok=True)

c = sqlite3.connect("data/nodes.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("SELECT fp, clash FROM nodes ORDER BY fp")]

changed = 0
dropped = 0
diffs = Counter()
clean = []
for r in rows:
    raw = json.loads(r["clash"])
    s = normalize.sanitize(raw)
    if s is None:
        dropped += 1
        continue
    if json.dumps(s, sort_keys=True) != json.dumps(
            {k: v for k, v in raw.items() if v is not None}, sort_keys=True):
        changed += 1
        for k in set(raw) | set(s):
            if raw.get(k) != s.get(k):
                diffs[k] += 1
    clean.append((r["fp"], s))

print(f"总数 {len(rows)}  修正 {changed}  丢弃 {dropped}  可用 {len(clean)}")
print("\n改动最多的字段:")
for k, v in diffs.most_common(12):
    print(f"  {k:<22} {v}")

# 修完再按 200 个一批丢给 mihomo
bad = 0
total = 0
msgs = []
for start in range(0, len(clean), CHUNK):
    part = clean[start:start + CHUNK]
    proxies = []
    for i, (fp, p) in enumerate(part):
        q = dict(p)
        q["name"] = f"n{i}"
        proxies.append(q)
    conf = {"mixed-port": 7999, "log-level": "silent",
            "proxies": proxies, "rules": ["MATCH,DIRECT"]}
    path = os.path.join(work, "s.yaml")
    yamlutil.dump_to(path, conf)
    total += 1
    r = subprocess.run([exe, "-t", "-d", work, "-f", path],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        bad += 1
        for line in (r.stdout + r.stderr).splitlines():
            if "error" in line.lower():
                msgs.append(line.strip()[:150])
                break

print(f"\n消毒后：{total} 批，失败 {bad} 批")
for m in msgs[:8]:
    print("  " + m)
