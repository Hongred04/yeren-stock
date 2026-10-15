# -*- coding: utf-8 -*-
"""E11 大金融中长线（1~3 年持有）—— 独立于短线体系的一套卖出纪律。

--------------------------------------------------------------- 为什么单独做
用户 2026-10-15 原话：
    「还有持仓大金融属性的股票时不要用当前市场的判断来给出卖点，你去网上搜
      一下这类票一般都是持有1到3年的中长线短期市场的变化对它没有用，这类票
      要专门重新做一个策略出来」

这不是"再调一次参数"，而是**口径冲突**：现有 `trading.sell_signals` 是给
短线/波段仓设计的，其中 S1 退潮清仓是 critical（无条件清仓）。把它套在券商
持仓上，会出现「市场退潮 → 叫你清掉一只打算拿 3 年的票」这种自相矛盾的建议。
短线体系的每一个信号（涨停家数、跌停家数、炸板率、跌破5日线、断板节奏）衡量
的都是**几天的情绪**，而大金融的价格由**估值周期 + 利率环境 + 行业景气**驱动，
周期以年计。用日频情绪指标管年频仓位，等于噪声驱动。

所以本模块**不复用任何短线卖出信号**，四条卖出条件全部换成中长线口径：
    L1 估值见顶   —— 板块 PB/PE 历史分位到高位（核心锚）
    L2 基本面恶化 —— 个股 PE 转亏 / 行业亏损家数上升
    L3 目标达成   —— 持有到期 或 达到目标收益
    L4 极端止损   —— 宽幅（默认 -25%），只做灾难保护，且**必须**配合基本面复核

--------------------------------------------------------------- 与短线体系的边界
本模块**只负责卖出**，不产生买点。理由：用户说的是"给出卖点"，而大金融的
买入时机是另一个问题（周期位置、利率、配置比例）。擅自发明买点＝越界。

接入点：`core/portfolio.py` 的持仓体检。识别为大金融中长线的持仓走本模块，
**不调用** trading.sell_signals；其余持仓行为完全不变（零回归）。

--------------------------------------------------------------- 识别（两者结合）
用户选的是「标签 + 行业自动识别」结合：
    ① 持仓带 `longterm: true` 标签 → 直接认（用户的显式意愿最优先）；
    ② 否则看行业：行业名命中大金融词表 → 认（自动兜底）。
自动识别的口径与 `core/engine.py` 的 `_FOCUS_GROUPS["大金融"]` 保持一致，
不另造一套词表（口径分叉＝以后必然对不上）。

--------------------------------------------------------------- 诚实性
取不到估值时**不给估值类结论**，并如实说明"估值没取到"，绝不拿随机数当分位；
分位样本不足（< min_samples）时同样不出分位。做不到的就承认做不到。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from . import datasource as ds
from . import paths

# ---------------------------------------------------------------- 常量

# 大金融行业词表 —— 与 core/engine.py _FOCUS_GROUPS["大金融"] 同口径。
# 注意东财行业名带 Ⅱ/Ⅲ 后缀（"证券Ⅱ"），所以用**子串**匹配而非全等。
_FIN_KW = ("银行", "证券", "保险", "多元金融", "金融", "信托", "券商")

# 股票**名称**兜底：A 股金融股的名字几乎都以这些词收尾（中信证券、招商银行、
# 中国平安不在此列但有别名），所以名称匹配要求"以该词结尾"。
# 为什么必须收尾而不能用子串（实测教训 2026-10-15）：子串匹配会让
# **「保险柜制造」命中「保险」** —— 一家做保险柜的公司会被当成大金融，
# 拿 1~3 年中长线口径管它（不给短线卖点），这是实实在在的误伤。
# 行业名可以放宽（东财行业名形如"证券Ⅱ"），名称必须收紧。
_FIN_NAME_SUF = ("银行", "证券", "保险", "信托", "券商", "金融")

# 大金融板块（用于板块估值分位）—— 归一后的名字，datasource 内部会映射回
# 报表真名（证券→证券Ⅱ 等，见 _VALUE_BOARD_ALIAS）。
_FIN_BOARDS = ("证券", "银行", "保险", "多元金融")

HISTORY_PATH = paths.data_path("valuation_history.json")
_HISTORY_MAX_DAYS = 900          # 归档保留多少个交易日


# ---------------------------------------------------------------- 配置

_DEFAULTS = {
    "enabled": True,
    # 持有期（交易日）。1~3 年中长线：默认按 3 年 = 750 交易日算"到期复核"
    "hold_days": 750,
    "hold_days_min": 250,             # 满 1 年才开始提示"可考虑兑现"
    # 目标收益：达到即提示分批兑现（中长线不追求卖在最高点）
    "target_return": 0.60,            # +60%
    "target_return_review": 0.30,     # +30% 开始进入"关注区"
    # 估值见顶阈值（历史分位 %）—— 分位比绝对值可靠，因为不同板块的
    # PB 中枢差很远（银行 0.5~1.0，券商 1.3~2.4），用一个绝对 PB 卡所有板块
    # 必然错。默认：分位 ≥ 85% 提示减仓、≥ 95% 提示清仓。
    "pb_pct_warn": 85.0,
    "pb_pct_clear": 95.0,
    "pe_pct_warn": 85.0,
    "pe_pct_clear": 95.0,
    # 极端止损：中长线的宽幅保护，**不是**短线止损。
    # -25% 的由来：大金融（尤其券商）年度级别的正常回撤可以到 -20%，
    # 用短线 -8% 会在正常波动里被反复洗出去；但真跌 -25%+ 说明逻辑可能坏了，
    # 这时必须**回去看基本面**再决定，所以止损信号强制带"复核"语义。
    "stop_loss": -0.25,
    # 分位样本不足就不给分位（n 太小 → 分位是噪声）
    "min_samples": 60,
    # 算分位的**回看窗口**（交易日）。750 ≈ 3 年，与"持有 1~3 年"同尺度。
    # 为什么不用全史：实测「多元金融」PB 在 2023-02 是 9.4、2024 年降到 1.1
    # —— 那是真实重估，但把两个估值制度放进同一样本，算出的分位没有决策
    # 意义。窗口按持有尺度取，分位问的才是"相对我这段持有期，现在贵不贵"。
    "pct_window": 750,
    # ★ 长窗口（交易日）：≈10 年，用来回答**另一个**问题 —— "相对长历史，
    # 现在的绝对水平贵不贵"。
    # 为什么要两个窗口（调研实证，2026-10-15）：同一时点，
    #   · 银行 PB 0.763 在**近 3 年**窗口是 97% 分位（2023 中位 0.577 →
    #     2025 中位 0.718，是实打实的重估，所以确实在近三年区间顶部）；
    #   · 但同一个 0.763 在**长历史**里只有约 44% 分位（2018 前银行 PB 长期
    #     在 1.0 以上）。
    # 两个数都对，问的不是同一件事：短窗口答"这轮重估走完了没"，长窗口答
    # "绝对估值贵不贵"。**只报一个就会误导** —— 只报短窗口会把"刚从 0.52
    # 涨到 0.76"说成"估值见顶"，只报长窗口会漏掉这轮重估已经走完。
    "pct_window_long": 2400,
    # 银行这类票：**PE 不作为估值锚**（净息差下行 → 盈利下滑 → PE 被动抬高，
    # 看起来"变贵"其实只是盈利缩了）。实测银行 PE 分位 97% 而 PB 分位 44%，
    # 就是这个机制造成的。命中这些板块时估值信号**只看 PB**。
    "pe_unreliable_boards": ["银行", "保险"],
    # 板块回撤预警：从持有期高点回撤超过此值提示（中长线的"趋势破坏"）
    "drawdown_warn": -0.35,
}


def defaults() -> dict:
    return dict(_DEFAULTS)


def cfg_of(cfg: dict | None) -> dict:
    """取 E11 配置（缺失/被关时返回默认值副本）。"""
    c = dict(_DEFAULTS)
    got = ((cfg or {}).get("E11_longterm") or {})
    if isinstance(got, dict):
        c.update({k: v for k, v in got.items() if v is not None})
    return c


# ---------------------------------------------------------------- 归档

def _read_history() -> dict:
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_history(obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, HISTORY_PATH)
    except OSError:
        pass


def archive_valuation(board_series: dict, date: str = "") -> dict:
    """把板块估值序列**并入**本地归档（不覆盖已有日期的值）。

    为什么要本地归档：把"历史分位"变成**我们自己攒的**而不是每次现拉。
    现拉虽然可行，但一旦接口某天挂了/改口径，分位就没了；归档后哪怕上游
    断供，已有历史仍然能算分位 —— 这是中长线策略最该有的韧性。
    """
    if not board_series:
        return _read_history()
    hist = _read_history()
    for name, series in (board_series or {}).items():
        slot = hist.setdefault(name, {})
        if not isinstance(slot, dict):
            slot = {}
            hist[name] = slot
        for row in (series or []):
            d = str(row.get("date") or "")[:10]
            if not d:
                continue
            # 已有该日期的记录就不覆盖（第一次记下的最接近当日真实值）
            slot.setdefault(d, {"pe": row.get("pe"), "pe_ttm": row.get("pe_ttm"),
                                "pb": row.get("pb")})
        # 只留最近 N 个交易日，防止无限增长
        if len(slot) > _HISTORY_MAX_DAYS:
            keep = sorted(slot.keys(), reverse=True)[:_HISTORY_MAX_DAYS]
            hist[name] = {k: slot[k] for k in keep}
    hist["_ts"] = int(time.time())
    if date:
        hist["_date"] = date
    _write_history(hist)
    return hist


def _series_from_history(hist: dict, name: str, field: str = "pb") -> list[dict]:
    """本地归档 → 新→旧的序列（供 valuation_percentile 用）。"""
    slot = (hist or {}).get(name) or {}
    rows = []
    for d in sorted(slot.keys(), reverse=True):
        v = (slot.get(d) or {}).get(field)
        if isinstance(v, (int, float)) and v > 0:
            rows.append({"date": d, field: v})
    return rows


# ---------------------------------------------------------------- 识别

def is_financial(industry: str, name: str = "") -> bool:
    """行业（或名称）是否属于大金融。

    两套口径，因为两个字段的可信度不同：
      · 行业名（东财）—— 子串匹配即可，形如"证券Ⅱ"/"银行"，不会误伤；
      · 股票名 —— **必须收尾匹配**，否则"保险柜制造"会被当成保险股。
    """
    ind = str(industry or "").strip()
    if ind and any(k in ind for k in _FIN_KW):
        return True
    nm = str(name or "").strip()
    if nm and any(nm.endswith(s) for s in _FIN_NAME_SUF):
        return True
    # 习惯叫法：平安/人寿/太保 等不带"保险"二字的老牌险企
    if nm and any(a in nm for a in ("人寿", "人保", "太保", "平安", "财险")):
        return True
    return False


def is_longterm(holding: dict, industry: str = "") -> tuple[bool, str]:
    """该持仓是否按「大金融中长线」处理 → (是否, 理由)。

    ① 显式标签 `longterm: true`（或 `style: "longterm"`）→ 认，理由标明是标签；
    ② 否则行业命中大金融词表 → 认，理由标明是行业自动识别。
    """
    h = holding or {}
    for k in ("longterm", "is_longterm"):
        if h.get(k) is True:
            return True, "持仓已标记为「中长线」"
    if str(h.get("style") or "").strip().lower() in ("longterm", "long-term", "长线"):
        return True, "持仓 style 标为 longterm"
    if is_financial(industry, h.get("name")):
        return True, f"行业属大金融（{industry or '按名称识别'}），按中长线口径管理"
    return False, ""


# ---------------------------------------------------------------- 估值快照

def valuation_view(holdings: list[dict], cfg: dict | None = None,
                   use_archive: bool = True) -> dict:
    """取大金融持仓 + 板块的估值，并算历史分位。

    返回：
      {
        "stocks": {code: {pe_ttm, pe_lar, pb, industry, mv, date}},
        "boards": {板块名: {"pb":{value,pct,n,min,max,median},
                            "pe_ttm":{...}, "date":..}},
        "sources": [...],     # 实际供数的路，如实报
        "note": "...",
      }
    取不到的部分**留空并写明**，绝不编。
    """
    c = cfg_of(cfg)
    codes = [str(h.get("code") or "") for h in (holdings or [])]
    codes = [x for x in codes if x]

    # ① 个股估值（datacenter-web，活着的路）
    stocks = {}
    try:
        stocks = ds.fetch_valuation(codes) or {}
    except Exception:
        stocks = {}

    # ② 板块估值序列：优先本地归档（有韧性），不足则现拉并并入归档
    boards: dict[str, dict] = {}
    hist = _read_history() if use_archive else {}
    need_fetch = []
    for b in _FIN_BOARDS:
        if len(_series_from_history(hist, b, "pb")) >= int(c["min_samples"]):
            continue
        need_fetch.append(b)
    fetched = {}
    if need_fetch:
        try:
            fetched = ds.fetch_board_valuation(list(need_fetch),
                                               days=_HISTORY_MAX_DAYS) or {}
        except Exception:
            fetched = {}
        if fetched:
            try:
                archive_valuation(fetched)
                hist = _read_history()
            except Exception:
                pass

    for b in _FIN_BOARDS:
        ser = _series_from_history(hist, b, "pb")
        if not ser and fetched.get(b):
            ser = [{"date": r.get("date"), "pb": r.get("pb")}
                   for r in fetched[b] if r.get("pb")]
        peser = _series_from_history(hist, b, "pe_ttm")
        if not peser and fetched.get(b):
            peser = [{"date": r.get("date"), "pe_ttm": r.get("pe_ttm")}
                     for r in fetched[b] if r.get("pe_ttm")]
        if not ser and not peser:
            continue
        win, winl = int(c["pct_window"]), int(c["pct_window_long"])
        # 两个窗口都算：短窗口（≈持有尺度）与长窗口（≈长历史）。
        # 只报一个会误导，见 _DEFAULTS 里 pct_window_long 的说明。
        pb_short = ds.valuation_percentile(ser, "pb", window=win)
        pb_long = ds.valuation_percentile(ser, "pb", window=winl)
        pe_short = ds.valuation_percentile(peser, "pe_ttm", window=win)
        if pb_long.get("n", 0) <= pb_short.get("n", 0):
            pb_long = {"value": None, "n": 0, "pct": None,
                       "min": None, "max": None, "median": None}
        boards[b] = {
            "pb": pb_short,
            "pb_long": pb_long,
            "pe_ttm": pe_short,
            "date": (ser[0]["date"] if ser else (peser[0]["date"] if peser else "")),
        }

    srcs = []
    if stocks:
        srcs.append("datacenter-web RPT_VALUEANALYSIS_DET（个股 PE_TTM/PB_MRQ）")
    # 如实描述板块估值的来源。**不要说成"上游没取到"** —— 归档本来就是
    # 第一优先级（有韧性：上游断供也能算分位），走归档是正常路径不是降级。
    if fetched:
        srcs.append("datacenter-web RPT_VALUEINDUSTRY_DET（板块 PE/PB 日度序列）"
                    "→ 已并入本地归档")
    elif boards:
        srcs.append(f"本地归档 valuation_history.json（{HISTORY_PATH}）")

    note = ""
    if not stocks and not boards:
        note = "估值接口本轮没取到 —— 不给估值类结论（不猜）。"
    else:
        miss = [b for b in _FIN_BOARDS if b not in boards]
        if miss:
            note = "以下板块估值没取到，不给结论：" + "/".join(miss)

    return {"stocks": stocks, "boards": boards, "sources": srcs,
            "fetched": bool(fetched), "note": note}


# ---------------------------------------------------------------- 卖出判定

def _r3(v) -> float | None:
    return round(v, 3) if isinstance(v, (int, float)) else None


def evaluate_holding(holding: dict, val: dict, cfg: dict | None = None,
                     industry: str = "") -> dict:
    """单只大金融持仓的**中长线**体检 → {verdict, level, plain, signals, ...}。

    契约与 core/portfolio.py 的同名字段一致（verdict/level/plain/signals），
    这样前端持仓卡不用改就能显示 —— 但 signals 里是 L 系列中长线信号，
    **不含任何短线信号**。
    """
    c = cfg_of(cfg)
    h = holding or {}
    code = str(h.get("code") or "")
    bare = code[2:] if code[:2].lower() in ("sh", "sz", "bj") else code
    cost = h.get("cost")
    price = h.get("price")
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    price = float(price) if isinstance(price, (int, float)) and price > 0 else None
    pnl = ((price - cost) / cost) if (cost and price) else None

    st = (val.get("stocks") or {}).get(bare) or {}
    ind = industry or st.get("industry") or ""
    # 该股所属板块在本策略里的键（归一后）
    bkey = ""
    for b in _FIN_BOARDS:
        if b and b in str(ind):
            bkey = b
            break

    sigs: list[dict] = []

    # ---- L1 估值见顶（核心锚）----
    bv = (val.get("boards") or {}).get(bkey) if bkey else None
    if bv:
        # ★ 银行/保险这类票**只看 PB**：净息差下行 → 盈利下滑 → PE 被动抬高，
        #   用 PE 判贵贱会把"盈利缩了"读成"估值贵了"。实测同一时点银行 PE
        #   分位 97%、PB 分位 44%，就是这个机制。
        pe_ok = bkey not in tuple(c.get("pe_unreliable_boards") or ())
        fields = [("pb", "PB")] + ([("pe_ttm", "PE(TTM)")] if pe_ok else [])
        for fld, label in fields:
            p = (bv.get(fld) or {})
            pct, cur = p.get("pct"), p.get("value")
            if pct is None or cur is None:
                continue
            # 长窗口分位（若可用）：说明"绝对水平在长历史里的位置"，
            # 避免只拿短窗口把一次重估说成见顶。
            # ★ 措辞必须跟着数据走：长窗口分位**明显更低**时才是"其实没那么贵"
            #   的反证（用"但…只有"）；两者接近时是**印证**，说成"但…只有"
            #   会读成自相矛盾（实测银行 96.9% vs 97.4%，曾被写成"但…只有
            #   97.4%"，逻辑不通）。
            lp = (bv.get("pb_long") or {}) if fld == "pb" else {}
            ltxt = ""
            if lp.get("pct") is not None and lp.get("n"):
                lpc = float(lp["pct"])
                if lpc <= pct - 15:
                    ltxt = (f"；长历史（近 {lp['n']} 日）只有 {lpc}% 分位"
                            "—— 长周期看还没到极值")
                elif lpc >= pct + 15:
                    ltxt = (f"；长历史（近 {lp['n']} 日）更高，达 {lpc}% 分位"
                            "—— 长短窗口都指向高位")
                else:
                    ltxt = (f"；长历史（近 {lp['n']} 日）{lpc}% 分位，与短窗口一致")
            hi = float(c["pb_pct_clear"] if fld == "pb" else c["pe_pct_clear"])
            warn = float(c["pb_pct_warn"] if fld == "pb" else c["pe_pct_warn"])
            if pct >= hi:
                sigs.append({
                    "type": "L1", "name": f"{bkey}板块{label}历史高位",
                    "priority": "A", "urgency": "high",
                    "trigger": (f"{bkey}板块{label} {_r3(cur)}，处于近 {p['n']} 个"
                                f"交易日的 {pct}% 分位（区间 {_r3(p['min'])}~"
                                f"{_r3(p['max'])}）{ltxt}"),
                    "action": "估值已到历史高位，分批兑现 —— 中长线不赚最后一波",
                    "note": "中长线卖出看估值，不看盘面情绪",
                })
                break
            if pct >= warn:
                sigs.append({
                    "type": "L1", "name": f"{bkey}板块{label}偏高",
                    "priority": "B", "urgency": "medium",
                    "trigger": (f"{bkey}板块{label} {_r3(cur)}，处于近 {p['n']} 个"
                                f"交易日 {pct}% 分位{ltxt}"),
                    "action": "进入高估区，停止加仓；再涨到 95% 分位以上分批减",
                    "note": "中长线的减仓是渐进的，不是一次清",
                })
                break

    # ---- L2 基本面恶化 ----
    pe_ttm = st.get("pe_ttm")
    if pe_ttm is None and st:
        # 取到个股但 PE_TTM 为空/<=0 → 多半是亏损（接口只回正值）
        sigs.append({
            "type": "L2", "name": "个股亏损（PE 为负/缺失）",
            "priority": "A", "urgency": "high",
            "trigger": f"{st.get('industry') or ind} · 接口未回正的 PE_TTM（{st.get('date') or ''}）",
            "action": "基本面恶化信号 —— 核对最新财报（营收/净利是否连续下滑）后再决定",
            "note": "中长线的止损必须回到基本面，不看技术形态",
        })
    elif isinstance(pe_ttm, (int, float)) and pe_ttm > 0 and bv:
        bp = (bv.get("pe_ttm") or {}).get("value")
        if isinstance(bp, (int, float)) and bp > 0 and pe_ttm > bp * 2.0:
            sigs.append({
                "type": "L2", "name": "个股估值显著高于板块",
                "priority": "C", "urgency": "low",
                "trigger": (f"个股 PE(TTM) {round(pe_ttm,2)} 是 {bkey}板块 "
                            f"{round(bp,2)} 的 {round(pe_ttm/bp,1)} 倍"),
                "action": "同板块里偏贵 —— 若同时行业景气走弱，优先减这一只",
                "note": "比较口径：个股 vs 板块 PE(TTM)，来自同一天",
            })

    # ---- L3 目标达成 / 持有到期 ----
    if pnl is not None:
        if pnl >= float(c["target_return"]):
            sigs.append({
                "type": "L3", "name": "达到目标收益",
                "priority": "A", "urgency": "high",
                "trigger": f"浮盈 {round(pnl*100,1)}% ≥ 目标 {round(float(c['target_return'])*100)}%",
                "action": "分批兑现（中长线目标达成不建议一次性清仓）",
                "note": "中长线看的是年化，不是这一波涨了多少",
            })
        elif pnl >= float(c["target_return_review"]):
            sigs.append({
                "type": "L3", "name": "进入收益关注区",
                "priority": "B", "urgency": "medium",
                "trigger": f"浮盈 {round(pnl*100,1)}% ≥ 关注线 {round(float(c['target_return_review'])*100)}%",
                "action": "可继续持有；若估值同时到高位，先落袋一部分",
                "note": "收益与估值同时到高位才是较强的卖出理由",
            })
    hd = _hold_days(h)
    if hd is not None:
        if hd >= int(c["hold_days"]):
            sigs.append({
                "type": "L3", "name": "持有到期（复核）",
                "priority": "B", "urgency": "medium",
                "trigger": f"已持有 {hd} 个交易日 ≥ 计划 {int(c['hold_days'])}",
                "action": "到复核期：重新判断周期位置，逻辑还在就继续拿，不为了'到期'而卖",
                "note": "到期是复核触发点，不是强制卖出",
            })
        elif hd >= int(c["hold_days_min"]):
            sigs.append({
                "type": "L3", "name": "已过最短持有期",
                "priority": "C", "urgency": "low",
                "trigger": f"已持有 {hd} 个交易日 ≥ {int(c['hold_days_min'])}（约 1 年）",
                "action": "可以开始用估值/基本面决定去留，不必再被短线波动干扰",
                "note": "",
            })

    # ---- L4 极端止损（宽幅）----
    if pnl is not None and pnl <= float(c["stop_loss"]):
        sigs.append({
            "type": "L4", "name": "极端止损（宽幅·需复核）",
            "priority": "A", "urgency": "critical",
            "trigger": (f"浮亏 {round(pnl*100,1)}% ≤ {round(float(c['stop_loss'])*100)}%"
                        "（中长线宽幅止损线）"),
            "action": ("先复核基本面/板块逻辑：逻辑没坏可继续持有（这是中长线的正常回撤），"
                       "逻辑坏了才走 —— 不是跌到就无脑割"),
            "note": "宽幅止损只做灾难保护；中长线不设短线止损，避免被正常波动洗出去",
        })

    # 板块级回撤**不再**挂到每只票上。
    # 原来的做法：每只大金融持仓都收到一条「板块自持有期高点回撤 -41%」——
    # 实测三只券商各收到一条一模一样的信号。问题在于这个数是**板块自己的**
    # 统计量（PB 序列高点 vs 当前），与"你什么时候买的"毫无关系，逐票重复
    # 展示只会把真正的个股信号淹掉。改为组合级提示，见 evaluate() 的
    # board_notes。

    verdict, level = _verdict_of(sigs)
    return {"code": code, "name": h.get("name") or "", "industry": ind,
            "board": bkey, "pnl_pct": pnl, "hold_days": hd,
            "valuation": {"stock": st, "board": bv} if (st or bv) else {},
            "signals": sigs, "verdict": verdict, "level": level,
            "plain": _plain_of(level, sigs, bkey)}


def _verdict_of(sigs: list[dict]) -> tuple[str, str]:
    """信号 → 档位（档位名与短线体系一致，前端可直接显示）。"""
    if any(s.get("urgency") == "critical" for s in sigs):
        return "清仓离场", "critical"
    if any(s.get("urgency") == "high" for s in sigs):
        return "减仓", "high"
    if sigs:
        return "观察/减半", "medium"
    return "持有", "hold"


_PLAIN = {
    "critical": "先复核基本面再决定 —— 中长线的宽幅止损不是无脑割",
    "high": "中长线卖出条件触发，分批兑现",
    "medium": "继续持有，按上面的条件盯着",
    "hold": "拿着就行 —— 中长线看的是估值和基本面，短期波动不用管",
}


def _plain_of(level: str, sigs: list[dict], bkey: str) -> str:
    base = _PLAIN.get(level, "")
    if level == "hold":
        if bkey:
            return f"{base}（{bkey}持仓按 1~3 年中长线管理）"
        return base
    return base


def _hold_days(h: dict) -> int | None:
    """持仓天数（**自然周折算的交易日**，不是自然日）。

    买入日期有则用；没有就返回 None（不猜一个天数出来）。
    """
    d = str(h.get("date") or "").strip()
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            t0 = datetime.strptime(d, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    days = (datetime.now() - t0).days
    if days < 0:
        return 0
    # 自然日 → 交易日：一周 7 天里有 5 个交易日
    return int(days * 5 / 7)


def _board_drawdown(val: dict, bkey: str) -> float | None:
    """板块自序列高点回撤（用 PB 序列近似，新→旧）。"""
    if not bkey:
        return None
    bv = (val.get("boards") or {}).get(bkey) or {}
    p = (bv.get("pb") or {})
    cur, mx = p.get("value"), p.get("max")
    if not (isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0):
        return None
    return cur / mx - 1.0


# ---------------------------------------------------------------- 组合入口

def board_notes(val: dict, cfg: dict | None = None) -> list[dict]:
    """板块估值状态的人话摘要（组合级，**不逐票重复**）。

    实测过的问题：把板块回撤挂到每只持仓上，三只券商各收到一条一模一样的
    "板块回撤 -41%"——那是板块自己的历史分位统计，与买入时点无关，逐票展示
    纯属噪声。板块的事就在板块这一层说一次。
    """
    c = cfg_of(cfg)
    out = []
    for b in _FIN_BOARDS:
        bv = (val.get("boards") or {}).get(b)
        if not bv:
            continue
        pb = bv.get("pb") or {}
        pe = bv.get("pe_ttm") or {}
        if pb.get("pct") is None and pe.get("pct") is None:
            continue
        dd = _board_drawdown(val, b)
        bits = []
        if pb.get("value") is not None:
            s = f"PB {_r3(pb['value'])}"
            if pb.get("pct") is not None:
                s += (f"（近 {pb['n']} 日 {pb['pct']}% 分位，"
                      f"区间 {_r3(pb['min'])}~{_r3(pb['max'])}）")
            else:
                s += "（样本不足，不给分位）"
            pl = bv.get("pb_long") or {}
            if pl.get("pct") is not None:
                s += f"；长历史 {pl['pct']}% 分位（近 {pl['n']} 日）"
            bits.append(s)
        pe_ok = b not in tuple(c.get("pe_unreliable_boards") or ())
        if pe.get("value") is not None:
            s = f"PE(TTM) {_r3(pe['value'])}"
            if pe.get("pct") is not None and pe_ok:
                s += f"（{pe['pct']}% 分位）"
            if not pe_ok:
                s += "（该板块 PE 不作估值锚：净息差/盈利波动会使 PE 失真）"
            bits.append(s)
        if dd is not None:
            bits.append(f"自区间高点回撤 {round(dd*100,1)}%")
        pct = pb.get("pct")
        if pct is None:
            tone, what = "gry", "样本不足，暂不给估值结论"
        elif pct >= float(c["pb_pct_clear"]):
            tone, what = "red", "估值到历史高位 —— 中长线可分批兑现"
        elif pct >= float(c["pb_pct_warn"]):
            tone, what = "org", "进入高估区 —— 不加仓，接近 95% 分位再减"
        elif pct <= 20:
            tone, what = "grn", "估值在历史低位区间"
        else:
            tone, what = "gry", "估值处于历史中位区间"
        out.append({"board": b, "tone": tone, "what": what,
                    "detail": " · ".join(bits),
                    "date": (bv.get("date") or ""), "pct": pct})
    return out


def evaluate(holdings: list[dict], val: dict | None = None,
             cfg: dict | None = None) -> dict:
    """对所有持仓跑中长线判定 → 报告块。

    返回 {"items": {code: 判定}, "boards": {...}, "sources": [...], "note": ...}
    """
    val = val if val is not None else valuation_view(holdings, cfg)
    out: dict[str, dict] = {}
    for h in (holdings or []):
        code = str(h.get("code") or "")
        if not code:
            continue
        bare = code[2:] if code[:2].lower() in ("sh", "sz", "bj") else code
        st = (val.get("stocks") or {}).get(bare) or {}
        lt, why = is_longterm(h, st.get("industry") or "")
        if not lt:
            continue
        r = evaluate_holding(h, val, cfg, industry=st.get("industry") or "")
        r["reason"] = why
        out[code] = r
    return {"items": out, "boards": val.get("boards") or {},
            "board_notes": board_notes(val, cfg),
            "sources": val.get("sources") or [], "note": val.get("note") or ""}
