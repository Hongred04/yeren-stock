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
from . import datasource as ds
from . import fundstyle
from . import gene as gene_mod
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


# ★ 相对量能（分位 + 环比）—— 修「只看绝对阈值」的根子问题（v1.26.0）
#   绝对阈值（15000/12000/8000 亿）只能回答「钱够不够撬动大票」，
#   回答不了「这点钱相对最近是多是少」。只比绝对值会把**放量后的回落**
#   判成"峰值量能"：2026-10-12 两市 15378 亿（近 3 日最低、较 10-09 的
#   19006 亿回落 19%）被判峰值 —— 而同一天沪指量能处于近 29 日的 **27.6%
#   分位**（底部三分之一），标签与自己的数据直接打架（用户实测）。
#   「峰值」是相对概念：必须同时满足「绝对值够」+「相对位置高」+「不缩量」。
def _relative_volume(hist: list[dict], frac: float = 1.0,
                     lookback: int = 20) -> tuple[float | None, float | None, str]:
    """用沪指日K量能序列算当前量能的**相对位置**与**相对近期中位数的倍数**。

    为什么用沪指日K而不是本地成交额档案：本地档案才积累 3 天（样本太薄），
    而日K接口一次就能拿到 20+ 根；且这里只做**纯相对**比较（分位/比值），
    沪市量能与两市成交额同向，不需要"手 → 亿元"的换算，天然规避量纲问题。

      rel   = 当前（折算到全天口径）量能在近 lookback 日中的分位 0~1
      trend = 当前（折算到全天口径）量能 ÷ 近 lookback 日中位数

    frac：盘中已交易占比。盘中最后一根日K是**半天**的量，必须先折算到全天
          口径才能和历史整根日K比 —— 否则早盘必然被判成"缩量"（与成交额
          折算同一个道理）。
    样本不足（< 6 根）返回 (None, None, 原因)，调用方退回纯绝对阈值判定。
    """
    bars = list(hist or [])
    if len(bars) < 6:
        return None, None, "样本不足"
    # ★ 当日那根**不能**因为"量为 0"被过滤掉（v1.32.3 修）：原实现先 filter 再取
    #   `bars[-1]`，一旦今天这根是占位/半根（volume 为 0 —— 盘前、或 K 线源只回了
    #   未走完的当日 bar），`bars[-1]` 会悄悄变成**昨天**的收线，等于拿昨天的全天量
    #   ÷ 当日占比 当"今天"：相对分位顶到 100%、环比翻倍，峰值档永远降不下来。
    #   今天没有量 → 干脆不判（返回 None，退回纯绝对阈值），而不是拿昨天顶替。
    if not bars[-1].get("volume"):
        return None, None, "当日量能未形成"
    lb = max(2, min(int(lookback or 20), len(bars) - 1))
    prev = [float(b["volume"]) for b in bars[-lb - 1:-1] if b.get("volume")]
    f = max(min(float(frac or 1.0), 1.0), 0.02)
    cur = float(bars[-1]["volume"]) / f
    if len(prev) < 3 or cur <= 0:
        return None, None, "样本不足"
    rel = sum(1 for x in prev if x <= cur) / len(prev)
    s = sorted(prev)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return rel, (cur / med if med > 0 else None), f"近{lb}日"


def evaluate_volume(cfg: dict, market_amount: float, hist: list[dict],
                    session: dict | None = None,
                    fallback_amount: float = 0.0,
                    fallback_final: bool = True,
                    frac_hint: float | None = None,
                    ref_amount: float = 0.0,
                    ref_cum: float = 0.0,
                    ref_cum_days: int = 0) -> dict:
    """M2 量能定模式 —— 整个体系的模式总开关。

    session/fallback_amount：量能阈值是「全天成交额」口径，而快照里的
    成交额是当日累计值 —— 集合竞价时约等于零，早盘也只有两三成。
    盘前/非交易时段改用上一交易日的全天额（本地持久化），收盘后直读。
    口径写进 basis，界面上跟着显示，杜绝"没开盘就说极度缩量"这类假结论。

    fallback_final：上一交易日那条记录是不是**收盘后**的全天终值。盘中只扫到
    一半的值明显偏低，界面必须说明，不能冒充"全天"。

    frac_hint：由调用方从历史实测曲线算出的「截至当前时刻的累计占比」。

    ref_cum：**实测的「近期同一时刻累计成交额」中位数**（亿），见
        datasource.intraday_baseline。节奏对比必须用它 —— 它是钱对钱的事实，
        不需要"全天额 × 某个占比"这种二次换算。ref_cum_days 是它的样本天数，
        用于在样本不足时收敛措辞强度（措辞规则见下方节奏分支）。

    ★ 盘中**不外推全天额**（v1.24.1）：拿一条平均曲线把几十分钟的进度放大到
    全天，误差大到没有参考价值 —— 实测 10:29 已成交 7795 亿 → 外推 28487 亿，
    比前一天全天还多 50%，而同期基准当时只有 1 天样本。所以盘中 `amount` 一律
    返回 None，界面只报"已成交"（事实）+ "与近期同期的节奏对比"（也是事实）；
    模式判定仍按折算走，但明确标成"暂判"。收盘后 `amount` 才是全天终值。
    ref_amount：近 N 个已收盘交易日全天额中位数，用于算节奏倍数。

    hist：沪指日K（近 N 日）—— 供相对量能层算分位与环比，见 _relative_volume。
    """
    v = cfg.get("M2_volume", {})
    if not v.get("enabled", True):
        return {"mode": "disabled", "label": "量能模块已关闭", "advice": "自行判断",
                "short_only": False, "amount": None, "cum_amount": 0.0,
                "intraday": False, "pace": None, "pace_txt": "", "pace_src": "", "basis": "",
                "rel": None, "trend": None, "rel_txt": "", "gauge": None}

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
                "pace": None, "pace_txt": "", "pace_src": "", "raw_amount": round(amt_raw, 0),
                "basis": "无（当日数据未形成）", "red_flag": "",
                "rel": None, "trend": None, "rel_txt": "", "gauge": None,
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
                "pace": None, "pace_txt": "", "pace_src": "", "raw_amount": 0,
                "basis": "无（盘中数据未就绪）", "red_flag": "",
                "rel": None, "trend": None, "rel_txt": "", "gauge": None,
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

    # 节奏倍数：当前累计 ÷ 「近期**同一时刻**该有的累计」。这是**事实型**对比，
    # 比"外推全天额"稳得多，也是界面上真正有用的那句话。
    #
    # ★ 口径必须是钱对钱（v1.32.2）：基准只能取**档案里实测的同期累计额**。
    #   原条件的 `ref_amount > 0 and amt_raw > 0` 漏掉了"必须有实测同期基准"这一条，
    #   于是 frac_hint 为 None（实测占比样本不足）时仍强行用"全天额中位数 × 写死的
    #   经验曲线"造出一个看着确定的 pace —— 实测 2026-10-13 午间：经验曲线认为只走了
    #   45%，而 10-12 档案实测走了 67%，两者差 36%，足以把"与近期同期相当"
    #   渲染成"快 33%（明显放量）"；而分支阈值(1.25/0.8)本身就落在模型误差里。
    #
    # 三档边界（样本不足的档位不许把话说满）：
    #   ① 有实测同期累计 → 直接比（钱对钱）；样本 < min_base_days 时把天数写进文案
    #   ② 只有"实测占比 + 全天额中位数" → 折算后比（兼容不传实测累计的调用方）
    #   ③ 两者都没有 → 明说"同期基准积累中"，**不给数字**（不造一个看着确定的数）
    pace = None
    pace_txt = ""
    pace_src = ""
    if intraday:
        pace_fast, pace_slow, min_base_days = 1.25, 0.80, 3
        try:
            pace_fast = float(v.get("pace_fast", pace_fast) or pace_fast)
            pace_slow = float(v.get("pace_slow", pace_slow) or pace_slow)
            min_base_days = int(v.get("min_base_days", min_base_days) or min_base_days)
        except (TypeError, ValueError):
            pace_fast, pace_slow, min_base_days = 1.25, 0.80, 3
        frac_now = max(_traded_fraction(tmin), 0.02)
        frac_ok = bool(frac_hint) and 0.02 <= float(frac_hint) <= 1.0
        if frac_ok:
            frac_now = max(float(frac_hint), 0.02)
        exp, base_days = 0.0, 0
        if ref_cum > 0:
            exp, pace_src, base_days = float(ref_cum), "实测同期", int(ref_cum_days or 0)
        elif ref_amount > 0 and frac_ok:
            exp, pace_src = ref_amount * frac_now, "实测占比折算"
        if exp > 0 and amt_raw > 0:
            # ★ 判定用**原始比值**，四舍五入只用于对外显示：原来先 round(…, 2) 再比阈值，
            #   1.2450~1.2499 这类"差一点点"的读数会被进位成 1.25 而误判成"明显放量"
            #   （实测 12499/10000 = 1.2499 正落在这个缝里）—— 等于把阈值边界整体
            #   挪了 0.005。内部保持原值，只在返回时 round。
            pace = amt_raw / exp
            if pace >= pace_fast:
                pace_txt = f"比近期同期快 {(pace-1)*100:.0f}%（明显放量）"
            elif pace <= pace_slow:
                pace_txt = f"比近期同期慢 {(1-pace)*100:.0f}%（明显缩量）"
            else:
                pace_txt = "与近期同期相当"
            if pace_src == "实测同期" and 0 < base_days < min_base_days:
                pace_txt += f"（同期样本 {base_days} 天，仅供参考）"
        else:
            pace_txt = f"同期基准积累中（需 {min_base_days} 个以上交易日）"

    try:
        peak = float(v.get("peak_threshold", 15000) or 15000)
        comfort = float(v.get("comfort_threshold", 12000) or 12000)
        shrink = float(v.get("shrink_threshold", 8000) or 8000)
    except (TypeError, ValueError):
        peak, comfort, shrink = 15000.0, 12000.0, 8000.0
    # 阶梯必须单调（peak ≥ comfort ≥ shrink）。历史存档里 comfort=5000 反而
    # 低于 shrink=12000，而分支按 comfort→shrink 顺序判断，结果整个
    # [comfort, peak) 区间被 comfort 吃掉、`shrink_short` 成了**永不触发的
    # 死分支**（实测 8000 亿被报"量能舒适·游资具备轮动能力"）。就地纠正，
    # 并让配置加载层同步迁移存档（见 strategy_config.load_config）。
    _ladder_fixed = not (peak >= comfort >= shrink)
    if _ladder_fixed:
        peak, comfort, shrink = sorted((peak, comfort, shrink), reverse=True)

    # ---- ① 绝对档位：钱够不够撬动大票（物理约束，决定上限）----
    if amt >= peak:
        tier = 3
    elif amt >= comfort:
        tier = 2
    elif amt >= shrink:
        tier = 1
    else:
        tier = 0

    # ---- ② 相对修正：只在"相对明显偏低"时**降档**（绝不升档）----
    # 绝对阈值回答"够不够撬动大票"，相对位置回答"这点钱相对最近是多是少"。
    # 只降不升：8000 亿即便分位再高也不该被叫"峰值量能"（那是更荒谬的错）。
    lb = int(v.get("lookback", 20) or 20)
    rel, trend, rel_src = _relative_volume(
        hist, frac if intraday else 1.0, lookback=lb)
    try:
        peak_pct = float(v.get("peak_pct", 0.80))
        peak_trend_min = float(v.get("peak_trend_min", 1.00))
        shrink_pct = float(v.get("shrink_pct", 0.45))
    except (TypeError, ValueError):
        peak_pct, peak_trend_min, shrink_pct = 0.80, 1.00, 0.45

    _TIER_CN = {3: "峰值量能", 2: "量能舒适", 1: "缩量存量", 0: "极度缩量"}
    rel_adj = ""
    if rel is not None:
        _pos = f"{rel_src}的{rel*100:.0f}%分位"
        _tr = f"、为中位数的{trend*100:.0f}%" if trend is not None else ""
        _orig = tier
        # 两条降档判据各自独立判断（不是 if/elif）—— 峰值档既可能只降到
        # "舒适"（放量后回落），也可能一步降到"缩量存量"（已缩到低位），
        # 用 elif 会把后一步吃掉（实测 15378 亿应落到"缩量存量"却停在"舒适"）。
        if tier == 3 and (rel < peak_pct
                          or (trend is not None and trend < peak_trend_min)):
            tier = 2
        if tier == 2 and rel <= shrink_pct:
            tier = 1
        if tier != _orig:
            # ★ 这里原来写的是「成交额绝对值够（14144亿），但仅处近19日的21%分位
            #   —— 相对近期明显偏低，由量能舒适降为缩量存量」。实测 20261014
            #   渲染出来是一句自相矛盾的话：先说"够"再降档，读的人只会困惑
            #   "到底够不够"。
            #
            #   真相是两层各答各的问题，不该混成一句：
            #     · 绝对档位（peak 15000 / comfort 12000 / shrink 8000）回答的是
            #       "钱够不够撬动大票"这个**物理约束**，它的阀值是常量、不随市场
            #       漂移 —— 这是刻意的（见 core/amount.py 顶部：按分位重标会让最近
            #       15 天里 11 天判 dry 并建议空仓，同期涨停却有 32~101 只）。
            #       代价是它在当前市场下长期停在"舒适"，阈值本身已经**过期**。
            #     · 相对分位回答的是"这点钱相对最近是多是少"，它只降不升。
            #
            #   ★ 措辞要短，因为调用方已经把「两市成交 X亿，属于<档位名>」说在
            #     前面了（web/index.html 决策步骤第 2 问）。这里再说一遍金额和
            #     档位名，整句就变成「14144…属于缩量存量。成交额 14144亿…降为
            #     缩量存量；缩量存量，资金撬不动大票」—— 同一句话里同一个数字
            #     出现两次、同一个档位名出现三次（实测）。所以这一段只补
            #     **调用方没说过的信息**：阈值已过期这件事，以及为什么此刻要靠
            #     相对分位定档。
            _why = []
            if _orig == 3:
                _why.append(f"绝对上已过峰值阈值 {int(peak)}亿")
            elif _orig == 2:
                _why.append(f"名义上仍高于绝对阈值 {int(comfort)}亿")
            _expired = (f"绝对阈值是早期定下的，当前市场已长期低于它，"
                        f"该档实际由相对分位决定") if _orig == 2 else ""
            rel_adj = (
                f"（{'，'.join(_why)}" + (f"；{_expired}" if _expired else "")
                + f"）但仅处{_pos}{_tr} —— 相对近期明显偏低，"
                + ("不构成峰值量能，" if _orig == 3 else "")
                + f"由{_TIER_CN[_orig]}降为{_TIER_CN[tier]}")

    # ★ 放量下跌 ≠ 可以做中长线（v1.36.0 修）：原来 tier==3 的 advice 是
    #   「资金进得去大市值，做中长线波段/趋势票」，**只看成交额单因子、不看
    #   涨跌方向**。2026-10-15 实测：两市 15696 亿（rel=0.947 分位 95%、
    #   trend=1.19）判 peak_swing，而同一天沪指 -2.03%、深成指 -2.91%、
    #   创业板指 -3.68%、跌 5122 家 —— 放量下跌恰恰是中长线最差的进场点。
    #   `engine.py:119-125` 的长注释已经修过"放量后的回落被判成峰值"，但
    #   这一条（放量**下跌**）当时没修。
    #   判据只认**当日**指数方向：末根 K 线收盘 ≤ 开盘 即当日收阴。
    #   数据不足（hist 空/末根缺 OHLC）→ 不额外提示，保持原 advice 不变。
    _down_day = False
    if hist:
        _b = hist[-1] or {}
        try:
            _o, _c = float(_b["open"]), float(_b["close"])
            _down_day = _o > 0 and _c <= _o
        except (KeyError, TypeError, ValueError):
            _down_day = False

    if tier == 3:
        mode = "peak_swing"
        advice = ("资金进得去大市值，做中长线波段/趋势票"
                  if not _down_day else
                  "量能虽大但当日指数收阴 —— 是放量下跌不是放量上攻，"
                  "别在此位置建中长线仓，等指数企稳再谈波段")
        short_only = False
    elif tier == 2:
        mode = "comfort_short"
        advice = (f"量能处于舒适区（{int(comfort)}亿以上），"
                  f"游资具备轮动能力，题材可做"
                  + ("　※但当日指数收阴 —— 题材也只在低吸位做，不追高"
                     if _down_day else ""))
        short_only = False
    elif tier == 1:
        mode = "shrink_short"
        advice = ("缩量存量，资金撬不动大票 —— 这时才切短线中小题材的应对打法"
                  "（平常中长线波段，不打板不做短线），并降低单票仓位")
        short_only = True
    else:
        mode = "dry"
        advice = "极度缩量，量能不够说破大天都没用 —— 建议空仓等待量能恢复"
        short_only = True

    # ★ 两种 advice 的写法（rel_adj 说没说「由X降为Y」）：
    #   说了 → 档位名已经出现过，advice 只接**该怎么做**（去掉开头的档位名，
    #          否则整句变成「…降为缩量存量；缩量存量，资金撬不动大票」）；
    #   没说 → advice 自带档位名，信息完整。
    #   实测 20261014 的渲染就是这个坑：rel_adj 结尾「降为缩量存量」后面紧跟
    #   advice 开头的「缩量存量，」。同名字连读两遍，读起来像卡带。
    # ★ v1.36.0：`_tail` 剥掉档位名后可能**只剩一个标点**（例如 advice 就是
    #   "量能舒适，可做" → 剥完 "可做" 还好；但若是 "缩量存量。" 这种纯标点
    #   开头），拼出来会出现"…降为缩量存量；。"这种双标点。另外 tier3 在
    #   放量下跌时会走多句 advice，`rel_adj` 的"；"会把它和后文挤成一句。
    #   这里统一：剥完先剥开头的孤立标点；拼接用"。"而非"；"，让两句各成一句。
    if rel_adj:
        _tail = advice
        for _nm in ("缩量存量", "极度缩量", "量能舒适", "峰值量能"):
            if _tail.startswith(_nm):
                _tail = _tail[len(_nm):].lstrip("，,、 ")
                break
        # 剥完若以标点开头（纯标点残留）→ 继续剥，避免"；。"这种双标点
        _tail = _tail.lstrip("，,、。；;!！~ ")
        if _tail:
            advice = f"{rel_adj}。{_tail}"
        else:
            advice = rel_adj

    # 极致缩量还红盘
    #
    # ★ v1.36.0 修两处（用户实测 2026-10-15）：原文是
    #       recent = hist[-5:]
    #       if recent and all(b["close"] > b["open"] for b in recent[-3:]):
    #   ① `hist[-5:]` 切出来只用了 `[-3:]`，前两根是**死代码**，`recent` 变量
    #      毫无意义 —— 直接写 `hist[-3:]` 才是它真正想表达的。
    #   ② 判据错位：它看的是**近 3 日**是否连续收阳，而不是**当日**是否红盘。
    #      当日三大指数全线 -2.0% ~ -3.7%、跌 5122 家，只要前三天恰好收阳，
    #      就会给用户输出「筹码结构好，可低吸」—— 与当日盘面直接相反，这是
    #      「市场的情况和文字不符」里最容易造成真实亏损的一条。
    #   红盘说的是**今天这根 K 线**：close > open 即红盘（平盘不算）。
    #   数据不足（hist 为空或末根缺 OHLC）→ 不判定，绝不猜。
    red_flag = ""
    if v.get("shrink_red_flag") and hist:
        _today = hist[-1] or {}
        try:
            _o = float(_today["open"])
            _c = float(_today["close"])
        except (KeyError, TypeError, ValueError):
            _o = _c = None
        if _o is not None and _o > 0 and _c > _o:
            if mode in ("shrink_short", "dry"):
                mode = "shrink_red"
                advice = ("极致缩量还红盘 = 控盘好、散户90%套牢躺平 → "
                          "筹码结构好，可低吸；但这是缩量期的权宜打法，不是常态策略")
                red_flag = "缩量红盘"

    # 进度条标尺：交给后端算，前端不再写死"多少亿算满格"（原为 amount/180）。
    if intraday:
        gauge = min(100, round(pace * 55)) if pace is not None else None
    else:
        gauge = min(100, round(amt / max(peak * 1.2, 1.0) * 100))

    return {
        "mode": mode,
        "label": VOLUME_MODES.get(mode, mode),
        "advice": f"{advice}（量能口径：{basis}）",
        "short_only": short_only,
        # 全天成交额：**只有收盘后它才是事实**。盘中一律 None —— 界面不给外推数字。
        "amount": None if intraday else round(amt, 0),
        "cum_amount": round(amt_raw, 0),      # 盘中"已成交"（事实）
        "intraday": intraday,
        "pace": None if pace is None else round(pace, 2),   # 节奏倍数（None=基准不够）
        "pace_txt": pace_txt,
        "pace_src": pace_src,                 # 基准口径：实测同期 / 实测占比折算 / ""
        "raw_amount": round(amt_raw, 0),
        "basis": basis,
        "red_flag": red_flag,
        # 相对量能层：分位 / 相对中位数倍数 / 说明，供界面解释"为什么这么判"
        "rel": None if rel is None else round(rel, 3),
        "trend": None if trend is None else round(trend, 3),
        "rel_txt": ("" if rel is None else
                    f"{rel_src}分位 {rel*100:.0f}%"
                    + (f"、为中位数 {trend*100:.0f}%" if trend is not None else "")),
        "gauge": gauge,
        "ladder_fixed": _ladder_fixed,        # 阈值曾倒挂并被就地纠正（存档待迁移）
    }


# ================================================================ M3 情绪周期

SENTIMENT_STAGES = {
    "probe": "新周期首板 · 试错阶段",
    "climax": "情绪高潮 · 龙头主升",
    "follow": "后排跟风 · 扩散期",
    "ebb": "退潮期 · 无条件清仓",
    "unknown": "情绪数据不足",
}


def _is_live_health(health: dict | None) -> bool:
    """这份健康度是不是**实测**出来的（而不是"调用方没声明数据状态"时的空壳）。

    调用方连 health 都不传时，data_note 里的"指数/涨跌家数降级"根本无从谈起 ——
    那是显式喂数据、自己另做记账的调用点（如 selfcheck），引擎不该替它编一句
    降级提示。判据取 health_best 的 checked=-1 或 data_health 的 has_data。
    """
    h = health or {}
    if "has_data" in h:
        return bool(h.get("has_data"))
    return "has_data" not in h and "degraded" in h


def _resolve_health(n_zt: int, n_dt: int, n_zb: int) -> dict:
    """调用方没声明数据状态时，引擎自己去问数据源要一份（见 evaluate_sentiment）。

    判据**只认本轮** data_health()，绝不吃上一轮的结论：
      ① datasource.data_health()：本轮记账。返回体本身就是权威就绪判据
         （含 has_data，供 evaluate_sentiment 判"这一帧没有数据流"）。
         **不能**改回 health_best()/health_fallback()：那是"本轮记账 ∪ 上一轮
         降级"的保守合成，`checked: -1` 标记的就是"这条降级来自上一轮、不是
         本轮实测"。把它当实测证据用，等于让**上一轮**的降级永久污染此后每一帧
         —— 而消费点 pipeline.py:339 每轮开头都 reset_health()，reset 又会把
         刚跑完那轮的降级快照进 _health_last_round_bad（datasource.py:478），
         于是"上一轮降级"几乎永远非空、闸门几乎永远不放行，正是用户报的
         「过一会获得数据了就说不能出手」。判据只认本轮，那一轮没实测到降级
         就是没降级。
      ② 本轮记账为空（新进程第一轮 / 三池命中 TTL 没重新请求）→ 看三池是不是
         一个都没喂进来：全空就按"这次没取到"处理 —— 宁可让界面等一下，也不能
         在零数据上宣布"可以出手"。三池里有任意一个有货（哪怕另外两个是合法的
         空池）就不拦，避免把真行情误判成缺数据。
      ③ 数据源模块导入失败（极端环境）→ 只退回同一条"全空才算未就绪"，绝不放行。
    """
    try:
        from core import datasource as _ds
        return _ds.data_health()
    except Exception:
        pass
    if not (n_zt or n_dt or n_zb):
        return {"ok": False, "checked": 0,
                "degraded": [{"key": f"pool:{k}", "label": lbl, "ok": False,
                              "note": "本轮三池都没取到数据"}
                             for k, lbl in (("ZT", "涨停池"), ("DT", "跌停池"),
                                            ("ZB", "炸板池"))],
                "summary": "涨停池降级；跌停池降级；炸板池降级"}
    return {"ok": True, "degraded": [], "checked": 0, "summary": "关键数据全部正常"}


def evaluate_sentiment(cfg: dict, limit_up: list[dict], limit_down: list[dict],
                       broken: list[dict], zt_hist: list[dict],
                       real_date: str = "", tmin: int | None = None,
                       health: dict | None = None) -> dict:
    """M3 情绪周期定进退时点 —— 决定「能不能出手」。

    退潮期判定严格按「全能的野人」9月28日视频原话口径，四个现象
    任一成立即判退潮，不做任何额外加固：
      ① 亏钱效应扩散：亏钱面积（跌停+炸板占三分池比例）≥ 30%
      ② 中位股成排跌停：跌停/涨停 ≥ 0.15
      ③ A字杀不反包：炸板/涨停 ≥ 0.50
      ④ 高度标的被闷杀：跌停 ≥ 5 只
    判定成立 → 无条件清仓，停止一切低吸和打板，管住手。

    ★ 数据就绪闸门（v1.38.6，用户实测的顽固 bug 第三次复发后补的引擎层堵漏）
      症状：「每次首次启动就数据还未获得齐全就说可以出手，过一会获得数据了
      就说不能出手」。措辞上补过两次补丁（web/index.html 结论卡那段注释），
      病根一直没堵 —— 它根本不在措辞层，在**这一层**。

      根因链（三池接口全挂时）：
        · core/datasource.py 的三池取数失败 → 回退当日旧快照、失败则返回空表；
          降级只记进了 `data_health()`（_health_set），引擎层从来不消费它。
        · 空表喂进这里 → total=0 → 亏钱面积 = 0.0 → 四个退潮现象**一个都不成立**
          （判据全挂在 n_zt/n_dt 上）→ stage="unknown"。
        · 而 can_trade = stage != "ebb" → True，文案「可按周期节奏操作」。
      也就是说：**「接口没取到」与「今天真的没有涨停」在这一层长得一模一样，
      而两者被当成了同一件事——"没有退潮信号"。** 没有数据 ≠ 数据好。

      闸门口径（与用户敲定）：
        · 判据只认 `datasource.data_health()`，不自己看列表长度 —— 空池可能
          是真的没有跌停股（合法空值），也可能是接口没给（不合法空值），
          列表长度分不出这两者，降级记账分得出（见 _fetch_pool 的注释）。
        · 三池 pool:ZT/DT/ZB 任意一个 ok=False → 数据未就绪：data_ready=False、
          can_trade=False、not_ready_reason 写明缺什么。**没数据永远不等于
          可以出手** —— 这是安全侧的不对称：多等一下只损失一次机会，
          误放行是拿本金去赌一个没验证过的市场状态。
        · 指数/涨跌家数降级只降可信度（data_note），不阻断结论。
        · 退潮判定的阈值与信号口径一个字没动：数据齐了该怎么判还怎么判。

    health: 数据健康度（core/datasource.data_health()）或它 subsets 出的子集
            {ok, degraded:[{key,label,note}], summary}。**必须是本轮口径**。
            **为 None 表示"调用方不声明数据状态"**，此时引擎**自己去问数据源**
            （datasource.data_health()，仍是本轮）；只有连数据源都问不出结论
            （从没记过账、且三池一个都没喂进来）时才退化成"空池即未就绪"。
            ★ 就绪判据**只认本轮**：data_health() 的 degraded 就是唯一依据，
              跨轮提示一律不进闸门（详见 _resolve_health 的注释）。
    """
    s = cfg.get("M3_sentiment", {})
    n_zt = len(limit_up)
    n_dt = len(limit_down)
    n_zb = len(broken)

    # ── 数据就绪闸门：先判"能不能看"，再判"看到的是冷是热" ──
    # 就绪判据**必须由数据源说了算，不能自己看列表长度**：清单为空既可能是
    # 今天真没有跌停股（合法空值），也可能是接口没给（不合法空值），长度分不出
    # 这两者，降级记账分得出（见 datasource._fetch_pool 与 _fetch_memo）。
    #
    # health 为 None 时**不能**直接放行（那正是冷启动 bug 的入口：第一个调用点
    # 忘了传 health，闸门就等于不存在）。改为自己去问数据源：health_best()
    # 把本轮记账与上一轮结论取更保守的一份 —— 覆盖"本轮还没调过三池接口"
    # 与"三池命中 TTL 缓存没重新请求"这两个空窗；再问不出结论时，用"三池
    # 一个都没喂进来"当最后一道兜底（总比静默放行强，且不误伤任何有数据的调用）。
    if health is None:
        health = _resolve_health(n_zt, n_dt, n_zb)
    # _deg 只从**本轮实测**的 degradation 里筛 pool:*。health 为 None 时
    # _resolve_health 给的就是本轮 data_health()；调用方显式传进来的那份也
    # 必须是本轮口径（pipeline/server 传的都是 ds.data_health()）。绝不能
    # 让 health_fallback() 的上一轮条目参与这里 —— 它只能进 not_ready_reason
    # / data_note 的说明文字（见下方 elif not _deg 那段）。
    _deg = [d for d in list((health or {}).get("degraded") or [])
            if str(d.get("key") or "").startswith("pool:")]
    # 第三态：三池没有一个降级、可是这一帧**根本没取到任何数据流**（本轮记账
    # 一条都没有）。冷启动首屏正是这样：接口还没调、清单是空的，降级记账也是
    # 空的 —— 于是既没有报错可看，也拿不到涨停家数，四类退潮信号全不成立，
    # stage 落到 unknown、can_trade 落到 True。这就是用户反复看到的那一幕。
    # _no_data_flow 只在"有数据流证据、但证据不足以说明关键池可用"时才为真：
    # 空池真帧会带 has_data=True（见 datasource.data_health），不会被误伤。
    _no_data_flow = (health is not None and "has_data" in health
                     and not health.get("has_data")
                     and not (n_zt or n_dt or n_zb))
    data_ready = not _deg and not _no_data_flow
    not_ready_reason = ""
    data_note = ""
    if not data_ready:
        _labs = [str(d.get("label") or d.get("key") or "") for d in _deg]
        _why = "; ".join(str(d.get("note") or "").strip()
                         for d in _deg if str(d.get("note") or "").strip())
        if not _labs:
            _labs = ["涨停池", "跌停池", "炸板池"]
            _why = "本轮还没有取到任何池子数据"
        not_ready_reason = (f"{'、'.join(_labs)}接口本次未取到"
                            + (f"（{_why}）" if _why else "")
                            + "；没有这三个池子就算不出亏钱面积，"
                              "情绪状态无从判断 —— 数据没到手前一律按不能出手处理")
    elif _is_live_health(health):
        # 只有真降级过、且已经记过账（checked>0）时才提示。checked==0 说明
        # 本轮还没碰过任何接口（比如直接喂数据的调用点），不是"一切正常"。
        _h = health or {}
        if _h.get("checked") and not _h.get("ok"):
            data_note = (str(_h.get("summary") or "")
                         + "（只影响可信度，不影响结论）")
        elif not _deg:
            # 本轮完全干净 —— 就绪判据到此为止，结论照常给。
            # 这里只**补一句说明**：上一轮记过降级、本轮又没重新请求（TTL 命中）
            # 时提一句，免得界面看起来"数据一直是好的"。它只是文案，**不参与
            # data_ready、不参与 can_trade** —— 见 datasource.health_fallback。
            try:
                from core import datasource as _ds
                _prev = _ds.health_fallback()
                _pdeg = [d for d in (_prev.get("degraded") or [])
                         if str(d.get("key") or "").startswith("pool:")]
                if _pdeg:
                    data_note = ("（另："
                                 + "、".join(str(d.get("label") or d.get("key") or "")
                                             for d in _pdeg)
                                 + "本轮尚未重新取到，按上一轮口径仅供参考）")
            except Exception:                                # noqa: BLE001
                pass

    if not s.get("enabled", True):
        return {"stage": "disabled", "label": "情绪模块已关闭", "can_trade": True,
                "note": "不参与情绪判断", "metrics": {},
                "data_ready": data_ready,
                "not_ready_reason": not_ready_reason,
                "data_note": data_note}

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
    # ★ 未就绪时不喂样本：空池算出来的 hurt_area 是 0.0，那是"没数据"不是
    #   "没风险"，喂进去等于往稳定器里灌假样本（还会拖慢真退潮的确认）。
    st = stab.stabilize(real_date, is_ebb, hurt_area, hurt_ratio,
                        tmin=tmin, cfg=cfg) if data_ready else None
    ebb_confirmed = bool(st and st["confirmed"])

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

    # ★ 安全侧不对称：没数据 → 一定不能出手。can_trade 是"要不要动钱"的闸门，
    #   它的默认值必须是"不动"—— 多等一次刷新只损失一次机会，误放行是拿本金
    #   去赌一个还没验证过的市场状态。
    can_trade = (data_ready
                 and (stage != "ebb" or not s.get("clear_on_ebb", True)))

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
    if not data_ready:
        # 未就绪时 note 只讲"数据没到手"，不讲任何行情判断 ——
        # 这里给的话术会被前端直接显示在结论卡上，说错方向等于又骗用户一次。
        note = not_ready_reason
    elif stage == "ebb":
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

    # ★ 2026-10-15 修：label 原来是静态的「退潮期 · 无条件清仓」，可是退潮期
    #   按野哥规矩**允许 0.5 成试错仓**（trial_only=True）。于是页面上同一屏
    #   出现「无条件清仓」与「最多 0.5 成试错仓」两句互相打架的话 —— 用户看到
    #   的就是"一会说能买、一会说不推荐买"。label 必须跟着**实际允许的动作**走：
    #   能试错就说能试错，权重调成 0 才回到"无条件清仓"。
    label = SENTIMENT_STAGES.get(stage, stage)
    if stage == "ebb" and trial_only:
        label = "退潮期 · 只做首板试错仓"
    return {
        "stage": stage,
        "label": label,
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
        # 数据就绪闸门的三个字段（前端据此打叉 / 说明"为什么先别动"）：
        #   data_ready        关键池数据是否可用（三池任一降级即 False）
        #   not_ready_reason  未就绪时"缺什么"的中文说明（就绪时为空串）
        #   data_note         降级但**不阻断结论**时的提示（无则空串）
        "data_ready": data_ready,
        "not_ready_reason": not_ready_reason,
        "data_note": data_note,
        # 稳定器的完整状态：前端据此显示"预警/确认"和已持续多久
        # （未就绪时为 None —— 这一帧根本没喂样本，没有"稳定状态"可言；
        #   前端已用 `st||{}` 兜底，不会炸）
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

def _num(v, fallback: float) -> float:
    """把配置里读出来的值转成 float —— 配置是用户可改的 JSON，
    里面什么都可能出现（字符串、null、中文）。转不动就用内置缺省，
    绝不让一个错配置把整次扫描打断。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    return f if f == f else fallback      # NaN 也当转不动


def _imagination_score(snap: dict, bars: list[dict], zt_map: dict,
                       cfg_a: dict | None = None) -> tuple[float, list[str]]:
    """③ 想象力：名字好听 + 题材新 + 多重概念叠加 = 妖股有多条命。"""
    cfg_a = cfg_a or {}
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
    # ★ 已按区分度检验结果关闭（v1.37.0，见 _seal_quality_score 上方注释）：
    #   换手率的秩相关实测 +0.00、各档忽高忽低（28.0/15.9/15.9/16.4/19.4），
    #   是纯噪音。留着它占想象力分 15% 的权重，等于往排序里掺随机数。
    to = snap.get("turnover", 0)
    if not cfg_a.get("drop_turnover_weight", True):
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
    wash = ind.washout_signal(bars)
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
    # ★ 单位对齐（v1.26.0）：drawdown_from_high 返回的是**小数**（0.15 = 15%），
    #   原来拿它直接和 10/45/60 这些**百分数**比 —— 小数最大只有 1，
    #   于是 `10 <= dd <= 45` 与 `dd > 60` 是**永不命中的死分支**，而
    #   `dd < 3` 恒成立：加 0.08 的"洗盘空间充分"永远给不出，同时**每一只**
    #   票都被贴上"处于60日新高、位置偏高、获利盘压力大"的错误警示 ——
    #   包括实际已回撤 40% 的票，且与同一函数给出的"深度底部"结论直接打架。
    dd_pct = dd * 100
    if 10 <= dd_pct <= 45:
        score += 0.08
        reasons.append(f"距60日高回撤{dd_pct:.0f}%，洗盘空间充分")
    elif dd_pct < 3:
        # 负向结论要进 warnings —— 详情页「为什么选它」列的是理由，
        # 把"位置偏高、获利盘压力大"写进理由列，等于自相矛盾
        warns.append(f"处于60日新高（回撤{dd_pct:.0f}%），位置偏高、获利盘压力大")
    elif dd_pct > 60:
        warns.append(f"距高点回撤{dd_pct:.0f}%，趋势已破，非洗盘而是走弱")

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


def _seal_quality_score(snap: dict, zt_row: dict | None,
                        a: dict) -> tuple[float, list[str]]:
    """封板质量打分 —— 全系统区分度最强的因子（v1.37.0）。

    依据（core/discrimination.py 四关检验，243 天归档 × 当日涨停池 = 16542 条，
    结果 = 次日是否再涨停，基线 19.1%）：
      · 封板时间  lift 3.73x  秩相关 +1.00  10.0% → 37.3%
      · 封单额    lift 2.63x  秩相关 +1.00  12.2% → 32.2%
    两者都单调、方向一致、强度接近基线的两倍 —— 是全项目唯一过了四关的量价因子。
    此前它们只用在 `_first_board_bonus`（仅今日首板、上限 +10 分），
    连板股与未涨停票完全拿不到，等于把这个最强信号关在门外。

    打分（0~1，由 M4_aesthetic.weight_seal_quality 决定最终权重）：
      · 封板时间 60%：按首封时间线性给分，越早越高。
      · 封单额 40%：封单额 / 流通市值，按分位标定，越厚越高。

    ★ 标定取值来自实测分位（2026-10-14 涨停池 30 只），不是拍脑袋：
      首封时间  09:25 竞价一字 9 只 / 09:30 5 只 / 09:31~10:06 逐只递增。
      封单/流通 p10 1.51% · p50 2.71% · p90 8.33% · p100 15.37%。
      最初把「09:30 满分、14:00 归零」「1.5% 满分」直接写死，实测导致
      **30 只里 9 只并列满分、最低仍有 75 分** —— 正是 discrimination G1
      要抓的「塌缩」。所以这里改用两端标定：
        时间：09:25(竞价一字) 满分 → 10:30 之后归零，覆盖真实的 09:25~10:06 区间；
        封单：0.5%(p00 附近) 起分 → 8%(p90) 满分，让中位 2.71% 落在中间而不是顶部。
      两者都是「越高越好」的单调映射，不会改变检验出的单调性。
    """
    if not zt_row:
        return 0.0, []
    try:
        early_full = int(a.get("seal_early_full", 92500) or 92500)
        early_zero = int(a.get("seal_early_zero", 103000) or 103000)
        ratio_full = float(a.get("seal_ratio_full", 0.08) or 0)
        ratio_zero = float(a.get("seal_ratio_zero", 0.005) or 0)
    except (TypeError, ValueError):
        early_full, early_zero, ratio_full, ratio_zero = 92500, 103000, 0.08, 0.005

    score = 0.0
    why: list[str] = []

    ft = int(zt_row.get("first_time") or 0)
    if ft and early_zero > early_full:
        # 越早封分越高；early_full 及更早封顶、early_zero 及更晚归零
        r = (early_zero - ft) / (early_zero - early_full)
        r = max(0.0, min(1.0, r))
        score += 0.6 * r
        why.append(f"{ft // 10000:02d}:{ft // 100 % 100:02d} 封板")

    if ratio_full > ratio_zero:
        mv = float(snap.get("float_mv") or 0)
        fund = float(zt_row.get("fund") or 0)
        if mv > 0 and fund > 0:
            ratio = fund / mv
            r = (ratio - ratio_zero) / (ratio_full - ratio_zero)
            r = max(0.0, min(1.0, r))
            score += 0.4 * r
            why.append(f"封单 {ratio * 100:.2f}% 流通")

    return min(score, 1.0), why


def _small_size_score(snap: dict, a: dict) -> tuple[float, list[str]]:
    """小市值 / 小成交额加分 —— 方向为负的因子要反着用（v1.37.0）。

    依据（同一份四关检验）：
      · 流通市值  lift 0.59x  秩相关 −1.00  6.8~22.3亿 23.9% → 1087亿以上 14.0%
      · 当日成交额 lift 0.49x 秩相关 −1.00  144万~2.2亿 28.2% → 15.9亿以上 13.9%
    ★ 这两条**不是"没用"**：秩相关完美 −1.00 说明关系稳定单调，
      只是方向和小盘直觉一致、和"越大越强"相反 —— 小盘好拉。
      所以这里给**小**的加分，而不是给大的扣分（不引入惩罚，避免双重计数：
      `_price_mv_score` 已经在按 mv_max / mv_mid_threshold 扣大票的分了）。

    打分：市值与成交额各占一半，各自在"样本最小区间"给满分、到上限归零。
    """
    score = 0.0
    why: list[str] = []

    try:
        mv_full = float(a.get("small_mv_full", 20.0) or 20.0)
        mv_zero = float(a.get("small_mv_zero", 200.0) or 200.0)
        amt_full = float(a.get("small_amt_full", 2e8) or 2e8)
        amt_zero = float(a.get("small_amt_zero", 2e9) or 2e9)
    except (TypeError, ValueError):
        mv_full, mv_zero, amt_full, amt_zero = 20.0, 200.0, 2e8, 2e9

    mv = float(snap.get("float_mv") or 0) / 1e8
    if mv > 0 and mv_zero > mv_full:
        r = (mv_zero - mv) / (mv_zero - mv_full)
        r = max(0.0, min(1.0, r))
        score += 0.5 * r
        if r > 0:
            why.append(f"小盘 {mv:.1f}亿")

    amt = float(snap.get("amount") or 0)
    if amt > 0 and amt_zero > amt_full:
        r = (amt_zero - amt) / (amt_zero - amt_full)
        r = max(0.0, min(1.0, r))
        score += 0.5 * r
        if r > 0:
            why.append(f"成交 {amt / 1e8:.1f}亿")

    return min(score, 1.0), why


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

    # 「情绪题材 / 业绩配置」二分：机构主导或中大盘趋势票 = 业绩配置（能穿越
    # 披露期）；游资/小市值/连板基因 = 情绪题材（只配短线，披露期易被证伪）。
    _is_config = (ftype == "institutional") or (mv >= mv_mid)
    nature = ({"key": "config", "label": "业绩配置",
               "note": "机构主导/中大盘趋势票 —— 有业绩兜底，能穿越披露期"}
              if _is_config else
              {"key": "theme", "label": "情绪题材",
               "note": "游资/小市值/连板基因 —— 靠情绪与政策驱动，"
                       "披露期易被证伪，只配短线"})

    if ftype == "zhuang":
        return {"key": "avoid", "label": "庄股 · 不参与",
                "nature": nature,
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
                "nature": nature,
                "note": ("平常打法：大资金/趋势票 —— 沿5日线持有、小阳线爬坡不动手；"
                         "突然加速拉涨停 = 派发信号，减仓而不是加仓。")}

    if lus20 >= 2:
        return {"key": "short", "label": "短线题材",
                "nature": nature,
                "note": ("缩量期的应对打法：有连板基因的题材票 —— 小仓位、快进快出，"
                         "跟着断板节奏走；节奏破坏就撤，不把短线拿成中线。")}
    return {"key": "short", "label": "短线题材",
            "nature": nature,
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


def _first_board_bonus(snap: dict, zt_row: dict | None,
                       a: dict) -> tuple[float, list[str]]:
    """「可打的首板」加权 —— 让今天**真能打**的首板优先进入候选（用户选定方案 A）。

    背景（用户 10-13）：候选原来只按审美三要素打分排序，完全不看"今天能不能动手"
    —— 实测 3 只已 3 连板（体系定性"不接力"）占了前三位，而当天涨停池里
    22 只真首板（含价格/市值/换手全合格的两只）一只都没上榜。

    只对**今日首板**（在涨停池里且 boards == 1）生效，并按野人体系里首板质量的
    三个硬指标给分（上限 first_board_bonus）：
      · 早封（首封时间 ≤ first_board_early，默认 10:00 前）   → 占 40%
      · 封成（break_count == 0，没炸过板）                    → 占 30%
      · 封单厚度（封单额 / 流通市值 ≥ first_board_fund_ratio） → 占 30%
    质量不够的首板拿不到加权 —— 加的是"量价配合"，不是"只要是首板就加分"。
    返回 (加分, 理由)。
    """
    if not zt_row or int(zt_row.get("boards") or 0) != 1:
        return 0.0, []
    try:
        max_b = float(a.get("first_board_bonus", 10) or 0) / 100.0
    except (TypeError, ValueError):
        max_b = 0.10
    if max_b <= 0:
        return 0.0, []
    try:
        early = int(a.get("first_board_early", 100000) or 100000)
        ratio_min = float(a.get("first_board_fund_ratio", 0.005) or 0)
    except (TypeError, ValueError):
        early, ratio_min = 100000, 0.005

    got: float = 0.0
    why: list[str] = []
    ft = int(zt_row.get("first_time") or 0)
    if ft and ft <= early:
        got += max_b * 0.4
        why.append(f"早封 {ft // 10000:02d}:{ft // 100 % 100:02d}")
    if not int(zt_row.get("break_count") or 0):
        got += max_b * 0.3
        why.append("封板未炸")
    mv = float(snap.get("float_mv") or 0)
    fund = float(zt_row.get("fund") or 0)
    if mv > 0 and ratio_min > 0 and fund / mv >= ratio_min:
        got += max_b * 0.3
        why.append(f"封单 {fund / mv * 100:.1f}% 流通")
    return got, why


def screen(cfg: dict, snaps: dict[str, dict], kl: dict[str, list[dict]],
           zt: list[dict], macro: dict, volume: dict, sentiment: dict,
           top_n: int = 20, progress=None, preopen: bool = False,
           gene_table: dict | None = None) -> list[dict]:
    """M4+M5 选股主流程：审美三要素打分 + 仓位建议。

    preopen：盘前/竞价/休市时段全市场换手、成交额均为 0（当日数据
    未形成），放开流动性门槛，名单按昨收状态作观察名单。
    gene_table：涨停基因表（core/gene.py 产出）。不传则不启用股性加分 ——
    保持老调法 screen(cfg, snaps, kl, zt, macro, volume, sentiment) 行为不变。
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
    # ★ 候选股 = 「今天还能出手的票」（用户 10-13 反馈）：
    #   已涨停且连板 ≥2 的票，体系对它们的定性一律是"不接力/不追"（退潮期直接给
    #   block，其余节点给「涨停不追」）—— 放在候选列表里只会被读成"推荐打 2 板/多板"，
    #   与"买首板"的口径自相矛盾。实测 10-13：3 只 3 连板（均不可接力）占了前三个
    #   候选位，而当天涨停池里 22 只首板（含价/市值/换手全合格的新力金融、亚邦股份）
    #   一只都没上榜 —— 排序只看分数，不看"能不能动手"。
    #   首板（boards == 1）不在此列：那是体系的核心打法（首板次日竞价）。
    try:
        max_boards = int(a.get("max_candidate_boards", 1))
    except (TypeError, ValueError):
        max_boards = 1

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
        if int(zt_map.get(code, {}).get("boards", 0) or 0) > max_boards:
            continue                      # 连板股不进候选（见上方 max_candidate_boards）
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
        i, ireasons = _imagination_score(snap, bars, zt_map, a)

        if need_bull and not gdetail.get("ma_aligned"):
            continue

        total = (g * wg + p * wp + i * wi) / wsum
        # ★ 可打性加权（方案 A，v1.32.5）：审美分只回答"这票好不好"，候选排序还要
        #   回答"今天能不能打"。只给今日首板、且按封板质量给（见 _first_board_bonus）。
        fb_bonus, fb_why = _first_board_bonus(snap, zt_map.get(code), a)
        if fb_bonus:
            total = min(1.0, total + fb_bonus)   # 封顶 100 分，不给溢出分数

        # ★ 区分度检验落地（v1.37.0）：把四关检验证明有效的因子接进打分。
        #   两个加权都走「原始分 × 权重 / 归一化分母」，与审美三要素同构，
        #   这样调权重（包括关成 0）不会改变分数的量纲。
        #   实测依据见 core/discrimination.py 与 check_signals.py。
        extra_num = 0.0
        extra_den = 0.0
        sq = 0.0
        ss = 0.0
        gscore = 0.0
        if a.get("use_seal_quality", True):
            sq, sq_why = _seal_quality_score(snap, zt_map.get(code), a)
            w_sq = _num(a.get("weight_seal_quality", 15), 15.0)
            if w_sq > 0:
                extra_num += sq * w_sq
                extra_den += w_sq
                if sq_why:
                    greasons = [f"封板质量 {sq * 100:.0f} 分"
                                f"（{'、'.join(sq_why)}）"] + greasons
        if a.get("use_small_size_bias", True):
            ss, ss_why = _small_size_score(snap, a)
            w_ss = _num(a.get("weight_small_size", 10), 10.0)
            if w_ss > 0:
                extra_num += ss * w_ss
                extra_den += w_ss
                if ss_why:
                    greasons = [f"小盘弹性（{'、'.join(ss_why)}）"] + greasons
        # ★ 涨停基因（v1.38.0，E11）：门槛式，见 core/gene.seal_score 上方注释。
        #   只在传了 gene_table 时启用；够到顶档才给分（不是线性——实测该因子
        #   在首板上的形状是门槛不是斜坡，G4 判"方向不一致"是对的）。
        if gene_table and a.get("use_gene_seal", True):
            w_g = _num(a.get("weight_gene_seal", 8), 8.0)
            # ★ 门槛与最小样本从配置读（M4_aesthetic.gene_top_seal / gene_min_first），
            #   不传即用 core/gene.py 的标定常量 —— 以前这两个键写了但没人读，
            #   改了也不生效（等于给用户一个假的旋钮）。
            gscore, gwhy = gene_mod.seal_score(
                gene_mod.of(code, gene_table),
                int(zt_map.get(code, {}).get("boards", 0) or 0),
                top_seal=_num(a.get("gene_top_seal", gene_mod.TOP_SEAL),
                              gene_mod.TOP_SEAL),
                min_first=int(_num(a.get("gene_min_first", gene_mod.MIN_FIRST),
                                   gene_mod.MIN_FIRST)),
                weight=w_g)
            if w_g > 0:
                extra_num += gscore * w_g
                extra_den += w_g
                if gwhy and gscore > 0:
                    greasons = [f"股性加成 +{gscore:.0f}（{gwhy}）"] + greasons
        if extra_den > 0:
            total = (total * wsum + extra_num) / (wsum + extra_den)

        reasons = greasons + preasons + ireasons
        if fb_why:
            reasons = [f"首板可打加权 +{fb_bonus * 100:.0f}"
                       f"（{'、'.join(fb_why)}）"] + reasons
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
                # 区分度检验落地的新增两项（v1.37.0）：前两项是检验证明有效的因子，
                # 详情页「为什么选它」会展示；没参与打分时这里给 None 而不是 0，
                # 免得前端把"没启用"画成"得了 0 分"。
                "seal_quality": (round(sq * 100, 1)
                                 if a.get("use_seal_quality", True) else None),
                "small_size": (round(ss * 100, 1)
                               if a.get("use_small_size_bias", True) else None),
                # 涨停基因（E11）：只在启用且该票真的吃到分时给数值。
                # 没吃到给 0（而不是 None）—— 与上面两项不同，这里"没吃到"
                # 本身就是结论（它没够到门槛），不是"没启用"。
                "gene_seal": (round(gscore, 1)
                              if (gene_table and a.get("use_gene_seal", True))
                              else None),
            },
            "graph_detail": gdetail,
            "reasons": reasons,
            "warnings": warns,
            "in_zt": code in zt_map,
            "zt_boards": zt_map.get(code, {}).get("boards", 0),
            "first_board_bonus": round(fb_bonus * 100, 1),   # 可打性加权（0=没加）
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
