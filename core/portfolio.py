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
from . import indicators as ind
from . import paths
from . import regime
from . import trading
from . import universe

PATH = paths.data_path("portfolio.json")


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- 卖出参考价
# ★ 为什么要有这一段：原来持仓页只给一句「无条件清仓」，用户看完仍然不知道
#   「今天开盘就卖？还是等反弹到某个价再卖？」—— 信号是判断，不是动作。
#   这里把每只持仓的卖出条件翻译成**具体价位 + 到什么价做什么**，
#   全部来自 K 线里已有的均线/前高/成本，不引入任何新指标。
def exit_plan(bars: list[dict], cost: float, price: float) -> dict:
    """给一只持仓算「卖出参考价 + 触发条件 + 到什么价做什么」。

    返回 {levels:[{label, price, what, tone}], stop, stop_pct, note}。
    所有价位都来自 K 线（MA5/MA10/MA20/近20日高点/成本），没有就如实留空 ——
    ★ 宁可少给一条，也不要编一个数字让用户照着挂单。
    """
    out: dict = {"levels": [], "stop": None, "stop_pct": None, "note": ""}
    if not bars or not price:
        out["note"] = "没有足够的K线数据，算不出参考价 —— 不要凭这张表的空白做决定。"
        return out
    closes = [b.get("close") for b in bars if b.get("close")]
    if len(closes) < 5:
        out["note"] = "K线不足5根，算不出均线参考位。"
        return out

    # ★★ 两个「现价」必须分清 —— 这是这里最容易错的地方：
    #   price 参数是**实时快照价**（盘中在动），K线收盘价是**上一根已收盘**的。
    #   拿实时价去和「由收盘价算出的均线」比大小，盘中会得出完全相反的结论
    #   （实测：茅台实时 1285 > MA10 1259 → 本该显示「跌破才走」，
    #    却因为拿实时价和滞后一天的K线混算而报成「已跌破」）。
    #   所以：**判定用最后一根K线收盘价**（与均线同源、同口径），
    #   实时价只用来告诉用户「现在多少钱」，两者不一致时如实说明。
    last_close = closes[-1]
    intrady = abs(price - last_close) > max(0.01, last_close * 0.0005)
    out["last_close"] = round(last_close, 2)
    out["intraday_diff"] = round(price - last_close, 2) if intrady else 0.0

    ma5 = ind.ma(closes, 5)
    ma10 = ind.ma(closes, 10) if len(closes) >= 10 else None
    ma20 = ind.ma(closes, 20) if len(closes) >= 20 else None

    highs = [b.get("high") or b.get("close") for b in bars[-20:]]
    hi20 = max(h for h in highs if h) if any(highs) else None

    lv: list[dict] = []

    # ★ 关键：均线可能在收盘价**上方**（票在回调，或已经跌破）。
    #   这时候再说「跌破 10.62 就走」是错的 —— 收盘价 9.75 早就在它下面了，
    #   用户照做会以为「还没到」而漏掉真正的危险。所以每个价位先判
    #   「收盘价站在这条线之上还是之下」，用不同的话说。
    #   （比较对象一律是 last_close，不是实时价 —— 见上面的长注释）
    #
    #   ★★ 这里踩过一个方向写反的坑，记下来：
    #      最初写成 _rel(p) 返回「p 在收盘价的上/下方」（p>close→up），
    #      然后拿 `if _rel(ma10) == "dn"` 当「已跌破」用 —— 方向正好反了。
    #      ma10 < close（正常持有状态）会被判成「已跌破」，而真正跌破
    #      （ma10 > close）反而显示「跌破就走」。实测茅台现价 1284.96、
    #      MA10 1259.30（明明在下方，安全）却报「⚠ 已跌破」。根因是
    #      属性写的是**均线**的位置，条件问的却是**价格**的位置。
    #      现在改成 broken(p) 直接回答「价格是否已在该线下方」，不留歧义。
    def broken(p: float) -> bool:
        """收盘价是否已经跌到这条线下方（= 该线在收盘价上方）。"""
        return p > last_close

    # ① 近20日高点：只在上方时才有「突破」的意义
    if hi20 and last_close >= hi20 * 0.985:
        lv.append({"label": "近20日高点", "price": round(hi20, 2), "tone": "org",
                   "rel": "up" if hi20 > last_close else "at",
                   "what": f"已经贴近前高 {hi20:.2f}。冲过去且站稳 → 拿着；"
                           f"冲到这附近上不去（放量不涨）→ 先减一半"})

    # ② 持有底线：MA5
    if ma5:
        if broken(ma5):
            # 收盘已在5日线下方：这是「反抽确认」而不是「跌破预警」
            lv.append({"label": "5日线（已跌破）", "price": round(ma5, 2),
                       "tone": "org", "rel": "dn",
                       "what": f"收盘价已经跌到 5 日线 {ma5:.2f} 下方 → 短线走弱成立。"
                               f"反弹到 {ma5:.2f} 附近上不去就减仓（这是最后的体面离场点）"})
        else:
            lv.append({"label": "5日线（短期生命线）", "price": round(ma5, 2),
                       "tone": "blu", "rel": "up",
                       "what": f"收盘跌破 {ma5:.2f} → 说明短线走弱，减仓；"
                               f"第二天站不回来就清掉"})

    # ③ 硬止损：MA10
    if ma10:
        if broken(ma10):
            lv.append({"label": "10日线（已跌破）", "price": round(ma10, 2),
                       "tone": "red", "rel": "dn",
                       "what": f"⚠ 收盘价 {last_close:.2f} 已经在 10 日线 {ma10:.2f} 下方 —— "
                               f"止损条件已经触发，不是「等跌到」。"
                               f"反抽到 {ma10:.2f} 是减仓机会，不是回本希望"})
        else:
            lv.append({"label": "10日线（止损线）", "price": round(ma10, 2),
                       "tone": "red", "rel": "up",
                       "what": f"收盘跌破 {ma10:.2f} → 无条件走，别等反弹、别补仓"})
        out["stop"] = round(ma10, 2)
        out["stop_pct"] = round((ma10 / last_close - 1) * 100, 2)
    elif ma5 and broken(ma5):
        # 没有 MA10 时用 MA5 当止损，否则「止损价」这一栏会是空的
        out["stop"] = round(ma5, 2)
        out["stop_pct"] = round((ma5 / last_close - 1) * 100, 2)

    # ④ 保本线：只有浮盈时才提 —— 亏着谈保本没意义
    if cost > 0 and last_close > cost:
        lv.append({"label": "保本线（你的成本）", "price": round(cost, 2),
                   "tone": "grn", "rel": "dn",
                   "what": f"回落到成本 {cost:.2f} 附近 → 至少把本金拿回来。"
                           f"已经赚了的钱再亏回去，比没赚过更伤"})

    # ⑤ 深套：给解套参考而不是止损（早过了止损位，现在砍是最差的选择）
    if cost > 0 and last_close <= cost * 0.92:
        lv.append({"label": "解套参考（成本）", "price": round(cost, 2),
                   "tone": "org", "rel": "up",
                   "what": f"已亏 {(last_close/cost-1)*100:.1f}%。反弹到成本 {cost:.2f} 附近"
                           f"是减仓机会 —— 别指望一次回本，先降仓位"})

    if ma20:
        if broken(ma20):
            lv.append({"label": "20日线（已跌破）", "price": round(ma20, 2),
                       "tone": "red", "rel": "dn",
                       "what": (f"收盘价已在 20 日线 {ma20:.2f} 下方 —— 中期趋势已经坏了，"
                                f"这只票不是「等反弹」的问题，是仓位该降到最低的问题")})
        else:
            lv.append({"label": "20日线（趋势线）", "price": round(ma20, 2),
                       "tone": "gry", "rel": "up",
                       "what": (f"跌破 {ma20:.2f} 意味着中期趋势坏了 —— "
                                f"这时候不是止损问题，是「不该再拿」的问题")})

    # 按与收盘价的距离排序：最近的先看（用户最该盯的就是最近那个）
    lv.sort(key=lambda x: abs(x["price"] - last_close))
    out["levels"] = lv
    # ★ 止损已触发时必须在 note 里点明，否则用户看到「止损价 10.62 / 现价 10.28」
    #   只会以为「还没到」，而事实正好相反。
    # ★ 注意：这两条 note 是**互斥**的 —— 早前写成 if/elif，但 elif 分支的
    #   `not out["note"]` 与上面的判断撞车，会出现「止损已跌破」和「跌破止损线
    #   当天就走」同时挂在一条记录上的自相矛盾文案。
    if out["stop"] and last_close < out["stop"]:
        out["note"] = (f"⚠ 止损线 {out['stop']:.2f} 已经被跌破了（收盘 {last_close:.2f}）—— "
                       f"止损不是「等跌到」，是「已经到了」。按纪律应该已经离场；"
                       f"还没走的话，反抽到 {out['stop']:.2f} 附近就是最后的减仓机会。")
    else:
        out["note"] = ("价位按离收盘价的远近排 —— 先盯第一条。"
                       "跌破止损线当天就走，不要留到第二天看。")
    # 盘中提示：收盘价是上一根K线的，实时价比它高/低要说清楚，
    # 否则用户拿实时价对照这些线会得出错误结论。
    if intrady:
        out["note"] += (f"（注意：这些线按收盘价 {last_close:.2f} 算，"
                        f"现在实时价 {price:.2f}，{'+' if price > last_close else ''}"
                        f"{price - last_close:.2f} —— 盘中看的是实时价，"
                        f"但**判定要等收盘**。）".replace("**", ""))
    return out


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
    """卖出信号 → 行动档位。

    critical 一律清仓（S1 退潮清仓 / S4 A字杀 / S7 断板节奏破坏 / S8 庄股）。
    原实现写成 `any(type in (...)) or urgent`，其中 any(...) 恒被 urgent 短路，
    是死代码，这里按真实语义收拢。
    """
    if any(s.get("urgency") == "critical" for s in sell_sigs):
        return "清仓离场", "critical"
    if any(s.get("urgency") == "high" for s in sell_sigs):
        return "减仓", "high"
    if sell_sigs:
        return "观察/减半", "medium"
    return "持有", "hold"


# 档位 → 人话。★ 用户反馈「不够好理解要说人话」：原来的「清仓离场/观察/减半」
# 是行话，看完不知道今天到底该干什么。这里给出「今天做什么」的一句话。
_VERDICT_PLAIN = {
    "critical": "今天就走 —— 别再等了",
    "high": "今天减一半，剩下的盯着止损线",
    "medium": "先不动，但别加仓，盯住下面的参考价",
    "hold": "拿着就行，没有卖出信号",
}


def plain_action(level: str, verdict: str, exitp: dict) -> str:
    """把档位翻译成一句可执行的话（带具体价位）。"""
    base = _VERDICT_PLAIN.get(level, verdict)
    stop = exitp.get("stop") if exitp else None
    if level in ("critical", "high") and stop:
        # ★ 「跌破 X 更是必须走」只在**止损还没破**时才是对的。
        #   止损已经破了（收盘 < stop）还说「跌破就更要走」等于告诉用户
        #   「现在还能等」—— 和事实正好相反。破了就直接说破了。
        close = exitp.get("last_close")
        if close and close < stop:
            return f"{base}（止损线 {stop:.2f} 已经跌破，不是「等跌到」）"
        return f"{base}（跌破 {stop:.2f} 更是必须走）"
    return base


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
    # 节点（今日打法，单一出处 core/regime.py）—— 用来判「今天适不适合做长」
    rg = regime.classify(sentiment, volume)
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
        # zt_boards 不能写死 0：sell_signals 的 S2（龙头止盈，≥5 连板）就是靠它判的，
        # 写死 0 等于持仓体检永远不出止盈信号。持仓拿不到当日涨停池，就用
        # K线自己数连板（口径与候选股一致）。
        boards_now = ind.consecutive_boards(bars, code) if bars else 0
        pseudo = {"code": code, "name": h.get("name") or snap.get("name", ""),
                  "price": price, "pct": _f(snap.get("pct")),
                  "turnover": _f(snap.get("turnover")),
                  "zt_boards": boards_now, "fund_style": fs}
        sells = trading.sell_signals(pseudo, bars, sentiment, volume, cfg) if bars else []
        if not bars:
            sells = [{"type": "—", "name": "无K线数据", "priority": "C",
                      "trigger": "未取到该股K线", "action": "手动核对", "urgency": "medium"}]
        verdict, level = _verdict(sells, fs)
        # 卖出参考价：把「该不该走」落成「到哪个价做什么」
        exitp = exit_plan(bars, cost, price) if bars else {
            "levels": [], "stop": None, "stop_pct": None,
            "note": "没有K线数据，算不出参考价。"}
        plain = plain_action(level, verdict, exitp)
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
        # 做长警示：非做长节点下，持仓里的波段属性票 —— 做长=绞肉机。
        # 波段属性（口径对齐 style_fit）：机构主导 / 中大盘（≥150亿）/ 卖出信号
        # 里出现 swing 口径 —— 三者任一即视为波段仓。
        _mv_yi = _f(snap.get("float_mv")) / 1e8
        _is_swing = (fs.get("type") == "institutional" or _mv_yi >= 150.0
                     or any((s.get("style") == "swing") for s in sells))
        swing_alert = ""
        if not rg["allow_swing"] and _is_swing:
            swing_alert = (f"当前{rg['node_label']}（{rg['mode_name']}）：做长=绞肉机 "
                           "—— 波段仓位按短线纪律处理（跌破就走，不幻想回本）")
        out.append({
            "code": code, "name": h.get("name") or snap.get("name", ""),
            "cost": cost, "shares": shares, "date": h.get("date", ""),
            "note": h.get("note", ""), "price": price, "pct": _f(snap.get("pct")),
            "pnl": round(mv - cst, 1) if (mv and cst) else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "mv": round(mv, 1), "verdict": verdict, "level": level,
            "plain": plain, "exit_plan": exitp,
            "signals": sells, "fund_style": fs.get("type", "unknown"),
            "r3": r3, "unwind": unwind, "swing_alert": swing_alert,
        })
        if level in ("critical", "high"):
            checks.append({"level": "red" if level == "critical" else "yellow",
                           "item": f"{out[-1]['name']} 持仓告警：{verdict}",
                           "action": (sells[0].get("action", "") if sells else "")})
        if swing_alert:
            checks.append({"level": "yellow",
                           "item": f"{out[-1]['name']} 做长警示：{rg['node_label']}",
                           "action": swing_alert})

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
