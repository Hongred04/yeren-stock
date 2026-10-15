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

from . import regime
from . import trading


def _next_trade_date(real_date: str, holidays=None) -> tuple[str, bool]:
    """real_date（YYYYMMDD）之后的下一个交易日，返回 ('MM-DD', 是否精确)。

    ★ v1.36.0：原来只做"工作日 +1 跳周末"，**不看休市日历**，节前会把"明日"
    写成实际休市的日期（10-01 会写成 10-02），整张「明日判断」卡的日期是错的。

    第二个返回值是**精确性标志**，供调用方决定措辞：
      · True  —— 算出来的日期确定是交易日（跳过了周末 + 命中了休市表，
                 或本机日期本就远离任何已知假期）；
      · False —— 只跳了周末，休市表里没有覆盖到这个年份，日期可能因法定
                 节假日而顺延，文案必须保留"以交易所公告为准"的免责。
    绝不猜：宁可标 False 让界面说"估算"，也不能让用户以为日期是准的。
    """
    try:
        d = datetime.strptime(real_date, "%Y%m%d")
    except (ValueError, TypeError):
        d = datetime.now()
    # 休市表：接受 {"20261001", ...} 或 [20261001, ...] 两种写法
    hs: set[str] = set()
    for x in (holidays or ()):
        s = str(x).strip()
        if s:
            hs.add(s)
    d += timedelta(days=1)
    for _ in range(30):                    # 上限 30 天，防脏数据死循环
        ds_ = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds_ not in hs:
            break
        d += timedelta(days=1)
    # 表里**没有任何**这一年附近的条目 → 无法确认，标 False（保留免责）
    exact = bool(hs) and any(s[:4] >= d.strftime("%Y")[:4] for s in hs)
    return d.strftime("%m-%d"), exact


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
          macro: dict | None = None, plan: dict | None = None,
          sowing: dict | None = None) -> dict:
    """构建明日判断。所有入参都是本轮分析已算出的结果，不再打接口。"""
    sentiment = sentiment or {}
    volume = volume or {}
    meter = meter or {}
    ml = ml or {}
    flow = flow or {}
    sectors = sectors or {}
    plan = plan or {}
    sowing = sowing or {}

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

    # 亏钱面积警戒线读配置（M3_sentiment.ebb_hurt_ratio，默认 0.30）——
    # 不写死：改了配置，界面文案要跟着变（硬编码阈值 = 文案与配置脱节）。
    try:
        hurt_th = float((cfg.get("M3_sentiment", {}) or {}).get("ebb_hurt_ratio", 0.30))
    except (TypeError, ValueError):
        hurt_th = 0.30
    th_txt = f"{hurt_th * 100:.0f}%"
    watch_th = max(0.0, hurt_th - 0.05)   # 比警戒线低 5 个点就开始盯

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

    # ---- E8 题材播种（事件→受益→标的）· 前瞻线 ----
    # 用户 10-13：「打板要提前找题材」。E8 的三个信号是**前瞻**的：今天的新播种
    # 题材、独苗首板、标签漂移的重复启动票 —— 它们不是"今天谁最强"（那是 mainline/
    # sectors 的事），而是"明天该盯着谁有没有第二只跟上"。这里把它并进明日预案。
    sow_themes = sowing.get("themes") or []
    sow_lonely = sowing.get("lonely") or []
    sow_restarts = sowing.get("restarts") or []
    sow_new = [t.get("theme") for t in sow_themes if t.get("trend") == "new"]
    sow_lonely_names = [x.get("name") for x in sow_lonely if x.get("name")]
    sow_drift = [r.get("name") for r in sow_restarts if r.get("drifted")]

    if sow_new or sow_lonely_names or sow_drift:
        bits = []
        if sow_new:
            bits.append(f"新播种 {len(sow_new)} 个（{'、'.join(str(t) for t in sow_new[:3])}）")
        if sow_lonely_names:
            bits.append(f"独苗 {len(sow_lonely_names)} 个（{'、'.join(str(n) for n in sow_lonely_names[:3])}）")
        if sow_drift:
            bits.append(f"标签漂移 {len(sow_drift)} 只（{'、'.join(str(n) for n in sow_drift[:3])}）")
        points.append({"k": "题材播种", "v": "；".join(bits),
                       "note": ("明日看：新播种题材有没有第二只跟上（有第二只才谈主线）、"
                                "独苗能否晋级二板、漂移票是否续强（续强=逻辑接力成立，"
                                "断板=逻辑证伪）")})

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
    if hurt >= watch_th or stage == "ebb":
        watch.append(f"亏钱面积（跌停+炸板占比）—— 现在 {hurt*100:.0f}%，"
                     f"回落到 {th_txt} 以下才算修复")
    else:
        watch.append(f"亏钱面积（跌停+炸板占比）—— 现在 {hurt*100:.0f}%，"
                     f"上行穿越 {th_txt} 就是退潮预警")
    if theme:
        watch.append(f"「{theme}」连板梯队 —— 高度板是否续板、首板还有没有活口")
    if strongest.get("name"):
        watch.append(f"最强板块「{strongest['name']}」的龙头 —— 续强则板块延续，断板则资金要转场")

    # E8 前瞻线：新播种题材 / 漂移票，是"明天该盯哪几条线"里最前瞻的两条
    if sow_new:
        watch.append(f"新播种题材「{'、'.join(str(t) for t in sow_new[:3])}」—— "
                     f"明天有没有第二只跟上，有第二只才谈主线")
    if sow_drift:
        watch.append(f"标签漂移的「{'、'.join(str(n) for n in sow_drift[:3])}」—— "
                     f"明天续强 = 逻辑接力成立，断板 = 逻辑证伪")

    # ---- 情景预案 ----
    scenarios: list[dict] = []
    if stage == "ebb" or vol_mode == "dry":
        scenarios.append({"cond": f"若明日开盘亏钱面积回落到 {th_txt} 以下、且出现 3 家以上新首板",
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
    if sow_new and stage not in ("ebb",) and vol_mode != "dry":
        scenarios.append({"cond": f"若新播种题材「{'、'.join(str(t) for t in sow_new[:2])}」"
                                  f"出现第二只、独苗晋级二板",
                          "then": "按新周期题材对待：优先进首板（试错仓），确认后再加仓"})

    # 下一交易日：能确认就用准确措辞，不能确认就保留"估算 + 免责"（v1.36.0）
    _holidays = (cfg.get("M7_earnings") or {}).get("market_holidays") or []
    next_date, next_exact = _next_trade_date(real_date, _holidays)
    tail = ("明日为交易日（已按休市日历核对）。"
            "以上是推演不是预测 —— 开盘后以量能和情绪的实际读数修正。"
            if next_exact else
            "明日为工作日估算日期（节假日顺延以交易所公告为准）。"
            "以上是推演不是预测 —— 开盘后以量能和情绪的实际读数修正。")
    # ---- 野人节奏：随盘面阶段换交易系统 ----
    # 「熊市不做长线，牛市不做短线……随盘面变化去改变自己的交易系统」。
    # 把「当前阶段该用什么打法」直接说出来，不让你自己猜。
    # ★ 打法 = M3 阶段（能不能出手）× M2 量能（撬不撬得动大票）的**组合结论**，
    #   不能只看阶段：高潮期撞上极度缩量时，阶段口径喊「短线快枪」、量能口径喊
    #   「空仓等待」，同屏两句行动指令必打架（用户 10-12 实测同类问题）。
    #   优先级：退潮 > 极度缩量 > 缩量切短线 > 阶段默认。
    # 打法 = 节点结论（M3 阶段 × M2 量能），**单一出处见 core/regime.py** ——
    # trading 的波段买点门禁读同一个函数，两处不会再各算一套而打架。
    # 优先级（退潮 > 极度缩量 > 缩量 > 阶段默认）也收在 regime 里。
    rg = regime.classify(sentiment, volume, macro)
    mode_name, mode_note = rg["mode_name"], rg["mode_note"]
    rot = (ml or {}).get("rotation_note") or ""
    if rot:
        mode_note = f"{mode_note}。{rot}"
    dn = _disclosure_note(real_date)
    if dn:
        mode_note = f"{mode_note}。{dn}"

    return {
        "date": next_date, "date_exact": next_exact,
        "headline": head, "tone": tone,
        "mode": {"name": mode_name, "note": mode_note},
        "points": points, "watch": watch, "scenarios": scenarios,
        "note": tail,
    }
