#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据采集流水线 —— 网页版

只用标准库起服务（PyYAML 是项目本来就有的），不装 Flask / FastAPI / uvicorn，
单进程常驻内存约 35-45MB。流水线本身还是跑 run.py 子进程，
所以网页挂了不影响任务，任务挂了网页还在。

    python web.py                             # 读 config.yaml 的 web 段
    python web.py --port 8787 --token abc123  # 临时指定
    python web.py --no-auth                   # 只在内网/本机用

接口一览：
    GET  /                    单页界面（没 token 时给登录页）
    GET  /healthz             存活探针，免鉴权
    GET  /api/status          运行状态 + 库内统计
    GET  /api/stream          SSE：日志 / 进度 / 完成事件
    GET  /api/log?since=N     拉日志尾巴（SSE 被反代挡住时的兜底）
    GET  /api/sources         数据源质量表
    GET  /api/nodes           可用节点，支持 proto/limit/lat 筛选
    GET  /api/export          按同一套筛选导出 clash / yaml / base64 / 明文
    GET  /api/report          本次报告 markdown
    GET  /api/sub             订阅页要的信息：密钥、各格式链接、节点数
    GET  /sub/<key>/<fmt>     订阅本体，免 Cookie（key 本身就是凭证）
    GET  /download/<file>     下载 output 里的成品
    POST /api/run             启动流水线  {"cmd":"all","extra":["--limit","500"]}
    POST /api/stop            中断
    POST /api/sub/reset       换一个新的订阅密钥（旧链接立刻失效）
    GET  /api/schedule        后台任务排期 + systemd timer 状态
    POST /api/schedule        改排期 {enabled,mode,at,every_hours}
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from nodehub import exporter, probe as probedb, schedule, yamlutil  # noqa: E402
from nodehub.config import load_config, source_meta  # noqa: E402
from nodehub.lock import holder as lock_holder  # noqa: E402
from nodehub.progress import parse as parse_progress  # noqa: E402
from nodehub.store import Store  # noqa: E402

INDEX_PATH = os.path.join(ROOT, "webui", "index.html")
PROBE_PATH = os.path.join(ROOT, "webui", "probe.html")
OUTPUT_FILES = {"alive_clash.yaml", "alive_sub.txt", "alive.txt", "report.md"}
CMDS = {"all", "collect", "check", "verify", "manage", "stats", "speed"}
# 订阅测速是另一条线，命令单独放行，免得主流水线也能被塞 probe 参数
PROBE_CMDS = {"probe"}
FLAGS = {"--unchecked", "--full", "--no-real", "--no-verify", "--fast", "--slow",
         "--no-speed"}
VALUE_FLAGS = {"--limit", "--top", "--run", "--size", "--max-speed"}
# 手动检测 / 订阅测速用的参数，值不是数字，单独校验格式
EXTRA_FLAGS = {"--fp", "--type", "--scope", "--mode", "--ids"}
FP_RE = re.compile(r"^[0-9a-f,]{1,20000}$")
TYPE_RE = re.compile(r"^[a-z0-9,]{1,200}$")
IDS_RE = re.compile(r"^[0-9,]{1,6000}$")
MODES = ("fast", "deep", "speed", "auto")
SPLIT_RE = re.compile(rb"[\r\n]")

LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据采集流水线 · 登录</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6fa;
  font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:#1f2937}
form{background:#fff;border:1px solid #e3e8f0;border-radius:14px;padding:30px 28px;width:min(92vw,360px);
  box-shadow:0 8px 28px rgba(31,41,55,.06)}
h1{margin:0 0 4px;font-size:17px}
p{margin:0 0 18px;color:#6b7280;font-size:13px}
input{width:100%;padding:11px 12px;border:1px solid #d8dfea;border-radius:9px;font-size:15px;outline:none}
input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:9px;background:#2563eb;color:#fff;
  font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#1d4ed8}
.tip{margin-top:14px;color:#9aa3af;font-size:12px;line-height:1.5}
</style>
<form method="post" action="/login">
  <h1>数据采集流水线</h1>
  <p>输入访问令牌（web.token 或启动时打印的那个）</p>
  <input name="token" type="password" autofocus autocomplete="current-password" placeholder="访问令牌">
  <button type="submit">进入</button>
  <div class="tip">令牌存在 data/web_token.txt，也可以在 config.yaml 的 web.token 里固定写死。</div>
</form></html>"""


def log(msg: str) -> None:
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- 运行管理器

class Runner:
    """管着 run.py 子进程：启动、读日志、广播 SSE、停止。

    主流水线和订阅测速各用一个实例：锁文件、状态文件、命令白名单都分开，
    这样在网页上点测速不会把主流水线的日志冲掉，也不会互相卡住。
    """

    def __init__(self, app: "App", lock_name: str = "run.lock",
                 state_name: str = "web_state.json", cmds=None):
        self.app = app
        self.lock_name = lock_name
        self.state_name = state_name
        # 只有主流水线才去接定时任务的结果（测速页有自己的记录，别串味）
        self.adopt_sched = state_name == "web_state.json"
        self.cmds = set(cmds) if cmds else set(CMDS)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.subs: set[queue.Queue] = set()
        self.events: deque[dict] = deque(maxlen=400)
        self.seq = 0
        self.state = {
            "running": False, "cmd": None, "args": [],
            "started": None, "elapsed": 0.0, "pct": 0.0,
            "phase": "就绪", "detail": "点「一键更新」开始，或选单独步骤。",
            "exit_code": None,
            "last_ok_at": None, "last_elapsed": 0.0, "seen_end": 0.0,
        }
        self._load_last()

    def _lock_path(self) -> str:
        return os.path.join(ROOT, "data", self.lock_name)

    # ---- 上次运行信息，重启后卡片还能显示
    def _state_file(self) -> str:
        return os.path.join(ROOT, "data", self.state_name)

    def _load_last(self) -> None:
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                d = json.load(f)
            self.state["last_ok_at"] = d.get("last_ok_at")
            self.state["last_elapsed"] = float(d.get("last_elapsed") or 0)
            self.state["seen_end"] = float(d.get("seen_end") or 0)
        except Exception:
            pass
        self._adopt_sched_last()

    def _adopt_sched_last(self) -> None:
        """把 systemd 定时器跑完的那次结果接过来，顶部「上次耗时」才不会一直是「-」。

        本进程只知道自己在跑的这条流水线；定时任务是另一个进程（run.py tick），
        它把结果写在 data/schedule_state.json。这里只认「比本进程见过的更晚」
        的那次，网页手动跑的记录不会被旧的定时结果盖掉。
        """
        if not self.adopt_sched:
            return
        try:
            st = schedule.load_state(ROOT)
            end = float(st.get("last_end") or 0)
            if end <= 0 or end <= float(self.state.get("seen_end") or 0):
                return
            with self.lock:
                self.state["seen_end"] = end
                if st.get("last_dur"):
                    self.state["last_elapsed"] = float(st["last_dur"])
                if st.get("last_ok"):
                    self.state["last_ok_at"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(end))
        except Exception:
            pass

    def _save_last(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._state_file()), exist_ok=True)
            with open(self._state_file(), "w", encoding="utf-8") as f:
                json.dump({"last_ok_at": self.state["last_ok_at"],
                           "last_elapsed": self.state["last_elapsed"],
                           "seen_end": self.state.get("seen_end") or 0}, f)
        except Exception:
            pass

    # ---- 事件广播
    def _emit(self, ev: dict) -> None:
        with self.lock:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def replay(self, since: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > since]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def status(self) -> dict:
        self._adopt_sched_last()
        with self.lock:
            st = dict(self.state)
        if st["running"] and st["started"]:
            st["elapsed"] = time.time() - st["started"]
        # 定时任务在跑时不占本进程的 running，但界面得知道，别让用户白点按钮
        if not st["running"]:
            pid = lock_holder(self._lock_path())
            if pid:
                st["external_pid"] = pid
        return st

    # ---- 生命周期
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self, cmd: str, extra: list[str]) -> dict:
        if cmd not in self.cmds:
            raise ValueError(f"不认识的命令: {cmd}")
        extra = validate_extra(extra)
        with self.lock:
            if self.proc and self.proc.poll() is None:
                raise RuntimeError("已有任务在跑")
            # 定时任务（systemd timer）可能正在跑，它不经过本进程，得看锁
            pid = lock_holder(self._lock_path())
            if pid:
                who = f"PID {pid}" if pid > 0 else "另一个进程"
                raise RuntimeError(f"定时任务正在运行（{who}），等它跑完再试")

            args = [sys.executable, "-u", "run.py", cmd]
            if self.app.config_path and self.app.config_path != os.path.join(ROOT, "config.yaml"):
                args += ["--config", self.app.config_path]
            args += extra

            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            kwargs: dict = dict(cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, bufsize=0)
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True     # 方便整组 kill 掉 mihomo 子进程
            try:
                self.proc = subprocess.Popen(args, **kwargs)
            except Exception as e:
                raise RuntimeError(f"启动失败: {e}") from e

            self.events.clear()
            self.state.update(
                running=True, cmd=cmd, args=extra, started=time.time(),
                elapsed=0.0, pct=0.0, phase="启动中", detail="正在启动流水线…",
                exit_code=None,
            )
            proc = self.proc

        self._emit({"type": "start", "cmd": cmd, "args": extra,
                    "text": "$ run.py " + " ".join([cmd] + extra)})
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        return {"ok": True, "cmd": cmd, "args": extra}

    def stop(self) -> bool:
        p = self.proc
        if not p or p.poll() is not None:
            return False
        try:
            if os.name == "nt":
                p.terminate()
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                return False
        self._line("[已请求停止]")
        threading.Thread(target=self._force_kill, args=(p,), daemon=True).start()
        return True

    def _force_kill(self, p: subprocess.Popen) -> None:
        try:
            p.wait(timeout=10)
            return
        except Exception:
            pass
        try:
            if os.name == "nt":
                p.kill()
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass

    # ---- 日志读取（同时按 \n 和 \r 切，下载进度条也能实时出来）
    def _reader(self, proc: subprocess.Popen) -> None:
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096) if proc.stdout else b""
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = SPLIT_RE.search(buf)
                    if not m:
                        break
                    line, buf = buf[:m.start()], buf[m.end():]
                    self._line(line.decode("utf-8", "replace"))
            if buf:
                self._line(buf.decode("utf-8", "replace"))
        except Exception as e:
            self._line(f"[读取失败] {type(e).__name__}: {e}")
        code = proc.wait()
        self._finish(code)

    def _line(self, text: str) -> None:
        if not text.strip():
            return
        ev = parse_progress(text)
        with self.lock:
            if ev["pct"] is not None:
                self.state["pct"] = max(self.state["pct"], min(100.0, ev["pct"]))
            if ev["phase"]:
                self.state["phase"] = ev["phase"]
            if ev["detail"] is not None:
                self.state["detail"] = ev["detail"]
            pct = self.state["pct"]
        self._emit({"type": "line", "text": text, "tag": ev["tag"],
                    "pct": ev["pct"], "phase": ev["phase"], "detail": ev["detail"],
                    "cur": pct})

    def _finish(self, code: int) -> None:
        with self.lock:
            started = self.state["started"] or time.time()
            elapsed = max(0.0, time.time() - started)
            self.state.update(running=False, elapsed=elapsed, exit_code=code)
            # 记下这次跑到几点，免得定时任务的旧结果盖掉网页刚跑出来的
            self.state["seen_end"] = time.time()
            if code == 0:
                self.state.update(pct=100.0, phase="完成",
                                  detail=f"耗时 {elapsed:.0f} 秒")
                self.state["last_ok_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.state["last_elapsed"] = elapsed
            elif self.state["phase"] not in ("已中断",):
                self.state.update(phase="已中断", detail=f"进程退出码 {code}")
            self.proc = None
        self._save_last()
        self._emit({"type": "done", "code": code, "elapsed": elapsed,
                    "ok_at": self.state["last_ok_at"]})


def validate_extra(extra: list) -> list[str]:
    """只放行已知参数，避免网页端变成任意命令执行。"""
    out: list[str] = []
    if not isinstance(extra, list):
        return out
    i = 0
    while i < len(extra):
        a = str(extra[i])
        if a in FLAGS:
            out.append(a)
            i += 1
        elif a in VALUE_FLAGS and i + 1 < len(extra):
            v = str(extra[i + 1])
            if not v.lstrip("-").isdigit():
                raise ValueError(f"{a} 需要数字")
            out += [a, v]
            i += 2
        elif a in EXTRA_FLAGS and i + 1 < len(extra):
            v = str(extra[i + 1]).strip()
            if a == "--fp" and not FP_RE.match(v):
                raise ValueError("--fp 只能是节点指纹（十六进制），逗号分隔")
            if a == "--type" and not TYPE_RE.match(v):
                raise ValueError("--type 只能是协议名，逗号分隔")
            if a == "--scope" and v not in ("all", "alive", "dead", "new"):
                raise ValueError("--scope 只能是 all/alive/dead/new")
            if a == "--mode" and v not in MODES:
                raise ValueError("--mode 只能是 fast/deep/speed/auto")
            if a == "--ids" and not IDS_RE.match(v):
                raise ValueError("--ids 只能是节点 ID，逗号分隔")
            out += [a, v]
            i += 2
        else:
            raise ValueError(f"不支持的参数: {a}")
    return out


# ---------------------------------------------------------------- 数据查询

class App:
    def __init__(self, cfg: dict, config_path: str, token: str):
        self.cfg = cfg
        self.config_path = config_path
        self.token = token
        db = (cfg.get("store") or {}).get("db", "data/nodes.db")
        self.db_path = db if os.path.isabs(db) else os.path.join(ROOT, db)
        self.store = Store(self.db_path)
        self.runner = Runner(self)
        self.shutdown_flag = False

        self._stats_cache: tuple[float, dict] | None = None
        self._src_cache: tuple[float, dict] | None = None
        self._ex_cache: tuple[float, dict] | None = None
        self._proxy_cache: tuple[float, dict] | None = None
        self._index_cache: tuple[float, bytes] | None = None
        self._sub_cache: tuple[float, dict] | None = None
        self.meta = source_meta(cfg, ROOT)
        self.sub_key = self._load_sub_key()

        # 订阅测速：独立的库 + 独立的运行器，和主流水线互不影响
        self.probe_db = probe_db = probedb.ProbeDB(
            os.path.join(ROOT, "data", "probe.db"))
        self.prunner = Runner(self, lock_name="probe.lock",
                              state_name="probe_state.json", cmds=PROBE_CMDS)
        self.prunner.state["detail"] = "贴一个订阅链接，点「拉取」开始。"
        self._probe_index_cache: tuple[float, bytes] | None = None
        del probe_db

    # ---- 订阅密钥
    # 订阅链接要能直接贴进客户端，没法带 Cookie 或 ?token=，
    # 所以路径里的 key 本身就是凭证：知道 key 就能拉订阅。
    # 存在 data/sub_key.txt 里，重启不变；泄露了就在页面上重置。
    SUB_KEY_FILE = os.path.join(ROOT, "data", "sub_key.txt")

    def _load_sub_key(self) -> str:
        try:
            with open(self.SUB_KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
            if len(k) >= 16:
                return k
        except OSError:
            pass
        return self._write_sub_key(secrets.token_urlsafe(12))

    def _write_sub_key(self, k: str) -> str:
        try:
            os.makedirs(os.path.dirname(self.SUB_KEY_FILE), exist_ok=True)
            with open(self.SUB_KEY_FILE, "w", encoding="utf-8") as f:
                f.write(k)
            os.chmod(self.SUB_KEY_FILE, 0o600)
        except OSError as e:
            log(f"订阅密钥写不进去: {e}")
        return k

    def reset_sub_key(self) -> str:
        self.sub_key = self._write_sub_key(secrets.token_urlsafe(12))
        self._sub_cache = None
        return self.sub_key

    # ---- 后台任务排期
    def schedule_info(self) -> dict:
        """排期快照 + systemd timer 的实时状态。

        timer 状态只是「显示用」：真到不到点由 run.py tick 自己算（读
        data/schedule.json），所以即使这里拿不到 systemctl 的输出，
        排期照常生效。
        """
        info = schedule.summary(ROOT)
        info["timer"] = self._timer_state()
        return info

    def _timer_state(self) -> dict:
        out = {"active": None, "enabled": None, "next": "", "text": "未知"}
        try:
            act = subprocess.run(["systemctl", "is-active", "nodehub-update.timer"],
                                 capture_output=True, text=True, timeout=3)
            out["active"] = (act.stdout or "").strip() or "unknown"
            ena = subprocess.run(["systemctl", "is-enabled", "nodehub-update.timer"],
                                 capture_output=True, text=True, timeout=3)
            out["enabled"] = (ena.stdout or "").strip() or "unknown"
        except Exception:
            # 没装 systemd / 容器里跑，拿不到就当未知，不影响排期本身
            return out
        if out["active"] == "active":
            # 顺手把「下一次检查心跳」的时间带上，让人看得出这台机器确实在轮询
            try:
                lt = subprocess.run(["systemctl", "list-timers", "nodehub-update.timer",
                                     "--output=json", "--no-pager"],
                                    capture_output=True, text=True, timeout=3)
                rows = json.loads((lt.stdout or "").strip() or "[]")
                nxt = float((rows[0] or {}).get("next") or 0) / 1e6
                if nxt > 0:
                    out["next"] = time.strftime("%H:%M:%S", time.localtime(nxt))
            except Exception:
                pass
            tail = f"，下次检查 {out['next']}" if out["next"] else ""
            out["text"] = f"运行中（每 10 分钟检查一次{tail}）"
        elif out["active"] in ("inactive", "failed"):
            out["text"] = "未运行（排期不会生效，用 systemctl start nodehub-update.timer 打开）"
        return out

    def save_schedule(self, payload: dict) -> dict:
        """网页上改排期。只认四个字段，其余一律忽略。"""
        cur = schedule.load(ROOT)
        data = {
            "enabled": payload.get("enabled", cur.get("enabled")),
            "mode": payload.get("mode", cur.get("mode")),
            "at": payload.get("at", cur.get("at")),
            "every_hours": payload.get("every_hours", cur.get("every_hours")),
        }
        saved = schedule.save(ROOT, data)
        log(f"排期已更新：{schedule.describe(saved)}")
        return self.schedule_info()

    def sub_nodes(self, min_age: float = 0.0,
                  tier: str = "alive") -> tuple[list[dict], int, int]:
        """返回 (实际给订阅的节点, 符合条件的总数, 上限)。

        tier 是三档筛查的结果，对应订阅页上能选的三档：
          alive（默认）深度筛查通过 / speed 最终筛查有下载速度 / tcp 快速筛查端口通

        上限来自 export.top_n：手机上导入几千个节点会卡，所以默认截到 500。
        页面要把「有多少被截掉」说清楚，不然用户会以为漏了。
        """
        tier = (tier or "alive").lower()
        if tier not in self.store.TIERS:
            tier = "alive"
        rows = self.store.alive_nodes(0, min_age_days=min_age,
                                      min_ok=2 if min_age > 0 else 0, tier=tier)
        top = int(((self.cfg.get("export") or {}).get("top_n") or 0))
        return (rows[:top] if top else rows), len(rows), top

    def sub_info(self, base: str, min_age: float = 0.0,
                 tier: str = "alive") -> dict:
        """订阅页需要的数据：每种格式一条链接 + 实际能出多少节点。"""
        tier = (tier or "alive").lower()
        if tier not in self.store.TIERS:
            tier = "alive"
        key = (base, min_age, tier)
        now = time.time()
        if self._sub_cache and now - self._sub_cache[0] < 5 \
                and self._sub_cache[1].get("_key") == key:
            return {k: v for k, v in self._sub_cache[1].items() if k != "_key"}
        nodes, available, limit = self.sub_nodes(min_age, tier)
        params = []
        if min_age > 0:
            params.append(f"min_age={min_age:g}")
        if tier != "alive":
            params.append(f"tier={tier}")
        q = ("?" + "&".join(params)) if params else ""
        items = []
        for f in exporter.FORMATS:
            usable, skipped = exporter.fmt_nodes(nodes, f["id"])
            items.append({
                "id": f["id"], "name": f["name"], "apps": f["apps"],
                "desc": f["desc"], "ext": f["ext"],
                "count": len(usable), "skipped": skipped,
                "url": f"{base}/sub/{self.sub_key}/{f['id']}{q}",
            })
        out = {"_key": key, "key": self.sub_key, "base": base,
               "min_age": min_age, "tier": tier,
               "tiers": self.store.tier_counts(min_age),
               "alive": len(nodes),
               "available": available, "limit": limit, "formats": items}
        self._sub_cache = (now, out)
        return {k: v for k, v in out.items() if k != "_key"}

    # ---- 统计（3 秒缓存，前端 5 秒轮询一次）
    def stats(self) -> dict:
        now = time.time()
        if self._stats_cache and now - self._stats_cache[0] < 3:
            return self._stats_cache[1]
        st = self.store.stats()
        with sqlite3.connect(self.db_path, timeout=10) as c:
            src = c.execute("SELECT COUNT(*) FROM sources WHERE last_count > 0").fetchone()[0]
        st["sources_active"] = src
        try:
            st["long"] = self.store.long_counts()   # {"3": n, "5": n, "7": n}
        except Exception:
            st["long"] = {}
        self._stats_cache = (now, st)
        return st

    def proxy(self) -> dict:
        now = time.time()
        if self._proxy_cache and now - self._proxy_cache[0] < 10:
            return self._proxy_cache[1]
        url = ((self.cfg.get("fetch") or {}).get("proxy") or "").strip()
        up = False
        m = re.match(r"(?:socks5h?|http)://([^:/]+):(\d+)", url)
        if m:
            try:
                socket.create_connection((m.group(1), int(m.group(2))), timeout=0.6).close()
                up = True
            except Exception:
                up = False
        info = {"url": url, "up": up}
        self._proxy_cache = (now, info)
        return info

    def last_run_lines(self, limit: int = 300, name: str = "last_run.log") -> list[dict]:
        """读 data/<name>（run.py 每次运行都会写），转成前端能直接用的行。

        场景：昨晚是定时任务跑的，网页进程手里没有那段输出；重启过也一样。
        订阅测速走自己那份 probe_last.log，别把主流水线的日志混进来。
        """
        p = os.path.join(ROOT, "data", name)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-limit:]
        except Exception:
            return []
        out = []
        for line in lines:
            t = line.rstrip("\r\n")
            if not t.strip():
                continue
            ev = parse_progress(t)
            out.append({"text": t, "tag": ev["tag"]})
        return out

    def sources(self) -> dict:
        now = time.time()
        if self._src_cache and now - self._src_cache[0] < 60:
            return self._src_cache[1]

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute("""
                SELECT s.name, s.last_count AS y,
                       COUNT(DISTINCT n.fp) AS tested,
                       COUNT(DISTINCT CASE WHEN n.alive=1 THEN n.fp END) AS alive
                FROM sources s
                LEFT JOIN node_sources ns ON ns.source_id = s.id
                LEFT JOIN nodes n ON n.fp = ns.fp
                     AND n.last_check IS NOT NULL AND n.last_error != 'tcp_unreachable'
                GROUP BY s.name
            """)]
        finally:
            conn.close()

        # 独有节点：只被一个源收录的，算这个源的独家贡献。这个查询重，缓存 5 分钟
        if not self._ex_cache or now - self._ex_cache[0] > 300:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                ex = {r["name"]: r["ex"] for r in conn.execute("""
                    SELECT s.name, COUNT(*) AS ex
                    FROM node_sources ns JOIN sources s ON s.id = ns.source_id
                    WHERE ns.fp IN (SELECT fp FROM node_sources GROUP BY fp HAVING COUNT(*) = 1)
                    GROUP BY s.name
                """)}
            finally:
                conn.close()
            self._ex_cache = (now, ex)
        ex_map = self._ex_cache[1]

        items = []
        for r in rows:
            meta = self.meta.get(r["name"], {})
            if meta.get("status") == "archived":
                continue
            tested = r["tested"] or 0
            alive = r["alive"] or 0
            items.append({
                "name": r["name"],
                "y": meta.get("yield") or r["y"] or 0,
                "tested": tested,
                "alive": alive,
                "rate": (alive * 100 / tested) if tested >= 10 else None,
                "ex": ex_map.get(r["name"], 0),
                "reach": {"direct": "直连", "proxy": "需代理",
                          "both": "都行"}.get(meta.get("reach"), "-"),
                "kind": meta.get("kind", "github"),
            })
        items.sort(key=lambda x: (x["rate"] is None, -(x["rate"] or 0), -x["alive"]))
        good = sum(1 for x in items if x["rate"] is not None and x["rate"] >= 20)
        payload = {"rows": items, "good": good,
                   "alive": self.stats()["alive"], "total_sources": len(items)}
        self._src_cache = (now, payload)
        return payload

    # ---- 节点筛选（表格和导出共用同一套，界面上看到几行就导出几行）
    def pick_nodes(self, qs: dict) -> list[dict]:
        try:
            min_age = float((qs.get("min_age") or ["0"])[0] or 0)
        except ValueError:
            min_age = 0.0
        # 长期可用：除了够天数，还要求至少成功验证过 2 次，
        # 免得「只测过一次、刚好通过」的节点混进来
        tier = (qs.get("tier") or ["alive"])[0].lower()
        if tier not in self.store.TIERS:
            tier = "alive"
        rows = self.store.alive_nodes(0, min_age_days=min_age,
                                      min_ok=2 if min_age > 0 else 0, tier=tier)
        proto = (qs.get("proto") or [""])[0]
        if proto and proto != "all":
            rows = [r for r in rows if r["type"] == proto]
        try:
            lat = int((qs.get("lat") or ["0"])[0] or 0)
        except ValueError:
            lat = 0
        if lat > 0:
            rows = [r for r in rows if r.get("latency") and r["latency"] <= lat]
        try:
            limit = int((qs.get("limit") or ["0"])[0] or 0)
        except ValueError:
            limit = 0
        if limit > 0:
            rows = rows[:limit]
        return rows

    def nodes(self, qs: dict) -> dict:
        rows = self.pick_nodes(qs)
        alive_total = self.stats()["alive"]
        # 有些源只给 clash 配置没给原始链接，这里统一补一份，前端点行就能复制
        uris = exporter.nodes_to_uris(rows)
        out = []
        for i, r in enumerate(rows):
            out.append({
                "fp": r["fp"], "name": r.get("name") or "", "type": r["type"],
                "server": r["server"], "port": r["port"], "latency": r.get("latency"),
                "first_seen": r.get("first_seen"), "ok_count": r.get("ok_count") or 0,
                "alive_since": r.get("alive_since") or r.get("first_seen"),
                # 快速检测的结果：1 通 / 0 不通 / None 没测过
                # tcp_ok 列默认 0，不能直接拿来判断「没测过」——要看 tcp_check 有没有时间戳
                "tcp_ok": (1 if r.get("tcp_ok") else 0) if r.get("tcp_check") else None,
                "tcp_ms": r.get("tcp_ms"),
                # 最终筛查：真实下载吞吐（字节/秒），None 就是没测过或没跑通
                "speed": r.get("speed"),
                "ttfb": r.get("ttfb"),
                "speed_at": r.get("speed_at"),
                "uri": r.get("uri") or (uris[i] if i < len(uris) else ""),
            })
        return {"rows": out, "shown": len(out), "alive_total": alive_total}

    def export(self, qs: dict) -> tuple[str, str, str]:
        """返回 (内容, 扩展名, 格式名)。"""
        rows = self.pick_nodes(qs)
        fmt = (qs.get("fmt") or ["clash"])[0]
        if not rows:
            return "", "txt", "没有符合条件的节点"
        if fmt == "clash":
            conf = exporter.build_clash_config(rows)
            return (yamlutil.dump(conf),
                    "yaml", "Clash 配置")
        if fmt == "proxies":
            ps = exporter.build_proxies(rows)
            ps = [{k: v for k, v in p.items() if not k.startswith("_")} for p in ps]
            return (yamlutil.dump({"proxies": ps}), "yaml", "仅节点列表")
        uris = exporter.nodes_to_uris(rows)
        if fmt == "base64":
            import base64
            return (base64.b64encode("\n".join(uris).encode()).decode(),
                    "txt", "base64 订阅")
        return "\n".join(uris), "txt", "明文链接"

    def _page(self, path: str, cache_attr: str) -> bytes:
        now = time.time()
        cache = getattr(self, cache_attr)
        if cache and now - cache[0] < 5:
            return cache[1]
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = (f"<h1>缺少 {os.path.basename(path)}</h1>"
                    "<p>把前端文件放回 webui/ 目录再刷新。</p>").encode()
        setattr(self, cache_attr, (now, body))
        return body

    def index(self) -> bytes:
        return self._page(INDEX_PATH, "_index_cache")

    def probe_index(self) -> bytes:
        return self._page(PROBE_PATH, "_probe_index_cache")

    # ---- 订阅测速 ---------------------------------------------------
    # 页面上的所有动作都落在这几个方法上。注意「拉取订阅」是同步的
    # （最多几秒），「检测」才是起子进程，避免网页卡住。

    def probe_fetch(self, url: str, title: str) -> dict:
        text, final_url = probedb.fetch_sub(url)
        nodes, fmt, bad = probedb.parse_sub(text)
        if not nodes:
            raise probedb.ProbeError("订阅里没有解析出可用的节点")
        rid = self.probe_db.create_run(title.strip() or final_url, final_url,
                                       fmt, nodes)
        return {"id": rid, "total": len(nodes), "dropped": bad, "fmt": fmt}

    def probe_run_detail(self, rid: int) -> dict:
        run = self.probe_db.run(rid)
        if not run:
            raise ValueError("记录不存在")
        rows = self.probe_db.nodes(rid)
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "name": r["name"], "type": r["type"],
                "server": r["server"], "port": r["port"],
                "tcp_ok": r["tcp_ok"], "tcp_ms": r["tcp_ms"],
                "latency": r["latency"], "ttfb": r["ttfb"], "speed": r["speed"],
                "ok": r["ok"], "err": r["err"], "serr": r["serr"],
                "uri": r["uri"], "tested_at": r["tested_at"],
            })
        return {"run": run, "nodes": out}

    def probe_test(self, rid: int, mode: str, size: int,
                   max_speed: int, ids: list[int]) -> dict:
        if mode not in MODES:
            raise ValueError("mode 只能是 fast/deep/speed/auto")
        size = int(size or 10)
        if size not in (10, 20, 50):
            raise ValueError("测速文件只能是 10/20/50 MB")
        max_speed = max(1, min(200, int(max_speed or 15)))
        if not self.probe_db.run(rid):
            raise ValueError("记录不存在")
        extra = ["--run", str(rid), "--mode", mode,
                 "--size", str(size), "--max-speed", str(max_speed)]
        if ids:
            extra += ["--ids", ",".join(str(int(i)) for i in ids)]
        return self.prunner.start("probe", extra)

    def probe_export(self, rid: int, fmt: str, only_ok: bool,
                     sort: str, only_speed: bool = False,
                     min_speed: float = 0.0) -> tuple[str, str, str, int, int]:
        rows = self.probe_db.nodes(rid)
        if only_ok:
            rows = [r for r in rows if r["ok"]]
        # 只有真跑出下载速度的节点才算「能用」：TCP 通、延迟低只说明端口开着
        if only_speed:
            rows = [r for r in rows if (r["speed"] or 0) > 0]
        if min_speed and min_speed > 0:
            floor = min_speed * 1048576
            rows = [r for r in rows if (r["speed"] or 0) >= floor]
        if sort == "speed":
            rows.sort(key=lambda r: -(r["speed"] or 0))
        elif sort == "latency":
            rows.sort(key=lambda r: (r["latency"] or 999999))
        else:
            rows.sort(key=lambda r: r["id"])

        # 节点名里带上测出来的速度，客户端列表里一眼能看出快慢
        import re as _re
        stale = _re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|MB/s)\b", _re.I)

        def name_fn(p, idx, n):
            base = (n.get("name") or "").strip()
            # 订阅里的名字常常自带上一轮测的「123ms」，不清掉会和新结果打架
            base = stale.sub("", base).strip()
            if not base or base == n.get("server") or \
                    base == f"{n.get('server')}:{n.get('port')}":
                base = f"{(p.get('type') or '?').upper()}-{idx:03d}"
            bits = []
            if n.get("speed"):
                mb = n["speed"] / 1048576
                bits.append(f"{mb:.1f}MB/s" if mb >= 10 else f"{mb:.2f}MB/s")
            if n.get("latency"):
                bits.append(f"{n['latency']}ms")
            return f"{base} {' '.join(bits)}".strip() if bits else base

        return exporter.build_sub(fmt, rows, name_fn=name_fn)


def _speed_name(p: dict, idx: int, n: dict) -> str:
    """「有速度」这一档的节点名带上实测吞吐，客户端列表里一眼看出快慢。"""
    t = (p.get("type") or "?").upper()
    mb = (n.get("speed") or 0) / 1048576
    sp = f"{mb:.1f}MB/s" if mb >= 10 else f"{mb:.2f}MB/s"
    lat = f" {n['latency']}ms" if n.get("latency") else ""
    return f"{t}-{idx:03d} {sp}{lat}"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "nodehub-web/1.0"
    app: App = None  # type: ignore[assignment]

    # ---- 基础工具
    def log_message(self, fmt: str, *args) -> None:  # 默认实现会往 stderr 刷屏
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _redirect(self, path: str) -> None:
        extra = {"Location": path}
        if self.app.token:
            extra["Set-Cookie"] = (f"nh={self.app.token}; Path=/; HttpOnly; "
                                   f"SameSite=Lax; Max-Age=2592000")
        self._send(302, b"", extra=extra)

    def _authed(self, qs: dict) -> bool:
        tok = self.app.token
        if not tok:
            return True

        def same(a: str) -> bool:
            # 定长比较，别用 == 给计时攻击留口子；非 ASCII 直接判失败
            try:
                return hmac.compare_digest(a, tok)
            except TypeError:
                return False

        if same((qs.get("token") or [""])[0]):
            return True
        if same(self.headers.get("X-Token") or ""):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nh" and same(v):
                return True
        return False

    # ---- 订阅
    def _base_url(self) -> str:
        """拼出对外可访问的根地址。反代后面要靠 X-Forwarded-* 才知道真实协议。"""
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if not proto:
            proto = "https" if (self.headers.get("X-Forwarded-Ssl") or "") == "on" else "http"
        host = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip() \
            or self.headers.get("Host") or ""
        if not host:
            host = f"127.0.0.1:{self.server.server_address[1]}"
        return f"{proto}://{host}"

    def _sub(self, path: str, qs: dict) -> None:
        """GET /sub/<key>/<fmt> —— 免鉴权，key 对了就给内容。"""
        app = self.app
        parts = [p for p in path[len("/sub/"):].split("/") if p]
        if len(parts) != 2:
            return self._send(404, b"not found")
        key, fmt = unquote(parts[0]), unquote(parts[1])
        # 用比较不等长也算安全的方式校验；key 不对一律 404，
        # 不返回 401，免得别人靠状态码试探路径对不对
        if not app.sub_key or not hmac.compare_digest(key, app.sub_key):
            return self._send(404, b"not found")
        if fmt not in {f["id"] for f in exporter.FORMATS}:
            return self._send(404, b"unknown format")
        try:
            age = float((qs.get("min_age") or ["0"])[0] or 0)
        except ValueError:
            age = 0.0
        # 订阅可以自选档位：alive 能连接 / speed 有下载速度 / tcp 只测了端口
        tier = (qs.get("tier") or ["alive"])[0].lower()
        if tier not in app.store.TIERS:
            tier = "alive"
        try:
            text, ext, ctype, n, skipped = exporter.build_sub(
                fmt, app.sub_nodes(age, tier)[0],
                name_fn=_speed_name if tier == "speed" else None)
        except Exception as e:
            log(f"订阅 {fmt} 生成失败: {type(e).__name__}: {e}")
            return self._send(500, f"生成失败: {e}".encode("utf-8"))
        if not text:
            # 空订阅也给 200 + 空内容，客户端会显示「0 个节点」而不是报错
            text = ""
        headers = {
            # clash / mihomo 认这个头，决定多久回来拉一次
            "profile-update-interval": "6",
            "X-Nodehub-Count": str(n),
            "X-Nodehub-Skipped": str(skipped),
            "X-Nodehub-Tier": tier,
        }
        return self._send(200, text.encode("utf-8"), ctype, headers)

    def _body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    # ---- GET
    def do_GET(self) -> None:
        u = urlsplit(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/healthz":
            return self._send(200, b"ok")

        # 订阅链接要能直接粘进客户端，所以走不了 Cookie/令牌，靠路径里的 key 认人。
        # 必须放在鉴权判断之前。
        if path.startswith("/sub/"):
            return self._sub(path, qs)

        if not self._authed(qs):
            if path.startswith("/api/") or path.startswith("/download/"):
                return self._json(401, {"error": "unauthorized"})
            return self._send(200, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

        # 带 ?token= 打开页面时种 cookie，然后跳到不带参数的地址（别把 token 留在地址栏）。
        # 只对页面生效——API 请求带 token 就直接放行，不能返回 302。
        if path in ("/", "/index.html", "/probe") and (qs.get("token") or [""])[0] \
                and self.app.token:
            return self._redirect(path)

        app = self.app
        try:
            if path in ("/", "/index.html"):
                return self._send(200, app.index(), "text/html; charset=utf-8")
            if path in ("/probe", "/probe.html"):
                return self._send(200, app.probe_index(), "text/html; charset=utf-8")

            # ---- 订阅测速
            if path == "/api/probe/status":
                return self._json(200, {"run": app.prunner.status(),
                                        "runs": app.probe_db.runs()})
            if path == "/api/probe/runs":
                return self._json(200, {"runs": app.probe_db.runs()})
            if path == "/api/probe/run":
                try:
                    rid = int((qs.get("id") or ["0"])[0])
                except ValueError:
                    rid = 0
                try:
                    return self._json(200, app.probe_run_detail(rid))
                except ValueError as e:
                    return self._json(404, {"error": str(e)})
            if path == "/api/probe/stream":
                return self._sse(qs, app.prunner, "probe_last.log")
            if path == "/api/probe/export":
                try:
                    rid = int((qs.get("id") or ["0"])[0])
                except ValueError:
                    rid = 0
                fmt = (qs.get("fmt") or ["clash"])[0]
                if fmt not in {f["id"] for f in exporter.FORMATS}:
                    return self._json(404, {"error": "unknown format"})
                only_ok = (qs.get("only_ok") or ["1"])[0] not in ("0", "false")
                only_speed = (qs.get("only_speed") or ["0"])[0] in ("1", "true")
                try:
                    min_speed = float((qs.get("min_speed") or ["0"])[0])
                except ValueError:
                    min_speed = 0.0
                sort = (qs.get("sort") or ["speed"])[0]
                try:
                    text, ext, ctype, n, skipped = app.probe_export(
                        rid, fmt, only_ok, sort, only_speed, min_speed)
                except ValueError as e:
                    return self._json(404, {"error": str(e)})
                if (qs.get("download") or ["0"])[0] in ("1", "true"):
                    name = f"probe-{rid}.{ext}"
                    return self._send(200, text.encode("utf-8"), ctype, {
                        "Content-Disposition": f'attachment; filename="{name}"',
                        "X-Nodehub-Count": str(n),
                        "X-Nodehub-Skipped": str(skipped),
                    })
                return self._json(200, {"text": text, "count": n,
                                        "skipped": skipped, "ext": ext})
            if path == "/api/schedule":
                return self._json(200, app.schedule_info())
            if path == "/api/status":
                st = app.runner.status()
                return self._json(200, {
                    "run": st, "stats": app.stats(), "proxy": app.proxy(),
                    "now": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            if path == "/api/stream":
                return self._sse(qs)
            if path == "/api/log":
                since = int((qs.get("since") or ["0"])[0] or 0)
                return self._json(200, {"events": app.runner.replay(since),
                                        "seq": app.runner.seq})
            if path == "/api/sources":
                return self._json(200, app.sources())
            if path == "/api/nodes":
                return self._json(200, app.nodes(qs))
            if path == "/api/export":
                text, ext, desc = app.export(qs)
                if not text:
                    return self._json(404, {"error": desc})
                if (qs.get("download") or ["0"])[0] == "1":
                    name = f"nodes_{time.strftime('%Y%m%d_%H%M')}.{ext}"
                    return self._send(200, text.encode("utf-8"),
                                      "application/octet-stream",
                                      {"Content-Disposition":
                                       f'attachment; filename="{name}"'})
                return self._json(200, {"text": text, "ext": ext, "desc": desc,
                                        "count": len(app.pick_nodes(qs))})
            if path == "/api/sub":
                try:
                    age = float((qs.get("min_age") or ["0"])[0] or 0)
                except ValueError:
                    age = 0.0
                tier = (qs.get("tier") or ["alive"])[0].lower()
                return self._json(200, app.sub_info(self._base_url(), age, tier))
            if path == "/api/report":
                p = os.path.join(ROOT, "output", "report.md")
                if not os.path.exists(p):
                    return self._json(404, {"error": "还没有报告，先跑一次"})
                with open(p, encoding="utf-8") as f:
                    return self._send(200, f.read().encode("utf-8"),
                                      "text/markdown; charset=utf-8")
            if path.startswith("/download/"):
                name = unquote(path[len("/download/"):])
                if name not in OUTPUT_FILES:
                    return self._json(404, {"error": "文件不存在"})
                p = os.path.join(ROOT, "output", name)
                if not os.path.exists(p):
                    return self._json(404, {"error": "文件还没生成，先跑一次"})
                with open(p, "rb") as f:
                    data = f.read()
                return self._send(200, data, "application/octet-stream",
                                  {"Content-Disposition":
                                   f'attachment; filename="{name}"'})
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log(f"GET {path} 出错: {type(e).__name__}: {e}")
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ---- SSE
    def _sse(self, qs: dict, runner=None, hist_name: str = "last_run.log") -> None:
        app = self.app
        runner = runner or app.runner
        try:
            since = int((qs.get("since") or ["0"])[0] or 0)
        except ValueError:
            since = 0

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")   # nginx 别缓冲
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        q = runner.subscribe()
        try:
            buffered = runner.replay(since)
            for ev in buffered:
                self._sse_write(ev)
            # 本进程没跑过任务（重启过 / 昨晚是定时任务跑的）：把上次的日志补上，
            # 不然用户点开页面只看到一片空白。
            if not buffered and not runner.running():
                hist = app.last_run_lines(name=hist_name)
                if hist:
                    self._sse_write({"type": "history", "lines": hist})
            self._sse_write({"type": "status", "run": runner.status()})
            while not app.shutdown_flag:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(ev)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            app.runner.unsubscribe(q)

    def _sse_write(self, ev: dict) -> None:
        data = json.dumps(ev, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---- POST
    def do_POST(self) -> None:
        u = urlsplit(self.path)
        path, qs = u.path, parse_qs(u.query)
        raw = self._body()

        if path == "/login":
            form = parse_qs(raw.decode("utf-8", "replace"))
            tok = (form.get("token") or [""])[0].strip()
            if tok and tok == self.app.token:
                return self._redirect("/")
            return self._send(403, LOGIN_HTML.replace(
                "<p>输入访问令牌", "<p>令牌不对，重新输入。输入访问令牌"
            ).encode("utf-8"), "text/html; charset=utf-8")

        if not self._authed(qs):
            return self._json(401, {"error": "unauthorized"})

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        try:
            if path == "/api/run":
                cmd = str(payload.get("cmd") or "all")
                extra = payload.get("extra") or []
                res = self.app.runner.start(cmd, extra)
                return self._json(200, res)
            if path == "/api/stop":
                return self._json(200, {"ok": self.app.runner.stop()})
            if path == "/api/sub/reset":
                return self._json(200, {"ok": True, "key": self.app.reset_sub_key()})
            if path == "/api/schedule":
                return self._json(200, self.app.save_schedule(payload))

            # ---- 订阅测速
            if path == "/api/probe/fetch":
                url = str(payload.get("url") or "").strip()
                title = str(payload.get("title") or "").strip()[:120]
                return self._json(200, self.app.probe_fetch(url, title))
            if path == "/api/probe/test":
                ids = payload.get("ids") or []
                if not isinstance(ids, list):
                    ids = []
                return self._json(200, self.app.probe_test(
                    int(payload.get("id") or 0),
                    str(payload.get("mode") or "auto"),
                    int(payload.get("size") or 10),
                    int(payload.get("max_speed") or 15),
                    [int(i) for i in ids[:500] if str(i).isdigit()]))
            if path == "/api/probe/stop":
                return self._json(200, {"ok": self.app.prunner.stop()})
            if path == "/api/probe/rename":
                rid = int(payload.get("id") or 0)
                self.app.probe_db.rename(rid, str(payload.get("title") or ""))
                return self._json(200, {"ok": True})
            if path == "/api/probe/delete":
                rid = int(payload.get("id") or 0)
                self.app.probe_db.delete_run(rid)
                return self._json(200, {"ok": True})
            if path == "/api/probe/node/rename":
                self.app.probe_db.rename_node(int(payload.get("id") or 0),
                                              str(payload.get("name") or ""))
                return self._json(200, {"ok": True})
            if path == "/api/probe/node/delete":
                ids = payload.get("ids") or []
                if not isinstance(ids, list):
                    ids = []
                n = self.app.probe_db.delete_nodes(
                    [int(i) for i in ids[:2000] if str(i).isdigit()])
                return self._json(200, {"ok": True, "deleted": n})
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except probedb.ProbeError as e:
            # 订阅拉不到 / 格式认不出来，是用户输入问题，别报 500
            return self._json(400, {"error": str(e)})
        except RuntimeError as e:
            return self._json(409, {"error": str(e)})
        except Exception as e:
            log(f"POST {path} 出错: {type(e).__name__}: {e}")
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------- 启动

def resolve_token(cfg: dict, cli_token: str | None, no_auth: bool) -> str:
    if no_auth:
        return ""
    if cli_token:
        return cli_token
    tok = str(((cfg.get("web") or {}).get("token") or "")).strip()
    if tok:
        return tok
    # 没配就生成一个，落盘复用——不然每次重启都要重新看日志找 token
    p = os.path.join(ROOT, "data", "web_token.txt")
    try:
        with open(p, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(12)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(tok)
        os.chmod(p, 0o600)
    except Exception:
        pass
    return tok


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="数据采集流水线 · 网页版")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--token", default=None, help="访问令牌，覆盖 config 里的 web.token")
    ap.add_argument("--no-auth", action="store_true", help="关闭鉴权（仅内网/本机）")
    args = ap.parse_args()

    cfg = load_config(args.config, ROOT)
    web = cfg.get("web") or {}
    host = args.host or str(web.get("host") or "0.0.0.0")
    port = args.port or int(web.get("port") or 8787)
    token = resolve_token(cfg, args.token, args.no_auth)

    app = App(cfg, os.path.abspath(args.config), token)
    Handler.app = app

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True

    def shutdown(*_a) -> None:
        app.shutdown_flag = True
        app.runner.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except Exception:
            pass

    show = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print()
    print("  数据采集流水线 · 网页版")
    print("  ─────────────────────────────────────────")
    print(f"  监听    http://{host}:{port}")
    print(f"  打开    http://{show}:{port}" + (f"/?token={token}" if token else ""))
    print(f"  令牌    {token or '（已关闭鉴权）'}")
    print(f"  数据库  {app.db_path}")
    print(f"  配置    {args.config}")
    print("  ─────────────────────────────────────────")
    print("  Ctrl+C 退出")
    print()
    sys.stdout.flush()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.shutdown_flag = True
        app.runner.stop()
        httpd.server_close()
        log("已退出")


if __name__ == "__main__":
    main()
