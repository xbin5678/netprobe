"""VPS 侧对同一批节点做 TCP 握手，和本机结果对照。"""
import json
import sys

sys.path.insert(0, "/opt/nodehub")
from nodehub import tester  # noqa: E402

raw = json.load(open("/tmp/raw18.json", encoding="utf-8"))
nodes = [{"fp": f"{r['server']}:{r['port']}", "server": r["server"],
          "port": r["port"]} for r in raw]
ok = tester.tcp_precheck(nodes, timeout=4.0, threads=18)
print("VPS TCP 握手成功:", len(ok), "/", len(nodes))
for n in nodes:
    k = n["fp"]
    print(("  ✓ %-32s %sms" % (k, ok[k])) if k in ok else ("  ✗ %-32s 超时/拒绝" % k))
