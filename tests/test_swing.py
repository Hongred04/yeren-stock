# -*- coding: utf-8 -*-
"""野人《波段体系》判定测试。

波段是这套系统"平常打法"的底座（原话：平常中长线波段为主、缩量时才切短线），
但 v1.24.1 之前的买卖点全是短线口径（B1 首板 / B2 回踩 / S1 退潮…），
第一入场点、第二入场点、第二波接入、高位滞涨离场**一个都没有**。
本测试钉死这四类判定，以及周月线量能、洗盘/破位区分。

构造手法：先造基础序列 → 读出真实均线值 → 再追加"回踩到该均线"的那一根K线。
比手算均线稳，失败时也能一眼看出是哪个前置条件没满足。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import swing   # noqa: E402

C = dict(swing.SWING_DEFAULTS)

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def ramp(a: float, b: float, n: int) -> list[float]:
    if n <= 1:
        return [b]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def mk(closes: list[float], vols: list[float] | None = None) -> list[dict]:
    """按交易日（跳过周末）生成日K，open=close、振幅 ±1%。"""
    bars, d = [], datetime(2026, 1, 5)
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        v = vols[i] if vols else 1000.0
        bars.append({"date": d.strftime("%Y-%m-%d"), "open": c, "high": c * 1.01,
                     "low": c * 0.99, "close": c, "volume": v})
        d += timedelta(days=1)
    return bars


def append(bars: list[dict], close: float, low: float, vol: float = 1000.0) -> list[dict]:
    d = datetime.strptime(bars[-1]["date"], "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return bars + [{"date": d.strftime("%Y-%m-%d"), "open": close * 1.002,
                    "high": max(close, low) * 1.012, "low": low,
                    "close": close, "volume": vol}]


def main():
    print("[1] 均线状态：5日线上穿所有均线 + 20日线拐头向上 + 粘合度")
    bars = mk(ramp(100, 80, 30) + ramp(80, 92, 12))
    ms = swing.ma_snapshot(bars, C)
    ok(ms["ok"], "均线可算")
    ok(ms["above_all"], "下跌后转强 → 5日线上穿所有均线")
    ok(ms["turning_up"], "20日线拐头向上")
    ok(ms["converge_pct"] is not None and ms["converged"] is False,
       f"刚转强时均线还没粘合（{ms['converge_pct']*100:.1f}%）")
    # 粘合度：长期横盘 → 均线粘合
    flat = swing.ma_snapshot(mk([10.0] * 40), C)
    ok(flat["converged"] is True,
       f"长期横盘 → 均线高度粘合（{flat['converge_pct']*100:.1f}%）")
    ok(swing.ma_snapshot(mk([10.0] * 10), C)["ok"] is False, "K线不足 → 不硬判")

    print("\n[2] 第一入场点：转强 + 回踩 10 日线")
    m10 = ms["ma"][10]
    w1b = append(bars, close=m10 * 1.004, low=m10 * 0.998)   # 回踩到 10 日线
    ms2 = swing.ma_snapshot(w1b, C)
    ok(ms2["above_all"] and ms2["turning_up"], "前置仍成立（回踩没破坏结构）")
    w1 = swing.first_entry(w1b, C)
    ok(w1 is not None and w1["type"] == "W1",
       f"第一入场点触发（回踩{w1['line'] if w1 else '-'}）")
    # 反例：没有回踩到均线（一路高走）就不该给这个入场点
    far = append(bars, close=bars[-1]["close"] * 1.05,
                 low=bars[-1]["close"] * 1.03)
    ok(swing.first_entry(far, C) is None, "没回踩到均线 → 不给第一入场点")

    print("\n[3] 第二入场点：从 5 日线打到 20 日线附近")
    m20 = ms["ma"][20]
    w2b = append(bars, close=m20 * 1.006, low=m20 * 0.997)
    w2 = swing.second_entry(w2b, C)
    ok(w2 is not None and w2["type"] == "W2", "第二入场点触发")
    ok(w2["from_ma5"] is True, "识别出「从 5 日线打下来」")
    shrink = append(bars, close=m20 * 1.006, low=m20 * 0.997, vol=500.0)
    ok(swing.second_entry(shrink, C)["shrink"] is True,
       "弱庄形态：回踩 20 日线且缩量（原话「一定缩量回踩」）")
    ok(swing.second_entry(bars, C) is None, "没打到 20 日线 → 不给第二入场点")

    print("\n[4] 第二波接入：第一波涨够 30% → 回调 → 5日线重新拐头 + 均线粘合")
    base2 = mk(ramp(10, 10.2, 20) + ramp(10.2, 14.5, 20))     # 第一波 +42%
    w3 = swing.second_wave(base2, C)
    ok(w3 is None, "第一波刚见顶、还没回调 → 不给第二波接入")
    pull = mk(ramp(10, 10.2, 20) + ramp(10.2, 14.5, 20) + ramp(14.5, 12.6, 10)
              + ramp(12.6, 12.8, 6))
    w3b = swing.second_wave(pull, C)
    ok(w3b is not None, "涨够 30% + 回调到位 → 进入第二波观察")
    if w3b:
        ok(w3b["gain"] >= 0.30, f"第一波涨幅 {w3b['gain']*100:.0f}% ≥ 30%")
        ok(0.05 <= w3b["pullback"] <= 0.35, f"回调 {w3b['pullback']*100:.0f}% 在合理区间")
        ok(w3b["ready"] is (w3b["turn5"] and w3b["converged"]),
           "ready = 5日线重新拐头 且 均线粘合（两个条件缺一不可）")
    # 涨幅不够 30% → 谈不上第二波
    small = mk(ramp(10, 11, 25) + ramp(11, 10.2, 8))
    ok(swing.second_wave(small, C) is None, "第一波只涨 10% → 不给第二波接入")

    print("\n[5] 离场点：高位放量大涨滞涨（有人在出货）")
    hi_bars = mk(ramp(10, 16, 40))
    stall = append(hi_bars, close=hi_bars[-1]["close"] * 1.008,
                   low=hi_bars[-1]["close"] * 0.99, vol=3200.0)
    ex = swing.exit_point(stall, C)
    ok(ex is not None and ex["type"] == "WS1", "高位 + 放量 + 只涨 0.8% → 第一离场点")
    ok(ex["vol_ratio"] >= 1.5, f"放量确认（量比 {ex['vol_ratio']}）")
    big = append(hi_bars, close=hi_bars[-1]["close"] * 1.06,
                 low=hi_bars[-1]["close"] * 0.99, vol=3200.0)
    ok(swing.exit_point(big, C) is None, "放量但真大涨（+6%）→ 不是滞涨，不误报")
    # 反例：放量滞涨但处在**低位**（大跌之后的反弹）→ 不是出货，不报离场
    lowpos = append(mk(ramp(20, 10, 40)), close=10.0 * 1.008,
                    low=10.0 * 0.99, vol=3200.0)
    lp = swing.exit_point(lowpos, C)
    ok(lp is None, "放量滞涨但在低位（大跌后反弹）→ 不报离场")

    print("\n[6] 洗盘 vs 破位：缩量回踩不走，放量破位才走")
    wsh = append(bars, close=m20 * 1.004, low=m20 * 0.997, vol=500.0)
    ok(swing.washout_or_break(wsh, C)["state"] == "washout", "缩量回踩 → 洗盘（不动手）")
    brk = append(bars, close=m20 * 0.94, low=m20 * 0.93, vol=3000.0)
    ok(swing.washout_or_break(brk, C)["state"] == "break", "放量跌破 20 日线 → 破位（走）")

    print("\n[7] 周线量能（原话：看周线和月线，这句话值一万金）")
    vols = []
    for w in range(6):
        vols += [1000.0] * 5 if w < 5 else [1300.0] * 5      # 最后一周温和放大
    wk = swing.volume_health(mk([10.0] * 30, vols), weeks=8)
    ok(wk["ok"] is True, "周线样本够")
    ok("温和放大" in wk["state"], f"最后一周量能温和放大：{wk['state']}")
    ok(swing.volume_health(mk([10.0] * 8), weeks=8)["ok"] is False,
       "样本不足 → 明说不足，不硬判")

    print("\n[8] 综合判定：阶段 + 纪律清单")
    ev = swing.evaluate(stall, "sh600000", {"M11_swing": C})
    ok(ev["ok"] is True, "综合判定可用")
    ok(ev["stage"] == "stall" and ev["exit"], f"高位滞涨 → 阶段={ev['stage_cn']}")
    ok(len(ev["checklist"]) >= 6, f"心法清单 {len(ev['checklist'])} 条")
    ok("锁仓" in "".join(c["t"] for c in ev["checklist"]),
       "含「锁仓才可重仓」（波段与短线的根本区别）")
    ev2 = swing.evaluate(w1b, "sh600000", {"M11_swing": C})
    ok(ev2["stage"] == "first" and ev2["entry"], f"回踩10日线 → 阶段={ev2['stage_cn']}")
    ok(swing.evaluate([], "sh600000", {"M11_swing": C})["ok"] is False,
       "K线不足 → 不硬判")
    ok(swing.evaluate(bars, "sh600000", {"M11_swing": {"enabled": False}})["ok"] is False,
       "模块关闭 → 不判")

    print("\n[9] 接进买卖点体系：W 信号 + 按打法排序 + WS1 离场")
    from core import strategy_config as sc
    from core import trading
    cfg_full = sc.load_config()
    ok("M11_swing" in cfg_full, "M11_swing 已进策略配置（可调参/可关）")

    def mk_stock(style_key, swing_res):
        return {"code": "sh600000", "name": "测试", "sector": "银行", "price": 10.0,
                "pct": 10.0, "zt_boards": 1, "turnover": 3.0,
                "fund_style": {"type": "unknown"},
                "style_fit": {"key": style_key},
                "swing": swing_res}

    sent = {"stage": "climax", "can_trade": True, "label": "高潮", "signals": []}
    vol = {"mode": "normal", "red_flag": False}
    # ★ 节点门禁（v1.25.3）：波段买点(W1/W2/W3)只在「起步/扩散 + 非缩量」节点才发。
    #   排序契约测试用「做长友好」节点，否则 W 会被门禁撤下、根本测不到排序。
    sent_sw = {"stage": "follow", "can_trade": True, "label": "扩散", "signals": []}
    vol_sw = {"mode": "peak_swing", "red_flag": False}

    # 同一只票同时有 B1（首板，短线）与 W1（回踩10日线，波段）→ 看排序
    sw_w1 = swing.evaluate(w1b, "sh600000", cfg_full)
    swing_sig = trading.buy_signals(mk_stock("swing", sw_w1), w1b, sent_sw, vol_sw, cfg_full)
    t_sw = [s["type"] for s in swing_sig]
    ok("W1" in t_sw and "B1" in t_sw, f"波段票同时拿到 B1 与 W1：{t_sw}")
    ok(t_sw[0] == "W1", f"波段票 → W 排前面（{t_sw}）")
    ok(t_sw[-1] == "HOLD", f"禁买提示永远排最后（{t_sw}）")
    short_sig = trading.buy_signals(mk_stock("short", sw_w1), w1b, sent_sw, vol_sw, cfg_full)
    t_sh = [s["type"] for s in short_sig]
    ok(t_sh[0] == "B1" and t_sh.index("W1") > 0,
       f"短线票 → B 排前面（{t_sh}）")
    ok(all(s.get("style") in (None, "swing", "short") for s in swing_sig),
       "买点带 style 标记（前端据此分组）")

    # ★ 节点门禁：非做长节点（高潮/缩量）→ 撤下 W、改发「当前节点不做长」
    gated = trading.buy_signals(mk_stock("swing", sw_w1), w1b, sent, vol, cfg_full)
    t_g = [s["type"] for s in gated]
    ok("W1" not in t_g, f"高潮节点 → 不发波段买点（{t_g}）")
    ok(any(s.get("name") == "当前节点不做长" for s in gated),
       f"高潮节点 → 改发「当前节点不做长」提示（{t_g}）")

    # 高位放量滞涨 → 波段离场点进卖点
    sw_ex = swing.evaluate(stall, "sh600000", cfg_full)
    sells = trading.sell_signals(mk_stock("swing", sw_ex), stall, sent, vol, cfg_full)
    t_sell = [s["type"] for s in sells]
    ok("WS1" in t_sell, f"高位放量滞涨 → 卖点含 WS1：{t_sell}")
    ok(t_sell[0] == "WS1", f"波段票的卖点里 WS1 排前面（{t_sell}）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
