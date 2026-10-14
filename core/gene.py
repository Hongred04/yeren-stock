# -*- coding: utf-8 -*-
"""涨停基因（E11）—— 个股历史股性统计。

回答「这只票以前涨停之后，第二天还涨不涨得住」。

------------------------------------------------------------------ 为什么是这三个
参考开源项目（limitupsniper 等）的「涨停基因五项因子」里，本项目**只能算两项**：

  可算 · 涨停频率   freq —— 该票在已过交易日里涨停过几天的比例
  可算 · 首板封板率 seal —— 该票历史上「首板次日再涨停」的比例
  可算 · 连板惯性   cons —— 该票历史上「连板次日再涨停」的比例

★ 明确不做的三项，以及为什么（不是懒，是本项目数据里真的没有）：
  归档 data/pool_history/*.json 的字段是
  code/name/price/pct/amount/float_mv/turnover/fund/first_time/last_time/
  break_count/boards/sector/stat/reason/hot_rank/hot_tag ——
  **没有 open / high / low / close(前收) / pre_close**。
  K 线缓存 cache/kline/*.json 虽有 OHLC 但只有 70 根、且滞后一个交易日，
  撑不起「250 日」的窗口。所以：
    ✗ 涨停次日溢价>5% 比例 —— 需要次日最高价，没有
    ✗ 次日开盘平均溢价     —— 需要 open 与前收，没有
    ✗ 首板次日红盘率       —— 需要次日收盘涨跌幅，且对「次日没涨停」的票也没有价格
  这三项若硬算，只能拿归档当天池内的 pct 冒充，那是把「涨停当天的涨幅」
  当成「次日溢价」—— 一个必然是 +10% 的假数字。宁缺勿编。

------------------------------------------------------------------ 口径（防未来函数）
对交易日 d 的每条涨停记录，因子只用 **d 之前**的归档记录累积，
不使用 d 当天及以后的信息（当天的涨停要等收盘才知道，拿它算当天因子就是未来函数）。
累积量：某票在 d 之前涨停过几天、其中「首板次日再涨停」几次、「连板次日再涨停」几次。

★ 「次日再涨停」的判据：该票出现在**次日归档池**里。
  前提已验证（243 天归档实测）：
    · 池子规模 min 27 / max 178 / 中位 64，无一天 <10 只 ⇒ 「次日在池」是可靠信号
    · 连板自洽性 3159 一致 / 1 不一致（0.0%）⇒ 归档本身可信
  仍然加了 min_pool 守卫：某日池子小于 min_pool 时，该日的「缺席」不作数。

------------------------------------------------------------------ 收缩
用 **Wilson 95% 下界**（core/indicators 之外的独立实现，因为这里要的是下界不是比例）：
  「1 次 1 中」的原始比例是 100%，所有小样本票会一起顶到满分 —— 那正是 G1 塌缩。
  Wilson 下界对 n 小自动向 0 收，n=1 命中时只给 ~0.21，n=20 命中 10 次给 ~0.27。
实测（243 天 / 16542 条样本，基线次日再涨停率 19.1%）：
  freq  档 15.6 / 19.8 / 20.3 / 24.2%   lift 1.56x  ρ=+1.00  单调
  seal  0组 15.3 → 18.6 / 22.8 / 24.5 / 29.0%   +9.9pp 单调
  cons  0组 16.4 → 20.0 / 21.2 / 33.1 / 32.2%   +13.1pp 单调
  ★ 注意 seal/cons 有 54.7% / 73.5% 的样本值为 0（该票还没有历史），
    分位数分箱会把这些 0 挤成空档、看起来像「切不开」——
    那 0 本身是有信息的（0组 −2.7~−3.8pp，比基线差），所以按「有历史」分开看才对。
  ★ 历史样本量 n 本身**不是**单调因子（n=8 时反而 −3.4pp），
    有信号的是比例（Wilson 下界），不是次数。这条钉在 tests 里。
"""
from __future__ import annotations

import math
from collections import defaultdict

# 算「次日再涨停」时，次日池子至少要有这么多只才认可「缺席 = 没涨停」
MIN_POOL = 10


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 置信区间下界。n<=0 返回 0。"""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    r = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z * z / (4.0 * n * n)))
    return max(0.0, (c - r) / d)


def build(history: dict, dates: list, pools: dict,
          min_pool: int = MIN_POOL, snapshots: dict | None = None) -> dict:
    """逐日累积出每只票的股性。**只依赖传入的归档，不碰网络。**

    ★ 未来函数防线（这条是刻意的，改之前先读）：
      本函数同时产出两种东西，**用途完全不同**，不能混用：

      1. 返回值 table —— 截至**最后一个交易日**的全量股性表。
         实盘选股时用它（今天要判的票，本来就该用截至昨天的全部历史）。
      2. snapshots[date] —— 截至**该日之前**的股性快照。
         回测 / 区分度检验**必须**用它，不能用返回值。

      ★ 实测教训：拿返回值去评历史样本，freq 的 Q4 会从 24.2% 虚高到 29.5%、
        seal 的「无历史组」会从 15.3% 掉到 1.3% —— 那是拿 2026 年的历史
        去预测 2025 年的样本，纯未来函数。回测一律走 snapshots。

    Args:
        history: 预留参数（当前未用，保留给将来只传增量的场景）
        dates:   已排序的交易日列表
        pools:   {date: [row, ...]}
        snapshots: 传 dict 则填充为 {date: {code: 股性}}（截至该日之前的累积）
    Returns:
        {code: {"freq","seal","cons","n_zt","n_first","n_first_hit",
                "n_cons","n_cons_hit","last_date"}}
    """
    zt_days = defaultdict(int)      # 涨停过几天
    first_n = defaultdict(int)      # 首板出现几次
    first_hit = defaultdict(int)    # 首板次日再涨停几次
    cons_n = defaultdict(int)       # 连板出现几次
    cons_hit = defaultdict(int)     # 连板次日再涨停几次
    last_seen = {}

    def _snap(elapsed_days, date):
        """把当前累积量整理成一份股性快照。"""
        out = {}
        for c in zt_days:
            out[c] = {
                "freq": zt_days[c] / max(elapsed_days, 1),
                "seal": wilson_lower(first_hit[c], first_n[c]),
                "cons": wilson_lower(cons_hit[c], cons_n[c]),
                "n_zt": zt_days[c],
                "n_first": first_n[c],
                "n_first_hit": first_hit[c],
                "n_cons": cons_n[c],
                "n_cons_hit": cons_hit[c],
                "last_date": last_seen.get(c, ""),
            }
        return out

    done = 0
    for i in range(len(dates) - 1):
        d, nd = dates[i], dates[i + 1]
        cur, nxt_rows = pools.get(d) or [], pools.get(nd) or []
        if len(cur) < min_pool:
            continue
        # ★ 快照必须在使用当天记录**之前**生成 —— 排在这个位置就是防未来函数
        if snapshots is not None:
            snapshots[d] = _snap(done, d)
        # 次日池子太小 ⇒ 这天的「缺席」不可信，仍累积次数但不计命中
        trust_next = len(nxt_rows) >= min_pool
        nxt = {r.get('code') for r in nxt_rows} if trust_next else None
        for r in cur:
            c = r.get('code')
            if not c:
                continue
            b = r.get('boards') or 1
            hit = 1 if (nxt is not None and c in nxt) else 0
            if b == 1:
                first_n[c] += 1
                first_hit[c] += hit
            else:
                cons_n[c] += 1
                cons_hit[c] += hit
            zt_days[c] += 1
            last_seen[c] = d
        done += 1

    if snapshots is not None and dates:
        snapshots[dates[-1]] = _snap(done, dates[-1])
    return _snap(done, dates[-1] if dates else "")


def of(code: str, table: dict) -> dict:
    """取某只票的股性；没有历史时返回全 0 结构（而不是 None）。

    返回 0 而不是 None 的理由：调用方（评分、前端）不需要到处判空；
    「没有历史」在这次检验里本身就是 −2.7~−3.8pp 的负信号，
    用 0 表示既简单又方向正确。
    """
    t = (table or {}).get(code)
    if not t:
        return {"freq": 0.0, "seal": 0.0, "cons": 0.0, "n_zt": 0,
                "n_first": 0, "n_first_hit": 0, "n_cons": 0,
                "n_cons_hit": 0, "last_date": "", "known": False}
    d = dict(t)
    d["known"] = True
    return d


def summary(g: dict, name: str = "") -> str:
    """一句人话，给前端与选股理由用。没有历史时如实说没有。"""
    if not g or not g.get("known"):
        return "这只票过去一年没有涨停记录（新股或长期没动过）"
    parts = []
    if g.get("n_first"):
        parts.append(f"首板 {g['n_first']} 次、次日再涨停 "
                     f"{g['n_first_hit']} 次")
    if g.get("n_cons"):
        parts.append(f"连板 {g['n_cons']} 次、次日再涨停 "
                     f"{g['n_cons_hit']} 次")
    tail = ("；".join(parts) + f"（涨停频率 {g['freq']*100:.1f}%）") if parts \
        else f"涨停 {g.get('n_zt', 0)} 次"
    return tail


# ---------------------------------------------------------------- 评分接入

# ★ 候选口径决定了一切：cfg.M4_aesthetic.max_candidate_boards 默认 = 1，
#   即**只有首板票会进候选**。所以「对首板有没有用」才是唯一要紧的问题，
#   对 2板/3板 的效应再强也用不上（那些票根本进不了候选）。
#
# ★ 首板上的实测形状（243 天归档、6340 条「首板 && n_first≥2」样本，基线 14.8%）：
#
#   它不是单调梯度，是**顶档门槛**：
#       G1–G4 判「方向不一致、落在噪音区间」(ρ=+0.6 < 0.8) —— 各档
#       14.1 / 15.6 / 14.7 / 14.6 / 19.4% 确实不单调，只有顶档跳出来。
#       G4 判"不单调"在这里是**正确的**：它本来就不是线性因子。
#       所以不能给线性权重 —— 那等于给中间档的行为付钱。
#
#   但顶档是稳的（分段 +2.9 / +4.6 / +4.7pp；逐月 12 个月里 11 个月为正）。
#   所以做法是**门槛式加分**：够到顶档给分，不够不给。
#
# ★ 门槛值与依据：
#       seal ≥ TOP_SEAL(0.0945) ⇒ 顶档，实测 18.5% vs 其余 14.3%（+4.2pp）
#   0.0945 是 seal 的 80 分位（用 243 天全样本标定）。
TOP_SEAL = 0.0945

# ★ 为什么门槛还要求 n_first ≥ MIN_FIRST：
#   封板率是比率，1 次历史时「1 中 1」原始比例 100%，Wilson 下界也已 0.21
#   （超过门槛）—— 那是 1 个样本编出来的结论。要 2 次以上才有意义。
MIN_FIRST = 2

# ★ 权重 8：+4.2pp 的效应量级，明显小于封板质量（60%→37% 那档，权重 15）、
#   也小于审美三要素。给 8 是"值得加权但不该主导排序"。
#   ★ 不能再高：顶档只有 1461/6340 ≈ 23% 的票吃得到，权重一大就会变成
#   「有没有吃到股性分」在主导排序，那是在排"上市够久"。
SEAL_W = 8.0


def seal_score(g: dict, boards: int, top_seal: float | None = None,
               min_first: int | None = None,
               weight: float | None = None) -> tuple[float, str]:
    """把封板率换算成加分与理由。返回 (分数, 理由文案)。

    门槛式：够到 TOP_SEAL 给满分，否则 0。不按梯度给 —— 见上方注释，
    这个因子在首板上的形状是门槛不是斜坡，按斜坡给分等于给噪音付钱。

    top_seal / min_first / weight 三个参数留给配置覆盖（strategY_config 的
    M4_aesthetic.gene_top_seal / gene_min_first / weight_gene_seal）。
    ★ 传 None 就用本模块的标定常量 —— 调用方不传时行为与以前完全一致。
    """
    if not g or not g.get("known"):
        return 0.0, ""
    if (boards or 0) != 1:
        # 只对首板给分：候选口径就是首板；2 板以上实测效应更强但那批票
        # 根本进不了候选，给分只是白白改变排序、没有可执行含义。
        return 0.0, ""
    _min_first = MIN_FIRST if min_first is None else int(min_first)
    _top = TOP_SEAL if top_seal is None else float(top_seal)
    _w = SEAL_W if weight is None else float(weight)
    n_first = g.get("n_first") or 0
    if n_first < _min_first:
        return 0.0, ""
    s = float(g.get("seal") or 0.0)
    if s < _top:
        return 0.0, ""
    pct = (g.get("n_first_hit") or 0) / max(n_first, 1)
    why = (f"股性（首板历史 {n_first} 次、次日再涨停 {g.get('n_first_hit', 0)} 次"
           f"＝{pct*100:.0f}%，收缩后 {s*100:.1f}%）")
    return _w, why
