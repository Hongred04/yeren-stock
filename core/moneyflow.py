# -*- coding: utf-8 -*-
"""E3 主力资金流（扩展模块，非野人原体系）。

把「钱往哪走」量化，三层口径：
  ① 题材资金流（可靠口径）—— 当日涨停池个股的主力净流入按行业题材聚合，
     「今天涨停的钱主要砸在哪个方向」，最贴合打板语境；
  ② 候选股主力资金流 —— 每只候选项的超大单/大单净额与净占比；
  ③ 行业板块资金流排行 —— 东财 clist（best-effort，接口受限时自动隐藏）。

东财 push2 口径：f62 主力净额、f184 主力净占比、f66/72/78/84 超大/大/中/小单。

灵感来源（GitHub 同类项目）：
  · simonlin1212/a-stock-data —— 资金面层（板块资金流向 + 个股 120 日资金流）；
  · Zhu-Aemon/slingshot —— 沪深港通资金流向；
  · cheehwa88/aiagents-stock —— 资金流向分析师维度。

与「野人体系」的接口：M4 想象力打分只看题材本身，E3 补一条「主力真金白银
在不在」；M2 量能看总量，E3 看结构（钱进了哪些题材/个股）。
"""
from __future__ import annotations

from . import datasource as ds


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _theme_flow(zt: list[dict] | None) -> list[dict]:
    """涨停股按行业题材聚合主力净流入（ulist.np 批量口径，可靠）。"""
    if not zt:
        return []
    codes = [r.get("code") for r in zt if r.get("code")]
    flows = ds.fetch_stock_moneyflow(codes)
    if not flows:
        return []
    agg: dict[str, dict] = {}
    for r in zt:
        f = flows.get(r.get("code"))
        if not f:
            continue
        sec = (r.get("sector") or "").strip() or "未分类"
        g = agg.setdefault(sec, {"sector": sec, "main": 0.0, "n": 0,
                                 "max_boards": 0})
        g["main"] += f["main"]
        g["n"] += 1
        g["max_boards"] = max(g["max_boards"], int(_f(r.get("boards"), 1)))
    out = [{"sector": g["sector"], "main": round(g["main"] / 1e8, 2),
            "n": g["n"], "max_boards": g["max_boards"]}
           for g in agg.values()]
    out.sort(key=lambda x: -x["main"])
    return out


def evaluate(cfg: dict | None = None, picks: list[dict] | None = None,
             snaps: dict | None = None, theme: str = "",
             zt: list[dict] | None = None) -> dict:
    """构建资金流面板。picks 会被就地补上 moneyflow 字段。"""
    a = (cfg or {}).get("E3_flow", {})
    if not a.get("enabled", True):
        return {"enabled": False, "available": False, "label": "模块已关闭",
                "sectors": [], "boards_in": [], "boards_out": [], "summary": ""}

    # ① 题材资金流（可靠）
    sectors = _theme_flow(zt)
    sec_in = [s for s in sectors if s["main"] > 0][:8]
    sec_out = [s for s in sectors if s["main"] < 0][-6:][::-1]

    # ② 候选股主力资金流
    if pics := (picks or []):
        flow_map = ds.fetch_stock_moneyflow([p["code"] for p in pics])
        for p in pics:
            f = flow_map.get(p["code"])
            if f:
                p["moneyflow"] = {
                    "main": round(f["main"] / 1e8, 2),          # 亿
                    "main_pct": round(f["main_pct"], 2),
                    "super": round(f["super"] / 1e8, 2),
                    "big": round(f["big"] / 1e8, 2),
                }

    # ③ 行业板块资金流排行（clist，best-effort）
    board_n = int(_f(a.get("board_top_n"), 10)) or 10
    boards = ds.fetch_board_moneyflow(60)
    boards_in = [{"name": b["name"], "pct": b["pct"],
                  "main": round(b["main"] / 1e8, 2),
                  "main_pct": round(b["main_pct"], 2),
                  "lead": b.get("lead", "")} for b in boards[:board_n]]
    boards_out = [{"name": b["name"], "pct": b["pct"],
                   "main": round(b["main"] / 1e8, 2),
                   "main_pct": round(b["main_pct"], 2)}
                  for b in sorted(boards, key=lambda x: x["main"])[:5]]

    theme_flow = None
    if theme and sectors:
        for s in sectors:
            if s["sector"] == theme or (s["sector"] and (s["sector"] in theme or theme in s["sector"])):
                theme_flow = s
                break

    available = bool(sectors or boards_in)
    if sectors:
        top = "、".join(f"{s['sector']}({s['main']:+.1f}亿/{s['n']}只)" for s in sec_in[:3]) or "无"
        summary = f"涨停股主力净流入居前：{top}"
    elif boards_in:
        top = "、".join(f"{b['name']}({b['main']:+.0f}亿)" for b in boards_in[:3])
        summary = f"行业板块主力净流入居前：{top}"
    else:
        summary = "资金流数据暂不可用（接口受限）"

    checks = []
    hot = sec_in[0] if sec_in else None
    if hot and hot["n"] >= 3 and hot["main"] > 0:
        checks.append({"level": "green", "item": f"题材「{hot['sector']}」资金合力",
                       "action": f"该题材 {hot['n']} 只涨停且主力净流入 {hot['main']:+.1f} 亿 —— "
                                 "资金真金白银在里，延续性较强"})
    if theme_flow and theme_flow["main"] < 0:
        checks.append({"level": "yellow",
                       "item": f"主线题材「{theme_flow['sector']}」主力净流出 {theme_flow['main']:+.1f} 亿",
                       "action": "题材有涨停但主力在撤 —— 谨慎接力，只吃情绪不恋战"})

    return {
        "enabled": True, "available": available, "label": "主力资金流",
        "sectors": sectors, "sec_in": sec_in, "sec_out": sec_out,
        "boards_in": boards_in, "boards_out": boards_out,
        "boards_available": bool(boards_in),
        "theme_flow": theme_flow, "summary": summary, "checks": checks,
        "note": "主力净额 = 超大单 + 大单；题材资金流按当日涨停池聚合（谁涨停多、"
                "资金净流入大 = 主战场）。板块资金流接口受限时自动隐藏。",
    }
