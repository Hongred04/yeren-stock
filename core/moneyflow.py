# -*- coding: utf-8 -*-
"""E3 主力资金流 + 「资金在干什么」结论（扩展模块，非野人原体系）。

三层口径：
  ① 题材资金流（可靠口径）—— 当日涨停池个股的主力净流入按行业题材聚合，
     「今天涨停的钱主要砸在哪个方向」，最贴合打板语境；
  ② 候选股主力资金流 —— 每只候选项的超大单/大单净额与净占比；
  ③ 板块资金流排行 —— 东财 push2 ulist.np（行业 + 概念，含领涨股）。

「资金在干什么」结论（capital_view）：
  龙虎榜席位 / 机构专用 / 北向 / 知名游资的数据**只进这个结论**，
  不在界面上摆原始明细 —— 用户要的是判断：游资在接力还是在撤、
  机构在吸筹还是在派发、主力钱往哪个方向砸。

东财 push2 口径：f62 主力净额、f184 主力净占比、f66/72/78/84 超大/大/中/小单。
数据源受限时结论会**指名道姓**（东财 / 新浪 / 腾讯 哪家受限），不写"接口异常"。
"""
from __future__ import annotations

import json
import os
import time

from . import datasource as ds
from . import paths

BOARD_HIST_PATH = paths.data_path("board_flow_history.json")


def _flow_up(row: dict) -> bool:
    """板块当天算不算「钱在进」：有主力口径看 main，没有就看涨幅。"""
    m = row.get("main")
    if isinstance(m, (int, float)):
        return m > 0
    p = row.get("pct")
    return isinstance(p, (int, float)) and p > 0


def _archive_boards(real_date: str, boards_all: list[dict]) -> None:
    """把板块资金快照按日归档（同日覆盖 —— 越晚越接近收盘最终值），留 40 天。

    这是「板块资金记忆」的数据底座：单日快照没意义，连续几天的方向才有 ——
    「连续 N 日净流入」比「今天流入」值钱得多。
    """
    if not real_date or not boards_all:
        return
    snap = [{"name": b.get("name"), "pct": b.get("pct"), "main": b.get("main")}
            for b in boards_all if b.get("name")]
    if not snap:
        return
    try:
        try:
            with open(BOARD_HIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        data[real_date] = snap
        for k in sorted(data.keys())[:-40]:
            data.pop(k, None)
        os.makedirs(os.path.dirname(BOARD_HIST_PATH), exist_ok=True)
        tmp = BOARD_HIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, BOARD_HIST_PATH)
    except OSError:
        pass


def board_streaks(real_date: str = "") -> dict[str, int]:
    """{板块名: 连续「钱在进」天数}，从最近归档日往回数（按已归档交易日）。

    只在连续归档日上都成立才算 —— 断一天就归零（中途断了说明资金转过场）。
    """
    try:
        with open(BOARD_HIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    dates = sorted(data.keys())
    if real_date:
        dates = [d for d in dates if d <= real_date]
    runs: dict[str, int] = {}
    active: set[str] | None = None
    for d in reversed(dates):
        up = {b.get("name") for b in data.get(d, [])
              if b.get("name") and _flow_up(b)}
        if active is None:
            active = set(up)
            for n in active:
                runs[n] = 1
        else:
            keep = active & up
            if not keep:
                break
            for n in keep:
                runs[n] += 1
            active = keep
    return runs


def board_cum_pct(real_date: str = "", days: int = 3) -> dict[str, float]:
    """{板块名: 近 N 个已归档交易日的累计涨幅%} —— 「板块位置」的代理指标。

    没有板块指数K线，就用归档快照里的逐日涨幅累加：**近 N 日跌得多 = 位置低**。
    只统计已归档的日子（覆盖不全就是"没数据"，返回里干脆不带这个板块名，
    由调用方当中性处理 —— 不猜）。
    """
    try:
        with open(BOARD_HIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    dates = sorted(data.keys())
    if real_date:
        dates = [d for d in dates if d <= real_date]
    dates = dates[-max(1, int(days)):]
    out: dict[str, float] = {}
    for d in dates:
        for b in data.get(d, []):
            n = b.get("name")
            p = b.get("pct")
            if n and isinstance(p, (int, float)):
                out[n] = round(out.get(n, 0.0) + float(p), 2)
    return out


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
        # ★ 2026-10-15 修：THS 降级行的 main 可能是 None（净额格解析不出，
        #   datasource._ths_flow_row 返回 main=None），+= None 直接崩。
        if f.get("main") is None:
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


def _bk(b: dict) -> dict:
    m = b.get("main")
    return {"name": b.get("name", ""), "pct": b.get("pct"),
            "main": (round(m / 1e8, 2) if m is not None else None),
            "main_pct": (round(b["main_pct"], 2)
                         if b.get("main_pct") is not None else None),
            "amount": (round((b.get("amount") or 0) / 1e8, 2)
                       if b.get("amount") else None),
            "lead": b.get("lead", ""), "source": b.get("source", "")}


def capital_view(flow: dict, lhb: dict | None = None,
                 ml: dict | None = None) -> dict:
    """把席位/机构/北向/主力净流入收成一句结论：当前资金在干什么。

    只做判断，不摆明细（原龙虎榜页已按用户要求下线）。
    """
    lhb = lhb or {}
    ml = ml or {}
    items: list[dict] = []
    # 结构化净额（亿）—— E5 等下游模块直接读这几个字段，
    # 不要去 `"净买" in inst_txt` 那样解析中文文案（脆，改一个字就崩）
    inst_net_val: float | None = None
    north_net_val: float | None = None

    # ① 游资：席位活跃度
    hm = lhb.get("hotmoney") or []
    hm_net = _f(lhb.get("hotmoney_net"))
    if lhb.get("available") and hm:
        tags = "、".join(h.get("tag", "") for h in hm[:2])
        names = []
        for h in hm[:3]:
            for s in (h.get("stocks") or [])[:1]:
                if s.get("name"):
                    names.append(s["name"])
        stock_txt = f"（{('、'.join(names[:3]))}）" if names else ""
        if hm_net > 0:
            items.append({
                "k": "游资", "level": "red",
                "v": f"席位净买 {hm_net:+.1f} 亿 —— {tags} 在接力{stock_txt}",
                "note": "游资接力盘活跃：题材股按断板节奏做，节奏破坏就撤",
            })
        else:
            items.append({
                "k": "游资", "level": "gry",
                "v": f"席位净卖 {hm_net:+.1f} 亿 —— {tags} 在兑现{stock_txt}",
                "note": "游资在撤退：不接高位、不追连板，等他们回来再说",
            })
    # ② 机构：机构专用席位方向
    inst = lhb.get("institutions") or {}
    if lhb.get("available") and inst.get("net") is not None:
        net = _f(inst.get("net"))
        inst_net_val = net            # 结构化暴露给下游模块（别去解析中文文案）
        if net >= 0:
            items.append({"k": "机构", "level": "red",
                          "v": f"机构专用席位净买 {net:+.2f} 亿",
                          "note": "机构在真金白银买 —— 中军/波段方向更稳，趋势票可拿"})
        else:
            items.append({"k": "机构", "level": "gry",
                          "v": f"机构专用席位净卖 {net:+.2f} 亿",
                          "note": "机构在高位兑现 —— 别接高位大票，防派发"})
    # ③ 北向
    north = lhb.get("northbound") or {}
    if lhb.get("available") and north.get("net") is not None:
        net = _f(north.get("net"))
        north_net_val = net
        items.append({"k": "北向", "level": "red" if net >= 0 else "gry",
                      "v": f"北向资金净{'买' if net >= 0 else '卖'} {net:+.2f} 亿",
                      "note": ("外资方向偏多" if net >= 0 else "外资方向偏空") +
                              ("（龙虎榜盘中未披露，为最近一期）"
                               if not lhb.get("is_today") else "")})

    # ④ 主力主攻方向（涨停股资金聚合 + 板块排行）
    sec_in = flow.get("sec_in") or []
    sec_out = flow.get("sec_out") or []
    if sec_in:
        top = "、".join(f"{s['sector']}({s['main']:+.1f}亿)" for s in sec_in[:3])
        items.append({"k": "主攻方向", "level": "red",
                      "v": f"涨停股资金主攻：{top}",
                      "note": "涨停最集中且主力净流入 —— 这就是当前的主战场"})
    if sec_out:
        out = "、".join(f"{s['sector']}({s['main']:+.1f}亿)" for s in sec_out[:2])
        items.append({"k": "撤离方向", "level": "gry",
                      "v": f"资金从这些题材撤：{out}",
                      "note": "有涨停但主力在撤 —— 谨慎接力，只吃情绪不恋战"})
    boards_in = flow.get("boards_in") or []
    if boards_in and flow.get("board_has_main"):
        top = "、".join(f"{b['name']}({b['main']:+.0f}亿)"
                        for b in boards_in[:3] if b.get("main") is not None)
        if top:
            items.append({"k": "板块吸金", "level": "blu",
                          "v": f"板块主力净流入居前：{top}",
                          "note": "资金在往这些方向堆 —— 板块内出涨停就重点看"})

    # 结论一句话
    if items:
        hm_txt = next((x["v"] for x in items if x["k"] == "游资"), "")
        inst_txt = next((x["v"] for x in items if x["k"] == "机构"), "")
        if hm_txt and inst_txt:
            hm_pos = "净买" in hm_txt
            inst_pos = "净买" in inst_txt
            if hm_pos and not inst_pos:
                headline = "游资接力、机构兑现 —— 有热度、有派发，做题材不做高位大票"
            elif hm_pos and inst_pos:
                headline = "游资、机构同向买入 —— 资金合力最好的状态，可积极些"
            elif (not hm_pos) and inst_pos:
                headline = "机构在吸、游资在看 —— 偏波段底仓逻辑，不追短线"
            else:
                headline = "游资、机构同向撤退 —— 资金离场期，防守优先"
        elif hm_txt:
            headline = "主线由游资主导 —— 跟着断板节奏，快进快出"
        elif inst_txt:
            headline = "资金由机构主导 —— 按波段逻辑拿，加速才减仓"
        elif sec_in:
            headline = "看主力资金方向：涨停资金净流入最集中的题材就是当前主战场"
        else:
            headline = "资金数据有限，按主力净流入方向谨慎跟随"
    else:
        headline = "资金明细暂不可用 —— 先看量能与情绪，不硬猜"

    note = flow.get("source_note") or ""
    return {"available": bool(items), "headline": headline,
            "items": items, "note": note,
            # 结构化净额（亿，None = 该项没数据）—— 给下游模块（如 E5）用
            "inst_net": inst_net_val,
            "hotmoney_net": (round(hm_net, 2) if lhb.get("available") and hm else None),
            "north_net": north_net_val}


def evaluate(cfg: dict | None = None, picks: list[dict] | None = None,
             snaps: dict | None = None, theme: str = "",
             zt: list[dict] | None = None,
             lhb: dict | None = None,
             real_date: str = "") -> dict:
    """构建资金流面板。picks 会被就地补上 moneyflow 字段。"""
    a = (cfg or {}).get("E3_flow", {})
    if not a.get("enabled", True):
        return {"enabled": False, "available": False, "label": "模块已关闭",
                "sectors": [], "boards_in": [], "boards_out": [], "boards_all": [],
                "summary": "", "capital": {"available": False,
                                           "headline": "资金模块已关闭", "items": []}}

    # ① 题材资金流（可靠）
    sectors = _theme_flow(zt)
    sec_in = [s for s in sectors if s["main"] > 0][:8]
    sec_out = [s for s in sectors if s["main"] < 0][-6:][::-1]

    # ② 候选股主力资金流
    if pics := (picks or []):
        flow_map = ds.fetch_stock_moneyflow([p["code"] for p in pics])
        for p in pics:
            f = flow_map.get(p["code"])
            if not f:
                continue
            # ★ 2026-10-15 修（TypeError: NoneType doesn't define __round__）：
            #   THS 降级行（fetch_stock_moneyflow 第三级，source=ths）只有
            #   main（净额），main_pct/super/big 恒为 None —— round(None)
            #   直接炸掉整轮扫描。字段各自判空，None 原样透传（前端已按
            #   null 显示 '—'），有值的才 round。
            def _r(v, div=1.0):
                return round(v / div, 2) if isinstance(v, (int, float)) else None
            p["moneyflow"] = {
                "main": _r(f.get("main"), 1e8),               # 亿
                "main_pct": _r(f.get("main_pct")),
                "super": _r(f.get("super"), 1e8),
                "big": _r(f.get("big"), 1e8),
                "source": f.get("source", ""),                # 界面如实标注
            }

    # ③ 板块资金流排行（ulist.np，行业 + 概念一起排）
    # 取 200 行：底层本来就是全量取数、只是切片返回，顺带把归档做厚，
    # "连续 N 日净流入"才不会因为板块掉出前 60 就断档
    board_n = int(_f(a.get("board_top_n"), 10)) or 10
    boards = ds.fetch_board_moneyflow(200)
    board_src = (boards[0].get("source") if boards else "") or ""
    # ths 口径有净流入（近似主力净额），sina 才完全没有 —— 都能进"板块吸金"
    has_main = board_src in ("em", "ths")

    boards_all = [_bk(b) for b in boards]
    # 板块资金记忆：归档今日快照 + 标注每个板块「连续钱在进的天数」
    try:
        _archive_boards(real_date, boards_all)
        streaks = board_streaks(real_date)
        for b in boards_all:
            b["streak"] = streaks.get(b.get("name"), 1)
    except Exception:
        pass
    boards_in = boards_all[:board_n]
    # 有主力净额就按净额排，否则退而用成交额（钱多=关注度高）
    rank_key = (lambda x: (x.get("main") or 0)) if has_main \
        else (lambda x: (x.get("amount") or 0))
    boards_out = sorted(boards_all, key=rank_key)[:5] if boards_all else []

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
        if not boards_in:
            summary += "　|　东财板块接口受限（冷却中），板块资金排行暂缺"
    elif boards_in and has_main:
        # ★ 2026-10-15 修：THS 降级行的 main 可能是 None（净流入格是 '--' 时
        #   datasource._ths_flow_row 返回 None），f"{None:+.0f}" 直接
        #   TypeError 炸掉整个资金面板。与下面 :251「板块吸金」那处保持
        #   同一种写法：先过滤掉没有净额的行，一条都没就退回下一档。
        top = "、".join(f"{b['name']}({b['main']:+.0f}亿)"
                        for b in boards_in[:3] if b.get("main") is not None)
        if top:
            summary = f"板块主力净流入居前：{top}"
        else:
            summary = "板块主力净额暂缺（板块榜取到了，但净流入格没解析出来）"
    elif boards_in:
        top = "、".join(f"{b['name']}({b['pct']:+.1f}%/{b['amount']:.0f}亿)"
                        for b in boards_in[:3] if b.get("amount") is not None) or "无"
        summary = f"东财主力净额接口受限，改用新浪口径（涨幅/成交额）—— 活跃板块：{top}"
    else:
        summary = "东财资金接口受限，且新浪板块兜底也不可用 —— 资金方向暂无法判断"

    # 数据源受限时，指名道姓
    if not boards_all:
        source_note = "板块数据暂不可用（东财限速中，新浪兜底也没取到）"
    elif board_src == "sina":
        source_note = "板块数据来自新浪（东财被限速）：没有主力净额，只有涨幅/成交额/领涨股"
    elif board_src == "ths":
        source_note = "板块数据来自同花顺（东财被限速）：净流入为同花顺口径，与东财主力净额相近但非同一数"
    else:
        source_note = "板块数据来自东财"

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

    out = {
        "enabled": True, "available": available, "label": "主力资金流",
        "sectors": sectors, "sec_in": sec_in, "sec_out": sec_out,
        "boards_in": boards_in, "boards_out": boards_out, "boards_all": boards_all,
        "boards_available": bool(boards_in),
        "board_source": board_src, "board_has_main": has_main,
        "theme_flow": theme_flow, "summary": summary, "checks": checks,
        "source_note": source_note,
    }
    # 「资金在干什么」结论：席位/机构/北向只进这里，不上原始明细
    out["capital"] = capital_view(out, lhb, None)
    return out
