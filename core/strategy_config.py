# -*- coding: utf-8 -*-
"""策略配置定义。

设计原则：把「全能的野人」体系拆成 9 个可独立开关/调参的模块，
每个模块的阈值都暴露成配置项，实现「随时改变选股策略」。

模块编号对应用户的总表：
  M1 宏观周期  M2 量能模式  M3 情绪周期  M4 审美三要素
  M5 仓位操作系统  M6 消息与信息观  M7 业绩披露期  M8 保命纪律
  M9 题材主线五信号（消息定级→合力→中军→老龙→次日验证）
  M10 主导资金分类（机构/游资/庄股，定离场）

★ 策略口径（一次说清，别再写反）：
  · 野人的体系**不是「量能萎缩就买」**——量能萎缩只是市场状态，
    意识到市场缩量之后，才切到**短线题材的应对打法**；
  · 平常他做中长线波段，**不打板、不做短线**；
  · 原来的四套预设（野人原版/激进打板/保守波段/超短首板）已**融合成
    这一套自适应体系**：同一套打分选标的，个股适合哪种打法由
    engine.style_fit 判定，**只写在股票详情页**。
"""
from __future__ import annotations

import copy
import json
import os

from . import paths
from . import swing as swing_mod

CONFIG_PATH = paths.data_path("strategy.json")

DEFAULT_PRESET = "wildman"


# ---------------------------------------------------------------- 预设策略

# M9 题材主线五信号：阈值随时可在面板里改
_M9_DEFAULTS = {
    "enabled": True,
    "auto_message": True,             # 自动抓快讯定级（手动录入 message_level 优先）
    "message_level": "auto",          # 消息定级别：S/A/B/C，auto=交给自动定级
    "message_note": "",               # 今日消息备注
    "min_theme_zt": 3,                # 首日合力：题材内同日涨停最少家数
    "min_early_seal": 2,              # 首日合力：开盘60分钟内最少封板家数
    "early_deadline": "10:30",        # 首日合力：早封截止时刻
    "board_volume_surge": 1.3,        # 板块指数量能/前5日均量 的带量线
    "midarmy_mv_min": 100.0,          # 中军：流通市值门槛（亿）
    "midarmy_pct_min": 3.0,           # 中军：异动涨幅门槛（%）
    "midarmy_min_count": 1,           # 中军：最少参与家数
    "old_dragon_min_boards": 3,       # 老龙档案：历史最高连板门槛
    "old_dragon_pct": 5.0,            # 老龙异动涨幅门槛（%）
    "dragon_premium_min": 3.0,        # 次日：空间龙候选平均高开溢价线（%）
    "firstboard_survive_min": 0.5,    # 次日：昨日首板活口留存率线
    "survive_pct_min": 3.0,           # 次日：活口判定涨幅（%）
    "midarmy_feedback_max": -2.0,     # 次日：中军负反馈线（%）
    "theme_lookback_days": 10,        # 主线回归：近N日内领衔过=回归日（非新题材首日）
}

# M10 主导资金分类：机构/游资/庄股，离场口径完全不同
_M10_DEFAULTS = {
    "enabled": True,
    "inst_mv_min": 80.0,              # 机构票流通市值门槛（亿）——中军体量
    "inst_max_zt20": 1,               # 近20日涨停次数上限（多了就不是慢牛）
    "inst_max_daily_gain": 7.5,       # 单日最大涨幅上限（小阳线爬坡）
    "inst_min_pct20": 5.0,            # 20日涨幅下限（确实在走趋势）
    "inst_max_pct20": 60.0,           # 20日涨幅上限（超过=已过热）
    "hot_zt10_min": 2,                # 近10日涨停次数下限（连板节奏=游资）
    "zhuang_crash_pct": -7.0,         # 庄股：断崖单日跌幅
    "zhuang_chop_amp": 2.8,           # 庄股：断崖前日均波动上限（无声阴跌）
}

# ---- M11 波段体系（野人《波段体系》7 讲，v1.25.0 融合） ----
# 这是"平常打法"的量化落地：第一/第二入场点、第二波接入、高位滞涨离场。
# 阈值定义在 core/swing.py 里（与判定逻辑放在一起，改逻辑时不容易漏改阈值）。
_M11_DEFAULTS = dict(swing_mod.SWING_DEFAULTS)

# ---- 扩展模块（E1-E3，参考 GitHub 同类项目新增，非野人原体系） ----
_E1_DEFAULTS = {
    "enabled": True,                  # E1 情绪温度计：连板梯队/炸板率/晋级率/涨跌家数
    "ebb_hurt_ratio": 0.30,           # 退潮预警用的亏钱面积阈值（与 M3 同口径）
}
_E2_DEFAULTS = {
    "enabled": True,                  # E2 龙虎榜·游资席位（数据只进内部判断，不上界面）
    "top_n": 12,                      # 净买入榜展示条数（lhb.py 读它、配置面板也编辑它）
}
_E3_DEFAULTS = {
    "enabled": True,                  # E3 主力资金流
    "board_top_n": 10,                # 行业板块净流入展示条数
}
_E4_DEFAULTS = {
    "enabled": True,                  # E4 信号复盘档案（胜率闭环）
    "cost_pct": 0.002,                # 往返总成本（佣金+印花税+滑点），从收益扣除
    "max_settle_per_scan": 30,        # 每轮最多结算多少只（控数据源压力）
}
# E10 轻量回测（v1.37.0）—— 把 E4 的逐笔收益聚合成组合级净值曲线。
# 为什么单独一个模块而不是塞进 E4：E4 回答「这个信号准不准」，
# E10 回答「照这套做下去钱是多了还是少了、中间最深亏多少」—— 后者需要
# 仓位假设（等权）与组合聚合，是另一层的问题，混在一起两边都说不清。
_E10_DEFAULTS = {
    "enabled": True,
    # 一笔交易持有几个交易日（用 E4 档案的 ret1/ret3/ret5 哪一档）
    # 默认 5：短线打法的实际持有周期；改成 1 会变成"隔日超短"口径，
    # 数字完全不同，所以这个值要跟着你的实际持有习惯调。
    "hold_days": 5,
    # 少于这么多笔就不给结论（只如实说样本不够）
    "min_samples": 10,
}

# E11 大金融中长线（用户 2026-10-15）
# 「持仓大金融属性的股票时不要用当前市场的判断来给出卖点 …… 这类票一般都是
#  持有 1 到 3 年的中长线，短期市场的变化对它没有用，这类票要专门重新做一个
#  策略出来」。
# 与短线体系的口径冲突不在参数上，而在**时间尺度**上：S1 退潮清仓是
# critical「无条件清仓」，套在打算拿 3 年的券商仓上就自相矛盾。所以本组
# 配置是给 core/longterm.py 的独立一套卖出纪律（L1 估值 / L2 基本面 /
# L3 目标达成·到期 / L4 宽幅止损），不与 M3-M10 共享任何阈值。
# 默认值的由来（调研实证）：
#   · 分位阈 85%/95%：券商 PB 的历史 80% 分位约 2.26、最高 5.70、最低 1.05，
#     故"高位减、极高分位清"用分位表达比用绝对 PB 稳（各板块中枢差很远）；
#   · 止损 -25%：大金融年度级正常回撤可达 -20%，短线 -8% 会被反复洗出去；
#   · 银行/保险 PE 不作锚：净息差下行 → 盈利缩 → PE 被动抬高。实测同一
#     时点银行 PE 分位 97% 而 PB 分位 44%，这就是"用 PE 判银行贵贱"的坑。
_E11_DEFAULTS = {
    "enabled": True,
    "hold_days": 750,                 # 计划持有 3 年（交易日）＝到期复核点
    "hold_days_min": 250,             # 满 1 年才开始提示可用估值决定去留
    "target_return": 0.60,            # +60% 分批兑现
    "target_return_review": 0.30,     # +30% 进入关注区
    "pb_pct_warn": 85.0,              # PB 历史分位 ≥85% 提示减仓
    "pb_pct_clear": 95.0,             # ≥95% 提示分批兑现
    "pe_pct_warn": 85.0,
    "pe_pct_clear": 95.0,
    "stop_loss": -0.25,               # 宽幅灾难位（需配合基本面复核）
    "drawdown_warn": -0.35,           # 板块自区间高点回撤（仅供参考，组合级）
    "min_samples": 60,                # 分位样本下限（不足则不给分位）
    "pct_window": 750,                # 分位回看窗口≈3 年（与持有尺度一致）
    "pct_window_long": 2400,          # 长窗口≈10 年（判"绝对水平贵不贵"）
    "pe_unreliable_boards": ["银行", "保险"],
}
_E5_DEFAULTS = {
    "enabled": True,                  # E5 低位承接方向（退潮期机构资金去哪）
    # 「价没动·钱在进」的涨幅上限（板块级）—— 它同时是「追涨线」：
    # 线内算还在吸筹区间，超出就标「涨幅已大·不追」、降权并沉到列表末尾。
    # 1.0 的由来（可复核）：10-13 实测「主力净流入 ≥0.3 亿」的板块共 35 个，
    # 当日涨幅中位数 +1.09% —— 1% 大致压在中位数上（线上约 4 成板块通过）。
    # 调到 2% 会让 74% 的板块通过（几乎不过滤）；调到 0.5% 只留 14%（很苛刻）。
    "max_pct": 1.0,
    "min_main": 0.3,                  # 主力净流入低于此额（亿）视为噪音，不参与评分
    "pos_days": 3,                    # 「位置」看近几个已归档交易日的累计涨幅
    "top_n": 5,                       # 最多输出几个方向（做"方向清单"，不是 3 个硬推荐）
    "min_score": 55,                  # 低于此分不输出（宁缺毋滥）
    "trial_weight": 0.05,             # 仓位口径：退潮期试错仓（与 M5 同口径）
}

# E6 停牌·补位推演（用户 10-13：新华传媒昨晚停牌核查，今天应能预判同题材补位）
_E6_DEFAULTS = {
    "enabled": True,
    # 停牌股自身必须**近期连板到这个高度**才算"龙头停牌"，否则只当噪声：
    # 实测 10-13 全市场零成交的有两只 —— 新华传媒（10 板·出版龙头，该推演）
    # 与新亚制程（1 板，纯噪声）。阈值就是用来把后者挡在外面的。
    "min_boards": 2,
    "lookback_days": 5,               # 翻几天本地涨停池归档来找"谁是龙头"
    "max_halts": 3,                   # 最多推演几只停牌股
    "top_peers": 4,                   # 每只停牌股最多列几个同题材补位候选
    "early_deadline": "10:30",        # 「早封」的分界线（补位打分里占 40 分）
    # 相邻题材候选的排序扣分（同行业不扣）。见 substitute.THEME_SIBLINGS：
    # 东财行业把"内容/传媒"切成 出版/影视院线/电视广播/数字媒体/广告营销，
    # 只按行业全等匹配会漏掉真正的补位龙头（10-13 是影视院线的慈文传媒），
    # 所以放宽到相邻行业，但用扣分体现标签差异 —— 既不沉底、也不同权。
    "near_penalty": 8.0,
}

# E7 逆势连板票·独立通道（用户 10-13 的九阳股份案例）
_E7_DEFAULTS = {
    "enabled": True,
    # 连板高度门槛：低于此高度撑不出"产业逻辑"的结论（实测九阳 6 板、
    # 新华传媒 10 板；3 板是能看出"跨过退潮还在延续"的最低限度）
    "min_boards": 3,
    "lookback_days": 10,              # 翻几个归档交易日找连板链
    "top_n": 5,
    # 首板日流通市值上限（亿）：首板时小市值才谈得上弹性。实测九阳首板约 79 亿。
    # 注意是**首板那天**的市值（用日K首板价反推），不是今天的 —— 涨了 60% 之后
    # 它已经 134 亿，但买点那一刻它合格。
    "mv_max": 100.0,
    # 「缩量一字」的判定：该板换手 ≤ 前一板换手 × 此值
    "shrink_ratio": 0.7,
    # 「炸板回封」的判定：炸板次数 ≥ 此值、且当日仍收在涨停
    "reseal_breaks": 3,
    "mv_check": True,                 # 是否取日K算首板市值（关掉可省一次请求）
}

# E8 题材播种（事件 → 受益 → 标的）· 前置题材池（用户 10-13「打板要提前找题材」）
_E8_DEFAULTS = {
    "enabled": True,
    # 看几个归档交易日来判题材生命周期与"重复启动"。默认 30：实测九阳两次启动
    # 间隔 9 个交易日（09-11 → 09-28），15 天窗口会把第一次启动挤出视野，
    # 把"两次启动"误判成"一路连板"。
    "lookback_days": 30,
    "top_themes": 8,                  # 题材日历最多列几个
    "top_lonely": 3,                  # 独苗题材最多列几个
    "top_restarts": 6,                # 重复启动最多列几只
    "lonely_max_peers": 1,            # 独苗定义：题材今日涨停 ≤ 此数且是首板
    "drifted_only": False,            # True=重复启动只列"标签漂移"的（更聚焦但更少）
}


# E9 竞价早读（集合竞价 09:15–09:25 → 今天该盯什么）
# 用户 10-14「做个当前集合竞价时的分析板块」。此前代码里到处有"集合竞价"这个词，
# 但它只是个**时段标签**（market_session 的 phase="auction"），竞价本身从没被当
# 数据读过 —— 09:20 打开界面看到的还是昨天的结论。腾讯快照的竞价匹配价一直在拉，
# 本模块把它读出来。零新增接口。
_E9_DEFAULTS = {
    "enabled": True,
    # 异动榜各取前几只（一字 / 大高开 / 大低开）。10 是有意的克制：竞价异动
    # 一屏看十几只已经够，再多就变成"全市场都在异动"等于没筛选。
    "top_n": 10,
    # 昨日涨停池列几只。默认 12：实测涨停池常有 50+ 只，全列出来是信息倾泻，
    # 而排序已经把"一字 > 高开 > 平开 > 低开 > 大幅低开"排好了，前 12 只正好
    # 覆盖住最该看的两端。想看全部时前端有"展开全部"。
    "top_prev": 12,
    # "大高开/大低开"的分界（%）。取 5%：实测竞价 0~2% 档占全市场 89%，
    # 那是噪声底；2~5% 也只是略偏；到 5% 才是真正的态度表达。
    "big_gap": 5.0,
    # 市场偏向（抢筹/抛售）的判定阈值（%）：涨停+高开家数 与 跌停+低开家数
    # 的净差占比超过此值才下结论。±5% 是有意的宽松 —— 卡太紧会把每天都
    # 判成"抢"或"逃"。
    "bias_th": 5.0,
}


PRESETS: dict[str, dict] = {
    # ===== 单一融合体系：四套预设已合并，个股打法写在详情页 =====
    "wildman": {
        "name": "野人体系",
        "desc": ("平常中长线波段为主、不打板不做短线；市场缩量时才切短线题材"
                 "应对打法。个股适合哪种打法写在股票详情里。"),
        "M1_macro": {
            "enabled": True,
            "fear_hawkish": "risk_off",       # 危机式降息/暴力加息期
            "easing_start": "bull_finance",   # 降息+降准 → 大金融波段
            "hawkish_start": "caution",       # 加息开启 → 警惕
            "manual_override": "auto",
        },
        "M2_volume": {
            "enabled": True,
            # ---- 绝对档位（亿，两市成交额**全天**口径）----
            # ★ 三个阈值必须满足 peak ≥ comfort ≥ shrink（单调递减）。
            #   历史默认值把 comfort 写成 5000、shrink 写成 12000（倒挂），
            #   而判定按 comfort → shrink 顺序分支，于是整个 [5000, 15000)
            #   区间都被"量能舒适"吃掉，`shrink_short` 成了**永不触发的死分支**
            #   —— 实测 8000 亿被报成"量能舒适·游资具备轮动能力"。已修正。
            "peak_threshold": 15000,          # 峰值量能下限（还需相对位置达标）
            "comfort_threshold": 12000,       # 量能舒适下限（游资具备轮动能力）
            "shrink_threshold": 8000,         # 缩量存量下限；低于此=极度缩量→空仓
            "shrink_red_flag": True,          # 极致缩量还红盘 = 筹码结构好
            # ---- 相对口径（治「只看绝对阈值」）----
            # 绝对阈值只回答"钱够不够撬动大票"，回答不了"相对最近是多是少"。
            # 不加这一层，放量后的回落会被判成"峰值量能"：2026-10-12 两市
            # 15378 亿（较 10-09 的 19006 亿回落 19%）被判峰值，而同日沪指量能
            # 处于近 29 日的 27.6% 分位 —— 标签与自己的数据打架（用户实测）。
            "lookback": 20,                   # 分位/环比回看的交易日数
            "peak_pct": 0.80,                 # 峰值量能要求：近N日分位 ≥ 此值
            "peak_trend_min": 1.00,           # 峰值量能要求：≥近N日中位数的此倍数
            "shrink_pct": 0.45,               # 缩量要求：近N日分位 ≤ 此值
        },
        "M3_sentiment": {
            "enabled": True,
            "ebb_hurt_ratio": 0.30,           # 亏钱效应扩散面积阈值
            "ebb_limit_down_ratio": 0.15,     # 跌停/涨停 比
            "ebb_broken_ratio": 0.50,         # 炸板/涨停 比
            "clear_on_ebb": True,             # 退潮无条件清仓（已持仓）
            "ebb_trial_weight": 0.05,         # 退潮期允许的试错仓：0.5 成（0 = 完全空仓）
            "probe_board": 1,                 # 首板试错
            "cycle_low_probe": True,          # 退潮后等新周期首板
            # 情绪阶段滞回参数（治盘中"一会能出手一会退潮"，见 stage_stabilizer）
            "ebb_enter_n": 2, "ebb_enter_span": 60, "ebb_strong_margin": 0.15,
            "ebb_exit_n": 2, "ebb_exit_span": 180, "ebb_exit_margin": 0.10,
            "ebb_warmup_min": 30,
        },
        "M4_aesthetic": {
            "enabled": True,
            "price_min": 5.0, "price_max": 10.0,
            "mv_max": 100.0,                  # 亿，流通市值上限
            "mv_mid_threshold": 500.0,        # 亿，超此为中军/趋势票→短线不做
            "exclude_boards": ["科创板", "北交所"],
            "need_bull_ma": False,            # 均线多头排列作为加分而非必需
            "recent_limitup_days": 20,        # 前N日有涨停
            "recent_limitup_min": 1,
            "history_two_board": True,        # 历史有两板基因
            "min_turnover": 1.0,              # 最低换手%
            "max_turnover": 25.0,
            "exclude_st": True,
            # 候选股最多允许的**当日连板数**（默认 1 = 只放首板及尚未涨停的票）。
            # 连板 ≥2 的票体系一律"不接力/不追"，放进候选列表会被读成"推荐打 2 板"，
            # 与"买首板"的口径自相矛盾（用户 10-13 反馈）。
            "max_candidate_boards": 1,
            # 「可打的首板」加权（分，0 = 关闭）：审美三要素只回答"这票好不好"，
            # 还要回答"今天能不能打"。只给**今日首板**、且按封板质量给（早封/未炸/封单厚），
            # 让真能打的首板优先进入候选（用户 10-13 选定方案 A）。
            "first_board_bonus": 10,
            "first_board_early": 100000,      # 早封判定：首封时间 ≤ 10:00:00
            "first_board_fund_ratio": 0.005,  # 封单额 / 流通市值 ≥ 0.5% 算封得厚
            "weight_graph": 35, "weight_price_mv": 30, "weight_imagination": 35,
            # ── 区分度检验落地（v1.37.0，见 core/discrimination.py / check_signals.py）──
            # 用 243 天归档 × 当日涨停池 = 16542 条样本跑 G1–G4 四关，
            # 结果 = 次日是否再涨停（基线 19.1%）。实测：
            #   · 封板时间  lift 3.73x  秩相关 +1.00   10.0% → 37.3%   ← 全系统最强因子
            #   · 封单额    lift 2.63x  秩相关 +1.00   12.2% → 32.2%
            #   · 成交额    lift 0.49x  秩相关 −1.00   28.2% → 13.9%   方向反了
            #   · 流通市值  lift 0.59x  秩相关 −1.00   23.9% → 14.0%   方向反了
            #   · 换手率    秩相关 +0.00            ← 落在噪音区间，不该占权重
            #   · 题材热度  秩相关 +0.80（未过 G4）
            # 下面三个开关就是把这些结论接进打分。默认全开，关掉即回到旧口径。
            "use_seal_quality": True,         # 封板质量（早封 + 封单厚）参与打分
            "use_small_size_bias": True,      # 小市值/小成交额加分（方向为负，反着用）
            "drop_turnover_weight": True,     # 想象力分里去掉"换手活跃"这一项
            "weight_seal_quality": 15,        # 封板质量权重（仅对今日涨停的票生效）
            "weight_small_size": 10,          # 小市值/小成交额权重
            # ★ 涨停基因 / 股性（E11，v1.39.0）—— 个股历史「涨停之后还涨不涨得住」。
            #   数据限制：归档无 open/high/前收，所以参考项目的五项因子里
            #   「次日溢价 / 次日开盘溢价 / 次日红盘率」三项做不了，只做了封板率与频率。
            #   实测（243 天归档）：
            #     · 涨停频率 freq —— 合并 lift 1.65x，但同板数内部方向会翻
            #       （1板 +4.1pp / 2板 −4.6pp / 3板 −5.7pp），辛普森悖论 ⇒ 不接
            #     · 首板封板率 seal —— 首板上是**门槛**不是斜坡：
            #       顶档（seal ≥ 0.0945）18.5% vs 其余 14.3% = +4.2pp，
            #       分段 +2.9/+4.6/+4.7pp、逐月 12 个月里 11 个月为正（稳定）。
            #       中间档不单调（G4 判"方向不一致"是对的），所以只能门槛式给分。
            "use_gene_seal": True,            # 股性门槛加成参不参与打分
            "weight_gene_seal": 8,            # 股性权重（够门槛给满分，否则 0）
            "gene_top_seal": 0.0945,          # 顶档门槛（seal 的 80 分位，全样本标定）
            "gene_min_first": 2,              # 至少几次首板历史才算数（1 次样本编不出结论）
            # 封板质量的标定端点（实测分位标定，改动前先看 engine._seal_quality_score 注释）
            "seal_early_full": 92500,         # 09:25 竞价一字 = 满分
            "seal_early_zero": 103000,        # 10:30 及更晚封板 = 0 分
            "seal_ratio_full": 0.08,          # 封单/流通 8%（约 p90）= 满分
            "seal_ratio_zero": 0.005,         # 封单/流通 0.5%（约 p00）= 0 分
            # 小市值/小成交额的标定端点（越小越加分）
            "small_mv_full": 20.0,            # 20 亿及以下 = 满分（p01 附近）
            "small_mv_zero": 200.0,           # 200 亿以上 = 0 分
            "small_amt_full": 200000000.0,    # 2 亿及以下 = 满分（p01 附近）
            "small_amt_zero": 2000000000.0,   # 20 亿以上 = 0 分
        },
        "M5_position": {
            "enabled": True,
            "pick_count": 4,                  # Day1 平铺 3-4 只
            "first_weight": 0.20,             # 每只试错 20%
            "cash_reserve": 0.20,
            "hold_before_boards": 5,          # 龙头五板前不折腾
            "add_on_strength": True,          # Day2 去弱留强
            "max_single_weight": 0.60,
            "ebb_trial_weight": 0.05,         # 退潮期试错仓：0.5 成，只做首板
        },
        "M6_message": {
            "enabled": True,
            "no_chase_clear_good": True,      # 明牌利好不追
            "follow_money_not_news": True,
        },
        "M7_earnings": {
            "enabled": True,
            "q3_pre_disclose_start": "10-08",
            "hold_cash_before_holiday": True,  # 节前持币
            "post_holiday_focus": ["大金融", "大科技"],
            "only_with_earnings": True,        # 业绩期只做有业绩方向
            # 休市日历（yyyyMMdd），供 outlook._next_trade_date 精确推算下一交易日。
            # ★ 为什么需要它（v1.39.0）：原来只按"工作日 +1 跳周末"估算，节前
            #   会把"明日"写成实际休市的日期（如 10-01 写成 10-02），「明日判断」
            #   整张卡的日期是错的，用户按它做隔日计划就会落空。
            # 这里是**显式表**而不是调接口：outlook 在渲染路径上，不该为了一行
            #   日期去发网络请求。放不进预设的年份用接口口径兜底（见 outlook）。
            # 数据源：上交所《年度休市安排》2026 年表
            #   https://www.sse.com.cn/disclosure/dealinstruc/closed/
            #   只列**工作日休市**；周末本来就不开市，由 weekday()>=5 处理，
            #   表里不重复收录（收了也不出错，但会让 exact 判据更难读）。
            #   注：因放假而调休的**周末**（如 2026-09-20 周日、10-10 周六
            #   "为周末休市"）不必收录 —— 本来就是周末。
            #   每年 12 月交易所公布次年安排后往这里补；
            #   补了之后移动端/PC 端的「明日判断」日期才会去掉"估算"字样。
            "market_holidays": [
                # 元旦：1/1(四)–1/3(六) 休市，1/5(一) 开市
                "20260101", "20260102",
                # 春节：2/15(日)–2/23(一) 休市，2/24(二) 开市
                "20260216", "20260217", "20260218", "20260219", "20260220",
                "20260223",
                # 清明：4/4(六)–4/6(一) 休市，4/7(二) 开市
                "20260406",
                # 劳动节：5/1(五)–5/5(二) 休市，5/6(三) 开市
                "20260501", "20260504", "20260505",
                # 端午：6/19(五)–6/21(日) 休市，6/22(一) 开市
                "20260619",
                # 中秋：9/25(五)–9/27(日) 休市，9/28(一) 开市
                "20260925",
                # 国庆：10/1(四)–10/7(三) 休市，10/8(四) 开市
                "20261001", "20261002", "20261005", "20261006", "20261007",
            ],
        },
        "M8_discipline": {
            "enabled": True,
            "rules": [
                {"id": "capital", "text": "保住本金是第一原则，单笔风险不超过总资金2%", "on": True},
                {"id": "ebb_clear", "text": "退潮期无条件清仓，管住手", "on": True},
                {"id": "cut_weak", "text": "去弱留强不带幻想，砍错不心疼", "on": True},
                {"id": "no_anti_cycle", "text": "不对抗周期，量能不够不做", "on": True},
                {"id": "rational", "text": "理性独立思考，不带幻想、不犟", "on": True},
                {"id": "no_chase", "text": "明牌不追", "on": True},
                {"id": "no_noearn", "text": "业绩披露期不碰无业绩题材", "on": True},
                {"id": "holiday_cash", "text": "中长线节假日持币、短线走一步看一步", "on": True},
                {"id": "volume_only", "text": "缩量市场只信量能，不信任何板块吹嘘", "on": True},
                {"id": "shrink_react", "text": "短线是缩量期的应对打法、不是常态：平时中长线波段，不打板不做短线", "on": True},
            ],
        },
        "M9_mainline": dict(_M9_DEFAULTS),
        "M10_fundstyle": dict(_M10_DEFAULTS),
        "M11_swing": dict(_M11_DEFAULTS),
        "E1_meter": dict(_E1_DEFAULTS),
        "E2_lhb": dict(_E2_DEFAULTS),
        "E3_flow": dict(_E3_DEFAULTS),
        "E4_journal": dict(_E4_DEFAULTS),
        "E5_rebound": dict(_E5_DEFAULTS),
        "E6_substitute": dict(_E6_DEFAULTS),
        "E7_industry": dict(_E7_DEFAULTS),
        "E8_sowing": dict(_E8_DEFAULTS),
        "E9_auction": dict(_E9_DEFAULTS),
        "E10_backtest": dict(_E10_DEFAULTS),
        "E11_longterm": dict(_E11_DEFAULTS),
    },
}


def get_preset(key: str) -> dict:
    if key not in PRESETS:
        key = DEFAULT_PRESET
    return copy.deepcopy(PRESETS[key])


def list_presets() -> list[dict]:
    return [{"key": k, "name": v["name"], "desc": v["desc"]} for k, v in PRESETS.items()]


def default_config() -> dict:
    cfg = get_preset(DEFAULT_PRESET)
    cfg["preset"] = DEFAULT_PRESET
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，缺失的键保留 base 默认值。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_rules(default_rules: list, saved_rules: list | None) -> list:
    """M8 纪律规则按 id 合并：保留用户对已有规则的开关，补齐新增规则。

    列表是整体替换的（_deep_merge 只递归 dict），不这样做的话，
    新增的纪律条款会被旧存档里的整份列表覆盖掉、永远不出现。
    """
    saved_by_id = {r.get("id"): r for r in (saved_rules or [])
                   if isinstance(r, dict)}
    out = []
    for r in default_rules:
        s = saved_by_id.get(r.get("id"))
        out.append({**r, "on": bool(s.get("on", r.get("on", True)))} if s else dict(r))
    known = {r.get("id") for r in default_rules}
    for r in saved_rules or []:
        if isinstance(r, dict) and r.get("id") not in known:
            out.append(r)
    return out


# 已被融合掉的历史预设：存档里残留这些键时，配置整体回到单套默认
_LEGACY_PRESETS = {"aggressive", "conservative", "firstboard"}


# 被「按百分比渲染」写坏过的字段：它们的默认值本身就是**百分数**（3.0 = 3%），
# 但配置面板曾用 data-pct 渲染（保存时 ÷100）→ 用户只要改一次就存成 0.03，
# 而 core/swing.py 里是 `abs(pct) > float(cfg["stall_pct"])`（pct 是百分数）
# → 条件几乎恒真 → **高位放量滞涨离场点 WS1 几乎不再触发**（实测确认）。
# 这里按 ÷100 的逆运算就地还原；面板渲染器也已改回 num（见 web/index.html）。
_PCT_FIELDS = {
    "M11_swing": ("stall_pct",),
}


def _fix_pct_fields(cfg: dict) -> bool:
    """把被 ÷100 写坏的百分数字段 ×100 还原。只认默认值本身 > 1 的字段。"""
    changed = False
    for mod, keys in _PCT_FIELDS.items():
        m = cfg.get(mod)
        base = PRESETS[DEFAULT_PRESET].get(mod) or {}
        if not isinstance(m, dict):
            continue
        for k in keys:
            try:
                dv = float(base.get(k, 0))
                v = float(m.get(k, dv))
            except (TypeError, ValueError):
                continue
            if dv > 1 and 0 < v < 1:
                m[k] = round(v * 100, 4)
                changed = True
    return changed


def _fix_m2_ladder(cfg: dict) -> bool:
    """把历史存档里**倒挂**的 M2 量能阶梯重置为单调阶梯，返回是否改动。

    老默认值是 peak=15000 / comfort=5000 / shrink=12000 —— comfort 反而低于
    shrink。判定按 comfort → shrink 的顺序分支，于是 [5000, 15000) 全被
    "量能舒适"吃掉，`shrink_short` 永不触发（缩量期切短线的打法形同虚设）。

    只在**确认倒挂**（peak < comfort 或 comfort < shrink）时重置；
    用户自己调成单调递减的值一律保留。
    """
    m2 = cfg.get("M2_volume")
    if not isinstance(m2, dict):
        return False
    base = PRESETS[DEFAULT_PRESET]["M2_volume"]
    try:
        peak = float(m2.get("peak_threshold", base["peak_threshold"]))
        comfort = float(m2.get("comfort_threshold", base["comfort_threshold"]))
        shrink = float(m2.get("shrink_threshold", base["shrink_threshold"]))
    except (TypeError, ValueError):
        peak = comfort = shrink = 0.0
    if peak >= comfort >= shrink:
        return False
    for k in ("peak_threshold", "comfort_threshold", "shrink_threshold"):
        m2[k] = base[k]
    return True


def load_config() -> dict:
    """读取当前配置；不存在则写入默认。

    两种迁移：
      · 旧四套预设（aggressive/conservative/firstboard）已融合成一套 ——
        存档 preset 命中历史键时，配置整体回到单套默认（那些值是预设派生
        的，不是用户手调，融合后失去意义）；
      · M8 纪律规则按 id 合并，新增条款不会因旧存档整份列表而被吞掉。
    """
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            key = saved.get("preset", DEFAULT_PRESET)
            if key in _LEGACY_PRESETS:
                cfg = default_config()
                save_config(cfg)
                return cfg
            if key not in PRESETS:
                key = DEFAULT_PRESET
            base = PRESETS[key]
            cfg = _deep_merge(base, {k: v for k, v in saved.items() if k != "preset"})
            cfg["M8_discipline"]["rules"] = _merge_rules(
                base.get("M8_discipline", {}).get("rules", []),
                (saved.get("M8_discipline") or {}).get("rules"))
            cfg["preset"] = key
            cfg["name"] = base.get("name", cfg.get("name", ""))
            cfg["desc"] = base.get("desc", "")
            # 历史存档的两类就地修正，任一命中就落盘：
            #   · M2 量能阶梯倒挂（老默认 comfort=5000 < shrink=12000）；
            #   · 百分数字段被配置面板 ÷100 写坏（M11_swing.stall_pct）。
            fixed = _fix_m2_ladder(cfg) | _fix_pct_fields(cfg)
            if fixed:
                save_config(cfg)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    cfg = default_config()
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def apply_preset(cfg: dict, preset_key: str, overrides: dict | None = None) -> dict:
    """切换预设（现已融合为单套，保留此接口用于「恢复默认」）。"""
    base = get_preset(preset_key)
    merged = _deep_merge(base, overrides or {})
    merged["preset"] = preset_key if preset_key in PRESETS else DEFAULT_PRESET
    return merged
