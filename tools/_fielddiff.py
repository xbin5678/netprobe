"""逐字段对比：库里原始 clash 配置 vs 线上导出的配置。

只比协议字段（type/server/port/password/cipher/ws-opts...），
name 是故意加速度后缀的，单独列为「预期差异」。
"""
import json
import sys

import yaml

raw = json.load(open(".tmp/raw18.json", encoding="utf-8"))
exp = yaml.safe_load(open(".tmp/export_now.yaml", encoding="utf-8"))
proxies = exp.get("proxies") or []

# 导出按 server:port 建索引
by_sp = {}
for p in proxies:
    by_sp[(str(p.get("server")), int(p.get("port") or 0))] = p

IGNORE = {"name"}


def canon(v):
    """把值规范化成可比较的形式：dict 递归、bool/str 严格区分。"""
    if isinstance(v, dict):
        return {k: canon(x) for k, x in sorted(v.items()) if k not in IGNORE}
    if isinstance(v, list):
        return [canon(x) for x in v]
    return v


total = 0
exact = 0
diff_nodes = []
missing = []

for r in raw:
    sp = (str(r.get("server")), int(r.get("port") or 0))
    e = by_sp.get(sp)
    if e is None:
        missing.append(sp)
        continue
    try:
        orig = yaml.safe_load(r.get("clash") or "{}")
    except Exception as ex:
        print("原始 clash 解析失败", sp, ex)
        continue
    total += 1
    a, b = canon(orig), canon(e)

    keys_a, keys_b = set(a), set(b)
    only_a = keys_a - keys_b
    only_b = keys_b - keys_a
    changed = {k for k in (keys_a & keys_b) if a[k] != b[k]}

    if not only_a and not only_b and not changed:
        exact += 1
    else:
        diff_nodes.append((sp, r.get("type"), only_a, only_b, changed,
                           {k: (a.get(k), b.get(k)) for k in changed}))

print(f"参与对比节点: {total}")
print(f"逐字段完全一致: {exact}")
print(f"有差异: {len(diff_nodes)}")
print(f"导出里找不到的: {len(missing)}")
for sp in missing:
    print("  缺:", sp)

for sp, t, oa, ob, ch, kv in diff_nodes:
    print(f"\n[{t}] {sp[0]}:{sp[1]}")
    if oa:
        print("  原始多出:", sorted(oa))
    if ob:
        print("  导出多出:", sorted(ob))
    for k, (x, y) in kv.items():
        print(f"  变更 {k}: {x!r} -> {y!r}")

# 名字对比（预期有速度后缀）
print("\n--- 节点名对比（预期差异）---")
for r in raw[:6]:
    sp = (str(r.get("server")), int(r.get("port") or 0))
    e = by_sp.get(sp)
    if e:
        print(f"  {r.get('name')!r}  ->  {e.get('name')!r}")
