"""把 VPS 上 alive 档节点的 server/port 导出来，供本机做 TCP 可达性统计。"""
import json
import sys

sys.path.insert(0, "/opt/nodehub")
from nodehub.store import Store  # noqa: E402

st = Store("/opt/nodehub/data/nodes.db")
rows = st.alive_nodes(0, tier="alive")
out = [{"server": r["server"], "port": r["port"], "type": r["type"],
        "speed": r.get("speed"), "latency": r.get("latency")} for r in rows]
sys.stderr.write("alive=%d\n" % len(out))
print(json.dumps(out, ensure_ascii=False))
