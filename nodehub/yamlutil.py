"""YAML 输出：所有字符串值强制加引号（键不加）。

为什么不能直接用 yaml.safe_dump
-------------------------------
REALITY 的 short-id 是十六进制字符串，像 `08`、`11e9`、`8e45`、`0e40` 这种
「长得像数字」的值，PyYAML 会原样输出成 `short-id: 08`，而 mihomo（Go 的
yaml 解析器）会把它读成数字，于是报 `invalid REALITY short ID`，
**整个配置文件加载失败**——不是这一个节点不可用，是所有节点一起废。

实测 3.2 万节点的库里有 11 个这样的节点，各自拖垮了 200 个节点的一整批检测；
用户导入订阅时同样会整份失败。所以统一用这里的 dump()，把所有字符串值都引起来，
不再依赖两边解析器对「像不像数字」的判断是否一致。
"""
from __future__ import annotations

import yaml


class QStr(str):
    """标记：输出时加引号。"""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _repr_qstr(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


_QuotedDumper.add_representer(QStr, _repr_qstr)


def _wrap(o):
    """把值里的字符串包成 QStr，字典的键保持原样。"""
    if isinstance(o, str):
        return QStr(o)
    if isinstance(o, dict):
        return {k: _wrap(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_wrap(x) for x in o]
    return o


def dump(data, **kw) -> str:
    """等价于 yaml.safe_dump，但字符串值一定带引号。"""
    kw.setdefault("allow_unicode", True)
    kw.setdefault("sort_keys", False)
    return yaml.dump(_wrap(data), Dumper=_QuotedDumper, **kw)


def dump_to(path: str, data, **kw) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump(data, **kw))
