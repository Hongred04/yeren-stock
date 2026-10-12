# -*- coding: utf-8 -*-
"""明日判断预测 —— 基于情绪/量能/题材/资金/板块的次日推演。

不是算命，是把今天收盘时确认过的状态，翻译成「明天开盘先看什么、
什么情况下能动手、什么情况下继续守」的行动预案：

  · headline 一句话定调（防守 / 均衡 / 试攻）
  · points   五个维度的判断（情绪/量能/题材/资金/板块）
  · watch    明日开盘要盯的几件事（带阈值）
  · scenarios 若…则… 的情景预案

日期按工作日估算（法定节假日可能顺延），界面上会如实标注。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import trading


def _next_trade_date(real_date: str) -> str:
    """real_date（YYYYMMDD）之后的下一个工作日，'10-12' 格式。"""
    try:
        d = datetime.strptime(real_date, "%Y%m%d")
    except (ValueError, TypeError):
        d = datetime.now()
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%m-%d")


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


_DISCLOSURE_MONTHS = {
    1: "年报预告窗口", 4: "年报/一季报披露窗口", 7: "中报预告窗口",
    8: "中报披露窗口", 10: "三季报披露窗口",
}


def _disclosure_note(real_date: str) -> str:
    """业绩披露窗口提示 —— 野人 10-08：「马上进入业绩预披露期，大资金谨慎
    （不信 Q3 比 Q2 好）→ 存量资金互掏口袋」。披露期结束大资金才会回来。"""
    try:
        m = int(real_date[4:6])
    except (ValueError, IndexError, TypeError):
        return ""
    label = _DISCLOSURE_MONTHS.get(m)
    if not label:
        return ""
    return (f"现在是{label}：大资金对业绩谨慎、盘面容易走成存量博弈（互掏口袋）——"
            "题材当短线看、不格局；等业绩落地，新子弹才会来")


def build(cfg: dict, real_date: str, sentiment: dict, volume: dict,
          meter: dict | None = None, ml: dict | None = None,
          flow: dict | None = None, sectors: dict | None = None,
          macro: dict | None = None, plan: dict | None = None) -> dict:
    """构建明日判断。所有入参都是本轮分析已算出的结果，不再打接口。"""
    sentiment = sentiment or {}
    volume = volume or {}
    meter = meter or {}
    ml = ml or {}
    flow = flow or {}
    sectors = sectors or {}
    plan = plan or {}

    stage = sentiment.get("stage", "unknown")
    mm = sentiment.get("metrics") or {}
    mtr = meter.get("metrics") or {}
    vol_mode = volume.get("mode", "unknown")
    # 盘中 amount 是 None（不给全天外推数字）—— 那就报"已成交"，别显示 0
    vol_amt = _f(volume.get("amount")) or _f(volume.get("cum_amount"))
    theme = ml.get("theme") or ""
    ml_status = ml.get("status") or ""
    capital = flow.get("capital") or {}
    strongest = sectors.get("strongest") or {}

    hurt = _f(mm.get("hurt_area"))
    n_dt = int(_f(mm.get("limit_down")))
    promote = mtr.get("promote_rate")
    money_effect = mtr.get("money_effect")
    temp = meter.get("temperature")

    def _p(v, suffix="%", d=1):
        return "—" if v is None else f"{_f(v):.{d}f}{suffix}"

    # ---- 定调 ----
    if stage == "ebb":
        tone, head = "defense", "退潮未结束：明日先看亏钱面积能否回落，防守为主"
    elif vol_mode == "dry":
        tone, head = "defense", "极度缩量：量能没回来之前，反弹都是弱反弹"
    elif stage == "climax":
        tone, head = "neutral", "高位博弈：明日防断板 —— 龙头梯队断层就是退潮起点"
    elif stage == "probe":
        tone, head = "attack", "新周期起步：明日看首板能否晋级二板"
    elif stage == "follow":
        tone, head = "neutral", "扩散跟风期：明日看龙头能否续强、跟风接不接得上"
    else:
        tone, head = "neutral", "方向未明：明日先看量和情绪，别抢跑"

    # ---- 五维判断 ----
    points: list[dict] = []

    s_txt = sentiment.get("label") or stage
    extra = []
    if temp is not None:
        extra.append(f"温度 {temp}°")
    if promote is not None:
        extra.append(f"晋级率 {_p(promote * 100, '', 0)}%")
    if money_effect is not None:
        extra.append(f"赚钱效应 {_p(money_effect)}")
    points.append({"k": "情绪",
                   "v": f"{s_txt}" + ("（" + "·".join(extra) + "）" if extra else ""),
                   "note": ("明日早盘先看亏钱面积（现在 " + f"{hurt*100:.0f}%" +
                            "）：回落代表修复，继续抬升就是退潮延续"
                            if stage == "ebb" else
                            "情绪位置决定仓位：启动期试错、高潮期防退潮")})

    vol_note = (f"现在 {vol_amt:.0f} 亿（{volume.get('basis') or ''}）。"
                if vol_amt else "")
    vol_note += ("明日开盘半小时的成交额比今天同期放大，反弹才有底气；继续缩 = 只守不攻"
                 if vol_mode in ("shrink_short", "shrink_red", "dry") else
                 "量能在位，按计划操作即可")
    points.append({"k": "量能", "v": volume.get("label") or vol_mode, "note": vol_note})

    if ml_status == "confirmed":
        streak = int(_f(ml.get("streak")))
        t_note = (f"主线「{theme}」" + (f"已连续 {streak} 日活跃、" if streak >= 2 else "")
                  + f"五信号共振 —— 明日盯：空间龙继续高开、"
                  f"昨日首板有活口、中军不砸盘，三者任一走坏按退潮预备处理")
    elif ml_status == "day1_pending":
        _st = int(_f(ml.get("streak"), 0))
        if _st >= 2:
            t_note = (f"「{theme}」已连续 {_st} 日活跃 —— 今日合力重新成立但待确认，"
                      "明日开盘看：空间龙溢价、首板留存、中军反馈")
        else:
            t_note = (f"主线「{theme}」首日启动、还没确认 —— 明日开盘三件事："
                      f"空间龙溢价、首板留存、中军反馈；过线才叫主线")
    elif ml_status == "one_day":
        _st = int(_f(ml.get("streak"), 0))
        t_note = (f"「{theme}」已连续 {_st} 日活跃，不是一日游 —— 但主线不成立"
                  f"（游资行情、容量有限）：明日轻仓短炒或不参与，当短线看、不格局"
                  if _st >= 2 else
                  "题材一日游特征 —— 明日不接力，等下一个方向自己走出来")
    elif ml_status == "watch":
        streak = int(_f(ml.get("streak")))
        if streak >= 2 and theme:
            t_note = (f"「{theme}」已连续 {streak} 日活跃、但承接数据不足 —— "
                      f"明日看老龙能否新高、中军是否稳住再定性")
        else:
            t_note = "主线信号不全 —— 明日先看清方向再加进来，别对着半截信号下注"
    else:
        t_note = "题材状态待积累 —— 明日以涨停梯度和早封家数为准"
    points.append({"k": "题材", "v": theme or "未确认", "note": t_note})

    points.append({"k": "资金",
                   "v": capital.get("headline") or flow.get("summary") or "数据不足",
                   "note": flow.get("source_note") or ""})

    if strongest.get("name"):
        pts = "、".join(strongest.get("facts") or [])
        points.append({"k": "最强板块", "v": strongest.get("name", ""),
                       "note": (pts + "。" if pts else "") +
                               "明日看它的梯队是加固还是断层：加固=主线巩固，断层=资金转场"})
    else:
        points.append({"k": "最强板块", "v": "无合力方向", "note": "板块合力不足，明日只做个股不做板块"})

    # ---- 盯什么（带阈值）----
    watch: list[str] = []
    if vol_mode in ("shrink_short", "shrink_red", "dry"):
        watch.append("开盘半小时两市成交额 —— 比今日同期放大才谈进攻")
    if hurt >= 0.25 or stage == "ebb":
        watch.append(f"亏钱面积（跌停+炸板占比）—— 现在 {hurt*100:.0f}%，"
                     f"回落到 30% 以下才算修复")
    else:
        watch.append(f"亏钱面积（跌停+炸板占比）—— 现在 {hurt*100:.0f}%，"
                     f"上行穿越 30% 就是退潮预警")
    if theme:
        watch.append(f"「{theme}」连板梯队 —— 高度板是否续板、首板还有没有活口")
    if strongest.get("name"):
        watch.append(f"最强板块「{strongest['name']}」的龙头 —— 续强则板块延续，断板则资金要转场")

    # ---- 情景预案 ----
    scenarios: list[dict] = []
    if stage == "ebb" or vol_mode == "dry":
        scenarios.append({"cond": "若明日开盘亏钱面积回落到 30% 以下、且出现 3 家以上新首板",
                          "then": "按新周期起步对待：只用试错仓（首板），亏了就走"})
        scenarios.append({"cond": "若跌停家数继续增加、亏钱面积抬升",
                          "then": "继续防守：不接力、不低吸，等新周期首板"})
    else:
        scenarios.append({"cond": "若量能比今日同期放大、高度板不死",
                          "then": f"按计划出手：目标仓位 {plan.get('target_total', 0)*100:.0f}%，"
                                  f"首选{'主线「' + theme + '」' if theme else '最强板块'}的最强一只"})
        scenarios.append({"cond": "若量能继续缩、高位股开始断板",
                          "then": "降仓等待：只留最强，其余兑现，等量能回来"})
    if ml_status == "day1_pending" and theme:
        scenarios.append({"cond": f"若「{theme}」首板批量晋级、空间龙高开",
                          "then": "主线确认，按主线节奏做龙头与中军"})

    tail = ("明日为工作日估算日期（节假日顺延以交易所公告为准）。"
            "以上是推演不是预测 —— 开盘后以量能和情绪的实际读数修正。")
    # ---- 野人节奏：随盘面阶段换交易系统 ----
    # 「熊市不做长线，牛市不做短线……随盘面变化去改变自己的交易系统」。
    # 把「当前阶段该用什么打法」直接说出来，不让你自己猜。
    mode_map = {
        "ebb": ("防守空仓", "空仓或试错仓，不做长线 —— 保存子弹；市场不好的时候"
                              "恰恰是执行去弱留强的时点"),
        "ice": ("休息", "休息为主，空仓等启动信号 —— 手痒也别做长线"),
        "revive": ("轻仓试错", "轻仓试错 + 波段思维 —— 只看低位、资金刚动的地方"
                              "（要信早信），拿得住"),
        "probe": ("波段试错", "新方向刚起步 —— 低位+资金刚动的地方跟进去"
                             "（要信早信），拿得住"),
        "follow": ("波段持股", "扩散期波段持股 —— 主线内不轻易换，拿得住才有肉"),
        "ferment": ("波段持股", "发酵期波段持股 —— 主线内不轻易换，吃主升"),
        "climax": ("短线快枪", "高潮期短线题材思维 —— 存量博弈不格局，快进快出，"
                              "同时执行去弱留强"),
    }
    mode_name, mode_note = mode_map.get(stage, ("观察", "方向未明 —— 先看清阶段，再选打法"))
    rot = (ml or {}).get("rotation_note") or ""
    if rot:
        mode_note = f"{mode_note}。{rot}"
    dn = _disclosure_note(real_date)
    if dn:
        mode_note = f"{mode_note}。{dn}"

    return {
        "date": _next_trade_date(real_date),
        "headline": head, "tone": tone,
        "mode": {"name": mode_name, "note": mode_note},
        "points": points, "watch": watch, "scenarios": scenarios,
        "note": tail,
    }
