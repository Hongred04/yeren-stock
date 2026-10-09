# -*- coding: utf-8 -*-
"""情绪阶段稳定器测试 —— 用模拟的盘中噪声序列，验证阶段不会来回横跳。

直接造一段"原始信号忽真忽假"的行情（这正是真机上的样子），
断言稳定后的结论：该稳的稳住、该快的快、该慢的慢。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 让状态文件落在临时目录，别污染真实 data/
_tmp = tempfile.mkdtemp(prefix="stab_")
os.environ["STOCK_DATA_DIR"] = _tmp

from core import stage_stabilizer as stab   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


class Clock:
    """可控时钟：测试里"经过 90 秒"就是 t += 90。"""

    def __init__(self, t0=1_700_000_000.0):
        self.t = t0

    def __call__(self):
        return self.t


clock = Clock()
stab.time.time = clock          # 打桩

D = "2026-10-09"
TH = 0.30
NORMAL, WARN, HOT = 0.20, 0.34, 0.52


def feed(raw, hurt, tmin=60, advance=30):
    clock.t += advance
    return stab.stabilize(D, raw, hurt, TH, tmin=tmin)


def main():
    stab.reset(D)

    print("[1] 平静期：原始信号为假 → 不确认")
    r = feed(False, NORMAL)
    ok(not r["confirmed"], "不确认退潮")
    ok(r["state"] == "calm", "状态 = calm")

    print("\n[2] 单帧噪声（一跳一落）→ 不能被带跑")
    r = feed(True, WARN)                      # 第 1 次真
    ok(not r["confirmed"], "首次真信号不立即确认")
    ok(r["state"] == "watch", "状态 = watch（退潮预警）")
    r = feed(False, NORMAL, advance=10)       # 马上又假
    ok(not r["confirmed"], "信号回落 → 不确认")
    ok(r["state"] == "calm", "回到 calm")
    print("      ↑ 旧逻辑在这里就会报一次「退潮、无条件清仓」")

    print("\n[3] 持续两帧、跨越 60s → 确认退潮")
    feed(True, WARN, advance=0)
    r = feed(True, WARN, advance=70)
    ok(r["confirmed"], "连续 2 次且跨度 ≥60s → 确认")
    ok(r["state"] == "ebb", "状态 = ebb")
    ok(r["changed"], "本次发生了状态切换")

    print("\n[4] 确认后再出现单帧噪声 → 必须稳住，不许弹回")
    r = feed(False, WARN, advance=20)
    ok(r["confirmed"], "单帧假信号不解除退潮")
    ok(r["state"] in ("ebb", "recovering"), f"状态仍是退潮侧：{r['state']}")
    r = feed(False, NORMAL, advance=20)
    ok(r["confirmed"], "两帧但间隔不够 → 仍不解触")
    r = feed(False, NORMAL, advance=200)      # 持续时间够了
    ok(not r["confirmed"], "连续 + 跨度够了才解除")
    ok(r["state"] == "calm", "状态 = calm")

    print("\n[5] 强信号（超阈值一个身位）→ 不等确认，立刻认")
    r = feed(True, HOT)
    ok(r["confirmed"], "强信号直接确认")
    ok("直接确认" in (r["note"] or ""), "说明里点出是强信号")
    stab.reset(D)

    print("\n[6] 开盘保护：头 30 分钟只认强信号")
    r = feed(True, WARN, tmin=8, advance=0)
    r = feed(True, WARN, tmin=12, advance=90)
    ok(not r["confirmed"], "开盘初期 12 分钟、非强信号 → 不确认")
    ok(r["state"] == "watch", "只给预警")
    r = feed(True, HOT, tmin=14, advance=10)
    ok(r["confirmed"], "但真塌方（强信号）照样立刻认")
    stab.reset(D)
    r = feed(True, WARN, tmin=40, advance=0)
    r = feed(True, WARN, tmin=40, advance=90)
    ok(r["confirmed"], "过了 30 分钟保护期就该正常确认了")

    print("\n[7] 换日自动归零")
    r = stab.stabilize("2026-10-10", False, NORMAL, TH, tmin=5)
    ok(not r["confirmed"], "新交易日不会继承前一天的退潮状态")
    ok(r["state"] == "calm", "状态 = calm")

    print("\n[8] 常规噪声序列：30 次里 12 次忽真忽假，看最终翻转了几次")
    stab.reset(D)
    flips = 0
    prev = False
    seq = [False, True, False, True, True, False, False, True, False, True,
           True, True, False, True, False, False, True, True, False, False,
           True, False, True, False, False, True, False, True, True, False]
    for i, raw in enumerate(seq):
        hurt = WARN if raw else NORMAL
        r = feed(raw, hurt, tmin=60, advance=25)
        if r["confirmed"] != prev:
            flips += 1
            prev = r["confirmed"]
    ok(flips <= 3, f"原始信号跳变 {sum(1 for a, b in zip(seq, seq[1:]) if a != b)} 次，"
                   f"稳定后只切换 {flips} 次")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
