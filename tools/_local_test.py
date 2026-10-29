"""在本机对「本地 TCP 可达」的节点做真实 HTTP 测试 + 下载测速。

用法: python tools/_local_test.py [测速个数]
读 .tmp/reach164.json，输出 .tmp/local_ok.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodehub import normalize, probe, tester  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "bin", "mihomo.exe")
TOPN = int(sys.argv[1] or 15)

rows = json.load(open(os.path.join(ROOT, ".tmp", "reach164.json"), encoding="utf-8"))
# 去重（同一个 server:port 可能有多条）
seen, uniq = set(), []
for r in rows:
    k = (r["server"], r["port"])
    if k in seen:
        continue
    seen.add(k)
    uniq.append(r)
rows = uniq
print(f"本地 TCP 可达 {len(rows)} 个，开始真实 HTTP 测试…")

cfg = {"test_urls": ["https://www.gstatic.com/generate_204",
                     "https://cp.cloudflare.com/generate_204"],
       "timeout_ms": 5000, "threads": 16, "chunk": 60}
t0 = time.time()
res = tester.real_test(rows, cfg, ROOT, EXE)
ok = [r for r in rows if res.get(r["fp"], (False,))[0]]
print(f"真实可用 {len(ok)} / {len(rows)}（{time.time()-t0:.0f}s）")
for r in ok[:10]:
    good, lat, _ = res[r["fp"]]
    print(f"  ✓ {r['type']:<8} {r['server']}:{r['port']:<6} {lat}ms")

if ok and TOPN:
    ok.sort(key=lambda r: res[r["fp"]][1] or 99999)
    cand = ok[:TOPN]
    print(f"\n给最快的 {len(cand)} 个测下载速度（10MB）…")
    sp = probe.speed_rows(cand, {}, ROOT, EXE, size_mb=10, max_sec=12, chunk=10,
                          on_one=lambda i, n, r: print(
                              f"  {i}/{n} " + (f"{r[0]/1048576:6.2f} MB/s" if r[0]
                                              else f"失败 {r[2]}")))
    got = [(r, sp.get(r["fp"], (None, None, ""))[0]) for r in cand]
    got = [(r, s) for r, s in got if s]
    print(f"测出速度 {len(got)} 个")
    for r, s in got:
        print(f"  ✓ {r['type']:<8} {r['server']}:{r['port']:<6} {s/1048576:6.2f} MB/s")
else:
    got = []

out = [{"fp": r["fp"], "type": r["type"], "server": r["server"], "port": r["port"],
        "latency": res[r["fp"]][1], "speed": s} for r, s in got]
json.dump(out, open(os.path.join(ROOT, ".tmp", "local_ok.json"), "w",
                    encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n已写 .tmp/local_ok.json（{len(out)} 个真正能用）")
