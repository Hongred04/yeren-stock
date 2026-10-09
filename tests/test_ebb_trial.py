# -*- coding: utf-8 -*-
"""退潮期试错仓测试 —— 野哥规矩：退潮不是一刀切空仓，可以 0.5 成试错。

要点：
  · 退潮期**首板** → 给"退潮试错仓"建议，仓位 0.5 成
  · 退潮期**连板** → 不给开仓（退潮里接力就是接刀子）
  · 把 ebb_trial_weight 调成 0 → 回到原来的"无条件清仓、0%"
  · 极度缩量优先于试错（量能不够谈什么仓位）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="trial_")

from core import strategy_config as sc   # noqa: E402
from core import trading                # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


CFG = sc.default_config() if hasattr(sc, "default_config") else None
if CFG is None:
    CFG = sc.load_config()

BARS = [{"date": f"2026-09-{i:02d}", "open": 10, "close": 10 + i * 0.01,
         "high": 10.1, "low": 9.9, "volume": 100000} for i in range(1, 40)]


def sent(stage, trial_only=False, tw=0.05, can_trade=True, note=""):
    return {"stage": stage, "can_trade": can_trade, "trial_only": trial_only,
            "ebb_trial_weight": tw, "note": note, "label": stage}


def vol(mode="comfort_short"):
    return {"mode": mode, "advice": "量能舒适"}


def main():
    print("[1] 退潮 + 首板 → 给试错仓建议")
    s = sent("ebb", trial_only=True, can_trade=False, note="退潮期：不接力")
    sig = trading.buy_signals({"code": "sh600000", "zt_boards": 1}, BARS,
                              s, vol(), CFG)
    ok(len(sig) == 1, "只给一条建议")
    ok(sig[0].get("trial") is True, "标记为试错")
    ok("0.5 成" in sig[0]["position"] and "5%" in sig[0]["position"],
       f"仓位写成 0.5 成（5%）：{sig[0]['position']}")
    ok("首板" in sig[0]["entry"], "限定只做首板")
    ok(sig[0].get("blocked") is not True, "不是被禁的状态")

    print("\n[2] 退潮 + 连板 → 不给开仓")
    sig = trading.buy_signals({"code": "sh600000", "zt_boards": 3}, BARS,
                              s, vol(), CFG)
    ok(sig[0].get("blocked") is True, "连板被挡")
    ok("只做首板" in sig[0]["name"] or "首板" in sig[0]["note"], "说明为什么挡")

    print("\n[3] 仓位链：退潮期目标仓位 = 试错档，不是 0")
    plan = trading.position_plan(CFG, [{"code": "sh600000"}], s, vol(), {})
    ok(abs(plan["target_total"] - 0.05) < 1e-6,
       f"目标总仓位 {plan['target_total']}（期望 0.05）")
    ok(plan.get("trial") is True, "标记为试错链")
    stages = " ".join(c["stage"] for c in plan["chain"])
    ok("退潮试错" in stages, f"链条里有退潮试错：{stages}")

    print("\n[4] 把试错仓调到 0 → 回到无条件清仓")
    cfg0 = sc.load_config()
    cfg0["M3_sentiment"]["ebb_trial_weight"] = 0
    cfg0["M5_position"]["ebb_trial_weight"] = 0
    s0 = sent("ebb", trial_only=False, can_trade=False)
    plan0 = trading.position_plan(cfg0, [{"code": "sh600000"}], s0, vol(), {})
    ok(plan0["target_total"] == 0.0, "目标仓位 0")
    sig0 = trading.buy_signals({"code": "sh600000", "zt_boards": 1}, BARS,
                               s0, vol(), CFG)
    ok(sig0[0].get("blocked") is True, "首板也不给开")
    ok("禁止开仓" in sig0[0]["name"], "回到「禁止开仓」")

    print("\n[5] 极度缩量优先：缩量时试错也停")
    sig = trading.buy_signals({"code": "sh600000", "zt_boards": 1}, BARS,
                              s, vol("dry"), CFG)
    ok(sig[0].get("blocked") is True, "缩量下不给试错")
    ok(sig[0]["name"] == "禁止开仓", f"走的是缩量那条：{sig[0]['name']}")

    print("\n[6] 非退潮期不受影响")
    s2 = sent("ferment", trial_only=False, can_trade=True)
    sig = trading.buy_signals({"code": "sh600000", "zt_boards": 1}, BARS,
                              s2, vol(), CFG)
    ok(all(not x.get("trial") for x in sig), "正常期不出现试错标记")
    plan2 = trading.position_plan(CFG, [{"code": "sh600000"}], s2, vol(), {})
    ok(plan2["target_total"] > 0.05, f"正常期仓位正常：{plan2['target_total']}")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
