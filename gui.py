#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据采集流水线 —— 图形界面

双击「启动界面.bat」或 `pythonw gui.py` 打开。

设计要点：
- 用 subprocess 跑 run.py，逐行读 stdout 解析进度。这样点「停止」能真的杀掉进程，
  也不用担心 tkinter 和流水线抢 GIL。
- 所有面板数据直接查 SQLite，跑完自动刷新。
"""
from __future__ import annotations

import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "nodes.db")
OUTDIR = os.path.join(ROOT, "output")
CONFIG = os.path.join(ROOT, "config.yaml")

# 流水线子进程固定用带控制台的 python.exe。
# 界面可能是用 pythonw.exe 启动的，那样子进程拿不到 stdout，进度就全废了。
_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PIPELINE_PY = _PY if os.path.exists(_PY) else sys.executable

# ---------------------------------------------------------------- 配色
BG = "#f4f6fa"
CARD = "#ffffff"
BORDER = "#dde3ec"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
GREEN = "#16a34a"
RED = "#dc2626"
AMBER = "#d97706"

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_T = ("Microsoft YaHei UI", 16, "bold")
FONT_MONO = ("Consolas", 9)


def read_proxy() -> tuple[str, bool]:
    """返回 (代理地址, 端口是否在监听)。"""
    try:
        import yaml
        with open(CONFIG, encoding="utf-8") as f:
            p = (yaml.safe_load(f) or {}).get("fetch", {}).get("proxy") or ""
    except Exception:
        return "", False
    m = re.match(r"(?:socks5h?|http)://([^:/]+):(\d+)", p)
    if not m:
        return p, False
    import socket
    try:
        socket.create_connection((m.group(1), int(m.group(2))), timeout=0.8).close()
        return p, True
    except Exception:
        return p, False


def db_query(sql: str, args: tuple = ()) -> list[dict]:
    if not os.path.exists(DB):
        return []
    try:
        conn = sqlite3.connect(DB, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, args)]
        conn.close()
        return rows
    except Exception:
        return []



def set_clipboard(text: str) -> bool:
    """写系统剪贴板。优先用 Win32 API —— Tk 自己的剪贴板在程序退出后会失效。"""
    try:
        import ctypes
        from ctypes import wintypes
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise MemoryError
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        for _ in range(5):
            if u32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            return False
        try:
            u32.EmptyClipboard()
            u32.SetClipboardData(CF_UNICODETEXT, h)
        finally:
            u32.CloseClipboard()
        return True
    except Exception:
        try:
            import tkinter as _tk
            r = _tk._default_root
            if r:
                r.clipboard_clear()
                r.clipboard_append(text)
                r.update()
                return True
        except Exception:
            pass
        return False


def human_days(ts: str) -> str:
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"
    d = (datetime.now() - t).total_seconds()
    if d < 3600:
        return f"{int(d // 60)} 分钟"
    if d < 86400:
        return f"{d / 3600:.1f} 小时"
    return f"{d / 86400:.1f} 天"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("数据采集流水线")
        self.geometry("1180x780")
        self.minsize(980, 660)
        self.configure(bg=BG)

        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.progress = 0.0
        self.running = False
        self.last_elapsed = 0.0
        self._last_refresh = 0.0
        self._ex_cache = None
        self._ex_ts = 0.0
        self.node_rows: list[dict] = []

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_all()
        self.after(120, self._poll)

    # ------------------------------------------------------------ 样式
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=FONT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground=TEXT, font=FONT)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=FONT_S)
        st.configure("H1.TLabel", background=BG, foreground=TEXT, font=FONT_T)
        st.configure("Stat.TLabel", background=CARD, foreground=TEXT,
                     font=("Microsoft YaHei UI", 17, "bold"))
        st.configure("TButton", font=FONT, padding=(12, 7), relief="flat",
                     background="#e8edf6", foreground=TEXT, borderwidth=0)
        st.map("TButton", background=[("active", "#dbe4f3"), ("disabled", "#f0f2f6")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     font=FONT_B, padding=(16, 8))
        st.map("Accent.TButton", background=[("active", "#1d4ed8"),
                                             ("disabled", "#9db4e8")])
        st.configure("Danger.TButton", background="#fde8e8", foreground=RED)
        st.map("Danger.TButton", background=[("active", "#fbd5d5")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", font=FONT, padding=(18, 8),
                     background="#e8edf6", foreground=MUTED, borderwidth=0)
        st.map("TNotebook.Tab",
               background=[("selected", CARD)],
               foreground=[("selected", ACCENT)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, font=FONT_S, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"),
                     background="#eef2f9", foreground=MUTED, relief="flat")
        st.map("Treeview.Heading", background=[("active", "#e3eaf5")])
        st.configure("TProgressbar", background=ACCENT, troughcolor="#dfe6f2",
                     borderwidth=0, thickness=14, bordercolor=BORDER,
                     lightcolor=ACCENT, darkcolor=ACCENT)

    # ------------------------------------------------------------ 布局
    def _build(self):
        # 顶栏
        top = ttk.Frame(self)
        top.pack(fill="x", padx=18, pady=(16, 8))

        ttk.Label(top, text="数据采集流水线", style="H1.TLabel").pack(side="left")

        self.lbl_proxy = tk.Label(top, text="", bg=BG, fg=MUTED, font=FONT_S)
        self.lbl_proxy.pack(side="left", padx=(18, 0), pady=(6, 0))

        stats = ttk.Frame(top)
        stats.pack(side="right")
        self.stat_widgets = {}
        for key, label in (("total", "库内节点"), ("alive", "当前可用"),
                           ("src", "有货源"), ("last", "上次耗时")):
            box = ttk.Frame(stats, style="Card.TFrame", padding=(16, 8))
            box.pack(side="left", padx=(8, 0))
            v = ttk.Label(box, text="-", style="Stat.TLabel")
            v.pack(anchor="e")
            ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="e")
            self.stat_widgets[key] = v

        # 工具栏
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=18, pady=(4, 10))
        self.btn_all = ttk.Button(bar, text="▶  一键更新", style="Accent.TButton",
                                  command=lambda: self.start("all"))
        self.btn_all.pack(side="left")
        for text, cmd, extra in (("只采集", "collect", []),
                                 ("检测新节点", "check", ["--unchecked"]),
                                 ("全量重测", "check", []),
                                 ("只复核", "verify", [])):
            b = ttk.Button(bar, text=text,
                           command=lambda c=cmd, e=extra: self.start(c, e))
            b.pack(side="left", padx=(8, 0))
        self.btn_stop = ttk.Button(bar, text="■  停止", style="Danger.TButton",
                                   command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开输出目录", command=self.open_output).pack(side="right")

        # 进度
        prog = ttk.Frame(self, style="Card.TFrame", padding=(16, 12))
        prog.pack(fill="x", padx=18, pady=(0, 10))
        row = ttk.Frame(prog, style="Card.TFrame")
        row.pack(fill="x")
        self.lbl_phase = ttk.Label(row, text="就绪", style="Card.TLabel", font=FONT_B)
        self.lbl_phase.pack(side="left")
        self.lbl_pct = ttk.Label(row, text="0%", style="Muted.TLabel")
        self.lbl_pct.pack(side="right")
        self.bar = ttk.Progressbar(prog, maximum=100, value=0)
        self.bar.pack(fill="x", pady=(8, 4))
        self.lbl_detail = ttk.Label(prog, text="点「一键更新」开始，或从左边选单独步骤。",
                                    style="Muted.TLabel")
        self.lbl_detail.pack(anchor="w")

        # 标签页
        nb = self.nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=18, pady=(0, 16))

        self.tab_log = ttk.Frame(nb, style="Card.TFrame")
        self.tab_src = ttk.Frame(nb, style="Card.TFrame")
        self.tab_node = ttk.Frame(nb, style="Card.TFrame")
        nb.add(self.tab_log, text="  运行日志  ")
        nb.add(self.tab_src, text="  数据源  ")
        nb.add(self.tab_node, text="  可用节点  ")

        self._build_log()
        self._build_sources()
        self._build_nodes()

        # 状态栏
        self.lbl_status = tk.Label(self, text="", bg=BG, fg=MUTED, font=FONT_S,
                                   anchor="w")
        self.lbl_status.pack(fill="x", padx=20, pady=(0, 10))

    def _build_log(self):
        wrap = ttk.Frame(self.tab_log, style="Card.TFrame", padding=(14, 12))
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, bg=CARD, fg=TEXT, font=FONT_MONO, relief="flat",
                           wrap="none", padx=6, pady=6, insertbackground=TEXT)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.txt.tag_configure("ok", foreground=GREEN)
        self.txt.tag_configure("err", foreground=RED)
        self.txt.tag_configure("warn", foreground=AMBER)
        self.txt.tag_configure("dim", foreground=MUTED)
        self.txt.configure(state="disabled")

    def _tree(self, parent, cols, widths, anchors=None, height=14):
        wrap = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        wrap.pack(fill="both", expand=True)
        tv = ttk.Treeview(wrap, columns=cols, show="headings", height=height)
        for i, c in enumerate(cols):
            tv.heading(c, text=c, anchor="w")
            tv.column(c, width=widths[i], anchor=(anchors or ["w"] * len(cols))[i],
                      stretch=(i == 0))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("hi", foreground=GREEN)
        tv.tag_configure("lo", foreground=RED)
        tv.tag_configure("mid", foreground=AMBER)
        tv.tag_configure("odd", background="#fafbfd")
        tv.tag_configure("dim", foreground=MUTED)
        return tv

    def _build_sources(self):
        head = ttk.Frame(self.tab_src, style="Card.TFrame", padding=(16, 12, 16, 0))
        head.pack(fill="x")
        row = ttk.Frame(head, style="Card.TFrame")
        row.pack(fill="x")
        self.lbl_src_head = ttk.Label(row, text="", style="Card.TLabel", font=FONT_B)
        self.lbl_src_head.pack(side="left")
        self.var_filter = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="只看订阅仓库", variable=self.var_filter,
                       bg=CARD, fg=MUTED, font=FONT_S, activebackground=CARD,
                       selectcolor=CARD, relief="flat", bd=0,
                       command=self.refresh_sources).pack(side="right")
        ttk.Label(head, text="按通过率排序。产量大但通过率低的源没有价值——"
                             "大合集仓库常见 0.5-3%，小型精选源能到 30-55%。",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        self.tv_src = self._tree(
            self.tab_src,
            ["源", "产量", "通过率", "可用/已测", "独有", "通道", "类型"],
            [330, 80, 80, 100, 80, 80, 90],
            ["w", "e", "e", "e", "e", "center", "center"])

    def _build_nodes(self):
        head = ttk.Frame(self.tab_node, style="Card.TFrame", padding=(16, 12, 16, 0))
        head.pack(fill="x")
        self.lbl_node_head = ttk.Label(head, text="", style="Card.TLabel", font=FONT_B)
        self.lbl_node_head.pack(anchor="w")
        ttk.Label(head, text="存活 = 这个节点第一次被采集到距今多久。不是连续在线时长——"
                             "公开节点断断续续很正常，仅供参考。双击某行可复制该节点链接。",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

        # ---- 导出工具条
        bar = ttk.Frame(self.tab_node, style="Card.TFrame", padding=(16, 10, 16, 4))
        bar.pack(fill="x")

        def combo(parent, label, var, values, width=10):
            ttk.Label(parent, text=label, style="Card.TLabel").pack(side="left")
            cb = ttk.Combobox(parent, textvariable=var, values=values, width=width,
                              state="readonly", font=FONT_S)
            cb.pack(side="left", padx=(5, 14))
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_nodes())
            return cb

        self.var_proto = tk.StringVar(value="全部")
        combo(bar, "分类", self.var_proto,
              ["全部", "vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "tuic"], 11)

        self.var_limit = tk.StringVar(value="全部")
        combo(bar, "数量", self.var_limit,
              ["全部", "最快 10", "最快 20", "最快 50", "最快 100"], 10)

        self.var_lat = tk.StringVar(value="不限")
        combo(bar, "延迟", self.var_lat,
              ["不限", "≤ 800ms", "≤ 1500ms", "≤ 3000ms"], 11)

        self.var_fmt = tk.StringVar(value="Clash 配置（完整）")
        combo(bar, "格式", self.var_fmt,
              ["Clash 配置（完整）", "仅节点列表（YAML）",
               "base64 订阅", "明文链接"], 18)

        self.btn_save = ttk.Button(bar, text="💾  导出文件", command=self.save_export)
        self.btn_save.pack(side="right")
        self.btn_copy = ttk.Button(bar, text="📋  复制到剪贴板", style="Accent.TButton",
                                   command=self.copy_export)
        self.btn_copy.pack(side="right", padx=(0, 8))

        self.lbl_toast = tk.Label(self.tab_node, text="", bg=CARD, fg=GREEN,
                                  font=FONT_S, anchor="w")
        self.lbl_toast.pack(fill="x", padx=18)

        self.tv_node = self._tree(
            self.tab_node,
            ["节点", "协议", "地址", "延迟", "存活", "通过次数", "首次发现"],
            [210, 85, 260, 80, 90, 85, 145],
            ["w", "center", "w", "e", "e", "e", "center"])

        self.tv_node.bind("<Double-1>", self._copy_one)
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="复制这个节点的链接", command=self._copy_one)
        self._menu.add_command(label="复制选中行的链接", command=self._copy_selected)
        self.tv_node.bind("<Button-3>", self._popup)

    # ------------------------------------------------------------ 导出
    def _current_rows(self) -> list[dict]:
        """按当前筛选条件返回节点（导出和表格共用同一份）。"""
        rows = self.node_rows
        proto = self.var_proto.get()
        if proto != "全部":
            rows = [r for r in rows if r["type"] == proto]
        lat = self.var_lat.get()
        if lat != "不限":
            cap = int(lat.replace("≤", "").replace("ms", "").strip())
            rows = [r for r in rows if r.get("latency") and r["latency"] <= cap]
        lim = self.var_limit.get()
        if lim.startswith("最快"):
            rows = rows[: int(lim.replace("最快", "").strip())]
        return rows

    def _render(self, rows: list[dict]) -> tuple[str, str, str]:
        """按选定格式生成内容，返回 (文本, 扩展名, 格式说明)。"""
        from nodehub import exporter
        fmt = self.var_fmt.get()
        if not rows:
            return "", "txt", "没有符合条件的节点"

        if fmt.startswith("Clash"):
            import yaml as _y
            conf = exporter.build_clash_config(rows)
            return (_y.safe_dump(conf, allow_unicode=True, sort_keys=False),
                    "yaml", "Clash 配置")
        if fmt.startswith("仅节点"):
            import yaml as _y
            ps = exporter.build_proxies(rows)
            ps = [{k: v for k, v in p.items() if not k.startswith("_")} for p in ps]
            return _y.safe_dump({"proxies": ps}, allow_unicode=True, sort_keys=False), \
                "yaml", "仅节点列表"
        uris = exporter.nodes_to_uris(rows)
        if fmt == "base64 订阅":
            import base64 as _b
            return _b.b64encode("\n".join(uris).encode()).decode(), "txt", "base64 订阅"
        return "\n".join(uris), "txt", "明文链接"

    def copy_export(self):
        rows = self._current_rows()
        text, _, desc = self._render(rows)
        if not text:
            self._toast("没有符合条件的节点，换个筛选条件试试。", RED)
            return
        set_clipboard(text)
        self._toast(f"已复制 {len(rows)} 个节点（{desc}，{len(text):,} 字符）到剪贴板 ✓", GREEN)

    def save_export(self):
        rows = self._current_rows()
        text, ext, desc = self._render(rows)
        if not text:
            self._toast("没有符合条件的节点，换个筛选条件试试。", RED)
            return
        default = f"nodes_{len(rows)}个_{time.strftime('%m%d_%H%M')}.{ext}"
        path = filedialog.asksaveasfilename(
            title="导出节点", initialdir=OUTDIR, initialfile=default,
            defaultextension=f".{ext}",
            filetypes=[("YAML 配置", "*.yaml"), ("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._toast(f"已导出 {len(rows)} 个节点（{desc}）→ {os.path.basename(path)} ✓", GREEN)
        except Exception as e:
            self._toast(f"导出失败：{e}", RED)

    def _node_uri(self, iid: str) -> str:
        from nodehub import exporter
        idx = self.tv_node.index(iid)
        rows = self._current_rows()
        if 0 <= idx < len(rows):
            u = exporter.nodes_to_uris([rows[idx]])
            return u[0] if u else ""
        return ""

    def _copy_one(self, event=None):
        iid = self.tv_node.focus() if event is None else self.tv_node.identify_row(event.y)
        if not iid:
            return
        u = self._node_uri(iid)
        if u:
            set_clipboard(u)
            self._toast(f"已复制该节点链接（{len(u)} 字符）✓", GREEN)

    def _copy_selected(self):
        ids = self.tv_node.selection()
        uris = [self._node_uri(i) for i in ids]
        uris = [u for u in uris if u]
        if uris:
            set_clipboard("\n".join(uris))
            self._toast(f"已复制 {len(uris)} 个节点链接 ✓", GREEN)

    def _popup(self, event):
        iid = self.tv_node.identify_row(event.y)
        if iid:
            self.tv_node.selection_set(iid)
            self.tv_node.focus(iid)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _toast(self, msg: str, color: str = GREEN):
        self.lbl_toast.configure(text=msg, fg=color)
        if getattr(self, "_toast_job", None):
            try:
                self.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.after(6000, lambda: self.lbl_toast.configure(text=""))

    # ------------------------------------------------------------ 数据刷新
    def refresh_all(self):
        self.refresh_stats()
        self.refresh_sources()
        self.refresh_nodes()

    def refresh_stats(self):
        s = db_query("SELECT COUNT(*) n, SUM(alive) a FROM nodes")
        total = s[0]["n"] if s else 0
        alive = (s[0]["a"] or 0) if s else 0
        src = db_query("SELECT COUNT(*) n FROM sources WHERE last_count > 0")
        self.stat_widgets["total"].configure(text=f"{total:,}")
        self.stat_widgets["alive"].configure(text=f"{alive:,}")
        self.stat_widgets["src"].configure(text=f"{src[0]['n'] if src else 0}")
        self.stat_widgets["last"].configure(
            text=f"{self.last_elapsed:.0f}s" if self.last_elapsed else "-")

        proxy, ok = read_proxy()
        if ok:
            self.lbl_proxy.configure(text="● 代理已开启（会一起抓 TG 频道）", fg=GREEN)
        else:
            self.lbl_proxy.configure(text="○ 代理未开启（跳过 TG 频道）", fg=MUTED)

    def refresh_sources(self):
        rows = db_query("""
            SELECT s.name, s.last_count AS y,
                   COUNT(DISTINCT n.fp) AS tested,
                   COUNT(DISTINCT CASE WHEN n.alive=1 THEN n.fp END) AS alive
            FROM sources s
            LEFT JOIN node_sources ns ON ns.source_id = s.id
            LEFT JOIN nodes n ON n.fp = ns.fp
                 AND n.last_check IS NOT NULL AND n.last_error != 'tcp_unreachable'
            GROUP BY s.name
        """)
        # 独有节点：只被这一个源收录的。这个查询有点重，缓存 30 秒
        if self._ex_cache is None or time.time() - self._ex_ts > 30:
            self._ex_cache = {r["name"]: r["ex"] for r in db_query("""
                SELECT s.name, COUNT(*) AS ex
                FROM node_sources ns JOIN sources s ON s.id = ns.source_id
                WHERE ns.fp IN (SELECT fp FROM node_sources GROUP BY fp HAVING COUNT(*) = 1)
                GROUP BY s.name
            """)}
            self._ex_ts = time.time()
        ex_map = self._ex_cache

        reg = {}
        try:
            import yaml
            with open(os.path.join(ROOT, "sources", "registry.yaml"), encoding="utf-8") as f:
                for x in (yaml.safe_load(f) or {}).get("sources", []):
                    reg[x["id"]] = x
        except Exception:
            pass

        items = []
        for r in rows:
            meta = reg.get(r["name"], {})
            if meta.get("status") == "archived":
                continue
            tested = r["tested"] or 0
            alive = r["alive"] or 0
            if tested < 10:
                rate = None          # 样本太少，不参与排名
            else:
                rate = alive * 100 / tested
            items.append({
                "name": r["name"], "y": meta.get("yield") or r["y"] or 0,
                "tested": tested, "alive": alive, "rate": rate,
                "ex": ex_map.get(r["name"], 0),
                "reach": {"direct": "直连", "proxy": "需代理",
                          "both": "都行"}.get(meta.get("reach"), "-"),
                "kind": meta.get("kind", "github"),
            })

        only_repo = self.var_filter.get()
        if only_repo:
            items = [x for x in items if x["kind"] != "telegram"]
        # 有通过率的排前面，再按通过率、可用数
        items.sort(key=lambda x: (x["rate"] is None, -(x["rate"] or 0), -x["alive"]))

        self.tv_src.delete(*self.tv_src.get_children())
        for i, it in enumerate(items):
            if it["rate"] is None:
                rate_s, tag = "样本少", "dim"
            else:
                rate_s = f"{it['rate']:.1f}%"
                tag = "hi" if it["rate"] >= 20 else ("mid" if it["rate"] >= 5 else "lo")
            self.tv_src.insert(
                "", "end",
                values=(it["name"], f"{it['y']:,}", rate_s,
                        f"{it['alive']}/{it['tested']}", it["ex"],
                        it["reach"], it["kind"]),
                tags=(tag,))

        good = [x for x in items if x["rate"] is not None and x["rate"] >= 20]
        distinct = db_query("SELECT COUNT(*) n FROM nodes WHERE alive=1")
        self.lbl_src_head.configure(
            text=f"显示 {len(items)} 个源　｜　通过率 ≥20% 的 {len(good)} 个　｜　"
                 f"可用节点 {distinct[0]['n'] if distinct else 0} 个　｜　"
                 f"通过率 = 真连测试过的节点里最终可用的比例")

    def refresh_nodes(self):
        rows = db_query("""
            SELECT fp, name, type, server, port, latency, first_seen, ok_count, uri, clash
            FROM nodes WHERE alive=1
            ORDER BY latency IS NULL, latency ASC
        """)
        self.node_rows = rows
        view = self._current_rows()

        self.tv_node.delete(*self.tv_node.get_children())
        for i, r in enumerate(view):
            lat = r["latency"]
            lat_s = f"{lat} ms" if lat else "-"
            tag = "hi" if lat and lat < 800 else ("mid" if lat and lat < 2000 else "")
            if not tag and i % 2:
                tag = "odd"
            # 名字没意义就用和导出一致的编号，客户端里看到的就是这个
            label = (r["name"] or "").strip()[:44]
            if not label or label in (f"{r['server']}:{r['port']}", r["server"]):
                lat_txt = f" {lat}ms" if lat else ""
                label = f"{r['type'].upper()}-{i+1:03d}{lat_txt}"
            self.tv_node.insert(
                "", "end",
                values=(label, r["type"],
                        f"{r['server']}:{r['port']}", lat_s,
                        human_days(r["first_seen"]), r["ok_count"],
                        (r["first_seen"] or "")[:16]),
                tags=(tag,))

        lats = [r["latency"] for r in view if r["latency"]]
        avg = int(sum(lats) / len(lats)) if lats else 0
        filt = "（已筛选）" if len(view) != len(rows) else ""
        self.lbl_node_head.configure(
            text=f"可用节点 {len(view)} 个{filt}　｜　最快 {min(lats) if lats else '-'} ms　｜　"
                 f"平均 {avg} ms　｜　库内可用共 {len(rows)} 个")

    # ------------------------------------------------------------ 任务
    def start(self, cmd: str, extra: list[str] | None = None):
        if self.running:
            return
        self.running = True
        self.progress = 0
        self.bar["value"] = 0
        self.lbl_pct.configure(text="0%")
        self.btn_all.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.clear_log()
        extra = extra or []
        self.log("$ run.py " + cmd + " " + " ".join(extra) + "\n", "dim")

        args = [PIPELINE_PY, "-u", "run.py", cmd] + extra
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        creation = 0x08000000 if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, creationflags=creation, bufsize=1,
                universal_newlines=True, encoding="utf-8", errors="replace")
        except Exception as e:
            self.log(f"启动失败: {e}\n", "err")
            self.finish()
            return
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        t0 = time.time()
        try:
            for line in self.proc.stdout:
                self.q.put(("line", line.rstrip("\n")))
        except Exception as e:
            self.q.put(("line", f"[读取失败] {e}"))
        code = self.proc.wait() if self.proc else -1
        self.q.put(("done", (code, time.time() - t0)))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.log("\n[已请求停止]\n", "warn")
            except Exception:
                pass

    def finish(self):
        self.running = False
        self.proc = None
        self._ex_cache = None          # 跑完重算独有数
        self.btn_all.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.refresh_all()

    # ------------------------------------------------------------ 输出解析
    def log(self, text: str, tag: str = ""):
        self.txt.configure(state="normal")
        self.txt.insert("end", text + ("\n" if not text.endswith("\n") else ""), tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _set_progress(self, pct: float, phase: str | None = None, detail: str | None = None):
        self.progress = max(self.progress, min(100.0, pct))
        self.bar["value"] = self.progress
        self.lbl_pct.configure(text=f"{int(self.progress)}%")
        if phase:
            self.lbl_phase.configure(text=phase)
        if detail is not None:
            self.lbl_detail.configure(text=detail)

    def _handle_line(self, line: str):
        # 颜色
        tag = ""
        if line.startswith("  ✓") or "OK " in line[:6]:
            tag = "ok"
        elif line.startswith("  ✗") or "失败" in line or "错误" in line:
            tag = "err"
        elif "跳过" in line or "⚠" in line:
            tag = "warn"
        elif line.startswith("===") or line.startswith("  ["):
            tag = "dim"
        self.log(line, tag)

        # 进度解析
        if "步骤 1 / 搜集" in line:
            self._set_progress(2, "采集", "正在抓取所有订阅源…")
        m = re.search(r"完成: (\d+)/(\d+) 个源有效", line)
        if m:
            self._set_progress(18, "采集完成",
                               f"{m.group(1)}/{m.group(2)} 个源有节点")
        if "步骤 2 / 管理" in line:
            self._set_progress(20, "入库", "跨源去重、清理过期…")
        m = re.search(r"节点库总量: ([\d,]+)", line)
        if m:
            self._set_progress(22, None, f"库内共 {m.group(1)} 个节点")
        m = re.search(r"待检测: (\d+) 个节点", line)
        if m:
            self._set_progress(23, "TCP 预筛",
                               f"待检测 {m.group(1)} 个节点，先测端口通不通…")
        m = re.search(r"\[1/2\] TCP 预筛: (\d+) 通过 / (\d+) 淘汰", line)
        if m:
            self._set_progress(45, "真实网页访问",
                               f"TCP 通过 {m.group(1)} 个，开始模拟网页访问…")
        m = re.search(r"真连测试 (\d+)-(\d+) / (\d+)", line)
        if m:
            end, total = int(m.group(2)), int(m.group(3))
            pct = 45 + 40 * (end / max(total, 1))
            self._set_progress(pct, "真实网页访问",
                               f"正在验证 {end}/{total} 个节点能否真的打开网页…")
        m = re.search(r"\[2/2\] 真连测试: (\d+) 可用", line)
        if m:
            self._set_progress(87, None, f"{m.group(1)} 个节点可用")
        if "复核 —— " in line:
            self._set_progress(89, "复核", "对判定可用的节点再测一遍…")
        m = re.search(r"复核通过 (\d+)/(\d+)", line)
        if m:
            self._set_progress(96, "复核完成", f"{m.group(1)}/{m.group(2)} 通过")
        if line.strip() == "  完成":
            self._set_progress(100, "完成", "全部步骤跑完")

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._handle_line(payload)
                elif kind == "done":
                    code, elapsed = payload
                    self.last_elapsed = elapsed
                    if code == 0:
                        self._set_progress(100, "完成", f"耗时 {elapsed:.0f} 秒")
                        self.lbl_status.configure(
                            text=f"上次运行成功 · {time.strftime('%H:%M:%S')} · 耗时 {elapsed:.0f}s",
                            fg=GREEN)
                    else:
                        self.lbl_phase.configure(text="已中断")
                        self.lbl_status.configure(text=f"进程退出码 {code}", fg=RED)
                    self.finish()
        except queue.Empty:
            pass

        # 运行中每 3 秒刷一次统计，能实时看到库内变化
        if self.running and time.time() - self._last_refresh > 3:
            self._last_refresh = time.time()
            self.refresh_stats()

        self.after(120, self._poll)

    def open_output(self):
        if not os.path.exists(OUTDIR):
            messagebox.showinfo("提示", "还没生成 output 目录，先跑一次。")
            return
        try:
            os.startfile(OUTDIR)      # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("提示", f"目录位置：\n{OUTDIR}")

    def on_close(self):
        if self.running:
            if not messagebox.askokcancel("确认退出", "任务还在跑，确定要中断并退出吗？"):
                return
            self.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
