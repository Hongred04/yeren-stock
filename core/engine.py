# -*- coding: utf-8 -*-
"""策略引擎：把九大模块的判断逻辑编码为可执行判定。

核心链路（与用户体系一致）：
  宏观周期定牛熊 → 量能定模式 → 情绪周期定时点
  → 审美三要素定标的 → 平铺去弱留强定仓位 → 纪律保命
  → M9 题材主线五信号定性 → M10 主导资金分类定离场
"""
from __future__ import annotations

from datetime import datetime

from . import chips as chips_mod
from . import fundstyle
from . import indicators as ind
from . import stage_stabilizer as stab
from . import trading
from . import universe


# ================================================================ M1 宏观周期

# 用户提供的周期事件表（用于回溯展示与当前定位）
MACRO_EVENTS = [
    {"period": "2008", "event": "次贷危机·危机式降息", "market": "6124→1664",
     "conclusion": "危机式降息≠利好", "tag": "bear", "date": "2008"},
    {"period": "2015.12-2018.12", "event": "美联储加息", "market": "上证3500→2440",
     "conclusion": "加利空", "tag": "bear", "date": "2015-12"},
    {"period": "2022.3-2023.7", "event": "暴力加息525BP", "market": "全年承压，高估值成长杀底",
     "conclusion": "见底信号", "tag": "bottom", "date": "2022-03"},
    {"period": "2024.9.18", "event": "开启降息", "market": "九二四行情，大金融领涨、大盘涨停",
     "conclusion": "降息+降准=大金融波段", "tag": "bull", "date": "2024-09-18"},
    {"period": "2027.9.18", "event": "老美开启加息", "market": "短期利好、长期触动流动性",
     "conclusion": "当前警惕对象", "tag": "caution", "date": "2027-09-18"},
]


def evaluate_macro(cfg: dict) -> dict:
    """M1 宏观周期评估。支持手工覆盖。"""
    m = cfg.get("M1_macro", {})
    if not m.get("enabled", True):
        return {"stage": "disabled", "label": "宏观模块已关闭", "risk": "neutral",
                "focus": [], "note": "不参与周期判断，仅按量能与情绪操作"}

    ov = m.get("manual_override", "auto")
    now = datetime.now()
    if ov != "auto":
        mapping = {
            "bull_finance": ("bull_finance", "大金融波段", "low",
                             ["银行", "证券", "保险", "房地产"]),
            "bull_tech": ("bull_tech", "大科技主升", "low",
                          ["半导体", "算力", "AI应用", "消费电子"]),
            "caution": ("caution", "加息/流动性收紧，警惕", "high", []),
            "risk_off": ("risk_off", "危机式降息，风险规避", "high", ["黄金", "防御"]),
            "bottom": ("bottom", "加息末期见底信号", "medium", []),
        }
        key, label, risk, focus = mapping.get(ov, mapping["caution"])
        return {"stage": key, "label": label, "risk": risk, "focus": focus,
                "note": "手工覆盖模式", "manual": True}

    # 自动模式：按当前时间落在哪个周期区间
    y = now.year
    if y < 2024:
        stage, label, risk = "caution", "历史区间，参考2024.9降息起点", "medium"
        focus = []
    elif y == 2024:
        stage, label, risk = "bull_finance", "2024.9.18 降息开启 → 大金融波段", "low"
        focus = ["银行", "证券", "保险"]
    elif y == 2025:
        stage, label, risk = "bull_tech", "降息延续 → 科技成长主升", "low"
        focus = ["半导体", "算力", "AI应用"]
    elif y == 2026:
        stage, label, risk = "bull_tech", "降息周期延续 → 科技仍是主线", "low"
        focus = ["半导体", "算力", "AI应用", "机器人"]
    else:
        stage, label, risk = "caution", "2027.9.18 老美开启加息 → 警惕流动性收紧", "high"
        focus = ["高股息", "防御"]

    return {
        "stage": stage, "label": label, "risk": risk, "focus": focus,
        "note": "自动模式：以美联储周期定位，" + ("当前处于降息周期" if y <= 2026 else "已进入加息预警区"),
        "manual": False,
    }


# ================================================================ M2 量能模式

VOLUME_MODES = {
    "peak_swing": "峰值量能",
    "comfort_short": "量能舒适",
    "shrink_short": "缩量存量",
    "shrink_red": "极致缩量还红盘",
    "dry": "极度缩量",
    "pending": "量能未就绪",
}
# ★ label 只描述**市场量能状态**，不夹带行动指令/选股口径：
#   「峰值量能」是状态，「做中长线波段大票」是打法 —— 市场量能状态 ≠ 个股选股
#   口径，混在一句话里必打架（用户 10-12 实测：副标题「中长线波段大票」与打法框
#   「短线快枪」同屏自相矛盾）。选股口径只出现在 outlook.mode（今日应对打法）
#   与 M11 波段面板；量能对应的行动建议在本模块的 advice 字段里，按量能口径呈现。

# A股成交额日内累计占比经验曲线：开盘急、午间缓、尾盘回升。
# 量能阈值都是"全天成交额"口径，盘中拿累计值直接比阈值，
# 早盘必然误判成极度缩量 —— 按已交易分钟折算成预估全天额再比。
_DAY_PROFILE = [(0, 0.02), (15, 0.10), (30, 0.18), (60, 0.28),
                (120, 0.45), (180, 0.68), (210, 0.85), (240, 1.0)]


def _traded_fraction(tmin: int) -> float:
    tmin = max(0, min(240, int(tmin)))
    for (t1, f1), (t2, f2) in zip(_DAY_PROFILE, _DAY_PROFILE[1:]):
        if tmin <= t2:
            return f1 + (f2 - f1) * (tmin - t1) / (t2 - t1)
    return 1.0


def evaluate_volume(cfg: dict, market_amount: float, hist: list[dict],
                    session: dict | None = None,
                    fallback_amount: float = 0.0,
                    fallback_final: bool = True,
                    frac_hint: float | None = None,
                    ref_amount: float = 0.0) -> dict:
    """M2 量能定模式 —— 整个体系的模式总开关。

    session/fallback_amount：量能阈值是「全天成交额」口径，而快照里的
    成交额是当日累计值 —— 集合竞价时约等于零，早盘也只有两三成。
    盘前/非交易时段改用上一交易日的全天额（本地持久化），收盘后直读。
    口径写进 basis，界面上跟着显示，杜绝"没开盘就说极度缩量"这类假结论。

    fallback_final：上一交易日那条记录是不是**收盘后**的全天终值。盘中只扫到
    一半的值明显偏低，界面必须说明，不能冒充"全天"。

    frac_hint：由调用方从历史实测曲线算出的「截至当前时刻的累计占比」。

    ★ 盘中**不外推全天额**（v1.24.1）：拿一条平均曲线把几十分钟的进度放大到
    全天，误差大到没有参考价值 —— 实测 10:29 已成交 7795 亿 → 外推 28487 亿，
    比前一天全天还多 50%，而同期基准当时只有 1 天样本。所以盘中 `amount` 一律
    返回 None，界面只报"已成交"（事实）+ "与近期同期的节奏对比"（也是事实）；
    模式判定仍按折算走，但明确标成"暂判"。收盘后 `amount` 才是全天终值。
    ref_amount：近 N 个已收盘交易日全天额中位数，用于算节奏倍数。
    """
    v = cfg.get("M2_volume", {})
    if not v.get("enabled", True):
        return {"mode": "disabled", "label": "量能模块已关闭", "advice": "自行判断",
                "short_only": False, "amount": None, "cum_amount": 0.0,
                "intraday": False, "pace": None, "pace_txt": "", "basis": ""}

    amt_raw = market_amount / 1e8 if market_amount > 1e5 else market_amount
    phase = (session or {}).get("phase", "")
    tmin = (session or {}).get("tmin", 240)
    intraday = phase in ("morning", "noon", "afternoon", "tail")

    if phase in ("pre_open", "auction", "rest"):
        if fallback_amount and fallback_amount > 0:
            amt = fallback_amount
            basis = ("上一完整交易日全天" if fallback_final
                     else "上一交易日盘中快照（偏低，仅供参考）")
        else:
            return {
                "mode": "pending", "label": VOLUME_MODES["pending"],
                "advice": ("当前是盘前/非交易时段，当日量能还没形成 —— "
                           "别急着定模式，开盘半小时后再看量能"),
                "short_only": False,
                "amount": None, "cum_amount": 0.0, "intraday": False,
                "pace": None, "pace_txt": "", "raw_amount": round(amt_raw, 0),
                "basis": "无（当日数据未形成）", "red_flag": "",
            }
    elif phase in ("just_closed", "after_close"):
        amt = amt_raw
        basis = "当日全天（已收盘）"
    elif intraday:
        if amt_raw <= 0:
            # 盘中但累计额为 0：数据未就绪（接口异常/刚开盘第一分钟）
            return {
                "mode": "pending", "label": VOLUME_MODES["pending"],
                "advice": ("盘中但成交额数据未形成 —— 等数据就绪后再定模式，"
                           "先不动手"),
                "short_only": False,
                "amount": None, "cum_amount": 0.0, "intraday": True,
                "pace": None, "pace_txt": "", "raw_amount": 0,
                "basis": "无（盘中数据未就绪）", "red_flag": "",
            }
        frac_static = max(_traded_fraction(tmin), 0.02)
        frac, frac_src = frac_static, "经验曲线"
        if frac_hint and 0.02 <= float(frac_hint) <= 1.0:
            # 实测曲线优先：不同交易日的量能形态差别很大（放量日开盘更集中、
            # 缩量日更平），写死的曲线必然有系统偏差
            frac, frac_src = max(float(frac_hint), 0.02), "近N日实测"
        # 内部折算只用于**模式判定**，不对外输出全天数字
        amt = min(max(amt_raw / frac, amt_raw), amt_raw * 6)
        basis = f"盘中累计{amt_raw:.0f}亿 · 模式按{frac_src}折算暂判"
    else:
        amt = amt_raw
        basis = "当日实时"

    # 节奏倍数：当前累计 ÷ 「近期同期该有的累计」。这是**事实型**对比，
    # 比"外推全天额"稳得多，也是界面上真正有用的那句话。
    pace = None
    pace_txt = ""
    if intraday:
        if ref_amount > 0 and amt_raw > 0:
            frac_now = max(_traded_fraction(tmin), 0.02)
            if frac_hint and 0.02 <= float(frac_hint) <= 1.0:
                frac_now = max(float(frac_hint), 0.02)
            exp = ref_amount * frac_now
            if exp > 0:
                pace = round(amt_raw / exp, 2)
                if pace >= 1.25:
                    pace_txt = f"比近期同期快 {(pace-1)*100:.0f}%（明显放量）"
                elif pace <= 0.8:
                    pace_txt = f"比近期同期慢 {(1-pace)*100:.0f}%（明显缩量）"
                else:
                    pace_txt = "与近期同期相当"
        else:
            pace_txt = "同期基准积累中（需 3 个以上交易日）"

    peak = v.get("peak_threshold", 15000)
    comfort = v.get("comfort_threshold", 5000)
    shrink = v.get("shrink_threshold", 12000)

    if amt >= peak:
        mode = "peak_swing"
        advice = "资金进得去大市值，做中长线波段/趋势票"
        short_only = False
    elif amt >= comfort:
        mode = "comfort_short"
        advice = f"量能回到舒适区（{int(comfort)}亿以上），游资具备轮动能力，题材可做"
        short_only = False
    elif amt >= shrink:
        mode = "shrink_short"
        advice = ("缩量存量，资金撬不动大票 —— 这时才切短线中小题材的应对打法"
                  "（平常中长线波段，不打板不做短线），并降低单票仓位")
        short_only = True
    else:
        mode = "dry"
        advice = "极度缩量，量能不够说破大天都没用 —— 建议空仓等待量能恢复"
        short_only = True

    # 极致缩量还红盘
    red_flag = ""
    if v.get("shrink_red_flag") and hist:
        recent = hist[-5:]
        if recent and all(b["close"] > b["open"] for b in recent[-3:]):
            if mode in ("shrink_short", "dry"):
                mode = "shrink_red"
                advice = ("极致缩量还红盘 = 控盘好、散户90%套牢躺平 → "
                          "筹码结构好，可低吸；但这是缩量期的权宜打法，不是常态策略")
                red_flag = "缩量红盘"

    return {
        "mode": mode,
        "label": VOLUME_MODES.get(mode, mode),
        "advice": f"{advice}（量能口径：{basis}）",
        "short_only": short_only,
        # 全天成交额：**只有收盘后它才是事实**。盘中一律 None —— 界面不给外推数字。
        "amount": None if intraday else round(amt, 0),
        "cum_amount": round(amt_raw, 0),      # 盘中"已成交"（事实）
        "intraday": intraday,
        "pace": pace,                         # 相对近期同期的节奏倍数（None=基准不够）
        "pace_txt": pace_txt,
        "raw_amount": round(amt_raw, 0),
        "basis": basis,
        "red_flag": red_flag,
    }


# ================================================================ M3 情绪周期

SENTIMENT_STAGES = {
    "probe": "新周期首板 · 试错阶段",
    "climax": "情绪高潮 · 龙头主升",
    "follow": "后排跟风 · 扩散期",
    "ebb": "退潮期 · 无条件清仓",
    "unknown": "情绪数据不足",
}


def evaluate_sentiment(cfg: dict, limit_up: list[dict], limit_down: list[dict],
                       broken: list[dict], zt_hist: list[dict],
                       real_date: str = "", tmin: int | None = None) -> dict:
    """M3 情绪周期定进退时点 —— 决定「能不能出手」。

    退潮期判定严格按「全能的野人」9月28日视频原话口径，四个现象
    任一成立即判退潮，不做任何额外加固：
      ① 亏钱效应扩散：亏钱面积（跌停+炸板占三分池比例）≥ 30%
      ② 中位股成排跌停：跌停/涨停 ≥ 0.15
      ③ A字杀不反包：炸板/涨停 ≥ 0.50
      ④ 高度标的被闷杀：跌停 ≥ 5 只
    判定成立 → 无条件清仓，停止一切低吸和打板，管住手。
    """
    s = cfg.get("M3_sentiment", {})
    n_zt = len(limit_up)
    n_dt = len(limit_down)
    n_zb = len(broken)

    if not s.get("enabled", True):
        return {"stage": "disabled", "label": "情绪模块已关闭", "can_trade": True,
                "note": "不参与情绪判断", "metrics": {}}

    hurt_ratio = s.get("ebb_hurt_ratio", 0.30)
    dt_ratio_th = s.get("ebb_limit_down_ratio", 0.15)
    zb_ratio_th = s.get("ebb_broken_ratio", 0.50)

    total = n_zt + n_dt + n_zb
    hurt_area = (n_dt + n_zb) / total if total else 0.0

    signals = []
    if total and hurt_area >= hurt_ratio:
        signals.append(f"亏钱效应扩散 {hurt_area*100:.0f}% ≥ {hurt_ratio*100:.0f}%")
    if n_zt and n_dt / n_zt >= dt_ratio_th:
        signals.append(f"中位股成排跌停：跌停/涨停比 {n_dt/n_zt:.2f} ≥ {dt_ratio_th}")
    if n_zt and n_zb / n_zt >= zb_ratio_th:
        signals.append(f"A字杀不反包：炸板/涨停比 {n_zb/n_zt:.2f} ≥ {zb_ratio_th}")
    if n_dt >= 5:
        signals.append(f"高度标的被闷杀 {n_dt} 只")

    is_ebb = bool(signals)

    # 原始信号只是"这一瞬间"的读数，盘中会来回跳。交给滞回稳定器过一遍，
    # 确认后的结论才是能照着执行的那个（见 core/stage_stabilizer.py）。
    st = stab.stabilize(real_date, is_ebb, hurt_area, hurt_ratio,
                        tmin=tmin, cfg=cfg)
    ebb_confirmed = st["confirmed"]

    # 最高连板（龙头高度）
    max_boards = max((r.get("boards", 1) for r in limit_up), default=0)
    probe = s.get("probe_board", 1)

    if ebb_confirmed:
        stage = "ebb"
    elif max_boards >= 4:
        stage = "climax" if max_boards >= 5 else "follow"
    elif n_zt > 0 and max_boards <= probe:
        stage = "probe"
    elif n_zt == 0:
        stage = "unknown"
    else:
        stage = "follow"

    can_trade = stage != "ebb" or not s.get("clear_on_ebb", True)

    # 退潮期不是一刀切禁止出手：野哥的规矩是允许 **0.5 成试错仓**，
    # 但只做首板、且亏了就走。这里把"能不能开、能开多大"讲清楚，
    # 由 buy_signals / position_plan 分别落到建议与仓位链上。
    trial_w = 0.0
    if stage == "ebb":
        try:
            trial_w = max(0.0, float(s.get("ebb_trial_weight", 0.05) or 0.0))
        except (TypeError, ValueError):
            trial_w = 0.0
    trial_only = stage == "ebb" and trial_w > 0

    seal = n_zt / (n_zt + n_zb) if (n_zt + n_zb) else 0

    # 文案按"稳定后"的状态给，别让页面自相矛盾
    if stage == "ebb":
        if trial_only:
            note = (f"退潮期：不接力、不重仓；最多 {trading.pos_cn(trial_w)} 试错仓，"
                    f"只做首板，亏了就走")
        else:
            note = "退潮期：无条件清仓，停止一切低吸和打板，管住手"
    elif st["state"] == "watch":
        note = (f"退潮预警未确认（亏钱面积 {hurt_area*100:.0f}%，"
                f"原始信号已触发 {st['raw_streak']} 次）—— 先降仓、别急着清，等确认")
    else:
        note = "可按周期节奏操作"

    return {
        "stage": stage,
        "label": SENTIMENT_STAGES.get(stage, stage),
        "can_trade": can_trade,
        "trial_only": trial_only,             # True = 只允许试错仓，不能正常仓位
        "ebb_trial_weight": round(trial_w, 4),
        "max_new_position": round(trial_w if trial_only else 1.0, 4),
        "max_boards": max_boards,
        "signals": signals,
        "structural_signals": [],
        "area_signals": [],
        "warn": "",
        "trend": "",
        "note": note,
        # 稳定器的完整状态：前端据此显示"预警/确认"和已持续多久
        "stability": st,
        "raw_ebb": is_ebb,
        "metrics": {
            "limit_up": n_zt, "limit_down": n_dt, "broken": n_zb,
            "hurt_area": round(hurt_area, 3),
            "max_boards": max_boards,
            "seal_rate": round(seal, 3) if (n_zt + n_zb) else 0,
        },
        "history": zt_hist[-10:],
    }


# ================================================================ M4 审美三要素

def _imagination_score(snap: dict, bars: list[dict], zt_map: dict) -> tuple[float, list[str]]:
    """③ 想象力：名字好听 + 题材新 + 多重概念叠加 = 妖股有多条命。"""
    score = 0.0
    reasons = []
    name = snap.get("name", "")

    # 概念/题材关键词
    hot_concepts = [
        "算力", "AI", "人工智能", "芯片", "半导体", "光模块", "机器人", "固态电池",
        "低空", "商业航天", "卫星", "军工", "华为", "苹果", "小米", "特斯拉",
        "数据", "国资", "并购", "重组", "新能源", "光伏", "储能", "氢能", "核聚变",
        "量子", "折叠屏", "MR", "AR", "VR", "培育钻石", "合成生物", "创新药",
        "稳定币", "数字货币", "跨境支付", "消费", "免税", "文旅", "IP",
    ]
    hit = [c for c in hot_concepts if c in name]
    if hit:
        score += 0.3
        reasons.append(f"名称含热点概念({'/'.join(hit[:2])})")

    # 涨停池中的题材标签
    zt = zt_map.get(snap["code"])
    if zt and zt.get("sector"):
        score += 0.25
        reasons.append(f"题材:{zt['sector']}")
        if zt.get("boards", 1) >= 2:
            score += 0.2
            reasons.append(f"{zt['boards']}连板验证题材强度")

    # 换手活跃度 = 有资金关注
    to = snap.get("turnover", 0)
    if to >= 3:
        score += 0.15
        reasons.append(f"换手{to:.1f}%资金活跃")
    elif to >= 1.5:
        score += 0.08

    # 多重概念叠加的代理指标：近期多次涨停（多轮炒作 = 多条命）
    if bars:
        lus = ind.count_limit_ups(bars, snap["code"], lookback=60)
        if len(lus) >= 3:
            score += 0.2
            reasons.append(f"60日内{len(lus)}次涨停，多重概念叠加")
        elif len(lus) == 2:
            score += 0.12
            reasons.append("60日内2次涨停")

    return min(score, 1.0), reasons


def _graph_score(bars: list[dict], code: str, cfg_a: dict) -> tuple[float, dict, list[str], list[str]]:
    """① 图形：底部量缩价缩 + 均线多头排列 + 上下影线 + 筹码分布（估算）。"""
    reasons = []
    warns: list[str] = []
    if not bars:
        return 0.0, {}, ["无K线数据"], []

    base = ind.base_pattern(bars, 20)
    wash = ind.washout_signal(bars, 10)
    maa = ind.ma_bull_alignment(bars)
    lus = ind.count_limit_ups(bars, code, lookback=cfg_a.get("recent_limitup_days", 20))
    his_max = ind.max_boards(bars, code)
    dd = ind.drawdown_from_high(bars, 60)
    pct_pos = (bars[-1]["close"] - bars[0]["close"]) / bars[0]["close"] * 100 if bars[0]["close"] else 0

    score = 0.0
    # 位置分：越接近底部越好（洗盘充分）
    if base["deep_base"]:
        score += 0.3
        reasons.append(f"深度底部(区间位置{base['pos']*100:.0f}%)")
    elif base["near_base"]:
        score += 0.2
        reasons.append(f"接近底部(区间位置{base['pos']*100:.0f}%)")
    else:
        score += 0.05

    # 缩量分
    if base["shrink"] < 0.6:
        score += 0.2
        reasons.append(f"量能压缩{base['shrink']*100:.0f}%")
    elif base["shrink"] < 0.85:
        score += 0.12
        reasons.append(f"温和缩量{base['shrink']*100:.0f}%")

    # 均线多头
    if maa.get("aligned"):
        score += 0.2
        reasons.append("均线多头排列")
    elif bars[-1]["close"] > (maa.get("ma20") or 0) and (maa.get("ma20") or 0) > 0:
        score += 0.08
        reasons.append("站上20日线")

    # 洗盘影线
    score += 0.2 * wash.get("score", 0)
    if wash.get("long_lower"):
        reasons.append("长下影撬板")
    if wash.get("both_shadow"):
        reasons.append("上下影线洗盘充分")

    # 近期涨停基因
    recent_days = cfg_a.get("recent_limitup_days", 20)
    recent_min = cfg_a.get("recent_limitup_min", 1)
    if len(lus) >= recent_min:
        score += 0.15
        reasons.append(f"前{recent_days}日有{len(lus)}次涨停，活性仍在")

    # 历史两板基因
    if cfg_a.get("history_two_board") and his_max >= 2:
        score += 0.15
        reasons.append(f"历史最高{his_max}连板，妖股体质")

    # 前期跌幅（洗盘空间）：需要的是"回撤过、企稳"，而非一路新高
    # 创新高说明位置偏高、获利盘压力大，与"洗盘充分"相悖
    if 10 <= dd <= 45:
        score += 0.08
        reasons.append(f"距60日高回撤{dd*100:.0f}%，洗盘空间充分")
    elif dd < 3:
        # 负向结论要进 warnings —— 详情页「为什么选它」列的是理由，
        # 把"位置偏高、获利盘压力大"写进理由列，等于自相矛盾
        warns.append(f"处于60日新高（回撤{dd*100:.0f}%），位置偏高、获利盘压力大")
    elif dd > 60:
        warns.append(f"距高点回撤{dd*100:.0f}%，趋势已破，非洗盘而是走弱")

    # 筹码分布量化（估算）：获利盘 / 成本乖离 / 集中度 —— 只做小权重加分与提示，
    # 不动选股主口径；详情页展示完整分布
    chip = chips_mod.analyze(bars, close=bars[-1].get("close"))
    if chip:
        wr = chip["winner_rate"]
        if wr <= 0.25:
            score += 0.08
            reasons.append(f"获利盘仅 {wr*100:.0f}%，筹码低位沉淀、上方套牢盘轻")
        elif wr <= 0.70:
            score += 0.05
            reasons.append(f"获利盘 {wr*100:.0f}%，筹码结构均衡")
        elif wr >= 0.92:
            warns.append(f"获利盘 {wr*100:.0f}%，人人浮盈、抛压随时释放")
        if chip["concentration"] <= 0.16:
            score += 0.04
            reasons.append(f"筹码高度集中（集中度 {chip['concentration']:.2f}）")
        elif chip["concentration"] >= 0.35:
            warns.append(f"筹码分散（集中度 {chip['concentration']:.2f}），上方阻力杂")

    detail = {
        "base_pos": round(base["pos"], 3),
        "shrink": round(base["shrink"], 3),
        "ma_aligned": maa.get("aligned", False),
        "ma5": round(maa.get("ma5") or 0, 2),
        "ma10": round(maa.get("ma10") or 0, 2),
        "ma20": round(maa.get("ma20") or 0, 2),
        "ma60": round(maa.get("ma60") or 0, 2),
        "washout": round(wash.get("score", 0), 2),
        "recent_limitups": len(lus),
        "history_max_boards": his_max,
        "drawdown60": round(dd, 3),
        "period_pct": round(pct_pos, 2),
    }
    if chip:
        detail["chips"] = chip
    return min(score, 1.0), detail, reasons, warns


def _price_mv_score(snap: dict, cfg_a: dict) -> tuple[float, list[str], list[str]]:
    """② 价格市值：股价5-10元、市值100亿以下；500亿=中军不做短线。"""
    reasons, warns = [], []
    price = snap["price"]
    mv = snap.get("float_mv", 0) / 1e8  # 亿
    pmin, pmax = cfg_a.get("price_min", 5.0), cfg_a.get("price_max", 10.0)
    mv_max = cfg_a.get("mv_max", 100.0)
    mv_mid = cfg_a.get("mv_mid_threshold", 500.0)

    score = 0.0
    if pmin <= price <= pmax:
        score += 0.5
        reasons.append(f"股价{price:.2f}元在{ pmin:.0f}-{pmax:.0f}元舒适区（散户跟得起）")
    elif pmin * 0.7 <= price <= pmax * 1.4:
        score += 0.28
        reasons.append(f"股价{price:.2f}元偏离舒适区但仍可接受")
    else:
        score += 0.05
        warns.append(f"股价{price:.2f}元超出{ pmin:.0f}-{pmax:.0f}元区间，散户跟不起")

    if mv <= mv_max:
        score += 0.5
        reasons.append(f"流通市值{mv:.1f}亿<{mv_max:.0f}亿，资金撬得动")
    elif mv <= mv_mid:
        score += 0.25
        warns.append(f"流通市值{mv:.1f}亿偏大，弹性下降")
    else:
        score += 0.05
        warns.append(f"流通市值{mv:.1f}亿>={mv_mid:.0f}亿属中军/趋势票，不在短线审美范围")

    return min(score, 1.0), reasons, warns


def _vr_tags(snap: dict) -> tuple[list[str], list[str]]:
    """量比标签：≥2.0 放量 / <0.8 缩量。不参与打分，只做提示。"""
    vr = snap.get("volume_ratio")
    if vr is None or vr <= 0:
        return [], []
    if vr >= 2.0:
        return [f"量比 {vr:.2f}，显著放量"], []
    if vr < 0.8:
        return [], [f"量比 {vr:.2f}，量能偏冷（缩量）"]
    return [], []


# ================================================================ 打法适配（个股）
# 四套预设已融合成一套自适应体系：选股口径相同，区别在「这只票怎么打」。
# 判定结果只写在**股票详情页**，不进候选表。

def style_fit(cfg: dict, stock: dict, bars: list[dict],
              fund_style: dict | None = None) -> dict:
    """判定这只股票适合哪种打法（个股口径，只进详情页）。

      · 短线题材 —— 小市值/连板基因/游资主导：小仓位、快进快出，
        只做首板与回踩，不恋战（体系里这是缩量期的应对打法）；
      · 中长线波段 —— 中军体量/机构主导/趋势在：沿均线持有，
        小阳线爬坡不动手，突然加速=派发（体系里这是平常打法）；
      · 回避 —— 庄股特征，盈亏比极差。

    ★ label 只写**打法名**，不写「平常/缩量期应对」这类市场状态断言 ——
    今天是什么市场状态由 outlook.mode（今日应对打法）统一说，详情页再断言
    一次「缩量期」就会和当天的量能结论打架（用户 10-12 实测同类问题）。
    体系里的角色定位放在 note 里说明。

    stock["float_mv"] 单位是亿；bars 供均线/涨停基因判定。
    """
    fs = fund_style or {}
    ftype = fs.get("type", "unknown")
    code = stock.get("code", "")
    mv = float(stock.get("float_mv") or 0)
    a = cfg.get("M4_aesthetic", {})
    mv_mid = float(a.get("mv_mid_threshold", 500.0) or 500.0)

    if ftype == "zhuang":
        return {"key": "avoid", "label": "庄股 · 不参与",
                "note": "做庄失败打回原点，盈亏比极差；断崖式下跌绝不接，已持有的无条件出。"}

    bars = bars or []
    maa = ind.ma_bull_alignment(bars) if bars else {}
    aligned = bool(maa.get("aligned"))
    lus20 = len(ind.count_limit_ups(bars, code, lookback=20)) if bars else 0

    swing = (ftype == "institutional"
             or mv >= max(150.0, mv_mid * 0.4)
             or (aligned and lus20 <= 1 and mv >= 100.0))
    if swing:
        return {"key": "swing", "label": "中长线波段",
                "note": ("平常打法：大资金/趋势票 —— 沿5日线持有、小阳线爬坡不动手；"
                         "突然加速拉涨停 = 派发信号，减仓而不是加仓。")}

    if lus20 >= 2:
        return {"key": "short", "label": "短线题材",
                "note": ("缩量期的应对打法：有连板基因的题材票 —— 小仓位、快进快出，"
                         "跟着断板节奏走；节奏破坏就撤，不把短线拿成中线。")}
    return {"key": "short", "label": "短线题材",
            "note": ("缩量期的应对打法：小市值题材票 —— 小仓位、快进快出，"
                     "只做首板与回踩企稳；这是权宜打法，节奏不对就空仓。")}


def score_one(cfg: dict, code: str, snap: dict, bars: list[dict],
              zt: list[dict], preopen: bool = False) -> dict:
    """对**单只**股票按候选股同一套口径打分，并列出「它为什么没进今天的候选」。

    用于自选 / 持仓里点开一只不在当日候选名单里的股票 —— 让「点不开」变成
    「能打开，而且知道差在哪」。只负责打分与过滤判定，买卖点由 pipeline 附加。
    """
    a = cfg.get("M4_aesthetic", {})
    zt_map = {r["code"]: r for r in zt}
    rec = zt_map.get(code, {})
    bars = bars or []

    g, gdetail, greasons, gwarns = _graph_score(bars, code, a) if bars else (0.0, {}, [], [])
    p_sc, preasons, pwarns = _price_mv_score(snap, a)
    i_sc, ireasons = _imagination_score(snap, bars, zt_map)

    wg = a.get("weight_graph", 35)
    wp = a.get("weight_price_mv", 30)
    wi = a.get("weight_imagination", 35)
    wsum = max(wg + wp + wi, 1)
    total = (g * wg + p_sc * wp + i_sc * wi) / wsum

    reasons = list(greasons) + list(preasons) + list(ireasons)
    warns = list(gwarns) + list(pwarns)
    vr_r, vr_w = _vr_tags(snap)
    reasons += vr_r
    warns += vr_w

    # 逐条复现 screen() 里的 continue 条件 —— 这就是「为什么没进候选」的答案
    filters: list[str] = []
    name = snap.get("name", "") or ""
    to = snap.get("turnover", 0) or 0
    if not a.get("enabled", True):
        filters.append("M4 审美三要素已关闭，今天不选股")
    if a.get("exclude_st", True) and ("ST" in name.upper() or "退" in name):
        filters.append("名字含 ST / 退，被排除")
    bd = universe.board_of(code)
    if bd in set(a.get("exclude_boards", [])):
        filters.append(f"{bd}板块被排除")
    if len(bars) < 25:
        filters.append(f"日K线不足 25 根（只有 {len(bars)} 根），判不了形态")
    else:
        if not preopen:
            lo, hi = a.get("min_turnover", 1.0), a.get("max_turnover", 25.0)
            if to < lo:
                filters.append(f"换手 {to:.2f}% 低于下限 {lo}%")
            elif to > hi:
                filters.append(f"换手 {to:.2f}% 高于上限 {hi}%")
            if snap.get("amount", 0) <= 0:
                filters.append("当日无成交额（停牌或一字未开板）")
        if a.get("need_bull_ma") and not gdetail.get("ma_aligned"):
            filters.append("未形成均线多头排列（策略要求必须多头）")

    result = {
        "code": code,
        "name": name,
        "board": bd,
        "price": snap.get("price", 0),
        "pct": snap.get("pct", 0),
        "turnover": to,
        "float_mv": snap.get("float_mv", 0) / 1e8,
        "amount": snap.get("amount", 0),
        "volume_ratio": snap.get("volume_ratio"),
        "avg_price": snap.get("avg_price"),
        "pe": snap.get("pe"),
        "amplitude": snap.get("amplitude"),
        "score": round(total * 100, 1),
        "scores": {
            "graph": round(g * 100, 1),
            "price_mv": round(p_sc * 100, 1),
            "imagination": round(i_sc * 100, 1),
        },
        "graph_detail": gdetail,
        "reasons": reasons,
        "warnings": warns,
        "in_zt": code in zt_map,
        "zt_boards": rec.get("boards", 0),
        "sector": rec.get("sector", ""),
        "filters": filters,          # 非空 = 今天进不了候选，原因在此
        "in_pick": False,
    }
    # M10 主导资金分类 + 打法适配：详情页要用（管道原实现会再算一次，这里统一）
    if cfg.get("M10_fundstyle", {}).get("enabled", True) and bars:
        result["fund_style"] = fundstyle.classify(bars, code, result.get("float_mv", 0.0), cfg)
    result["style_fit"] = style_fit(cfg, result, bars, result.get("fund_style"))
    return result


def screen(cfg: dict, snaps: dict[str, dict], kl: dict[str, list[dict]],
           zt: list[dict], macro: dict, volume: dict, sentiment: dict,
           top_n: int = 20, progress=None, preopen: bool = False) -> list[dict]:
    """M4+M5 选股主流程：审美三要素打分 + 仓位建议。

    preopen：盘前/竞价/休市时段全市场换手、成交额均为 0（当日数据
    未形成），放开流动性门槛，名单按昨收状态作观察名单。
    """
    a = cfg.get("M4_aesthetic", {})
    if not a.get("enabled", True):
        return []

    zt_map = {r["code"]: r for r in zt}
    ex_boards = set(a.get("exclude_boards", []))
    min_to = a.get("min_turnover", 1.0)
    max_to = a.get("max_turnover", 25.0)
    exclude_st = a.get("exclude_st", True)
    need_bull = a.get("need_bull_ma", False)

    wg = a.get("weight_graph", 35)
    wp = a.get("weight_price_mv", 30)
    wi = a.get("weight_imagination", 35)
    wsum = max(wg + wp + wi, 1)

    # 一致性过滤：退潮期 / 极度缩量
    # 退潮期允许 0.5 成试错仓（只做首板），所以不是简单地把 tradable 全关掉。
    allow = True
    block_note = ""
    trial_only = bool(sentiment.get("trial_only"))
    if not sentiment.get("can_trade", True):
        if trial_only:
            tw = float(sentiment.get("ebb_trial_weight") or 0.05)
            block_note = (f"退潮期试错仓：只做首板，单笔 ≤{trading.pos_cn(tw)}；"
                          f"连板股一律不接力")
        else:
            allow = False
            block_note = "情绪退潮期：退潮期策略是无条件清仓、停止一切低吸和打板"
    elif volume.get("mode") == "dry":
        allow = False
        block_note = "极度缩量：量能不够说破大天都没用，建议空仓等待"

    results = []
    for idx, (code, snap) in enumerate(snaps.items()):
        if progress and idx % 800 == 0:
            progress(idx, len(snaps))

        if exclude_st and ("ST" in snap["name"].upper() or "退" in snap["name"]):
            continue
        if universe.board_of(code) in ex_boards:
            continue
        to = snap.get("turnover", 0)
        if not preopen:
            if to < min_to or to > max_to:
                continue
            # 无成交（停牌/一字无换手）跳过；盘前换手未形成，跳过该门槛
            if snap.get("amount", 0) <= 0:
                continue

        bars = kl.get(code) or []
        if len(bars) < 25:
            continue

        g, gdetail, greasons, gwarns = _graph_score(bars, code, a)
        p, preasons, pwarns = _price_mv_score(snap, a)
        i, ireasons = _imagination_score(snap, bars, zt_map)

        if need_bull and not gdetail.get("ma_aligned"):
            continue

        total = (g * wg + p * wp + i * wi) / wsum
        reasons = greasons + preasons + ireasons
        warns = list(gwarns) + list(pwarns)

        # 量比：当日放量程度 —— 短线最直接的确认信号。不参与打分（避免动选股口径），
        # 只作为理由/警示呈现，详情页也会单独显示。
        vr_r, vr_w = _vr_tags(snap)
        reasons = reasons + vr_r
        warns = warns + vr_w

        results.append({
            "code": code,
            "name": snap["name"],
            "board": universe.board_of(code),
            "price": snap["price"],
            "pct": snap.get("pct", 0),
            "turnover": to,
            "float_mv": snap.get("float_mv", 0) / 1e8,
            "amount": snap.get("amount", 0),
            "volume_ratio": snap.get("volume_ratio"),   # 量比
            "avg_price": snap.get("avg_price"),         # 日内均价
            "score": round(total * 100, 1),
            "scores": {
                "graph": round(g * 100, 1),
                "price_mv": round(p * 100, 1),
                "imagination": round(i * 100, 1),
            },
            "graph_detail": gdetail,
            "reasons": reasons,
            "warnings": warns,
            "in_zt": code in zt_map,
            "zt_boards": zt_map.get(code, {}).get("boards", 0),
            "sector": zt_map.get(code, {}).get("sector", ""),
            # 交易许可：退潮/极度缩量时，只有首板在试错仓范围内算"可做"，
            # 其余仅作观察名单，不构成开仓建议
            "tradable": allow and (not trial_only
                                   or zt_map.get(code, {}).get("boards", 0) <= 1),
            "trial_only": trial_only,
            "block_note": block_note,
        })

    results.sort(key=lambda x: -x["score"])
    top = results[:top_n]
    # M10 主导资金分类 + 打法适配：只对最终候选做，省算力
    for p in top:
        if cfg.get("M10_fundstyle", {}).get("enabled", True):
            p["fund_style"] = fundstyle.classify(
                kl.get(p["code"], []), p["code"], p.get("float_mv", 0.0), cfg)
        p["style_fit"] = style_fit(cfg, p, kl.get(p["code"], []), p.get("fund_style"))
    return top


# ================================================================ M6 消息与信息观

def evaluate_message(cfg: dict, zt: list[dict]) -> dict:
    """M6 消息观：消息服务于资金，明牌利好不追。"""
    m = cfg.get("M6_message", {})
    if not m.get("enabled", True):
        return {"enabled": False, "principles": [], "note": "消息模块已关闭"}
    return {
        "enabled": True,
        "principles": [
            {"t": "消息服务于资金，不包括散户",
             "d": "散户与大资金之间存在信息差，跟消息不如跟资金"},
            {"t": "明牌利好出来当天：要么买不到，要么买进去都是坑",
             "d": "主力已提前进场，明牌=出货"},
            {"t": "房地产案例：利好明牌+无人接盘 → 主力自己从地板拉起来",
             "d": "市场经常反人性，利好出尽不等于下跌"},
            {"t": "多空跷跷板：看好大科技↔看好大金融、大市值↔小市值",
             "d": "没有纯多头纯空头，随盘面切换"},
        ],
        "advice": ("明牌不追：当日大幅高开/放量涨停的利好标的，"
                   "要么买不到要么是坑，等回调再看资金是否真的进场"),
        "zt_count": len(zt),
    }


# ================================================================ M7 业绩披露期

EARNINGS_RULES = [
    {"situation": "业绩好于二季度", "result": "利好落地即利空，高开低走",
     "tag": "bear"},
    {"situation": "业绩差于二季度", "result": "利空落地还是利空，低开低走",
     "tag": "bear"},
    {"situation": "低位无业绩题材（AI应用/算力）", "result": "黄鼠狼给鸡拜年，必是套",
     "tag": "trap"},
    {"situation": "只有业绩的方向", "result": "大科技、大金融", "tag": "safe"},
]


def evaluate_earnings(cfg: dict) -> dict:
    """M7 业绩披露期专规则。当前处于 Q3 预披露期（10月8日开启）。"""
    m = cfg.get("M7_earnings", {})
    if not m.get("enabled", True):
        return {"enabled": False, "in_window": False, "rules": EARNINGS_RULES,
                "note": "业绩模块已关闭"}
    now = datetime.now()
    mm, dd = now.month, now.day
    # Q3 预披露期：10月8日 - 10月31日
    in_window = (mm == 10 and dd >= 8) or mm == 11
    focus = m.get("post_holiday_focus", ["大金融", "大科技"])
    return {
        "enabled": True,
        "in_window": in_window,
        "window": "Q3 预披露期 10月8日开启",
        "rules": EARNINGS_RULES,
        "focus": focus,
        "advice": ("业绩披露期不碰无业绩题材（AI应用/算力低位='黄鼠狼给鸡拜年'）；"
                   "仓位只给有业绩的方向（大金融）或自己的短线舒适区；"
                   "披露完毕立马变多头"),
        "hold_cash": m.get("hold_cash_before_holiday", True),
        "note": f"当前{'处于' if in_window else '不在'}业绩披露专规则窗口",
    }


# ================================================================ M8 纪律检查

# 「大金融/大科技」这类口语大类 → 系统行业名的模糊映射。
# 配置里 focus 写的是口语（"大科技"），而涨停池/活跃榜用的是行业口径
# （"软件开发""半导体"），不映射就永远匹配不上（2026-10-10 用户实测抓到）。
_FOCUS_GROUPS = {
    "大科技": ("半导体", "软件", "计算机", "通信", "电子", "传媒", "互联网",
               "IT服务", "光学", "消费电子", "元件", "芯片", "算力", "游戏",
               "数字媒体", "元器件"),
    "大金融": ("银行", "证券", "保险", "多元金融", "金融", "信托"),
}


def _focus_alive(focus: list, themes: list) -> list:
    """focus 里哪些大类方向**今天确实有涨停合力**（活跃榜前 8 内模糊命中）。"""
    names = [str(t.get("sector") or "") for t in (themes or [])[:8]]
    alive = []
    for f in focus or []:
        kws = _FOCUS_GROUPS.get(str(f)) or (str(f),)
        if any(any(k in n for k in kws) for n in names if n):
            alive.append(str(f))
    return alive


def evaluate_discipline(cfg: dict, sentiment: dict, volume: dict,
                        macro: dict, earnings: dict,
                        mainline: dict | None = None) -> dict:
    """M8 保命纪律清单 + 实时合规检查。mainline: M9 主线五信号结果（可选联动）。"""
    d = cfg.get("M8_discipline", {})
    rules = d.get("rules", [])
    active = [r for r in rules if r.get("on")]

    # 实时体检项
    checks = []
    if sentiment.get("stage") == "ebb":
        checks.append({"level": "red", "item": "情绪处于退潮期",
                       "action": "无条件清仓，停止一切低吸和打板，管住手"})
    if volume.get("mode") == "dry":
        checks.append({"level": "red", "item": "市场极度缩量",
                       "action": "量能不够说破大天都没用，建议空仓"})
    elif volume.get("short_only"):
        checks.append({"level": "yellow", "item": "缩量存量市场",
                       "action": "只做短线中小题材，不做中长线大票"})
    if macro.get("risk") == "high":
        checks.append({"level": "yellow", "item": "宏观处于加息/风险区",
                       "action": "不加杠杆，降低仓位"})
    if earnings.get("in_window"):
        _focus = list(earnings.get("focus") or ["大金融", "大科技"])
        # 静态方向名不能当"今日机会"喊 —— 必须和当日活跃榜交叉校验：
        # 有合力才说"当前有表现的是 X"，没有就明说"先观察、别硬上"。
        # （用户实测：科技当天涨停 0 家，界面却还在喊"做大科技"）
        _alive = _focus_alive(_focus, (mainline or {}).get("themes") or [])
        if _alive:
            _act = (f"业绩期只做有业绩支撑的方向 —— 当前有合力的是：{'、'.join(_alive)}"
                    f"（其余方向等它自己出现合力）")
        else:
            _act = (f"业绩期只做有业绩支撑的方向（关注：{'、'.join(_focus)}）—— "
                    f"今日这些方向暂无涨停合力，先观察、别硬上")
        checks.append({"level": "yellow", "item": "业绩披露期", "action": _act})
    if mainline and mainline.get("enabled", True):
        ml_status = mainline.get("status")
        if ml_status == "one_day":
            # item 也要跟着连续活跃天数走 —— 上一次只改了主线卡的 label，
            # 这里的「一日游特征」漏了，又变成「连续4日活跃 + 一日游」自相矛盾
            try:
                _sk = int(mainline.get("streak") or 0)
            except (TypeError, ValueError):
                _sk = 0
            _item = (f"题材主线容量不足（已连续 {_sk} 日活跃，非一日游）"
                     if _sk >= 2 else "题材主线一日游特征")
            _tail = "题材内不接力，只看不动" if _sk < 2 else "按短线节奏处理，去弱留强"
            checks.append({"level": "yellow", "item": _item,
                           "action": (mainline.get("verdict_note") or "").rstrip("。，； ")
                                     + "。" + _tail})
        elif ml_status == "day1_pending":
            checks.append({"level": "yellow", "item": "题材主线待次日验证",
                           "action": "首日启动成立但主线未确认 —— 明日开盘检验空间龙溢价、"
                                     "首板留存、中军反馈后再定性"})
        elif ml_status == "confirmed":
            checks.append({"level": "green", "item": "题材主线已确认",
                           "action": f"主线题材「{mainline.get('theme', '')}」五信号共振，"
                                     f"可按主线节奏参与"})
    if not checks:
        checks.append({"level": "green", "item": "当前无重大纪律冲突",
                       "action": "按计划执行即可"})

    worst = "green"
    for c in checks:
        if c["level"] == "red":
            worst = "red"
            break
        if c["level"] == "yellow":
            worst = "yellow"

    return {
        "enabled": d.get("enabled", True),
        "rules": active,
        "checks": checks,
        "status": worst,
        "creed": "保住本金是第一原则 · 交易唯有自渡",
    }
