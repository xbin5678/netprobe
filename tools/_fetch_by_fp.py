import json
import sys

import sqlite3

fps = json.loads(sys.stdin.read())
c = sqlite3.connect("/opt/nodehub/data/nodes.db")
c.row_factory = sqlite3.Row
q = ",".join("?" * len(fps))
rows = [dict(r) for r in c.execute(
    f"SELECT fp,clash,uri,latency,speed FROM nodes WHERE fp IN ({q})", fps)]
sys.stdout.write(json.dumps(rows))
