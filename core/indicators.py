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
    """**最近 lookback 根 K 线里**的涨停日列表。

    ★ 是"窗口内"而不是"最后 N 个涨停日"（v1.32.3 修）：原实现 `out[-lookback:]`
      取的是**最后 lookback 个涨停日**，与日期窗口完全无关 —— 实测造 70 根 K 线、
      涨停全部发生在第 1~5 根（最近 20 根一次都没有），`lookback=20` 仍返回 4 次。
      后果是用户可见的错误：详情页照着这个数显示「近20日涨停 4 次」，
      选股理由写"前20日有4次涨停，活性仍在"并**加 0.15 分**，
      `style_fit` 还会因 `lus20 >= 2` 把票误判成"短线题材"。
      多取一根是为了算首根的涨幅（需要前一日收盘价）。
    """
    if len(bars) < 2:
        return []
    lookback = max(1, int(lookback or 1))
    seg = bars[-(lookback + 1):]
    th = limit_up_threshold(code)
    out = []
    for i in range(1, len(seg)):
        pc = seg[i - 1].get("close") or 0
        if pc > 0 and (seg[i]["close"] - pc) / pc * 100 >= th:
            out.append(seg[i])
    return out


def _pct_change(cur_bar: dict, prev_bar: dict) -> float | None:
    """相邻两根的涨幅（%）。前收 <= 0 或缺失时返回 None（不参与判定）。

    除零守卫：数据源给到 0/缺字段（次新股、停牌、接口部分响应）时，
    原来会直接 ZeroDivisionError 把整个选股流程打断。
    """
    pc = prev_bar.get("close") or 0
    if pc <= 0:
        return None
    return (cur_bar["close"] - pc) / pc * 100


def consecutive_boards(bars: list[dict], code: str) -> int:
    """截至最后一根K线的连板数。"""
    th = limit_up_threshold(code)
    cnt = 0
    for i in range(len(bars) - 1, 0, -1):
        pct = _pct_change(bars[i], bars[i - 1])
        if pct is not None and pct >= th:
            cnt += 1
        else:
            # 非涨停（含数据缺失）都算中断：连板必须连续
            break
    return cnt


def max_boards(bars: list[dict], code: str) -> int:
    """历史最大连板高度。"""
    th = limit_up_threshold(code)
    best = cur = 0
    for i in range(1, len(bars)):
        pct = _pct_change(bars[i], bars[i - 1])
        if pct is not None and pct >= th:
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
    """底部形态：区间位置 + 缩量程度。

    ★ 返回值必须**始终**含 pos/shrink/near_base/deep_base 四个键：
    调用方（engine._graph_score）是 `base["deep_base"]` 直接下标取值的，
    原来 K 线不足 lookback 时的提前返回漏了 deep_base —— 只要一只票的
    K 线少于 20 根（次新股、上市不久、或K线源只回了部分数据），
    `/api/stock` 详情就会 KeyError 崩掉（screen 有 >=25 根的过滤兜着，
    score_one 没有）。样本够 2 根就按实际根数算，比一律返回中性值更有信息量。
    """
    if len(bars) < 2:
        return {"pos": 0.5, "shrink": 1.0, "near_base": False, "deep_base": False}
    lookback = max(2, min(int(lookback or 20), len(bars)))
    seg = bars[-lookback:]
    highs = [b["high"] for b in seg]
    lows = [b["low"] for b in seg]
    hi, lo = max(highs), min(lows)
    rng = hi - lo
    pos = (bars[-1]["close"] - lo) / rng if rng > 0 else 0.5
    # max(1, ...)：lookback 小于 8 时 lookback//4 会是 0，而 shrink_ratio 的
    # n=0 会走 bars[-0:]（=全序列）再除以 0 → ZeroDivisionError。
    sh = shrink_ratio(bars, max(1, min(5, lookback // 4)))
    return {
        "pos": pos,
        "shrink": sh,
        "near_base": pos <= 0.45,
        "deep_base": pos <= 0.3,
    }


def washout_signal(bars: list[dict]) -> dict:
    """洗盘信号：长下影线（撬板）+ 缩量。

    v1.32.3：删掉原来那个**从未被使用**的 `lookback` 形参 —— 调用方传 10 会以为
    它把窗口限制在近 10 根，实际影线只看 `bars[-1]`、缩量看 `shrink_ratio(bars, 5)`
    （自身要求 ≥10 根）。留着这个参数只会误导下一个改代码的人。
    """
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
