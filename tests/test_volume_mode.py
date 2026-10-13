# -*- coding: utf-8 -*-
"""M2 量能模式判定的回归护栏 —— 「放量后的回落被判成峰值量能」。

用户实测（2026-10-12）：两市成交 15378 亿被判为「峰值量能」，
提示"资金进得去大市值，做中长线波段/趋势票"。
核对数据：近三日 16821 → 19006 → 15378，15378 是**近 3 日最低**、
较 10-09 的高点回落 19%；同日沪指量能处于近 29 日的 **27.6% 分位**
（底部三分之一）。标签与自己的数据直接打架。

根因两条：

  ① 判定是**纯绝对阈值**（amt ≥ 15000 → 峰值），完全不看近期分位与环比。
     「峰值」是相对概念，绝对阈值只能回答"钱够不够撬动大票"，
     回答不了"这点钱相对最近是多是少"。
  ② 分档阶梯**倒挂**：老默认 comfort=5000 反而低于 shrink=12000，
     而分支按 comfort → shrink 顺序判断，于是 [5000, 15000) 全被
     "量能舒适"吃掉、`shrink_short` 成了**永不触发的死分支**
     （实测 8000 亿被报"量能舒适·游资具备轮动能力"）。

修法：
  · 绝对档位（peak ≥ comfort ≥ shrink，阶梯单调）决定上限；
  · 相对层（近 N 日分位 rel + 相对中位数倍数 trend）只在"相对明显偏低"
    时**降档**，绝不升档（8000 亿分位再高也不该叫峰值）；
  · 两条降档判据独立判断，允许 峰值 → 舒适 → 缩量 连续降两档。

另附 M4 图形评分「回撤单位」的回归：drawdown_from_high 返回**小数**，
原来拿它和 10/45/60 这些**百分数**比 —— 加分项永不触发，且每只票都被
误贴"处于60日新高、位置偏高"。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine          # noqa: E402
from core import indicators as ind   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


CFG = {"M2_volume": {
    "enabled": True,
    "peak_threshold": 15000, "comfort_threshold": 12000, "shrink_threshold": 8000,
    "shrink_red_flag": False,          # 隔离：本测试只看分档本身
    "lookback": 20, "peak_pct": 0.80, "peak_trend_min": 1.00, "shrink_pct": 0.45,
}}
SESSION_CLOSED = {"phase": "after_close", "tmin": 240}


def hist_series(prev: list[float], today: float) -> list[dict]:
    """构造沪指日K序列：prev 为历史各日量，最后一根是今日。"""
    vols = list(prev) + [today]
    return [{"date": f"2026-09-{i + 1:02d}", "open": 1.0, "close": 1.0,
             "high": 1.0, "low": 1.0, "volume": float(v)}
            for i, v in enumerate(vols)]


# 历史：12 天 100 + 7 天 90 → 中位数 100
HIST_FLAT = [100.0] * 12 + [90.0] * 7


def vol(amount: float, hist, session=None, cfg=None):
    return engine.evaluate_volume(cfg or CFG, amount, hist,
                                  session=session or SESSION_CLOSED)


def main():
    print("[1] 用户实测场景：15378 亿（近3日最低、分位 37%）→ 不得判为峰值")
    # 今日 95，历史中位数 100 → rel = 7/19 = 0.368、trend = 0.95
    h = hist_series(HIST_FLAT, 95.0)
    r = vol(15378.3, h)
    ok(r["mode"] != "peak_swing", f"不是峰值量能（实际 {r['label']}）")
    ok(r["mode"] == "shrink_short", f"落到缩量存量（实际 {r['mode']}）")
    ok(r["short_only"] is True, "缩量存量 → short_only（切短线应对打法）")
    ok("不构成峰值量能" in r["advice"], "文案说明了为什么不算峰值")
    ok(r["rel"] is not None and r["rel"] <= 0.45, f"相对分位被算出（{r['rel']}）")
    ok(r["trend"] is not None and r["trend"] < 1.0, f"环比低于中位数（{r['trend']}）")

    print("\n[2] 真放量日（分位 100%、环比 1.30）→ 峰值量能")
    h2 = hist_series(HIST_FLAT, 130.0)
    r2 = vol(19006.0, h2)
    ok(r2["mode"] == "peak_swing", f"峰值量能（实际 {r2['mode']}）")
    ok(r2["short_only"] is False, "峰值量能 → 做中长线波段")

    print("\n[3] 绝对额够、相对只是略低（分位 53%）→ 降一档到量能舒适")
    # 历史 80→170 均匀分布，今日 128 → rel = 10/19 ≈ 0.53、trend ≈ 1.02
    HIST_SPREAD = [80.0 + 5 * i for i in range(19)]
    h3 = hist_series(HIST_SPREAD, 128.0)
    r3 = vol(16000.0, h3)
    ok(r3["mode"] != "peak_swing", f"不算峰值（实际 {r3['label']}）")
    ok(r3["mode"] == "comfort_short", f"降到量能舒适（实际 {r3['mode']}）")
    ok(r3["short_only"] is False, "仍属舒适区，不强制切短线")

    print("\n[4] 四个档位全部可达 —— shrink_short 不再是死分支")
    seen = {}
    for amt, today in ((20000.0, 130.0), (13000.0, 100.0), (9000.0, 95.0), (5000.0, 60.0)):
        seen[vol(amt, hist_series(HIST_FLAT, today))["mode"]] = amt
    for m in ("peak_swing", "comfort_short", "shrink_short", "dry"):
        ok(m in seen, f"{m} 可达（{seen.get(m, '—')} 亿）")

    print("\n[5] 阶梯倒挂（老存档 comfort=5000 < shrink=12000）就地纠正")
    bad_cfg = {"M2_volume": dict(CFG["M2_volume"],
                                 comfort_threshold=5000, shrink_threshold=12000)}
    rb = vol(9000.0, hist_series(HIST_FLAT, 95.0), cfg=bad_cfg)
    ok(rb.get("ladder_fixed") is True, "标记 ladder_fixed")
    ok(rb["mode"] == "shrink_short", f"9000 亿仍正确落到缩量存量（实际 {rb['mode']}）")
    rc = vol(16000.0, hist_series(HIST_FLAT, 130.0), cfg=bad_cfg)
    ok(rc["mode"] == "peak_swing", f"16000 亿真放量仍是峰值（实际 {rc['mode']}）")

    print("\n[6] 相对口径缺数据时退回纯绝对阈值（不崩、不误降档）")
    for hx in ([], [{"date": "d", "open": 1, "close": 1, "high": 1, "low": 1}]):
        rr = vol(16000.0, hx)
        ok(rr["mode"] == "peak_swing" and rr["rel"] is None,
           f"hist 不足 → 按绝对阈值判峰值（{rr['mode']}）")

    print("\n[7] 盘中：折算后再比，早盘不因只成交了半天就被判缩量")
    # 10:00 累计 5000 亿（frac≈0.18）→ 折算约 27778 亿；沪指今日量也按同口径折算
    h4 = hist_series(HIST_FLAT, 100.0 * 0.18)
    ri = engine.evaluate_volume(CFG, 5000.0 * 1e8, h4,
                                session={"phase": "morning", "tmin": 30})
    ok(ri["intraday"] is True and ri["amount"] is None,
       "盘中 amount 仍为 None（不给全天外推数字）")
    ok(ri["mode"] == "peak_swing", f"折算后按峰值暂判（实际 {ri['mode']}）")
    ok(ri["rel"] is not None and ri["rel"] >= 0.8,
       f"盘中相对分位同样按折算口径（{ri['rel']}）")

    print("\n[8] 进度条标尺由后端按配置算（前端不再写死 amount/180）")
    rg = vol(15378.3, hist_series(HIST_FLAT, 95.0))
    ok(isinstance(rg.get("gauge"), int) and 0 <= rg["gauge"] <= 100,
       f"收盘后给出 0-100 的 gauge（{rg['gauge']}）")
    ok(rg["gauge"] <= 90, "15378 亿不该画到接近满格（原来写死 18000 亿=满格 → 85%）")

    print("\n[9] M4 图形评分：回撤单位对齐（小数 vs 百分数）")
    a_cfg = {"price_min": 5.0, "price_max": 10.0, "mv_max": 100.0,
             "mv_mid_threshold": 500.0, "recent_limitup_days": 20,
             "recent_limitup_min": 1, "history_two_board": True}

    def bars_of(prices):
        return [{"open": p * 0.99, "close": p, "high": p * 1.01, "low": p * 0.98,
                 "volume": 1000.0} for p in prices]

    deep = bars_of([100] * 5 + [95, 90, 85, 80, 75, 70, 65, 60] + [60] * 12)
    g, det, reasons, warns = engine._graph_score(deep, "sh600000", a_cfg)
    ok(any("洗盘空间充分" in x for x in reasons),
       "回撤 40% → 拿到洗盘空间充分加分（原来永不触发）")
    ok(not any("60日新高" in x for x in warns),
       "回撤 40% → 不再被误贴处于60日新高警示")

    newhigh = bars_of([50 + i for i in range(25)])
    g2, det2, reasons2, warns2 = engine._graph_score(newhigh, "sh600000", a_cfg)
    ok(any("60日新高" in x for x in warns2), "一路新高 → 才提示位置偏高")
    ok(det2["drawdown60"] <= 0.05, f"一路新高的回撤是小数值（{det2['drawdown60']}）")

    print("\n[10] base_pattern 任何长度都含 deep_base 键（/api/stock 次新股不崩）")
    for n in (0, 1, 3, 10, 19, 25):
        bp = ind.base_pattern(bars_of([10 + i * 0.1 for i in range(n)]), 20)
        ok("deep_base" in bp and "near_base" in bp and "pos" in bp,
           f"{n} 根 K 线 → 四个键齐全")
    try:
        engine._graph_score(bars_of([10, 10.2, 10.1]), "sh600000", a_cfg)
        ok(True, "3 根 K 线走完 _graph_score 不抛异常")
    except Exception as e:                       # noqa: BLE001
        ok(False, f"3 根 K 线抛异常：{type(e).__name__}: {e}")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
