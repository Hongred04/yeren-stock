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

buy_signals 的返回值契约：
  列表里**既有可执行买点，也有"当日别买"的提示**，靠 kind 字段区分：
    · kind="buy"  → 可执行买点（B0/B1/B2/B3/B4），带 entry/stop/position；
    · kind="block"→ 禁买/不买提示（HOLD，blocked=True），只有"为什么别买"。
  消费者（前端 / journal）必须按 kind 或 blocked 过滤，不能把 block 当买点展示
  —— 以前前端把"涨停不追"塞进「其他买入时机」列表，自相矛盾。
sell_signals 全是可执行动作，无 block 语义。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import indicators as ind
from . import regime


# ---------------------------------------------------------------- 交易时段

# 时段是买入建议的执行前提：B1/B2/B3 的进场都写"次日/回踩"，
# 只有 B4 尾盘博弈是当日定时动作 —— 时段不对就不给。
def _hhmm_to_min(hhmm: int) -> int:
    """HHMM → 当日第几分钟。**不能用 hhmm 直接相减**：HHMM 不是线性刻度
    （9:59=959 → 10:00=1000 只差 1 分钟却差 41），直接减每跨一小时就多算 40 分钟。
    这个错会让盘中"已交易分钟数"虚高 → 日内占比虚高 → 全天额被低估 30%+。"""
    return (hhmm // 100) * 60 + (hhmm % 100)


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
    # 当日已交易分钟数（9:30 起算、剔除午休，0..240）—— 量能日内折算用。
    # 各段边界用"当日第几分钟"算，别再拿 HHMM 相减。
    m = _hhmm_to_min(t)
    if phase == "morning":
        tmin = m - _hhmm_to_min(930)
    elif phase == "noon":
        tmin = 120
    elif phase == "afternoon":
        tmin = 120 + (m - _hhmm_to_min(1300))
    elif phase == "tail":
        tmin = min(240, 180 + (m - _hhmm_to_min(1400)))
    elif phase in ("just_closed", "after_close"):
        tmin = 240
    else:
        tmin = 0
    return {
        "phase": phase,
        "label": label,
        "tmin": max(0, tmin),
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

def _stop_price(structure_low: float, close: float, max_loss: float = 0.07) -> float:
    """止损价：结构位与最大亏损双约束，取更高的那个（少亏优先）。

    首板的最低点有时离现价很远（低开拉板），单用结构位止损幅度过大；
    用 max(前低-1%, 现价×(1-max_loss)) 保证单笔试错亏损有上限。

    **止损必须落在现价下方**：结构位若已在现价之上（如破位票的 10 日线），
    取 max 会得到一个"高于现价的止损"，页面上还会显示成 +3.3% 的"亏损"——
    那是把止损写成了止盈。这里直接剔除 >= 现价的候选，保证返回值恒 < close。
    """
    floor = close * (1 - max_loss)
    cands = [x for x in (structure_low * 0.99 if structure_low else 0.0, floor)
             if 0 < x < close]
    return max(cands) if cands else floor


def _loss_pct(stop: float, close: float) -> str:
    """止损相对现价的百分比（负数是亏损）。止损 >= 现价说明该止损无效，返回 '—'。"""
    try:
        if stop >= close:
            return "—"
        return f"{(stop / close - 1) * 100:.1f}%"
    except ZeroDivisionError:
        return "—"


def buy_signals(stock: dict, bars: list[dict], sentiment: dict,
                volume: dict, cfg: dict) -> list[dict]:
    """生成买入点建议列表。

    进场价一律给**具体数字 + 触发条件**（竞价怎么开、回踩到哪里、什么情况放弃），
    不再给"低吸""逢低"这类没法执行的词。
    """
    out = []
    if not bars:
        return out
    code = stock["code"]
    close = bars[-1]["close"]
    a = cfg.get("M4_aesthetic", {})
    last = bars[-1]
    thr = ind.limit_up_threshold(code)
    fw = float(cfg.get("M5_position", {}).get("first_weight", 0.20) or 0.20)

    def _ordered(sigs: list[dict]) -> list[dict]:
        """按打法把匹配的一路排前面：波段票先看 W、短线票先看 B。
        禁买提示（kind=block）永远排最后 —— 它是"别买"，不该抢在买点前面。"""
        want_swing = ((stock.get("style_fit") or {}).get("key") == "swing")

        def key(s):
            blk = 1 if (s.get("kind") == "block" or s.get("blocked")) else 0
            mism = 0 if ((s.get("style") == "swing") == want_swing) else 1
            return (blk, mism)
        return sorted(sigs, key=key)

    # 交易许可检查：退潮期 / 极度缩量
    # 退潮期不再一刀切禁止出手 —— 允许 0.5 成试错仓，但**只做首板**：
    # 退潮里唯一有胜率结构的就是新周期第一板，接力/低吸都是接刀子。
    if not sentiment.get("can_trade", True) or volume.get("mode") == "dry":
        dry = volume.get("mode") == "dry"
        if sentiment.get("trial_only") and not dry:
            tw = float(sentiment.get("ebb_trial_weight") or 0.05)
            boards = int(stock.get("zt_boards") or 0)
            # ★ 退潮期的"可买"只给**真首板**（boards == 1，即今天已封板的第一个板）。
            #   原来写的是 `boards <= 1` —— 把**尚未涨停**的票也标成 kind=buy 的
            #   「退潮试错仓」，而这条买点自己的文案却写着"只做首板……不是首板就别碰"：
            #   候选列表于是一整屏都是"不是首板就别碰"的票，用户读到的就是
            #   "推荐了一堆马后炮"（10-13 反馈）。
            if boards == 1:
                return [{
                    "type": "B0", "name": "退潮试错仓 · 首板", "priority": "B",
                    "kind": "buy", "style": "short",
                    "trigger": f"退潮期只做首板（{sentiment.get('note') or ''}）",
                    "entry": (f"只做首板：今日已封板 → **不追当日价**，"
                              f"按次日竞价口径执行（高开 0~3% 是预期内、"
                              f"＞5% 属抢筹过头）；单笔不超过 {pos_cn(tw)}"),
                    "stop": "跌破买入价无条件走 —— 退潮期不留恋、不等反包",
                    "target": "小赚即走，不奢望连板",
                    "position": f"{pos_cn(tw)}（试错）",
                    "note": ("退潮期的试错仓是保持盘感用的，不是抄底；"
                             "宁可错过不可做错"),
                    "trial": True,
                }]
            if boards == 0:
                # 尚未涨停 → **今天没有可执行的买点**。给"等首板"的观察提示，
                # 而不是把它标成买入。这类票不进候选（候选 = 有可执行买点的票）。
                return [{
                    "type": "W0", "name": "等首板（退潮期只打首板）",
                    "priority": "C", "kind": "watch", "style": "short",
                    "trigger": "退潮期只做首板试错 —— 该股当前尚未涨停",
                    "entry": (f"留在观察里等它自己走出首板；不是首板就别碰。"
                              f"首板当日封板后按次日竞价口径，单笔 ≤{pos_cn(tw)}"),
                    "stop": "—",
                    "target": "—",
                    "position": "0%（当前不可买）",
                    "note": ("退潮期用一个极小仓位保持盘感，宁可错过不可做错；"
                             "先等首板，别提前埋伏"),
                    "trial": True,
                }]
            return [{
                "type": "HOLD", "name": "退潮期只做首板", "priority": "A",
                "kind": "block",
                "trigger": f"该股已 {boards} 连板 —— 退潮期不接力",
                "entry": f"退潮期只允许首板试错，上限 {pos_cn(tw)}；已连板的不参与",
                "stop": "—",
                "target": "等新周期首板",
                "position": "0%（新增）",
                "note": "退潮期接力的胜率极低，管住手",
                "blocked": True,
            }]
        reason = "情绪退潮期" if not sentiment.get("can_trade", True) else "极度缩量"
        return [{
            "type": "HOLD", "name": "禁止开仓", "priority": "A",
            "kind": "block",
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
            "kind": "block",
            "trigger": "；".join(fs.get("evidence", [])[:2]) or "庄股特征",
            "entry": "不参与",
            "stop": "若已持仓，遇断崖式下跌无条件出，绝不反包",
            "target": "—",
            "position": "0%",
            "note": "做庄失败打回原点，盈亏比极差",
            "blocked": True,
        }]

    # B1 首板试错：处于首板且情绪允许出手。
    # 门禁必须与「今日结论」一致：页面对 probe/follow/climax 都写着"可以出手 /
    # 小仓位试水"，却只在 probe/follow 给首板买点，climax 期一个买点都不给
    # —— 系统自己前后矛盾。首板的**次日竞价口径**在任何非退潮阶段都成立，
    # 故放宽到 probe/follow/climax（ebb/disabled 早已在上面的许可检查里挡掉）。
    sess = market_session()
    premarket = sess["phase"] in ("pre_open", "auction", "rest")
    # 盘前/竞价/休市时当日涨停还没发生，涨停池里的 boards 是上一交易
    # 日的 —— 只按实时涨幅判"今日已涨停"，否则盘前满屏假"涨停不追"
    at_limit = (stock.get("pct", 0) >= thr
                or (stock.get("zt_boards", 0) >= 1 and not premarket))
    # 进场时点要说准：盘前/竞价 → 就是今天这一场；周末/节假日休市 →
    # 下一场是「下个交易日」；盘中/盘后 → 涨停发生在今天，买点在「次日」。
    if sess["phase"] in ("pre_open", "auction"):
        open_prefix = "今日"
    elif sess["phase"] == "rest":
        open_prefix = "下个交易日"
    else:
        open_prefix = "次日"
    low1 = low_of(bars[-1])
    stop1 = _stop_price(low1, close)
    if (stock.get("zt_boards", 0) == 1
            and sentiment.get("stage") in ("probe", "follow", "climax")):
        note = "新周期一定是首板，二板还属于老周期"
        if at_limit:
            note += "；今日已封板，不追当日价，按次日口径执行"
        out.append({
            "type": "B1", "name": "首板试错",
            "priority": "A",
            "kind": "buy", "style": "short",
            "trigger": (f"新周期首板（{stock.get('sector') or '题材内'}）—— "
                        f"{open_prefix}竞价高开 0~3% 是预期内的，＞5% 就是抢筹过头"),
            # 分两笔的口径必须自洽：高开 0~3% 才买第一笔；高开 3~5% 先观望、
            # 回踩站稳再补；>5% 抢筹过头不追；低开破位直接放弃。
            # （原文写"高开>5% 等回踩补另一半"，可那时第一笔根本没成交，
            #   "补另一半"无从谈起。）
            "entry": (f"{open_prefix}竞价分两笔：① 高开 0~3% 先买一半；"
                      f"② 高开 3~5% 先观望，回踩 {close*0.98:.2f} 站稳再补另一半；"
                      f"③ 高开＞5% 属抢筹过头不追；低开跌破 {close*0.95:.2f} 直接放弃"),
            "stop": (f"跌破 {stop1:.2f}（约 {_loss_pct(stop1, close)}）无条件走 —— "
                     f"试错失败只亏很小一笔"),
            "target": "看二板：封住二板继续持有，二板炸板走人",
            "position": f"单票试错仓 {pos_cn(fw)}（分两笔进）",
            "note": note + "；买的是「明天有人接力」的预期，不是今天的涨停板",
        })

    # ---- M11 波段体系：第一/第二入场点、第二波接入（野哥的"平常打法"）----
    # 野人体系里波段是**常态**、短线只是缩量期的应对打法；而 v1.24.1 之前的买卖点
    # 全是短线口径。这里把三个波段入场点补进买点体系，并按 style_fit 把匹配的
    # 一路排在前面（波段票先看 W、短线票先看 B），避免"两套口径打架"。
    sw = stock.get("swing") or {}
    # ★ 节点门禁：波段（做长）只在「起步/扩散 + 非缩量」节点成立 ——
    #   退潮/极干/缩量/高潮期一律撤下波段买点（同样的票，节点不对就是绞肉机）。
    #   判定与 outlook 的「今日应对打法」同源（core/regime.py），从构造上不分叉。
    rg = regime.classify(sentiment, volume)
    _has_swing_setup = bool(sw.get("ok") and (
        sw.get("w1") or sw.get("w2") or (sw.get("w3") or {}).get("ready")))
    if sw.get("ok") and rg["allow_swing"]:
        w1 = sw.get("w1")
        if w1:
            out.append({
                "type": "W1", "name": "波段第一入场点", "priority": "A",
                "kind": "buy", "style": "swing",
                "trigger": (f"5日线上穿所有均线 + 20日线拐头向上；今日回踩{w1['line']} "
                            f"{w1['level']}（{'爆量确认' if w1.get('burst_ok') else '量能待确认'}）"),
                "entry": (f"回踩 {w1['level']} 附近建第一笔；"
                          f"没量就不动手（有量才有价），不追高"),
                "stop": (f"有效跌破 {w1['level']} 或 20 日线离场 —— "
                         f"回踩失败说明这不是第一入场点"),
                "target": "波段短则3个月、长则半年 —— 买的是信仰/逻辑/题材，不是三天涨跌",
                "position": "波段可重仓（锁仓=看懂主力意图）；先建一半，站稳再加",
                "note": "没人能买最低点，只能买相对低点；买完要扛得住洗盘震仓",
            })
        w2 = sw.get("w2")
        if w2:
            out.append({
                "type": "W2", "name": "波段第二入场点", "priority": "A",
                "kind": "buy", "style": "swing",
                "trigger": (f"{'从5日线急打到' if w2.get('from_ma5') else '回踩到'}"
                            f"20日线附近 {w2['level']}"
                            f"（{'缩量回踩·弱庄形态更可靠' if w2.get('shrink') else '量能待确认'}）"),
                "entry": f"20日线 {w2['level']} 附近介入；缩量回踩的才接",
                "stop": "放量跌破 20 日线 → 逻辑坏了，走（缩量跌破先看两天）",
                "target": "第二入场点是主力没走的证据（第一波涨幅不足以出货）",
                "position": "波段可重仓；沿 5 日线持有，小阴小阳不动手",
                "note": "强庄沿5日线小阴小阳走，某天从5日线打到20日线附近 = 第二入场点",
            })
        w3 = sw.get("w3")
        if w3 and w3.get("ready"):
            out.append({
                "type": "W3", "name": "波段第二波接入点", "priority": "B",
                "kind": "buy", "style": "swing",
                "trigger": (f"第一波已涨 {w3['gain']*100:.0f}%、回调 {w3['pullback']*100:.0f}%；"
                            f"5日线重新拐头向上 + 均线高度粘合（筹码交换充分）"),
                "entry": "等放量大阳线**突破后的回调**介入 —— 突破当天不追",
                "stop": "跌破 20 日线（第二波不成立）",
                "target": "高位横盘是在等 20/30 日均线上来、让获利盘换手；换手充分后面才拉得轻松",
                "position": "确认后再加，别一次打满",
                "note": "涨30%后回调主力没走（30%不足以出货）；止跌阳线可先轻仓保持关联",
            })
    elif _has_swing_setup and not rg["allow_swing"]:
        # 有波段形态、但节点不支持做长 —— 不给波段买点，只给一条门禁提示
        # （短线 B 信号不受影响，上面已按需产生）。
        out.append({
            "type": "HOLD", "name": "当前节点不做长", "priority": "A",
            "kind": "block", "blocked": True,
            "trigger": f'{rg["node_label"]}：{rg["mode_name"]} —— 波段（做长）当前不成立',
            "entry": "不新开波段仓；题材按短线纪律处理（快进快出）",
            "stop": "—",
            "target": "等节点转好（阶段回到起步/扩散、量能回到峰值）再看波段",
            "position": "0%（波段新增）",
            "note": "同样的票，主升期捂是机会，退潮/缩量期捂是绞肉机 —— "
                    "不是标的的事，是节点的事",
        })

    # 当日已涨停：B2/B3/B4 全是"当日进场"口径，涨停板上不成立 ——
    # 只保留 B1（次日介入口径），再给一条明确的"涨停不追"提示
    if at_limit:
        boards_now = stock.get("zt_boards", 0)
        board_txt = f"，{boards_now}板" if boards_now else ""
        nxt = ("首板次日看竞价：高开 0~3% 是预期内、＞5% 属抢筹过头"
               if boards_now <= 1 else
               f"{boards_now} 连板属高位，次日只看竞价强弱与能否回封，断板就走")
        out.append({
            "type": "HOLD", "name": "涨停不追", "priority": "A",
            "kind": "block",
            "trigger": (f"今日已封涨停（+{stock.get('pct',0):.1f}%{board_txt}）"
                        "，当日不给追买建议"),
            "entry": f"涨停板上买入=追明牌，炸板就是日内大面；要买等次日 —— {nxt}",
            "stop": "已持仓：次日炸板转弱按卖出信号离场，不恋战",
            "target": "—",
            "position": "0%（当日新增）",
            "note": "封死了买不到，能轻易买到大概率要炸 —— 明牌不追",
            "blocked": True,
        })
        return _ordered(out)

    after = sess["after_hours"]
    gap = pre_holiday_gap()

    # B2 龙头回踩：连板基因按"截至昨日近5日涨停次数"算 ——
    # zt_boards 是当日涨停池口径，回踩日必然不在池里，用它判 B2 这条信号永远出不来
    boards_recent = len(ind.count_limit_ups(bars[:-1], code, lookback=5)) if len(bars) > 1 else 0
    ma5 = ind.ma([b["close"] for b in bars], 5)
    ma10 = ind.ma([b["close"] for b in bars], 10)
    hold_before = cfg.get("M5_position", {}).get("hold_before_boards", 5)
    # 回踩成立的前提是**趋势没坏**：现价必须还在 10 日线上方。
    # 否则（现价已在 10 日线下方）那是破位不是回踩 —— 同时会与 S3 破位止损
    # 打架（同一只票既说"回踩可买"又说"破位离场"），且止损位会高于现价。
    if (2 <= boards_recent < hold_before and ma5 and ma10 and close > ma10
            and close <= ma5 * 1.03 and close >= ma5 * 0.95):
        # 止损 = max(10日线, 现价×(1-7%))：10日线是结构位，-7% 是单笔最大亏损上限
        stop2 = _stop_price(ma10, close)
        out.append({
            "type": "B2", "name": "龙头回踩",
            "priority": "A",
            "kind": "buy", "style": "short",
            "trigger": (f"近5日 {boards_recent} 次涨停的龙头回踩，现价 {close:.2f} "
                        f"贴近5日线 {ma5:.2f}（缩量不破）"),
            "entry": (f"回踩 {ma5:.2f}~{close*1.01:.2f} 区间分批介入，缩量企稳为佳；"
                      f"直接高开走强不追，等回踩再说"),
            "stop": (f"有效跌破 {stop2:.2f}（约 {_loss_pct(stop2, close)}，"
                     f"10日线与最大亏损双保险）离场"),
            "target": f"龙头走到 {hold_before} 板前不折腾，吃主升",
            "position": "去弱留强后最强者的加仓位（40-60%）",
            "note": "去弱留强不带幻想，砍错不心疼",
        })

    # B3 缩量红盘低吸
    if volume.get("red_flag") and stock.get("pct", 0) > 0:
        stop3 = close * 0.95
        out.append({
            "type": "B3", "name": "缩量红盘低吸",
            "priority": "B",
            "kind": "buy", "style": "short",
            # ★ 盘中 volume["amount"] 恒为 None（engine 的既有契约：不给全天外推
            #   数字），而 dict.get(k, 默认值) 在**键存在但值为 None** 时仍返回 None
            #   → f"{None:.0f}" 抛 TypeError，buy_signals 外层没有 try/except，
            #   **会打断整轮扫描**（实测：盘中命中缩量红盘时必崩）。
            #   盘中退回当日已成交额（cum_amount，事实值）。
            "trigger": (f"极致缩量（{(volume.get('amount') or volume.get('cum_amount') or 0):.0f}亿）仍红盘 "
                        f"{stock.get('pct',0):.2f}%，控盘好、筹码结构稳"),
            "entry": (f"不追高：回落到 {close*0.99:.2f} 附近分两笔低吸"
                      if not after else
                      f"次日开盘不追，回落到 {close*0.99:.2f} 附近分两笔低吸"),
            "stop": f"跌破 {stop3:.2f}（-5%）止损，缩量期不硬扛",
            "target": "先看反包昨日高点：反包不上就走",
            "position": "小仓位试探（缩量市场只信量能）",
            "note": "散户90%套牢躺平、庄家控盘好；缩量期权宜打法，不宜重仓接",
        })

    # B4 尾盘博弈：只做节前最后交易日的尾盘窗口 ——
    # 非尾盘时段、非节前、当日已涨停，都不给（以前只看价格区间，
    # 涨停票照样推"尾盘买入"，是假信号）
    if (a.get("price_min", 5) <= stock.get("price", 0) <= a.get("price_max", 10)
            and sess["in_tail"] and gap >= 4):
        out.append({
            "type": "B4", "name": "尾盘博弈",
            "priority": "C",
            "kind": "buy", "style": "short",
            "trigger": f"节前最后交易日（距下一交易日 {gap} 天），已到尾盘窗口（14:00后）",
            "entry": "现在到 14:30 之间小仓位入场，避开假期消息面的不确定性",
            "stop": "次日低开超3% 走人",
            "target": "节后情绪延续则持有",
            "position": "小仓位",
            "note": "节假日博弈只在尾盘入场，非尾盘时段不出此信号",
        })

    return _ordered(out)


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

    # ---- M11 波段离场点：高位放量大涨滞涨 = 有人在出货 ----
    # 野哥的第一离场点。短线体系里没有这条（S2 只看连板高度），而波段票的
    # 顶部往往不是"连板断了"，是"放量了但价不涨了"。
    sw = stock.get("swing") or {}
    ex = sw.get("exit") if sw.get("ok") else None
    if ex:
        out.append({
            "type": "WS1", "name": "波段离场点·高位放量滞涨", "priority": "A",
            "style": "swing", "urgency": "high",
            "trigger": (f"高位（60日位置 {ex['pos']*100:.0f}%）放量 {ex['vol_ratio']} 倍，"
                        f"却只涨 {ex['pct']:+.2f}% —— 有人在出货"),
            "action": ("先把利润装进口袋（减仓/清仓）。放量大涨的底层逻辑是有人在出货，"
                       "底下托单维持股价不让你发现"),
            "note": ("若之后跌不下去（震荡两天、货出完股价不跌）= 有人接得住，"
                     "那反而是第二入场点 —— 别一刀砍死"),
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

    # 排序：critical 永远第一（无条件清仓/立即离场）；其余按打法匹配优先
    # （波段票先看波段离场点，短线票先看短线的）
    _want_swing = ((stock.get("style_fit") or {}).get("key") == "swing")

    def _skey(s):
        crit = 0 if s.get("urgency") == "critical" else 1
        mism = 0 if ((s.get("style") == "swing") == _want_swing) else 1
        return (crit, mism)
    return sorted(out, key=_skey)


# ---------------------------------------------------------------- 仓位链


def pos_cn(w: float) -> str:
    """仓位的中文写法：0.05 → '0.5 成（5%）'。

    交易员嘴里说的都是"几成仓"，直接摆百分比还得自己换算，
    所以两样一起给，避免"0.5 成"被误读成 0.5%。
    """
    return f"{w*10:g} 成（{w*100:g}%）"

def position_plan(cfg: dict, picks: list[dict], sentiment: dict,
                  volume: dict, macro: dict) -> dict:
    """M5 仓位操作系统：试错 → 加仓 → 推仓 → 锁仓。"""
    p = cfg.get("M5_position", {})
    if not p.get("enabled", True):
        # ★ 必须和其它分支**同形**：前端读 pos.target_total / pos.chain_stage
        #   （index.html 的结论卡「总仓位」、决策步骤第 5 问、仓位卡），
        #   缺了它们就是 undefined*100 → 界面显示 "NaN%"。chain_stage 用
        #   "disabled"，前端 STAGE_CN 已把它映射成「已关闭」。
        return {"chain": [], "total": 0, "target_total": 0.0,
                "chain_stage": "disabled",
                "note": "仓位模块已关闭，自行控制"}

    n = p.get("pick_count", 4)
    first_w = p.get("first_weight", 0.20)
    cash = p.get("cash_reserve", 0.20)
    max_single = p.get("max_single_weight", 0.60)
    hold_before = p.get("hold_before_boards", 5)

    # ★ 数据未就绪闸门（v1.38.6）：没数据时**给不出任何仓位**。
    #   必须读 data_ready 而不是只看 stage —— stage="unknown" 分不出
    #   「接口没取到」和「今天真的没有涨停」这两件事（前者是暂态、后者是市场
    #   事实），只看 stage 就会在冷启动时按"无信号"给出 Day1 平铺链：
    #   strong_w = min(max_single, 1-cash/2) = 0.6 → 目标总仓位 60%。
    #   这正是用户看到的「先喊可以出手」在仓位卡上的那一半。
    #   放在退潮分支**之前**：未就绪时连"退潮试错仓"都不给 —— 试错仓的规矩
    #   是"只做首板、亏了就走"，而没数据时连首板名单本身都是残缺的。
    if not sentiment.get("data_ready", True):
        return {
            "chain": [{"stage": "数据未就绪", "action": "暂不给仓位建议",
                       "weight": 0.0, "total": 0.0,
                       "note": "关键池数据没到手，仓位链按空仓处理"}],
            "target_total": 0.0,
            "chain_stage": "not_ready",
            "note": (sentiment.get("not_ready_reason")
                     or "关键数据（涨停/跌停/炸板池）本次未取到，"
                        "情绪状态无法判断 —— 先空仓，等数据齐了再谈仓位"),
        }

    # 退潮期 → 默认空仓；但允许一笔 0.5 成试错仓（野哥的规矩）
    if sentiment.get("stage") == "ebb":
        tw = p.get("ebb_trial_weight", sentiment.get("ebb_trial_weight", 0.05))
        try:
            tw = max(0.0, float(tw or 0.0))
        except (TypeError, ValueError):
            tw = 0.0
        if tw > 0:
            return {
                "chain": [
                    {"stage": "退潮试错", "action": f"只做首板，单笔不超过 {pos_cn(tw)}",
                     "weight": round(tw, 4), "total": round(tw, 4),
                     "note": "试错仓是保持手感、维持盘感用的，不是抄底；宁可错过，不可做错"},
                    {"stage": "等待", "action": "空仓等新周期首板", "weight": 0.0,
                     "total": 0.0, "note": "新周期一定是首板"},
                ],
                "target_total": round(tw, 4),
                "chain_stage": "ebb",
                "trial": True,
                "note": (f"退潮期：不接力、不重仓。最多 {tw*100:.1f}% 试错仓，"
                         f"只做首板，亏了立刻走"),
            }
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
