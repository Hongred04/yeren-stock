# -*- coding: utf-8 -*-
"""野人《波段体系》—— 中长线波段的入场 / 持有 / 离场判定。

来源：野人哥《波段体系》7 讲（2026-10-12 转写整理）。
本模块只做"波段这一路"，与短线 B/S 体系并列、互不干扰 —— 野哥原话是
**双账户**：大账户做波段，小账户做短线感受水温（"春江水暖鸭先知"）。

七讲要点 → 量化口径：

  ① 第一入场点：5 日线上穿所有均线 + 20 日线**开始拐头向上**；
     强庄沿 5 日线趋势向上，**回踩 10 日线或 20 日线** = 买点。
  ② 第二入场点：强庄沿 5 日线小阴小阳走，某天从 5 日线**打到 20 日线附近**；
     弱庄回踩到 20 日线附近**伴随缩量**（"一定缩量回踩"）。
  ③ 量价铁律：有量才有价，量一定要**爆量**（底部放量=主力行为，散户没这么大能量）；
     **看周线和月线** —— "主力可以作假，但作不了周线月线那么大的假"。
  ④ 第二波接入：第一波涨 30% 后回调（30% 不足以出货，主力没走）→
     5 日线拐头向下、下穿 10/20 日线后企稳 → 5 日线**重新拐头向上** →
     与 10/20/30 日线**高度粘合** → 放量大阳线突破 → **回调时介入（不追）**。
  ⑤ 离场点：**高位放量大涨滞涨** = 有人在出货 = 第一离场点（先把利润装进口袋）；
     出完货**跌不下去**（震荡两天货出完股价不跌）= 有人接得住 = 反而是第二入场点。
  ⑥ 洗盘识别：**缩量回踩 = 洗盘（不走）**；放量破位 = 逻辑坏了（走）。
  ⑦ 心法：没人能买最低点；预期管理（涨在车上、跌有子弹）；不盯分时；
     锁仓 = 看懂主力意图，波段**可以重仓**（与短线不能重仓的根本区别）。

⚠️ 单句摘抄有风险：本模块的判定都是"周期位置 + 量价验证 + 仓位管理"的组合，
   任何一条单独拿出来用都会走样。
"""
from __future__ import annotations

from datetime import datetime

from . import indicators as ind

# 阈值集中在默认配置里（strategy_config 会引用），方便调参而不动逻辑
SWING_DEFAULTS = {
    "enabled": True,
    "turn_days": 3,            # 20 日线"拐头"看几天
    "pullback_tol": 0.02,      # 回踩容忍：触及均线上方 2% 内算回踩
    "burst_ratio": 1.5,        # 爆量：当日量 ≥ N × 近5日均量（不含当日）
    "shrink_ratio": 0.75,      # 缩量：当日量 ≤ N × 近5日均量
    "converge_pct": 0.03,      # 均线粘合：5/10/20/30 均线极差 ÷ 收盘 ≤ 3%
    "wave_gain": 0.30,         # 第二波前提：第一波涨幅 ≥ 30%
    "wave_pull_min": 0.05,     # 回调幅度下界
    "wave_pull_max": 0.35,     # 回调幅度上界（超过就是趋势坏了）
    "stall_pct": 3.0,          # 滞涨：涨跌幅绝对值 ≤ 3%
    "high_pos": 0.70,          # 高位：60 日区间位置 ≥ 0.7
    "high_gain20": 0.25,       # 或近 20 日涨幅 ≥ 25%
    "burst_lookback": 10,      # 近 N 根内出现过爆量（底部放量的痕迹）
    "break_tol": 0.97,         # 收盘跌破 20 日线 × 0.97 视为破位
}


def _cfg(cfg: dict | None) -> dict:
    out = dict(SWING_DEFAULTS)
    a = (cfg or {}).get("M11_swing", {}) or {}
    for k in SWING_DEFAULTS:
        if k in a:
            out[k] = a[k]
    return out


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- 周/月线聚合

def _parse_date(s: str):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _agg(bars: list[dict], keyfn) -> list[dict]:
    """按 keyfn 聚合日K → 周K / 月K。日期取该周期最后一根。"""
    out, cur, cur_key = [], None, None
    for b in bars:
        d = _parse_date(b.get("date", ""))
        if d is None:
            continue
        k = keyfn(d)
        if k != cur_key:
            if cur:
                out.append(cur)
            cur_key = k
            cur = {"date": b.get("date", ""), "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"],
                   "volume": _num(b.get("volume"))}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["date"] = b.get("date", cur["date"])
            cur["volume"] += _num(b.get("volume"))
    if cur:
        out.append(cur)
    return out


def weekly(bars: list[dict]) -> list[dict]:
    return _agg(bars, lambda d: d.isocalendar()[:2])


def monthly(bars: list[dict]) -> list[dict]:
    return _agg(bars, lambda d: (d.year, d.month))


def volume_health(bars: list[dict], weeks: int = 8) -> dict:
    """周线量能健康度 —— "看周线和月线，这句话值一万金"。

    温和放大=健康（有活水）；天量=警惕对倒/出货；萎缩=没资金进来。
    """
    wk = weekly(bars)[-weeks:]
    if len(wk) < 3:
        return {"ok": False, "note": "周线样本不足（需 3 周以上）"}
    vols = [w["volume"] for w in wk]
    base = sum(vols[:-1]) / max(len(vols) - 1, 1)
    r = (vols[-1] / base) if base > 0 else 0.0
    if r >= 2.2:
        state, good = "天量（警惕对倒/出货）", False
    elif r >= 1.15:
        state, good = "温和放大（健康·有活水）", True
    elif r <= 0.7:
        state, good = "萎缩（没新资金进来）", False
    else:
        state, good = "平稳", True
    mo = monthly(bars)
    mo_note = ""
    if len(mo) >= 2:
        mo_note = ("月线走强" if mo[-1]["close"] > mo[0]["close"] else "月线偏弱")
    return {"ok": True, "weeks": len(wk), "vol_ratio": round(r, 2),
            "state": state, "good": good, "monthly": mo_note,
            "note": f"近{len(wk)}周量能{state}" + (f" · {mo_note}" if mo_note else "")}


# ---------------------------------------------------------------- 量能 / 均线

def volume_burst(bars: list[dict], cfg: dict) -> dict:
    """当日量能状态：爆量 / 缩量 / 正常（基准是"不含当日"的近 5 日均量）。"""
    if len(bars) < 6:
        return {"state": "unknown", "ratio": None}
    v = _num(bars[-1].get("volume"))
    base = sum(_num(b.get("volume")) for b in bars[-6:-1]) / 5
    if base <= 0:
        return {"state": "unknown", "ratio": None}
    r = v / base
    if r >= cfg["burst_ratio"]:
        st = "burst"
    elif r <= cfg["shrink_ratio"]:
        st = "shrink"
    else:
        st = "normal"
    return {"state": st, "ratio": round(r, 2)}


def recent_burst(bars: list[dict], cfg: dict) -> bool:
    """近 N 根内出现过爆量 —— "底部放量 = 主力行为"的痕迹。"""
    lb = int(cfg["burst_lookback"])
    seg = bars[-lb:]
    if len(seg) < 6:
        return False
    for i in range(5, len(seg)):
        sub = seg[:i + 1]
        v = _num(sub[-1].get("volume"))
        base = sum(_num(b.get("volume")) for b in sub[-6:-1]) / 5
        if base > 0 and v / base >= cfg["burst_ratio"]:
            return True
    return False


def ma_snapshot(bars: list[dict], cfg: dict) -> dict:
    """均线状态：5日线上穿所有均线 / 20日线拐头向上 / 均线粘合度。"""
    if len(bars) < 21:
        return {"ok": False}
    closes = [b["close"] for b in bars]
    m = {n: ind.ma(closes, n) for n in (5, 10, 20, 30, 60)}
    if not m[5] or not m[20]:
        return {"ok": False}
    above_all = all(m[5] > m[n] for n in (10, 20, 30, 60) if m[n])
    ser20 = ind.ma_series(closes, 20)
    td = int(cfg["turn_days"])
    prev20 = ser20[-1 - td] if len(ser20) > td else None
    turning_up = bool(prev20 and m[20] > prev20)
    vals = [v for v in (m[5], m[10], m[20], m[30]) if v]
    cv = ((max(vals) - min(vals)) / closes[-1]) if (vals and closes[-1]) else None
    return {"ok": True, "ma": m, "above_all": bool(above_all),
            "turning_up": turning_up,
            "converge_pct": round(cv, 4) if cv is not None else None,
            "converged": bool(cv is not None and cv <= cfg["converge_pct"])}


def _pos_and_gain20(bars: list[dict]) -> tuple[float, float]:
    """60 日区间位置 + 近 20 日涨幅。"""
    seg = bars[-60:] if len(bars) >= 60 else bars
    hi = max(b["high"] for b in seg)
    lo = min(b["low"] for b in seg)
    close = bars[-1]["close"]
    pos = (close - lo) / (hi - lo) if hi > lo else 0.5
    gain20 = 0.0
    if len(bars) > 21 and bars[-21]["close"] > 0:
        gain20 = close / bars[-21]["close"] - 1
    return pos, gain20


# ---------------------------------------------------------------- 三个入场点

def first_entry(bars: list[dict], cfg: dict) -> dict | None:
    """第一入场点：5日线上穿所有均线 + 20日线拐头向上 + 回踩10/20日线。"""
    ms = ma_snapshot(bars, cfg)
    if not ms.get("ok") or not (ms["above_all"] and ms["turning_up"]):
        return None
    close = bars[-1]["close"]
    low = _num(bars[-1].get("low"), close)
    tol = float(cfg["pullback_tol"])
    m10, m20 = ms["ma"][10], ms["ma"][20]
    hit = None
    if m10 and low <= m10 * (1 + tol):
        hit = ("10日线", m10)
    if m20 and low <= m20 * (1 + tol) and (hit is None or m20 > hit[1]):
        hit = ("20日线", m20)
    if hit is None:
        return None
    name, lvl = hit
    if close < lvl * (1 - tol):          # 有效跌破就不是回踩了
        return None
    v = volume_burst(bars, cfg)
    return {"type": "W1", "name": "波段第一入场点", "line": name,
            "level": round(lvl, 2), "vol": v,
            "burst_ok": (v["state"] == "burst") or recent_burst(bars, cfg),
            "converged": ms["converged"]}


def second_entry(bars: list[dict], cfg: dict) -> dict | None:
    """第二入场点：从5日线打到20日线附近（强庄）；或缩量回踩20日线（弱庄）。"""
    ms = ma_snapshot(bars, cfg)
    if not ms.get("ok"):
        return None
    close = bars[-1]["close"]
    low = _num(bars[-1].get("low"), close)
    m5, m20 = ms["ma"][5], ms["ma"][20]
    if not m20:
        return None
    tol = float(cfg["pullback_tol"])
    if not (low <= m20 * (1 + tol) and close >= m20 * (1 - tol)):
        return None                       # 不在 20 日线附近
    v = volume_burst(bars, cfg)
    return {"type": "W2", "name": "波段第二入场点", "line": "20日线",
            "level": round(m20, 2), "vol": v,
            "from_ma5": bool(m5 and low < m5),
            "shrink": v["state"] == "shrink"}


def second_wave(bars: list[dict], cfg: dict) -> dict | None:
    """第二波接入点：第一波涨≥30% → 回调 → 5日线重新拐头 + 均线粘合。"""
    if len(bars) < 30:
        return None
    seg = bars[-60:] if len(bars) >= 60 else bars
    hi = max(b["high"] for b in seg)
    hi_i = max(range(len(seg)), key=lambda i: seg[i]["high"])
    if hi_i <= 0:
        return None
    lo_before = min(b["low"] for b in seg[:hi_i + 1])
    if lo_before <= 0:
        return None
    gain = hi / lo_before - 1
    if gain < float(cfg["wave_gain"]):
        return None                       # 第一波没涨够，谈不上第二波
    close = bars[-1]["close"]
    pull = (hi - close) / hi if hi > 0 else 0
    if not (float(cfg["wave_pull_min"]) <= pull <= float(cfg["wave_pull_max"])):
        return None
    closes = [b["close"] for b in bars]
    ser5 = ind.ma_series(closes, 5)
    turn5 = bool(len(ser5) > 3 and ser5[-1] and ser5[-4] and ser5[-1] > ser5[-4])
    ms = ma_snapshot(bars, cfg)
    v = volume_burst(bars, cfg)
    return {"type": "W3", "name": "波段第二波接入点", "gain": round(gain, 3),
            "pullback": round(pull, 3), "turn5": turn5,
            "converged": bool(ms.get("converged")),
            "vol": v,
            # 可动手的条件：5日线重新拐头 + 均线高度粘合（放量大阳突破后再等回调）
            "ready": bool(turn5 and ms.get("converged"))}


# ---------------------------------------------------------------- 离场 / 洗盘

def exit_point(bars: list[dict], cfg: dict) -> dict | None:
    """第一离场点：**高位放量大涨滞涨** = 有人在出货（"放量100多亿只涨2个点"）。"""
    if len(bars) < 25:
        return None
    close = bars[-1]["close"]
    prev = bars[-2]["close"]
    if prev <= 0:
        return None
    pct = (close - prev) / prev * 100
    if abs(pct) > float(cfg["stall_pct"]):
        return None                       # 真大涨/大跌都不叫"滞涨"
    v = volume_burst(bars, cfg)
    if v["state"] != "burst":
        return None                       # 必须是放量
    pos, gain20 = _pos_and_gain20(bars)
    if not (pos >= float(cfg["high_pos"]) or gain20 >= float(cfg["high_gain20"])):
        return None                       # 必须是高位
    return {"type": "WS1", "name": "高位放量滞涨（第一离场点）",
            "pct": round(pct, 2), "vol_ratio": v["ratio"],
            "pos": round(pos, 2), "gain20": round(gain20, 3)}


def washout_or_break(bars: list[dict], cfg: dict) -> dict:
    """缩量回踩=洗盘（不走）；放量破位=逻辑坏了（走）。"""
    if len(bars) < 25:
        return {"state": "unknown", "note": ""}
    ms = ma_snapshot(bars, cfg)
    m20 = (ms.get("ma") or {}).get(20) if ms.get("ok") else None
    if not m20:
        return {"state": "unknown", "note": ""}
    close = bars[-1]["close"]
    v = volume_burst(bars, cfg)
    if close < m20 * float(cfg["break_tol"]):
        if v["state"] == "shrink":
            return {"state": "washout",
                    "note": "跌破20日线但缩量 —— 更像洗盘，先别急着砍，看两天"}
        return {"state": "break",
                "note": "放量跌破20日线 —— 逻辑坏了，走（错不在你第二天跌了，错在系统）"}
    if v["state"] == "shrink":
        return {"state": "washout", "note": "缩量回踩且站在20日线上方 —— 洗盘，不动手"}
    return {"state": "normal", "note": ""}


# ---------------------------------------------------------------- 综合

STAGE_CN = {
    "idle": "未启动",
    "first": "第一入场点",
    "second": "第二入场点",
    "wave2": "第二波接入",
    "hold": "主升持有",
    "stall": "高位滞涨·离场",
    "break": "破位·离场",
    "wash": "洗盘",
}


def evaluate(bars: list[dict], code: str = "", cfg: dict | None = None) -> dict:
    """波段体系综合判定。返回可直接给前端渲染的结构。"""
    c = _cfg(cfg)
    if not c.get("enabled", True) or not bars or len(bars) < 25:
        return {"ok": False, "stage": "idle", "note": "K线不足 25 根，波段判定做不了"}

    ms = ma_snapshot(bars, c)
    vh = volume_health(bars)
    wb = washout_or_break(bars, c)
    ex = exit_point(bars, c)
    w1 = first_entry(bars, c)
    w2 = second_entry(bars, c)
    w3 = second_wave(bars, c)

    # 阶段判定：离场 > 破位 > 入场点 > 洗盘 > 持有
    if ex:
        stage = "stall"
    elif wb["state"] == "break":
        stage = "break"
    elif w1:
        stage = "first"
    elif w2:
        stage = "second"
    elif w3 and w3.get("ready"):
        stage = "wave2"
    elif wb["state"] == "washout":
        stage = "wash"
    else:
        stage = "hold"

    entry = w1 or w2 or (w3 if (w3 and w3.get("ready")) else None)
    pos, gain20 = _pos_and_gain20(bars)

    # 纪律清单（心法 → 可核对的条目）
    checklist = [
        {"t": "不盯分时", "d": "跌 5% 着急、跌 10% 恐惧 = 把波段当短线做，必亏"},
        {"t": "预期管理", "d": "上涨时保证在车上，下跌时保证有子弹和口粮"},
        {"t": "没人能买最低点", "d": "只能买相对低点；想买最低点的人不适合在股市博弈"},
        {"t": "买在相对低点 + 扛住洗盘震仓", "d": "这是迎接主升的唯一途径"},
        {"t": "锁仓才可重仓", "d": "看懂主力意图 = 锁仓；波段可以重仓，短线不行"},
        {"t": "买在分歧转一致，卖在一致转分歧", "d": "不是「买在分歧卖在一致」——顺序反了"},
        {"t": "看周线和月线", "d": "日线可骗，周月线温和的量才是真相"},
    ]

    note_bits = []
    if vh.get("ok"):
        note_bits.append(vh["note"])
    if ms.get("ok"):
        note_bits.append(f"5日线{'已' if ms['above_all'] else '未'}上穿所有均线，"
                         f"20日线{'拐头向上' if ms['turning_up'] else '未拐头'}")
        if ms.get("converge_pct") is not None:
            note_bits.append(f"均线粘合度 {ms['converge_pct']*100:.1f}%"
                             f"{'（高度粘合·时刻关注）' if ms['converged'] else ''}")
    if wb.get("note"):
        note_bits.append(wb["note"])

    return {
        "ok": True,
        "stage": stage,
        "stage_cn": STAGE_CN.get(stage, stage),
        "entry": entry,
        "w1": w1, "w2": w2, "w3": w3,
        "exit": ex,
        "washout": wb,
        "ma": ms,
        "volume": vh,
        "pos": round(pos, 2), "gain20": round(gain20, 3),
        "checklist": checklist,
        "note": "；".join(note_bits) or "波段条件未形成",
        "creed": "波段短则3个月、长则半年 —— 买的是信仰、逻辑、题材",
    }
