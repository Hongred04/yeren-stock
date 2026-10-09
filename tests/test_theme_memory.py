# -*- coding: utf-8 -*-
"""题材记忆测试 —— 电池 09-30→10-08→10-09 的教训。

要点：
  · 活跃榜前 3（涨停≥2家）都能被记住，不能只记每天的第一名；
  · 连续爬坡（第3→第2→第1）识别为「已连续 N 日活跃」，不能报「新题材首日」；
  · 昨日活跃但非第一名 → 基准用该题材自己的昨日涨停池（延续口径）；
  · 真·新题材仍然报「新题材首日」。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="theme_mem_")

from core import datasource as ds      # noqa: E402
from core import mainline              # noqa: E402
from core import strategy_config as sc  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def zt(code, name, sector, boards=1, first_time=93000, amount=1e8):
    return {"code": code, "name": name, "sector": sector, "boards": boards,
            "first_time": first_time, "amount": amount, "pct": 10.0,
            "float_mv": 5e9, "turnover": 5.0}


def main():
    # 造三段历史：电池 09-30 第3名 → 10-08 第2名 → 10-09 第1名（爬坡型）
    p0930 = ([zt(f"sz00010{i}", f"甲{i}", "汽车零部", 2) for i in range(5)]
             + [zt(f"sz00020{i}", f"乙{i}", "化学制品", 2) for i in range(4)]
             + [zt(f"sz00030{i}", f"丙{i}", "电池", 3) for i in range(3)])
    p1008 = ([zt(f"sz00040{i}", f"丁{i}", "化学制药", 1) for i in range(4)]
             + [zt(f"sz00050{i}", f"戊{i}", "电池", 3 + i) for i in range(3)]
             + [zt(f"sz00060{i}", f"己{i}", "化学制品", 2) for i in range(3)])
    today = ([zt(f"sz00050{i}", f"戊{i}", "电池", 4 + i) for i in range(3)]
             + [zt(f"sz00055{i}", f"戊后{i}", "电池", 1) for i in range(3)]
             + [zt(f"sz00070{i}", f"庚{i}", "出版", 9) for i in range(5)])
    ds.save_pool_history("20260930", p0930)
    ds.save_pool_history("20261008", p1008)
    ds.save_pool_history("20261009", today)

    cfg = sc.default_config()
    a = cfg["M9_mainline"]
    themes = mainline._cluster_themes(today, 103000)
    theme = themes[0]["sector"] if themes else ""
    ok(theme == "电池", f"今日主题材=电池（实际 {theme}）")

    # 快照：昨日电池股今日全部高开 +10%，其余平开
    snaps = {}
    for r in p1008:
        snaps[r["code"]] = {"prev_close": 10.0, "open": 11.0,
                            "pct": 9.9 if r["sector"] == "电池" else 1.0}
    for r in today:
        snaps.setdefault(r["code"], {"prev_close": 10.0, "open": 10.2, "pct": 10.0})

    print("[1] 连续爬坡题材：识别为延续，不是首日")
    s5, day2 = mainline._signal_next_day(a, "20261009", snaps, today, "电池", themes)
    ok(day2.get("theme_match") is True, "命中昨日活跃榜（theme_match=True）")
    ok(day2.get("return_day") is False, "不是回归日（昨天就在活跃）")
    ok(day2.get("streak") == 3, f"连续活跃 3 日（实际 {day2.get('streak')}）")
    ok(day2.get("streak_since") == "20260930", f"起始 09-30（实际 {day2.get('streak_since')}）")
    ok(day2.get("base_kind") == "该题材昨日涨停池", f"基准=该题材昨日池（{day2.get('base_kind')}）")
    ok("连续 3 日活跃" in s5["evidence"] and "不是首日" in " ".join(s5["detail"]),
       "证据里写明「连续 3 日活跃、不是首日」")
    tk = day2.get("theme_track") or []
    ok([x["rank"] for x in tk] == [3, 2, 1], f"轨迹 第3→第2→第1（实际 {[x['rank'] for x in tk]}）")
    ok(s5["state"] in ("pass", "fail"), f"走上延续检验分支（state={s5['state']}）")

    print("\n[2] 真·新题材：仍然报首日")
    today2 = [zt(f"sz00080{i}", f"辛{i}", "量子通信", 1 + i) for i in range(4)]
    snaps2 = {r["code"]: {"prev_close": 10.0, "open": 10.1, "pct": 10.0} for r in today2}
    # 昨日池也给出快照，保证延续检验有数据、能走到「新题材」判定
    for r in p1008:
        snaps2[r["code"]] = {"prev_close": 10.0, "open": 10.0,
                             "pct": 1.0 if r["sector"] != "电池" else 8.0}
    th2 = mainline._cluster_themes(today2, 103000)
    s5b, d2b = mainline._signal_next_day(a, "20261009", snaps2, today2, "量子通信", th2)
    ok(s5b["state"] == "pending", f"新题材 → pending（实际 {s5b['state']}）")
    ok("新题材首日" in s5b["evidence"], "证据明说「新题材首日」")
    ok(d2b.get("streak") == 1, f"新题材连续天数=1（实际 {d2b.get('streak')}）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
