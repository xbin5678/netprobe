#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Telegram 频道扫描器
------------------
从 TG 采集器项目里拿频道清单（目前用 Surfboardv2ray/TGParse 的 180 个频道），
逐个抓 t.me/s/<频道> 网页版，看能出多少节点。

必须开代理——国内网络直连 t.me 不通。

  python -u tools/tg_scan.py                # 只扫描，出报告
  python -u tools/tg_scan.py --add          # 把能出节点的写进台账（status=active）
  python -u tools/tg_scan.py --min 10       # 只收 ≥10 个节点的频道
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests
import yaml

from nodehub.parser import parse_uri
from nodehub import fetcher

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
URI_RE = re.compile(r"(?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\"'<>\\]+")
CH_LIST_URL = ("https://raw.githubusercontent.com/Surfboardv2ray/TGParse/"
               "main/telegramchannels.json")


def load_channels(cfg: dict) -> list[str]:
    """优先用本地缓存，没有就从 GitHub 拉。"""
    p = os.path.join(ROOT, "data", "tg_channels.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list) and d:
                return [str(x).strip().lstrip("@") for x in d if str(x).strip()]
        except Exception:
            pass
    text, err = fetcher.fetch_text(CH_LIST_URL, cfg)
    if not text:
        print(f"! 频道清单拉取失败: {err}")
        return []
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    d = json.loads(text)
    return [str(x).strip().lstrip("@") for x in d if str(x).strip()]


def scan_one(ch: str, cfg: dict, sess: requests.Session):
    url = f"https://t.me/s/{ch}"
    try:
        r = sess.get(url, timeout=(12, 25))
        if r.status_code != 200:
            return ch, 0, f"HTTP {r.status_code}", []
        text = html.unescape(r.text)
        uris = URI_RE.findall(text)
        nodes = [p for p in (parse_uri(x) for x in uris) if p]
        return ch, len(nodes), "ok", nodes
    except Exception as e:
        return ch, 0, type(e).__name__, []


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="扫描 Telegram 频道里的公开节点")
    ap.add_argument("--add", action="store_true", help="把有货的频道写进台账")
    ap.add_argument("--min", type=int, default=5, help="至少多少节点才收录")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["fetch"]
    if not cfg.get("proxy"):
        print("! config.yaml 里没有配 fetch.proxy，t.me 直连不通")
        return
    cfg["use_proxy"] = True

    chans = load_channels(cfg)
    if args.limit:
        chans = chans[: args.limit]
    print(f"扫描 {len(chans)} 个 Telegram 频道（走 {cfg['proxy']}）...\n", flush=True)

    sess = requests.Session()
    sess.trust_env = False
    sess.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    sess.headers.update({"User-Agent": UA})

    results = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(scan_one, c, cfg, sess) for c in chans]
        for fu in cf.as_completed(futs):
            ch, n, status, nodes = fu.result()
            done += 1
            results.append({"channel": ch, "count": n, "status": status, "nodes": nodes})
            if n >= args.min:
                print(f"  ✓ {ch:<30} {n:>4}", flush=True)
            if done % 30 == 0:
                print(f"    ...{done}/{len(chans)}", flush=True)

    good = sorted([r for r in results if r["count"] >= args.min],
                  key=lambda x: -x["count"])
    total = sum(r["count"] for r in good)
    print(f"\n{len(good)}/{len(results)} 个频道能出节点，合计 {total} 条（未去重）\n")
    for r in good:
        print(f"  {r['channel']:<30} {r['count']:>4}")

    out = os.path.join(ROOT, "data", "tg_scan.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in r.items() if k != "nodes"} for r in results],
                  f, ensure_ascii=False, indent=1)
    print(f"\n结果已存: {out}")

    if args.add and good:
        reg_path = os.path.join(ROOT, "sources", "registry.yaml")
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
        exist = {s["id"] for s in reg["sources"]}
        n = 0
        for r in good:
            rid = f"t.me/{r['channel']}"
            if rid in exist:
                continue
            reg["sources"].append({
                "id": rid,
                "url": f"https://t.me/s/{r['channel']}",
                "kind": "telegram",
                "status": "active",
                "format": "tg-html",
                "yield": r["count"],
                "types": [],
                "freq": "realtime",
                "reach": "proxy",
                "last_verified": time.strftime("%Y-%m-%d"),
                "note": f"TG 频道，网页版直接抓（tg_scan 发现，{r['count']} 条）",
                "tags": ["telegram"],
            })
            n += 1
        with open(reg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(reg, f, allow_unicode=True, sort_keys=False, width=120)
        print(f"已写入台账: {n} 个新频道（status=active, reach=proxy）")


if __name__ == "__main__":
    main()
