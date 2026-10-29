"""统计：把库里的节点按 200 个一批丢给 mihomo，有多少批会因为个别坏节点整体失败。"""
import json
import os
import sqlite3
import subprocess
import sys

import yaml

sys.path.insert(0, ".")
from nodehub import tester

CHUNK = 200
exe = os.path.join("bin", "mihomo.exe")
work = os.path.join("data", "_mh2")
os.makedirs(work, exist_ok=True)

c = sqlite3.connect("data/nodes.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("SELECT fp, clash FROM nodes ORDER BY fp")]
print("总节点:", len(rows))

bad_chunks = 0
bad_nodes_hint = []
total_chunks = 0
for start in range(0, len(rows), CHUNK):
    part = rows[start:start + CHUNK]
    proxies = []
    for i, r in enumerate(part):
        p = json.loads(r["clash"])
        p["name"] = f"n{i}"
        proxies.append(p)
    conf = {"mixed-port": 7999, "log-level": "silent",
            "proxies": proxies, "rules": ["MATCH,DIRECT"]}
    path = os.path.join(work, "c.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(conf, f, allow_unicode=True, sort_keys=False)
    total_chunks += 1
    r = subprocess.run([exe, "-t", "-d", work, "-f", path],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        bad_chunks += 1
        msg = ""
        for line in (r.stdout + r.stderr).splitlines():
            if "error" in line.lower() or "fatal" in line.lower():
                msg = line.strip()[:160]
                break
        bad_nodes_hint.append((start, msg))

print(f"\n批次总数 {total_chunks}，失败 {bad_chunks} 批")
for start, msg in bad_nodes_hint[:12]:
    print(f"  第 {start//CHUNK+1} 批 (从 {start} 开始): {msg}")
