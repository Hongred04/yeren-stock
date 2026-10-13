# -*- coding: utf-8 -*-
"""涨停池归档的完整性守卫（2026-10-13 实测的三条缺陷的回归护栏）。

实测到的三个问题（全部可复核）：
  ① **错日期**：本地 `pool_history/20261013.json` 的 46 只代码集合与「复盘接口
     查 20261012」**完全相同**，而且里面含**当天已停牌的新华传媒** ——
     它不可能出现在今天的涨停池里。根因是池接口**忽略 date 入参**、
     始终返回"最近一个有数据的交易日"，盘前扫描拿到的其实是上一交易日的池。
  ② **残缺被钉死**：`20261012` 只存了 16 只（当日早扫的部分池），当天真实
     涨停 46 只；而 `save_pool_history` 是"已有就不覆盖"，`autobackfill`
     是"只补缺"—— 错值再也修不回来。
  ③ **涨停家数当日首扫定稿**：`zt_history[20261012] = 16`，而收盘是 46。
     这条直接污染情绪周期的"涨停家数曲线"。

另外：覆盖时必须**保留东财那份的字段值** —— 复盘接口的 `break_count` 恒为 0、
换手/流通市值大 ~3.08 倍（实测九阳 10-09：复盘 56.29% / 41.2 亿 vs 东财
18.28% / 126.8 亿，用 K 线独立核对 139.2 万手 ÷ 7.62 亿股 = 18.3%，东财对）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="poolarch_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds            # noqa: E402
from core import kph                          # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def rows(*codes):
    return [{"code": c, "name": c, "boards": 1, "first_time": 93000,
             "break_count": 0, "turnover": 5.0, "float_mv": 1e9} for c in codes]


def zt_path():
    return os.path.join(_TMP, "zt_history.json")


def main():
    print("[1] 日期归属：拿到别的交易日的池就不该落盘")
    ok(ds.save_pool_history("20261013", rows("sh600001", "sh600002"),
                            qdate="20261012") is False,
       "qdate=10-12 却要写成 10-13 → 拒写（这正是「把昨天记成今天」的坑）")
    ok(ds.load_pool_history("20261013") == [], "拒写后磁盘上没有这个文件")
    ok(ds.save_pool_history("20261013", rows("sh600001", "sh600002"),
                            qdate="20261013") is True,
       "qdate 一致 → 正常写入")
    ok(len(ds.load_pool_history("20261013")) == 2, "内容落盘正确")

    print("\n[2] 与上一交易日完全同批 → 判为错日期的池")
    ok(ds.save_pool_history("20261012", rows("sh600003", "sh600004"),
                            qdate="20261012") is True, "先写 10-12 的正常归档")
    ok(ds.pool_history_prev_date("20261013") == "20261012",
       "prev_date 取最近一个更早的归档（10-12）")
    ok(ds.pool_history_duplicates("20261013", rows("sh600003", "sh600004")) is True,
       "新池与上一交易日代码集合完全相同 → duplicates=True")
    ok(ds.save_pool_history("20261013", rows("sh600003", "sh600004"),
                            qdate="20261013") is False,
       "错日期的池被拒写（这正是 10-13 装成 10-12 池的那种情况）")
    ok(ds.pool_history_duplicates("20261013", rows("sh600003", "sh999999")) is False,
       "只有部分重叠 → 不算重复")

    print("\n[3] 覆盖率：更完整才允许覆盖（自愈残缺），更少则保持原样")
    ds.save_pool_history("20261015", rows("sh600010", "sh600011"),
                         qdate="20261015")
    ok(len(ds.load_pool_history("20261015")) == 2, "先写下 2 只（模拟盘中早扫的残缺池）")
    ok(ds.save_pool_history("20261015", rows(*[f"sh60{i:04d}" for i in range(20)]),
                            qdate="20261015") is True,
       "20 只（更完整）→ 允许覆盖，残缺自愈")
    ok(len(ds.load_pool_history("20261015")) == 20, "覆盖后是 20 只")
    ok(ds.save_pool_history("20261015", rows("sh600010"), qdate="20261015") is False,
       "只剩 1 只（更少）→ 拒绝覆盖，防盘中抖动互相打架")
    ok(len(ds.load_pool_history("20261015")) == 20, "内容没被改小")

    print("\n[4] 覆盖时保留东财那份的字段值（复盘接口字段口径不同）")
    old = [{"code": "sz002242", "name": "九阳股份", "boards": 5, "first_time": 93027,
            "break_count": 6, "turnover": 18.28, "float_mv": 1.153e10,
            "sector": "小家电", "price": 15.13}]
    new = [{"code": "sz002242", "name": "九阳股份", "boards": 5, "first_time": 93027,
            "break_count": 0, "turnover": 56.29, "float_mv": 3.74e9,
            "sector": "电子商务", "price": 0.0}]
    m = ds._merge_pool_rows(old, new)
    ok(m[0]["break_count"] == 6, f"炸板次数保留东财值（{m[0]['break_count']}，复盘是 0）")
    ok(abs(m[0]["turnover"] - 18.28) < 0.01,
       f"换手保留东财值（{m[0]['turnover']}，复盘是 56.29）")
    ok(abs(m[0]["float_mv"] - 1.153e10) < 1e6, "流通市值保留东财值")
    ok(m[0]["sector"] == "小家电", "板块保留东财值（复盘给的是另一套分类）")
    ok(len(m) == 1 and m[0]["code"] == "sz002242", "成员集合以新池为准")

    print("\n[5] 涨停家数：当日不再首扫定稿")
    real_pool = ds._fetch_pool
    try:
        ds._fetch_pool = lambda kind, date, sort, ttl=120: [{}] * 16   # noqa: ARG005
        ds.fetch_limit_up_history(10, "20261020", final=False)
        h = {x["date"]: x for x in json.load(open(zt_path(), encoding="utf-8"))}
        ok(h["20261020"]["limit_up"] == 16, "盘中早扫先记 16 家")
        ds._fetch_pool = lambda kind, date, sort, ttl=120: [{}] * 46   # noqa: ARG005
        ds.fetch_limit_up_history(10, "20261020", final=False)
        h = {x["date"]: x for x in json.load(open(zt_path(), encoding="utf-8"))}
        ok(h["20261020"]["limit_up"] == 46,
           "★ 后续读数能更新它（原实现是「已存在就永不更新」，错值被钉死）")
        ds.fetch_limit_up_history(10, "20261020", final=True)
        ds._fetch_pool = lambda kind, date, sort, ttl=120: [{}] * 5    # noqa: ARG005
        ds.fetch_limit_up_history(10, "20261020", final=False)
        h = {x["date"]: x for x in json.load(open(zt_path(), encoding="utf-8"))}
        ok(h["20261020"]["limit_up"] == 46, "收盘终值写入后不再被盘中读数覆盖")
        ok(h["20261020"]["final"] is True, "终值带 final 标记")
    finally:
        ds._fetch_pool = real_pool

    print("\n[6] 历史错值可被定点修复")
    ok(ds.fix_limit_up_count("20261021", 42) is True, "补一条新的家数记录")
    ok(ds.fix_limit_up_count("20261021", 42) is False, "同值重复调用不再写盘")
    ok(ds.fix_limit_up_count("20261021", 51) is True, "错值可被修正")
    h = {x["date"]: x for x in json.load(open(zt_path(), encoding="utf-8"))}
    ok(h["20261021"]["limit_up"] == 51, "修正生效")

    print("\n[7] 对账修复：错日期的当日归档会被移走，不删除")
    # 用 force 才能造出这条坏归档 —— 正常路径下 [2] 的守卫会直接挡住它
    ds.save_pool_history("20261023", rows("sh600825", "sh600826"), qdate="20261023")
    ds.save_pool_history("20261024", rows("sh600825", "sh600826"), force=True)
    ok(ds.pool_history_duplicates("20261024", ds.load_pool_history("20261024")) is True,
       "先造出「今天装的其实是昨天那批」的坏归档")
    real_hp = kph.history_pool
    try:
        kph.history_pool = lambda d: []                    # 今天复盘还没生成
        kph._repaired.clear()
        r = kph.repair_pool_history("20261024", days=1)
        ok("20261024" in r["moved"], f"与上一交易日同批的当日归档被移走（{r}）")
        ok(ds.load_pool_history("20261024") == [], "移走后不再被当成交易日读进来")
        sus = os.path.join(ds.POOL_HIST_DIR, "_suspect")
        ok(os.path.exists(os.path.join(sus, "20261024.json")), "文件挪到了 _suspect/ 里可回溯")
        ok("_suspect" not in ds.pool_history_dates(),
           "pool_history_dates 不会把 _suspect 当成交易日")
    finally:
        kph.history_pool = real_hp

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
