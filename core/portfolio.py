# -*- coding: utf-8 -*-
"""自选股与持仓体检（扩展功能，非野人原体系）。

把「我今天该干什么」落到实处：本地记录持仓（成本/股数/买入日）与自选股，
每次扫描用同一套 M3-M10 规则给每只持仓做「体检」——盈亏 + 该不该走，
直接复用买卖点引擎（S1 退潮清仓 / S4 A字杀 / S6 机构加速 / S7 断板 / S8 庄股）。

灵感来源（GitHub 同类项目）：aiagents-stock / astock_analyzer 等盯盘系统的
「持仓跟踪 + 止盈止损提醒」；东财/同花顺自选股列表。

数据落地：data/portfolio.json（持仓 + 自选）。纯本地、不上行、不构成投资建议。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import fundstyle
from . import paths
from . import trading
from . import universe

PATH = paths.data_path("portfolio.json")


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- 存取

def load() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        obj = {}
    obj.setdefault("holdings", [])
    obj.setdefault("watchlist", [])
    # 老数据可能存的是裸 6 位代码（603466）或券商格式（603466.SH）——
    # 行情接口只认 sh/sz/bj 前缀，读的时候顺手规范一次，历史记录自愈。
    for h in obj["holdings"]:
        if h.get("code"):
            h["code"] = universe.normalize(h["code"])
    for w in obj["watchlist"]:
        if w.get("code"):
            w["code"] = universe.normalize(w["code"])
    return obj


def _save(obj: dict) -> None:
    obj["holdings"] = [h for h in obj.get("holdings", []) if h.get("code")]
    obj["watchlist"] = [w for w in obj.get("watchlist", []) if w.get("code")]
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)


def upsert_holding(code: str, name: str = "", cost: float = 0.0,
                   shares: float = 0.0, date: str = "", note: str = "") -> dict:
    code = universe.normalize(code)
    obj = load()
    for h in obj["holdings"]:
        if h["code"] == code:
            h.update({k: v for k, v in {"name": name, "cost": cost, "shares": shares,
                                        "date": date, "note": note}.items() if v not in ("", None)})
            _save(obj)
            return obj
    obj["holdings"].append({"code": code, "name": name, "cost": _f(cost),
                            "shares": _f(shares),
                            "date": date or datetime.now().strftime("%Y-%m-%d"),
                            "note": note})
    _save(obj)
    return obj


def remove_holding(code: str) -> dict:
    code = universe.normalize(code)
    obj = load()
    obj["holdings"] = [h for h in obj["holdings"] if h["code"] != code]
    _save(obj)
    return obj


def watch_upsert(code: str, name: str = "") -> dict:
    code = universe.normalize(code)
    obj = load()
    if not any(w["code"] == code for w in obj["watchlist"]):
        obj["watchlist"].append({"code": code, "name": name})
    _save(obj)
    return obj


def watch_remove(code: str) -> dict:
    code = universe.normalize(code)
    obj = load()
    obj["watchlist"] = [w for w in obj["watchlist"] if w["code"] != code]
    _save(obj)
    return obj


# ---------------------------------------------------------------- 体检

def _verdict(sell_sigs: list[dict], fund_style: dict) -> tuple[str, str]:
    """卖出信号 → 行动档位。"""
    urgent = [s for s in sell_sigs if s.get("urgency") == "critical"]
    if any(s.get("type") in ("S1", "S4", "S7", "S8") for s in urgent) or urgent:
        return "清仓离场", "critical"
    high = [s for s in sell_sigs if s.get("urgency") == "high"]
    if high:
        return "减仓", "high"
    if sell_sigs:
        return "观察/减半", "medium"
    return "持有", "hold"


def evaluate(snaps: dict, kl: dict, sentiment: dict, volume: dict,
             cfg: dict | None = None) -> dict:
    """对持仓逐只体检，返回盈亏 + 卖出信号 + 行动档位。"""
    obj = load()
    holdings = obj.get("holdings", [])
    watch = obj.get("watchlist", [])
    cfg = cfg or {}

    out = []
    tot_cost = tot_mv = 0.0
    checks = []
    for h in holdings:
        code = h["code"]
        snap = snaps.get(code) or {}
        bars = kl.get(code) or []
        price = _f(snap.get("price"))
        cost = _f(h.get("cost"))
        shares = _f(h.get("shares"))
        pnl_pct = ((price - cost) / cost * 100) if cost > 0 and price > 0 else None
        mv = price * shares if price and shares else 0.0
        cst = cost * shares if cost and shares else 0.0
        tot_cost += cst
        tot_mv += mv

        fs = fundstyle.classify(bars, code, _f(snap.get("float_mv")) / 1e8, cfg) if bars else {}
        pseudo = {"code": code, "name": h.get("name") or snap.get("name", ""),
                  "price": price, "pct": _f(snap.get("pct")),
                  "turnover": _f(snap.get("turnover")),
                  "zt_boards": 0, "fund_style": fs}
        sells = trading.sell_signals(pseudo, bars, sentiment, volume, cfg) if bars else []
        if not bars:
            sells = [{"type": "—", "name": "无K线数据", "priority": "C",
                      "trigger": "未取到该股K线", "action": "手动核对", "urgency": "medium"}]
        verdict, level = _verdict(sells, fs)
        r3 = None
        if len(bars) >= 4:
            c0, c3 = _f(bars[-4].get("close")), _f(bars[-1].get("close"))
            if c0 > 0 and c3 > 0:
                r3 = round((c3 / c0 - 1) * 100, 1)      # 近 3 日涨幅（去弱留强用）
        # 野人「扶老太太下楼梯」：被套（≤-5%）时的向下解套参考。
        # 只对日内有波动的票成立 —— 一字锁死/流动性差的票做不动这个动作。
        unwind = ""
        if pnl_pct is not None and pnl_pct <= -5:
            unwind = ("被套参考「扶老太太下楼梯」（适合日内有波动的票）："
                      "开盘先卖 → 低位（-3%~-5%）接回反复磨；出现高开修复才格局、"
                      "冲高回本线卖一半；不符合预期就解套走人、不恋战")
        out.append({
            "code": code, "name": h.get("name") or snap.get("name", ""),
            "cost": cost, "shares": shares, "date": h.get("date", ""),
            "note": h.get("note", ""), "price": price, "pct": _f(snap.get("pct")),
            "pnl": round(mv - cst, 1) if (mv and cst) else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "mv": round(mv, 1), "verdict": verdict, "level": level,
            "signals": sells, "fund_style": fs.get("type", "unknown"),
            "r3": r3, "unwind": unwind,
        })
        if level in ("critical", "high"):
            checks.append({"level": "red" if level == "critical" else "yellow",
                           "item": f"{out[-1]['name']} 持仓告警：{verdict}",
                           "action": (sells[0].get("action", "") if sells else "")})

    # 自选股快照
    wout = []
    for w in watch:
        s = snaps.get(w["code"]) or {}
        wout.append({"code": w["code"], "name": w.get("name") or s.get("name", ""),
                     "price": _f(s.get("price")), "pct": _f(s.get("pct")),
                     "turnover": _f(s.get("turnover"))})

    total_pnl = (tot_mv - tot_cost) if tot_cost else 0.0
    return {
        "holdings": out, "watchlist": wout,
        "total_cost": round(tot_cost, 1), "total_mv": round(tot_mv, 1),
        "total_pnl": round(total_pnl, 1),
        "total_pnl_pct": round(total_pnl / tot_cost * 100, 2) if tot_cost else None,
        "checks": checks,
        "n_holdings": len(out), "n_watch": len(wout),
        "note": "持仓体检复用 M3-M10 的卖出信号（退潮清仓/A字杀/机构加速/断板/庄股），"
                "纯本地记录，不构成投资建议。",
    }


def rotation_check(holdings_out: list[dict], ml: dict | None = None,
                   stage: str = "") -> list[dict]:
    """去弱留强体检 —— 野人：「手里拿的是后排，前排走得好你就是不换」，
    「去弱留强最好的时间点是市场不好的时候」。

    口径（只用现有数据，零额外请求）：
      · 组内对比：持仓里近 3 日涨幅最低者 vs 最高者，差 ≥8 个百分点且弱者为负
        → 「弱者掉队」提示；
      · 主线明确时，掉队者单独点名（当前主线「XX」，你手里却是 YY）；
      · 退潮期（ebb）措辞加硬 —— 那是执行去弱留强的最佳时点。
    只在持仓 ≥2 只且有有效近3日数据时才判，不凑数、不吓唬人。
    """
    ml = ml or {}
    rows = [(h.get("r3"), h) for h in (holdings_out or [])
            if h.get("r3") is not None]
    if len(rows) < 2:
        return []
    best = max(rows, key=lambda x: x[0])
    worst = min(rows, key=lambda x: x[0])
    gap = round(best[0] - worst[0], 1)
    out = []
    if gap < 8 or worst[0] >= 0:
        return []
    theme = ml.get("theme") or ""
    if stage == "ebb":
        head = (f"去弱留强（退潮期正是换仓时点）：{worst[1].get('name')} 近3日 "
                f"{worst[0]:+.1f}%，组合内最强 {best[1].get('name')} {best[0]:+.1f}%"
                f"（差 {gap} 个点）")
        act = ("掉队的先处理 —— 别主观认定自己手上那个才是核心；"
               "换不动就至少别再加")
    else:
        head = (f"去弱留强：{worst[1].get('name')} 近3日 {worst[0]:+.1f}%，"
                f"组合内最强 {best[1].get('name')} {best[0]:+.1f}%"
                f"（差 {gap} 个点）")
        act = (f"后排换到更强方向{('（当前主线「' + theme + '」）') if theme else ''}；"
               "卖错了要敢认，别为了证明自己没错去补掉队的")
    out.append({"level": "yellow", "item": head, "action": act})
    return out
