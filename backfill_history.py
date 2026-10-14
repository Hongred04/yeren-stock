# -*- coding: utf-8 -*-
"""一次性回补 250 天涨停池归档（用户 m03100「按照推荐的策略来」）。

为什么需要
----------
档案是系统上线后才开始记的，目前只有 18 个交易日。后果：
  · 温度曲线要 30 天窗口，画不满；
  · 234 个题材标签里 80 个（34%）只出现 1 次 → 判不出「新播种」还是「老题材再启动」；
  · journal 的 ret5 全是 null → 5 日胜率算不出来；
  · 那个「补涨假设」的回测只用 18 天就下了结论 —— 样本太薄。

本脚本复用 core/kph.py 的 history_pool（任意历史日的收盘复盘口径，含一字板），
逐日写入 pool_history/<date>.json，格式与本系统自己的归档完全同构。

用法
----
    python backfill_history.py                 # 默认补 365 个自然日
    python backfill_history.py 500             # 补 500 个自然日
    python backfill_history.py --dry           # 只报缺哪几天，不抓

已存在的日期**默认跳过**（不覆盖）。要强制重写某段，用 --force。
"""
from __future__ import annotations

import io
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import datasource as ds      # noqa: E402
from core import kph                   # noqa: E402


def workdays_before(real_date: str, days: int) -> list[str]:
    """real_date 之前的 days 个自然日窗口里的工作日，旧→新（便于看进度）。"""
    try:
        end = datetime.strptime(real_date, "%Y%m%d").date()
    except ValueError:
        return []
    out: list[str] = []
    d = end - timedelta(days=days)
    while d < end:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:]]
    dry = "--dry" in args
    force = "--force" in args
    args = [a for a in args if not a.startswith("--")]
    days = int(args[0]) if args and args[0].isdigit() else 365

    # 以「本地档案里最新的一天」为基准，而不是今天 —— 服务器可能停在周末
    have = sorted(ds.pool_history_dates())
    if not have:
        print("本地还没有任何归档，先用一次正常扫描生成今天这份再回来。")
        return 1
    anchor = have[-1]
    print(f"本地归档 {len(have)} 天：{have[0]} ~ {anchor}")
    print(f"回补窗口：{anchor} 往前 {days} 个自然日\n")

    want = workdays_before(anchor, days)
    todo = [d for d in want if force or d not in set(have)]
    print(f"窗口内工作日 {len(want)} 天，需要回补 {len(todo)} 天"
          f"（已有 {len(want) - len(todo)} 天）")
    if dry:
        print(" ".join(todo))
        return 0
    if not todo:
        print("没有要补的，收工。")
        return 0

    t0 = time.time()
    filled: list[str] = []
    empty: list[str] = []
    errs = 0
    total_rows = 0
    for i, d in enumerate(todo, 1):
        try:
            pool = kph.history_pool(d)
        except Exception as e:                                   # noqa: BLE001
            pool = []
            errs += 1
            print(f"  !! {d} 异常：{type(e).__name__}: {e}")
        if pool:
            ok = ds.save_pool_history(d, pool, force=True)
            filled.append(d)
            total_rows += len(pool)
            tag = "" if ok else " (写入失败)"
            print(f"  [{i}/{len(todo)}] {d}  {len(pool):3d} 只{tag}")
        else:
            empty.append(d)
            print(f"  [{i}/{len(todo)}] {d}  —  （非交易日或接口无数据）")
    dt = time.time() - t0
    print(f"\n完成：补上 {len(filled)} 天 / 共 {total_rows} 行，"
          f"空 {len(empty)} 天，异常 {errs} 次，用时 {dt:.1f}s")
    now = sorted(ds.pool_history_dates())
    print(f"归档现在 {len(now)} 天：{now[0]} ~ {now[-1]}")
    if len(now) > 1:
        print(f"平均每天 {total_rows / max(len(filled), 1):.0f} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
