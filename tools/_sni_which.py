"""决定性测试：mihomo 的 trojan 到底认 sni 还是 servername？

给同一批节点分别填一个**故意错误**的 SNI：
  D  用 servername 填错值
  E  用 sni 填错值
如果 D 全通、E 全挂 —— 说明 servername 被 mihomo 忽略，改名就是真 bug。
如果两者都挂 —— 说明两个字段都被识别，改名无害。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/opt/nodehub")

from nodehub import config, tester  # noqa: E402

DB = "/opt/nodehub/data/nodes.db"
ROOT = "/opt/nodehub"
BAD = "wrong-sni-probe-9f3k2.invalid"

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
    if len(picked) >= 10:
        break

D, E = [], []
for r, p in picked:
    fp = r["fp"]
    q = {k: v for k, v in p.items() if k not in ("sni", "servername")}
    q["servername"] = BAD
    D.append((fp, q))
    w = {k: v for k, v in p.items() if k not in ("sni", "servername")}
    w["sni"] = BAD
    E.append((fp, w))

cfg = config.load_config(os.path.join(ROOT, "config.yaml"), ROOT)
cfg = cfg.get("check", cfg)
exe = os.path.join(ROOT, "bin", "mihomo")

print(f"mihomo: {exe}")
for label, items in ((f"D servername={BAD}", D), (f"E sni={BAD}", E)):
    res = tester._run_chunk(items, cfg, ROOT, exe)
    ok = sum(1 for v in res.values() if v[0])
    print(f"\n{label}: {ok}/{len(items)} 通")
    for fp, (good, lat, err) in list(res.items())[:5]:
        print(f"   {'✓' if good else '✗'} {fp[:24]:26} {lat} {err}")
