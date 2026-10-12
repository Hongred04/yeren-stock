# -*- coding: utf-8 -*-
"""节点（regime）契约测试 —— 钉「节点决定打法」的融合（v1.25.3）。

背景（用户提供博主风控哲学）：节点决定打法，不是标的决定 —— 同样的票，
主升期捂是机会，退潮/缩量期捂是绞肉机。落成三条规则：节点→打法硬门禁、
题材/配置二分、熊市做长警示。

本测试钉住的契约：
  · `regime.classify` 把 M3 阶段 × M2 量能收敛成节点（优先级 退潮 > 极度缩量 >
    缩量 > 阶段默认），且 `allow_swing` 只在「起步/扩散 + 非缩量」为真；
  · `trading.buy_signals`：做长友好节点发 W1；非做长节点**不发** W 且改发一条
    「当前节点不做长」门禁提示；无线段形态的票不加这条噪音；
  · `outlook.build` 的 mode 与 regime 同源（杜绝两处口径分叉）；
  · `style_fit` 的 nature（情绪题材 / 业绩配置）二分。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine          # noqa: E402
from core import outlook         # noqa: E402
from core import regime          # noqa: E402
from core import swing           # noqa: E402
from core import trading         # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


BARS = [
    {"date": "2026-09-24", "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.1, "volume": 900},
    {"date": "2026-09-25", "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.2, "volume": 1000},
]
CFG = {"M4_aesthetic": {}, "M5_position": {"first_weight": 0.2}}

# 一只「有波段形态」的票（swing 现成喂进去，buy_signals 不重算形态）
SWING_STOCK = {
    "code": "sh600001", "name": "甲", "pct": 0.0, "zt_boards": 0,
    "style_fit": {"key": "swing"}, "fund_style": {"type": "institutional"},
    "swing": {"ok": True, "w1": {"line": "10日线", "level": 10.0, "burst_ok": True},
              "w2": None, "w3": None},
}
NOSWING_STOCK = {
    "code": "sh600002", "name": "乙", "pct": 0.0, "zt_boards": 0,
    "style_fit": {"key": "short"}, "fund_style": {"type": "hotmoney"},
    "swing": {"ok": False},
}


def main():
    print("[1] regime.classify 矩阵（节点 = M3 阶段 × M2 量能）")
    cases = [
        # (sentiment, volume, key, mode_name, allow_swing)
        ({"stage": "ebb"}, {"mode": "peak_swing"}, "ebb", "防守空仓", False),
        ({"stage": "climax"}, {"mode": "dry"}, "dry", "防守空仓", False),
        ({"stage": "probe"}, {"mode": "shrink_red"}, "shrink_red", "缩量低吸应对", False),
        ({"stage": "probe"}, {"mode": "shrink_short"}, "shrink_short", "短线应对", False),
        ({"stage": "probe"}, {"mode": "peak_swing"}, "probe", "波段试错", True),
        ({"stage": "follow"}, {"mode": "peak_swing"}, "follow", "波段持股", True),
        ({"stage": "follow"}, {"mode": "comfort_short"}, "follow", "波段持股", True),
        ({"stage": "climax"}, {"mode": "peak_swing"}, "climax", "短线快枪", False),
        ({"stage": "unknown"}, {"mode": "pending"}, "unknown", "观察", False),
    ]
    for s, v, k, mn, aw in cases:
        rg = regime.classify(s, v)
        ok(rg["key"] == k and rg["mode_name"] == mn and rg["allow_swing"] is aw,
           f'{s.get("stage")} × {v.get("mode")} → {rg["key"]}/{rg["mode_name"]}/'
           f'allow_swing={rg["allow_swing"]}')

    print("[2] 退潮优先于量能；极度缩量优先于阶段（优先级）")
    ok(regime.classify({"stage": "ebb"}, {"mode": "peak_swing"})["key"] == "ebb",
       "退潮压过峰值量能")
    ok(regime.classify({"stage": "follow"}, {"mode": "dry"})["key"] == "dry",
       "极度缩量压过扩散阶段")
    ok(regime.classify({"stage": "probe"}, {"mode": "shrink_short"})["allow_swing"] is False,
       "缩量期不做长（allow_swing=False）")

    print("[3] buy_signals 波段门禁：做长友好节点发 W1")
    s_probe = {"stage": "probe", "can_trade": True}
    s_ebb = {"stage": "ebb", "can_trade": False, "note": "退潮"}
    v_peak = {"mode": "peak_swing"}
    v_shrink = {"mode": "shrink_short"}
    sigs = trading.buy_signals(SWING_STOCK, BARS, s_probe, v_peak, CFG)
    ok(any(x["type"] == "W1" for x in sigs), "probe×峰值量能 → 发 W1 波段买点")
    ok(all(x.get("kind") != "block" or "不做长" not in x.get("name", "") for x in sigs),
       "做长友好节点不出现「当前节点不做长」提示")

    print("[4] buy_signals 波段门禁：非做长节点撤下 W、改发门禁提示")
    sigs2 = trading.buy_signals(SWING_STOCK, BARS, s_probe, v_shrink, CFG)
    ok(not any(x["type"] in ("W1", "W2", "W3") for x in sigs2),
       "缩量节点 → W1/W2/W3 全部撤下")
    ok(any(x["type"] == "HOLD" and x.get("blocked") and "不做长" in x.get("name", "")
           for x in sigs2),
       "缩量节点 → 改发「当前节点不做长」门禁提示")
    sigs3 = trading.buy_signals(SWING_STOCK, BARS, s_ebb, v_peak, CFG)
    ok(not any(x["type"] in ("W1", "W2", "W3") for x in sigs3),
       "退潮节点 → 不发 W（ebb 早退，连试错仓逻辑都不给波段）")

    print("[5] 无线段形态的票：非做长节点也不加「不做长」噪音")
    sigs4 = trading.buy_signals(NOSWING_STOCK, BARS, s_probe, v_shrink, CFG)
    ok(not any("不做长" in x.get("name", "") for x in sigs4),
       "swing.ok=False → 不出现「当前节点不做长」")

    print("[6] outlook.mode 与 regime 同源（杜绝两处口径分叉）")
    for s, v in [({"stage": "probe"}, {"mode": "peak_swing"}),
                 ({"stage": "climax"}, {"mode": "peak_swing"}),
                 ({"stage": "probe"}, {"mode": "shrink_short"}),
                 ({"stage": "ebb"}, {"mode": "peak_swing"})]:
        rg = regime.classify(s, v)
        out = outlook.build(CFG, "20261012", dict(s), dict(v))
        ok(out["mode"]["name"] == rg["mode_name"],
           f'outlook.mode={out["mode"]["name"]} == regime.mode={rg["mode_name"]}')

    print("[7] style_fit 的 nature 二分（情绪题材 / 业绩配置）")
    fit_cfg = engine.style_fit(
        CFG, {"code": "sh600001", "float_mv": 1000.0}, [],
        {"type": "institutional"})
    ok(fit_cfg["nature"]["key"] == "config" and fit_cfg["nature"]["label"] == "业绩配置",
       "机构主导 → 业绩配置")
    fit_th = engine.style_fit(
        CFG, {"code": "sz000001", "float_mv": 50.0}, [],
        {"type": "hotmoney"})
    ok(fit_th["nature"]["key"] == "theme" and fit_th["nature"]["label"] == "情绪题材",
       "小市值游资 → 情绪题材")
    ok("nature" in fit_th and fit_th["key"] in ("swing", "short", "avoid"),
       "nature 不改动既有 key（打法标签保持原样）")

    print("[8] swing.verdict 能不能买（节点 × 波段阶段）")
    sw_first = {"ok": True, "stage": "first", "note": "状态", "entry": {
        "type": "W1", "name": "波段第一入场点", "line": "10日线",
        "level": 10.0, "burst_ok": True}}
    v_yes = swing.verdict(sw_first, regime.classify({"stage": "follow"}, {"mode": "peak_swing"}))
    ok(v_yes["can"] == "yes" and "可以买" in v_yes["label"],
       f'起步/扩散 + 第一入场点 → 可以买（{v_yes["label"]}）')
    ok("回踩" in v_yes["reason"], "「可以买」的原因给出具体入场位")
    v_node = swing.verdict(sw_first, regime.classify({"stage": "ebb"}, {"mode": "peak_swing"}))
    ok(v_node["can"] == "no" and "不做长" in v_node["label"],
       f'退潮节点 → 不能买（{v_node["label"]}）')
    sw_stall = {"ok": True, "stage": "stall", "note": "高位放量滞涨"}
    v_stall = swing.verdict(sw_stall, regime.classify({"stage": "follow"}, {"mode": "peak_swing"}))
    ok(v_stall["can"] == "no" and "离场" in v_stall["label"],
       f'滞涨 → 不能买（{v_stall["label"]}）')
    v_bad = swing.verdict({"ok": False, "note": "K线不足 25 根"}, {})
    ok(v_bad["can"] == "no" and "数据不足" in v_bad["label"],
       "K线不足 → 数据不足/不能买")

    print("[9] 通达信软依赖：未装 / 不可用时优雅降级（不抛异常）")
    from core import datasource as ds
    ok(isinstance(ds.tdx_status(), dict) and "available" in ds.tdx_status(),
       "tdx_status() 可查询（数据源面板用）")
    _saved = ds._TDX_AVAILABLE
    ds._TDX_AVAILABLE = False
    ok(ds._kline_from_tdx("sh600519", 10) == [],
       "通达信不可用 → _kline_from_tdx 返回空列表、不抛异常（自动退回腾讯/新浪）")
    ds._TDX_AVAILABLE = _saved

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    sys.exit(1 if badc else 0)

if __name__ == "__main__":
    main()
