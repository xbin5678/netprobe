#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
反向溯源：从节点名里挖新源线索
----------------------------
节点名经常带来源标记（频道名、机场名、仓库名）。把库里所有节点名做词频统计，
剔除已知来源和常见地名/协议词，剩下的高频词就是还没被收录的源线索。

  python tools/trace_names.py            # 输出 Top 50
  python tools/trace_names.py --top 100
  python tools/trace_names.py --grep vpn # 只看着含 vpn 的
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from nodehub.store import Store

# 常见地名 / 协议 / 通用词，不算线索
STOP = {
    # 国家地区
    "us", "hk", "jp", "sg", "kr", "tw", "uk", "de", "fr", "nl", "ru", "ca", "au", "in",
    "tr", "br", "vn", "th", "my", "ph", "id", "ae", "sa", "il", "ir", "pk", "bd", "lk",
    "usa", "japan", "korea", "taiwan", "singapore", "germany", "france", "russia",
    "china", "hongkong", "hong", "kong", "united", "states", "america", "canada",
    "netherlands", "turkey", "india", "brazil", "vietnam", "thailand", "malaysia",
    "tokyo", "osaka", "seoul", "london", "paris", "frankfurt", "amsterdam",
    # 协议 / 客户端 / 通用
    "vmess", "vless", "trojan", "ssr", "ss", "hysteria", "hysteria2", "tuic", "reality",
    "clash", "mihomo", "singbox", "sing", "box", "v2ray", "xray", "node", "nodes",
    "free", "proxy", "proxies", "server", "cdn", "relay", "tls", "ws", "grpc", "http",
    "https", "tcp", "udp", "ipv4", "ipv6", "auto", "none", "default", "unknown",
    "com", "net", "org", "www", "top", "xyz", "info", "cloud", "online", "site",
    "premium", "vip", "fast", "speed", "plus", "pro", "max", "new", "test",
    "line", "nodefree", "share", "sub", "subscribe", "config", "update",
}


def tokens(name: str) -> list[str]:
    # 去掉 emoji / 符号，保留字母数字和中文
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", name)
    out = []
    for t in s.split():
        t = t.strip().lower()
        if len(t) < 3:
            continue
        if t.isdigit():
            continue
        # 纯 IP 段
        if re.fullmatch(r"\d+", t):
            continue
        out.append(t)
    return out


# 高信号来源标记：TG 频道提及、t.me 链接、中文品牌词
RE_MENTION = re.compile(r"@([A-Za-z0-9_]{3,32})")
RE_TME = re.compile(r"t\.me/([A-Za-z0-9_]{3,32})", re.I)
RE_CN = re.compile(r"[\u4e00-\u9fff]{2,8}")

# 域名里常见但无意义的片段
DOMAIN_NOISE = {
    "autos", "space", "workers", "production", "dashboard", "network", "work",
    "shop", "direct", "speedtest", "www", "api", "cdn", "static", "app", "apps",
    "dev", "xyz", "top", "vip", "fun", "cyou", "xin", "monster", "site", "online",
    "cloud", "host", "server", "service", "edge", "gateway", "proxy", "tun", "tunnel",
}


def strong_signals(name: str) -> list[tuple[str, str]]:
    """返回 [(类别, 词)]，只保留真正能指向数据源的特征。"""
    out = []
    for m in RE_MENTION.finditer(name):
        out.append(("TG频道", "@" + m.group(1).lower()))
    for m in RE_TME.finditer(name):
        out.append(("TG链接", "@" + m.group(1).lower()))
    for m in RE_CN.finditer(name):
        w = m.group(0)
        if len(w) >= 2 and w not in ("免费", "节点", "香港", "台湾", "日本", "美国", "新加坡",
                                     "韩国", "德国", "英国", "法国", "俄罗斯", "土耳其",
                                     "印度", "巴西", "越南", "泰国", "马来西亚", "菲律宾",
                                     "中转", "直连", "专线", "优化", "高速", "试用", "官网",
                                     "官网节点", "剩余", "流量", "到期"):
            out.append(("中文品牌", w))
    return out


def main():
    ap = argparse.ArgumentParser(description="从节点名反向溯源新数据源")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--grep", default="")
    ap.add_argument("--db", default="data/nodes.db")
    ap.add_argument("--registry", default="sources/registry.yaml")
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    store = Store(os.path.join(ROOT, args.db))
    rows = store.get_nodes()
    if not rows:
        print("节点库是空的，先跑 collect")
        return

    # 已知来源里的词，从结果里剔掉
    known = set()
    reg_path = os.path.join(ROOT, args.registry)
    if os.path.exists(reg_path):
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
        for s in reg.get("sources") or []:
            known.update(tokens(s["id"]))
            known.update(tokens(s["url"].split("/")[-1]))

    cnt = Counter()
    examples = defaultdict(list)
    kinds = {}
    for r in rows:
        name = r.get("name") or ""
        seen = set()
        # 1) 高信号：@频道 / t.me / 中文品牌
        for kind, w in strong_signals(name):
            if w in seen:
                continue
            seen.add(w)
            cnt[w] += 1
            kinds.setdefault(w, kind)
            if len(examples[w]) < 3:
                examples[w].append(name[:56])
        # 2) 低信号：域名片段（过滤掉无意义词）
        for t in tokens(name):
            if t in STOP or t in known or t in DOMAIN_NOISE or len(t) < 5:
                continue
            if t in seen or re.search(r"\d{3,}", t):
                continue
            seen.add(t)
            cnt[t] += 1
            kinds.setdefault(t, "域名片段")
            if len(examples[t]) < 3:
                examples[t].append(name[:56])

    items = [(w, c) for w, c in cnt.most_common(args.top * 4) if c >= 3]
    if args.grep:
        items = [(w, c) for w, c in items if args.grep.lower() in w]
    items = items[:args.top]

    print(f"扫描 {len(rows)} 个节点的名称，候选线索 {len(items)} 个\n")

    def show(title, rows_):
        if not rows_:
            return
        print(f"\n【{title}】")
        print(f"{'线索':<26}{'出现':>6}   示例节点名")
        print("-" * 96)
        for w, c in rows_:
            print(f"{w:<26}{c:>6}   {examples[w][0]}")

    show("高信号 · 直接可查（TG 频道 / 中文品牌）",
         [(w, c) for w, c in items if kinds[w] in ("TG频道", "TG链接", "中文品牌")])
    show("低信号 · 需人工判断（域名片段）",
         [(w, c) for w, c in items if kinds[w] == "域名片段"])

    print("\n下一步：把高信号里的词直接拿去查")
    print("  Telegram: https://t.me/s/<去掉@的词>")
    print("  GitHub  : https://github.com/search?q=<词>&type=repositories&s=updated&o=desc")
    print("  确认是真实来源后：python tools/sources_tool.py add <名字> <订阅URL>")


if __name__ == "__main__":
    main()
