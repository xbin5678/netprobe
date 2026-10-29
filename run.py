#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据采集流水线
-------------
  python run.py collect    步骤1 搜集：抓取所有订阅源
  python run.py manage     步骤2 管理：去重入库、清理过期
  python run.py check      步骤3 检测：TCP 预筛 + 真连测速，只留可用的
  python run.py all        一条龙（collect -> manage -> check -> 导出）
  python run.py tick       定时任务入口：到点才跑一次 all，没到点立刻退出
  python run.py stats      查看节点库现状

排期不在 systemd 的 unit 里，在 data/schedule.json（网页「定时任务」页可改）。
systemd 只负责每 10 分钟叫一次 tick。

常用参数:
  --limit 500      本次最多检测多少个节点
  --no-real        只做 TCP 预筛，不做真连测试（很快，但结果不准）
  --top 100        只导出最快的 100 个
  --config x.yaml  指定配置文件
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from nodehub import exporter, fetcher, schedule as sched, tester  # noqa: E402
from nodehub.config import archived_sources, load_config as _load_config  # noqa: E402
from nodehub.lock import Guard, holder  # noqa: E402
from nodehub.store import Store, now  # noqa: E402


class _Tee:
    """同时写控制台和日志文件。

    定时任务（systemd）不是从网页启动的，网页进程手里没有它的输出。
    把每次运行的输出落到 data/last_run.log，网页就能在没人盯着的时候
    也翻出「昨晚跑得怎么样」。
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _init_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _tee_to_file(path: str) -> None:
    """把 stdout 同时写到文件（每次运行覆盖，只留最近一次）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "w", encoding="utf-8", buffering=1, errors="replace")
        sys.stdout = _Tee(sys.stdout, f)
    except Exception:
        pass  # 写不了日志不影响主流程


def load_config(path: str) -> dict:
    # 订阅源以台账为单一事实源：读 registry 里 status=active 的条目
    return _load_config(path, ROOT)


def hr(title: str):
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


# ---------------------------------------------------------------- 步骤 1

def cmd_collect(cfg: dict, store: Store) -> None:
    hr("步骤 1 / 搜集 —— 抓取所有订阅源")
    sources = cfg.get("sources") or []
    fetch_cfg = cfg.get("fetch") or {}
    t0 = time.time()

    results = fetcher.fetch_all(sources, fetch_cfg)

    total_new = total_upd = 0
    ok_src = 0
    skipped = 0
    for r in results:
        if r.get("skipped"):
            skipped += 1
            continue
        status = "ok" if r["nodes"] else f"fail: {r['error']}"
        sid = store.upsert_source(r["source"], r["url"], status, len(r["nodes"]))
        if r["nodes"]:
            st = store.upsert_nodes(r["nodes"], sid)
            total_new += st["added"]
            total_upd += st["updated"]
            ok_src += 1
            print(f"  ✓ {r['source']:<28} {len(r['nodes']):>5} 个  "
                  f"[{r['fmt']}] 新增 {st['added']}")
        else:
            print(f"  ✗ {r['source']:<28}     -  {r['error']}")

    print(f"\n  完成: {ok_src}/{len(results)-skipped} 个源有效"
          + (f"（另 {skipped} 个 TG 频道因代理未开启跳过）" if skipped else "")
          + f", 新增 {total_new} 个节点, 更新 {total_upd} 个, 耗时 {time.time()-t0:.1f}s")


# ---------------------------------------------------------------- 步骤 2

def cmd_manage(cfg: dict, store: Store) -> None:
    hr("步骤 2 / 管理 —— 去重合并、清理过期节点")
    prune_days = int((cfg.get("store") or {}).get("prune_days", 7))
    removed = store.prune(prune_days)
    st = store.stats()
    print(f"  清理 {prune_days} 天未出现的节点: {removed} 个")
    print(f"  节点库总量: {st['total']}")
    print("  按协议:")
    for k, v in st["by_type"].items():
        print(f"    {k:<10} {v}")


# ---------------------------------------------------------------- 步骤 3

def cmd_check(cfg: dict, store: Store, limit: int = 0, no_real: bool = False,
              unchecked_only: bool = False, fast: bool = False,
              fps: list[str] | None = None, types: list[str] | None = None,
              scope: str = "") -> dict | None:
    """检测节点。

    两档：
    - fast=True  只连 TCP，判断端口通不通，几秒钟能扫几千个，**不改可用状态**
    - fast=False 先 TCP 预筛，再用 mihomo 真发 HTTP 请求，通过才算可用

    fps / types / scope 用来做「手动检测」：只测勾选的、只测某个协议、
    或者只测当前可用/已判死的。
    """
    hr("检测 —— " + ("快速（只测 TCP 通不通）" if fast else "深度（真实 HTTP 请求）"))
    ck = cfg.get("check") or {}
    tcp_cfg = ck.get("tcp") or {}
    real_cfg = ck.get("real") or {}

    max_nodes = limit or int(real_cfg.get("max_nodes", 0) or 0)
    retest_days = int(real_cfg.get("retest_days", 3) or 0)
    nodes = store.get_nodes(limit=max_nodes, only_unchecked=unchecked_only,
                            retest_days=retest_days if unchecked_only else 0,
                            fps=fps, types=types, scope=scope)

    if fps:
        print(f"  指定了 {len(fps)} 个节点，实际取到 {len(nodes)} 个")
    if types:
        print(f"  只看协议: {', '.join(types)}")

    # 归档源的节点不再检测——它们已经不会被采集，测了也是白测
    archived = _archived_sources(cfg)
    if archived and nodes:
        dormant = store.fps_only_from(archived)
        before = len(nodes)
        nodes = [n for n in nodes if n["fp"] not in dormant]
        if before != len(nodes):
            print(f"  跳过 {before - len(nodes)} 个只存在于归档源的节点")

    if not nodes:
        print("  没有待检测的节点" + ("（都测过了，去掉 --unchecked 可重测）" if unchecked_only else "，先跑 collect"))
        return None
    print(f"  待检测: {len(nodes)} 个节点")

    # ---- 快速检测：只测 TCP，结果写进 tcp_* 字段，不动 alive
    if fast:
        t0 = time.time()
        ok_map = tester.tcp_precheck(
            nodes,
            timeout=float(tcp_cfg.get("timeout", 3.0)),
            threads=int(tcp_cfg.get("threads", 200)),
        )
        for n in nodes:
            fp = n["fp"]
            store.mark_tcp(fp, fp in ok_map, ok_map.get(fp))
        alive_tcp = len(ok_map)
        print(f"  TCP 通 {alive_tcp} / 不通 {len(nodes) - alive_tcp} "
              f"({time.time()-t0:.1f}s)")
        print("  注意：TCP 通只代表端口开着，代理能不能用要跑「深度检测」才知道。")
        print(f"  当前可用节点（上次深度检测的结果）: {store.stats()['alive']}")
        return None

    # ---- 深度检测：TCP 预筛
    passed = nodes
    if tcp_cfg.get("enabled", True):
        t0 = time.time()
        ok_map = tester.tcp_precheck(
            nodes,
            timeout=float(tcp_cfg.get("timeout", 3.0)),
            threads=int(tcp_cfg.get("threads", 200)),
        )
        passed = [n for n in nodes if n["fp"] in ok_map]
        dead = [n for n in nodes if n["fp"] not in ok_map]
        for n in dead:
            store.mark_tcp(n["fp"], False, None)
            store.mark(n["fp"], False, None, "tcp", "tcp_unreachable")
        for n in passed:
            store.mark_tcp(n["fp"], True, ok_map[n["fp"]])
        print(f"  [1/2] TCP 预筛: {len(passed)} 通过 / {len(dead)} 淘汰 "
              f"({time.time()-t0:.1f}s)")

    if not passed:
        print("  没有节点通过 TCP 预筛")
        return None

    # ---- 深度检测：真连
    if no_real or not real_cfg.get("enabled", True):
        # 只测 TCP 的结果不能当可用——端口开着但对端没跑代理协议太常见了
        print("  [2/2] 已跳过真连测试，本次不改变可用状态")
    else:
        exe = tester.ensure_mihomo(ck, ROOT)
        if not exe:
            print("  ! 找不到 mihomo 内核，本次不改变可用状态")
        else:
            t0 = time.time()
            res = tester.real_test(passed, real_cfg, ROOT, exe)
            ok = 0
            for n in passed:
                good, lat, err = res.get(n["fp"], (False, None, "no_result"))
                if good:
                    ok += 1
                    store.mark(n["fp"], True, lat, "real")
                else:
                    store.mark(n["fp"], False, None, "real", err)
            print(f"  [2/2] 真连测试: {ok} 可用 / {len(passed)-ok} 淘汰 "
                  f"({time.time()-t0:.1f}s)")

    demoted = store.demote_stale(int(real_cfg.get("fail_threshold", 3)))
    if demoted:
        print(f"  连续失败达阈值降级: {demoted} 个")

    st = store.stats()
    print(f"\n  当前可用节点: {st['alive']} / {st['total']}")
    for k, v in st["alive_by_type"].items():
        print(f"    {k:<10} {v}")

    out = exporter.export_all(store, cfg.get("export") or {}, ROOT)
    print("\n  导出:")
    for k in ("clash", "sub", "plain", "report"):
        print(f"    {os.path.relpath(out[k], ROOT)}")
    return out


def cmd_speed(cfg: dict, store: Store, limit: int = 0,
              fps: list[str] | None = None) -> None:
    """最终筛查 —— 给已经证明能用的节点测真实下载速度。

    三档筛查的最后一档：
      快速（TCP 通）→ 深度（真实 HTTP）→ 最终（能搬字节）
    前两档都只说明「连得上」，只有这一档能说明「能跑流量」。结果写进
    nodes.speed，订阅页就能只订阅这一档。
    """
    hr("最终筛查 —— 测真实下载速度")
    sp = (cfg.get("check") or {}).get("speed") or {}
    if not sp.get("enabled", True):
        print("  已关闭（config 里 check.speed.enabled: false），跳过")
        return
    size_mb = int(sp.get("size", 10) or 10)
    if size_mb not in (10, 20, 50):
        size_mb = 10
    max_sec = float(sp.get("max_sec", 12) or 12)
    count = limit or int(sp.get("count", 30) or 30)

    if fps:
        want = set(fps)
        nodes = [n for n in store.alive_nodes(0) if n["fp"] in want]
        print(f"  指定 {len(fps)} 个节点，其中 {len(nodes)} 个当前可用")
    else:
        nodes = store.speed_candidates(count)
    if not nodes:
        print("  没有可测速的节点（要先有深度检测通过的节点）")
        return
    print(f"  候选 {len(nodes)} 个（当前可用的按延迟从快到慢取前 {count} 个）")
    print(f"  测速文件 {size_mb}MB / 单个最多 {max_sec:g}s，"
          f"最多约 {len(nodes) * size_mb}MB 流量")

    exe = tester.ensure_mihomo(cfg.get("check") or {}, ROOT)
    if not exe:
        print("  ! 找不到 mihomo 内核，跳过最终筛查")
        return

    from nodehub import probe as probemod

    t0 = time.time()

    def one(i, n, r):
        speed, _ttfb, err = r
        if speed:
            print(f"    {i}/{n}  {speed / 1048576:6.2f} MB/s")
        else:
            print(f"    {i}/{n}  失败 {err}")

    res = probemod.speed_rows(nodes, {}, ROOT, exe, size_mb=size_mb,
                              max_sec=max_sec,
                              chunk=int(sp.get("chunk", 10) or 10), on_one=one)
    good = 0
    for n in nodes:
        speed, ttfb, _err = res.get(n["fp"], (None, None, "no_result"))
        store.mark_speed(n["fp"], speed, ttfb)
        if speed:
            good += 1
    print(f"  测出速度 {good} / {len(nodes)} ({time.time() - t0:.1f}s)")
    print(f"  当前「有下载速度」的节点: {store.stats()['speed_ok']}")
    if good:
        print("  订阅页可以切到「有速度」那一档，只订阅这一批。")


# ---------------------------------------------------------------- 汇总

def cmd_stats(cfg: dict, store: Store) -> None:
    hr("节点库现状")
    st = store.stats()
    print(f"  总量 {st['total']} / 可用 {st['alive']}")
    if st["min_latency"]:
        print(f"  延迟: 最低 {st['min_latency']}ms, 平均 {int(st['avg_latency'] or 0)}ms")
    print("\n  采集源:")
    for s in st["sources"]:
        print(f"    {s['name']:<28} {s['last_count']:>5}  {s['last_status']:<22} {s['last_fetch'] or '-'}")


def cmd_summary(cfg: dict, store: Store, elapsed: float = 0) -> None:
    """跑完全流程后的收尾汇总。"""
    hr("完成")
    st = store.stats()
    total = st.get("total", 0)
    alive = st.get("alive", 0)
    print(f"  节点库总量: {total:,}")
    print(f"  本次可用:   {alive}")

    if alive:
        by = st.get("alive_by_type") or {}
        if by:
            print("  按协议: " + "、".join(f"{k} {v}" for k, v in
                                        sorted(by.items(), key=lambda x: -x[1])))
        mn, av = st.get("min_latency"), st.get("avg_latency")
        if mn and av:
            print(f"  延迟: 最低 {mn}ms / 平均 {int(av)}ms")
        spd = st.get("speed_ok") or 0
        mx = st.get("max_speed")
        if spd:
            print(f"  有下载速度: {spd} 个" +
                  (f"（最快 {mx / 1048576:.1f} MB/s）" if mx else "") +
                  " —— 订阅页切「有速度」那一档就是这批")
        elif (cfg.get("check") or {}).get("speed", {}).get("enabled", True):
            print("  有下载速度: 0 个（这一轮没测出速度，订阅页那一档会是空的）")

    outdir = os.path.join(ROOT, (cfg.get("export") or {}).get("dir", "output"))
    print()
    print("  可直接导入的文件：")
    for f, desc in (
        ("alive_clash.yaml", "Clash Verge / Mihomo Party / FlClash"),
        ("alive_sub.txt", "v2rayN / Karing / Shadowrocket（base64 订阅）"),
        ("alive.txt", "原始链接，一行一个"),
        ("report.md", "详细报告"),
    ):
        p = os.path.join(outdir, f)
        if os.path.exists(p):
            print(f"    {f:<20} {desc}")

    if elapsed:
        print(f"\n  总耗时 {elapsed/60:.1f} 分钟")
    print()
    print("  ⚠ 这些是公共公开节点，随时可能失效，也可能有人在监听流量。")
    print("    不要用来登录账号、收发敏感信息或做支付。")
    print()


def _archived_sources(cfg: dict) -> set[str]:
    """从台账里取出已归档的源名。"""
    return archived_sources(cfg, ROOT)


def cmd_verify(cfg: dict, store: Store, top: int = 0,
               fps: list[str] | None = None) -> None:
    """对当前标记为可用的节点做二次复核，剔除侥幸通过的。"""
    hr("复核 —— 对已判定可用的节点再测一遍")
    real_cfg = cfg.get("check") or {}
    real_cfg = real_cfg.get("real") or {}

    nodes = store.alive_nodes(0)
    if fps:
        want = set(fps)
        nodes = [n for n in nodes if n["fp"] in want]
        print(f"  指定 {len(fps)} 个节点，其中 {len(nodes)} 个当前可用")
    if top:
        nodes = nodes[:top]
    if not nodes:
        print("  没有可用节点")
        return
    print(f"  复核 {len(nodes)} 个节点")

    exe = tester.ensure_mihomo(cfg.get("check") or {}, ROOT)
    if not exe:
        print("  ! 找不到 mihomo 内核")
        return

    # 分片大小走 config 的 check.real.chunk（低配机调小一点，别把内核撑爆）
    res = tester.real_test(nodes, real_cfg, ROOT, exe)
    kept = 0
    for n in nodes:
        good, lat, err = res.get(n["fp"], (False, None, "no_result"))
        if good:
            kept += 1
            store.mark(n["fp"], True, lat, "verify")
        else:
            store.mark(n["fp"], False, None, "verify", err or "verify_failed")
    store.demote_stale(1)  # 复核失败直接降级，不给容错
    print(f"  复核通过 {kept}/{len(nodes)}")

    out = exporter.export_all(store, cfg.get("export") or {}, ROOT)
    print(f"  已重新导出 {out['count']} 个节点")


def cmd_all(cfg: dict, store: Store, args, t0: float) -> None:
    """一条龙：采集 → 去重入库 → 检测 → 复核 → 测速 → 汇总导出。

    定时任务（run.py tick）和手动「一键更新」走的都是这一段，所以单独抽出来。
    """
    cmd_collect(cfg, store)
    cmd_manage(cfg, store)
    incremental = not args.full
    cmd_check(cfg, store, args.limit, args.no_real, incremental)
    do_verify = bool((cfg.get("check") or {}).get("verify_after_all", True))
    # 复核无条件跑：没有新节点的那天（增量模式的常态）正是最需要复核的，
    # 否则可用列表会一直停在上次的结果上，死掉的节点永远筛不掉。
    if not args.no_real and do_verify and not args.no_verify:
        cmd_verify(cfg, store, args.limit)
    # 最终筛查放在最后：前面的两档把范围缩到几十个，这一步才跑得动。
    # 注意别把 --limit 传进来：--limit 是「这次检测多少个节点」，
    # 一旦串到测速这边，--limit 1000 就会变成 1000×10MB ≈ 10GB 流量。
    # 测速数量只认 config 里的 check.speed.count。
    if not args.no_real and not args.no_speed:
        cmd_speed(cfg, store)
    cmd_summary(cfg, store, time.time() - t0)


def main():
    _init_console()
    ap = argparse.ArgumentParser(description="数据采集流水线")
    ap.add_argument("cmd", choices=["collect", "manage", "check", "all", "stats",
                                    "verify", "speed", "probe", "tick"])
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--limit", type=int, default=0, help="本次最多检测多少节点")
    ap.add_argument("--no-real", action="store_true",
                    help="只做 TCP 预筛，不做真连（不改变可用状态）")
    ap.add_argument("--fast", action="store_true",
                    help="快速检测：只连 TCP，判断端口通不通，不改可用状态")
    ap.add_argument("--slow", action="store_true",
                    help="深度检测：TCP + 真实 HTTP 请求（默认行为，写出来只是更清楚）")
    ap.add_argument("--unchecked", action="store_true", help="只测从没测过的节点（断点续测）")
    ap.add_argument("--full", action="store_true", help="all 时全量重测（默认只测新节点）")
    ap.add_argument("--top", type=int, default=0, help="只导出最快 N 个")
    ap.add_argument("--fp", default="",
                    help="手动检测：只测这些节点（指纹，逗号分隔）")
    ap.add_argument("--type", default="",
                    help="手动检测：只测这些协议（vless,vmess,...，逗号分隔）")
    ap.add_argument("--scope", default="", choices=["", "all", "alive", "dead", "new"],
                    help="手动检测范围：all 全部 / alive 当前可用 / dead 已判死 / new 没测过的")
    ap.add_argument("--no-verify", action="store_true",
                    help="all 时跳过最后的复核（省一半时间，低配机用）")
    ap.add_argument("--no-speed", action="store_true",
                    help="all 时跳过最终筛查（测下载速度，费流量）")

    # ---- 订阅测速（probe 专用）
    ap.add_argument("--run", type=int, default=0, help="probe: 历史记录 ID")
    ap.add_argument("--url", default="", help="probe: 直接给订阅地址（会自动建一条记录）")
    ap.add_argument("--title", default="", help="probe: 记录标题")
    ap.add_argument("--mode", default="auto",
                    choices=["fast", "deep", "speed", "auto"],
                    help="probe: fast 只测TCP / deep 测延迟 / speed 测下载 / auto 全流程")
    ap.add_argument("--size", type=int, default=10,
                    help="probe: 测速文件大小(MB)，10/20/50")
    ap.add_argument("--max-speed", type=int, default=15,
                    help="probe: 最多给多少个节点测下载速度")
    ap.add_argument("--ids", default="", help="probe: 只测这几个节点 ID，逗号分隔")
    args = ap.parse_args()

    fps = [x.strip() for x in args.fp.split(",") if x.strip()]
    types = [x.strip() for x in args.type.split(",") if x.strip()]

    cfg = load_config(args.config)
    if args.top:
        cfg.setdefault("export", {})["top_n"] = args.top

    # 订阅测速走自己的一套：独立的库、独立的锁，和主流水线互不干扰，
    # 所以放在主锁判断之前直接返回。
    if args.cmd == "probe":
        from nodehub import probe as probemod

        if not args.run and not args.url:
            print("  需要 --run（历史记录 ID）或 --url（订阅地址）")
            sys.exit(2)
        pguard = Guard(os.path.join(ROOT, "data", "probe.lock"))
        if not pguard.acquire():
            print("  另一个测速任务正在跑，本次跳过。")
            sys.exit(75)
        _tee_to_file(os.path.join(ROOT, "data", "probe_last.log"))
        pdb = probemod.ProbeDB(os.path.join(ROOT, "data", "probe.db"))
        rid = args.run
        if not rid:
            try:
                text, final_url = probemod.fetch_sub(args.url)
                nodes, fmt, bad = probemod.parse_sub(text)
            except probemod.ProbeError as e:
                print(f"  {e}")
                sys.exit(2)
            if not nodes:
                print("  订阅里没有解析出节点")
                sys.exit(2)
            rid = pdb.create_run(args.title or final_url, final_url, fmt, nodes)
            print(f"  新建记录 #{rid}：{len(nodes)} 个节点（{fmt}，丢弃 {bad}）")
        ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
        sys.exit(probemod.run_probe(pdb, rid, cfg, ROOT, mode=args.mode,
                                    size_mb=args.size, max_speed=args.max_speed,
                                    ids=ids or None))

    # 定时任务：systemd timer 每 10 分钟跑一次 tick，这里判断到点没有。
    # 必须放在抢锁和 _tee_to_file 之前——没到点的时候连锁都不该碰，
    # 更不能把 data/last_run.log（网页用来显示上次运行日志的）清空。
    if args.cmd == "tick":
        is_due, sched_now, _st = sched.due(ROOT)
        if not is_due:
            nxt = sched.next_run(sched_now, _st)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else "未启用"
            print(f"  未到时间（{sched.describe(sched_now)}，下次 {when}），跳过")
            sys.exit(0)

    # 定时任务和网页按钮共用一把锁：撞上了就老实退出，别互相抢带宽。
    # stats 只读，不占锁。
    guard = None
    if args.cmd != "stats":
        guard = Guard(os.path.join(ROOT, "data", "run.lock"))
        if not guard.acquire():
            pid = holder(os.path.join(ROOT, "data", "run.lock"))
            who = f"PID {pid}" if pid and pid > 0 else "另一个进程"
            print(f"  已有任务在跑（{who}），本次跳过。要强制跑请先停掉它。")
            sys.exit(75)

    store = Store(os.path.join(ROOT, (cfg.get("store") or {}).get("db", "data/nodes.db")))

    if args.cmd != "stats":
        _tee_to_file(os.path.join(ROOT, "data", "last_run.log"))

    t0 = time.time()
    if args.cmd == "collect":
        cmd_collect(cfg, store)
    elif args.cmd == "manage":
        cmd_manage(cfg, store)
    elif args.cmd == "check":
        cmd_check(cfg, store, args.limit, args.no_real, args.unchecked,
                  fast=args.fast and not args.slow, fps=fps, types=types,
                  scope=args.scope)
    elif args.cmd == "stats":
        cmd_stats(cfg, store)
    elif args.cmd == "verify":
        cmd_verify(cfg, store, args.limit, fps)
    elif args.cmd == "speed":
        cmd_speed(cfg, store, args.limit, fps or None)
    elif args.cmd in ("all", "tick"):
        # tick 只是「到点了才走 all」，两者跑的是同一段流程，别写两份
        sched_state = args.cmd == "tick"
        if sched_state:
            sched.mark_start(ROOT)
        ok = False
        try:
            cmd_all(cfg, store, args, t0)
            ok = True
        finally:
            # 一定要记：不记的话下次 tick 会以为「从没跑过」，立刻又跑一遍
            if sched_state:
                sched.mark_end(ROOT, ok=ok, rc=0 if ok else 1)
        return

    print(f"\n总耗时 {time.time()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
