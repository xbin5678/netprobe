"""对比实验：同一批节点，「原始 clash」和「导出配置」在**本机网络**下谁能连。

用法: python tools/_cmp_local.py
读 .tmp/raw18.json（线上原始）和 .tmp/exp_speed.yaml（线上导出）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from nodehub import normalize, tester  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "bin", "mihomo.exe")

CFG = {
    "test_urls": ["https://www.gstatic.com/generate_204",
                  "https://cp.cloudflare.com/generate_204"],
    "timeout_ms": 5000,
    "threads": 12,
}

raw = json.load(open(os.path.join(ROOT, ".tmp", "raw18.json"), encoding="utf-8"))
exp = yaml.safe_load(open(os.path.join(ROOT, ".tmp", "exp_speed.yaml"), encoding="utf-8"))

# ① 原始：直接从库里的 clash 字段还原
raw_items = []
for r in raw:
    p = normalize.sanitize(json.loads(r["clash"]))
    if p:
        raw_items.append((f"raw:{r['server']}:{r['port']}", p))

# ② 导出：直接拿线上导出的那份 proxies
exp_items = []
for p in exp["proxies"]:
    q = dict(p)
    exp_items.append((f"exp:{q['server']}:{q['port']}", q))

print(f"原始 {len(raw_items)} 个 / 导出 {len(exp_items)} 个，内核 {EXE}")
if not os.path.exists(EXE):
    sys.exit("找不到 mihomo.exe")

print("\n=== 测「原始 clash」 ===")
a = tester._run_chunk(raw_items, CFG, ROOT, EXE)
print("\n=== 测「线上导出的配置」 ===")
b = tester._run_chunk(exp_items, CFG, ROOT, EXE)

print("\n{:<28} {:>10} {:>10}".format("节点", "原始", "导出"))
print("-" * 52)
ok_a = ok_b = 0
for key in [k for k, _ in raw_items]:
    ka = key
    kb = key.replace("raw:", "exp:", 1)
    ra, rb = a.get(ka), b.get(kb)
    sa = f"{ra[1]}ms" if ra and ra[0] else (ra[2] if ra else "?")
    sb = f"{rb[1]}ms" if rb and rb[0] else (rb[2] if rb else "?")
    ok_a += 1 if ra and ra[0] else 0
    ok_b += 1 if rb and rb[0] else 0
    mark = "" if (ra and ra[0]) == (rb and rb[0]) else "  <<< 不一致"
    print("{:<28} {:>10} {:>10}{}".format(key[4:], sa, sb, mark))

print("-" * 52)
print(f"原始可用 {ok_a}/{len(raw_items)}    导出可用 {ok_b}/{len(exp_items)}")
