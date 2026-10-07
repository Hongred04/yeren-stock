# -*- coding: utf-8 -*-
"""策略配置定义 + 预设。

设计原则：把「全能的野人」体系拆成 9 个可独立开关/调参的模块，
每个模块的阈值都暴露成配置项，实现「随时改变选股策略」。

模块编号对应用户的总表：
  M1 宏观周期  M2 量能模式  M3 情绪周期  M4 审美三要素
  M5 仓位操作系统  M6 消息与信息观  M7 业绩披露期  M8 保命纪律
  M9 题材主线五信号（消息定级→合力→中军→老龙→次日验证）
  M10 主导资金分类（机构/游资/庄股，定离场）
"""
from __future__ import annotations

import copy
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "strategy.json")


# ---------------------------------------------------------------- 预设策略

# M9 题材主线五信号：五套预设共用同一套阈值，可随时在面板里改
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


PRESETS: dict[str, dict] = {
    # ===== 野人原版：完全按用户提供的体系落地 =====
    "wildman": {
        "name": "野人原版",
        "desc": "全能的野人原味体系：缩量做短线题材、审美三要素、退潮清仓",
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
            "comfort_threshold": 5000,        # 亿，回到此量能=游刃有余（打板天下）
            "shrink_threshold": 12000,        # 亿，低于此=缩量存量→只做短线
            "shrink_red_flag": True,          # 极致缩量还红盘 = 筹码结构好
        },
        "M3_sentiment": {
            "enabled": True,
            "ebb_hurt_ratio": 0.30,           # 亏钱效应扩散面积阈值
            "ebb_limit_down_ratio": 0.15,     # 跌停/涨停 比
            "ebb_broken_ratio": 0.50,         # 炸板/涨停 比
            "clear_on_ebb": True,             # 退潮无条件清仓
            "probe_board": 1,                 # 首板试错
            "cycle_low_probe": True,          # 退潮后等新周期首板
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
            ],
        },
        "M9_mainline": dict(_M9_DEFAULTS),
        "M10_fundstyle": dict(_M10_DEFAULTS),
    },

    # ===== 激进打板：情绪高位接力 =====
    "aggressive": {
        "name": "激进打板",
        "desc": "只做龙头接力，放宽市值与价格限制，量能充足时的高弹性策略",
        "M1_macro": {"enabled": True, "manual_override": "auto"},
        "M2_volume": {
            "enabled": True, "peak_threshold": 12000, "comfort_threshold": 8000,
            "shrink_threshold": 9000, "shrink_red_flag": True,
        },
        "M3_sentiment": {
            "enabled": True, "ebb_hurt_ratio": 0.45, "ebb_limit_down_ratio": 0.25,
            "ebb_broken_ratio": 0.70, "clear_on_ebb": True, "probe_board": 1,
            "cycle_low_probe": True,
        },
        "M4_aesthetic": {
            "enabled": True,
            "price_min": 3.0, "price_max": 30.0,
            "mv_max": 300.0, "mv_mid_threshold": 800.0,
            "exclude_boards": ["北交所"],
            "need_bull_ma": False,
            "recent_limitup_days": 10, "recent_limitup_min": 1,
            "history_two_board": False,
            "min_turnover": 2.0, "max_turnover": 35.0,
            "exclude_st": True,
            "weight_graph": 25, "weight_price_mv": 20, "weight_imagination": 55,
        },
        "M5_position": {
            "enabled": True, "pick_count": 3, "first_weight": 0.30,
            "cash_reserve": 0.10, "hold_before_boards": 7,
            "add_on_strength": True, "max_single_weight": 0.70,
        },
        "M6_message": {"enabled": False},
        "M7_earnings": {"enabled": False},
        "M8_discipline": {
            "enabled": True,
            "rules": [
                {"id": "capital", "text": "保住本金是第一原则", "on": True},
                {"id": "ebb_clear", "text": "退潮期无条件清仓", "on": True},
                {"id": "cut_weak", "text": "砍错不心疼", "on": True},
                {"id": "no_anti_cycle", "text": "不对抗周期", "on": True},
            ],
        },
        "M9_mainline": dict(_M9_DEFAULTS),
        "M10_fundstyle": dict(_M10_DEFAULTS),
    },

    # ===== 保守波段：只做有业绩的大票 =====
    "conservative": {
        "name": "保守波段",
        "desc": "量能峰值才出手，只做中大盘有业绩方向，周期票为主",
        "M1_macro": {"enabled": True, "manual_override": "auto"},
        "M2_volume": {
            "enabled": True, "peak_threshold": 20000, "comfort_threshold": 10000,
            "shrink_threshold": 14000, "shrink_red_flag": False,
        },
        "M3_sentiment": {
            "enabled": True, "ebb_hurt_ratio": 0.20, "ebb_limit_down_ratio": 0.10,
            "ebb_broken_ratio": 0.35, "clear_on_ebb": True, "probe_board": 1,
            "cycle_low_probe": False,
        },
        "M4_aesthetic": {
            "enabled": True,
            "price_min": 3.0, "price_max": 60.0,
            "mv_max": 3000.0, "mv_mid_threshold": 100000.0,
            "exclude_boards": ["北交所"],
            "need_bull_ma": True,
            "recent_limitup_days": 60, "recent_limitup_min": 0,
            "history_two_board": False,
            "min_turnover": 0.3, "max_turnover": 15.0,
            "exclude_st": True,
            "weight_graph": 45, "weight_price_mv": 30, "weight_imagination": 25,
        },
        "M5_position": {
            "enabled": True, "pick_count": 5, "first_weight": 0.20,
            "cash_reserve": 0.30, "hold_before_boards": 3,
            "add_on_strength": False, "max_single_weight": 0.35,
        },
        "M6_message": {"enabled": True, "no_chase_clear_good": True,
                       "follow_money_not_news": True},
        "M7_earnings": {"enabled": True, "only_with_earnings": True},
        "M8_discipline": {
            "enabled": True,
            "rules": [
                {"id": "capital", "text": "保住本金是第一原则", "on": True},
                {"id": "ebb_clear", "text": "退潮期无条件清仓", "on": True},
                {"id": "no_anti_cycle", "text": "不对抗周期", "on": True},
                {"id": "no_chase", "text": "明牌不追", "on": True},
            ],
        },
        "M9_mainline": dict(_M9_DEFAULTS),
        "M10_fundstyle": dict(_M10_DEFAULTS),
    },

    # ===== 超短首板：只做新周期首板试错 =====
    "firstboard": {
        "name": "超短首板",
        "desc": "极度聚焦：只做新周期首板试错，小市值低价，图形干净",
        "M1_macro": {"enabled": True, "manual_override": "auto"},
        "M2_volume": {
            "enabled": True, "peak_threshold": 15000, "comfort_threshold": 5000,
            "shrink_threshold": 13000, "shrink_red_flag": True,
        },
        "M3_sentiment": {
            "enabled": True, "ebb_hurt_ratio": 0.25, "ebb_limit_down_ratio": 0.12,
            "ebb_broken_ratio": 0.40, "clear_on_ebb": True, "probe_board": 1,
            "cycle_low_probe": True,
        },
        "M4_aesthetic": {
            "enabled": True,
            "price_min": 4.0, "price_max": 15.0,
            "mv_max": 80.0, "mv_mid_threshold": 200.0,
            "exclude_boards": ["科创板", "北交所", "创业板"],
            "need_bull_ma": False,
            "recent_limitup_days": 30, "recent_limitup_min": 0,
            "history_two_board": False,
            "min_turnover": 1.5, "max_turnover": 20.0,
            "exclude_st": True,
            "weight_graph": 40, "weight_price_mv": 30, "weight_imagination": 30,
        },
        "M5_position": {
            "enabled": True, "pick_count": 4, "first_weight": 0.20,
            "cash_reserve": 0.20, "hold_before_boards": 3,
            "add_on_strength": True, "max_single_weight": 0.50,
        },
        "M6_message": {"enabled": True, "no_chase_clear_good": True},
        "M7_earnings": {"enabled": True, "only_with_earnings": False},
        "M8_discipline": {
            "enabled": True,
            "rules": [
                {"id": "capital", "text": "保住本金是第一原则", "on": True},
                {"id": "ebb_clear", "text": "退潮无条件清仓，管住手", "on": True},
                {"id": "cut_weak", "text": "去弱留强不带幻想", "on": True},
                {"id": "no_anti_cycle", "text": "不对抗周期", "on": True},
                {"id": "volume_only", "text": "只信量能", "on": True},
            ],
        },
        "M9_mainline": dict(_M9_DEFAULTS),
        "M10_fundstyle": dict(_M10_DEFAULTS),
    },
}


def get_preset(key: str) -> dict:
    if key not in PRESETS:
        raise KeyError(f"未知策略预设: {key}")
    return copy.deepcopy(PRESETS[key])


def list_presets() -> list[dict]:
    return [{"key": k, "name": v["name"], "desc": v["desc"]} for k, v in PRESETS.items()]


def default_config() -> dict:
    cfg = get_preset("wildman")
    cfg["preset"] = "wildman"
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


def load_config() -> dict:
    """读取当前配置；不存在则写入 wildman 默认。"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # 与预设合并，保证新增配置项自动补齐
            base = PRESETS.get(saved.get("preset", "wildman"), PRESETS["wildman"])
            cfg = _deep_merge(base, {k: v for k, v in saved.items() if k != "preset"})
            cfg["preset"] = saved.get("preset", "wildman")
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
    """切换预设，并叠加用户当前的自定义修改（保留已改动的键）。"""
    base = get_preset(preset_key)
    merged = _deep_merge(base, overrides or {})
    merged["preset"] = preset_key
    return merged
