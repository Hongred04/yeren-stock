# -*- coding: utf-8 -*-
"""E2 龙虎榜 · 游资席位（扩展模块，非野人原体系）。

给「谁在买」提供最硬的证据：每日龙虎榜上榜股 + 买卖席位明细（营业部），
按席位性质拆成 机构 / 北向 / 知名游资 / 普通营业部，并识别到具体个股。

灵感来源（GitHub 同类项目）：
  · simonlin1212/a-stock-data —— 龙虎榜席位层（上榜记录 + 买卖 TOP5 + 机构动向）；
  · Zhu-Aemon/slingshot —— api.em.lhb.get_lhb_data；
  · cheehwa88/aiagents-stock —— 游资龙虎榜跟踪；
  · limit-up-dao 涨停归因模型的「资金性质」维度（机构席位/知名游资）。

与「野人体系」的接口：直接喂给 M10 主导资金分类 —— 机构专用席位上榜更偏
机构票（离场=加速减仓），知名游资席位上榜更偏游资票（离场=断板节奏）。
"""
from __future__ import annotations

import re

from . import datasource as ds

# 知名游资席位关键词 → 短标签。
# 说明：席位绰号为公开复盘常用口径、非官方认定，仅供识别资金性质参考；
# 东财营业部全称会随合并改名微调，这里用「关键词包含」匹配，容错优先。
HOTMONEY_SEATS: list[tuple[str, str]] = [
    ("拉萨团结路", "拉萨天团"),
    ("拉萨东环路", "拉萨天团"),
    ("拉萨金融城", "拉萨天团"),
    ("拉萨北京东路", "拉萨天团"),
    ("上海溧阳路", "溧阳路"),
    ("宁波桑田路", "桑田路"),
    ("绍兴", "绍兴帮"),
    ("深圳益田路荣超商务中心", "荣超商务中心"),
    ("上海江苏路", "江苏路"),
    ("北京安立路", "北京帮"),
    ("深圳振华路", "深圳敢死队"),
    ("无锡清扬路", "清扬路"),
    ("杭州上塘路", "杭州帮"),
    ("成都北一环", "成都系"),
    ("深圳金田路", "金田路"),
]

INST_KEYWORDS = ("机构专用",)
NORTH_KEYWORDS = ("沪股通", "深股通", "香港中央结算")

_SHARES_URL = "https://data.eastmoney.com/stock/lhb.html"


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _seat_tags(dept: str) -> list[str]:
    """营业部全称 → 席位标签列表（机构/北向/知名游资）。"""
    if not dept:
        return []
    tags: list[str] = []
    if any(k in dept for k in INST_KEYWORDS):
        tags.append("机构专用")
    if any(k in dept for k in NORTH_KEYWORDS):
        tags.append("北向")
    for kw, name in HOTMONEY_SEATS:
        if kw in dept:
            tags.append(name)
            break
    return tags


def _is_hotmoney(dept: str) -> bool:
    return bool([t for t in _seat_tags(dept) if t not in ("机构专用", "北向")])


def _latest_date() -> str:
    rows = ds._dc_get("RPT_DAILYBILLBOARD_DETAILSNEW",
                      "sortColumns=TRADE_DATE&sortTypes=-1", 2)
    return str(rows[0].get("TRADE_DATE", ""))[:10] if rows else ""


def _fmt(d: str) -> str:
    """'20261008' → '2026-10-08'。"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def evaluate(cfg: dict | None = None, real_date: str = "",
             zt: list[dict] | None = None) -> dict:
    """拉取龙虎榜 + 席位，聚合出资金性质。zt 用于标记「上榜且涨停」。"""
    a = (cfg or {}).get("E2_lhb", {})
    if not a.get("enabled", True):
        return {"enabled": False, "available": False, "label": "模块已关闭",
                "top": [], "hotmoney": [], "seat_hits": {}, "summary": ""}

    want = _fmt(real_date) if real_date else ""
    date, rows = "", []
    if want:
        rows = ds.fetch_lhb(want)
        if rows:
            date = want
    if not rows:                       # 当日龙虎榜未出（盘中）→ 退到最近一期
        date = _latest_date()
        rows = ds.fetch_lhb(date) if date else []
    if not rows:
        return {"enabled": True, "available": False, "label": "数据源不可用",
                "date": "", "top": [], "hotmoney": [], "seat_hits": {},
                "summary": "龙虎榜数据暂不可用（接口异常或非交易日）", "checks": []}

    buys, sells = ds.fetch_lhb_seats(date)

    # 个股 → 买入席位标签集合
    stock_tags: dict[str, list[str]] = {}
    seat_net: dict[str, dict] = {}
    for s in buys + sells:
        tags = _seat_tags(s["dept"])
        if tags:
            cur = stock_tags.setdefault(s["code"], [])
            for t in tags:
                if t not in cur:
                    cur.append(t)
        g = seat_net.setdefault(s["dept"], {"dept": s["dept"], "tags": tags,
                                            "net": 0.0, "stocks": set()})
        g["net"] += s["net"]
        g["stocks"].add(s["code"])

    zt_codes = {r.get("code") for r in (zt or [])}
    by_code = {r["code"]: r for r in rows}

    # 席位画像：知名游资席位单独拎出，按净买额排序
    hotmoney = []
    for g in seat_net.values():
        if not _is_hotmoney(g["dept"]):
            continue
        hotmoney.append({
            "seat": g["dept"], "tag": "·".join(t for t in g["tags"]),
            "net": round(g["net"], 1), "n": len(g["stocks"]),
            "stocks": [{"code": c, "name": by_code.get(c, {}).get("name", ""),
                        "pct": by_code.get(c, {}).get("pct", 0)} for c in list(g["stocks"])[:5]],
        })
    hotmoney.sort(key=lambda x: -x["net"])

    inst_net = sum(g["net"] for g in seat_net.values() if "机构专用" in g["tags"])
    north_net = sum(g["net"] for g in seat_net.values() if "北向" in g["tags"])
    hm_net = sum(g["net"] for g in seat_net.values() if _is_hotmoney(g["dept"]))

    # 上榜股（按净买额降序），带席位标签 + 是否涨停
    rows_sorted = sorted(rows, key=lambda r: -_f(r.get("net")))
    top = []
    for r in rows_sorted:
        tags = stock_tags.get(r["code"], [])
        top.append({
            "code": r["code"], "name": r["name"], "pct": r["pct"],
            "net": round(_f(r.get("net")) / 1e4, 0),        # 万
            "buy": round(_f(r.get("buy")) / 1e8, 2),        # 亿
            "sell": round(_f(r.get("sell")) / 1e8, 2),
            "reason": r.get("reason", ""), "d1": r.get("d1"), "d5": r.get("d5"),
            "tags": tags, "in_zt": r["code"] in zt_codes,
        })

    top_n = int(_f(a.get("top_n"), 12)) or 12
    top_net = [p for p in top if p["net"] > 0][:top_n]
    net_in = sum(1 for r in rows if _f(r.get("net")) > 0)

    hm_names = "、".join(f"{h['tag']}({h['net']/1e4:+.0f}万)" for h in hotmoney[:3]) or "无"
    summary = (f"{date} 共 {len(rows)} 只上榜，净买入 {net_in} 只；"
               f"机构专用净 {inst_net/1e8:+.2f} 亿，北向净 {north_net/1e8:+.2f} 亿，"
               f"知名游资席位净 {hm_net/1e8:+.2f} 亿（{hm_names}）")

    checks = []
    if hm_net > 0 and hotmoney:
        checks.append({"level": "yellow",
                       "item": f"游资席位上榜活跃（{hotmoney[0]['tag']} 等）",
                       "action": "游资接力盘活跃 —— 相关标的按 M10 游资票口径，"
                                 "离场跟断板节奏"})
    if inst_net < 0 and abs(inst_net) > 1e8:
        checks.append({"level": "yellow", "item": "机构席位净卖出",
                       "action": "机构专用席位净卖出，注意高位兑现"})

    return {
        "enabled": True, "available": True, "label": "龙虎榜",
        "date": date, "is_today": bool(want and date == want),
        "source_url": _SHARES_URL,
        "top": top[:20], "top_net": top_net, "hotmoney": hotmoney[:8],
        "hotmoney_net": round(hm_net / 1e8, 2),
        "institutions": {"net": round(inst_net / 1e8, 2),
                         "stocks": [p for p in top if "机构专用" in p["tags"]][:6]},
        "northbound": {"net": round(north_net / 1e8, 2)},
        "summary": summary, "checks": checks,
        "seat_hits": stock_tags,
        "note": "席位绰号为公开复盘常用口径、非官方认定，仅供识别资金性质参考；"
                "盘中当日龙虎榜尚未披露时自动回退到最近一期。"
                "数据只进「资金在干什么」结论，不在界面展示明细。",
    }
