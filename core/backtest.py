# -*- coding: utf-8 -*-
"""E10 轻量回测 —— 把「信号级」的收益统计，聚合成「组合级」的净值曲线。

为什么要有它
------------
E4 信号复盘（core/journal.py）回答的是「B1 首板这个信号准不准」——
它有每笔的 1/3/5 日收益、胜率、均值。但它回答不了三个更要紧的问题：

  1. 照这套系统做下去，**钱是多了还是少了**？（信号胜率高 ≠ 组合赚钱：
     仓位分配、同时持仓数、现金闲置都会改变结果）
  2. 中间**最深亏多少**？能不能扛住？（只看胜率会严重高估体感）
  3. 收益是**稳定的还是靠一两笔**？（均值会被极值绑架）

本模块把逐笔收益按「等权、每日再平衡」的口径聚合成日频净值曲线，
再算总收益 / 年化 / 最大回撤 / 夏普 / 卡玛 / 胜率 / 盈亏比 —— 用一套
和 E4 完全相同的成交假设，保证两个页面的数字对得上。

成交假设（与 journal.py 逐条一致，不另立一套）
---------------------------------------------
  · **t+1 开盘进场**：信号是收盘后给的，一律按次日开盘价买入（防未来函数）；
  · **涨停不可买**：次日开盘贴涨停（一字/秒板）→ 记「未成交」，不进组合；
  · **停牌过滤**：次日无K线 → 记「未成交」，不进组合；
  · **成本计入**：买卖往返 cost_pct（默认 0.2%），进场时即扣；
  · **持有窗口**：默认持有 hold_days 个交易日（默认 5），按**收盘**卖出；
  · **等权、每日再平衡**：每个交易日把可用资金等分给当天持有的票。

★ 口径如实声明（不许含糊）
--------------------------
  · 这是**信号组合**的回测，不是「整个账户」的回测 —— 它假设你**只**按
    系统给的买点做，且每笔都给同样的钱。真实账户还有仓位上限、
    情绪择时、手动加减仓，结果不会一样。
  · 只统计**已结算/部分结算**的样本（skip 是买不到，不进胜率也不进净值）。
  · 净值曲线用的是**已实现收益**（卖出才计入），不是逐日浮动盈亏 ——
    后者需要逐日持仓市值，本模块不做（那需要完整持仓账本）。
  · 样本仍然很少（系统才跑了十几天），所以本模块**不输出年化**
    除非样本天数够（MIN_DAYS_FOR_ANNUAL）。宁可显示「样本不足」，
    也不要拿 12 天数据乘以 21 编出一个年化收益率骗自己。

与其它模块的关系：只读 signal_journal.json 与K线缓存，**永不改写信号**。
"""
from __future__ import annotations

import math
from datetime import datetime

# 年化需要的最少交易日 —— 低于这个数不做年化（12 天×21=252 是编数）
MIN_DAYS_FOR_ANNUAL = 60
# 夏普/卡玛需要的最少日收益样本
MIN_DAYS_FOR_RATIO = 30
TRADING_DAYS_PER_YEAR = 252


def _f(v, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _norm_date(v) -> str:
    return str(v or "").replace("-", "").replace("/", "")[:8]


def _d2(v: str) -> datetime | None:
    try:
        return datetime.strptime(_norm_date(v), "%Y%m%d")
    except ValueError:
        return None


# ---------------------------------------------------------------- 净值聚合

def equity_curve(trades: list[dict], dates: list[str] | None = None) -> dict:
    """把逐笔已实现收益聚合成日频净值曲线。

    trades: [{date, ret, ...}]，ret 是**已扣成本**的区间收益（如 ret5）。
    这里做的是「等权、按日再平衡」的资金曲线：
      期末资金 = 期初资金 × (1 + 当日所有已实现交易收益的等权平均)

    用等权平均而不是加权求和，是因为后者需要知道「每天总共投了多少钱」——
    那需要完整持仓账本；等权平均回答的是「每笔一样多的话，平均每笔赚多少」。

    ★ 同一只票在同一天只算一笔（dedupe）。
      原因（实测 20261009-20261012 的 166 条档案）：同一只票常常同时命中
      两个信号（如「退潮试错仓 B0」+「首板试错 B1」），各记一条。它们的
      **收益完全相同**（同一只票同一段行情），重复计入会让日收益被同一笔
      行情绑架。实测：不去重时 days=2、胜率 65.3%；按 (日期,代码) 去重后
      是 67.9% —— 数字会真的不一样。

    返回 {dates, nav, daily, trades_n, ...}。dates 为 None 时按 trades 里
    出现过的交易日排序。
    """
    # ① 按 (日期, 代码) 去重 —— 同一天同一只票只算一次
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for t in trades:
        d = _norm_date(t.get("date"))
        if not d or t.get("ret") is None:
            continue
        key = (d, t.get("code") or id(t))
        if key in seen:
            continue
        seen.add(key)
        uniq.append({**t, "date": d})

    by_day: dict[str, list[float]] = {}
    for t in uniq:
        by_day.setdefault(t["date"], []).append(_f(t["ret"]))

    if not by_day:
        return {"dates": [], "nav": [], "daily": [], "trades_n": 0,
                "span_days": 0}

    if dates is None:
        dates = sorted(by_day)
    else:
        # 只保留真的有交易的交易日：自然日补齐会让曲线被周末/停牌日拉平，
        # 看起来"没有波动"，是假象。
        dates = sorted(d for d in (_norm_date(x) for x in dates) if d in by_day)

    nav = [1.0]
    daily: list[float] = []
    for d in dates:
        rets = by_day.get(d) or []
        r = (sum(rets) / len(rets)) if rets else 0.0
        daily.append(round(r, 6))
        nav.append(round(nav[-1] * (1.0 + r), 6))

    return {
        "dates": dates,
        "nav": nav[1:],                      # 与 dates 对齐（首日前的 1.0 不占位）
        "daily": daily,
        "trades_n": len(uniq),
        "span_days": len(dates),
    }


# ---------------------------------------------------------------- 组合指标

def max_drawdown(nav: list[float]) -> tuple[float, int, int]:
    """最大回撤（负数，如 -0.12 = -12%）。返回 (mdd, peak_i, trough_i)。"""
    if not nav:
        return 0.0, -1, -1
    peak = nav[0]
    peak_i = 0
    worst = 0.0
    wi = wi_peak = -1
    for i, v in enumerate(nav):
        if v > peak:
            peak = v
            peak_i = i
        if peak > 0:
            dd = v / peak - 1.0
            if dd < worst:
                worst = dd
                wi = i
                wi_peak = peak_i
    return round(worst, 6), wi_peak, wi


def sharpe(daily: list[float], rf: float = 0.0,
           periods: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """夏普比率（日频 → 年化）。样本不足返回 None —— 不编数。"""
    if len(daily) < MIN_DAYS_FOR_RATIO:
        return None
    mu = sum(daily) / len(daily)
    var = sum((x - mu) ** 2 for x in daily) / max(1, len(daily) - 1)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return None
    return round((mu - rf / periods) / sd * math.sqrt(periods), 3)


def calmar(total_ret: float, mdd: float) -> float | None:
    """卡玛比率 = 年化收益 / |最大回撤|。样本不足或回撤为 0 时返回 None。"""
    if mdd >= -1e-9:
        return None
    return round(total_ret / abs(mdd), 3)


def profit_factor(rets: list[float]) -> float | None:
    """盈亏比 = 总盈利 / 总亏损（绝对值）。无亏损样本返回 None。"""
    gain = sum(r for r in rets if r > 0)
    loss = -sum(r for r in rets if r < 0)
    if loss <= 1e-12:
        return None
    return round(gain / loss, 3)


def summarize(curve: dict, rets: list[float],
              hold_days: int = 5) -> dict:
    """由净值曲线 + 逐笔收益算出组合级指标。"""
    nav = curve.get("nav") or []
    daily = curve.get("daily") or []
    total = round(nav[-1] - 1.0, 6) if nav else 0.0
    mdd, peak_i, trough_i = max_drawdown(nav or [1.0])
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]

    out = {
        "total_ret": total,
        "mdd": mdd,
        "daily_n": len(daily),
        "trades_n": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_ret": round(sum(rets) / n, 6) if n else None,
        "best": round(max(rets), 6) if rets else None,
        "worst": round(min(rets), 6) if rets else None,
        "profit_factor": profit_factor(rets),
        "sharpe": sharpe(daily),
        # 年化：样本不够就**不给**（null），前端显示「样本不足」
        "annual": None,
        "calmar": None,
        "min_days_for_annual": MIN_DAYS_FOR_ANNUAL,
        "mdd_peak_date": curve["dates"][peak_i] if 0 <= peak_i < len(curve.get("dates") or []) else None,
        "mdd_trough_date": curve["dates"][trough_i] if 0 <= trough_i < len(curve.get("dates") or []) else None,
    }
    span = curve.get("span_days") or 0
    if span >= MIN_DAYS_FOR_ANNUAL and total > -1.0:
        try:
            out["annual"] = round((1.0 + total) ** (TRADING_DAYS_PER_YEAR / span) - 1.0, 6)
            out["calmar"] = calmar(out["annual"], mdd)
        except (OverflowError, ValueError):
            pass
    return out


# ---------------------------------------------------------------- 分组诊断

def by_group(trades: list[dict], key: str = "signal") -> list[dict]:
    """按信号类型/板块分组，各自出一条净值曲线与指标。

    ★ 分组的**样本口径**：同一只票同一天命中多条信号时，它会在每个命中的
      信号里各出现一次 —— 这是有意的：这个表回答的是「B0 这个信号准不准」，
      那只票确实触发了 B0，就该算进去。但每组内部的净值曲线仍按
      (日期,代码) 去重，组内不会重复计数。
    """
    groups: dict[str, list[dict]] = {}
    for t in trades:
        k = t.get(key) or "?"
        groups.setdefault(str(k), []).append(t)
    rows = []
    for k, g in groups.items():
        name = g[0].get("signal_name") or k if key == "signal" else k
        # 组内去重后再算收益：否则同只票两个信号会给这一组贡献两次同样的收益
        seen: set[tuple] = set()
        rets: list[float] = []
        for t in g:
            if t.get("ret") is None:
                continue
            kk = (_norm_date(t.get("date")), t.get("code"))
            if kk in seen:
                continue
            seen.add(kk)
            rets.append(_f(t["ret"]))
        if not rets:
            continue
        curve = equity_curve(g)
        s = summarize(curve, rets)
        rows.append({"key": k, "name": name, "n": len(rets), **s})
    rows.sort(key=lambda x: (-(x["n"] or 0)))
    return rows


def by_score_band(trades: list[dict], bands: int = 4) -> list[dict]:
    """按**评分**分档看收益 —— 这是「因子有没有区分度」的最轻量检验。

    ticai-daban 的教训：它的 qscore 因子在全市场顶档占 82.7%（塌缩），
    等于没区分度。本函数用同样的问题问我们自己：
      「评分最高的那一档，收益真的比最低档高吗？」

    ★ 必须先按 (日期, 代码) 去重，否则分档结果会被重复票污染 ——
      实测不去重时「1 档评分最低反而收益最高」，去重后才看得清真实关系。
    ★ 样本极少时**不给单调性结论**：只有 2 个交易日、每天 4 档各 4~5 笔，
      任何"分档收益递增/递减"都是噪声。所以要同时看 spread 与 n，
      并在 spread 与 0 无法区分时如实标 inconclusive。
    """
    seen: set[tuple] = set()
    scored: list[dict] = []
    for t in trades:
        if t.get("score") is None or t.get("ret") is None:
            continue
        k = (_norm_date(t.get("date")), t.get("code"))
        if k in seen:
            continue
        seen.add(k)
        scored.append(t)
    if len(scored) < bands * 2:
        return []
    scored.sort(key=lambda t: _f(t.get("score")))
    per = len(scored) // bands
    if per < 1:
        return []
    rows = []
    for i in range(bands):
        lo = i * per
        hi = len(scored) if i == bands - 1 else (i + 1) * per
        g = scored[lo:hi]
        rets = [_f(t["ret"]) for t in g]
        if not rets:
            continue
        rows.append({
            "band": i + 1,
            "score_lo": round(_f(g[0].get("score")), 1),
            "score_hi": round(_f(g[-1].get("score")), 1),
            "n": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
            "avg_ret": round(sum(rets) / len(rets), 6),
        })
    if len(rows) >= 2:
        avgs = [r["avg_ret"] for r in rows]
        inc = all(avgs[i] <= avgs[i + 1] for i in range(len(avgs) - 1))
        dec = all(avgs[i] >= avgs[i + 1] for i in range(len(avgs) - 1))
        total_n = sum(r["n"] for r in rows)
        top_share = rows[-1]["n"] / total_n if total_n else 0.0
        spread = avgs[-1] - avgs[0]
        # 结论只在「有方向」且「极差大于一档成本」时才敢下 —— 否则是噪声
        conclusive = bool((inc or dec) and abs(spread) > 0.005)
        # ★ ok 的判据：只有「评分越高、收益也越高」（monotonic=='up'）才叫
        #   有区分度。写这行时踩过一次坑：原来写 `conclusive and top_share<0.5`，
        #   而 conclusive 对 up/down 都为真 —— 于是「评分越高越亏」这种
        #   **方向完全反了**的因子也被判成 ok=True，正好把最该报警的情况
        #   说成可用。down 必须单独判为不可信（不是"反向有效"，是没道理）。
        ok_flag = bool(conclusive and inc and top_share < 0.5)
        diag = {
            "monotonic": "up" if inc else "down" if dec else "none",
            "top_share": round(top_share, 4),
            "spread": round(spread, 6),
            "n": total_n,
            "conclusive": conclusive,
            "ok": ok_flag,
            "note": ("评分的高低与收益**方向不一致**，当前样本下评分没有区分度"
                     if not (inc or dec) else
                     ("★ 评分越高、收益反而越低 —— 分档方向是反的，"
                      "这个因子当前不可用，先查是不是评分里某一项在扣分反了"
                      if dec else "分档收益随评分递增")
                     + ("（但只有 %d 笔样本，还不足以当结论）" % total_n
                        if not conclusive else "")),
        }
        for r in rows:
            r["monotonic"] = diag["monotonic"]
        rows[0]["_diag"] = diag
    return rows


# ---------------------------------------------------------------- 口径与入口

def build(entries: list[dict], hold_days: int = 5,
          min_samples: int = 10) -> dict:
    """由 E4 档案条目构建回测结果。

    entries: signal_journal.json 的 entries（每笔含 signal/score/ret1..ret5）。
    hold_days: 用哪一档收益当「一笔交易」的收益（1/3/5）。
    min_samples: 少于这么多笔就不给结论（只如实说样本不够）。
    """
    h = hold_days if hold_days in (1, 3, 5) else 5
    rk = f"ret{h}"

    trades = []
    skipped = 0
    pending = 0
    for e in entries or []:
        if e.get("status") == "skip":
            skipped += 1
            continue
        r = e.get(rk)
        if r is None:
            pending += 1
            continue
        trades.append({
            "date": _norm_date(e.get("date")),
            "code": e.get("code"),
            "name": e.get("name", ""),
            "sector": e.get("sector", ""),
            "signal": e.get("signal"),
            "signal_name": e.get("signal_name"),
            "score": e.get("score"),
            "ret": _f(r),
        })

    rets = [t["ret"] for t in trades]
    curve = equity_curve(trades)
    # 组合级收益用**去重后**的样本（同一天同只票只算一笔）
    seen: set[tuple] = set()
    uniq_rets: list[float] = []
    for t in trades:
        k = (_norm_date(t.get("date")), t.get("code"))
        if k in seen:
            continue
        seen.add(k)
        uniq_rets.append(t["ret"])
    s = summarize(curve, uniq_rets, hold_days=h)

    enough = len(uniq_rets) >= min_samples
    dup_n = len(trades) - len(uniq_rets)
    note_bits = [
        f"每笔按 T+1 开盘买入、持有 {h} 个交易日收盘卖出，等权、已扣往返成本。",
        f"共 {len(trades)} 笔已结算样本"
        + (f"（其中 {dup_n} 笔是同一只票同一天命中多个信号，"
           "组合口径按一只票只算一笔，去重后 %d 笔）" % len(uniq_rets)
           if dup_n else "")
        + (f"，{pending} 笔还在跟踪" if pending else "")
        + (f"，{skipped} 笔未成交（涨停/停牌，不进组合）" if skipped else "")
        + "。",
    ]
    if not enough:
        note_bits.append(
            f"样本不足 {min_samples} 笔 —— 下面的数字**还不足以当结论**，"
            "先当趋势看。")
    if s.get("annual") is None:
        note_bits.append(
            f"样本天数不足 {MIN_DAYS_FOR_ANNUAL} 个交易日，**不做年化**"
            "（拿十几天数据乘 21 编出来的年化是骗自己）。")
    if (curve.get("span_days") or 0) < 5:
        note_bits.append(
            f"★ 组合曲线目前只有 {curve.get('span_days') or 0} 个交易日、"
            "最多两三个数据点 —— 回撤永远是 0%、夏普算不出来，"
            "**这不是「表现稳健」，是样本还没有**。")

    return {
        "enabled": True,
        "hold_days": h,
        "enough": enough,
        "min_samples": min_samples,
        "n_trades": len(trades),
        "n_uniq": len(uniq_rets),
        "n_dup": dup_n,
        "n_pending": pending,
        "n_skip": skipped,
        "curve": curve,
        "summary": s,
        "by_signal": by_group(trades, "signal"),
        "by_sector": by_group(trades, "sector")[:12],
        "by_score": by_score_band(trades),
        "note": "".join(note_bits),
    }


def pick_hold_days(entries: list[dict], want: int = 5,
                   min_samples: int = 10) -> tuple[int, str]:
    """选一档**真的有样本**的持有周期。

    为什么需要这个：档案是系统上线后才开始记的，所以「持有 5 日」的结果
    要等 5 个交易日才存在。实测刚上线时 ret5 有 0 笔、ret1 有 72 笔 ——
    如果死守默认 5，页面永远是空的（用户看不出这个功能存在）。

    策略：优先用 want；want 的样本不够就退到样本够的**最长**那一档，
    并如实说明「本次用的是 N 日口径，因为 M 日还没到期」。

    返回 (hold_days, 说明文字)。
    """
    counts = {}
    for h in (1, 3, 5):
        counts[h] = sum(1 for e in entries or [] if e.get(f"ret{h}") is not None)
    if counts.get(want, 0) >= min_samples:
        return want, ""
    # 找样本够的、且尽量接近 want 的档（优先更长，更接近默认意图）
    for h in sorted((x for x in counts if counts[x] >= min_samples),
                    key=lambda x: (-x, abs(x - want))):
        return h, (f"（本次按 {h} 日口径 —— {want} 日的结果还没到期，"
                   f"目前只有 {counts.get(want, 0)} 笔）")
    # 全都不够：用样本最多的那一档，让页面至少能显示"样本不足"而不是空白
    best = max(counts, key=lambda x: counts[x]) if counts else want
    if counts.get(best, 0) > counts.get(want, 0):
        return best, (f"（本次按 {best} 日口径 —— 各档样本都还不足，"
                      f"{best} 日最多，只有 {counts.get(best, 0)} 笔）")
    return want, ""


def run(cfg: dict | None = None, entries: list[dict] | None = None) -> dict:
    """入口。cfg 取 E10_backtest 段；任何异常都不允许带崩主扫描。"""
    a = (cfg or {}).get("E10_backtest", {})
    if not a.get("enabled", True):
        return {"enabled": False, "note": "回测模块已关闭"}
    try:
        if entries is None:
            from . import journal as journal_mod
            entries = journal_mod._load()
        want = int(_f(a.get("hold_days"), 5)) or 5
        mins = int(_f(a.get("min_samples"), 10)) or 10
        # 想要的那一档没样本就自动降档 —— 否则新装用户看到的是空白页
        h, why = pick_hold_days(entries, want=want, min_samples=mins)
        out = build(entries, hold_days=h, min_samples=mins)
        if why:
            out["hold_days_want"] = want
            out["hold_days_note"] = why
            out["note"] = out["note"] + why
        return out
    except Exception as e:                       # noqa: BLE001（绝不带崩主扫描）
        return {"enabled": True, "n_trades": 0, "curve": {"dates": [], "nav": []},
                "summary": {}, "by_signal": [], "by_sector": [], "by_score": [],
                "error": f"{type(e).__name__}: {e}"}
