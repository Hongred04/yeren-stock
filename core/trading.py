# -*- coding: utf-8 -*-
"""买卖点建议 + 仓位操作系统。

买入点体系（按胜率优先级）：
  B1 首板试错 —— 新周期首板，情绪冰点后的第一板（次日介入口径）
  B2 龙头回踩 —— 龙头五板前不折腾，回踩5日线/缩量不破
  B3 缩量红盘低吸 —— 极致缩量还红盘，控盘好
  B4 尾盘博弈 —— 仅节前最后交易日的尾盘（14:00后）给出，平时不出现

时间与涨停两条硬门槛：
  当日已涨停 → B2/B3/B4（当日进场类）全部不出，只留 B1 次日口径 + 「涨停不追」提示；
  B4 只在节前最后交易日的尾盘窗口出现，其他时段给"尾盘买入"就是假信号。

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

from datetime import datetime, timedelta

from . import indicators as ind


# ---------------------------------------------------------------- 交易时段

# 时段是买入建议的执行前提：B1/B2/B3 的进场都写"次日/回踩"，
# 只有 B4 尾盘博弈是当日定时动作 —— 时段不对就不给。
def market_session(now: datetime | None = None) -> dict:
    """A股当日交易时段判定（按本机时间）。"""
    now = now or datetime.now()
    t = now.hour * 100 + now.minute
    if now.weekday() >= 5:
        phase, label = "rest", "周末休市"
    elif t < 915:
        phase, label = "pre_open", "盘前"
    elif t < 930:
        phase, label = "auction", "集合竞价"
    elif t < 1130:
        phase, label = "morning", "早盘"
    elif t < 1300:
        phase, label = "noon", "午间休市"
    elif t < 1400:
        phase, label = "afternoon", "午后盘"
    elif t < 1500:
        phase, label = "tail", "尾盘"
    elif t < 1530:
        phase, label = "just_closed", "刚收盘"
    else:
        phase, label = "after_close", "盘后"
    return {
        "phase": phase,
        "label": label,
        "in_tail": phase == "tail",
        "after_hours": phase in ("rest", "just_closed", "after_close"),
        "is_trade_day": phase != "rest",
    }


def pre_holiday_gap(now: datetime | None = None) -> int:
    """距下一个交易日的自然日差（周末近似节假日：调休日股市同样休市）。

    普通周最大间隔是周五→周一的 3 天；所有 >=3 天的法定假期，
    最后交易日→恢复日必然 >=4 天。返回 >=4 即处于节前最后交易日。
    """
    now = now or datetime.now()
    d = now.date() + timedelta(days=1)
    gap = 1
    while d.weekday() >= 5:
        d += timedelta(days=1)
        gap += 1
    return gap


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
    at_limit = stock.get("pct", 0) >= thr or stock.get("zt_boards", 0) >= 1
    sess = market_session()
    if stock.get("zt_boards", 0) == 1 and sentiment.get("stage") in ("probe", "follow"):
        note = "新周期一定是首板，二板还属于老周期"
        if at_limit:
            note += "；今日已封板，不追当日价，按次日口径执行"
        out.append({
            "type": "B1", "name": "首板试错",
            "priority": "A",
            "trigger": f"新周期首板，{stock.get('sector','题材内')}启动信号",
            "entry": f"次日开盘不追高，回踩不破 {close*0.98:.2f} 介入；或竞价小幅低开介入",
            "stop": f"跌破首板最低价 {low_of(bars[-1])*0.98:.2f} 止损（试错失败只损失一笔很小的资金）",
            "target": "看二板，三板以上看龙头溢价",
            "position": "单票试错仓位，不超过总资金20%",
            "note": note,
        })

    # 当日已涨停：B2/B3/B4 全是"当日进场"口径，涨停板上不成立 ——
    # 只保留 B1（次日介入口径），再给一条明确的"涨停不追"提示
    if at_limit:
        boards_now = stock.get("zt_boards", 0)
        board_txt = f"，{boards_now}板" if boards_now else ""
        out.append({
            "type": "HOLD", "name": "涨停不追", "priority": "A",
            "trigger": (f"今日已封涨停（+{stock.get('pct',0):.1f}%{board_txt}）"
                        "，当日不给追买建议"),
            "entry": "涨停板上买入=追明牌，炸板就是日内大面；次日按回踩/接力口径再看",
            "stop": "已持仓：次日炸板转弱按卖出信号离场，不恋战",
            "target": "—",
            "position": "0%（当日新增）",
            "note": "封死了买不到，能轻易买到大概率要炸 —— 明牌不追",
            "blocked": True,
        })
        return out

    after = sess["after_hours"]
    gap = pre_holiday_gap()

    # B2 龙头回踩：连板基因按"截至昨日近5日涨停次数"算 ——
    # zt_boards 是当日涨停池口径，回踩日必然不在池里，用它判 B2 这条信号永远出不来
    boards_recent = len(ind.count_limit_ups(bars[:-1], code, lookback=5)) if len(bars) > 1 else 0
    ma5 = ind.ma([b["close"] for b in bars], 5)
    ma10 = ind.ma([b["close"] for b in bars], 10)
    hold_before = cfg.get("M5_position", {}).get("hold_before_boards", 5)
    if 2 <= boards_recent < hold_before and ma5 and close <= ma5 * 1.03 and close >= ma5 * 0.95:
        out.append({
            "type": "B2", "name": "龙头回踩",
            "priority": "A",
            "trigger": f"近5日{boards_recent}次涨停的龙头回踩5日线({ma5:.2f})，缩量不破位",
            "entry": (f"次日回踩 {ma5:.2f}~{close*1.01:.2f} 区间分批介入，缩量企稳为佳"
                      if after else
                      f"回踩 {ma5:.2f}~{close*1.01:.2f} 区间分批介入，缩量企稳为佳"),
            "stop": (f"有效跌破10日线 {ma10:.2f} 离场" if ma10
                     else "跌破回踩低点离场"),
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
            "entry": (f"次日开盘后等回落，{close*0.99:.2f} 附近低吸" if after else
                      f"不追高，等回落后于 {close*0.99:.2f} 附近低吸"),
            "stop": f"跌破 {close*0.95:.2f} 止损",
            "target": "缩量红盘=筹码结构好，博弈反包",
            "position": "小仓位试探，缩量市场只信量能",
            "note": "散户90%套牢躺平，此时不宜重仓接",
        })

    # B4 尾盘博弈：只做节前最后交易日的尾盘窗口 ——
    # 非尾盘时段、非节前、当日已涨停，都不给（以前只看价格区间，
    # 涨停票照样推"尾盘买入"，是假信号）
    if (a.get("price_min", 5) <= stock.get("price", 0) <= a.get("price_max", 10)
            and sess["in_tail"] and gap >= 4):
        out.append({
            "type": "B4", "name": "尾盘博弈",
            "priority": "C",
            "trigger": f"节前最后交易日（距下一交易日 {gap} 天），已到尾盘窗口（14:00后）",
            "entry": "现在到 14:30 之间小仓位入场，避开假期消息面的不确定性",
            "stop": "次日低开超3% 走人",
            "target": "节后情绪延续则持有",
            "position": "小仓位",
            "note": "节假日博弈只在尾盘入场，非尾盘时段不出此信号",
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
