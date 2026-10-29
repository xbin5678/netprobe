"""抓取订阅：多镜像回退 + 自动识别订阅格式。"""
from __future__ import annotations

import datetime as _dt
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml

from .parser import b64d, normalize_clash_proxy, parse_uri

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_local = threading.local()


def _session(proxy: str = "") -> requests.Session:
    s = getattr(_local, "s", None)
    key = getattr(_local, "k", None)
    if s is None or key != proxy:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "*/*"})
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        # 不读 HTTP_PROXY / HTTPS_PROXY 环境变量。
        # 这些变量经常是别的东西留下的（比如工具环境自己的沙箱代理），
        # 会让人误判成「代理开了但连不上」。要用代理就显式配 fetch.proxy。
        s.trust_env = False
        _local.s = s
        _local.k = proxy
    return s


# ---------------------------------------------------------------- 日期占位符

_DATE_RE = re.compile(r"\{(YYYYMMDD|YYYY|MM|DD)(-(\d+))?\}")


def render_date(url: str, offset_days: int = 0) -> str:
    d = _dt.datetime.now() - _dt.timedelta(days=offset_days)

    def repl(m):
        key = m.group(1)
        extra = int(m.group(3)) if m.group(3) else 0
        dd = d - _dt.timedelta(days=extra)
        return {
            "YYYYMMDD": dd.strftime("%Y%m%d"),
            "YYYY": dd.strftime("%Y"),
            "MM": dd.strftime("%m"),
            "DD": dd.strftime("%d"),
        }[key]

    return _DATE_RE.sub(repl, url)


def has_date_placeholder(url: str) -> bool:
    return bool(_DATE_RE.search(url))


# ---------------------------------------------------------------- 镜像

def candidate_urls(url: str, mirrors: list[str]) -> list[tuple[str, int]]:
    """给 GitHub raw 链接生成镜像候选列表，返回 [(url, 模板下标)]。

    模板下标用于「记住哪个镜像好用」——raw 被墙时第一个模板会白等一次超时，
    记住上次成功的那个下标，下次直接跳过前面的。
    """
    m = re.match(r"https://raw\.githubusercontent\.com/(.+)$", url)
    if not m:
        return [(url, -1)]
    path = m.group(1)
    # user/repo/branch/rest -> user/repo@branch/rest（jsdelivr 要求的分支写法）
    parts = path.split("/")
    path_jsd = "/".join(parts[:2]) + "@" + parts[2] + "/" + "/".join(parts[3:]) if len(parts) > 3 else path
    out = []
    for idx, tpl in enumerate(mirrors):
        try:
            out.append((tpl.format(path=path, path_jsd=path_jsd), idx))
        except Exception:
            continue
    # 去重但保留下标
    seen, uniq = set(), []
    for u, idx in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((u, idx))
    return uniq


# 上次成功的镜像模板下标。None = 还没成功过
_best_mirror: int | None = None


def _ordered(cands: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """把上次成功的镜像排到最前面，省掉一次必然超时的等待。"""
    if _best_mirror is None or len(cands) <= 1:
        return cands
    head = [c for c in cands if c[1] == _best_mirror]
    if not head:
        return cands
    return head + [c for c in cands if c[1] != _best_mirror]


def _is_html(text: str) -> bool:
    head = text.lstrip()[:600].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head>" in head[:200]


# ---------------------------------------------------------------- 抓取

def fetch_text(url: str, cfg: dict) -> tuple[str | None, str]:
    """返回 (文本, 实际使用的url)；失败时第二项是错误原因。"""
    global _best_mirror
    timeout = cfg.get("timeout", 25)
    retries = cfg.get("retries", 1)
    mirrors = cfg.get("mirrors") or ["https://raw.githubusercontent.com/{path}"]
    conn_timeout = min(5, timeout)
    proxy = (cfg.get("proxy") or "") if cfg.get("use_proxy") else ""

    last_err = ""
    for attempt in range(retries + 1):
        for cand, idx in _ordered(candidate_urls(url, mirrors)):
            try:
                r = _session(proxy).get(cand, timeout=(conn_timeout, timeout), allow_redirects=True)
                if r.status_code == 404:
                    # 404 是路径不存在，换镜像也是 404，立刻放弃
                    return None, "HTTP 404"
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                text = r.text
                if not text or len(text) < 20:
                    last_err = "empty"
                    continue
                if _is_html(text):
                    last_err = "html"
                    continue
                if idx >= 0:
                    _best_mirror = idx
                return text, cand
            except Exception as e:
                last_err = type(e).__name__
        if attempt < retries:
            continue
    return None, last_err


# ---------------------------------------------------------------- 解析订阅正文

def _looks_like_yaml(text: str) -> bool:
    # 有些订阅开头是几十行注释，不能只看前几百个字符
    return bool(re.search(r"(?m)^\s*proxies\s*:", text[:200000]))


def _extract_uris(text: str) -> list[str]:
    uris = re.findall(r"(?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\"'<>]+", text)
    return uris


def _salvage_yaml_proxies(text: str) -> list[dict]:
    """整份 YAML 解析失败时的兜底：逐行抢救 flow-style 条目 `- {name: ..., type: ...}`。

    很多聚合仓库的 yaml 会被截断在半个 flow mapping 上，导致 safe_load 直接抛错，
    但其实前面几百条都是好的。
    """
    out = []
    pat = re.compile(r"^\s*-\s*(\{.*\})\s*$")
    for line in text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        try:
            d = yaml.safe_load(m.group(1))
        except Exception:
            continue
        p = normalize_clash_proxy(d) if isinstance(d, dict) else None
        if p:
            out.append(p)
    return out


def parse_subscription(text: str) -> tuple[list[dict], str]:
    """把订阅正文解析成 clash proxy 列表，返回 (nodes, 格式说明)。"""
    text = text.strip()
    if not text:
        return [], "empty"

    # 1) clash / mihomo yaml
    if _looks_like_yaml(text):
        doc = None
        try:
            doc = yaml.safe_load(text)
        except Exception:
            doc = None
        if isinstance(doc, dict):
            raw = doc.get("proxies") or []
            out = [p for p in (normalize_clash_proxy(d) for d in raw) if p]
            if out:
                return out, "clash-yaml"
        # yaml 被截断 / 格式坏了，逐行抢救
        out = _salvage_yaml_proxies(text)
        if out:
            return out, "clash-yaml(salvaged)"

    # 2) 明文 URI 列表
    uris = _extract_uris(text)
    if not uris:
        # 3) base64 编码的 URI 列表
        dec = b64d(text)
        if dec:
            uris = _extract_uris(dec)
            if uris:
                return [p for p in (parse_uri(u) for u in uris) if p], "base64-uri"
        return [], "unknown"

    return [p for p in (parse_uri(u) for u in uris) if p], "plain-uri"


# ---------------------------------------------------------------- Telegram 频道

_TG_URI_RE = re.compile(r"(?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\"'<>\\]+")


def fetch_telegram(url: str, cfg: dict) -> tuple[list[dict], str]:
    """抓 t.me/s/<频道> 网页版，从消息正文里提取节点。

    TG 网页版只返回最近约 20 条消息，所以这个源的价值在于「新鲜」，
    需要高频轮询。必须走代理，国内直连不通。
    """
    import html as _html
    proxy = (cfg.get("proxy") or "") if cfg.get("use_proxy") else ""
    try:
        r = _session(proxy).get(url, timeout=(15, 30), allow_redirects=True)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        # HTML 实体反转义：&amp; -> &，否则 path=/a&amp;b 这类参数会解析错
        text = _html.unescape(r.text)
        uris = _TG_URI_RE.findall(text)
        seen, out = set(), []
        for u in uris:
            if u in seen:
                continue
            seen.add(u)
            p = parse_uri(u)
            if p:
                out.append(p)
        return out, f"tg-html({len(uris)} uris)"
    except Exception as e:
        return [], type(e).__name__


# ---------------------------------------------------------------- 代理可用性

_proxy_ok: bool | None = None


def proxy_available(cfg: dict) -> bool:
    """代理端口是否真的在监听。

    用来决定 TG 源要不要抓——代理关着的时候，154 个频道全去等超时纯属浪费。
    结果缓存，一次运行只探一次。
    """
    global _proxy_ok
    if _proxy_ok is not None:
        return _proxy_ok
    p = cfg.get("proxy") or ""
    m = re.match(r"(?:socks5h?|http)://([^:/]+):(\d+)", p)
    if not m:
        _proxy_ok = False
        return False
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=1.5):
            _proxy_ok = True
    except Exception:
        _proxy_ok = False
    return _proxy_ok


# ---------------------------------------------------------------- 批量抓取

def fetch_all(sources: list[dict], fetch_cfg: dict) -> list[dict]:
    """并发抓取所有源，返回 [{source, url, nodes, fmt, error}]"""
    conc = max(1, int(fetch_cfg.get("concurrency", 8)))
    has_proxy = bool(fetch_cfg.get("proxy")) and proxy_available(fetch_cfg)

    skipped_tg = 0

    def one(src):
        nonlocal skipped_tg
        name = src.get("name") or src["url"]
        url = src["url"]
        kind = src.get("kind") or "github"

        # Telegram 频道走单独的抓取路径，且必须走代理
        if kind == "telegram":
            if not has_proxy:
                skipped_tg += 1
                return {"source": name, "url": url, "nodes": [], "fmt": "",
                        "error": "跳过（代理未开启）", "skipped": True}
            tg_cfg = dict(fetch_cfg)
            tg_cfg["use_proxy"] = True
            nodes, fmt = fetch_telegram(url, tg_cfg)
            if nodes:
                return {"source": name, "url": url, "nodes": nodes, "fmt": fmt,
                        "error": None, "bytes": len(nodes)}
            return {"source": name, "url": url, "nodes": [], "fmt": fmt, "error": fmt}

        attempts = [0, 1, 2] if has_date_placeholder(url) else [0]
        last_err = ""
        for off in attempts:
            real = render_date(url, off)
            text, used = fetch_text(real, fetch_cfg)
            if text is None:
                last_err = used
                continue
            nodes, fmt = parse_subscription(text)
            if nodes:
                return {
                    "source": name, "url": used, "nodes": nodes,
                    "fmt": fmt, "error": None, "bytes": len(text),
                }
            last_err = f"parsed 0 nodes ({fmt})"
        return {"source": name, "url": url, "nodes": [], "fmt": "", "error": last_err}

    with ThreadPoolExecutor(max_workers=conc) as ex:
        out = list(ex.map(one, sources))
    if skipped_tg:
        print(f"  （{skipped_tg} 个 Telegram 频道已跳过：代理未开启）")
    return out
