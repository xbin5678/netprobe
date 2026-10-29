import sys, json
sys.path.insert(0, '/opt/nodehub')
from nodehub.store import Store

st = Store('/opt/nodehub/data/nodes.db')
rows = st.alive_nodes(0, tier='speed')
out = []
for r in rows:
    out.append({k: r.get(k) for k in
                ('fp', 'type', 'server', 'port', 'name', 'uri', 'clash',
                 'latency', 'speed', 'ttfb', 'alive', 'ok_count')})
sys.stderr.write('rows=%d\n' % len(out))
print(json.dumps(out, ensure_ascii=False))
