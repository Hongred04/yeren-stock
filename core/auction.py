# -*- coding: utf-8 -*-
"""E9 竞价早读（集合竞价 09:15–09:25 → 今天该盯什么）· 扩展模块。

一句话：**开盘前那十分钟，市场已经把态度摆在牌面上了 —— 谁被抢、谁被抛、
昨天涨停的那批今天还认不认账。**

来历（用户 10-14 的原始想法）：
「我觉得你可以给我做个当前集合竞价时的分析板块啊」

系统原状：代码里到处都有"集合竞价"这个词，但它**从来只是个时段标签**，
不是分析输入 ——
  · `trading.market_session()` 把 09:15–09:29 标成 phase="auction"；
  · `pipeline.py:412` 在竞价时段把两市成交额**清零**（因为指数快照此刻
    返回的是上一交易日全天额，见 datasource.py:936 的事故记录）；
  · `substitute.py` 的 PRE_PHASES 说竞价时段**区分不出停牌**（全市场成交额都是 0）；
  · `trading.py` 里所有进场口径都写"次日竞价"，但那是**执行提示**，不是研判。
所以用户在 09:20 打开界面，看到的还是昨天的结论。

★ 本模块补的就是这一段：**把竞价本身当数据读**。

## 数据从哪来（零新增接口）

腾讯快照 `_parse_tx_line` 已经在拉，只是**竞价时段真正有信息的字段被丢掉了**。
2026-10-14 09:20–09:25 实测 6173 只全市场样本，标定如下：

  可用（竞价时段有值）：
    [3] price       = **竞价匹配价（指示价）** ← 本模块的主信号
    [4] prev_close  = 昨收
    [32] pct        = 相对昨收的涨跌幅（等于 (3-4)/4）
    [9]/[10]        = 买一价/买一量，[9] 恒等于 [3]
    [12]            = 卖一剩余量
    [30] time       = 快照时间戳

  不可用（竞价时段恒为 0，**绝不能拿来算指标**）：
    [5] 今开 = 0.00      —— 还没开盘。★ 不要用 open 当竞价匹配价。
    [6] 成交量 = 0       [33] 最高 = 0      [34] 最低 = 0
    [37] 成交额 = 0      [38] 换手 = 0      [49] 量比 = 0
    [11] 卖一价 = 0.00   —— 竞价时段不单独给卖一价（24/24 全为 0）

  ★★ 委差 [50] 的坑（实测踩过，别重蹈）：
    直觉上 [50] = 买一量 - 卖一量。**实测 0/24 成立**。
    反例：sz000725 京东方Ａ 买量 5543、卖量 0，委差却是 **-7261** ——
    卖档余量为 0 却有巨额负委差，说明 [50] 反映的是**五档之外更深档位**。
    第二批 24 只里，`卖量>0` 时 [50] 恰好等于卖量（93=93、73=73、39=39…），
    `卖量==0` 时 [50] 为负 —— 关系随档位深浅漂移，**不稳定**。
    结论：**绝不能用 买/(买+卖) 算"委比"** —— 竞价时段卖侧几乎恒为 0，
    算出来会得到 ~99% 的假失衡，比不给数据更误导。排队量只做**描述**与
    **同批相对排名**，不做绝对强度。

## 做三件事

  ① **竞价温度** —— 全市场高开/低开分布 + 竞价涨停一字家数。回答
     "今天开盘是抢还是逃"。只用**尾部档位**（涨停/≥5%/跌停），
     因为实测 0~2% 档占 89%（3212+2310 / 6173）—— 那是噪声底，不构成信号。
  ② **昨日涨停池今晨竞价** —— 昨天那批涨停票今天怎么开：还在抢（高开续强）、
     认怂（低开）、还是一字顶死。这是打板体系**最该看的一栏**：
     连板能不能续，竞价先给一半答案。
  ③ **异动狙击** —— 竞价涨停一字（匹配价==涨停价）、大幅高开、
     以及**低开幅度大的前排票**（昨天强今天被抛弃，比高开更值得警惕）。

定位：**提醒去看一眼**，不是买入指令 —— 与 E5/E6/E7/E8 一致。
竞价的匹配价在 09:20–09:25 之间还会剧烈变化（实测同一只票几秒内
买量 6816→6624），**它给的是当下快照，不是定论**。

已知限制（如实标注，不粉饰）：
  · **无成交额/换手/量比** —— 竞价没有撮合，这些字段全是 0。
    所以"资金流入""放量"这类结论本模块**给不出来**，不要在这里找。
  · **无法区分停牌** —— 全市场成交额都是 0（见 substitute.py 的 PRE_PHASES），
    "没竞价"和"停牌"在快照里长得一样。
  · **指数成交额不可用** —— 竞价时段指数快照返回上一交易日全天额
    （datasource.py:936 记录的 2026-10-13 事故），本模块不读指数。
  · 匹配价≠开盘价：09:20 前可撤单，09:20–09:25 不可撤单，此后才是准的。
    本模块标出 snapshot 时间，让用户自己判断这份读数有多"定"。
"""
from __future__ import annotations

import time
from datetime import datetime

from . import datasource as ds
from . import trading

# 竞价时段（与 trading.market_session 的 phase 对齐）
AUCTION_PHASES = ("auction",)
# 竞价数据在开盘后仍可回看（"今晨竞价怎么开的"），这些时段允许出结果：
#   just_closed / after_close 时快照的 price=收盘价，已不是竞价态，
#   所以只有 auction 当场 + 盘前（读不到）之外，用 open 字段回看。
_POST_PHASES = ("morning", "noon", "afternoon", "tail", "just_closed", "after_close")

# 档位边界（%）。刻度直接对应 A 股涨跌幅制度，不做语义猜测：
#   主板 ±10%、创业板/科创板 ±20%、ST ±5% —— 所以"涨停"要按个股自己的
#   涨停价判定（快照里有 [47] limit_up），不能写死 9.9%。
GAP_BANDS = (
    ("limit_up", "竞价涨停", 0.0),      # 占位：实际用 limit_up 价判定
    ("gap5", "高开 ≥5%", 5.0),
    ("gap2", "高开 2~5%", 2.0),
    ("flat", "平开 ±2%", -2.0),
    ("down2", "低开 2~5%", -5.0),
    ("down5", "低开 ≥5%", -1e9),
)

# 大高开阈值：低于这个不出现在"异动"里（2% 以下实测占 89%，是噪声底）
BIG_GAP = 5.0
# 大低开阈值：昨天强、今天大幅低开 = 被抛弃
BIG_DROP = -5.0


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _int(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _gap(price: float, prev: float) -> float | None:
    """竞价高开幅度（%）。昨收无效时返回 None（不返回 0 冒充平开）。"""
    if prev <= 0 or price <= 0:
        return None
    return (price - prev) / prev * 100.0


def is_limit_up(snap: dict) -> bool:
    """竞价匹配价是否已经顶在涨停价上（竞价一字/准一字）。

    ★ 必须用该股自己的涨停价 [47] 判定：主板 10%、创业板/科创 20%、
      ST 5%，写死 9.9% 会把 20cm 的票全漏掉。
    """
    lu = _f(snap.get("limit_up"))
    px = _f(snap.get("price"))
    return lu > 0 and px > 0 and abs(px - lu) < 0.005


def is_limit_down(snap: dict) -> bool:
    ld = _f(snap.get("limit_down"))
    px = _f(snap.get("price"))
    return ld > 0 and px > 0 and abs(px - ld) < 0.005


def match_price(snap: dict) -> float:
    """竞价匹配价。

    竞价时段用 `price`（[3]）—— **不是 `open`**：实测 09:20 全市场
    [5] 今开都是 0.00，用它会把整块算成 0。
    开盘后用 `open`（那才是当日真实开盘价，可用于"今晨怎么开的"回看）。
    """
    sess = trading.market_session()
    if sess["phase"] in AUCTION_PHASES:
        return _f(snap.get("price"))
    op = _f(snap.get("open"))
    return op if op > 0 else _f(snap.get("price"))


def band_of(gap: float, snap: dict) -> str:
    """把高开幅度归到档位。涨停/跌停优先按个股涨跌停价判定。"""
    if is_limit_up(snap):
        return "limit_up"
    if is_limit_down(snap):
        return "limit_down"
    if gap >= 5.0:
        return "gap5"
    if gap >= 2.0:
        return "gap2"
    if gap > -2.0:
        return "flat"
    if gap > -5.0:
        return "down2"
    return "down5"


_BAND_CN = {
    "limit_up": "竞价涨停", "gap5": "高开≥5%", "gap2": "高开2-5%",
    "flat": "平开±2%", "down2": "低开2-5%", "down5": "低开≥5%",
    "limit_down": "竞价跌停",
}
# 展示顺序：抢筹 → 平 → 抛售
_BAND_ORDER = ("limit_up", "gap5", "gap2", "flat", "down2", "down5", "limit_down")


# ---------------------------------------------------------------- ① 竞价温度

def market_temperature(snaps: dict, bias_th: float = 5.0) -> dict:
    """全市场竞价高开/低开分布。回答"今天开盘是抢还是逃"。"""
    counts = {k: 0 for k in _BAND_ORDER}
    valid = 0
    scored: list[tuple] = []
    for code, s in snaps.items():
        gap = _gap(match_price(s), _f(s.get("prev_close")))
        if gap is None:
            continue
        valid += 1
        b = band_of(gap, s)
        counts[b] = counts.get(b, 0) + 1
        scored.append((code, s, gap, b))

    if valid == 0:
        return {"valid": 0, "bands": [], "summary": "", "bias": "unknown"}

    up = sum(counts[k] for k in ("limit_up", "gap5", "gap2"))
    dn = sum(counts[k] for k in ("down2", "down5", "limit_down"))
    flat = counts.get("flat", 0)

    # 偏向：抢筹家数 vs 抛售家数的**净差占比**。阈值取 ±5% 是有意的宽松 ——
    # 竞价 89% 的票都在 ±2% 内，卡太紧会把每天都判成"抢"或"逃"。
    net = (up - dn) / valid * 100.0
    if net >= bias_th:
        bias, bias_cn = "risk_on", "抢筹"
    elif net <= -bias_th:
        bias, bias_cn = "risk_off", "抛售"
    else:
        bias, bias_cn = "neutral", "中性"

    bands = []
    for k in _BAND_ORDER:
        n = counts.get(k, 0)
        bands.append({"key": k, "label": _BAND_CN[k], "n": n,
                      "pct": round(n / valid * 100.0, 1)})

    parts = [f"全市场 {valid} 只有效竞价"]
    if counts.get("limit_up"):
        parts.append(f"竞价涨停 {counts['limit_up']} 只")
    parts.append(f"高开(≥2%) {up} 只 vs 低开(≤-2%) {dn} 只")
    if counts.get("limit_down"):
        parts.append(f"竞价跌停 {counts['limit_down']} 只")
    summary = "；".join(parts) + f"。整体偏向：{bias_cn}（净差 {net:+.1f}%）。"

    return {
        "valid": valid, "bands": bands, "up": up, "down": dn, "flat": flat,
        "limit_up": counts.get("limit_up", 0),
        "limit_down": counts.get("limit_down", 0),
        "net_pct": round(net, 1), "bias": bias, "bias_cn": bias_cn,
        "summary": summary,
        # 内部用：②③ 复用同一份 scored，避免重复算
        "_scored": scored,
    }


# ---------------------------------------------------------------- ② 昨涨停今竞价

def yesterday_review(snaps: dict, zt_prev: list[dict], top_n: int = 12) -> dict:
    """昨日涨停池今日竞价表现 —— 打板体系最该看的一栏。

    "昨天封住的票今天怎么开"，比任何指标都直接地回答了
    「昨天的钱今天还认不认账」。
    """
    if not zt_prev:
        return {"n": 0, "rows": [], "strong": 0, "weak": 0, "dead": 0,
                "summary": "没有昨日涨停池数据"}

    rows = []
    for r in zt_prev:
        code = str(r.get("code") or "")
        s = snaps.get(code)
        if not s:
            rows.append({
                "code": code, "name": str(r.get("name") or ""),
                "boards_prev": _int(r.get("boards"), 1),
                "sector": str(r.get("sector") or ""),
                "gap": None, "state": "missing", "state_cn": "无竞价数据",
                "price": None, "prev_close": None, "limit_up": None,
                "why": "快照里没有这只票 —— 可能停牌、已退市，或本轮快照降级漏扫。",
            })
            continue
        prev = _f(s.get("prev_close"))
        px = match_price(s)
        g = _gap(px, prev)
        if g is None:
            state, state_cn = "missing", "无竞价数据"
        elif is_limit_up(s):
            state, state_cn = "sealed", "一字封死"
        elif g >= 5.0:
            state, state_cn = "strong", "高开抢筹"
        elif g >= 0.0:
            state, state_cn = "mild", "小幅高开"
        elif g > -5.0:
            state, state_cn = "weak", "低开示弱"
        else:
            state, state_cn = "dead", "大幅低开"

        rows.append({
            "code": code, "name": str(r.get("name") or s.get("name") or ""),
            "boards_prev": _int(r.get("boards"), 1),
            "sector": str(r.get("sector") or ""),
            "gap": None if g is None else round(g, 2),
            "price": px if px > 0 else None,
            "prev_close": prev if prev > 0 else None,
            "limit_up": _f(s.get("limit_up")) or None,
            "state": state, "state_cn": state_cn,
            "why": _why_prev(r, g, state),
        })

    # 排序：一字 > 高开 > 平 > 低开 > 大幅低开；同档按板数降序（高度优先）
    rank = {"sealed": 0, "strong": 1, "mild": 2, "weak": 3, "dead": 4, "missing": 5}
    rows.sort(key=lambda x: (rank.get(x["state"], 9), -(x["boards_prev"] or 0)))

    strong = sum(1 for x in rows if x["state"] in ("sealed", "strong"))
    mild = sum(1 for x in rows if x["state"] == "mild")
    weak = sum(1 for x in rows if x["state"] == "weak")
    dead = sum(1 for x in rows if x["state"] == "dead")
    missing = sum(1 for x in rows if x["state"] == "missing")

    if strong > len(rows) * 0.5:
        tone, tone_cn = "hot", "昨涨停今天还在抢"
    elif dead + weak > strong:
        tone, tone_cn = "cold", "昨涨停今天被抛"
    else:
        tone, tone_cn = "mixed", "昨涨停今天分化"

    parts = [f"昨日涨停 {len(rows)} 只"]
    if strong:
        parts.append(f"竞价续强 {strong} 只")
    if mild:
        parts.append(f"小幅高开 {mild} 只")
    if weak:
        parts.append(f"低开 {weak} 只")
    if dead:
        parts.append(f"大幅低开 {dead} 只")
    if missing:
        parts.append(f"无数据 {missing} 只")
    summary = "；".join(parts) + f"。{tone_cn}。"

    return {"n": len(rows), "rows": rows[:max(1, top_n)], "all_rows": rows,
            "strong": strong, "mild": mild, "weak": weak, "dead": dead,
            "missing": missing, "tone": tone, "tone_cn": tone_cn,
            "summary": summary}


def _why_prev(r: dict, g: float | None, state: str) -> str:
    """给昨涨停票的一句读法。"""
    b = _int(r.get("boards"), 1)
    sec = str(r.get("sector") or "").strip()
    tag = f"{b} 板" + (f"·{sec}" if sec else "")
    if state == "sealed":
        return f"{tag}：竞价直接顶在涨停价上（一字/准一字），今天是「强更强」的开局。"
    if state == "strong":
        return (f"{tag}：高开 {g:+.1f}% —— 昨天封住的筹码今天有人加价接，"
                f"晋级预期还在。按体系口径，高开 0~3% 是预期内，"
                f"＞5% 已是抢筹过头，追高要防冲高回落。")
    if state == "mild":
        return f"{tag}：小幅高开 {g:+.1f}%，属正常换手开局，看开盘后能不能顶住。"
    if state == "weak":
        return (f"{tag}：低开 {g:+.1f}% —— 昨天的封单没换来今天的溢价，"
                f"接力资金在退。断板风险明显上升。")
    if state == "dead":
        return (f"{tag}：大幅低开 {g:+.1f}% —— 昨天的板被直接抛弃，"
                f"前一天追进去的大概率被闷在里面。这是最该警惕的一档。")
    return f"{tag}：快照里取不到竞价数据。"


# ---------------------------------------------------------------- ③ 异动

def movers(scored: list[tuple], snaps: dict, top_n: int = 10,
           big_gap: float = BIG_GAP) -> dict:
    """竞价异动：一字涨停 / 大高开 / 大低开（各取前 N）。"""
    sealed, highs, lows = [], [], []
    for code, s, gap, band in scored:
        row = {
            "code": code, "name": str(s.get("name") or ""),
            "gap": round(gap, 2), "price": match_price(s),
            "prev_close": _f(s.get("prev_close")),
            "limit_up": _f(s.get("limit_up")) or None,
            "band": band,
        }
        if is_limit_up(s):
            sealed.append(row)
        elif gap >= big_gap:
            highs.append(row)
        elif gap <= -big_gap:
            lows.append(row)

    sealed.sort(key=lambda x: -x["gap"])
    highs.sort(key=lambda x: -x["gap"])
    lows.sort(key=lambda x: x["gap"])
    for r in highs:
        r["why"] = _why_high(r)
    for r in lows:
        r["why"] = _why_low(r)
    for r in sealed:
        r["why"] = "竞价匹配价已顶在涨停价上 —— 开盘即一字/准一字，抢不到，只能看能不能排上。"

    return {"sealed": sealed[:top_n], "highs": highs[:top_n], "lows": lows[:top_n],
            "n_sealed": len(sealed), "n_highs": len(highs), "n_lows": len(lows)}


def _why_high(r: dict) -> str:
    return (f"竞价 {r['gap']:+.1f}% —— 高开幅度已超出常规预期区间"
            f"（0~3% 为预期内），属抢筹。注意竞价匹配价在 09:20 前仍可变化，"
            f"而且高开过头最常见的结局是冲高回落。")


def _why_low(r: dict) -> str:
    return (f"竞价 {r['gap']:+.1f}% —— 大幅低开。若昨天它是强势票，"
            f"这就是被资金抛弃的直接证据；竞价跌停则是彻底没人接。")


# ---------------------------------------------------------------- 入口

def evaluate(cfg: dict | None = None, snaps: dict | None = None,
             zt: list[dict] | None = None, session: dict | None = None,
             real_date: str = "", prev_zt: list[dict] | None = None) -> dict:
    """E9 竞价早读。

    参数与其它 E 模块一致（enabled / 空态结构 / summary / note）。
    `prev_zt` 为昨日涨停池（调用方从归档取，见 pipeline）。
    """
    a = (cfg or {}).get("E9_auction", {}) or {}
    enabled = bool(a.get("enabled", True))
    sess = session or trading.market_session()
    phase = sess.get("phase", "")
    in_auction = phase in AUCTION_PHASES

    empty = {
        "enabled": enabled, "active": False, "reason": "", "mode": "竞价早读",
        "date": real_date, "phase": phase, "phase_cn": sess.get("label", ""),
        "in_auction": in_auction, "snapshot_time": "",
        "temperature": {}, "review": {}, "movers": {},
        "summary": "", "note": "",
    }
    if not enabled:
        empty["reason"] = "模块已关闭"
        return empty
    if not snaps:
        empty["reason"] = "本轮没有快照数据，读不出竞价情况"
        return empty

    top_n = _int(a.get("top_n"), 10)
    top_prev = _int(a.get("top_prev"), 12)
    try:
        big_gap = float(a.get("big_gap", BIG_GAP))
    except (TypeError, ValueError):
        big_gap = BIG_GAP
    try:
        bias_th = float(a.get("bias_th", 5.0))
    except (TypeError, ValueError):
        bias_th = 5.0

    temp = market_temperature(snaps, bias_th=bias_th)
    if not temp.get("valid"):
        empty["reason"] = ("快照里没有一只有效昨收 —— 竞价时段这批数据可能"
                           "降级了，读不出高开分布")
        return empty

    review = yesterday_review(snaps, prev_zt or [], top_n=top_prev)
    mv = movers(temp.get("_scored") or [], snaps, top_n=top_n, big_gap=big_gap)

    # 快照时间戳（[30]，格式 YYYYMMDDHHMMSS）—— 让用户知道这份读数有多"新"
    ts_raw = ""
    for s in snaps.values():
        t = str(s.get("time") or "")
        if len(t) >= 14:
            ts_raw = max(ts_raw, t) if ts_raw else t
    snap_time = ""
    if len(ts_raw) >= 14:
        snap_time = f"{ts_raw[8:10]}:{ts_raw[10:12]}:{ts_raw[12:14]}"

    parts = [temp["summary"]]
    if in_auction:
        parts.append("★ 现在就在竞价窗口内（09:15–09:25），匹配价还会变；"
                     "09:20 后不可撤单，此后才接近定局。")
    if review.get("n"):
        parts.append(review["summary"])
    summary = " ".join(parts)

    return {
        "enabled": True, "active": True, "reason": "", "mode": "竞价早读",
        "date": real_date, "phase": phase, "phase_cn": sess.get("label", ""),
        "in_auction": in_auction, "snapshot_time": snap_time,
        "temperature": {k: v for k, v in temp.items() if not k.startswith("_")},
        "review": {k: v for k, v in review.items() if k != "all_rows"},
        "movers": mv,
        "summary": summary,
        "note": (
            "**这张卡是开盘前的观察，不是买入指令** —— 与 E5/E6/E7/E8 定位一致。\n"
            "口径与限制（如实标注）：\n"
            "① 竞价时段**没有成交**，所以成交额/换手/量比全是 0 —— "
            "「资金流入」「放量」这类结论这里给不出来，不要在这找。\n"
            "② 竞价匹配价在 09:20 前可挂可撤、09:20–09:25 不可撤单，"
            "所以同一只票几十秒内就会变（实测 09:20 买量 6816→6624）。"
            "本卡只给**当下快照**，看时间戳判断它有多新。\n"
            "③ 「委比/买卖失衡」**刻意不做** —— 竞价时段卖侧几乎恒为 0，"
            "算出来的 99% 失衡是假的（见模块文档里 [50] 委差的实测）。\n"
            "④ 区分不出停牌（全市场成交额都是 0），所以「无竞价数据」"
            "可能只是停牌，不代表异常。"
        ),
    }
