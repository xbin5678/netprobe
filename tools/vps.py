"""极简 SSH 助手：部署/更新时把代码推到远程用，不参与运行时。

凭据不写在代码里，用环境变量传：

    export VPS_HOST=1.2.3.4 VPS_USER=root VPS_PASS='你的密码'
    export MSYS_NO_PATHCONV=1   # Windows/Git Bash 必加，否则远程路径会被改写

用法:
  python tools/vps.py run "命令"                    执行远程命令
  python tools/vps.py put <本地文件> <远程路径>      上传单个文件
  python tools/vps.py putdir <本地目录> <远程目录>   递归上传（自动跳过 venv/bin 等）
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("VPS_HOST", "")
USER = os.environ.get("VPS_USER", "root")
PASS = os.environ.get("VPS_PASS", "")
PORT = int(os.environ.get("VPS_PORT", "22"))

if not HOST or not PASS:
    sys.exit("请先设置环境变量 VPS_HOST 和 VPS_PASS（VPS_USER 默认 root）")


def client() -> paramiko.SSHClient:
    last = None
    for i in range(4):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(HOST, port=PORT, username=USER, password=PASS,
                      timeout=30, banner_timeout=30, auth_timeout=30,
                      allow_agent=False, look_for_keys=False)
            return c
        except Exception as e:  # 国际链路偶尔抖动，重试
            last = e
            time.sleep(2 + i * 2)
    raise last


def run(cmd: str, timeout: int = 900) -> int:
    c = client()
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout, get_pty=False)
        for line in iter(out.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
        e = err.read().decode("utf-8", "replace")
        if e.strip():
            sys.stderr.write(e)
        rc = out.channel.recv_exit_status()
        return rc
    finally:
        c.close()


def put(local: str, remote: str) -> None:
    """上传单个文件。SFTP 走不通时自动退回 base64 + exec 通道。

    注意 Windows 上必须 `export MSYS_NO_PATHCONV=1`，否则 Git Bash 会把
    `/root/x` 这类参数改写成 `C:/.../PortableGit/root/x`，SFTP 报 ENOENT，
    看起来像服务端没开 sftp，其实是本地路径被吃了。
    """
    with open(local, "rb") as f:
        data = f.read()
    c = client()
    try:
        sftp = c.open_sftp()
        try:
            sftp.putfo(__import__("io").BytesIO(data), remote)
            return
        finally:
            sftp.close()
    except Exception as e:
        print(f"SFTP 不可用（{type(e).__name__}），改用 base64 通道", file=sys.stderr)
    finally:
        c.close()
    put_bytes(data, remote)


def put_bytes(data: bytes, remote: str) -> None:
    """base64 分块塞进 exec 通道写文件，不依赖 SFTP。"""
    import base64
    b64 = base64.b64encode(data).decode()
    run(f": > {remote}")
    for i in range(0, len(b64), 60000):
        run(f"printf %s {b64[i:i + 60000]} | base64 -d >> {remote}")
    run(f"chmod 644 {remote}")


def putdir(local: str, remote: str) -> None:
    c = client()
    try:
        sftp = c.open_sftp()

        def mkdirs(path: str) -> None:
            parts = path.strip("/").split("/")
            cur = ""
            for p in parts:
                cur += "/" + p
                try:
                    sftp.stat(cur)
                except IOError:
                    sftp.mkdir(cur)

        mkdirs(remote)
        base = os.path.abspath(local)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in
                       {".git", ".venv", "__pycache__", "node_modules", ".workbuddy-ai"}]
            rel = os.path.relpath(root, base).replace("\\", "/")
            rdir = remote if rel == "." else remote + "/" + rel
            mkdirs(rdir)
            for f in files:
                if f.endswith((".pyc", ".log")):
                    continue
                sftp.put(os.path.join(root, f), rdir + "/" + f)
        sftp.close()
    finally:
        c.close()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "run":
        sys.exit(run(sys.argv[2]))
    elif action == "put":
        put(sys.argv[2], sys.argv[3])
        print("uploaded", sys.argv[3])
    elif action == "putdir":
        putdir(sys.argv[2], sys.argv[3])
        print("uploaded dir", sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
