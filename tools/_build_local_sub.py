"""把本机实测可用的节点取回完整数据，生成可直接导入的配置。

输入 .tmp/local_ok.json（本机真实 HTTP + 下载测速跑通的 fp 列表），
从 VPS 库里按 fp 取原始 clash，再走 exporter 生成 clash / base64 两份。
"""
import json
import os
import sys

import paramiko

HOST = os.environ["VPS_HOST"]
PASS = os.environ["VPS_PASS"]
USER = os.environ.get("VPS_USER", "root")

ok = json.load(open(".tmp/local_ok.json", encoding="utf-8"))
fps = [r["fp"] for r in ok]
print(f"本机实测可用 {len(fps)} 个")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASS, timeout=30,
          allow_agent=False, look_for_keys=False)

sftp = c.open_sftp()
sftp.put("tools/_fetch_by_fp.py", "/tmp/_fetch_by_fp.py")
sftp.close()

stdin, stdout, stderr = c.exec_command(
    "/opt/nodehub/.venv/bin/python /tmp/_fetch_by_fp.py")
stdin.write(json.dumps(fps))
stdin.channel.shutdown_write()
data = json.loads(stdout.read().decode())
err = stderr.read().decode()
c.close()
if err.strip():
    print("远端提示:", err.strip()[:200])
print(f"取回 {len(data)} 条完整节点")

# 按本机测出的速度排序
spd = {r["fp"]: r.get("speed") or 0 for r in ok}
data.sort(key=lambda r: -(spd.get(r["fp"]) or 0))

os.makedirs("out", exist_ok=True)

sys.path.insert(0, ".")
from nodehub import exporter  # noqa: E402


def name_fn(p, idx, n):
    v = spd.get(n["fp"]) or 0
    ms = n.get("latency") or 0
    tag = f"{v / 1048576:.2f}MB/s" if v >= 1048576 else f"{v / 1024:.0f}KB/s"
    return f"LOCAL-{idx:02d} {tag} {ms}ms"


clash_text = exporter.build_sub("clash", data, name_fn=name_fn)[0]
with open("out/local-ok-clash.yaml", "w", encoding="utf-8") as f:
    f.write(clash_text)

v2 = exporter.build_sub("v2ray", data, name_fn=name_fn)[0]
with open("out/local-ok-v2ray.txt", "w", encoding="utf-8") as f:
    f.write(v2)

print("已写出 out/local-ok-clash.yaml 与 out/local-ok-v2ray.txt")
