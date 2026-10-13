# -*- coding: utf-8 -*-
"""E5 低位承接方向（扩展模块，非野人原体系）。

一句话：**退潮期，机构在高位兑现的那笔钱，去哪了？**

来历（用户 10-13 的真实复盘）：10-12 是退潮日（温度 84→60、跌停 19 家、
最高板 10 → 3），野人判断「明日白酒老登股有一波小波段」，次日
酿酒概念 +0.44%、主力 **+1.23 亿**（由 10-12 的净流出转为净流入）、
白酒Ⅱ 涨停 2 家。拆开看，这条判断的每一步都能用系统**已有**的数据复现 ——
只是原体系里没有任何模块把这几步串起来（`outlook` 只会说"防守为主"，
资金维度只会说"做题材不做高位大票"）。本模块就是那个缺掉的第 3 步。

**触发场景（三个条件同时成立才输出）**：

  ① **退潮期** —— 高位票在派发（M3/E1：stage = ebb）
  ② **存量博弈** —— 没有增量资金，所以只可能是"小波段"而不是主升（M2）
  ③ **机构在兑现** —— 机构专用席位净卖出（E2 龙虎榜）

推理链：机构卖出的钱不会消失，它有三个硬约束 —— **要容量（装得下）、
要低位（不能追高）、要基本面理由（否则没法向持有人交代）**；
同时游资还在题材里接力，说明市场没死，只是"钱要换个地方"。

**筛选逻辑（板块级，全部用现有数据，不新增接口）**：

  · **价没动、钱在进**（核心）—— 主力净流入 > 0 且当日涨幅 ≤ 阈值。
    今天买、价格却没被买上去 = 吸筹；这是本模块最重要的一条。
  · **首日净流入**（streak == 1）—— 对应体系里那条
    「低位 + 主力净流入刚转正 = 资金刚动，要信早信」。
  · **有催化** —— 财联社风口板块给了基本面理由（机构建仓需要理由）。
  · **非主线** —— 支线/观察才符合"小波段"定位；已经是主线的方向不追高。

输出给「今日结论」页一张卡：方向 + 逐条依据 + **仓位口径**（退潮期试错仓，
不是重仓）。定位是"提醒去看一眼"，不是买入指令。
"""
from __future__ import annotations

from . import cls as cls_mod
from . import moneyflow as flow_mod

# 存量博弈的量能模式（有增量资金的模式要排除 —— 那属于"主升"场景，
# 不在本模块射程内）。口径与 engine.VOLUME_MODES 保持一致。
STOCK_VOLUME_MODES = ("peak_swing", "shrink_short", "shrink_red", "dry")


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def evaluate(cfg: dict | None = None, sentiment: dict | None = None,
             volume: dict | None = None, flow: dict | None = None,
             sectors: dict | None = None, lhb: dict | None = None,
             ml: dict | None = None, real_date: str = "") -> dict:
    """退潮期「机构兑现 → 资金去哪」推演。

    返回 {enabled, active, gate, reason, candidates[], summary, note}。
    `active=False` 时 candidates 为空 —— 不硬凑方向。
    """
    a = (cfg or {}).get("E5_rebound", {})
    empty = {"enabled": bool(a.get("enabled", True)), "active": False,
             "gate": {}, "reason": "", "candidates": [], "summary": "",
             "note": ""}
    if not a.get("enabled", True):
        empty["reason"] = "模块已关闭"
        return empty

    sentiment = sentiment or {}
    volume = volume or {}
    flow = flow or {}
    sectors = sectors or {}
    lhb = lhb or {}
    ml = ml or {}

    stage = str(sentiment.get("stage") or "")
    vmode = str(volume.get("mode") or "")
    # 机构/游资净额：优先读 `flow.capital` 的结构化字段（E2 的数据最终折进这里，
    # 报告里没有独立的 lhb 键）；没有结构化字段时才回退到原始 lhb。
    cap = flow.get("capital") or {}
    inst_net = cap.get("inst_net")
    inst_net = (_f((lhb.get("institutions") or {}).get("net"), 0.0)
                if inst_net is None else _f(inst_net))
    hot_net = cap.get("hotmoney_net")
    hot_net = _f(lhb.get("hotmoney_net"), 0.0) if hot_net is None else _f(hot_net)

    # ---- 三道闸门 ----
    g_ebb = stage == "ebb"
    g_stock = vmode in STOCK_VOLUME_MODES
    g_inst_out = inst_net < 0
    gate = {
        "ebb": {"ok": g_ebb, "txt": f"情绪阶段 {stage or '未知'}"
                + ("（退潮）" if g_ebb else "（非退潮）")},
        "stock_game": {"ok": g_stock, "txt": f"量能 {volume.get('label') or vmode or '未知'}"
                       + ("（存量博弈 → 只做小波段）" if g_stock else "（有增量 → 属主升场景，不在本模块射程）")},
        "inst_out": {"ok": g_inst_out,
                     "txt": f"机构专用席位净{'卖' if g_inst_out else '买'} "
                            f"{abs(inst_net):+.2f} 亿"
                            + ("（在高位兑现 → 钱要找地方去）" if g_inst_out else "")},
    }
    if not (g_ebb and g_stock and g_inst_out):
        miss = [k for k, v in gate.items() if not v["ok"]]
        empty["gate"] = gate
        empty["reason"] = ("场景不成立（未同时满足：" +
                           "、".join({"ebb": "退潮期", "stock_game": "存量博弈",
                                      "inst_out": "机构净卖出"}[k] for k in miss) + "）")
        return empty

    # ---- 板块候选：主力净流入为正的板块 ----
    boards = [b for b in (flow.get("boards_all") or [])
              if _f(b.get("main")) > 0 and b.get("name")]
    if not boards:
        empty["gate"] = gate
        empty["reason"] = "场景成立，但本轮没有「主力净流入为正」的板块可承接"
        return empty

    max_pct = _f(a.get("max_pct"), 1.0)
    top_n = int(a.get("top_n", 3))
    min_score = _f(a.get("min_score"), 55.0)
    trial = _f(a.get("trial_weight"), 0.05)

    # 催化（财联社风口板块）+ 板块强弱定级（复用同一套匹配逻辑）
    try:
        winds = cls_mod.market_wind()
    except Exception:                                  # noqa: BLE001
        winds = []
    sec_items = sectors.get("items") or []
    main_theme = str(ml.get("theme") or "")

    def _role_of(name: str) -> tuple[str, str]:
        """返回 (角色, 强弱)。匹配不上就是未知 —— 不硬猜。"""
        if not sec_items:
            return "", ""
        try:
            hit = cls_mod.match_heat(name, sec_items)
        except Exception:                              # noqa: BLE001
            hit = None
        if not hit:
            return "", ""
        return str(hit.get("role") or ""), str(hit.get("level") or "")

    def _catalyst_of(name: str) -> str:
        if not winds:
            return ""
        try:
            hit = cls_mod.match_heat(name, winds)
        except Exception:                              # noqa: BLE001
            hit = None
        return str((hit or {}).get("catalyst") or "")

    # ★ 评分必须用**分位数（相对强度）**，不能用绝对阈值。
    #   实测（10-13 缩量退潮日）：主力净流入为正的板块有 17 个、涨幅又都在 ±1% 内，
    #   布尔式打分会让 17 个板块同分（80/70），前 3 名靠原顺序随便切 —— 等于没排序。
    #   分位数是自我校准的：不管今天是放量还是缩量，都能分出高低。
    min_main = _f(a.get("min_main"), 0.3)      # 低于此额（亿）视为噪音，不参与
    pos_days = int(a.get("pos_days", 3))       # 「位置」看近几个归档交易日
    boards = [b for b in boards if _f(b.get("main")) >= min_main]
    if not boards:
        empty["gate"] = gate
        empty["reason"] = (f"场景成立（机构净卖 {inst_net:+.2f} 亿），"
                           f"但没有板块的主力净流入达到 {min_main} 亿（都是噪音）")
        return empty
    mains = sorted(_f(b.get("main")) for b in boards)
    mpcts = sorted(_f(b.get("main_pct")) for b in boards)
    # 板块位置：近 N 个已归档交易日的累计涨幅（跌得多 = 位置低）
    cum_map = flow_mod.board_cum_pct(real_date, pos_days)

    def _rank(v: float, arr: list[float]) -> float:
        """v 在 arr 中的分位（0~1）。"""
        if not arr:
            return 0.5
        return sum(1 for x in arr if x <= v) / len(arr)

    out = []
    for b in boards:
        name = str(b.get("name"))
        pct = _f(b.get("pct"))
        main = _f(b.get("main"))
        main_pct = _f(b.get("main_pct"))
        streak = int(_f(b.get("streak"), 1))
        role, level = _role_of(name)
        catalyst = _catalyst_of(name)

        score = 10.0                       # 基础分：主力净流入为正
        tags: list[str] = []
        # ① 资金体量分位（22 分）
        score += 22 * _rank(main, mains)
        # ② 资金强度分位：主力净额占成交额比（18 分）
        score += 18 * _rank(main_pct, mpcts)
        # ③ 价格没抬起来的程度（20 分）—— 越没涨越像吸筹；跌着被买也拿满
        rise = max(0.0, min(1.0, pct / max_pct)) if max_pct > 0 else 1.0
        score += 20 * (1 - rise)
        if pct <= max_pct:
            tags.append(f"价没动·钱在进（{pct:+.2f}%）")
        else:
            tags.append(f"涨幅已大（{pct:+.2f}%）·不追")
        # ④ 首日净流入 = 资金刚动（对应体系那条"要信早信"）
        if streak <= 1:
            score += 12
            tags.append("首日净流入（资金刚动）")
        else:
            score += min(8.0, 3.0 * streak)
            tags.append(f"连续 {streak} 日净流入")
        # ⑤ 有催化（机构建仓需要理由）
        if catalyst:
            score += 15
            tags.append("有催化")
        # ⑥ 板块角色：支线才符合"小波段"定位
        if role == "支线":
            score += 10
            tags.append("支线（非主线 → 小波段定位）")
        elif role == "主线":
            score -= 15
            tags.append("已是主线·不追")
        if main_theme and name == main_theme:
            score -= 15
            tags.append("与当前主线同名·不追")
        if main_pct >= 2:
            tags.append(f"主力净额占成交 {main_pct:.1f}%")
        # ⑦ 板块位置：近 N 日跌得多 = 位置低（高位方向不追）
        cum = cum_map.get(name)                 # None = 没有归档，当中性
        if cum is not None:
            if cum <= 0:
                score += 15 * min(1.0, -cum / 3.0)
                tags.append(f"近{pos_days}日累计 {cum:+.1f}%（位置偏低）")
            else:
                score -= 5 * min(1.0, cum / 3.0)
                tags.append(f"近{pos_days}日累计 {cum:+.1f}%（已抬升）")
        score = max(0.0, min(100.0, score))

        if score < min_score:
            continue
        out.append({
            "name": name, "pct": round(pct, 2), "main": round(main, 2),
            "main_pct": round(main_pct, 2), "streak": streak,
            "cum": (round(cum, 2) if cum is not None else None),
            "lead": str(b.get("lead") or ""), "role": role, "level": level,
            "catalyst": catalyst, "score": round(score, 1), "tags": tags,
            "how": (f"不追高：等它回踩或横盘再看，先盯 {b.get('lead') or '板块龙头'} "
                    f"能不能续强；涨幅一旦超过 {max_pct:.0f}% 就不是吸筹是追涨了"),
            "size": f"试错仓（不超过 {trial*100:.1f} 成）—— 退潮期不做重仓",
        })

    # 同分时按主力净额兜底排序 —— 不能让"原顺序"决定谁上榜
    out.sort(key=lambda x: (-x["score"], -x["main"]))
    out = out[:top_n]

    if not out:
        empty["gate"] = gate
        empty["reason"] = (f"场景成立（机构净卖 {inst_net:+.2f} 亿），"
                           f"但没有板块同时满足「资金在进 + 位置没抬起来 + 有理由」")
        return empty

    names = "、".join(x["name"] for x in out)
    summary = (f"机构在高位兑现 {inst_net:+.2f} 亿（游资净买 {hot_net:+.2f} 亿），"
               f"退潮期 + 存量博弈 —— 钱要找「容量够大、位置够低、有理由」的方向承接。"
               f"本轮可看：{names}。注意这是**小波段**定位，用试错仓，不是主升。")

    return {
        "enabled": True, "active": True, "gate": gate, "reason": "",
        "candidates": out, "summary": summary,
        "note": ("本模块只回答「退潮期机构兑现的钱可能去哪」，是**提醒去看一眼**，"
                 "不是买入指令。三个闸门（退潮 + 存量 + 机构净卖）缺一不出，"
                 "出了也不代表方向一定起来 —— 同样条件下资金也可能直接离场观望。"),
    }
