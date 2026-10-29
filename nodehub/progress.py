"""把 run.py 的 stdout 翻译成进度事件。

桌面界面和网页版共用这一套规则，省得两边各写一遍正则、进度对不上。
解析失败不算错——日志照常显示，只是进度条不动。
"""
from __future__ import annotations

import re


def classify(line: str) -> str:
    """给一行日志打颜色标签：ok / err / warn / dim / 空。"""
    if line.startswith("  ✓") or "OK " in line[:6]:
        return "ok"
    if line.startswith("  ✗") or "失败" in line or "错误" in line:
        return "err"
    if "跳过" in line or "⚠" in line:
        return "warn"
    if line.startswith("===") or line.startswith("  ["):
        return "dim"
    return ""


def parse(line: str) -> dict:
    """返回 {tag, pct, phase, detail}，没有进度的字段为 None。

    pct 只增不减由调用方保证（这里给的是「当前阶段应该到哪」）。
    """
    ev: dict = {"tag": classify(line), "pct": None, "phase": None, "detail": None}

    if "步骤 1 / 搜集" in line:
        ev.update(pct=2, phase="采集", detail="正在抓取所有订阅源…")

    m = re.search(r"完成: (\d+)/(\d+) 个源有效", line)
    if m:
        ev.update(pct=18, phase="采集完成",
                  detail=f"{m.group(1)}/{m.group(2)} 个源有节点")

    if "步骤 2 / 管理" in line:
        ev.update(pct=20, phase="入库", detail="跨源去重、清理过期…")

    m = re.search(r"节点库总量: ([\d,]+)", line)
    if m:
        ev.update(pct=22, detail=f"库内共 {m.group(1)} 个节点")

    m = re.search(r"待检测: (\d+) 个节点", line)
    if m:
        ev.update(pct=23, phase="TCP 预筛",
                  detail=f"待检测 {m.group(1)} 个节点，先测端口通不通…")

    m = re.search(r"\[1/2\] TCP 预筛: (\d+) 通过 / (\d+) 淘汰", line)
    if m:
        ev.update(pct=45, phase="真实网页访问",
                  detail=f"TCP 通过 {m.group(1)} 个，开始模拟网页访问…")

    m = re.search(r"真连测试 (\d+)-(\d+) / (\d+)", line)
    if m:
        end, total = int(m.group(2)), int(m.group(3))
        ev.update(pct=45 + 40 * (end / max(total, 1)), phase="真实网页访问",
                  detail=f"正在验证 {end}/{total} 个节点能否真的打开网页…")

    m = re.search(r"\[2/2\] 真连测试: (\d+) 可用", line)
    if m:
        ev.update(pct=87, detail=f"{m.group(1)} 个节点可用")

    if "复核 —— " in line:
        ev.update(pct=89, phase="复核", detail="对判定可用的节点再测一遍…")

    m = re.search(r"复核通过 (\d+)/(\d+)", line)
    if m:
        ev.update(pct=96, phase="复核完成", detail=f"{m.group(1)}/{m.group(2)} 通过")

    # ---- 订阅测速（run.py probe）
    if "订阅测速 —— " in line:
        ev.update(pct=3, phase="准备", detail=line.split("——", 1)[-1].strip())

    m = re.search(r"本次检测: (\d+) 个节点", line)
    if m:
        ev.update(pct=8, phase="准备检测",
                  detail=f"本次要测 {m.group(1)} 个节点")

    m = re.search(r"新建记录 #(\d+)：(\d+) 个节点", line)
    if m:
        ev.update(pct=6, phase="解析完成",
                  detail=f"从订阅里解析出 {m.group(2)} 个节点")

    m = re.search(r"\[(\d+)/(\d+)\] TCP 预筛: (\d+) 通过 / (\d+) 淘汰", line)
    if m:
        ev.update(pct=8 + 22 * int(m.group(1)) / max(int(m.group(2)), 1),
                  phase="TCP 预筛",
                  detail=f"端口通 {m.group(3)} 个，淘汰 {m.group(4)} 个")

    m = re.search(r"\[(\d+)/(\d+)\] 延迟测试: (\d+) 可用", line)
    if m:
        ev.update(pct=40, phase="延迟测试",
                  detail=f"{m.group(3)} 个节点能真的打开网页")

    m = re.search(r"\[(\d+)/(\d+)\] 下载测速 (\d+) 个", line)
    if m:
        ev.update(pct=55, phase="下载测速",
                  detail=f"开始给 {m.group(3)} 个节点测下载速度…")

    m = re.search(r"测速 (\d+)/(\d+)\s+([\d.]+) MB/s", line)
    if m:
        ev.update(pct=55 + 40 * int(m.group(1)) / max(int(m.group(2)), 1),
                  detail=f"第 {m.group(1)}/{m.group(2)} 个：{m.group(3)} MB/s")

    if re.match(r"^完成: ", line):
        ev.update(pct=100, phase="完成", detail=line[4:].strip())

    # ---- 最终筛查（run.py speed）：真跑一遍下载
    if "最终筛查 —— " in line:
        ev.update(pct=55, phase="最终筛查", detail="给可用节点测真实下载速度…")

    m = re.search(r"候选 (\d+) 个", line)
    if m:
        ev.update(pct=58, detail=f"候选 {m.group(1)} 个，按延迟从快到慢排队")

    m = re.search(r"^\s+(\d+)/(\d+)\s+([\d.]+) MB/s", line)
    if m:
        ev.update(pct=58 + 38 * int(m.group(1)) / max(int(m.group(2)), 1),
                  phase="下载测速",
                  detail=f"第 {m.group(1)}/{m.group(2)} 个：{m.group(3)} MB/s")

    m = re.search(r"测出速度 (\d+) / (\d+)", line)
    if m:
        ev.update(pct=98, phase="最终筛查完成",
                  detail=f"{m.group(1)}/{m.group(2)} 个测出下载速度")

    if line.strip() == "完成":
        ev.update(pct=100, phase="完成", detail="全部步骤跑完")

    return ev
