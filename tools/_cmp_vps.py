"""在 VPS 上跑同一组对比：原始 clash vs 线上导出的配置。

读 /tmp/raw18.json 和 /tmp/exp_speed.yaml，用 /opt/nodehub/bin/mihomo。
"""
import json
import os
import sys

sys.path.insert(0, "/opt/nodehub")

import yaml  # noqa: E402

from nodehub import normalize, tester  # noqa: E402

ROOT = "/opt/nodehub"
EXE = os.path.join(ROOT, "bin", "mihomo")

CFG = {
    "test_urls": ["https://www.gstatic.com/generate_204",
                  "https://cp.cloudflare.com/generate_204"],
    "timeout_ms": 5000,
    "threads": 12,
}

raw = json.load(open("/tmp/raw18.json", encoding="utf-8"))
exp = yaml.safe_load(open("/tmp/exp_speed.yaml", encoding="utf-8"))

raw_items = []
for r in raw:
    p = normalize.sanitize(json.loads(r["clash"]))
    if p:
        raw_items.append((f"raw:{r['server']}:{r['port']}", p))

exp_items = []
for p in exp["proxies"]:
    q = dict(p)
    exp_items.append((f"exp:{q['server']}:{q['port']}", q))

print(f"原始 {len(raw_items)} / 导出 {len(exp_items)}，内核存在={os.path.exists(EXE)}")

print("\n=== 原始 clash ===")
a = tester._run_chunk(raw_items, CFG, ROOT, EXE)
print("=== 线上导出配置 ===")
b = tester._run_chunk(exp_items, CFG, ROOT, EXE)

print("\n{:<30} {:>12} {:>12}".format("节点", "原始", "导出"))
print("-" * 58)
ok_a = ok_b = 0
for key, _ in raw_items:
    kb = key.replace("raw:", "exp:", 1)
    ra, rb = a.get(key), b.get(kb)
    sa = f"{ra[1]}ms" if ra and ra[0] else (ra[2] if ra else "?")
    sb = f"{rb[1]}ms" if rb and rb[0] else (rb[2] if rb else "?")
    ok_a += 1 if ra and ra[0] else 0
    ok_b += 1 if rb and rb[0] else 0
    mark = "" if (ra and ra[0]) == (rb and rb[0]) else "  <<< 不一致"
    print("{:<30} {:>12} {:>12}{}".format(key[4:], sa, sb, mark))
print("-" * 58)
print(f"原始可用 {ok_a}/{len(raw_items)}    导出可用 {ok_b}/{len(exp_items)}")
