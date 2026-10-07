# -*- coding: utf-8 -*-
"""技术指标与形态识别。

覆盖「全能的野人」审美三要素所需的全部量化口径：
  图形：底部量缩价缩、均线多头排列、上下影线洗盘、前N日涨停、连板基因
  量能：区间缩量比、量能峰谷
  情绪：连板高度、涨停基因强度
"""
from __future__ import annotations

import math


# ---------------------------------------------------------------- 基础指标

def ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ma_series(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [values[0]] if values else []
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], n: int = 14) -> float:
    if len(values) < n + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def atr(bars: list[dict], n: int = 14) -> float:
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(n, len(trs))


def macd(values: list[float]) -> tuple[float, float, float]:
    """返回 (DIF, DEA, MACD柱)"""
    if len(values) < 35:
        return 0.0, 0.0, 0.0
    e12, e26 = ema(values, 12), ema(values, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2


# ---------------------------------------------------------------- 量能

def volume_ratio(bars: list[dict], n: int = 5) -> float:
    """近 n 日均量 / 前 n 日均量。>1 放量，<1 缩量。"""
    if len(bars) < n * 2:
        return 1.0
    recent = bars[-n:]
    prev = bars[-n * 2:-n]
    vr = sum(b["volume"] for b in prev) / max(len(prev), 1)
    if vr <= 0:
        return 1.0
    return sum(b["volume"] for b in recent) / len(recent) / vr


def shrink_ratio(bars: list[dict], n: int = 5) -> float:
    """缩量比：区间均量相对前高点的压缩程度。越小越缩量。"""
    if len(bars) < n * 2:
        return 1.0
    recent = sum(b["volume"] for b in bars[-n:]) / n
    base = max(b["volume"] for b in bars[-n * 3:-n]) if len(bars) >= n * 3 else 0
    if base <= 0:
        return 1.0
    return recent / base


# ---------------------------------------------------------------- K线形态

def upper_shadow_ratio(bar: dict) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return (bar["high"] - max(bar["open"], bar["close"])) / rng


def lower_shadow_ratio(bar: dict) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return (min(bar["open"], bar["close"]) - bar["low"]) / rng


def is_limit_up(bar: dict, prev_close: float, threshold: float = 9.6) -> bool:
    """是否涨停（覆盖 10cm/20cm/30cm）。"""
    if prev_close <= 0:
        return False
    return (bar["close"] - prev_close) / prev_close * 100 >= threshold


def limit_up_threshold(code: str) -> float:
    """涨停幅度阈值：创业板/科创板 20%，北交所 30%，主板 10%。"""
    num = code[2:]
    if num.startswith(("300", "301", "688", "689")):
        return 19.5
    if num.startswith(("43", "83", "87", "88", "92")):
        return 29.0
    return 9.5


def count_limit_ups(bars: list[dict], code: str, lookback: int = 20) -> list[dict]:
    """近 lookback 日内的涨停日列表。"""
    th = limit_up_threshold(code)
    out = []
    for i in range(1, len(bars)):
        if (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100 >= th:
            out.append(bars[i])
    return out[-lookback:]


def consecutive_boards(bars: list[dict], code: str) -> int:
    """截至最后一根K线的连板数。"""
    th = limit_up_threshold(code)
    cnt = 0
    for i in range(len(bars) - 1, 0, -1):
        pct = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100
        if pct >= th:
            cnt += 1
        elif pct > 0:
            break
        else:
            break
    return cnt


def max_boards(bars: list[dict], code: str) -> int:
    """历史最大连板高度。"""
    th = limit_up_threshold(code)
    best = cur = 0
    for i in range(1, len(bars)):
        pct = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100
        if pct >= th:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def ma_bull_alignment(bars: list[dict]) -> dict:
    """均线多头排列判定。"""
    if len(bars) < 60:
        return {"aligned": False, "ma5": 0, "ma10": 0, "ma20": 0, "ma60": 0}
    closes = [b["close"] for b in bars]
    m5, m10, m20, m60 = (ma(closes, n) for n in (5, 10, 20, 60))
    aligned = m5 > m10 > m20 > m60
    return {"aligned": aligned, "ma5": m5, "ma10": m10, "ma20": m20, "ma60": m60}


def base_pattern(bars: list[dict], lookback: int = 20) -> dict:
    """底部形态：区间位置 + 缩量程度。"""
    if len(bars) < lookback:
        return {"pos": 0.5, "shrink": 1.0, "near_base": False}
    seg = bars[-lookback:]
    highs = [b["high"] for b in seg]
    lows = [b["low"] for b in seg]
    hi, lo = max(highs), min(lows)
    rng = hi - lo
    pos = (bars[-1]["close"] - lo) / rng if rng > 0 else 0.5
    sh = shrink_ratio(bars, min(5, lookback // 4))
    return {
        "pos": pos,
        "shrink": sh,
        "near_base": pos <= 0.45,
        "deep_base": pos <= 0.3,
    }


def washout_signal(bars: list[dict], lookback: int = 10) -> dict:
    """洗盘信号：长下影线（撬板）+ 缩量。"""
    if not bars:
        return {"long_lower": False, "score": 0.0}
    last = bars[-1]
    ls = lower_shadow_ratio(last)
    us = upper_shadow_ratio(last)
    rng = last["high"] - last["low"]
    long_lower = ls >= 0.4 and rng > 0
    # 上下影线都有 = 洗盘充分
    both = (ls >= 0.25 and us >= 0.15)
    score = 0.0
    if long_lower:
        score += 0.5
    if both:
        score += 0.3
    if shrink_ratio(bars, 5) < 0.85:
        score += 0.2
    return {"long_lower": long_lower, "both_shadow": both, "score": min(score, 1.0)}


def drawdown_from_high(bars: list[dict], lookback: int = 60) -> float:
    if len(bars) < lookback:
        lookback = len(bars)
    seg = bars[-lookback:]
    hi = max(b["high"] for b in seg)
    if hi <= 0:
        return 0.0
    return (hi - bars[-1]["close"]) / hi


def volatility(bars: list[dict], n: int = 20) -> float:
    """年化波动率（近似）。"""
    if len(bars) < n + 1:
        return 0.0
    rets = []
    for i in range(len(bars) - n, len(bars)):
        rets.append(math.log(bars[i]["close"] / bars[i - 1]["close"]))
    if not rets:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var) * math.sqrt(244) * 100
