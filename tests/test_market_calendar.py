# -*- coding: utf-8 -*-
"""休市日历**自动获取**的回归测试（v1.39.0）。

钉住两件事：
  ① 节假日日历是**自动抓 + 解析**上交所官网得来的，不是手填的表；
     不许再往 strategy_config 里塞 market_holidays 之类的静态表。
  ② 拿不到日历时**必须如实标 exact=False**（"估算"），
     绝不猜一个"明天肯定是交易日"出来。

为什么值得单独立测试：这个 bug 的形态是**静默出错** —— 节前把"明日"
写成休市日，用户按它做隔日计划就落空，而页面上看不出任何异常。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="holiday_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds          # noqa: E402
from core import outlook                   # noqa: E402
from core import strategy_config as sc     # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# 上交所 2026 年休市安排（工作日休市），作为"网络可用时的期望值"。
# 注意：这是**校验用**的常量，不是产品运行依赖 —— 产品自己去抓。
EXPECT_2026 = {
    "20260101", "20260102",                                  # 元旦
    "20260216", "20260217", "20260218", "20260219",
    "20260220", "20260223",                                  # 春节
    "20260406",                                              # 清明
    "20260501", "20260504", "20260505",                      # 劳动节
    "20260619",                                              # 端午
    "20260925",                                              # 中秋
    "20261001", "20261002", "20261005", "20261006", "20261007",  # 国庆
}


def main():
    # [1] 自动抓取 + 解析（离线时跳过，不让测试变成"必须有网"）
    print("[1] fetch_market_holidays 自动获取上交所休市日历")
    try:
        got = set(ds.fetch_market_holidays(2026))
    except Exception as e:
        got = set()
        print("       （抓取异常：%s）" % type(e).__name__)
    if not got:
        print("  ..  网络不可用 → 跳过内容校验，只验降级路径")
    else:
        ok(got == EXPECT_2026,
           "解析出的 2026 年工作日休市日与上交所公告一致（%d 天）" % len(got))
        # 周末不得混进来（它们本就不开市，收了会让 exact 判据更难读）
        import datetime as _dt
        weekday_only = all(_dt.datetime.strptime(d, "%Y%m%d").weekday() < 5
                           for d in got)
        ok(weekday_only, "收进来的全部是工作日（周末不在表里）")

    # [2] 节前精确推算（真实网络下必过；离线时断言"不许乱猜"）
    print()
    print("[2] 节前「明日」必须跳到节后首个交易日")
    cases = [
        ("20260930", "10-08", "国庆前"),
        ("20260924", "09-28", "中秋前"),
        ("20260213", "02-24", "春节前"),
        ("20260430", "05-06", "劳动节前"),
        ("20260403", "04-07", "清明前"),
        ("20260618", "06-22", "端午前"),
    ]
    if got:
        for d, want, label in cases:
            r = outlook._next_trade_date(d)
            ok(r[0] == want and r[1] is True,
               "%s -> %s（exact=True）" % (label, want))
    else:
        for d, want, label in cases[:2]:
            r = outlook._next_trade_date(d)
            ok(r[1] is False,
               "%s 离线时如实标 exact=False（不冒充精确）" % label)

    # [3] 普通日仍要给出确定结论
    print()
    print("[3] 普通交易日")
    r = outlook._next_trade_date("20261015")     # 周四
    if got:
        ok(r == ("10-16", True), "周四 -> 10-16（exact=True）")
    else:
        ok(r[0] == "10-16", "周四 -> 10-16（内容正确）")
    r2 = outlook._next_trade_date("20261016")    # 周五 -> 跳周末
    ok(r2[0] == "10-19", "周五 -> 10-19（跳过周末）")

    # [4] 跨年：次年安排未公布时不许自称精确
    print()
    print("[4] 跨年边界不得乱猜")
    r = outlook._next_trade_date("20261231")
    if got:
        # 2027 的安排此时必然还没公布（12 月才发），exact 必须为 False
        ok(r[1] is False,
           "12-31 看次日：2027 年安排未公布 -> exact=False（不猜 01-01）")
    else:
        ok(r[1] is False, "离线跨年 -> exact=False")

    # [5] 不许再退回"手填静态表"
    print()
    print("[5] 已废弃的静态表不得复活")
    cfg = sc.load_config()
    m7 = (cfg or {}).get("M7_earnings") or {}
    ok("market_holidays" not in m7,
       "strategy_config.M7_earnings 不再有手填的 market_holidays")

    # [6] 降级路径：抓不到日历 + K线也没有 -> 退回工作日估算并标 False
    print()
    print("[6] 完全断网时的降级必须诚实")
    _orig_h = ds.fetch_market_holidays
    _orig_c = ds.fetch_trade_dates
    try:
        ds.fetch_market_holidays = lambda year=None, ttl=86400: []
        ds.fetch_trade_dates = lambda n=300: []
        h = ds.trade_calendar_hint("20260930")
        ok(h["exact"] is False,
           "无日历无K线 -> exact=False（10-01 是休市日，但不冒充知道）")
        ok(h["next"] != "", "仍给出一个可显示的候选日期：%s" % h["next"])
    finally:
        ds.fetch_market_holidays = _orig_h
        ds.fetch_trade_dates = _orig_c

    print()
    print("===== %d 通过 / %d 失败 =====" % (okc, badc))
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
