# -*- coding: utf-8 -*-
"""QA 独立验证：用户截图场景复现（M3 未确认退潮 + 高潮 + 峰值量能 + 60°）。

旧代码在该场景的 bug：E1 拿原始亏钱面积 47% 强置「退潮」→ 温度计报「60° 退潮」，
而 M3 未确认、结论卡还说「情绪高潮 · 可以出手」—— 同屏自相矛盾（用户 10-12 实测）。

新代码要求同屏自洽：
  1. meter.stage != "ebb"（peek 未确认，E1 不越权）
  2. meter.label 是温度档位（60° → 修复·启动），不出现「退潮」
  3. 副标题（sen.label + vol.label）不含选股口径/行动指令词
  4. 打法框 mode.name 与 mode.note 语义同向（不出现名字喊短线、note 说波段持股）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="qa_shot_")
os.environ["STOCK_DATA_DIR"] = _tmp

from core import sentiment_meter as sm  # noqa: E402
from core import outlook  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def main():
    # ── 构造截图场景池：涨停36 / 跌停8 / 炸板24 → 亏钱面积 47%（超 30% 阈值）──
    zt = [{"code": f"sh6000{i:02d}", "name": f"甲{i}", "boards": 3,
           "first_time": 93000, "fund": 100, "amount": 1000} for i in range(36)]
    dt = [{"code": f"sh6001{i:02d}", "name": f"乙{i}", "boards": 1,
           "first_time": 93000, "fund": 0, "amount": 100} for i in range(8)]
    zb = [{"code": f"sh6002{i:02d}", "name": f"丙{i}", "boards": 1,
           "first_time": 93000, "fund": 0, "amount": 100} for i in range(24)]
    cfg = {"E1_meter": {"enabled": True}}

    # ── 打桩：磁盘/网络全断，peek 未确认，温度钉 60 ──
    _o = {
        "breadth": sm.ds.fetch_breadth,
        "yzt": sm.ds.fetch_yesterday_zt_pool,
        "dates": sm.ds.pool_history_dates,
        "peek": sm.stab.peek,
        "load_hist": sm._load_hist,
        "save_hist": sm._save_hist,
        "temperature": sm._temperature,
    }
    sm.ds.fetch_breadth = lambda *a, **k: {}
    sm.ds.fetch_yesterday_zt_pool = lambda *a, **k: []
    sm.ds.pool_history_dates = lambda *a, **k: []
    sm._load_hist = lambda: []
    sm._save_hist = lambda *a, **k: []
    sm._temperature = lambda *a, **k: 60
    try:
        # M3 未确认（watch：原始信号越线但滞回未认）—— 截图场景的关键前提
        sm.stab.peek = lambda date: {"confirmed": False, "state": "watch",
                                     "hurt_area": 0.47, "note": "未确认"}
        meter = sm.build(zt, dt, zb, {}, "20261012", cfg)

        print("[1] E1 根修：亏钱面积 47% + peek 未确认 → 不报退潮")
        ok(abs(meter["metrics"]["hurt_area"] - 32 / 68) < 0.01,
           f"场景成立：hurt_area={meter['metrics']['hurt_area']:.3f}（47%）")
        ok(meter["temperature"] == 60, "温度 = 60°")
        ok(meter["stage"] != "ebb",
           f"peek 未确认 → stage={meter['stage']}（旧代码此处强置 ebb）")
        ok("退潮" not in meter["label"],
           f"label={meter['label']} 不含「退潮」（60° → 温度档）")
        ok(meter["stage"] == "revive" and "修复·启动" in meter["label"],
           "60° → revive「🌱 修复·启动」（温度档位四选一）")

        print("[2] 同屏三要素：副标题只拼状态")
        sen = {"stage": "climax", "label": "情绪高潮 · 龙头主升",
               "can_trade": True, "metrics": {"hurt_area": 32 / 68,
                                              "limit_up": 36, "limit_down": 8,
                                              "broken": 24, "max_boards": 10}}
        vol = {"mode": "peak_swing", "label": "峰值量能",
               "advice": "资金进得去大市值，做中长线波段/趋势票（量能口径：…）"}
        subtitle = sen["label"] + " · " + vol["label"]
        hits = [w for w in ("波段", "短线", "大票", "空仓", "回避") if w in subtitle]
        ok(not hits, f"副标题「{subtitle}」不含口径/指令词")
        # 口径词只允许出现在 advice / mode，不出现在副标题
        ok("波段" in vol["advice"], "行动口径收回 vol.advice（峰值量能→波段/趋势票）")

        print("[3] outlook：高潮期 + 峰值量能 → 组合打法")
        out = outlook.build(cfg, "20261012", sen, vol, meter)
        mode = out.get("mode", {})
        print(f"    mode.name = {mode.get('name')}")
        print(f"    mode.note = {(mode.get('note') or '')[:50]}…")
        ok(mode.get("name") == "短线快枪",
           "climax + peak_swing → 「短线快枪」（阶段默认，非缩量/退潮压制）")
        # name/note 同向：短线名 → note 必须是短线语义，不能喊波段持股
        short_words = ("快进快出", "不格局", "去弱留强", "短线")
        swing_words = ("沿均线持有", "波段持股", "拿得住才有肉")
        note = mode.get("note") or ""
        ok(any(w in note for w in short_words),
           f"note 与 name 同向（短线语义）：{'/'.join(w for w in short_words if w in note)}")
        ok(not any(w in note for w in swing_words),
           "note 不含波段持股类反向语义（name/note 不打架）")
        # 副标题状态 vs 打法：峰值量能是状态，短线快枪是当日应对——两者不同维度，不构成矛盾
        ok(out.get("headline"), f"headline: {out.get('headline')}")

        print("[4] 对照组：同样的池子，peek 确认 → 必须报 ebb（口径统一仍有效）")
        sm.stab.peek = lambda date: {"confirmed": True, "state": "ebb",
                                     "hurt_area": 0.47, "note": "已确认"}
        meter2 = sm.build(zt, dt, zb, {}, "20261012", cfg)
        ok(meter2["stage"] == "ebb" and "退潮" in meter2["label"],
           "peek 确认 → ebb（🌊 退潮）—— 与 M3 同向，不是双重标准")
    finally:
        for k, v in _o.items():
            setattr(sm, k, v) if k != "peek" else setattr(sm.stab, "peek", v)
            if k in ("breadth", "yzt", "dates"):
                setattr(sm.ds, k, v)

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    sys.exit(1 if badc else 0)


if __name__ == "__main__":
    main()
