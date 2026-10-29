"""用二分法把「让整批失败」的坏节点精确找出来，并打印它的关键字段。"""
import json
import os
import sqlite3
import subprocess
import sys

import yaml

sys.path.insert(0, ".")
from nodehub import normalize, yamlutil

exe = os.path.join("bin", "mihomo.exe")
work = os.path.join("data", "_mh2")
os.makedirs(work, exist_ok=True)


def test(proxies):
    """返回 (ok, errmsg)"""
    ps = []
    for i, p in enumerate(proxies):
        q = dict(p)
        q["name"] = f"n{i}"
        ps.append(q)
    conf = {"mixed-port": 7999, "log-level": "silent",
            "proxies": ps, "rules": ["MATCH,DIRECT"]}
    path = os.path.join(work, "b.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(conf, f, allow_unicode=True, sort_keys=False)
    r = subprocess.run([exe, "-t", "-d", work, "-f", path],
                       capture_output=True, text=True, errors="replace")
    if r.returncode == 0:
        return True, ""
    for line in (r.stdout + r.stderr).splitlines():
        if "error" in line.lower():
            return False, line.split("msg=")[-1].strip()
    return False, "unknown"


def isolate(proxies, depth=0):
    """返回坏节点列表 [(proxy, err)]"""
    ok, err = test(proxies)
    if ok:
        return []
    if len(proxies) == 1:
        return [(proxies[0], err)]
    mid = len(proxies) // 2
    return isolate(proxies[:mid], depth + 1) + isolate(proxies[mid:], depth + 1)


c = sqlite3.connect("data/nodes.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("SELECT fp, clash FROM nodes ORDER BY fp")]
clean = []
for r in rows:
    s = normalize.sanitize(json.loads(r["clash"]))
    if s:
        clean.append((r["fp"], s))

print("待查", len(clean), "个节点")
bad = []
CH = 200
for start in range(0, len(clean), CH):
    part = [p for _, p in clean[start:start + CH]]
    if test(part)[0]:
        continue
    found = isolate(part)
    bad.extend(found)
    print(f"  批 {start//CH+1}: {len(found)} 个坏节点 -> {found[0][1][:80]}")

print(f"\n合计坏节点 {len(bad)}")
for p, err in bad[:10]:
    keep = {k: v for k, v in p.items() if k != "name"}
    print("\n---", err)
    print("   ", json.dumps(keep, ensure_ascii=False)[:400])
