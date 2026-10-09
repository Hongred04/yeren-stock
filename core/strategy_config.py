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

# ---- 扩展模块（E1-E3，参考 GitHub 同类项目新增，非野人原体系） ----
_E1_DEFAULTS = {
    "enabled": True,                  # E1 情绪温度计：连板梯队/炸板率/晋级率/涨跌家数
    "ebb_hurt_ratio": 0.30,           # 退潮预警用的亏钱面积阈值（与 M3 同口径）
}
_E2_DEFAULTS = {
    "enabled": True,                  # E2 龙虎榜·游资席位（数据只进内部判断，不上界面）
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
            "peak_threshold": 15000,          # 亿，两市成交额峰值模式阈值
            "comfort_threshold": 5000,        # 亿，回到此量能=游刃有余
            "shrink_threshold": 12000,        # 亿，低于此=缩量存量→切短线应对
            "shrink_red_flag": True,          # 极致缩量还红盘 = 筹码结构好
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
            "weight_graph": 35, "weight_price_mv": 30, "weight_imagination": 35,
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
        "E1_meter": dict(_E1_DEFAULTS),
        "E2_lhb": dict(_E2_DEFAULTS),
        "E3_flow": dict(_E3_DEFAULTS),
        "E4_journal": dict(_E4_DEFAULTS),
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
