"""跨进程任务锁。

定时器（systemd timer）和网页按钮都可能触发 run.py，两者撞一起会互相抢带宽、
抢 mihomo 端口，还会把同一批节点测两遍。用文件锁把它们串起来：

    guard = Guard(path)
    if not guard.acquire():
        print("已有任务在跑"); sys.exit(75)

锁随进程退出自动释放（flock 语义），所以不会留下僵尸锁。
Windows 上没有 fcntl，退化成「写 pid 文件 + 探活」，够用。
"""
from __future__ import annotations

import os

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 会真的去终止进程，改用 OpenProcess 探活。
        # 拿不到句柄 = 进程不存在；拿到句柄再看退出码是否还是 STILL_ACTIVE。
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                               ctypes.POINTER(wintypes.DWORD))
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259               # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            # 探活本身失败时宁可当成「活着」，免得两个任务同时跑
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_pid(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def holder(path: str) -> int | None:
    """谁在持锁？没人持锁返回 None。

    判断方式是「试着抢一下」：抢到了说明没人在跑（顺手释放），抢不到才去读 pid。
    这样即使进程被 kill -9 留下过期的 pid 文件，也不会误判。
    """
    if not os.path.exists(path):
        return None
    try:
        f = open(path, "r+", encoding="utf-8")
    except Exception:
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.seek(0)
                pid = int((f.read() or "0").strip() or 0)
                return pid if pid and _alive(pid) else -1
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return None
        pid = _read_pid(path)
        return pid if pid and _alive(pid) else None
    finally:
        try:
            f.close()
        except Exception:
            pass


class Guard:
    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        f = open(self.path, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._fh = f
        return True

    def release(self) -> None:
        f, self._fh = self._fh, None
        if not f:
            return
        try:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
