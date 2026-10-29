"""给每个源补测小样本，让通过率统计有数据。

只挑「真连测过 <10 个」的源，每个最多测 30 个节点，结果写回库。
跑完 sources_tool.py doc 就能看到真实的通过率排序。
"""
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from nodehub.store import Store
from nodehub import tester

SAMPLE = 30

cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
real_cfg = (cfg.get("check") or {}).get("real") or {}

store = Store(os.path.join(ROOT, "data", "nodes.db"))
conn = sqlite3.connect(os.path.join(ROOT, "data", "nodes.db"))
conn.row_factory = sqlite3.Row

q = """SELECT s.name,
              COUNT(DISTINCT n.fp) AS tested
       FROM nodes n
       JOIN node_sources ns ON ns.fp = n.fp
       JOIN sources s ON s.id = ns.source_id
       WHERE n.last_check IS NOT NULL AND n.last_error != 'tcp_unreachable'
       GROUP BY s.name"""
tested = {r["name"]: r["tested"] for r in conn.execute(q)}

q2 = """SELECT s.name FROM sources s"""
all_src = [r["name"] for r in conn.execute(q2)]

need = [n for n in all_src if tested.get(n, 0) < 10]
print(f"缺数据的源 {len(need)} 个，每个补测最多 {SAMPLE} 个\n")

random.seed(42)
total_ok = 0
for name in need:
    rows = [dict(r) for r in conn.execute(
        """SELECT n.* FROM nodes n JOIN node_sources ns ON ns.fp = n.fp
           JOIN sources s ON s.id = ns.source_id WHERE s.name = ?""", (name,))]
    if not rows:
        print(f"  {name:<34} 无节点")
        continue
    sample = random.sample(rows, min(SAMPLE, len(rows)))
    ok = tester.tcp_precheck(sample, 3.0, 120)
    cand = [n for n in sample if n["fp"] in ok]
    for n in sample:
        if n["fp"] not in ok:
            store.mark(n["fp"], False, None, "tcp", "tcp_unreachable")
    if not cand:
        print(f"  {name:<34} 样本 {len(sample):>3}  TCP 全不通")
        continue
    res = tester.real_test(cand, real_cfg, ROOT, os.path.join(ROOT, "bin", "mihomo.exe"),
                           chunk=200)
    passed = 0
    for n in cand:
        good, lat, err = res.get(n["fp"], (False, None, "no_result"))
        if good:
            passed += 1
            store.mark(n["fp"], True, lat, "sample")
        else:
            store.mark(n["fp"], False, None, "sample", err)
    total_ok += passed
    print(f"  {name:<34} 样本 {len(sample):>3}  TCP {len(cand):>3}  通过 {passed:>3}"
          f"  ({passed * 100 // max(len(cand), 1)}%)")

print(f"\n补测完成，新增可用节点 {total_ok} 个")
