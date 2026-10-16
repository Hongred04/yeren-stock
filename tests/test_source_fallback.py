# -*- coding: utf-8 -*-
"""数据源降级回归测试 —— 钉死「换了数据源，结论就变」这一类 bug（问题文档第 12 项）。

## 被钉的 bug
用户原话：「数据源变化也有bug自查一下」。

`pipeline.py` 的 snapshot 降级策略是**整块替换**：腾讯失败且覆盖率不足时
`snapshots = fetch_snapshots_sina(codes)`。但两家字段数不同 ——
**腾讯 21 个、新浪只有 7 个**，`limit_up` / `limit_down` 只有腾讯有。
降级路上「字段变少」被下游错误地解读成了「事实不同」，共三处：

### BUG 1（HIGH）竞价一字板全部误判成「高开≥5%」
`core/auction.py` `is_limit_up()` 原为：
    lu = _f(snap.get("limit_up"))      # 新浪路 None → _f(None)==0.0
    return lu > 0 and ...              # 恒 False
于是新浪路上**每一只**竞价涨停都被 `band_of` 判成 gap5，竞价涨停家数掉到 0。
实测同一市场只换源：涨停 1→0 只、bias risk_on(抢筹)→neutral(中性)。

### BUG 2（MEDIUM）快照时间戳被格式差异吃掉
`evaluate()` 里 `if len(t) >= 14` —— 腾讯是 "20261016161447"（14位）符合，
新浪是 "15:34:59"（8位）**恒不符合** → snapshot_time 恒空串。
偏偏这个字段的用途就是「让用户知道这份读数有多新」，而新浪正是最不可靠时走的路。

### BUG 3（MEDIUM）降级用空结果覆盖了手里的有效数据
`fetch_kline` 的 `producer()` 原为三行 `if len(bars) < 25: bars = 下一路`，
**无条件赋值**，而失败的一路返回 `[]` → 「tdx=24 根有效 + tx 超时 + sina 空」
最终得到 **0 根**，明明有 24 根却被更差的源冲掉；调用方据此记
`_health_set("klines", False)`，一次抖动被记成「K线整体不可用」。

## 本测试守的契约
  · 同一市场数据，带 limit_up（腾讯）与不带（新浪）必须给出**相同**的
    涨停判定与 band —— 字段缺失不得改变结论；
  · 涨跌停价推算幅度必须用**真实比例**（主板10%/创业科创20%/北交所30%/ST5%），
    不得用 indicators.limit_up_threshold（那是 9.5 的判定下限，会把
    1444.27 算成 1437.70）；
  · 时间戳两种格式（14位 / HH:MM:SS）都要能读出来；
  · fetch_kline 降级只许「往上换」，空结果绝不能覆盖有效结果；
  · 三路全失败时仍要如实返回空（不许为了「不覆盖」而编造数据）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auction           # noqa: E402
from core import datasource as ds  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# ================================================================ BUG 1

print("=" * 62)
print("BUG 1：字段缺失不得改变涨停判定（同一市场两种源结论必须一致）")
print("=" * 62)

# 600519 昨收 1312.97，真实涨停价 1444.27（10%）
BASE = {"code": "sh600519", "name": "贵州茅台", "price": 1444.27,
        "prev_close": 1312.97, "open": 1444.27, "high": 1444.27,
        "low": 1444.27, "volume": 100}
TX = dict(BASE, limit_up=1444.27, limit_down=1181.67, time="20261016161447")
SINA = dict(BASE, time="15:34:59")            # 新浪：无 limit_up/limit_down

gap = (BASE["price"] / BASE["prev_close"] - 1) * 100

a = auction.is_limit_up(TX)
b = auction.is_limit_up(SINA)
print(f"  腾讯(有limit_up)={a}   新浪(无limit_up)={b}")
ok(a is True, "腾讯路能识别竞价涨停（原有行为不许改坏）")
ok(b is True, "新浪路也能识别竞价涨停（原 bug：恒 False → 误判成高开≥5%）")
ok(a == b, "两种源的涨停判定一致 —— 字段变少不等于事实不同")

ba, bb = auction.band_of(gap, TX), auction.band_of(gap, SINA)
print(f"  band: 腾讯={ba}  新浪={bb}")
ok(ba == bb == "limit_up", "两种源都归入「竞价涨停」档（原 bug：新浪落到 gap5）")

# 跌停同理
lowsnap = {"code": "sh600519", "price": 1181.67, "prev_close": 1312.97}
ok(auction.is_limit_down(dict(lowsnap, limit_down=1181.67)) is True,
   "腾讯路能识别竞价跌停")
ok(auction.is_limit_down(lowsnap) is True,
   "新浪路也能识别竞价跌停（按昨收推算跌停价）")

# 幅度必须真实：不能用 limit_up_threshold（9.5 是判定下限不是幅度）
print()
print("  幅度推算（必须用真实比例，不能用 9.5 判定下限）：")
CASES = [("sh600519", 1312.97, "贵州茅台", 1444.27),
         ("sz300750", 100.0, "宁德时代", 120.0),      # 创业板 20%
         ("sh688111", 50.0, "金山办公", 60.0),        # 科创板 20%
         ("sh600001", 10.0, "ST测试", 10.50)]         # ST 5%
for code, prev, nm, exp in CASES:
    p = auction._limit_prices({"code": code, "prev_close": prev, "name": nm})
    got = p[0] if p else None
    flag = "OK" if (got is not None and abs(got - exp) < 0.01) else "XX"
    print(f"    {code:10} {nm:8} 昨收{prev:8} → 涨停{got} 期望{exp}  {flag}")
    ok(got is not None and abs(got - exp) < 0.01,
       f"{code} 涨跌停幅度推算正确（{nm}）")

# 缺昨收时不许瞎算
ok(auction._limit_prices({"code": "sh600519"}) is None,
   "拿不到昨收时返回 None（不许拿 0 当昨收推出涨停价 0）")
ok(auction.is_limit_up({"code": "sh600519", "price": 10.0}) is False,
   "缺昨收时判定为 False（诚实：算不出来就不说它是涨停）")

# ================================================================ BUG 2

print()
print("=" * 62)
print("BUG 2：两种时间戳格式都要能读出来")
print("=" * 62)

for nm, snap, exp in (("腾讯 14位", TX, "16:14:47"),
                      ("新浪 HH:MM:SS", SINA, "15:34:59")):
    r = auction.evaluate({}, snaps={"sh600519": snap}, zt=[], real_date="20261016")
    got = r.get("snapshot_time")
    print(f"  {nm:14} snapshot_time={got!r} 期望{exp!r}")
    ok(got == exp, f"{nm} 的时间戳能正确读出（原 bug：新浪恒为空串）")

# 两种源必须给出同一个 bias（原 bug：腾讯 risk_on / 新浪 neutral）
r_tx = auction.evaluate({}, snaps={"sh600519": TX}, zt=[], real_date="20261016")
r_sn = auction.evaluate({}, snaps={"sh600519": SINA}, zt=[], real_date="20261016")
bt = (r_tx.get("temperature") or {}).get("bias")
bs = (r_sn.get("temperature") or {}).get("bias")
print(f"  bias: 腾讯={bt}  新浪={bs}")
ok(bt == bs, "两种源的市场倾向一致（原 bug：抢筹→中性）")

# 乱七八糟的时间戳不许崩，也不许编造
for bad in ("", "abc", "123", None):
    r = auction.evaluate({}, snaps={"sh600519": dict(BASE, time=bad)}, zt=[],
                         real_date="20261016")
    if r.get("snapshot_time"):
        ok(False, f"非法时间戳 {bad!r} 应留空，实得 {r.get('snapshot_time')!r}")
    else:
        ok(True, f"非法时间戳 {bad!r} → 留空（不编造）")

# ================================================================ BUG 3

print()
print("=" * 62)
print("BUG 3：fetch_kline 降级只许往上换，空结果不得覆盖有效结果")
print("=" * 62)

_orig = (ds._kline_from_tdx, ds._kline_from_tx, ds._kline_from_sina)


def _mk(n):
    return [{"date": f"2026-01-{i + 1:02d}", "open": 1.0, "close": 1.0,
             "high": 1.0, "low": 1.0, "volume": 1.0} for i in range(n)]


try:
    for label, t, x, s, exp in (("tdx=24有效 tx=[] sina=[]", 24, 0, 0, 24),
                                ("tdx=0 tx=24有效 sina=[]", 0, 24, 0, 24),
                                ("tdx=0 tx=0 sina=24有效", 0, 0, 24, 24),
                                ("tdx=0 tx=0 sina=0 全空", 0, 0, 0, 0),
                                ("tdx=6 tx=30 sina=0 (换更长的)", 6, 30, 0, 30)):
        ds._kline_from_tdx = lambda c, n, t=t: _mk(t)
        ds._kline_from_tx = lambda c, n, x=x: _mk(x)
        ds._kline_from_sina = lambda c, n, s=s: _mk(s)
        # ★ 必须清缓存，否则第二轮起直接命中上一轮结果
        #   （缓存变量叫 `_cache`，小写；写成 `_CACHE` 会静默命中 getattr
        #    默认值 → 永远清不掉 → 所有用例都量到同一个旧值，看起来像
        #    「代码没生效」，其实是测试没生效）
        for k in [k for k in list(ds._cache) if "kline" in k]:
            ds._cache.pop(k, None)
            ds._cache_ts.pop(k, None)
        got = len(ds.fetch_kline("sh000001", 70))
        print(f"  {label:24} → {got:3} 根  期望{exp:3}  {'OK' if got == exp else 'XX'}")
        ok(got == exp, f"{label} → {got} 根（空/更短的结果不得覆盖有效结果）")
finally:
    ds._kline_from_tdx, ds._kline_from_tx, ds._kline_from_sina = _orig

# 真实调用一次，确认没把正常路径改坏
real = ds.fetch_kline("sh000001", 70)
print(f"  真实 fetch_kline('sh000001',70) → {len(real)} 根")
ok(len(real) > 25, "真实数据源仍能取到完整K线（改动没有破坏正常路径）")

print()
print("=" * 62)
print(f"通过 {okc} / 失败 {badc}")
print("=" * 62)
sys.exit(1 if badc else 0)
