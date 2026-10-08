# -*- coding: utf-8 -*-
"""策略引擎：把九大模块的判断逻辑编码为可执行判定。

核心链路（与用户体系一致）：
  宏观周期定牛熊 → 量能定模式 → 情绪周期定时点
  → 审美三要素定标的 → 平铺去弱留强定仓位 → 纪律保命
  → M9 题材主线五信号定性 → M10 主导资金分类定离场
"""
from __future__ import annotations

from datetime import datetime

from . import fundstyle
from . import indicators as ind
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
    "peak_swing": "峰值量能 · 中长线波段大票",
    "comfort_short": "舒适区(≥5000亿) · 打板选手天下",
    "shrink_short": "缩量存量 · 只做短线中小题材",
    "shrink_red": "极致缩量还红盘 · 筹码结构好",
    "dry": "极度缩量 · 空仓等待",
    "pending": "量能未就绪 · 盘前/非交易时段",
}

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
                    fallback_amount: float = 0.0) -> dict:
    """M2 量能定模式 —— 整个体系的模式总开关。

    session/fallback_amount：量能阈值是「全天成交额」口径，而快照里的
    成交额是当日累计值 —— 集合竞价时约等于 0，早盘也只有两三成。
    盘前/非交易时段改用上一完整交易日的全天额（本地持久化），
    盘中按日内占比折算成预估全天额，收盘后直读。口径写进 basis，
    界面上跟着显示，杜绝"没开盘就说极度缩量"这类假结论。
    """
    v = cfg.get("M2_volume", {})
    if not v.get("enabled", True):
        return {"mode": "disabled", "label": "量能模块已关闭", "advice": "自行判断",
                "short_only": False, "amount": market_amount}

    amt_raw = market_amount / 1e8 if market_amount > 1e5 else market_amount
    phase = (session or {}).get("phase", "")
    tmin = (session or {}).get("tmin", 240)

    if phase in ("pre_open", "auction", "rest"):
        if fallback_amount and fallback_amount > 0:
            amt = fallback_amount
            basis = "上一完整交易日全天"
        else:
            return {
                "mode": "pending", "label": VOLUME_MODES["pending"],
                "advice": ("当前是盘前/非交易时段，当日量能还没形成 —— "
                           "别急着定模式，开盘半小时后再看量能"),
                "short_only": False,
                "amount": 0, "raw_amount": round(amt_raw, 0),
                "basis": "无（当日数据未形成）", "red_flag": "",
            }
    elif phase in ("just_closed", "after_close"):
        amt = amt_raw
        basis = "当日全天（已收盘）"
    elif phase in ("morning", "noon", "afternoon", "tail"):
        if amt_raw <= 0:
            # 盘中但累计额为 0：数据未就绪（接口异常/刚开盘第一分钟）
            return {
                "mode": "pending", "label": VOLUME_MODES["pending"],
                "advice": ("盘中但成交额数据未形成 —— 等数据就绪后再定模式，"
                           "先不动手"),
                "short_only": False,
                "amount": 0, "raw_amount": 0,
                "basis": "无（盘中数据未就绪）", "red_flag": "",
            }
        frac = max(_traded_fraction(tmin), 0.02)
        est = amt_raw / frac if amt_raw > 0 else 0.0
        amt = min(max(est, amt_raw), amt_raw * 6) if amt_raw > 0 else 0.0
        basis = f"盘中预估全天（已成交{amt_raw:.0f}亿）"
    else:
        amt = amt_raw
        basis = "当日实时"

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
        advice = "缩量存量，资金撬不动大票 —— 只做短线中小题材，降低单票仓位"
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
                          "筹码结构好，可低吸但仍不做中长线")
                red_flag = "缩量红盘"

    return {
        "mode": mode,
        "label": VOLUME_MODES.get(mode, mode),
        "advice": f"{advice}（量能口径：{basis}）",
        "short_only": short_only,
        "amount": round(amt, 0),
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
                       broken: list[dict], zt_hist: list[dict]) -> dict:
    """M3 情绪周期定进退时点 —— 决定「能不能出手」。"""
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

    # 退潮四要素（用户的量化定义）
    structural = []   # 结构信号：闷杀/A字杀/中位股跌停
    area_signal = []  # 面积信号：亏钱效应扩散

    if hurt_area >= hurt_ratio:
        area_signal.append(f"亏钱效应扩散 {hurt_area*100:.0f}% ≥ {hurt_ratio*100:.0f}%")
    if n_zt and n_dt / n_zt >= dt_ratio_th:
        structural.append(f"跌停/涨停比 {n_dt/n_zt:.2f} ≥ {dt_ratio_th}（中位股成排跌停）")
    if n_zt and n_zb / n_zt >= zb_ratio_th:
        structural.append(f"炸板/涨停比 {n_zb/n_zt:.2f} ≥ {zb_ratio_th}（A字杀不反包）")
    if n_dt >= 5:
        structural.append(f"高度标的被闷杀 {n_dt} 只")
    # 封板率崩塌 = 高位票接不住
    seal = n_zt / (n_zt + n_zb) if (n_zt + n_zb) else 0
    if n_zt + n_zb >= 10 and seal < 0.55:
        structural.append(f"封板率仅 {seal*100:.0f}%，资金接力意愿下滑")

    # 判定：必须同时满足「亏钱面积达标」+「至少一条结构信号」才认定退潮，
    # 避免单日节前效应被误判为周期退潮。
    is_ebb = bool(area_signal) and len(structural) >= 1
    signals = area_signal + structural

    # 最高连板（龙头高度）
    max_boards = max((r.get("boards", 1) for r in limit_up), default=0)
    probe = s.get("probe_board", 1)

    if is_ebb:
        stage = "ebb"
    elif max_boards >= 4:
        stage = "climax" if max_boards >= 5 else "follow"
    elif n_zt > 0 and max_boards <= probe:
        stage = "probe"
    elif n_zt == 0:
        stage = "ebb" if n_dt > 5 else "unknown"
    else:
        stage = "follow"

    # 阶段判定的补充逻辑：连续涨停家数下滑
    trend = ""
    if len(zt_hist) >= 3:
        recent3 = [x["limit_up"] for x in zt_hist[-3:]]
        if len(recent3) == 3:
            if recent3[0] > recent3[1] > recent3[2] and recent3[2] < recent3[0] * 0.6:
                trend = "涨停家数连续萎缩，情绪走弱"
                if stage in ("climax", "follow") and n_zt < 40:
                    stage = "ebb"
                    signals.append(trend)

    can_trade = stage != "ebb" or not s.get("clear_on_ebb", True)

    # 接近退潮的预警（面积未达标但结构已坏）
    warn = ""
    if stage != "ebb" and len(structural) >= 2:
        warn = ("结构信号已出现但亏钱面积未扩散到阈值 —— 处于退潮酝酿期，"
                "降低仓位、不加新周期以外的开仓")
    if stage == "ebb" and not area_signal:
        warn = "结构信号恶化但面积未达标，仍按退潮处理（宁可错过，不可做错）"

    return {
        "stage": stage,
        "label": SENTIMENT_STAGES.get(stage, stage),
        "can_trade": can_trade,
        "max_boards": max_boards,
        "signals": signals,
        "structural_signals": structural,
        "area_signals": area_signal,
        "warn": warn,
        "trend": trend,
        "note": ("退潮期：无条件清仓，停止一切低吸和打板，管住手" if stage == "ebb"
                 else "可按周期节奏操作"),
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


def _graph_score(bars: list[dict], code: str, cfg_a: dict) -> tuple[float, dict, list[str]]:
    """① 图形：底部量缩价缩 + 均线多头排列 + 上下影线。"""
    reasons = []
    if not bars:
        return 0.0, {}, ["无K线数据"]

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
        reasons.append(f"处于60日新高（回撤{dd*100:.0f}%），位置偏高、获利盘压力大")
    elif dd > 60:
        reasons.append(f"距高点回撤{dd*100:.0f}%，趋势已破，非洗盘而是走弱")

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
    return min(score, 1.0), detail, reasons


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
        reasons.append(f"流通市值{mv:.1f}亿偏大，弹性下降")
    else:
        score += 0.05
        warns.append(f"流通市值{mv:.1f}亿>={mv_mid:.0f}亿属中军/趋势票，不在短线审美范围")

    return min(score, 1.0), reasons, warns


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

    # 一致性过滤：情绪退潮 + 量能极度缩 → 缩量红盘仍可低吸，其余不动手
    allow = True
    block_note = ""
    if not sentiment.get("can_trade", True):
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

        g, gdetail, greasons = _graph_score(bars, code, a)
        p, preasons, pwarns = _price_mv_score(snap, a)
        i, ireasons = _imagination_score(snap, bars, zt_map)

        if need_bull and not gdetail.get("ma_aligned"):
            continue

        total = (g * wg + p * wp + i * wi) / wsum
        reasons = greasons + preasons + ireasons
        warns = pwarns

        results.append({
            "code": code,
            "name": snap["name"],
            "board": universe.board_of(code),
            "price": snap["price"],
            "pct": snap.get("pct", 0),
            "turnover": to,
            "float_mv": snap.get("float_mv", 0) / 1e8,
            "amount": snap.get("amount", 0),
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
            # 交易许可：退潮/极度缩量时，选股结果仅作观察名单，不构成开仓建议
            "tradable": allow,
            "block_note": block_note,
        })

    results.sort(key=lambda x: -x["score"])
    top = results[:top_n]
    # M10 主导资金分类：只对最终候选做，省算力
    if cfg.get("M10_fundstyle", {}).get("enabled", True):
        for p in top:
            p["fund_style"] = fundstyle.classify(
                kl.get(p["code"], []), p["code"], p.get("float_mv", 0.0), cfg)
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
                   "节前波段持币过节，节后只做大金融（有业绩）或缩在自己短线舒适区；"
                   "披露完毕后立马变多头"),
        "hold_cash": m.get("hold_cash_before_holiday", True),
        "note": f"当前{'处于' if in_window else '不在'}业绩披露专规则窗口",
    }


# ================================================================ M8 纪律检查

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
        checks.append({"level": "yellow", "item": "业绩披露期",
                       "action": "不碰无业绩题材，只做有大金融/大科技"})
    if mainline and mainline.get("enabled", True):
        ml_status = mainline.get("status")
        if ml_status == "one_day":
            checks.append({"level": "yellow", "item": "题材主线一日游特征",
                           "action": (mainline.get("verdict_note") or "")
                                     + " 题材内不接力，只看不动"})
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
