"""xhttp-opts 保留 vs 丢弃，对真实可连性的影响。

同一批 xhttp 节点，跑两遍真连测试：
  1. 新版 sanitize（保留 xhttp-opts，缺省补 path=/）
  2. 模拟旧版（把 xhttp-opts 整段摘掉）
看通过数量差多少。
"""
import json
import sqlite3
import sys

sys.path.insert(0, "/opt/nodehub")
from nodehub import normalize, tester  # noqa: E402

ROOT, EXE = "/opt/nodehub", "/opt/nodehub/bin/mihomo"
cfg = {"test_urls": ["http://www.gstatic.com/generate_204"],
       "timeout_ms": 5000, "threads": 24, "chunk": 40}

c = sqlite3.connect("/opt/nodehub/data/nodes.db")
nodes = []
for fp, clash in c.execute("select fp, clash from nodes"):
    try:
        p = json.loads(clash or "{}")
    except Exception:
        continue
    if str(p.get("network") or "").lower() == "xhttp":
        nodes.append({"fp": fp, "clash": clash})
print(f"xhttp 节点 {len(nodes)} 个")

print("\n=== 新版（保留 xhttp-opts）")
r1 = tester.real_test(nodes, cfg, ROOT, EXE)
ok1 = {k for k, v in r1.items() if v[0]}

orig = normalize.sanitize


def _old(p):
    q = orig(p)
    if q:
        q.pop("xhttp-opts", None)
    return q


normalize.sanitize = _old
print("\n=== 旧版（丢掉 xhttp-opts）")
r2 = tester.real_test(nodes, cfg, ROOT, EXE)
ok2 = {k for k, v in r2.items() if v[0]}
normalize.sanitize = orig

print(f"\n新版通过 {len(ok1)}，旧版通过 {len(ok2)}")
print(f"新版多出来 {len(ok1 - ok2)} 个，旧版独有 {len(ok2 - ok1)} 个")
for k in list(ok1 - ok2)[:5]:
    print(f"  新版可用 {r1[k][1]}ms  {k[:8]}")
