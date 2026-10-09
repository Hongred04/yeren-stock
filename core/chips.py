# -*- coding: utf-8 -*-
"""筹码分布量化 —— 用日K线估算获利盘 / 套牢盘 / 成本分布。

没有 L2、没有逐笔成交，就用经典近似：把每天的成交量按当日价格区间
（三角形分布、峰值在典型价）铺开，越久远的交易日权重越衰减 ——
把以前只能写文字的「筹码结构好 / 散户套牢躺平」变成数字：

  · 获利盘比例（现价之下的筹码占比）
  · 平均成本 + 成本乖离（现价 vs 平均成本）
  · 筹码集中度（(p95−p5)/(p95+p5)，常用口径）与 90% 成本区间
  · 分布直方图（前端画筹码峰用）

口径说明：用不复权日K的成交量做**相对**结构估计，只用于表达
「低位沉淀 / 高位派发」这类相对判断；除权日附近绝对价格会有偏差，
短线（几周）窗口内足够用。不参与硬过滤，只进详情页展示 + 理由/警示。
"""
from __future__ import annotations

DEFAULT_BINS = 48
SERIES_BINS = 30          # 给前端的直方图档数


def analyze(bars: list[dict], close: float | None = None,
            lookback: int = 120, bins: int = DEFAULT_BINS,
            decay: float = 0.965) -> dict:
    """估算筹码分布。bar 需要 open/high/low/close/volume 字段。

    返回 {} 表示样本不足（K线 <20 根或价格区间无效），调用方直接跳过。
    """
    if not bars:
        return {}
    bars = [b for b in bars[-lookback:]
            if (b.get("high") or 0) > 0 and (b.get("low") or 0) > 0]
    if len(bars) < 20:
        return {}

    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    if hi <= lo:
        return {}
    close = close or bars[-1].get("close") or hi
    step = (hi - lo) / bins
    chips = [0.0] * bins

    n = len(bars)
    for i, b in enumerate(bars):
        w = decay ** (n - 1 - i)                  # 越久远权重越小
        vol = b.get("volume") or 0.0
        if vol <= 0:
            vol = 1.0                             # 无量日按最小权重占一档
        b_lo = max(0, min(bins - 1, int((b["low"] - lo) / step)))
        b_hi = max(0, min(bins - 1, int((b["high"] - lo) / step)))
        if b_hi <= b_lo:
            chips[b_lo] += w * vol
            continue
        # 三角形分布：峰值在典型价 (h+l+c)/3，向两端线性衰减
        tp = (b["high"] + b["low"] + b["close"]) / 3
        weights = []
        for k in range(b_lo, b_hi + 1):
            price = lo + (k + 0.5) * step
            ww = 1.0 - abs(price - tp) / (step * (b_hi - b_lo + 1) / 2 + step)
            weights.append(max(0.05, ww))
        tw = sum(weights)
        for k, ww in zip(range(b_lo, b_hi + 1), weights):
            chips[k] += w * vol * ww / tw

    total = sum(chips)
    if total <= 0:
        return {}
    norm = [c / total for c in chips]

    def cdf_le(price: float) -> float:
        return sum(c for k, c in enumerate(norm)
                   if lo + (k + 0.5) * step <= price)

    def quantile(q: float) -> float:
        acc = 0.0
        for k, c in enumerate(norm):
            acc += c
            if acc >= q:
                return lo + (k + 0.5) * step
        return hi

    winner = cdf_le(close)
    avg_cost = sum((lo + (k + 0.5) * step) * c for k, c in enumerate(norm))
    p5, p15, p50, p85, p95 = (quantile(q) for q in (0.05, 0.15, 0.50, 0.85, 0.95))
    concentration = (p95 - p5) / (p95 + p5) if (p95 + p5) else 0.0
    deviation = (close - avg_cost) / avg_cost if avg_cost else 0.0

    # 直方图下采样
    if bins != SERIES_BINS:
        merged = [0.0] * SERIES_BINS
        for k, c in enumerate(norm):
            merged[min(SERIES_BINS - 1, k * SERIES_BINS // bins)] += c
        series = merged
    else:
        series = norm

    return {
        "winner_rate": round(winner, 3),
        "trapped_rate": round(1 - winner, 3),
        "avg_cost": round(avg_cost, 2),
        "deviation": round(deviation, 3),
        "concentration": round(concentration, 3),
        "p5": round(p5, 2), "p15": round(p15, 2), "p50": round(p50, 2),
        "p85": round(p85, 2), "p95": round(p95, 2),
        "lo": round(lo, 2), "hi": round(hi, 2),
        "series": [round(x, 4) for x in series],
        "close": round(close, 2),
        "window": n,
    }
