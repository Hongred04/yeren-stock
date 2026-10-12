# -*- coding: utf-8 -*-
"""交易时段与「已交易分钟数」测试 —— 盘中量能折算的地基。

用户实测：「盘中预估的全天成交额不准」。根因不在曲线本身，而在 `tmin`：
`market_session` 原来拿 HHMM **直接相减**（`t - 930`、`120 + (t - 1300)`），
而 HHMM 不是线性刻度 —— 9:59(959) → 10:00(1000) 只差 1 分钟却差 41。
结果每跨一个小时就多算 40 分钟：

    10:00 实际 30 分钟 → 算成 70    11:00 实际 90 → 算成 170

tmin 虚高 → 日内占比 `frac` 虚高 → 预估全天额被**低估 30% 以上**
（实测 10:21：真值约 28600 亿，页面给 19443 亿）。而量能模式阈值是
15000/5000/12000 亿 —— 低估足以把"峰值量能·做波段"错判成"缩量·做短线"。

本测试把每个时段边界都钉死。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trading import market_session, _hhmm_to_min   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# (时刻, 期望 phase, 期望已交易分钟)
CASES = [
    ((9, 10), "pre_open", 0),
    ((9, 15), "auction", 0),
    ((9, 29), "auction", 0),
    ((9, 30), "morning", 0),
    ((9, 31), "morning", 1),
    ((9, 59), "morning", 29),
    ((10, 0), "morning", 30),          # 跨小时：这里原来 +40
    ((10, 21), "morning", 51),
    ((10, 30), "morning", 60),
    ((11, 0), "morning", 90),          # 跨两小时：原来 +80
    ((11, 29), "morning", 119),
    ((11, 30), "noon", 120),
    ((12, 30), "noon", 120),
    ((13, 0), "afternoon", 120),
    ((13, 30), "afternoon", 150),
    ((13, 59), "afternoon", 179),
    ((14, 0), "tail", 180),
    ((14, 30), "tail", 210),
    ((14, 59), "tail", 239),
    ((15, 0), "just_closed", 240),
    ((15, 20), "just_closed", 240),
    ((15, 30), "after_close", 240),
    ((20, 0), "after_close", 240),
]


def main():
    print("[1] HHMM → 分钟数换算（HHMM 不是线性刻度）")
    ok(_hhmm_to_min(930) == 570, "9:30 → 570")
    ok(_hhmm_to_min(1000) == 600, "10:00 → 600（不是 1000-930=70）")
    ok(_hhmm_to_min(1130) == 690, "11:30 → 690")
    ok(_hhmm_to_min(1300) == 780, "13:00 → 780")
    ok(_hhmm_to_min(1500) == 900, "15:00 → 900")
    ok(_hhmm_to_min(1000) - _hhmm_to_min(959) == 1,
       "9:59 → 10:00 只差 1 分钟（直接相减会差 41）")

    print("\n[2] 各时段 phase 与已交易分钟数")
    bad = []
    for (hh, mm), phase, tmin in CASES:
        s = market_session(datetime(2026, 10, 12, hh, mm))
        if s["phase"] != phase or s["tmin"] != tmin:
            bad.append(f"{hh:02d}:{mm:02d} 期望 {phase}/{tmin}，"
                       f"实际 {s['phase']}/{s['tmin']}")
    ok(not bad, f"{len(CASES)} 个边界全部正确" if not bad else "；".join(bad[:4]))

    print("\n[3] tmin 单调不减（时间往前走，已交易分钟数不能倒退）")
    prev, mono = -1, True
    for (hh, mm), _, _ in CASES:
        v = market_session(datetime(2026, 10, 12, hh, mm))["tmin"]
        if v < prev:
            mono = False
        prev = v
    ok(mono, "整条时间轴单调不减")

    print("\n[4] 周末休市（tmin 归零、不算交易日）")
    s = market_session(datetime(2026, 10, 11, 10, 30))       # 周日
    ok(s["phase"] == "rest" and s["tmin"] == 0, "周日 → rest / tmin=0")
    ok(s["is_trade_day"] is False, "周末不是交易日")

    print("\n[5] 折算自洽：10:21 的占比必须小于 11:00（不能因为跨小时而虚高）")
    f_1021 = market_session(datetime(2026, 10, 12, 10, 21))["tmin"]
    f_1100 = market_session(datetime(2026, 10, 12, 11, 0))["tmin"]
    ok(f_1021 < f_1100, f"10:21({f_1021}) < 11:00({f_1100})")
    ok(f_1100 <= 120, f"上午最多 120 分钟（得到 {f_1100}）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
