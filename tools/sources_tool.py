#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据源台账工具
-------------
  python tools/sources_tool.py sync     探测所有源 -> 回填 registry.yaml -> 重生成 SOURCES.md
  python tools/sources_tool.py doc      只重新生成 SOURCES.md（不探测）
  python tools/sources_tool.py add NAME URL [--kind github] [--note "..."]

sync 只做「源」这一层的判断（能不能返回可解析的节点列表），
不做节点可用性检测——那需要干净的出口网络。
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

from nodehub import fetcher
from nodehub.parser import parse_uri

REGISTRY = os.path.join(ROOT, "sources", "registry.yaml")
METHOD = os.path.join(ROOT, "sources", "_method.md")
DOC = os.path.join(ROOT, "SOURCES.md")
CFG = os.path.join(ROOT, "config.yaml")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
URI_RE = re.compile(r"(?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\"'<>\\]+")

KIND_LABEL = {
    "github": "GitHub",
    "telegram": "Telegram",
    "site": "站点",
    "aggregator": "聚合",
}


def load_fetch_cfg() -> dict:
    with open(CFG, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("fetch", {})


# ---------------------------------------------------------------- 探测

def probe_telegram(url: str, cfg: dict):
    try:
        sess = requests.Session()
        sess.trust_env = False
        proxy = (cfg.get("proxy") or "") if cfg.get("use_proxy") else ""
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        sess.headers.update({"User-Agent": UA})
        r = sess.get(url, timeout=(15, 30))
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        text = html.unescape(r.text)
        uris = URI_RE.findall(text)
        nodes = [p for p in (parse_uri(x) for x in uris) if p]
        return nodes, f"tg-html({len(uris)} uris)"
    except Exception as e:
        return [], type(e).__name__


def probe(src: dict, cfg: dict) -> dict:
    url, kind = src["url"], src.get("kind", "github")
    t0 = time.time()
    if kind == "telegram":
        nodes, fmt = probe_telegram(url, cfg)
    else:
        nodes, fmt = [], "fail"
        offsets = [0, 1, 2] if re.search(r"\{(YYYYMMDD|YYYY|MM|DD)", url) else [0]
        for off in offsets:
            real = fetcher.render_date(url, off)
            text, used = fetcher.fetch_text(real, cfg)
            if not text:
                fmt = f"fetch:{used}"
                continue
            nodes, fmt = fetcher.parse_subscription(text)
            if nodes:
                break
            fmt = f"0 nodes ({fmt})"
    return {
        "yield": len(nodes),
        "format": fmt,
        "types": sorted({p.get("type") for p in nodes}) if nodes else [],
        "elapsed": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- 台账

def load_registry() -> dict:
    with open(REGISTRY, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_registry(doc: dict) -> None:
    doc["updated"] = time.strftime("%Y-%m-%d")
    with open(REGISTRY, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=120)


def cmd_sync(args):
    doc = load_registry()
    cfg = load_fetch_cfg()

    # 采集通道：direct = 不走代理（直连），proxy = 走 config 里配的代理
    # 两种通道能拿到的源不一样，所以要分别记录
    mode = args.mode
    if mode == "direct":
        cfg["use_proxy"] = False
    elif mode == "proxy":
        cfg["use_proxy"] = True
        if not cfg.get("proxy"):
            print("! config.yaml 里没有配 fetch.proxy，无法走代理")
            return

    srcs = doc.get("sources") or []

    targets = srcs
    if args.only:
        targets = [s for s in srcs if s.get("status") == args.only]
    if args.id:
        targets = [s for s in targets if s["id"] in args.id]

    print(f"探测 {len(targets)} 个源（并发 {args.workers}，通道 {mode}）...\n")
    results = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe, s, cfg): s for s in targets}
        for fu in cf.as_completed(futs):
            s = futs[fu]
            r = fu.result()
            results[s["id"]] = r
            mark = "OK " if r["yield"] else "-- "
            print(f"  {mark}{s['id']:<32} {r['yield']:>6}  {r['format'][:28]:<28} {r['elapsed']}s")

    today = time.strftime("%Y-%m-%d")
    for s in srcs:
        r = results.get(s["id"])
        if not r:
            continue
        s["yield"] = r["yield"]
        s["format"] = r["format"]
        s["types"] = r["types"]
        s["last_verified"] = today
        if r["yield"] > 0:
            # 记录这个源在哪种通道下可达
            prev = s.get("reach")
            if mode == "auto":
                pass
            elif prev in (None, "", mode):
                s["reach"] = mode
            else:
                s["reach"] = "both"
            if s.get("status") == "dead" and r["yield"] >= 5:
                s["status"] = "candidate"
        else:
            if s.get("status") == "active":
                s["status"] = "candidate"
                s["note"] = (s.get("note", "") + " ｜ 本次探测为 0").strip(" ｜")

    save_registry(doc)
    build_doc(doc)

    alive = [s for s in srcs if s.get("yield")]
    print(f"\n可用 {len(alive)}/{len(srcs)}，原始节点合计 {sum(s['yield'] for s in alive)} 条")
    print(f"台账已更新: sources/registry.yaml")
    print(f"文档已生成: SOURCES.md")


def cmd_archive(args):
    """把「产量大但通过率极低」的源归档。

    归档 ≠ 删除：条目保留在 registry 里，url / 产量 / 通过率 / 归档原因都在，
    这样 discover.py 的已知集合依然会跳过它们，以后不会重复收录。
    采集流水线只读 status=active，所以归档后就不再抓了。
    """
    doc = load_registry()
    pr = pass_rates()
    srcs = doc.get("sources") or []
    today = time.strftime("%Y-%m-%d")
    moved = []

    for s in srcs:
        if s.get("status") != "active":
            continue
        y = s.get("yield") or 0
        p = pr.get(s["id"])
        if not p or p[1] < 10:
            continue          # 没测够，不动
        rate = p[0] * 100 / p[1]
        if y >= args.min_yield and rate <= args.max_pass:
            s["status"] = "archived"
            s["archived_at"] = today
            s["archive_reason"] = (
                f"产量 {y:,} 但真连通过率仅 {rate:.1f}%（{p[0]}/{p[1]}），"
                f"典型大合集，占用采集和检测时间"
            )
            moved.append((s["id"], y, rate))

    if not moved:
        print("没有符合条件的源")
        return

    save_registry(doc)
    build_doc(doc)
    print(f"已归档 {len(moved)} 个源（条目保留，不会重复发现）：\n")
    for name, y, rate in sorted(moved, key=lambda x: -x[1]):
        print(f"  {name:<44}{y:>7} 条  通过率 {rate:.1f}%")
    print(f"\n台账已更新: sources/registry.yaml")
    print(f"文档已生成: SOURCES.md")


def cmd_doc(args):
    doc = load_registry()
    build_doc(doc)
    print("SOURCES.md 已重新生成")


def cmd_add(args):
    doc = load_registry()
    srcs = doc.setdefault("sources", [])
    if any(s["id"] == args.name for s in srcs):
        print(f"已存在: {args.name}")
        return
    srcs.append({
        "id": args.name,
        "url": args.url,
        "kind": args.kind,
        "status": "candidate",
        "format": "",
        "yield": 0,
        "types": [],
        "freq": "unknown",
        "last_verified": "",
        "note": args.note or "",
        "tags": [],
    })
    save_registry(doc)
    build_doc(doc)
    print(f"已加入候选: {args.name}\n下一步: python tools/sources_tool.py sync --id {args.name}")


# ---------------------------------------------------------------- 文档生成

def pass_rates() -> dict:
    """每个源的真实通过率：真连测试过的节点里有多少最终可用。

    这是判断源质量最硬的指标。TCP 通过率高没有意义——端口开着不代表服务活着。
    """
    import sqlite3
    db = os.path.join(ROOT, "data", "nodes.db")
    if not os.path.exists(db):
        return {}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        q = """SELECT s.name,
                      COUNT(DISTINCT n.fp) AS tested,
                      COUNT(DISTINCT CASE WHEN n.alive = 1 THEN n.fp END) AS alive
               FROM nodes n
               JOIN node_sources ns ON ns.fp = n.fp
               JOIN sources s ON s.id = ns.source_id
               WHERE n.last_check IS NOT NULL AND n.last_error != 'tcp_unreachable'
               GROUP BY s.name"""
        return {r["name"]: (r["alive"], r["tested"]) for r in conn.execute(q)}
    except Exception:
        return {}


def exclusive_counts() -> dict:
    """每个源独有的节点数（该节点只被这一个源提供）。

    这是判断「这个源到底值不值得留」最硬的指标——比产量可靠得多，
    因为产量可以被大量重复内容堆出来。
    """
    import sqlite3
    db = os.path.join(ROOT, "data", "nodes.db")
    if not os.path.exists(db):
        return {}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        q = """SELECT s.name, COUNT(*) AS ex
               FROM node_sources ns JOIN sources s ON s.id = ns.source_id
               WHERE ns.fp IN (SELECT fp FROM node_sources GROUP BY fp HAVING COUNT(*) = 1)
               GROUP BY s.name"""
        out = {r["name"]: r["ex"] for r in conn.execute(q)}
        conn.close()
        return out
    except Exception:
        return {}


def _table(rows: list[dict], ex: dict, pr: dict | None = None) -> str:
    pr = pr or {}
    head = "| 源 | 类型 | 产量 | 独有 | 独有比 | 通过率 | 通道 | 频率 | 说明 |"
    sep = "|---|---|---:|---:|---:|---:|---|---|---|"
    lines = [head, sep]
    for s in rows:
        types = ",".join(s.get("types") or []) or "-"
        note = (s.get("note") or "").replace("|", "\\|")
        y = s.get("yield", 0) or 0
        e = ex.get(s["id"], 0) if ex else None
        e_s = "-" if e is None else str(e)
        r_s = "-" if e is None or not y else f"{e * 100 // y}%"
        reach = {"direct": "直连", "proxy": "需代理", "both": "都行"}.get(s.get("reach"), "-")
        p = pr.get(s["id"])
        if p and p[1] >= 10:
            r = p[0] * 100 / p[1]
            pr_s = f"{r:.1f}%" if r < 10 else f"{r:.0f}%"
        else:
            pr_s = "-"
        lines.append(
            f"| `{s['id']}` | {types} | {y} | {e_s} | {r_s} | {pr_s} | "
            f"{reach} | {s.get('freq', '-')} | {note} |"
        )
    return "\n".join(lines)


def build_doc(doc: dict) -> None:
    srcs = doc.get("sources") or []
    ex = exclusive_counts()
    pr = pass_rates()

    def is_tg(s):
        return s.get("kind") == "telegram" or s["id"].startswith("t.me/")

    def value(s):
        """真实价值 = 独有节点数 × 通过率。

        只看独有会高估「大而全但全是死节点」的源（比如 Leon406 独有 15k 但通过率 0%）；
        只看通过率会高估「小而精但没多少独家内容」的源。两个相乘才是实际能用的独有节点。
        """
        e = ex.get(s["id"], 0)
        p = pr.get(s["id"])
        if not p or p[1] < 10:
            return e * 0.15      # 没测过就按经验值 15% 估
        return e * p[0] / p[1]

    # 按真实价值排序 —— 产量和独有都会骗人
    act_all = [s for s in srcs if s.get("status") == "active"]
    active = sorted([s for s in act_all if not is_tg(s)], key=lambda x: -value(x))
    tg = sorted([s for s in act_all if is_tg(s)], key=lambda x: -value(x))
    cand = sorted([s for s in srcs if s.get("status") == "candidate"], key=lambda x: -x.get("yield", 0))
    archived = sorted([s for s in srcs if s.get("status") == "archived"],
                      key=lambda x: -x.get("yield", 0))
    dead = sorted([s for s in srcs if s.get("status") == "dead"], key=lambda x: x["id"])

    with open(METHOD, encoding="utf-8") as f:
        body = f.read()

    total_yield = sum(s.get("yield", 0) for s in active + tg)
    by_kind = {}
    for s in active + tg:
        by_kind[s.get("kind", "?")] = by_kind.get(s.get("kind", "?"), 0) + 1

    parts = [body.rstrip(), "", "---", "", "## 六、源台账（自动生成）", ""]
    parts.append(f"> 最后同步：**{doc.get('updated', '-')}**　｜　"
                 f"active **{len(active) + len(tg)}** 个"
                 f"（其中 Telegram 频道 {len(tg)} 个）　｜　候选 **{len(cand)}** 个　｜　"
                 f"归档 **{len(archived)}** 个　｜　失效 **{len(dead)}** 个　｜　"
                 f"原始节点合计 **{total_yield:,}** 条（去重前）")
    parts.append("")
    parts.append("按渠道分布：" + "、".join(f"{KIND_LABEL.get(k, k)} {v} 个" for k, v in sorted(by_kind.items())))
    parts.append("")

    parts.append(f"### 6.1 已收录 · 订阅仓库（{len(active)} 个）")
    parts.append("")
    parts.append("这些源已经接进采集流水线，`run.py collect` 会自动抓。")
    parts.append("**排序依据 = 独有 × 通过率**，也就是「这个源实际能贡献多少个能用的独家节点」。")
    parts.append("")
    total_ex = sum(ex.get(s["id"], 0) for s in active)
    parts.append(
        f"**产量**是解析出的节点条数。**独有**是这个源独家提供的节点数（别的源都没有），"
        f"**独有比** = 独有 ÷ 产量。当前 {len(active)} 个源共提供 "
        f"**{total_ex:,}** 个独家节点。"
    )
    parts.append("")

    # 用实际数据说明「产量 ≠ 价值」：自动找一个高产但独有极低的反例
    big = [s for s in active if (s.get("yield") or 0) >= 500]
    counter = None
    for s in big:
        y = s.get("yield") or 1
        e = ex.get(s["id"], 0)
        if e * 100 // y < 15:
            counter = s
            break
    best = max(active, key=lambda x: ex.get(x["id"], 0)) if active else None
    if counter and best:
        cy, ce = counter.get("yield", 0), ex.get(counter["id"], 0)
        by, be = best.get("yield", 0), ex.get(best["id"], 0)
        parts.append(
            f"> **为什么按独有排序而不是按产量**：产量可以被重复内容堆出来。"
            f"`{counter['id']}` 有 {cy:,} 条，独有只有 **{ce:,}** 个（{ce * 100 // max(cy, 1)}%）；"
            f"而 `{best['id']}` 产量 {by:,}，独有 **{be:,}** 个（{be * 100 // max(by, 1)}%）。"
            f"**看独有，别看产量。**"
        )
    parts.append("")
    parts.append(_table(active, ex, pr))
    parts.append("")

    # ---- Telegram 频道单独成节：数量太多，逐个列会把台账淹掉
    if tg:
        tg_yield = sum(s.get("yield", 0) for s in tg)
        tg_ex = sum(ex.get(s["id"], 0) for s in tg)
        parts.append(f"### 6.2 Telegram 频道（{len(tg)} 个，合并展示）")
        parts.append("")
        parts.append(
            f"这 {len(tg)} 个频道合计 {tg_yield:,} 条，但**独有节点只有 {tg_ex} 个**。"
            f"原因是 `Surfboardv2ray/TGParse` 本身就在聚合这些频道，内容高度重合。"
        )
        parts.append("")
        parts.append(
            "> **结论：TG 频道作为「量」的补充价值接近零，它的价值在「新」。**"
            "网页版只显示最近约 20 条消息，所以这些源拿到的是几小时内新发的节点，"
            "比 GitHub 聚合器（通常日更）快。适合高频轮询做「新鲜度」补充，不适合指望它扩容。"
        )
        parts.append("")
        parts.append("产量最高的 15 个：")
        parts.append("")
        rows = ["| 频道 | 产量 | 独有 |", "|---|---:|---:|"]
        for s in tg[:15]:
            rows.append(f"| `{s['id']}` | {s.get('yield', 0)} | {ex.get(s['id'], 0)} |")
        parts.append("\n".join(rows))
        parts.append("")
        parts.append("完整列表见 `sources/registry.yaml`（`kind: telegram`）。")
        parts.append("")

    if cand:
        parts.append(f"### 6.3 候选池（candidate，{len(cand)} 个）")
        parts.append("")
        parts.append("能出节点但产量低、或还没评估。产量 ≥ 5 且增量比合格就转 active。")
        parts.append("")
        parts.append(_table(cand, ex, pr))
        parts.append("")

    parts.append(f"### 6.4 已归档（archived，{len(archived)} 个）")
    parts.append("")
    parts.append(
        "**归档 ≠ 删除。** 这些源产量很大但真连通过率极低，已经停止采集，"
        "但条目保留在 `sources/registry.yaml` 里——`discover.py` 的已知集合依然会跳过它们，"
        "以后不会再重复收录。如果哪天想重新启用，把 `status` 改回 `active` 就行。"
    )
    parts.append("")
    if archived:
        rows = ["| 源 | 产量 | 通过率 | 归档原因 |", "|---|---:|---:|---|"]
        for s in archived:
            p = pr.get(s["id"])
            rate = f"{p[0] * 100 / p[1]:.1f}%" if p and p[1] else "-"
            reason = (s.get("archive_reason") or "").replace("|", "\\|")
            rows.append(f"| `{s['id']}` | {s.get('yield', 0):,} | {rate} | {reason} |")
        parts.append("\n".join(rows))
    parts.append("")

    parts.append(f"### 6.5 已失效（dead，{len(dead)} 个）")
    parts.append("")
    parts.append("**加新源之前先看这张表**，别把已经死掉的源反复加回来。")
    parts.append("")
    dead_rows = ["| 源 | 地址 | 失败原因 | 最后探测 |", "|---|---|---|---|"]
    for s in dead:
        url = s["url"]
        if len(url) > 70:
            url = url[:67] + "..."
        reason = (s.get("format") or "").replace("|", "\\|") or "-"
        dead_rows.append(f"| `{s['id']}` | `{url}` | {reason} | {s.get('last_verified', '-')} |")
    parts.append("\n".join(dead_rows))
    parts.append("")

    parts.append("---")
    parts.append("")
    parts.append("*本文档由 `tools/sources_tool.py` 生成。方法论部分来自 `sources/_method.md`，"
                 "台账部分来自 `sources/registry.yaml`。*")

    with open(DOC, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="数据源台账工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="探测并回填台账")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--only", choices=["active", "candidate", "dead", "archived"])
    p.add_argument("--id", nargs="*", help="只同步指定 id")
    p.add_argument("--mode", default="auto", choices=["auto", "direct", "proxy"],
                   help="采集通道：direct=直连 / proxy=走系统代理 / auto=按 config")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("archive", help="归档高产低通过率的源（不删除，仅停采）")
    p.add_argument("--min-yield", type=int, default=500, help="产量下限")
    p.add_argument("--max-pass", type=float, default=5.0, help="通过率上限（百分比）")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("doc", help="只重生成文档")
    p.set_defaults(func=cmd_doc)

    p = sub.add_parser("add", help="加一个候选源")
    p.add_argument("name")
    p.add_argument("url")
    p.add_argument("--kind", default="github", choices=["github", "telegram", "site", "aggregator"])
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_add)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
