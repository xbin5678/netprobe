"""从 VPS 取文件下来（SFTP），跟 vps.py 的 put 配一对。

用法:
  python tools/vps_get.py <远程路径> <本地路径>

凭据同样走环境变量：
  export VPS_HOST=1.2.3.4 VPS_PASS='密码' MSYS_NO_PATHCONV=1
"""
import os
import sys

import paramiko

HOST = os.environ.get("VPS_HOST", "")
USER = os.environ.get("VPS_USER", "root")
PASS = os.environ.get("VPS_PASS", "")
PORT = int(os.environ.get("VPS_PORT", "22"))

if not HOST or not PASS:
    sys.exit("请先设置环境变量 VPS_HOST 和 VPS_PASS")

remote, local = sys.argv[1], sys.argv[2]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30,
          banner_timeout=30, auth_timeout=30, allow_agent=False, look_for_keys=False)
try:
    sftp = c.open_sftp()
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    sftp.get(remote, local)
    sftp.close()
    print(f"downloaded {remote} -> {local} ({os.path.getsize(local)} bytes)")
finally:
    c.close()
