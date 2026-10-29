"""按 server:port 从 VPS 节点库取出完整节点数据（供本机复测）。

读 /tmp/reach_local.json -> 输出 JSON 数组
"""
import json
import sys

sys.path.insert(0, "/opt/nodehub")
from nodehub.store import Store  # noqa: E402

want = json.load(open("/tmp/reach_local.json", encoding="utf-8"))
keys = {(w["server"], int(w["port"])): w.get("tcp_ms") for w in want}

st = Store("/opt/nodehub/data/nodes.db")
rows = st.alive_nodes(0, tier="alive")
out = []
for r in rows:
    k = (r["server"], int(r["port"]))
    if k in keys:
        out.append({kk: r.get(kk) for kk in
                    ("fp", "type", "server", "port", "name", "uri", "clash",
                     "latency", "speed")})
sys.stderr.write("matched=%d / want=%d\n" % (len(out), len(keys)))
print(json.dumps(out, ensure_ascii=False))
