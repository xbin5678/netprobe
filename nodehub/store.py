"""节点库：SQLite 存储、去重、生命周期管理。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    fp          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    server      TEXT NOT NULL,
    port        INTEGER NOT NULL,
    name        TEXT,
    uri         TEXT,
    clash       TEXT NOT NULL,
    first_seen  TEXT,
    last_seen   TEXT,
    seen_count  INTEGER DEFAULT 1,
    alive       INTEGER DEFAULT 0,
    alive_since TEXT,
    latency     INTEGER,
    last_check  TEXT,
    -- 快速检测（只连 TCP）的结果，跟 alive 完全解耦：
    -- TCP 通只说明端口开着，不代表代理能用，所以绝不拿它当「可用」。
    tcp_ok      INTEGER DEFAULT 0,
    tcp_ms      INTEGER,
    tcp_check   TEXT,
    -- 最终筛查：真实下载测出来的吞吐（字节/秒）。NULL 表示没测过。
    -- 这是三档筛查里最严的一档：TCP 通、延迟低都可能只是端口开着，
    -- 真能把字节搬回来才算能用，所以订阅页可以只订阅这一档。
    speed       REAL,
    ttfb        INTEGER,
    speed_at    TEXT,
    fail_count  INTEGER DEFAULT 0,
    ok_count    INTEGER DEFAULT 0,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_alive ON nodes(alive, latency);
CREATE INDEX IF NOT EXISTS idx_server ON nodes(server, port);
-- idx_tcp / idx_speed 放在迁移代码里建：老库这时还没有这些列

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE,
    url         TEXT,
    last_fetch  TEXT,
    last_status TEXT,
    last_count  INTEGER DEFAULT 0,
    last_added  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS node_sources (
    fp        TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    PRIMARY KEY (fp, source_id)
);

CREATE TABLE IF NOT EXISTS checks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    fp      TEXT,
    ts      TEXT,
    ok      INTEGER,
    latency INTEGER,
    stage   TEXT,
    err     TEXT
);
"""


def fingerprint(p: dict) -> str:
    """稳定指纹：协议 + 地址 + 端口 + 凭据，用于跨源去重。"""
    cred = p.get("uuid") or p.get("password") or ""
    raw = "|".join([
        str(p.get("type", "")),
        str(p.get("server", "")).lower(),
        str(p.get("port", "")),
        str(cred),
    ])
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = path
        with self._conn() as c:
            # WAL：流水线在写的时候网页还能读，不会动不动 database is locked
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("PRAGMA busy_timeout=10000")
            except Exception:
                pass
            c.executescript(SCHEMA)
            # 老库迁移：补 last_added 列
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sources)")}
            if "last_added" not in cols:
                c.execute("ALTER TABLE sources ADD COLUMN last_added INTEGER DEFAULT 0")

            # 老库迁移：补 alive_since（连续存活起始时间）
            # 历史数据没有这个信息，退一步用 first_seen 兜底，之后由 mark/demote 维护。
            ncols = {r["name"] for r in c.execute("PRAGMA table_info(nodes)")}
            if "alive_since" not in ncols:
                c.execute("ALTER TABLE nodes ADD COLUMN alive_since TEXT")
                c.execute("UPDATE nodes SET alive_since=COALESCE(first_seen, last_check) WHERE alive=1")

            # 老库迁移：补快速检测（TCP）相关列
            for col, decl in (("tcp_ok", "INTEGER DEFAULT 0"),
                              ("tcp_ms", "INTEGER"),
                              ("tcp_check", "TEXT")):
                if col not in ncols:
                    c.execute(f"ALTER TABLE nodes ADD COLUMN {col} {decl}")
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_tcp ON nodes(tcp_ok, tcp_ms)")
            except Exception:
                pass

            # 老库迁移：补最终筛查（下载测速）相关列
            for col, decl in (("speed", "REAL"), ("ttfb", "INTEGER"),
                              ("speed_at", "TEXT")):
                if col not in ncols:
                    c.execute(f"ALTER TABLE nodes ADD COLUMN {col} {decl}")
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_speed ON nodes(speed)")
            except Exception:
                pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------- sources

    def upsert_source(self, name: str, url: str, status: str, count: int, added: int = 0) -> int:
        with self._conn() as c:
            c.execute(
                """INSERT INTO sources(name,url,last_fetch,last_status,last_count,last_added)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     url=excluded.url, last_fetch=excluded.last_fetch,
                     last_status=excluded.last_status, last_count=excluded.last_count,
                     last_added=excluded.last_added""",
                (name, url, now(), status, count, added),
            )
            row = c.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
            return int(row["id"])

    # ---------------- nodes

    def upsert_nodes(self, proxies: list[dict], source_id: int) -> dict:
        ts = now()
        added = updated = dropped = 0
        with self._conn() as c:
            for p in proxies:
                # 入库前先消毒：字段不规范（cipher 拼错、REALITY 参数坏、server
                # 带方括号……）的节点留着也没用，还会拖垮整批检测和整份订阅。
                p = normalize.sanitize(p)
                if not p:
                    dropped += 1
                    continue
                fp = fingerprint(p)
                p = dict(p)
                p.pop("name", None)  # 原始名可能含特殊字符，展示名单独存
                clash_json = json.dumps(p, ensure_ascii=False)
                row = c.execute("SELECT fp, seen_count FROM nodes WHERE fp=?", (fp,)).fetchone()
                if row:
                    c.execute(
                        "UPDATE nodes SET last_seen=?, seen_count=seen_count+1, clash=? WHERE fp=?",
                        (ts, clash_json, fp),
                    )
                    updated += 1
                else:
                    c.execute(
                        """INSERT INTO nodes(fp,type,server,port,name,clash,first_seen,last_seen,seen_count)
                           VALUES(?,?,?,?,?,?,?,?,1)""",
                        (fp, p.get("type"), p.get("server"), p.get("port"),
                         p.get("name") or f"{p.get('server')}:{p.get('port')}",
                         clash_json, ts, ts),
                    )
                    added += 1
                c.execute(
                    "INSERT OR IGNORE INTO node_sources(fp,source_id) VALUES(?,?)",
                    (fp, source_id),
                )
        return {"added": added, "updated": updated, "dropped": dropped}

    def get_nodes(self, limit: int = 0, only_unchecked: bool = False,
                  retest_days: int = 0, fps: list[str] | None = None,
                  types: list[str] | None = None, scope: str = "") -> list[dict]:
        """取待检测节点。

        scope 为空时沿用老行为（only_unchecked 决定取新节点还是全部）；
        手动检测会显式给 scope=fps/alive/all/dead，再叠加 fps / types 过滤。
        """
        where: list[str] = []
        args: list = []
        if scope == "alive":
            where.append("alive=1")
        elif scope == "dead":
            where.append("alive=0")
        elif scope == "all":
            pass
        elif only_unchecked:
            if retest_days > 0:
                # 增量模式：没测过的 + 太久没测过的（判死的节点可能复活）
                cutoff = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - retest_days * 86400))
                where.append("(last_check IS NULL OR last_check < ?)")
                args.append(cutoff)
            else:
                where.append("last_check IS NULL")
        if fps:
            where.append("fp IN (%s)" % ",".join("?" * len(fps)))
            args.extend(fps)
        if types:
            where.append("type IN (%s)" % ",".join("?" * len(types)))
            args.extend(types)
        q = "SELECT * FROM nodes"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY last_check IS NULL DESC, alive DESC, latency ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def mark(self, fp: str, ok: bool, latency: int | None, stage: str, err: str = "") -> None:
        ts = now()
        with self._conn() as c:
            if ok:
                # alive_since 只在「死→活」那一刻重置：连续存活时长就是这么来的。
                # SET 右边读的都是旧值，所以 alive=0 判断的是这次之前的状态。
                c.execute(
                    """UPDATE nodes SET alive=1, latency=?, last_check=?, ok_count=ok_count+1,
                       fail_count=0, last_error='',
                       alive_since=CASE WHEN alive=0 OR alive_since IS NULL THEN ? ELSE alive_since END
                       WHERE fp=?""",
                    (latency, ts, ts, fp),
                )
            else:
                c.execute(
                    """UPDATE nodes SET fail_count=fail_count+1, last_check=?, last_error=? WHERE fp=?""",
                    (ts, err[:200], fp),
                )
            c.execute(
                "INSERT INTO checks(fp,ts,ok,latency,stage,err) VALUES(?,?,?,?,?,?)",
                (fp, ts, 1 if ok else 0, latency, stage, err[:200]),
            )

    def mark_tcp(self, fp: str, ok: bool, ms: int | None) -> None:
        """快速检测的结果：只记录「端口通不通」，绝不碰 alive。

        TCP 通不代表代理能用（端口开着但对端没跑代理协议，这种情况很常见），
        所以这里只写 tcp_ok/tcp_ms，是否可用仍然由真连测试说了算。
        """
        with self._conn() as c:
            c.execute(
                "UPDATE nodes SET tcp_ok=?, tcp_ms=?, tcp_check=? WHERE fp=?",
                (1 if ok else 0, ms if ok else None, now(), fp),
            )

    def mark_speed(self, fp: str, speed: float | None, ttfb: int | None = None) -> None:
        """最终筛查结果：真实下载吞吐（字节/秒）。speed=None 表示没跑通。

        和 mark_tcp 一样，这里不动 alive——测速失败不代表节点不可用
        （可能只是这一轮超时），但它确实不该出现在「有速度」那一档里。
        """
        with self._conn() as c:
            c.execute(
                "UPDATE nodes SET speed=?, ttfb=?, speed_at=? WHERE fp=?",
                (speed, ttfb if speed else None, now(), fp),
            )

    def speed_candidates(self, limit: int = 40) -> list[dict]:
        """挑出该测速的节点：当前可用、延迟已知，按延迟从快到慢取前 N 个。

        全库几万个节点不可能都测——10MB × 几万 是几个 TB 的流量。
        只测「已经证明能用」里最快的那批，性价比最高。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM nodes
                    WHERE alive=1 AND latency IS NOT NULL
                    ORDER BY latency ASC LIMIT ?""", (max(1, int(limit)),)
            ).fetchall()
        return [dict(r) for r in rows]

    def demote_stale(self, fail_threshold: int) -> int:
        """连续失败达到阈值的节点标记为死节点。"""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE nodes SET alive=0, alive_since=NULL
                   WHERE alive=1 AND fail_count>=?""", (fail_threshold,)
            )
            return cur.rowcount

    def prune(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        with self._conn() as c:
            fps = [r["fp"] for r in c.execute("SELECT fp FROM nodes WHERE last_seen < ?", (cutoff,))]
            for fp in fps:
                c.execute("DELETE FROM nodes WHERE fp=?", (fp,))
                c.execute("DELETE FROM node_sources WHERE fp=?", (fp,))
            return len(fps)

    # 三档筛查，对应订阅页上能选的三档
    TIERS = ("alive", "speed", "tcp")

    def alive_nodes(self, max_latency: int = 0, min_age_days: float = 0,
                    min_ok: int = 0, tier: str = "alive") -> list[dict]:
        """当前可用的节点。

        min_age_days: 只要「连续存活」超过这么多天的（按 alive_since 算）
        min_ok:       至少要成功验证过多少次
        tier:         哪一档筛查的结果
                      alive（默认）深度筛查通过 / speed 最终筛查有下载速度 /
                      tcp 快速筛查端口通（**不代表代理能用**）
        """
        tier = (tier or "alive").lower()
        if tier not in self.TIERS:
            tier = "alive"
        if tier == "tcp":
            q = "SELECT * FROM nodes WHERE tcp_ok=1"
            order = " ORDER BY tcp_ms ASC, latency ASC"
        elif tier == "speed":
            q = "SELECT * FROM nodes WHERE alive=1 AND speed>0"
            order = " ORDER BY speed DESC"
        else:
            q = "SELECT * FROM nodes WHERE alive=1"
            order = " ORDER BY latency ASC"
        args: list = []
        if max_latency:
            q += " AND latency IS NOT NULL AND latency<=?"
            args.append(max_latency)
        if min_age_days > 0:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - min_age_days * 86400))
            q += " AND COALESCE(alive_since, first_seen) <= ?"
            args.append(cutoff)
        if min_ok > 0:
            q += " AND ok_count >= ?"
            args.append(min_ok)
        q += order
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def tier_counts(self, min_age_days: float = 0.0) -> dict:
        """三档各有多少个，给订阅页的档位按钮当角标。"""
        where, args = "", []
        if min_age_days > 0:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - min_age_days * 86400))
            where = " AND COALESCE(alive_since, first_seen) <= ?"
            args.append(cutoff)
        with self._conn() as c:
            row = c.execute(
                f"""SELECT SUM(CASE WHEN alive=1 THEN 1 ELSE 0 END) AS alive,
                           SUM(CASE WHEN alive=1 AND speed>0 THEN 1 ELSE 0 END) AS speed,
                           SUM(CASE WHEN tcp_ok=1 THEN 1 ELSE 0 END) AS tcp
                      FROM nodes WHERE 1=1{where}""", args
            ).fetchone()
        return {k: int((row[k] if row else 0) or 0) for k in ("alive", "speed", "tcp")}

    def long_counts(self, days: tuple[int, ...] = (1, 3, 5, 7)) -> dict:
        """各档「连续存活超过 N 天」的节点数，给界面上的角标用。"""
        out: dict = {}
        for d in days:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - d * 86400))
            with self._conn() as c:
                row = c.execute(
                    """SELECT COUNT(*) n FROM nodes
                       WHERE alive=1 AND ok_count>=2
                         AND COALESCE(alive_since, first_seen) <= ?""", (cutoff,)
                ).fetchone()
            out[str(d)] = int(row["n"])

        # 库里最老的可用节点活了多久——列表空的时候用来说「还得等几天」
        with self._conn() as c:
            row = c.execute(
                "SELECT MIN(COALESCE(alive_since, first_seen)) m FROM nodes WHERE alive=1"
            ).fetchone()
        oldest = 0.0
        if row and row["m"]:
            try:
                t0 = time.mktime(time.strptime(row["m"], "%Y-%m-%d %H:%M:%S"))
                oldest = max(0.0, (time.time() - t0) / 3600)
            except Exception:
                oldest = 0.0
        out["oldest_hours"] = round(oldest, 1)
        return out

    def fps_only_from(self, source_names: set[str]) -> set[str]:
        """找出「只被这些源收录」的节点指纹。

        归档一批源之后，用这个把只存在于归档源的节点排除掉，
        免得 check 还在测一堆永远不会再被采集的节点。
        """
        if not source_names:
            return set()
        marks = ",".join("?" * len(source_names))
        q = f"""SELECT fp FROM node_sources
                GROUP BY fp
                HAVING SUM(CASE WHEN source_id IN
                    (SELECT id FROM sources WHERE name IN ({marks})) THEN 1 ELSE 0 END)
                    = COUNT(*)"""
        with self._conn() as c:
            return {r["fp"] for r in c.execute(q, tuple(source_names))}

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM nodes").fetchone()["n"]
            alive = c.execute("SELECT COUNT(*) n FROM nodes WHERE alive=1").fetchone()["n"]
            by_type = {
                r["type"]: r["n"]
                for r in c.execute(
                    "SELECT type, COUNT(*) n FROM nodes GROUP BY type ORDER BY n DESC"
                ).fetchall()
            }
            alive_by_type = {
                r["type"]: r["n"]
                for r in c.execute(
                    "SELECT type, COUNT(*) n FROM nodes WHERE alive=1 GROUP BY type ORDER BY n DESC"
                ).fetchall()
            }
            src = [
                dict(r)
                for r in c.execute(
                    "SELECT name,last_fetch,last_status,last_count FROM sources ORDER BY last_count DESC"
                ).fetchall()
            ]
            lat = c.execute(
                "SELECT MIN(latency) mn, AVG(latency) av FROM nodes WHERE alive=1 AND latency IS NOT NULL"
            ).fetchone()
            # 快速检测的分布：没测过 / 通 / 不通
            tcp_ok = c.execute("SELECT COUNT(*) n FROM nodes WHERE tcp_ok=1").fetchone()["n"]
            tcp_checked = c.execute(
                "SELECT COUNT(*) n FROM nodes WHERE tcp_check IS NOT NULL").fetchone()["n"]
            # 最终筛查：有多少个真测出了下载速度
            speed_ok = c.execute(
                "SELECT COUNT(*) n FROM nodes WHERE alive=1 AND speed>0").fetchone()["n"]
            spd = c.execute(
                "SELECT MAX(speed) mx FROM nodes WHERE alive=1 AND speed>0").fetchone()
        return {
            "total": total, "alive": alive,
            "by_type": by_type, "alive_by_type": alive_by_type,
            "sources": src,
            "min_latency": lat["mn"], "avg_latency": lat["av"],
            "tcp_ok": tcp_ok, "tcp_checked": tcp_checked,
            "tcp_untested": total - tcp_checked,
            "speed_ok": speed_ok, "max_speed": spd["mx"] if spd else None,
        }
