# -*- coding: utf-8 -*-
"""用 243 天归档对系统真实因子做区分度检验（G1–G4）。

为什么不能只用 journal
----------------------
signal_journal.json 只有 166 条、其中已结算 72 条 —— 远低于
discrimination.MIN_TOTAL(40)×5档 的量级，做出来的 lift 不可信。
回补了 243 天归档之后，可以自己造一个**受控样本**：
  · 每天取涨停池里的票（B1 首板/B2 连板的近似）；
  · 用当天就能算出来的量做因子（连板数、封板时间、成交额、题材热度…）；
  · 结果 = 次日是否再涨停 / 次日收益 —— 用归档里次一日的池子判定。
这不能替代真回测（没有逐笔、没有盘中），但足以回答
「这个因子分不分得开」这个结构性问题。
用法
----
    python check_signals.py              # 检验内置的 7 个因子
    python check_signals.py --bins 4     # 改档数
    python check_signals.py --detail     # 额外打印逐档明细

结论怎么读
----------
  「可用」    —— 四检全过。这个因子在你的样本里真的有区分度。
  「塌缩」    —— 分数挤在最高档（同类项目 qscore 实测 82.7%）。
  「切不开」  —— 档位太少或某一档独占（如连板数：1 板占 74%）。
  「方向反了」—— 单调但方向为负。**这不是没用**，是应该用负权重。
  「方向不一致」—— 忽高忽低，落在噪音区间，才是真的没用。
  「无区分度」—— 顶档并不比底档好。
  「样本不足」—— 低于 MIN_TOTAL，不下结论。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import datasource as ds          # noqa: E402
from core import discrimination as dc      # noqa: E402


def build_rows(dates, pools, codes_on):
    """构造受控样本：每个交易日涨停池里的每只票 + 当天可知的因子。"""
    rows = []
    for i in range(len(dates) - 1):
        d, nd = dates[i], dates[i + 1]
        pool = pools[d]
        if not pool:
            continue
        sec_n = Counter(str(r.get("sector") or "") for r in pool)
        for r in pool:
            rows.append({
                "date": d, "code": r["code"],
                "boards": max(int(r.get("boards") or 1), 1),
                "sector_n": sec_n.get(str(r.get("sector") or ""), 1),
                "fbt": int(r.get("first_time") or 0),
                "amount": float(r.get("amount") or 0),
                "float_mv": float(r.get("float_mv") or 0),
                "turnover": float(r.get("turnover") or 0),
                "fund": float(r.get("fund") or 0),
                # 结果 = 次日是否再涨停。
                # ★ 如实声明：归档无价格，这不是收益率而是二值结果，
                #   所以 G3 的 lift 读作「顶档/底档的再涨停率之比」。
                "ret": 1.0 if r["code"] in codes_on.get(nd, set()) else 0.0,
            })
    return rows


def _fmt(v):
    """档位边界的人类可读格式 —— 原始科学计数法看不懂。"""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v/1e8:.1f}亿"
    if a >= 1e4:
        return f"{v/1e4:.0f}万"
    if a >= 100:
        return f"{v:.0f}"
    return f"{v:.2f}"


# 待检因子。每个都必须「当下算分时就能拿到」，否则就是未来函数。
FACTORS = [
    ("连板数", lambda r: r["boards"]),
    ("封板时间", lambda r: -r["fbt"] if r["fbt"] else None),   # 越早封分越高
    ("当日成交额", lambda r: r["amount"] or None),
    ("流通市值", lambda r: r["float_mv"] or None),
    ("换手率", lambda r: r["turnover"] or None),
    ("封单额", lambda r: r["fund"] or None),
    ("题材热度", lambda r: r["sector_n"]),
]


def main() -> int:
    args = sys.argv[1:]
    bins = 5
    if "--bins" in args:
        try:
            bins = int(args[args.index("--bins") + 1])
        except (IndexError, ValueError):
            print("--bins 后面要跟一个整数")
            return 2
    detail = "--detail" in args

    dates = sorted(ds.pool_history_dates())
    if len(dates) < 5:
        print(f"归档只有 {len(dates)} 天，先跑 backfill_history.py 补历史。")
        return 1
    pools = {d: ds.load_pool_history(d) or [] for d in dates}
    codes_on = {d: {r["code"] for r in pools[d]} for d in dates}
    print(f"归档 {len(dates)} 天：{dates[0]} ~ {dates[-1]}")

    rows = build_rows(dates, pools, codes_on)
    if not rows:
        print("没构造出样本（归档里没有涨停池）。")
        return 1
    base = sum(r["ret"] for r in rows) / len(rows)
    print(f"受控样本 {len(rows)} 条（{len(dates)-1} 个交易日 × 当日涨停池）")
    print(f"样本基线：次日再涨停率 {base*100:.1f}%\n")

    items = []
    for name, fn in FACTORS:
        sub = [{"score": fn(r), "ret": r["ret"]}
               for r in rows if fn(r) is not None]
        items.append({"name": name, "rows": sub, "bins": bins})
    res = dc.report(items)

    print("=" * 76)
    print(f"{'因子':12s} {'样本':>6s} {'顶档占比':>8s} {'lift':>7s} "
          f"{'秩相关':>7s}  结论")
    print("=" * 76)
    for it in res["items"]:
        lift = it.get("g3_lift")
        rho = it.get("g4_rho")
        print(f"{it['name']:12s} {it['n']:6d} "
              f"{it.get('g1_top_share', 0)*100:7.0f}% "
              f"{(f'{lift:.2f}x' if lift else '—'):>7s} "
              f"{(f'{rho:+.2f}' if rho is not None else '—'):>7s}  "
              f"{it['verdict']}")

    if detail:
        print()
        print("=" * 76)
        print("逐档明细（每档的次日再涨停率）")
        print("=" * 76)
        for it in res["items"]:
            if it["n"] < dc.MIN_TOTAL:
                continue
            print(f"\n{it['name']}  —— {it['verdict']}")
            for b in it["bins"]:
                if b["n"] == 0 or b["mean"] is None:
                    continue
                bar = "█" * int((b["mean"] or 0) * 60)
                print(f"  档{b['bin']}  [{_fmt(b['lo']):>8s} ~ {_fmt(b['hi']):>8s}]"
                      f"  n={b['n']:5d}  {b['mean']*100:5.1f}%  {bar}")

    # 汇总：谁不可用 —— 这是跑这个工具的真正目的
    print()
    bad = [it for it in res["items"]
           if it["n"] >= dc.MIN_TOTAL and not it["usable"]]
    if bad:
        print(f"★ {len(bad)} 个因子没过检验（共 {res['n_tested']} 个）：")
        for it in bad:
            print(f"    {it['name']}：{it['verdict']}")
    else:
        print("★ 全部因子四检全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
