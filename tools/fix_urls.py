"""规范化 discover 自动发现的 URL（把 refs/heads/xxx 之类还原成规范形式），并重新探测确认。"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from nodehub import fetcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))["fetch"]
p = os.path.join(ROOT, "sources", "registry.yaml")
d = yaml.safe_load(open(p, encoding="utf-8"))

fixed = bad = same = 0
for s in d["sources"]:
    if "discovered" not in (s.get("tags") or []):
        continue
    u = s["url"]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+/[^/]+)/(.+)$", u)
    if not m:
        continue
    repo, tail = m.groups()
    for pre in ("refs/heads/", "heads/"):
        if tail.startswith(pre):
            tail = tail[len(pre):]
            break
    branch, _, path = tail.partition("/")
    nu = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    if nu == u:
        same += 1
        continue
    text, err = fetcher.fetch_text(nu, cfg)
    if not text:
        print(f"  ✗ {repo}: {err}")
        bad += 1
        continue
    nodes, fmt = fetcher.parse_subscription(text)
    if len(nodes) >= 5:
        s["url"] = nu
        s["yield"] = len(nodes)
        s["format"] = fmt
        s["types"] = sorted({x.get("type") for x in nodes})
        fixed += 1
        print(f"  ✓ {repo:<44} {len(nodes):>6}")
    else:
        print(f"  ✗ {repo}: 规范化后只有 {len(nodes)} 条")
        bad += 1

yaml.safe_dump(d, open(p, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False, width=120)
print(f"\n规范化 {fixed} 条，本就正确 {same} 条，失败 {bad} 条")
