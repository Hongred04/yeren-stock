# -*- coding: utf-8 -*-
"""M10 主导资金分类与离场策略。

同一个趋势，主导资金不同，离场点完全不同：
  机构票（中军）：小阳线爬五日线、进二退一，慢牛走半个月~一个月涨 50%。
      离场信号 = 加速 —— 机构票突然拉涨停（哪怕只有一个）就是趋势末端的
      出货冲顶，立刻开始减仓；把「加速冲顶」当机构派发的最后一段。
  游资票（龙头）：连板/断板节奏，两板一断、两板一断，一周五个板。
      离场跟着断板节奏走，节奏被破坏就撤。
  庄股：不参与。断崖式下跌 + 做庄失败打回原点，盈亏比极差。

速度本身就是分类依据：同样是五个板，游资一周走完（吃情绪溢价），
机构一个月走完（吃波段收益）—— 持仓周期和卖出策略按「谁在主导」来定。
"""
from __future__ import annotations

from . import indicators as ind

TYPE_LABELS = {
    "institutional": "机构(中军)",
    "hotmoney": "游资(龙头)",
    "zhuang": "庄股·回避",
    "unknown": "未分类",
}

HOLD_NOTES = {
    "institutional": "沿5日线持有，进二退一是正常节奏；突然拉涨停（加速）就是减仓信号，不恋战",
    "hotmoney": "跟着断板节奏走：两板一断、两板一断，节奏没破就拿着；断板不回封就撤",
    "zhuang": "不参与：断崖式下跌 + 做庄失败打回原点，盈亏比极差",
    "unknown": "主导资金特征不明显，按 M4 审美与情绪周期常规执行",
}


def _pct_series(bars: list[dict]) -> list[float]:
    out = [0.0]
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        out.append((bars[i]["close"] - pc) / pc * 100 if pc > 0 else 0.0)
    return out


def _above_ma5_days(closes: list[float], lookback: int = 20) -> float:
    """近 lookback 日收盘站在5日线上的占比。"""
    n = 0
    total = 0
    for i in range(len(closes) - lookback, len(closes)):
        if i < 4:
            continue
        total += 1
        if closes[i] > sum(closes[i - 4:i + 1]) / 5:
            n += 1
    return n / total if total else 0.0


def classify(bars: list[dict], code: str, float_mv_yi: float = 0.0,
             cfg: dict | None = None) -> dict:
    """按主导资金把个股趋势分类，并给出该类型的离场口径。

    返回 {type,label,speed,hold_note,evidence,metrics,accel,broken_board,rhythm_broken}
    """
    m = (cfg or {}).get("M10_fundstyle", {})
    inst_mv_min = float(m.get("inst_mv_min", 80.0))
    inst_max_zt20 = int(m.get("inst_max_zt20", 1))
    inst_max_gain = float(m.get("inst_max_daily_gain", 7.5))
    inst_min_pct20 = float(m.get("inst_min_pct20", 5.0))
    inst_max_pct20 = float(m.get("inst_max_pct20", 60.0))
    hot_zt10_min = int(m.get("hot_zt10_min", 2))
    crash_pct = float(m.get("zhuang_crash_pct", -7.0))
    chop_amp = float(m.get("zhuang_chop_amp", 2.8))

    base = {"type": "unknown", "label": TYPE_LABELS["unknown"],
            "speed": "—", "hold_note": HOLD_NOTES["unknown"],
            "evidence": [], "metrics": {},
            "accel": False, "broken_board": False, "rhythm_broken": False}
    if len(bars) < 25:
        return base

    th = ind.limit_up_threshold(code)
    pcts = _pct_series(bars)
    closes = [b["close"] for b in bars]
    zt20 = [p for p in pcts[-20:] if p >= th]
    zt10 = [p for p in pcts[-10:] if p >= th]
    boards_now = ind.consecutive_boards(bars, code)
    pct20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else \
            (closes[-1] / closes[0] - 1) * 100
    max_gain20 = max(pcts[-20:])
    above5 = _above_ma5_days(closes, 20)

    # ---- 庄股特征：前期无声阴跌/横盘（低波动、无涨停）→ 近10日断崖 ----
    crash_i = next((len(pcts) - 10 + k for k, p in enumerate(pcts[-10:])
                    if p <= crash_pct), None)
    zhuang_hit = False
    if crash_i is not None:
        prior = [abs(p) for p in pcts[max(0, crash_i - 15):crash_i]]
        chop = sum(prior) / len(prior) if prior else 99.0
        no_prior_zt = not any(p >= th for p in pcts[max(0, crash_i - 15):crash_i])
        zhuang_hit = chop <= chop_amp and no_prior_zt
        base["metrics"]["chop"] = round(chop, 2)
        base["metrics"]["crash_day_pct"] = round(pcts[crash_i], 2)

    # ---- 游资特征：连板节奏 ----
    today_zt = pcts[-1] >= th
    last_zt_ago = next((k for k in range(1, 6) if pcts[-1 - k] >= th), None)
    broken = last_zt_ago == 1 and not today_zt          # 昨涨停今未封 = 断板
    rhythm_broken = (last_zt_ago is not None and last_zt_ago >= 2
                     and not today_zt)                   # 断板≥2天不回封 = 节奏破坏
    # 本轮速度：最近一次涨停距今天数 → 该轮用了几天
    run_days = (last_zt_ago or 0) + 1 if (zt10 or today_zt) else 0

    metrics = {
        "pct20": round(pct20, 1), "zt20": len(zt20), "zt10": len(zt10),
        "boards_now": boards_now, "max_gain20": round(max_gain20, 1),
        "above_ma5": round(above5, 2), "float_mv": round(float_mv_yi, 0),
    }

    if zhuang_hit:
        base.update({
            "type": "zhuang", "label": TYPE_LABELS["zhuang"],
            "speed": "断崖风险·不参与",
            "hold_note": HOLD_NOTES["zhuang"],
            "evidence": [f"前期日均波动仅 {base['metrics']['chop']:.1f}% 且无涨停，"
                         f"近10日出现 {base['metrics']['crash_day_pct']:.1f}% 断崖大阴",
                         "典型做庄失败形态：打回原点，盈亏比极差"],
        })
        base["metrics"] = {**base["metrics"], **metrics}
        return base

    if len(zt10) >= hot_zt10_min or boards_now >= 2:
        base.update({
            "type": "hotmoney", "label": TYPE_LABELS["hotmoney"],
            "speed": f"快速·情绪（本轮 {max(boards_now, len(zt10))} 板用了约 {run_days} 天）",
            "hold_note": HOLD_NOTES["hotmoney"],
            "evidence": [f"近10日 {len(zt10)} 次涨停、当前 {boards_now} 连板 —— 连板/断板节奏",
                         "同样是五个板，一周走完=游资吃情绪溢价；持仓按天计"],
            "broken_board": broken, "rhythm_broken": rhythm_broken,
        })
        base["metrics"] = metrics
        return base

    if (float_mv_yi >= inst_mv_min and len(zt20) <= inst_max_zt20
            and inst_min_pct20 <= pct20 <= inst_max_pct20
            and above5 >= 0.55):
        # 慢牛校验用「核心趋势」涨幅：剔除最近2日的可能加速涨停，
        # 否则加速日（+10%）自己会打破单日涨幅上限、漏判类型
        recent_zt = any(p >= th for p in pcts[-2:])
        prior_zt = any(p >= th for p in pcts[-22:-2])
        core = pcts[-22:-2] if recent_zt else pcts[-20:]
        max_gain_core = max((abs(p) for p in core), default=0.0)
        if max_gain_core > inst_max_gain:
            base["metrics"] = metrics
            base["evidence"] = ["趋势波动偏大，主导资金特征不典型"]
            return base
        base.update({
            "type": "institutional", "label": TYPE_LABELS["institutional"],
            "speed": f"慢速·波段（20日 +{pct20:.0f}%，日均 {pct20/20:.1f}%，周期约2~4周）",
            "hold_note": HOLD_NOTES["institutional"],
            "evidence": [f"流通 {float_mv_yi:.0f} 亿、近20日仅 {len(zt20)} 次涨停、"
                         f"核心趋势单日最大波动 {max_gain_core:.1f}% —— 小阳线爬五日线",
                         "进二退一的机构慢牛：一个月走完的五个板，吃的是波段收益",
                         f"近20日 {above5 * 100:.0f}% 时间站上5日线"],
        })
        base["metrics"] = metrics
        # 加速 = 兑现信号：慢趋势里突然出现第一个涨停板（近2日内），不是加仓点是减仓点
        base["accel"] = recent_zt and not prior_zt
        return base

    base["metrics"] = metrics
    base["evidence"] = ["主导资金特征不明显（市值/波动/涨停节奏均不典型）"]
    return base
