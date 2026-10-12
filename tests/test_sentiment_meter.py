# -*- coding: utf-8 -*-
"""E1 情绪温度计测试 —— 钉「退潮判定权只归 M3 稳定器」契约（v1.25.2）。

背景（用户 10-12 实测「自相矛盾」的另一半）：`_stage_of()` 曾收 ebb 入参、
拿**原始**亏钱面积（hurt_area ≥ 阈值，M3 口径）和「温度骤降 18°」直接把 E1
阶段强置成 "ebb" —— 与同函数注释「退潮与否以 M3 稳定器的确认为准」字面相悖：
M3 未经滞回确认（结论卡「情绪高潮 · 可以出手」）时 E1 已报「退潮」，同屏
两把尺子对不上。

本测试钉住修复后的规格：
  · `_stage_of(temp, prev_temp)` 只按温度分非退潮档位（climax/ferment/revive/ice）；
  · 亏钱面积超阈值、温度骤降，都**不**让 E1 报退潮（后者只作 drop_alert 信号）；
  · 只有 stab.peek 确认退潮，E1 才报 "ebb"（统一口径仍然有效）；
  · 温度数值 `_temperature()` 不经过 `_stage_of`，本测试不动任何温度/信号数值。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 让历史/状态文件落在临时目录，别污染真实 data/
_tmp = tempfile.mkdtemp(prefix="meter_")
os.environ["STOCK_DATA_DIR"] = _tmp

from core import sentiment_meter as sm   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# 最小行情池：涨停 1 / 跌停 1 / 炸板 1 → 亏钱面积 2/3 = 66.7%，远超 30% 阈值
ZT = [{"code": "sh600001", "name": "甲", "boards": 3, "first_time": 93000,
       "fund": 100, "amount": 1000}]
DT = [{"code": "sh600002", "name": "乙", "boards": 1, "first_time": 93000,
       "fund": 0, "amount": 100}]
ZB = [{"code": "sh600003", "name": "丙", "boards": 1, "first_time": 93000,
       "fund": 0, "amount": 100}]
CFG = {"E1_meter": {"enabled": True}}

_orig = {}


def _patch():
    """把所有会碰磁盘/网络的入口打桩，让 build() 完全可控。"""
    _orig["breadth"] = sm.ds.fetch_breadth
    _orig["yzt"] = sm.ds.fetch_yesterday_zt_pool
    _orig["dates"] = sm.ds.pool_history_dates
    _orig["load_hist"] = sm._load_hist
    _orig["save_hist"] = sm._save_hist
    _orig["peek"] = sm.stab.peek
    sm.ds.fetch_breadth = lambda *a, **k: {}
    sm.ds.fetch_yesterday_zt_pool = lambda *a, **k: []
    sm.ds.pool_history_dates = lambda *a, **k: []
    sm._load_hist = lambda: []
    sm._save_hist = lambda *a, **k: []


def _restore():
    for k, v in _orig.items():
        if k == "breadth":
            sm.ds.fetch_breadth = v
        elif k == "yzt":
            sm.ds.fetch_yesterday_zt_pool = v
        elif k == "dates":
            sm.ds.pool_history_dates = v
        elif k == "load_hist":
            sm._load_hist = v
        elif k == "save_hist":
            sm._save_hist = v
        elif k == "peek":
            sm.stab.peek = v
    _orig.clear()


def main():
    _patch()
    try:
        print("[1] 温度分档：只分非退潮档位（用户截图场景 60° 也不许报退潮）")
        ok(sm._stage_of(60, None) == "revive",
           "60° + 亏钱面积 47% 的用户截图场景 → revive，不是 ebb")
        ok(sm._stage_of(85, None) == "climax", "85° → climax")
        ok(sm._stage_of(70, None) == "ferment", "70° → ferment")
        ok(sm._stage_of(45, None) == "revive", "45° → revive")
        ok(sm._stage_of(30, None) == "ice", "30° → ice")
        ok(sm._stage_of(62, None) == "ferment", "62° 边界值 → ferment")
        ok(sm._stage_of(41, None) == "ice", "41° 边界值 → ice")

        print("[2] 亏钱面积超阈值 + M3 未确认 → E1 不报退潮（根修核心）")
        sm.stab.peek = lambda date: {"confirmed": False, "state": "calm",
                                     "hurt_area": 0.0, "note": ""}
        m = sm.build(ZT, DT, ZB, {}, "20261012", CFG)
        ok(m["metrics"]["hurt_area"] >= 0.30,
           f"场景成立：亏钱面积 {m['metrics']['hurt_area']*100:.0f}% ≥ 30% 阈值")
        ok(m["stage"] != "ebb" and "退潮" not in m["label"],
           f"peek 未确认 → stage={m['stage']}（{m['label']}），不冒充退潮")
        ok(m["stage"] == sm._stage_of(m["temperature"], None),
           "stage 严格跟随温度分档，与亏钱面积脱钩")

        print("[3] 温度骤降只作 drop_alert 信号，不升级成退潮阶段")
        sm._load_hist = lambda: [{"date": "20260101", "temperature": 80,
                                  "limit_up": 90, "limit_down": 2, "broken": 5,
                                  "max_boards": 7, "promote_rate": 0.3,
                                  "broken_ratio": 0.05}]
        _orig_temperature = sm._temperature
        sm._temperature = lambda *a, **k: 62
        try:
            ok(sm._stage_of(62, 80) == "ferment",
               "prev 80° → 今 62°（骤降 18°）→ 仍按温度分档 ferment")
            m2 = sm.build(ZT, DT, ZB, {}, "20260102", CFG)
            ok(m2["metrics"]["drop_alert"] is True,
               "build() 的 metrics.drop_alert = True（预警信号保留）")
            ok(m2["stage"] == "ferment" and "退潮" not in m2["label"],
               f"骤降当日 stage={m2['stage']}（{m2['label']}），不报退潮")
        finally:
            sm._temperature = _orig_temperature
            sm._load_hist = lambda: []

        print("[4] M3 稳定器确认退潮 → E1 才报 ebb（统一口径仍然有效）")
        sm.stab.peek = lambda date: {"confirmed": True, "state": "ebb",
                                     "hurt_area": 0.67, "note": "已确认"}
        m3 = sm.build(ZT, DT, ZB, {}, "20261012", CFG)
        ok(m3["stage"] == "ebb" and "退潮" in m3["label"],
           f"peek 确认 → stage=ebb（{m3['label']}）")
        ok(m3["advice"] == sm.STAGES["ebb"]["advice"],
           "ebb 的 advice 走退潮档（空仓，别抄底、别幻想）")

        print("[5] 模块关闭路径不受影响")
        m4 = sm.build(ZT, DT, ZB, {}, "20261012", {"E1_meter": {"enabled": False}})
        ok(m4["stage"] == "disabled" and m4["metrics"] == {},
           "enabled=False → disabled，空 metrics")
    finally:
        _restore()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    sys.exit(1 if badc else 0)


if __name__ == "__main__":
    main()
