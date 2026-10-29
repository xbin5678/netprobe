"""可用性检测：TCP 预筛 + mihomo 真连测试。"""
from __future__ import annotations

import gzip
import json
import os
import platform
import random
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from urllib.parse import quote

from . import normalize, yamlutil

import requests
import yaml  # noqa: F401  (保留给外部依赖)

MIHOMO_VER = "v1.19.33"
RELEASE_MIRRORS = [
    "https://github.com/MetaCubeX/mihomo/releases/download/{ver}/{asset}",
    "https://ghfast.top/https://github.com/MetaCubeX/mihomo/releases/download/{ver}/{asset}",
    "https://gh-proxy.com/https://github.com/MetaCubeX/mihomo/releases/download/{ver}/{asset}",
]


def _target() -> tuple[str, str, str]:
    """按当前平台选内核资产，返回 (资产名, 解压后文件名, 压缩格式)。

    Linux/macOS 的发布包是 .gz，Windows 是 .zip，名字也带平台前缀——
    以前这里写死 windows-amd64，放到 VPS 上必然下不动。
    """
    m = platform.machine().lower()
    if m in ("aarch64", "arm64"):
        arch = "arm64"
    elif m in ("i386", "i686", "x86"):
        arch = "386"
    else:
        arch = "amd64"

    if sys.platform == "win32":
        return f"mihomo-windows-{arch}-compatible-{MIHOMO_VER}.zip", "mihomo.exe", "zip"
    if sys.platform == "darwin":
        return f"mihomo-darwin-{arch}-{MIHOMO_VER}.gz", "mihomo", "gz"
    # linux：amd64 用 compatible 版，老 CPU 也能跑；arm64 没有 compatible 变体
    suffix = "-compatible" if arch in ("amd64", "386") else ""
    return f"mihomo-linux-{arch}{suffix}-{MIHOMO_VER}.gz", "mihomo", "gz"


# ---------------------------------------------------------------- mihomo 内核

def ensure_mihomo(cfg: dict, root: str) -> str | None:
    """返回 mihomo 可执行文件路径，不存在则自动下载。"""
    custom = (cfg.get("mihomo_path") or "").strip()
    if custom:
        return custom if os.path.exists(custom) else None

    asset, exe_name, kind = _target()
    exe = os.path.join(root, "bin", exe_name)
    if os.path.exists(exe):
        return exe

    bin_dir = os.path.join(root, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    pkg_path = os.path.join(bin_dir, asset)

    for tpl in RELEASE_MIRRORS:
        url = tpl.format(ver=MIHOMO_VER, asset=asset)
        try:
            print(f"  ↓ 下载 mihomo 内核: {url.split('/')[2]} ({asset})")
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    continue
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(pkg_path, "wb") as f:
                    for chunk in r.iter_content(262144):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            print(f"\r    {pct:3d}%  {done/1048576:.1f}MB", end="", flush=True)
                print()
            if os.path.getsize(pkg_path) < 3_000_000:
                continue
            if kind == "zip":
                with zipfile.ZipFile(pkg_path) as z:
                    for n in z.namelist():
                        if n.lower().endswith(".exe"):
                            with z.open(n) as src, open(exe, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            break
            else:
                with gzip.open(pkg_path, "rb") as src, open(exe, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.remove(pkg_path)
            if os.path.exists(exe):
                print(f"  ✓ mihomo 就绪: {exe}")
                return exe
        except Exception as e:
            print(f"  ! 下载失败({type(e).__name__})，换镜像重试")
            try:
                os.remove(pkg_path)
            except Exception:
                pass
            continue
    return None


# ---------------------------------------------------------------- 阶段一：TCP 预筛

def _is_ip(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        pass
    return ":" in host          # 粗略当作 IPv6


def resolve_hosts(hosts: list[str], threads: int = 200, deadline: float = 25.0) -> dict:
    """并发解析域名，超时的不等。

    坑：socket.create_connection 的 timeout 只作用于 connect，不管 DNS。
    域名多的时候 getaddrinfo 能卡好几分钟，TCP 预筛会莫名其妙变慢 10 倍。
    """
    out: dict[str, str | None] = {}
    lock = threading.Lock()
    hosts = [h for h in set(hosts) if not _is_ip(h)]
    if not hosts:
        return out

    def work(h):
        try:
            infos = socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM)
            ip = infos[0][4][0] if infos else None
        except Exception:
            ip = None
        with lock:
            out[h] = ip

    ex = ThreadPoolExecutor(max_workers=min(threads, len(hosts)))
    try:
        futs = [ex.submit(work, h) for h in hosts]
        done, _ = wait(futs, timeout=deadline)
        # 没解析完的当作解析失败，不再等
        with lock:
            for h in hosts:
                out.setdefault(h, None)
    finally:
        ex.shutdown(wait=False)
    return out


def tcp_precheck(nodes: list[dict], timeout: float = 3.0, threads: int = 200) -> dict:
    """返回 {fp: 连接耗时ms}，只包含 TCP 能连上的。"""
    result: dict[str, int] = {}
    lock = threading.Lock()

    # 先把域名批量解析掉，避免在连接阶段被 DNS 卡住
    dns = resolve_hosts([n["server"] for n in nodes], threads=threads)

    def probe(n):
        host, port = n["server"], int(n["port"])
        if not _is_ip(host):
            ip = dns.get(host)
            if not ip:
                return          # 解析失败直接判死，不去等 connect 超时
            host = ip
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                ms = int((time.time() - t0) * 1000)
                with lock:
                    result[n["fp"]] = ms
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(probe, nodes))
    return result


# ---------------------------------------------------------------- 阶段二：mihomo 真连

def _wait_api(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _test_one(name: str, port: int, urls: list[str], timeout_ms: int):
    last = "timeout"
    for u in urls:
        try:
            r = requests.get(
                f"http://127.0.0.1:{port}/proxies/{quote(name, safe='')}/delay",
                params={"timeout": timeout_ms, "url": u},
                timeout=timeout_ms / 1000 + 3,
            )
            if r.status_code == 200:
                d = r.json().get("delay")
                if isinstance(d, int) and d > 0:
                    return True, d, ""
            else:
                try:
                    msg = r.json().get("message", "")[:80]
                except Exception:
                    msg = f"HTTP {r.status_code}"
                # 记下来：以前所有失败都报 timeout，配置写错（比如 cipher 不认）
                # 也显示成超时，排查时完全看不出区别
                last = msg or last
                if "timeout" in msg.lower() or "refused" in msg.lower():
                    continue
        except Exception as e:
            last = type(e).__name__
            continue
    return False, None, last[:80]


def _run_chunk(items: list[tuple[str, dict]], cfg: dict, root: str, exe: str,
               depth: int = 0) -> dict:
    """items: [(fp, proxy_dict)] -> {fp: (ok, latency, err)}"""
    port = random.randint(20000, 45000)
    workdir = os.path.join(root, "data", "mihomo")
    os.makedirs(workdir, exist_ok=True)

    proxies = []
    name_map = {}
    for i, (fp, p) in enumerate(items):
        nm = f"n{i:05d}"
        q = {k: v for k, v in p.items() if k != "name"}
        q["name"] = nm
        proxies.append(q)
        name_map[nm] = fp

    conf = {
        "mixed-port": port + 1,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{port}",
        "geo-auto-update": False,
        "geodata-mode": False,
        "proxies": proxies,
        "rules": ["MATCH,DIRECT"],
    }
    cfg_path = os.path.join(workdir, f"test_{port}.yaml")
    # 必须用 yamlutil：REALITY 的 short-id 如果是 "08" 这种值，裸着写会被
    # mihomo 当成数字，整批节点一起加载失败
    yamlutil.dump_to(cfg_path, conf)

    proc = None
    out: dict = {}
    try:
        creation = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [exe, "-d", workdir, "-f", cfg_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        if not _wait_api(port):
            # mihomo 加载配置是全有或全无的：只要有一个节点字段不合法
            # （cipher 拼错、REALITY 参数坏……），整份配置加载失败。
            # 不能因此把整批 200 个都判死，二分下去把坏的挑出来，
            # 剩下的照常测。正常情况不会走到这里（入库和导出都已消毒）。
            if len(items) > 1 and depth < 10:
                mid = len(items) // 2
                left = _run_chunk(items[:mid], cfg, root, exe, depth + 1)
                right = _run_chunk(items[mid:], cfg, root, exe, depth + 1)
                left.update(right)
                return left
            return {fp: (False, None, "bad_config") for fp, _ in items}

        urls = cfg.get("test_urls") or ["http://www.gstatic.com/generate_204"]
        timeout_ms = int(cfg.get("timeout_ms", 5000))
        threads = max(1, int(cfg.get("threads", 24)))

        def work(nm):
            return nm, _test_one(nm, port, urls, timeout_ms)

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(work, nm) for nm in name_map]
            for fu in as_completed(futs):
                nm, res = fu.result()
                out[name_map[nm]] = res
        return out
    except Exception as e:
        return {fp: (False, None, f"chunk_err:{type(e).__name__}") for fp, _ in items}
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            os.remove(cfg_path)
        except Exception:
            pass


def real_test(nodes: list[dict], cfg: dict, root: str, exe: str,
              chunk: int | None = None) -> dict:
    """返回 {fp: (ok, latency, err)}

    chunk 可用配置里的 `check.real.chunk` 覆盖——低配机一次灌 250 个节点
    给内核，内存会直接顶上去；60-80 个更稳。
    """
    if chunk is None:
        chunk = int(cfg.get("chunk", 0) or 0) or 250
    items = []
    dropped = 0
    for n in nodes:
        # 老库里可能还留着消毒前入库的脏节点，检测前再过一遍
        p = normalize.sanitize(json.loads(n["clash"]))
        if p is None:
            dropped += 1
            continue
        items.append((n["fp"], p))
    if dropped:
        print(f"  跳过 {dropped} 个字段不合法的节点")
    out: dict = {}
    total = len(items)
    for start in range(0, total, chunk):
        part = items[start:start + chunk]
        print(f"  真连测试 {start+1}-{min(start+chunk,total)} / {total} ...", end="", flush=True)
        res = _run_chunk(part, cfg, root, exe)
        ok = sum(1 for v in res.values() if v[0])
        print(f" 通过 {ok}")
        out.update(res)
    return out
