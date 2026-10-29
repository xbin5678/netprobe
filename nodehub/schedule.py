"""后台任务的排期：什么时候自动跑一次完整流水线。

为什么不把排期写进 systemd 的 unit
----------------------------------
改 timer 得动 `/etc/systemd/system`，而 web.py 跑在 `ProtectSystem=full` 的沙箱里
（见 deploy/nodehub-web.service），`/etc` 是只读的，网页上根本改不了；就算放开
沙箱，也要 root + daemon-reload，等于给网页开一个提权口子。

所以 systemd 那边只装一个「每 10 分钟问一次」的 timer，真正的排期放在
`data/schedule.json`，由 `run.py tick` 每次判断到点没有：

    systemd timer（每 10 分钟）→ run.py tick → 读 schedule.json
                                             → 没到点：立刻退出（0.2 秒）
                                             → 到点：跑完整的 all 流程

好处：
- 网页改完立刻生效，不用 root、不用重启服务
- 机器关机期间错过的任务，开机后第一次 tick 就补上（比 systemd 的
  Persistent=true 更可靠，因为间隔模式下它也能算对）
- 排期文件坏掉时退回默认值，不会让定时任务彻底停摆

state 与 schedule 分两个文件：schedule.json 是人改的，schedule_state.json 是
程序写的（上次跑了没、成功没），混在一起容易被人手改坏。
"""
from __future__ import annotations

import json
import os
import time

# 默认：每天凌晨 04:17 跑一次（避开整点，整点是全网最挤的时候）
DEFAULTS = {
    "enabled": True,
    "mode": "daily",          # daily=每天固定时刻 / interval=每隔 N 小时
    "at": "04:17",            # mode=daily 时用，HH:MM
    "every_hours": 6,         # mode=interval 时用，1~168
}

# 两次触发的最小间隔。正常情况下 last_start 已经挡住了重复触发，
# 这里只是兜底：万一状态文件写失败，也别一分钟内连跑十遍。
MIN_GAP = 120.0

HOURS_MIN, HOURS_MAX = 1, 168


def _schedule_path(root: str) -> str:
    return os.path.join(root, "data", "schedule.json")


def _state_path(root: str) -> str:
    return os.path.join(root, "data", "schedule_state.json")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict) -> None:
    """先写临时文件再 rename：中途断电也不会留下半个 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def parse_at(v) -> tuple[int, int] | None:
    """'4:17' / '04:17' / '04：17'（中文冒号）都认，非法返回 None。"""
    s = str(v or "").strip().replace("：", ":").replace(".", ":")
    if ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def normalize(d: dict) -> dict:
    """把用户输入/文件内容收拾成一定合法的排期。非法值退回默认，不抛异常。"""
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        return out

    if "enabled" in d:
        out["enabled"] = bool(d.get("enabled"))

    mode = str(d.get("mode") or "").strip().lower()
    if mode in ("daily", "interval"):
        out["mode"] = mode
    elif mode in ("every", "hours", "hourly"):
        out["mode"] = "interval"

    hm = parse_at(d.get("at"))
    if hm:
        out["at"] = f"{hm[0]:02d}:{hm[1]:02d}"
    elif "at" in d:
        out["at"] = DEFAULTS["at"]      # 写了个不认识的时刻，退回默认

    try:
        hours = float(d.get("every_hours"))
    except (TypeError, ValueError):
        hours = float(DEFAULTS["every_hours"])
    if not (HOURS_MIN <= hours <= HOURS_MAX):
        hours = float(DEFAULTS["every_hours"])
    # 6.0 -> 6，1.5 保留小数
    out["every_hours"] = int(hours) if float(hours).is_integer() else round(hours, 2)
    return out


def load(root: str) -> dict:
    return normalize(_read_json(_schedule_path(root)))


def save(root: str, data: dict) -> dict:
    clean = normalize(data)
    _write_json(_schedule_path(root), clean)
    return clean


def load_state(root: str) -> dict:
    return _read_json(_state_path(root))


def mark_start(root: str, ts: float | None = None) -> None:
    st = load_state(root)
    st["last_start"] = float(ts if ts is not None else time.time())
    st["running"] = True
    _write_json(_state_path(root), st)


def mark_end(root: str, ok: bool, rc: int = 0, ts: float | None = None) -> None:
    st = load_state(root)
    end = float(ts if ts is not None else time.time())
    st["last_end"] = end
    st["last_ok"] = bool(ok)
    st["last_rc"] = int(rc)
    st["running"] = False
    start = float(st.get("last_start") or 0)
    if start > 0 and end >= start:
        st["last_dur"] = round(end - start, 1)
    _write_json(_state_path(root), st)


def next_run(sched: dict, state: dict, now_ts: float | None = None) -> float | None:
    """下次该跑的时间戳；暂停中返回 None。"""
    if not sched.get("enabled"):
        return None
    now_ts = time.time() if now_ts is None else float(now_ts)
    last = float(state.get("last_start") or 0)

    if sched.get("mode") == "interval":
        if last <= 0:
            return now_ts                      # 从没跑过 -> 立刻到点
        return last + float(sched.get("every_hours") or 6) * 3600.0

    hm = parse_at(sched.get("at")) or parse_at(DEFAULTS["at"])
    assert hm is not None
    base = time.localtime(now_ts)
    target = time.mktime((base.tm_year, base.tm_mon, base.tm_mday,
                          hm[0], hm[1], 0, 0, 0, -1))
    if last >= target:
        # 今天这个点已经跑过了，排到明天
        target = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + 1,
                              hm[0], hm[1], 0, 0, 0, -1))
    return target


def due(root: str, now_ts: float | None = None) -> tuple[bool, dict, dict]:
    """到点没有。返回 (是否该跑, 排期, 状态)。"""
    sched = load(root)
    state = load_state(root)
    now_ts = time.time() if now_ts is None else float(now_ts)
    if not sched.get("enabled"):
        return False, sched, state
    last = float(state.get("last_start") or 0)
    if last > 0 and now_ts - last < MIN_GAP:
        return False, sched, state
    nxt = next_run(sched, state, now_ts)
    return bool(nxt is not None and now_ts >= nxt), sched, state


def describe(sched: dict) -> str:
    """给界面和日志用的一句话。"""
    if not sched.get("enabled"):
        return "已暂停"
    if sched.get("mode") == "interval":
        h = sched.get("every_hours") or 6
        return f"每 {h} 小时一次"
    return f"每天 {sched.get('at') or DEFAULTS['at']}"


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, OSError, OverflowError):
        return "-"


def summary(root: str, now_ts: float | None = None) -> dict:
    """网页 /api/schedule 用的完整快照。"""
    now_ts = time.time() if now_ts is None else float(now_ts)
    sched = load(root)
    state = load_state(root)
    nxt = next_run(sched, state, now_ts)
    if not sched.get("enabled"):
        next_text = "暂停中"
    elif nxt is not None and nxt <= now_ts:
        # 从没跑过、或者已经过了点还没被 tick 叫醒：别显示一个过去的时间，
        # 那会让人以为任务卡住了
        next_text = "即将运行（10 分钟内）"
    else:
        next_text = _fmt_ts(nxt)
    return {
        "sched": sched,
        "desc": describe(sched),
        "next_ts": nxt,
        "next_text": next_text,
        "last_text": _fmt_ts(state.get("last_end") or state.get("last_start")),
        "last_ok": state.get("last_ok"),
        "last_rc": state.get("last_rc"),
        "last_dur": state.get("last_dur"),
        "running": bool(state.get("running")),
        "due_now": bool(nxt is not None and now_ts >= nxt),
        "now_text": _fmt_ts(now_ts),
    }
