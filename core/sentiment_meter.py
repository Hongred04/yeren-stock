# -*- coding: utf-8 -*-
"""E1 情绪温度计（扩展模块，非野人原体系）—— 把「连板梯队 / 炸板率 / 晋级率 /
涨跌家数」量化为一根 0-100 的温度柱，给 M3 情绪周期补上连续刻度。

灵感来源（GitHub 同类项目）：
  · 连板系列「情绪周期」文章：冰点→启动→发酵→高潮→退潮 六阶段，看四个指标
    ——连板高度、炸板率、连板梯队、赚钱效应；
  · slingshot：市场宽度（涨跌家数）作为情绪底座；
  · limit-up-dao 涨停归因模型：封板质量（首封时间 / 炸板次数 / 封单额）。

M3 给的是「能不能出手」的离散判断（退潮/非退潮），本模块给的是连续的
「今天多少度」，两者互补：温度骤降 + M3 退潮 = 更硬的离场信号。
"""
from __future__ import annotations

import json
import os

from . import datasource as ds
from . import paths
from . import stage_stabilizer as stab

HIST_PATH = paths.data_path("sentiment_history.json")

# 情绪阶段（六阶段口径）
STAGES = {
    "ice":      {"label": "冰点", "emoji": "🧊", "advice": "连板绝迹、谁板谁死 —— 空仓等启动信号，手痒打板=送钱"},
    "revive":   {"label": "修复·启动", "emoji": "🌱", "advice": "出现破局板、赚钱效应萌芽 —— 轻仓试错，只打最强"},
    "ferment":  {"label": "发酵", "emoji": "🔥", "advice": "梯队完整、溢价高 —— 最赚钱的阶段，重仓打板吃主升"},
    "climax":   {"label": "高潮", "emoji": "🌋", "advice": "龙头成妖、情绪最热 —— 持股为主、卖出为辅，最疯狂也最危险"},
    "ebb":      {"label": "退潮", "emoji": "🌊", "advice": "高位炸板、高度骤降 —— 空仓，别抄底、别幻想"},
}


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _i(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- 涨停梯队

def _ladder(zt: list[dict]) -> list[dict]:
    """连板梯队：按连板高度聚合，1 板在前。含封板质量（封成比、一字数）。"""
    groups: dict[int, dict] = {}
    for r in zt:
        b = max(_i(r.get("boards"), 1), 1)
        g = groups.setdefault(b, {"boards": b, "count": 0, "names": [],
                                  "one_word": 0, "_fund": 0.0, "_amt": 0.0})
        g["count"] += 1
        if len(g["names"]) < 6:
            g["names"].append(r.get("name", ""))
        ft = _i(r.get("first_time"))
        if 0 < ft < 93000:
            g["one_word"] += 1
        g["_fund"] += _f(r.get("fund"))
        g["_amt"] += _f(r.get("amount"))
    out = []
    for b, g in sorted(groups.items(), key=lambda kv: -kv[0]):
        ratio = (g["_fund"] / g["_amt"]) if g["_amt"] > 0 else 0.0
        out.append({"boards": b, "count": g["count"], "names": g["names"],
                    "one_word": g["one_word"], "seal_ratio": round(ratio, 2)})
    return out


def _prev_pool(real_date: str) -> tuple[str, list[dict]]:
    """real_date 之前的最近一个已归档涨停池。"""
    dates = [d for d in ds.pool_history_dates() if not real_date or d < real_date]
    if not dates:
        return "", []
    return dates[-1], ds.load_pool_history(dates[-1])


# ---------------------------------------------------------------- 温度判定

def _temperature(limit_up: int, max_boards: int, broken_ratio: float,
                 promote_rate: float | None, up_ratio: float | None) -> int:
    """0-100 情绪温度。四项加权：涨停家数30 / 连板高度25 / 炸板率20 / 晋级率25。"""
    # ① 涨停家数（0-30）
    if limit_up >= 100:
        t1 = 30
    elif limit_up >= 70:
        t1 = 25
    elif limit_up >= 50:
        t1 = 20
    elif limit_up >= 30:
        t1 = 14
    elif limit_up >= 15:
        t1 = 8
    else:
        t1 = 4
    # ② 连板高度（0-25）
    if max_boards >= 7:
        t2 = 25
    elif max_boards >= 6:
        t2 = 22
    elif max_boards >= 5:
        t2 = 19
    elif max_boards >= 4:
        t2 = 14
    elif max_boards >= 3:
        t2 = 9
    else:
        t2 = 3
    # ③ 炸板率（0-20）：低=情绪好
    if broken_ratio < 0.15:
        t3 = 20
    elif broken_ratio < 0.25:
        t3 = 14
    elif broken_ratio < 0.35:
        t3 = 8
    else:
        t3 = 3
    # ④ 晋级率 / 赚钱效应（0-25）：无昨日数据时用涨跌家数比代替
    if promote_rate is not None:
        if promote_rate >= 0.30:
            t4 = 25
        elif promote_rate >= 0.20:
            t4 = 18
        elif promote_rate >= 0.10:
            t4 = 10
        else:
            t4 = 4
    else:
        ur = up_ratio if up_ratio is not None else 0.5
        t4 = int(max(0.0, min(1.0, (ur - 0.35) / 0.35)) * 22) + 3
    return int(max(0, min(100, t1 + t2 + t3 + t4)))


def _stage_of(temp: int, prev_temp: int | None = None) -> str:
    """按温度分 0-100 的非退潮档位：climax / ferment / revive / ice。

    ★ 只分**非退潮档位**：退潮与否以 M3 稳定器的确认为准（build() 里的 peek
    覆盖）—— 原始亏钱面积、温度骤降都只是**预警信号**，不在这里升级成"退潮"
    阶段。v1.25.2 修：本函数曾收 ebb 入参、拿原始亏钱面积（hurt_area ≥ 阈值，
    M3 口径）和「温度骤降 18°」直接 return "ebb"，与上述规格字面相悖 ——
    M3 未经滞回确认（结论卡"情绪高潮·可以出手"）时 E1 已报"退潮"，
    同屏两把尺子对不上（用户 10-12 实测「自相矛盾」的另一半）。
    """
    if temp >= 80:
        return "climax"
    if temp >= 62:
        return "ferment"
    if temp >= 42:
        return "revive"
    return "ice"


# ---------------------------------------------------------------- 历史存档

def _load_hist() -> list[dict]:
    try:
        with open(HIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _save_hist(real_date: str, row: dict) -> list[dict]:
    hist = _load_hist()
    hist = [h for h in hist if h.get("date") != real_date]
    hist.append({"date": real_date, **row})
    hist.sort(key=lambda x: x["date"])
    # 上限 260：归档回补后有一年的数据，留 120 会让重建的历史被实时扫描
    # 逐步挤掉（每天新增 1 条、挤掉 1 条，几个月后又回到只有 120 天）。
    hist = hist[-260:]
    try:
        os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
        os.replace(tmp, HIST_PATH)
    except OSError:
        pass
    return hist


# ---------------------------------------------------------------- 入口

def _space_board(real_date: str, max_boards: int,
                 days: int = 20) -> dict:
    """空间板检测 —— 野人：「空间板打开之后，后面的短线生态就好做了」。

    空间板 = 场内资金用真金白银把高度打上去（试探监管 + 打开天花板）。
    口径：今日最高连板 vs 近 N 个交易日（不含今日）的最高连板——
    突破 = 空间打开（短线敢做的高度出来了）；未突破 = 高度压制
    （谁板谁死，做多高度没打开）。只读本地归档，零额外请求。
    """
    try:
        dates = [d for d in ds.pool_history_dates() if d < real_date][-days:]
        best, best_d = 0, ""
        for d in dates:
            pool = ds.load_pool_history(d) or []
            mb = max((int(r.get("boards") or 1) for r in pool), default=0)
            if mb > best:
                best, best_d = mb, d
        if not best or not max_boards:
            return {}
        broke = max_boards > best
        return {
            "today": max_boards, "prev20": best, "prev_date": best_d,
            "open": broke,
            "note": (f"空间打开：今日最高 {max_boards} 板，突破近20日前高 {best} 板"
                     f"（{best_d}）—— 短线生态转好，敢做的高度出来了"
                     if broke else
                     f"高度压制：今日最高 {max_boards} 板，近20日前高 {best} 板"
                     f"（{best_d}）—— 空间没打开之前，别把预期打太高"),
        }
    except Exception:
        return {}


def build(zt: list[dict], dt: list[dict], zb: list[dict], snaps: dict,
          real_date: str, cfg: dict | None = None) -> dict:
    """构建情绪温度计。zt/dt/zb 为涨停/跌停/炸板池，snaps 为全市场快照。"""
    a = (cfg or {}).get("E1_meter", {})
    if not a.get("enabled", True):
        return {"enabled": False, "temperature": 0, "stage": "disabled",
                "label": "模块已关闭", "advice": "", "ladder": [], "metrics": {}}

    n_zt, n_dt, n_zb = len(zt), len(dt), len(zb)
    ladder = _ladder(zt)
    max_boards = max((b["boards"] for b in ladder), default=0)
    seal_pool = n_zt + n_zb
    broken_ratio = (n_zb / seal_pool) if seal_pool else 0.0
    seal_rate = (n_zt / seal_pool) if seal_pool else 0.0

    # 昨日涨停池 → 晋级率 / 赚钱效应（打板承接）
    # 优先用东财「昨日涨停池+今日表现」：独立口径、不依赖本地归档与全市场
    # 快照 —— 归档缺档/冷启动时它照样有数，打板溢价不再显示"待积累"。
    # 全零涨幅视为无效（盘前当日数据未生成），退回本地算法。
    pdate, ppool = _prev_pool(real_date)
    promote_rate = None
    money_effect = None
    height_premium = None
    premium_src = ""
    yzt = ds.fetch_yesterday_zt_pool()
    if yzt and any(_f(r.get("pct")) != 0 for r in yzt):
        today_zt = {r.get("code") for r in zt}
        prem = [_f(r.get("pct")) for r in yzt]
        money_effect = round(sum(prem) / len(prem), 2)
        alive = sum(1 for r in yzt
                    if _f(r.get("pct")) >= 9.5 or r.get("code") in today_zt)
        promote_rate = round(alive / len(yzt), 3)
        hs = [_f(r.get("pct")) for r in yzt if _i(r.get("boards_y"), 1) >= 2]
        if hs:
            height_premium = round(sum(hs) / len(hs), 2)
        premium_src = "em"
    elif ppool and snaps:
        today_zt = {r.get("code") for r in zt}
        alive = 0
        prem_sum = 0.0
        prem_cnt = 0
        h_sum = 0.0
        h_cnt = 0
        for r in ppool:
            snap = snaps.get(r.get("code"))
            if not snap:
                continue
            p = _f(snap.get("pct"))
            prem_sum += p
            prem_cnt += 1
            if r.get("code") in today_zt or p >= 9.5:
                alive += 1
            if _i(r.get("boards"), 1) >= 2:
                h_sum += p
                h_cnt += 1
        if prem_cnt:
            promote_rate = round(alive / len(ppool), 3)
            money_effect = round(prem_sum / prem_cnt, 2)
        if h_cnt:
            height_premium = round(h_sum / h_cnt, 2)
        premium_src = "local" if prem_cnt else ""

    # ★ 优先用**已经在手的全市场快照**自算涨跌家数（零网络、零耗时）。
    #   以前这里是无条件 fetch_breadth()：push2 被封时要 3.01 秒才降级到
    #   开盘红，而那个数与本函数手上的 snaps 数出来**完全一样**（实测连读
    #   三次都是 978/4496/699）。快照为空（冷启动/盘前）时才走网络降级链。
    breadth = ds.fetch_breadth(snaps=snaps) if snaps else ds.fetch_breadth()
    up_ratio = breadth.get("up_ratio") if breadth else None

    temp = _temperature(n_zt, max_boards, broken_ratio, promote_rate, up_ratio)

    # 亏钱面积（事实数据，进 metrics）。退潮与否**只由 M3 稳定器确认**（见下方
    # peek 覆盖）—— E1 不自己拿原始面积强置"退潮"（v1.25.2 修：曾与自身注释相悖）。
    total3 = n_zt + n_dt + n_zb
    hurt_area = (n_dt + n_zb) / total3 if total3 else 0.0

    # 前一日温度（较上日变化 / 骤降预警信号用）
    hist = _load_hist()
    prev_temp = None
    for h in reversed(hist):
        if h.get("date", "") < real_date:
            prev_temp = h.get("temperature")
            break

    stage = _stage_of(temp, prev_temp)
    # 退潮与否**以 M3 稳定器的确认为准**（E1 只用自己的温度决定非退潮时的档位）。
    # 两个页面各报一个阶段是"一会能出手一会退潮"的另一个来源，这里统一口径。
    v = stab.peek(real_date)
    if v["confirmed"]:
        stage = "ebb"
    info = STAGES.get(stage, STAGES["revive"])

    # 一字板家数 / 平均封成比
    one_word = sum(1 for r in zt if 0 < _i(r.get("first_time")) < 93000)
    fund_sum = sum(_f(r.get("fund")) for r in zt)
    amt_sum = sum(_f(r.get("amount")) for r in zt)
    avg_seal_ratio = round(fund_sum / amt_sum, 2) if amt_sum > 0 else 0.0

    # 温度骤降预警：只作**事实信号**进 metrics（供界面将来做「较昨日 -18°」提示），
    # 不升级成"退潮"阶段 —— 退潮判定权归 M3 稳定器（v1.25.2）
    drop_alert = bool(prev_temp is not None and prev_temp - temp >= 18 and prev_temp >= 60)
    metrics = {
        "limit_up": n_zt, "limit_down": n_dt, "broken": n_zb,
        "seal_rate": round(seal_rate, 3), "broken_ratio": round(broken_ratio, 3),
        "max_boards": max_boards, "one_word": one_word,
        "hurt_area": round(hurt_area, 3),
        "promote_rate": promote_rate, "money_effect": money_effect,
        "height_premium": height_premium, "avg_seal_ratio": avg_seal_ratio,
        "premium_src": premium_src,       # em=东财昨日涨停池 / local=本地归档
        "breadth": breadth, "prev_pool_date": pdate, "prev_temp": prev_temp,
        "drop_alert": drop_alert,
    }
    delta = (temp - prev_temp) if prev_temp is not None else None

    # 落盘（同一交易日复算覆盖，形成温度曲线）
    _save_hist(real_date, {
        "temperature": temp, "stage": stage, "limit_up": n_zt,
        "limit_down": n_dt, "broken": n_zb, "max_boards": max_boards,
        "promote_rate": promote_rate, "broken_ratio": round(broken_ratio, 3),
    })
    # ★ 曲线上限从 30 提到 120：归档回补后有近一年的数据（243 天），
    #   只画 30 天等于把补来的历史又丢掉。前端按需截取，这里给全。
    #   src 字段透传（"rebuild" = 由归档重建、缺炸板率，前端据此标注）。
    heat_series = [{"date": h.get("date"), "temperature": h.get("temperature"),
                    "limit_up": h.get("limit_up"), "max_boards": h.get("max_boards"),
                    "src": h.get("src") or "live"}
                   for h in _load_hist()][-120:]

    return {
        "enabled": True, "temperature": temp, "stage": stage,
        "label": f"{info['emoji']} {info['label']}", "advice": info["advice"],
        "delta": delta, "ladder": ladder, "metrics": metrics,
        "space": _space_board(real_date, max_boards),
        "heat_series": heat_series,
        "note": "温度 = 涨停家数30 + 连板高度25 + 炸板率20 + 晋级率/宽度25；"
                "M3 判「能不能出手」，温度计看「今天多少度」",
    }
