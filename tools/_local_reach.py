"""本机对 alive 档做 TCP 可达性筛选，把可达的 server:port 存下来。

用法: python tools/_local_reach.py
读 .tmp/alive722.json，写 .tmp/reach_local.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodehub import tester  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(os.path.join(ROOT, ".tmp", "alive722.json"), encoding="utf-8"))
nodes = [{"fp": f"{x['server']}:{x['port']}", "server": x["server"],
          "port": x["port"]} for x in d]
print(f"本机对 {len(nodes)} 个节点做 TCP 握手…")
ok = tester.tcp_precheck(nodes, timeout=4.0, threads=200)
print(f"本机可达 {len(ok)} / {len(nodes)}")

out = [{"server": k.rsplit(":", 1)[0], "port": int(k.rsplit(":", 1)[1]),
        "tcp_ms": v} for k, v in sorted(ok.items(), key=lambda t: t[1])]
json.dump(out, open(os.path.join(ROOT, ".tmp", "reach_local.json"), "w",
                    encoding="utf-8"), ensure_ascii=False)
print("已写 .tmp/reach_local.json")
