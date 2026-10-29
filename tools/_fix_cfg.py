"""修 config.yaml 里被 sed 插坏的 check.speed 段（一次性脚本）。"""
import re

p = "/opt/nodehub/config.yaml"
s = open(p, encoding="utf-8").read()

# 上一步 sed 把 \n 当字面量插进去了，先整段删掉
s = re.sub(r"^  speed:/n.*?^    chunk: 10\n", "", s, flags=re.M | re.S)

block = ("  speed:\n"
         "    enabled: true\n"
         "    count: 30\n"
         "    size: 10\n"
         "    max_sec: 12\n"
         "    chunk: 10\n")

if "  speed:\n    enabled:" not in s:
    s = s.replace("  verify_after_all: true\n",
                  "  verify_after_all: true\n" + block, 1)

open(p, "w", encoding="utf-8").write(s)

import yaml  # noqa: E402
d = yaml.safe_load(open(p, encoding="utf-8"))
print("check.speed =", d["check"]["speed"])
print("check.real.threads =", d["check"]["real"]["threads"])
