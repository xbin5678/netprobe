#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
源发现器：用 GitHub 搜索 API 系统性扫新仓库
------------------------------------------
不靠记忆猜仓库名，而是按关键词搜「最近有推送」的仓库。

探测策略（按成本从低到高）：
  1. 先读仓库 README，用正则把里面出现的订阅地址抠出来 —— 大多数聚合仓库都会写
  2. 没有的话再试探 6 个最常见的产物路径
  3. 能出 ≥5 个节点才算数

  python -u tools/discover.py                  # 默认 10 组关键词
  python -u tools/discover.py --days 2 --stars 20
  python -u tools/discover.py --add            # 写进台账候选池

GitHub 搜索 API 未认证限速 10 次/分钟，脚本自动间隔。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from nodehub import fetcher

QUERIES = [
    "free v2ray nodes",
    "v2ray subscription",
    "clash subscription",
    "free clash nodes",
    "vless subscription",
    "free proxy nodes",
    "公开节点",
    "کانفیگ رایگان",
    "бесплатный v2ray",
    "free vpn configs",
]

# 只保留命中率最高的几个路径
CAND_PATHS = ["sub", "list.yml", "clash.yaml", "v2ray.txt", "sub.txt", "list.txt"]

RAW_LINK = re.compile(r"https?://raw\.githubusercontent\.com/[^\s\"'<>)\]]+")
GHPROXY_LINK = re.compile(r"https?://[^\s\"'<>)\]]*?/(raw\.githubusercontent\.com/[^\s\"'<>)\]]+)")


def gh_search(q: str, days: int, stars: int, per_page: int = 50) -> list[dict]:
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{q} pushed:>={since} stars:>={stars}"
    url = ("https://api.github.com/search/repositories?q="
           + urllib.parse.quote(query)
           + f"&sort=updated&order=desc&per_page={per_page}")
    try:
        import requests
        r = requests.get(url, timeout=(10, 30),
                         headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": "nodehub-discover"})
        if r.status_code != 200:
            print(f"    ! {q}: HTTP {r.status_code}", flush=True)
            return []
        return r.json().get("items") or []
    except Exception as e:
        print(f"    ! {q}: {type(e).__name__}", flush=True)
        return []


def probe_url(url: str, cfg: dict):
    text, _ = fetcher.fetch_text(url, cfg)
    if not text:
        return 0, ""
    nodes, fmt = fetcher.parse_subscription(text)
    return len(nodes), fmt


def find_source(repo: str, cfg: dict):
    """返回 (分支, 路径, 节点数, 格式) 或 None。"""
    # --- 第一步：从 README 里抠订阅地址（最省）
    links = []
    for br in ("main", "master"):
        text, _ = fetcher.fetch_text(
            f"https://raw.githubusercontent.com/{repo}/{br}/README.md", cfg)
        if not text:
            continue
        for m in RAW_LINK.finditer(text):
            links.append(m.group(0).rstrip(".,;"))
        for m in GHPROXY_LINK.finditer(text):
            links.append("https://" + m.group(1))
        break
    seen = set()
    for link in links:
        if link in seen or "/releases/" in link:
            continue
        seen.add(link)
        if len(seen) > 4:
            break
        n, fmt = probe_url(link, cfg)
        if n >= 5:
            parts = link.split("raw.githubusercontent.com/")[-1].split("/")
            if len(parts) >= 4:
                return parts[2], "/".join(parts[3:]), n, fmt
            return "main", link, n, fmt

    # --- 第二步：试探常见产物路径
    for br in ("main", "master"):
        for p in CAND_PATHS:
            n, fmt = probe_url(f"https://raw.githubusercontent.com/{repo}/{br}/{p}", cfg)
            if n >= 5:
                return br, p, n, fmt
    return None


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="用 GitHub 搜索发现新数据源")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--stars", type=int, default=10)
    ap.add_argument("--max-repos", type=int, default=50)
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["fetch"]

    reg_path = os.path.join(ROOT, "sources", "registry.yaml")
    with open(reg_path, encoding="utf-8") as f:
        reg = yaml.safe_load(f) or {}
    known = set()
    for s in reg.get("sources") or []:
        m = s["url"].split("raw.githubusercontent.com/")[-1].split("/")
        if len(m) >= 2:
            known.add(f"{m[0]}/{m[1]}")

    print(f"搜索 {len(QUERIES)} 组关键词（近 {args.days} 天，star ≥ {args.stars}）...\n", flush=True)
    repos: dict[str, dict] = {}
    for i, q in enumerate(QUERIES):
        items = gh_search(q, args.days, args.stars)
        new = 0
        for it in items:
            fn = it["full_name"]
            if fn in known or fn in repos:
                continue
            repos[fn] = {
                "repo": fn,
                "stars": it["stargazers_count"],
                "pushed": it["pushed_at"][:10],
                "desc": (it.get("description") or "")[:60],
                "q": q,
            }
            new += 1
        print(f"  [{i+1}/{len(QUERIES)}] {q:<22} 命中 {len(items):>3}，新增候选 {new}", flush=True)
        if i < len(QUERIES) - 1:
            time.sleep(6.5)

    cand = sorted(repos.values(), key=lambda x: -x["stars"])[: args.max_repos]
    print(f"\n共 {len(repos)} 个未知仓库，探测前 {len(cand)} 个...\n", flush=True)

    found, done = [], 0

    def work(r):
        return r, find_source(r["repo"], cfg)

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, r) for r in cand]
        for fu in cf.as_completed(futs):
            r, hit = fu.result()
            done += 1
            if hit:
                br, path, n, fmt = hit
                r.update(branch=br, path=path, count=n, format=fmt)
                found.append(r)
                print(f"  ✓ {r['repo']:<44} ★{r['stars']:<5} {n:>5}  {path}", flush=True)
            if done % 10 == 0:
                print(f"    ...{done}/{len(cand)}，已找到 {len(found)} 个", flush=True)

    print(f"\n找到 {len(found)} 个能出节点的源：\n", flush=True)
    for r in sorted(found, key=lambda x: -x["count"]):
        print(f"  {r['repo']:<44} ★{r['stars']:<5} {r['count']:>5}  "
              f"{r['branch']}/{r['path']}", flush=True)

    out = os.path.join(ROOT, "data", "discovered.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=1)
    print(f"\n结果已存: {out}", flush=True)

    if args.add and found:
        n = 0
        for r in found:
            rid = r["repo"]
            if any(s["id"] == rid for s in reg["sources"]):
                continue
            reg["sources"].append({
                "id": rid,
                "url": f"https://raw.githubusercontent.com/{rid}/{r['branch']}/{r['path']}",
                "kind": "github",
                "status": "candidate",
                "format": r["format"],
                "yield": r["count"],
                "types": [],
                "freq": "unknown",
                "last_verified": time.strftime("%Y-%m-%d"),
                "note": f"discover 自动发现（★{r['stars']}，搜索词「{r['q']}」）",
                "tags": ["discovered"],
            })
            n += 1
        with open(reg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(reg, f, allow_unicode=True, sort_keys=False, width=120)
        print(f"已写入台账候选池: {n} 条", flush=True)


if __name__ == "__main__":
    main()
