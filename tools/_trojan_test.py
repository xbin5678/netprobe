"""在 VPS 上验证：trojan 的 SNI 字段被改名成 servername 后还能不能用。

对同一批 trojan 节点（sni != server 的 CDN 前置节点）跑三份配置：
  A 原始 clash（带 sni）
  B 消毒后（servername，即现在导出的样子）
  C 原始 clash 但手工把 sni 换成 servername（复现 B，排除其他字段干扰）
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/opt/nodehub")

from nodehub import normalize, tester, config  # noqa: E402

DB = "/opt/nodehub/data/nodes.db"
ROOT = "/opt/nodehub"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute(
    "SELECT fp,server,port,clash FROM nodes WHERE type='trojan' AND alive=1")]
c.close()

picked = []
for r in rows:
    try:
        p = json.loads(r["clash"] or "{}")
    except Exception:
        continue
    sni = p.get("sni") or p.get("servername") or ""
    if sni and sni != r["server"]:
        picked.append((r, p))
    if len(picked) >= 12:
        break

print(f"挑出 {len(picked)} 个 sni!=server 的 trojan 节点")

A, B, C = [], [], []
for r, p in picked:
    fp = r["fp"]
    A.append((fp, dict(p)))
    n = normalize.sanitize(dict(p)) or dict(p)
    B.append((fp, n))
    q = dict(p)
    q["servername"] = q.pop("sni", "")
    C.append((fp, q))

print("\nB 组（消毒后）实际字段：")
print(json.dumps(B[0][1], ensure_ascii=False, indent=2))

cfg = config.load_config(os.path.join(ROOT, "config.yaml"), ROOT)
cfg = cfg.get("check", cfg)
exe = os.path.join(ROOT, "bin", "mihomo")

for label, items in (("A 原始 sni", A), ("B 消毒后 servername", B),
                     ("C 手改 servername", C)):
    res = tester._run_chunk(items, cfg, ROOT, exe)
    ok = sum(1 for v in res.values() if v[0])
    print(f"\n{label}: {ok}/{len(items)} 通")
    for fp, (good, lat, err) in list(res.items())[:6]:
        print(f"   {'✓' if good else '✗'} {fp[:24]:26} {lat} {err}")
