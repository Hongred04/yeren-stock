# -*- coding: utf-8 -*-
"""买卖点建议 + 仓位操作系统。

买入点体系（按胜率优先级）：
  B1 首板试错 —— 新周期首板，情绪冰点后的第一板
  B2 龙头回踩 —— 龙头五板前不折腾，回踩5日线/缩量不破
  B3 缩量红盘低吸 —— 极致缩量还红盘，控盘好
  B4 尾盘博弈 —— 节假日博弈只在尾盘两点左右入场

卖出点体系：
  S1 退潮清仓 —— 无条件清仓，最高优先级
  S2 龙头止盈 —— 达到目标板数/情绪高潮后段
  S3 破位止损 —— 破关键均线/前低
  S4 A字杀警报 —— 炸板+跌停，绝不反包，立即出
  S5 去弱留强 —— 相对强度掉队则换仓
  S6 机构加速/破5日线 —— 机构(中军)票专用离场口径
  S7 断板节奏 —— 游资(龙头)票专用离场口径
  S8 庄股回避 —— 庄股不参与

M10 主导资金分类（fundstyle）：同一趋势主导资金不同离场点完全不同 ——
  机构票突然拉涨停=出货冲顶立刻减仓；游资票跟断板节奏、节奏破坏就撤；
  庄股断崖式下跌盈亏比极差，不参与。
"""
from __future__ import annotations

from . import indicators as ind


# ---------------------------------------------------------------- 买入点

def buy_signals(stock: dict, bars: list[dict], sentiment: dict,
                volume: dict, cfg: dict) -> list[dict]:
    """生成买入点建议列表。"""
    out = []
    if not bars:
        return out
    code = stock["code"]
    close = bars[-1]["close"]
    a = cfg.get("M4_aesthetic", {})
    last = bars[-1]
    prev = bars[-2] if len(bars) > 1 else last
    thr = ind.limit_up_threshold(code)

    # 交易许可检查：退潮期 / 极度缩量 → 不给开仓建议，只给观察提示
    if not sentiment.get("can_trade", True) or volume.get("mode") == "dry":
        reason = ("情绪退潮期" if not sentiment.get("can_trade", True) else "极度缩量")
        return [{
            "type": "HOLD", "name": "禁止开仓", "priority": "A",
            "trigger": f"{reason}：{sentiment.get('note') or volume.get('advice','')}",
            "entry": "不新开仓位；已持仓按卖出信号执行",
            "stop": "—",
            "target": "空仓等新周期首板",
            "position": "0%（新增）",
            "note": "退潮期管住手，量能不够说破大天都没用",
            "blocked": True,
        }]

    # M10：庄股不参与（断崖式下跌 + 做庄失败打回原点，盈亏比极差）
    fs = stock.get("fund_style") or {}
    if fs.get("type") == "zhuang":
        return [{
            "type": "HOLD", "name": "庄股不参与", "priority": "A",
            "trigger": "；".join(fs.get("evidence", [])[:2]) or "庄股特征",
            "entry": "不参与",
            "stop": "若已持仓，遇断崖式下跌无条件出，绝不反包",
            "target": "—",
            "position": "0%",
            "note": "做庄失败打回原点，盈亏比极差",
            "blocked": True,
        }]

    # B1 首板试错：处于首板且情绪非退潮
    if stock.get("zt_boards", 0) == 1 and sentiment.get("stage") in ("probe", "follow"):
        out.append({
            "type": "B1", "name": "首板试错",
            "priority": "A",
            "trigger": f"新周期首板，{stock.get('sector','题材内')}启动信号",
            "entry": f"次日开盘不追高，回踩不破 {close*0.98:.2f} 介入；或竞价小幅低开介入",
            "stop": f"跌破首板最低价 {low_of(bars[-1])*0.98:.2f} 止损（试错失败只损失一笔很小的资金）",
            "target": "看二板，三板以上看龙头溢价",
            "position": "单票试错仓位，不超过总资金20%",
            "note": "新周期一定是首板，二板还属于老周期",
        })

    # B2 龙头回踩
    boards = stock.get("zt_boards", 0)
    ma5 = ind.ma([b["close"] for b in bars], 5)
    ma10 = ind.ma([b["close"] for b in bars], 10)
    hold_before = cfg.get("M5_position", {}).get("hold_before_boards", 5)
    if boards >= 2 and boards < hold_before and ma5 and close <= ma5 * 1.03 and close >= ma5 * 0.95:
        out.append({
            "type": "B2", "name": "龙头回踩",
            "priority": "A",
            "trigger": f"{boards}连板龙头回踩5日线({ma5:.2f})，缩量不破位",
            "entry": f"回踩 {ma5:.2f}~{close*1.01:.2f} 区间分批介入，缩量企稳为佳",
            "stop": f"有效跌破10日线 {ma10:.2f} 离场",
            "target": f"龙头走到{hold_before}板前不折腾，吃主升",
            "position": "可加至 40-60%（去弱留强后最强者）",
            "note": "去弱留强不带幻想，砍错不心疼",
        })

    # B3 缩量红盘低吸
    if volume.get("red_flag") and stock.get("pct", 0) > 0:
        out.append({
            "type": "B3", "name": "缩量红盘低吸",
            "priority": "B",
            "trigger": f"极致缩量（{volume.get('amount',0):.0f}亿）仍红盘 {stock.get('pct',0):.2f}%，控盘好",
            "entry": f"不追高，等回落后于 {close*0.99:.2f} 附近低吸",
            "stop": f"跌破 {close*0.95:.2f} 止损",
            "target": "缩量红盘=筹码结构好，博弈反包",
            "position": "小仓位试探，缩量市场只信量能",
            "note": "散户90%套牢躺平，此时不宜重仓接",
        })

    # B4 尾盘博弈
    if a.get("price_min", 5) <= stock.get("price", 0) <= a.get("price_max", 10):
        out.append({
            "type": "B4", "name": "尾盘博弈",
            "priority": "C",
            "trigger": "节假日/节前博弈，缩量市场波动小",
            "entry": "只在尾盘两点（14:00-14:30）左右入场，避开日内波动",
            "stop": "次日低开超3% 走人",
            "target": "节后情绪延续则持有",
            "position": "小仓位",
            "note": "散户波动最小窗口",
        })

    return out


def low_of(bar: dict) -> float:
    return bar.get("low", bar.get("close", 0))


# ---------------------------------------------------------------- 卖出点

def sell_signals(stock: dict, bars: list[dict], sentiment: dict,
                 volume: dict, cfg: dict) -> list[dict]:
    """生成卖出点建议。"""
    out = []
    if not bars:
        return out
    close = bars[-1]["close"]
    ma5 = ind.ma([b["close"] for b in bars], 5)
    ma10 = ind.ma([b["close"] for b in bars], 10)
    ma20 = ind.ma([b["close"] for b in bars], 20)
    a = cfg.get("M4_aesthetic", {})

    # S1 退潮清仓（最高优先级）
    if sentiment.get("stage") == "ebb":
        sigs = sentiment.get("signals", [])
        out.append({
            "type": "S1", "name": "退潮清仓", "priority": "A",
            "trigger": "；".join(sigs) if sigs else "情绪退潮信号触发",
            "action": "无条件清仓，停止一切低吸和打板，管住手",
            "urgency": "critical",
            "note": "退潮期最忌留恋，保住本金是第一原则",
        })

    # S2 龙头止盈
    boards = stock.get("zt_boards", 0)
    if boards >= 5:
        out.append({
            "type": "S2", "name": "龙头止盈", "priority": "A",
            "trigger": f"已 {boards} 连板，进入情绪高潮后段",
            "action": f"分批止盈，锁定利润；涨停打开且不回封立即走",
            "urgency": "high",
            "note": "情绪高潮→退潮的转换通常在高位",
        })

    # S3 破位止损
    if ma10 and close < ma10 * 0.97:
        out.append({
            "type": "S3", "name": "破位止损", "priority": "B",
            "trigger": f"跌破10日线 {ma10:.2f} 达 {(1-close/ma10)*100:.1f}%",
            "action": f"离场观望，待重新站上再考虑（现价 {close:.2f}）",
            "urgency": "high",
            "note": "去弱留强，砍错不心疼",
        })

    # S4 A字杀警报
    if stock.get("pct", 0) < -7:
        out.append({
            "type": "S4", "name": "A字杀警报", "priority": "A",
            "trigger": f"单日大跌 {stock.get('pct',0):.2f}%",
            "action": "立即离场，绝不反包（退潮期典型A字杀特征）",
            "urgency": "critical",
            "note": "亏钱效应扩散时，A字杀不可接飞刀",
        })

    # S5 去弱留强
    if stock.get("turnover", 0) > 0 and stock.get("pct", 0) < -2 and sentiment.get("stage") in ("climax", "follow"):
        out.append({
            "type": "S5", "name": "去弱留强", "priority": "C",
            "trigger": f"涨跌幅 {stock.get('pct',0):.2f}%，弱于同题材龙头",
            "action": "把仓位转移到题材内最强者，砍掉弱票",
            "urgency": "medium",
            "note": "竞价强更强定强弱，果断砍弱",
        })

    # ---- M10 主导资金类型专属离场口径 ----
    fs = stock.get("fund_style") or {}
    if fs.get("type") == "institutional":
        if fs.get("accel"):
            out.append({
                "type": "S6", "name": "机构加速·减仓", "priority": "A",
                "trigger": "慢牛途中突然拉涨停（加速冲顶）",
                "action": "立刻开始减仓 —— 加速不是加仓点，是机构派发的最后一段",
                "urgency": "high",
                "note": "机构票离场信号=加速：沿5日线慢牛的票突然涨停=出货冲顶",
            })
        elif ma5 and close < ma5:
            out.append({
                "type": "S6", "name": "机构票破5日线", "priority": "B",
                "trigger": f"跌破5日线 {ma5:.2f}（慢牛节奏改变）",
                "action": "先减半仓保住波段利润，收复5日线再考虑接回",
                "urgency": "medium",
                "note": "机构票小阳线爬五日线，破了就是节奏变了",
            })
    elif fs.get("type") == "hotmoney":
        if fs.get("rhythm_broken"):
            out.append({
                "type": "S7", "name": "断板节奏破坏", "priority": "A",
                "trigger": "断板2天以上未回封，连板节奏已被破坏",
                "action": "撤 —— 游资票按断板节奏走，节奏没了就走",
                "urgency": "critical",
                "note": "两板一断、两板一断；节奏破坏=情绪溢价结束",
            })
        elif fs.get("broken_board"):
            out.append({
                "type": "S7", "name": "断板日", "priority": "B",
                "trigger": "昨日涨停、今日未封板（断板）",
                "action": "盯回封力度：不回封按节奏减仓/离场",
                "urgency": "high",
                "note": "游资票离场跟着断板节奏走",
            })
    elif fs.get("type") == "zhuang":
        out.append({
            "type": "S8", "name": "庄股·回避", "priority": "A",
            "trigger": "；".join(fs.get("evidence", [])[:2]) or "庄股特征",
            "action": "不参与；若已持仓，断崖式下跌无条件出",
            "urgency": "high",
            "note": "做庄失败打回原点，盈亏比极差",
        })

    return out


# ---------------------------------------------------------------- 仓位链

def position_plan(cfg: dict, picks: list[dict], sentiment: dict,
                  volume: dict, macro: dict) -> dict:
    """M5 仓位操作系统：试错 → 加仓 → 推仓 → 锁仓。"""
    p = cfg.get("M5_position", {})
    if not p.get("enabled", True):
        return {"chain": [], "total": 0, "note": "仓位模块已关闭，自行控制"}

    n = p.get("pick_count", 4)
    first_w = p.get("first_weight", 0.20)
    cash = p.get("cash_reserve", 0.20)
    max_single = p.get("max_single_weight", 0.60)
    hold_before = p.get("hold_before_boards", 5)

    # 退潮期 → 目标仓位0
    if sentiment.get("stage") == "ebb":
        return {
            "chain": [
                {"stage": "退潮", "action": "无条件清仓", "weight": 0.0,
                 "note": "保住本金是第一原则"},
                {"stage": "等待", "action": "空仓等新周期首板", "weight": 0.0,
                 "note": "新周期一定是首板"},
            ],
            "target_total": 0.0,
            "chain_stage": "ebb",
            "note": "当前处于退潮期，所有标的空仓",
        }

    # 极度缩量 → 降低仓位
    dry = volume.get("mode") == "dry"
    top_n = min(n, len(picks))
    if top_n == 0:
        return {"chain": [], "target_total": 0.0, "chain_stage": "idle",
                "note": "无符合审美标的，空仓等待"}

    # Day1 平铺
    per = min(first_w, (1 - cash) / top_n) if top_n else 0
    chain = [
        {"stage": "Day1 平铺", "action": f"选出题材内{top_n}只，每只试错",
         "weight": round(per, 3), "total": round(per * top_n, 3),
         "note": f"各{per*100:.0f}%，合计{per*top_n*100:.0f}%，留{cash*100:.0f}%现金"},
    ]

    # Day2 去弱留强
    if p.get("add_on_strength", True):
        strong_w = min(max_single, 1 - cash / 2)
        chain.append({
            "stage": "Day2 去弱留强",
            "action": "竞价强更强定强弱，砍掉弱票，仓位全转移最强者",
            "weight": round(strong_w, 3), "total": round(strong_w, 3),
            "note": f"最强者可至{strong_w*100:.0f}%，砍错不心疼",
        })

    chain.append({
        "stage": "锁仓", "action": f"龙头{hold_before}板前不折腾",
        "weight": round(strong_w if p.get('add_on_strength', True) else per * top_n, 3),
        "total": round(strong_w if p.get('add_on_strength', True) else per * top_n, 3),
        "note": "吃主升",
    })

    target = strong_w if p.get("add_on_strength", True) else per * top_n
    if dry:
        target *= 0.5
        chain[0]["note"] += "（极度缩量，仓位减半）"

    return {
        "chain": chain,
        "target_total": round(target, 3),
        "chain_stage": sentiment.get("stage", "probe"),
        "per_stock_first": round(per, 3),
        "note": f"当前情绪阶段：{sentiment.get('label','')}",
    }
