# -*- coding: utf-8 -*-
"""从回补的归档重建温度曲线（sentiment_history.json）。

为什么需要单独做这一步
--------------------
回补归档（pool_history/）只让「温度曲线」的下游有料，但温度曲线本身读的是
另一个文件 sentiment_history.json —— 它只在**实时扫描时**写入。所以：
  · pool_history 有 243 天 → 题材生命周期、空间板、昨日池全都活了；
  · sentiment_history 仍然只有 5 天 → 情绪温度计上的曲线还是画不满。

本脚本用同样口径把这 243 天补进 sentiment_history.json。

★ 必须如实声明的重建边界
------------------------
温度 = 涨停家数 30 + 连板高度 25 + 炸板率 20 + 晋级率 25。
  · 涨停家数  → 可重建（归档行数 或 history_emotion 的 limit_up）；
  · 连板高度  → 可重建（归档 max(boards)）；
  · 炸板率    → **无法重建**（历史归档里没有炸板池），用中性档 14 分估；
  · 晋级率    → **可重建**（用「今天涨停里昨天也涨停」的比例 —— 与实盘
                 口径的差异写在下面，属同量纲近似）。
所以重建出来的温度**不是**当时的实盘温度，而是同口径下的近似值。
为避免污染真实数据，写进 sentiment_history.json 的每条都带
"src":"rebuild" 标记，前端与后续逻辑可以据此区分。

用法
----
    python rebuild_heat.py            # 预览，不写
    python rebuild_heat.py --write    # 真写
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import datasource as ds               # noqa: E402
from core import kph                            # noqa: E402
from core import paths                          # noqa: E402

HIST_PATH = paths.data_path("sentiment_history.json")
NEUTRAL_BROKEN = 0.20        # 炸板率中性档（<0.25 → 14 分）


def temp_of(limit_up, max_boards, broken_ratio, promote_rate):
    """与 core/sentiment_meter.py:_temperature 逐档一致（改那边要同步改这里）。"""
    if limit_up >= 100: t1 = 30
    elif limit_up >= 70: t1 = 25
    elif limit_up >= 50: t1 = 20
    elif limit_up >= 30: t1 = 14
    elif limit_up >= 15: t1 = 8
    else: t1 = 4
    if max_boards >= 7: t2 = 25
    elif max_boards >= 6: t2 = 22
    elif max_boards >= 5: t2 = 19
    elif max_boards >= 4: t2 = 14
    elif max_boards >= 3: t2 = 9
    else: t2 = 3
    if broken_ratio < 0.15: t3 = 20
    elif broken_ratio < 0.25: t3 = 14
    elif broken_ratio < 0.35: t3 = 8
    else: t3 = 3
    if promote_rate is not None:
        if promote_rate >= 0.30: t4 = 25
        elif promote_rate >= 0.20: t4 = 18
        elif promote_rate >= 0.10: t4 = 10
        else: t4 = 4
    else:
        t4 = 14
    return int(max(0, min(100, t1 + t2 + t3 + t4)))


def stage_of(temp, prev):
    """与 sentiment_meter.py:_stage_of 一致（退潮另由 M3 稳定器覆盖，属实时概念）。"""
    if prev is None:
        return "unknown"
    if temp >= 80: return "climax"
    if temp <= 25: return "ebb"
    if temp >= prev + 10: return "revive"
    if temp <= prev - 10: return "ebb"
    return "follow"


def main() -> int:
    write = "--write" in sys.argv
    dates = sorted(ds.pool_history_dates())
    if not dates:
        print("没有归档，先跑 backfill_history.py")
        return 1
    pools = {d: ds.load_pool_history(d) or [] for d in dates}
    codes = {d: {r["code"] for r in pools[d]} for d in dates}

    rows = []
    prev_temp = None
    for i, d in enumerate(dates):
        pool = pools[d]
        # 需要「前一交易日」算晋级率：涨停里有多少昨天也涨停
        promote = None
        if i > 0:
            prev_codes = codes[dates[i - 1]]
            if prev_codes:
                alive = len([c for c in prev_codes if c in codes[d]])
                promote = round(alive / len(prev_codes), 3)
        max_boards = max((int(r.get("boards") or 1) for r in pool), default=0)
        # 涨停家数：用 history_emotion 的收盘口径（含一字板），失败退回归档行数
        em = kph.history_emotion(d)
        limit_up = int(em.get("limit_up") or 0) or len(pool)
        t = temp_of(limit_up, max_boards, NEUTRAL_BROKEN, promote)
        rows.append({
            "date": d, "temperature": t, "stage": stage_of(t, prev_temp),
            "limit_up": limit_up, "limit_down": int(em.get("limit_down") or 0),
            "broken": 0, "max_boards": max_boards,
            "promote_rate": promote, "broken_ratio": NEUTRAL_BROKEN,
            "src": "rebuild",
        })
        prev_temp = t

    temps = [r["temperature"] for r in rows]
    print(f"重建 {len(rows)} 天：{rows[0]['date']} ~ {rows[-1]['date']}")
    print(f"温度 最低 {min(temps)} / 最高 {max(temps)} / 均值 {sum(temps)/len(temps):.1f}")
    有晋 = sum(1 for r in rows if r["promote_rate"] is not None)
    print(f"晋级率可算 {有晋} 天；炸板率缺失，统一用中性档 {NEUTRAL_BROKEN}（14 分）")

    # 现有真实记录
    old = []
    try:
        with io.open(HIST_PATH, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    real = [h for h in old if h.get("src") != "rebuild"]
    print(f"\n现有记录 {len(old)} 条，其中实盘 {len(real)} 条："
          f"{[h.get('date') for h in real]}")

    merged = {h["date"]: h for h in rows}
    for h in real:                       # 实盘记录优先，绝不覆盖
        merged[h["date"]] = h
    out = sorted(merged.values(), key=lambda x: x["date"])
    print(f"合并后 {len(out)} 条（实盘 {len(real)} 条原样保留）")
    if not write:
        print("\n（预览模式，未写入。加 --write 真写）")
        return 0
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    tmp = HIST_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, HIST_PATH)
    print(f"\n已写入 {HIST_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
