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


# ---------------------------------------------------------------- 打法的分工

# 「会做的做补涨，不会做的做波段」—— 这句话本身没错，但**补涨不是一个可以
# 事先判定的买点**。2026-10 用 18 个交易日归档做过回测（结论已写入下面
# playbook 的理由里），口径如下：
#
#   假设：龙头高位滞涨后，同题材低位票会补涨。
#   测法：龙头（≥N 板且当日不再涨停）→ 次日同题材的新面孔（昨日未涨停）→
#         再次日是否涨停；对照是「同日全体涨停票」的市场基线。
#
#   结果（三个测法方向一致，效应为负）：
#     · ≥2板门槛  同题材新面孔 11.4%  vs 基线 21.6%  → −10.2pp
#     · ≥3板门槛  同题材新面孔  0.0%  vs 基线 21.6%  → −21.6pp
#     · 放宽到题材家族（1166 个样本）15.4% vs 21.6% → −6.2pp
#     · 直接对照（同题材 vs 非题材，都只看新面孔）    → −5.5pp / −16.6pp
#   原本看起来有的 +8.7pp 超额，控制住"连板惯性"后就消失了 —— 那 30 个百分点
#   全部来自"昨天涨停今天还涨停"的票（命中率 51.6%），不是补涨。
#
#   附带证伪两个细节：①「名字共字」−1.9pp / −3.0pp，无正效应；
#   ② 用户举的案例（九阳股份 sz002242 断板 + 阳光乳业 sz001318）经查
#      ——阳光乳业 09-30 就已经涨停过，10-13 是**复燃**不是补位；两者题材
#      （家电/电子商务 vs 饮料乳品）毫无关系；且九阳 10-13 是**断板**不是
#      "高位震荡"。名字同字纯属巧合（当日涨停池 67 只里含"阳"字的 3 只，
#      全市场含"阳"字 47 只）。
#
# 所以：**不新增补涨模块**（加了会给出比随机还低 5~17 个百分点的假信号），
# 也不放宽 E6 停牌补位的触发条件。这里只做一件不引入假信号的事 —— 把
# 「这只票该用哪一路打法」讲清楚，让用户知道自己在做的是哪一路。
PLAY_CN = {"swing": "波段", "short": "短线", "avoid": "不做"}

# 高位股的定义（"主力在高位做出货准备"的那个"高位"）
_HIGH_BOARDS = 4          # 连板达到这个数，位置已经在高位
_HIGH_GAIN20 = 0.50       # 或近 20 日涨幅达到这个数


def verdict(sw: dict, rg: dict | None = None) -> dict:
    """波段「能不能买」结论 —— 把「节点 × 波段阶段」翻译成一句人话。

    详情页顶部要**直接回答**：能不能买、为什么、依据是什么（以前只罗列
    阶段与入场点，用户得自己拼结论）。can ∈ yes/watch/no。
      · 节点不允许做长 → 不能买（是节点的问题，不是这只票形态的问题）；
      · 阶段是入场点 且 节点允许 → 可以买；
      · 洗盘中 / 滞涨破位 / 无入场点 → 观察或不能买。
    口径与 trading 的波段买点门禁同源（节点都读 core/regime.py），不会打架。
    """
    rg = rg or {}
    if not sw or not sw.get("ok"):
        return {"can": "no", "label": "数据不足",
                "reason": (sw or {}).get("note") or "K线不足 25 根，波段判定做不了",
                "why": ""}
    stage = sw.get("stage")
    node = rg.get("node_label", "")
    mode = rg.get("mode_name", "")
    if not rg.get("allow_swing", True):
        return {"can": "no", "label": "当前节点不做长",
                "reason": f"{node}（{mode}）—— 波段（做长）当前不成立，题材只按短线纪律做",
                "why": "同样的票，主升期捂是机会，退潮/缩量期捂是绞肉机 —— "
                       "不是标的的事，是节点的事"}
    if stage in ("first", "second", "wave2") and sw.get("entry"):
        e = sw["entry"]
        if e.get("type") == "W3":
            reason = (f"第二波接入：第一波涨 {e.get('gain', 0)*100:.0f}%、"
                      f"回调 {e.get('pullback', 0)*100:.0f}%，5日线重新拐头、"
                      f"均线{'高度粘合' if e.get('converged') else '尚未粘合'} —— "
                      "等放量大阳突破后的**回调**介入")
        elif e.get("type") == "W2":
            reason = (f"第二入场点：回踩 20 日线 {e.get('level')} 附近介入"
                      f"（{'缩量回踩·弱庄形态更可靠' if e.get('shrink') else '量能待确认'}）")
        else:
            reason = (f"第一入场点：5日线上穿所有均线 + 20日线拐头向上，"
                      f"回踩 {e.get('line')} {e.get('level')} 建第一笔"
                      f"（{'爆量确认' if e.get('burst_ok') else '量能待确认'}）")
        return {"can": "yes", "label": f"可以买（{e.get('name', '波段入场点')}）",
                "reason": reason,
                "why": "波段是平常打法 —— 买在相对低点、扛住洗盘震仓，是迎接主升的途径"}
    if stage == "wash":
        return {"can": "watch", "label": "观察（洗盘中）",
                "reason": sw.get("note") or "缩量回踩=洗盘，等企稳信号再说",
                "why": "缩量回踩是洗盘、放量破位才走 —— 没企稳别急着接"}
    if stage in ("stall", "break"):
        return {"can": "no", "label": "不能买（离场信号）",
                "reason": sw.get("note") or ("高位放量滞涨" if stage == "stall" else "放量破位"),
                "why": "高位放量滞涨=有人出货，先落袋；放量破位=逻辑坏了，先离场"}
    return {"can": "watch", "label": "持有（无新入场点）",
            "reason": sw.get("note") or "当前不在入场点",
            "why": "已持有则沿 5 日线持有；小阳线爬坡不动手，突然加速=派发"}


def playbook(sw: dict, p: dict | None = None, rg: dict | None = None) -> dict:
    """「这只票该用哪一路打法」—— 把双账户的分工讲清楚。

    野哥的心法是**双账户**：大账户做波段（锁仓、可重仓），小账户做短线
    （感受水温）。用户问的「会做的做补涨，不会做的做波段」说的是同一件事的
    另一面 —— 知道自己做的是哪一路。但**补涨这一路经过回测不成立**（见上方
    注释），所以这里给出的三路是：波段 / 短线 / 不做。

    与 verdict() 的分工：verdict 回答「能不能买」（can ∈ yes/watch/no），
    playbook 回答「用哪一路做」。两者都读同一个 sw，不另起口径。
    """
    rg = rg or {}
    p = p or {}
    if not sw or not sw.get("ok"):
        return {"play": "avoid", "play_cn": PLAY_CN["avoid"],
                "label": "数据不足", "reason": (sw or {}).get("note") or "波段判定做不了",
                "high": False, "boards": None}

    stage = sw.get("stage")
    boards = p.get("zt_boards")
    try:
        boards = int(boards) if boards is not None else None
    except (TypeError, ValueError):
        boards = None
    gain20 = _num(sw.get("gain20"))
    # 高位 = 连板够高 或 近 20 日涨幅够大。注意 gain20 来自 K 线，
    # 连板数来自当日快照 —— 两者任一成立即算高位（宁可早提醒）。
    high = bool((boards is not None and boards >= _HIGH_BOARDS)
                or gain20 >= _HIGH_GAIN20)

    # 已在高位 —— 这里是「别做补涨」提醒的落点：高位股自己就该考虑离场，
    # 更不该去接它的同题材低位票（回测里这一类胜率最低）。
    if high or stage in ("stall", "break"):
        if stage in ("stall", "break"):
            tail = "（高位放量滞涨=有人出货，先落袋）" if stage == "stall" \
                else "（放量破位=逻辑坏了，先离场）"
        else:
            tail = "（位置已经很高，随时可能派发）"
        return {"play": "avoid", "play_cn": PLAY_CN["avoid"],
                "label": "高位·不做补涨",
                "reason": f"已{'连板 ' + str(boards) + ' 板' if boards is not None and boards >= _HIGH_BOARDS else '处高位'}"
                          f"{tail}。别在这只票的同题材里找低位票接 —— "
                          "回测过：同题材新面孔次日涨停率比市场基线低 5~17 个百分点，"
                          "龙头一断，题材的关注度和承接资金一起退，低位票多是补跌前的最后挣扎",
                "high": True, "boards": boards}

    # 节点不允许做长 —— 但短线仍可做（这是双账户的意义：大账户歇着，小账户试水温）
    if not rg.get("allow_swing", True):
        return {"play": "short", "play_cn": PLAY_CN["short"],
                "label": "只做短线",
                "reason": f"{rg.get('node_label', '当前节点')}（{rg.get('mode_name', '')}）"
                          "不允许做长 —— 波段这一路今天不成立，要做只按短线纪律做"
                          "（不接力、不重仓、亏了就走）",
                "high": False, "boards": boards}

    # 入场点 + 节点允许 → 波段（这是"会做的做波段"那一半，也正是当前系统已有的能力）
    if stage in ("first", "second", "wave2") and sw.get("entry"):
        e = sw["entry"]
        return {"play": "swing", "play_cn": PLAY_CN["swing"],
                "label": f"按波段做（{e.get('name', '入场点')}）",
                "reason": "在波段入场点上、节点也允许做长 —— "
                          "买在相对低点、扛住洗盘震仓，是迎接主升的途径",
                "high": False, "boards": boards}

    if stage == "wash":
        return {"play": "swing", "play_cn": PLAY_CN["swing"],
                "label": "波段·等企稳",
                "reason": "缩量回踩是洗盘、放量破位才走 —— 已持有不动手，空仓等企稳信号",
                "high": False, "boards": boards}

    # 其余（无入场点的持有段）—— 不主动开新仓，也不劝退
    return {"play": "watch", "play_cn": "观察",
            "label": "无入场点·先看",
            "reason": "当前不在波段入场点上 —— 已持有沿 5 日线持有，"
                      "新仓等回踩到位再说",
            "high": False, "boards": boards}


def evaluate(bars: list[dict], code: str = "", cfg: dict | None = None,
             p: dict | None = None, rg: dict | None = None) -> dict:
    """波段体系综合判定。返回可直接给前端渲染的结构。

    p / rg 是可选入参：给了就算一份 playbook（打法分工），没给就不算。
    刻意不做成必需参数 —— 老的调用方（测试、探针）传两个参数仍然能跑。
    """
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

    out = {
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
    # 打法分工（波段/短线/不做）—— 只在调用方给了 p / rg 时才算，
    # 保证 evaluate(bars, code, cfg) 这种老调法行为不变。
    if p is not None or rg is not None:
        out["playbook"] = playbook(out, p, rg)
    return out
