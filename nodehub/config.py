"""配置加载：config.yaml + 源台账（registry.yaml）。

订阅源以台账为单一事实源——config.yaml 里不再重复列源，
只写 `sources_file` 指向台账，这里负责把 status=active 的条目拼进来。
run.py 和 web.py 都走这个函数，避免两边行为跑偏。
"""
from __future__ import annotations

import os

import yaml


def load_config(path: str, root: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    sf = cfg.get("sources_file")
    if sf:
        p = sf if os.path.isabs(sf) else os.path.join(root, sf)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                reg = yaml.safe_load(f) or {}
            srcs = [
                {"name": s["id"], "url": s["url"], "kind": s.get("kind") or "github"}
                for s in (reg.get("sources") or [])
                if s.get("status") == "active" and s.get("url")
            ]
            if srcs:
                cfg["sources"] = srcs
    return cfg


def archived_sources(cfg: dict, root: str) -> set[str]:
    """从台账里取出已归档的源名。"""
    sf = cfg.get("sources_file")
    if not sf:
        return set()
    p = sf if os.path.isabs(sf) else os.path.join(root, sf)
    if not os.path.exists(p):
        return set()
    try:
        with open(p, "r", encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
        return {s["id"] for s in (reg.get("sources") or []) if s.get("status") == "archived"}
    except Exception:
        return set()


def source_meta(cfg: dict, root: str) -> dict:
    """台账里每个源的元信息（yield / reach / kind / status），按 id 索引。"""
    sf = cfg.get("sources_file")
    if not sf:
        return {}
    p = sf if os.path.isabs(sf) else os.path.join(root, sf)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
        return {s["id"]: s for s in (reg.get("sources") or [])}
    except Exception:
        return {}
