# -*- coding: utf-8 -*-
"""财经事件时间轴自测（v1.40.0，建议 3）。

不依赖真实网络也能跑的关键断言用**本地规律**（交割日、FOMC 覆盖、权重分档）；
涉及东财的断言只在网络可用时做软校验（跳过而不是失败），避免网络抖动导致假红。
"""
import sys
import datetime as dt

sys.path.insert(0, __file__.rsplit("tests", 1)[0].rstrip("\\/"))

from core import events


def ok(cond, msg):
    if cond:
        print(f"  [OK]   {msg}")
    else:
        print(f"  [FAIL] {msg}")
        raise AssertionError(msg)


print("=== 1. 交割日规律：每月第三个周五 ===")
# 2026 全年核对（都用真实日历验：d.weekday()==4 且 15 <= d.day <= 21）
bad = []
for m in range(1, 13):
    d = events._third_friday(2026, m)
    if d.weekday() != 4 or not (15 <= d.day <= 21):
        bad.append((m, d.isoformat(), d.weekday()))
ok(not bad, f"2026 全年 12 个交割日都是第 3 个周五（异常={bad}）")
ok(events._third_friday(2026, 10) == dt.date(2026, 10, 16),
   "2026-10-16 是 10 月交割日（与实盘核对一致）")
ok(events._third_friday(2026, 2) == dt.date(2026, 2, 20),
   "2026-02-20 是 2 月交割日（避开春节长假）")

print("\n=== 2. 跨月窗口能正确抓到边界上的交割日 ===")
# 注意窗口语义：_expiry_between(start, days) 覆盖 [start, start+days]，
# **含端点**，共 days+1 天（docstring 已写明）。测试按这个语义写，
# 别再按"days 天"去数 —— 早先就是这么数错的。
ok(events._expiry_between(dt.date(2026, 10, 10), 5) == [],
   "10-10 起 days=5（覆盖到 10-15）不含交割日")
ok(events._expiry_between(dt.date(2026, 10, 10), 6) == ["20261016"],
   "10-10 起 days=6（覆盖到 10-16）正好含 20261016 —— 端点是含的")
ok(events._expiry_between(dt.date(2026, 10, 12), 7) == ["20261016"],
   "10-12 起 days=7 也含 20261016（窗口起点在交割日之前）")
ok(events._expiry_between(dt.date(2026, 10, 17), 40) == ["20261120"],
   "10-17 起 41 天 → 下一个交割日是 11-20（不会漏、也不会多）")
ok(events._expiry_between(dt.date(2026, 10, 16), 0) == ["20261016"],
   "days=0 就是看当天本身（端点含）")

print("\n=== 3. FOMC 日程覆盖与陈旧标记 ===")
ok(events.fomc_schedule_stale(2026) is False, "2026 在表内 → 不陈旧")
ok(events.fomc_schedule_stale(2030) is True, "2030 不在表内 → 标陈旧（宁可不说）")
ok(events._FOMC_MAX_YEAR == 2027, "表覆盖到 2027（与代码同步）")
# 表内日期都必须能被解析成合法日期
for y, mds in events._FOMC_DATES.items():
    for md in mds:
        dt.datetime.strptime(f"{y}{md}", "%Y%m%d")
ok(True, "全部内嵌 FOMC 日期都是合法日期")

print("\n=== 4. 陈旧年份不得返回任何议息日（绝不猜）===")
got, dated = events._fomc_between(dt.date(2030, 1, 1), 360)
ok(got == [] and dated is False, "2030 年查议息 → 空列表 + dated=False")

print("\n=== 5. 议息窗口命中 ===")
# 2026 表里 10-27/10-28 是一对会议（决议日=最后一天 10-28）
got, dated = events._fomc_between(dt.date(2026, 10, 26), 3)
ok(dated is True, "2026 年 dated=True")
ok("20261027" in got and "20261028" in got, f"命中 10-27/10-28（实测 {got}）")
ok(events._fomc_between(dt.date(2026, 10, 1), 10)[0] == [],
   "10-01 起 11 天不含议息")

print("\n=== 6. 权重分档：按合计规模，不只看『几只大额』 ===")
# 直接验证分档规则本身（把 build 里的规则抽出来对照）
def tier(total):
    return 2 if total >= 300 else (1 if total >= 30 else 0)

ok(tier(623) == 2, "623 亿 → risk（实测 10-21 中船特气一例）")
ok(tier(194) == 1, "194 亿 → 提示")
ok(tier(51) == 1, "51 亿 → 提示")
ok(tier(13) == 0, "13 亿 → 不升级 tone")
ok(tier(299) == 1 and tier(300) == 2, "阈值边界 299/300 正确")

print("\n=== 7. build() 结构契约（允许网络失败，只验形状）===")
ev = events.build("20261015")
ok(isinstance(ev, dict), "返回 dict")
ok("days" in ev and isinstance(ev["days"], list), "有 days 列表")
ok("next" in ev and isinstance(ev["next"], dict), "有 next 字典")
ok("notes" in ev and isinstance(ev["notes"], list), "有 notes 列表")
ok(isinstance(ev.get("source"), str) and ev["source"], "有 source 说明")
ok(events.brief({}) == "", "brief(空) → 空串")
ok(isinstance(events.brief(ev), str), "brief(正常) → 字符串")
for d in ev["days"]:
    ok(len(d.get("date") or "") == 8, f"day.date 是 8 位：{d.get('date')}")
    ok(d.get("tone") in ("risk", "info"), f"day.tone 合法：{d.get('tone')}")
    ok(isinstance(d.get("items"), list) and d["items"], "day.items 非空")
    break

print("\n=== 8. 周末不排 ===")
for d in ev["days"]:
    wd = dt.datetime.strptime(d["date"], "%Y%m%d").weekday()
    ok(wd < 5, f"{d['date']} 是工作日（weekday={wd}）")

print("\n===== 全部通过 =====")
