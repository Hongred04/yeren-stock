# -*- coding: utf-8 -*-
"""QA 独立验证脚本（严过关）：不轻信工程师自测，用 monkeypatch 实测根修。

验证点：
  1. _stage_of 只剩温度分档，无 ebb 出口（边界 41/42/60/61/62/80）
  2. peek 确认 → ebb；peek 未确认 + 亏钱面积 67% → 非 ebb（根修核心）
  3. drop_alert 只作信号，不升级阶段；hurt_area 仍在 metrics
  4. _stage_of 生产调用点唯一且用新签名（二参数）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="qa_meter_")
os.environ["STOCK_DATA_DIR"] = _tmp

from core import sentiment_meter as sm  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# 最小池：涨停1/跌停1/炸板1 → 亏钱面积 2/3 = 66.7%
ZT = [{"code": "sh600001", "name": "甲", "boards": 3, "first_time": 93000,
       "fund": 100, "amount": 1000}]
DT = [{"code": "sh600002", "name": "乙", "boards": 1, "first_time": 93000,
       "fund": 0, "amount": 100}]
ZB = [{"code": "sh600003", "name": "丙", "boards": 1, "first_time": 93000,
       "fund": 0, "amount": 100}]
CFG = {"E1_meter": {"enabled": True}}


def main():
    print("[1] _stage_of 边界（阈值 42/62/80，无 ebb 出口）")
    ok(sm._stage_of(60, None) == "revive", "60° → revive（用户截图场景）")
    ok(sm._stage_of(62, 80) == "ferment", "62°（昨 80°，骤降 18°）→ ferment，不报 ebb")
    ok(sm._stage_of(42, None) == "revive", "42° 边界 → revive")
    ok(sm._stage_of(41, None) == "ice", "41° 边界 → ice（低于 42 阈值）")
    ok(sm._stage_of(61, None) == "revive", "61° 边界 → revive（低于 62 阈值）")
    ok(sm._stage_of(80, None) == "climax", "80° 边界 → climax")
    ok(sm._stage_of(79, None) == "ferment", "79° → ferment")
    # 关键：无论 prev_temp 多极端，都不许出 ebb
    ok(sm._stage_of(62, 95) != "ebb", "prev 95° → 今 62° 也不出 ebb")
    ok(sm._stage_of(30, 60) != "ebb", "prev 60° → 今 30° 也不出 ebb")
    # 签名核查：二参数，prev_temp 有默认值
    import inspect
    sig = inspect.signature(sm._stage_of)
    params = list(sig.parameters)
    ok(params == ["temp", "prev_temp"], f"签名 {params}（旧版曾收 ebb 三参）")
    ok(sig.parameters["prev_temp"].default is None, "prev_temp 默认 None")

    print("[2] build()：peek 未确认 + 亏钱面积 67% → 非 ebb（根修核心）")
    _o = {
        "breadth": sm.ds.fetch_breadth,
        "yzt": sm.ds.fetch_yesterday_zt_pool,
        "dates": sm.ds.pool_history_dates,
        "peek": sm.stab.peek,
        "load_hist": sm._load_hist,
        "save_hist": sm._save_hist,
    }
    sm.ds.fetch_breadth = lambda *a, **k: {}
    sm.ds.fetch_yesterday_zt_pool = lambda *a, **k: []
    sm.ds.pool_history_dates = lambda *a, **k: []
    sm._load_hist = lambda: []
    sm._save_hist = lambda *a, **k: []
    try:
        sm.stab.peek = lambda date: {"confirmed": False, "state": "calm",
                                     "hurt_area": 0.0, "note": ""}
        m = sm.build(ZT, DT, ZB, {}, "20261012", CFG)
        ok(abs(m["metrics"]["hurt_area"] - 2 / 3) < 0.01,
           f"hurt_area={m['metrics']['hurt_area']:.3f}（仍在 metrics）")
        ok(m["stage"] != "ebb" and "退潮" not in m["label"],
           f"peek 未确认 → stage={m['stage']} label={m['label']}")
        ok("drop_alert" in m["metrics"], "metrics 含 drop_alert 字段")
        ok(m["metrics"]["drop_alert"] is False, "无历史温度 → drop_alert=False")

        print("[3] 骤降 18° 只作 drop_alert，不升级阶段")
        sm._load_hist = lambda: [{"date": "20260101", "temperature": 80,
                                  "limit_up": 90, "limit_down": 2, "broken": 5,
                                  "max_boards": 7, "promote_rate": 0.3,
                                  "broken_ratio": 0.05}]
        m2 = sm.build(ZT, DT, ZB, {}, "20260102", CFG)
        ok(m2["metrics"]["drop_alert"] is True,
           f"昨 80° → 今 {m2['temperature']}° drop_alert=True（信号保留）")
        ok(m2["stage"] != "ebb" and "退潮" not in m2["label"],
           f"骤降当日 stage={m2['stage']}（{m2['label']}）")
        ok(m2["metrics"]["prev_temp"] == 80, "prev_temp=80 进 metrics")
        sm._load_hist = lambda: []

        print("[4] peek 确认退潮 → E1 才报 ebb（统一口径有效）")
        sm.stab.peek = lambda date: {"confirmed": True, "state": "ebb",
                                     "hurt_area": 0.67, "note": "已确认"}
        m3 = sm.build(ZT, DT, ZB, {}, "20261012", CFG)
        ok(m3["stage"] == "ebb" and "退潮" in m3["label"],
           f"peek 确认 → stage=ebb（{m3['label']}）")
    finally:
        for k, v in _o.items():
            if k == "breadth":
                sm.ds.fetch_breadth = v
            elif k == "yzt":
                sm.ds.fetch_yesterday_zt_pool = v
            elif k == "dates":
                sm.ds.pool_history_dates = v
            elif k == "peek":
                sm.stab.peek = v
            elif k == "load_hist":
                sm._load_hist = v
            elif k == "save_hist":
                sm._save_hist = v

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    sys.exit(1 if badc else 0)


if __name__ == "__main__":
    main()
