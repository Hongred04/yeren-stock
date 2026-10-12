"""节点（regime）—— M3 情绪阶段 × M2 量能的**组合结论**。

「节点」决定**今天该用什么打法**，而不是由标的本身决定：同样的票，
主升期捂是机会，退潮/缩量期捂是绞肉机 —— 不是标的的事，是节点的事。

★ 本模块是「今日打法」的**唯一出处**：`outlook` 的 mode 与 `trading` 的
  波段买点门禁**都读它**。以前两处各算一套（outlook 说"只做短线"、买卖点却照样
  给"波段入场"），同屏必然打架（用户 10-12 实测过同类矛盾）。收敛到一处，
  从构造上杜绝两套口径分叉。

对外只有 classify()：
    rg = regime.classify(sentiment, volume)          # trading 用（拿不到 macro）
    rg = regime.classify(sentiment, volume, macro)   # outlook 用
    rg["allow_swing"]  # 今天允不允许「做长」（波段入场点 W1/W2/W3）
"""
from __future__ import annotations

# 打法文案表 —— 与 outlook 历史文案**逐字一致**（改这里即全局生效）。
# key 取 M3 阶段名（probe/climax/follow/ebb）+ E1 防御性映射（ice/revive/ferment）。
_MODE = {
    "ebb": ("防守空仓", "空仓或试错仓，不做长线 —— 保存子弹；市场不好的时候"
                       "恰恰是执行去弱留强的时点"),
    "probe": ("波段试错", "新方向刚起步 —— 低位+资金刚动的地方跟进去"
                         "（要信早信），拿得住"),
    "follow": ("波段持股", "扩散期波段持股 —— 主线内不轻易换，拿得住才有肉"),
    "climax": ("短线快枪", "高潮期短线题材思维 —— 存量博弈不格局，快进快出，"
                          "同时执行去弱留强"),
    # E1 温度计阶段（ice/revive/ferment）不会作为 sentiment.stage 传进来，
    # 留着只是防御性映射。
    "ice": ("休息", "休息为主，空仓等启动信号 —— 手痒也别做长线"),
    "revive": ("轻仓试错", "轻仓试错 + 波段思维 —— 只看低位、资金刚动的地方"
                          "（要信早信），拿得住"),
    "ferment": ("波段持股", "发酵期波段持股 —— 主线内不轻易换，吃主升"),
}

# 极度缩量 / 缩量期的应对打法文案
_DRY = ("防守空仓", "极度缩量：量能不够说破大天都没用 —— 空仓等量能恢复，"
                   "手痒=送钱；别用长线思维硬扛")
_SHRINK_RED = ("缩量低吸应对", "极致缩量还红盘 = 控盘好、筹码结构好，可低吸 —— "
                              "但这是缩量期的权宜打法，不是常态策略")
_SHRINK_SHORT = ("短线应对", "缩量存量：资金撬不动大票 —— 只做短线中小题材的应对"
                            "打法，降低单票仓位；量能回来再谈中长线波段")

# 节点 → 中文短名
_NODE_CN = {
    "ebb": "退潮节点", "dry": "缩量节点", "shrink_red": "缩量节点",
    "shrink_short": "缩量节点", "probe": "主升节点", "follow": "主升节点",
    "climax": "高潮节点",
}

# ★ 允许「做长」（波段入场）的节点 —— 只有起步/扩散，且量能没缩。
#   退潮、极度缩量、缩量期、高潮期一律不做长（题材只配短线）。
#   注意：**不引入 macro** —— trading 拿不到 macro，若 allow_swing 依赖它，
#   两处判定就会分叉，正是本模块要消灭的问题。macro 仅作备注参考。
_ALLOW_SWING = {"probe", "follow"}


def classify(sentiment: dict, volume: dict, macro: dict | None = None) -> dict:
    """把「M3 阶段 × M2 量能」收敛成一个节点结论。

    优先级（与 outlook 历史逻辑一致）：退潮 > 极度缩量 > 缩量 > 阶段默认。
    """
    sentiment = sentiment or {}
    volume = volume or {}
    stage = sentiment.get("stage", "unknown")
    vol_mode = volume.get("mode", "unknown")

    if stage == "ebb":
        key = "ebb"
    elif vol_mode == "dry":
        key = "dry"
    elif vol_mode == "shrink_red":
        key = "shrink_red"
    elif vol_mode == "shrink_short":
        key = "shrink_short"
    elif stage in _MODE:
        key = stage
    else:
        key = "unknown"

    if key == "ebb":
        mode_name, mode_note = _MODE["ebb"]
    elif key == "dry":
        mode_name, mode_note = _DRY
    elif key == "shrink_red":
        mode_name, mode_note = _SHRINK_RED
    elif key == "shrink_short":
        mode_name, mode_note = _SHRINK_SHORT
    elif key in _MODE:
        mode_name, mode_note = _MODE[key]
    else:
        mode_name, mode_note = ("观察", "方向未明 —— 先看清阶段，再选打法")

    allow_swing = key in _ALLOW_SWING
    return {
        "key": key,
        "node_label": _NODE_CN.get(key, "未知节点"),
        "mode_name": mode_name,
        "mode_note": mode_note,
        "allow_swing": allow_swing,
        "allow_trade": key not in ("ebb", "dry", "unknown"),
    }
