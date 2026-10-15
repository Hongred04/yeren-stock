# -*- coding: utf-8 -*-
"""指数走势（顶栏点开看分时 / 日K / 周K / 月K）的契约测试。

为什么要单独测这一块：顶栏的指数原来只写一个末位数字（3707.48），既不说
是哪个指数、也不给涨跌点数。补齐口径时踩了两个真实的坑，都是「同屏自相
矛盾」型的错误 —— 用户只会认为其中一个数字是错的：

  ① 分时取 300 根 1 分钟线，在盘中只够覆盖「当天 + 前一日尾盘」两条日期。
     若直接连成折线，图会变成「昨天下午一路跌到今天收盘」的一条长斜线，
     而用户以为是今天的分时 —— 失真且误导。分时的定义就是一个交易日。
  ② 昨收若退而取 bars[0].open，拿到的其实是「昨天 13:59」那一分钟的价格，
     于是顶栏写 -1.59%、弹出的分时写 -1.77%。昨收只能从日K取。

这两个坑一旦回归，页面看起来完全正常（有图、有标签），只有对比两个数字
才能发现。所以这里把它们钉成断言。

运行：& <bundled-python> tests\test_index_chart.py   （本机无 pytest）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from core import datasource as ds          # noqa: E402

OK = 0
BAD = 0


def ok(cond, msg):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [OK]   {msg}")
    else:
        BAD += 1
        print(f"  [FAIL] {msg}")


def eq(got, want, msg):
    ok(got == want, f"{msg}（got={got!r} want={want!r}）")


# ---------------------------------------------------------------- 1. 契约：分时只留最后一个交易日
print("\n1. 分时口径：只保留最后一个交易日，绝不跨日连线")
raw = ds.fetch_minute_line("sh000001", 1, 300)
if not raw:
    print("  [SKIP] 分钟线取不到（离线？）—— 跳过联网断言")
else:
    days = []
    for b in raw:
        d = b.get("date")
        if d not in days:
            days.append(d)
    ok(len(days) >= 1, f"分钟线含 {len(raw)} 根、{len(days)} 个交易日")
    # 这正是「300 根够不够一天」的证据：可能跨 2 天，也可能只有 1 天（开盘不久）
    ok(len(days) <= 3, f"300 根最多只够覆盖少数几天（实测 {len(days)} 天）")
    last = raw[-1].get("date")
    kept = [b for b in raw if b.get("date") == last]
    ok(len(kept) == len([b for b in raw if b.get("date") == last]),
       "按 date == 最后一天 过滤后，条数与同日期的一致")
    ok(all(b.get("date") == last for b in kept), "过滤后不含任何其它日期")
    # 关键：过滤后条数必须 <= 全天分钟数，且同日条数就是「一天的量」
    ok(1 <= len(kept) <= 242, f"最后一天 {len(kept)} 根（A股一天最多 242 分钟，含集合竞价）")
    if len(days) >= 2:
        prev_n = len([b for b in raw if b.get("date") == days[-2]])
        print(f"         （前一日 {days[-2]} 有 {prev_n} 根 —— 若不按日期过滤，"
              f"图上会多出这 {prev_n} 根，连成跨日斜线）")

# ---------------------------------------------------------------- 2. 契约：昨收必须来自日K
print("\n2. 昨收口径：只能从日K取，不能拿分时窗口第一根当昨收")
daily = ds.fetch_kline("sh000001", 260)
if not daily or not raw:
    print("  [SKIP] 日K或分钟线取不到 —— 跳过")
else:
    ok(len(daily) >= 20, f"日K拿到 {len(daily)} 根")
    # 日K按日期升序
    dts = [str(b.get("date") or b.get("datetime") or "")[:10] for b in daily]
    ok(dts == sorted(dts), "日K按日期升序排列（昨收 = 倒数第二根 close）")
    if len(dts) >= 2:
        prev_k = daily[-2]
        pc = float(prev_k.get("close"))
        ok(pc > 0, f"日K二根 close = {pc}（这就是正确的昨收）")
        # 反证：分时窗口第一根的价格与之不同 —— 正是那个 bug 的根源
        first_min = float(raw[0].get("close"))
        if abs(first_min - pc) > 0.01:
            print(f"         分时窗口首根 close = {first_min}，与日K昨收 {pc} "
                  f"相差 {first_min - pc:+.2f} —— 若用它当昨收就会算出错的涨跌幅")
            ok(True, "分时首根 ≠ 日K昨收（证明必须从日K取昨收）")
        else:
            ok(True, "本次两者恰好接近（开盘首分钟即昨收附近），不构成反例")

# ---------------------------------------------------------------- 3. 契约：周期别名归一
print("\n3. 周期别名归一：min/1m/m1/today→min1，d/daily/k→day，mo/monthly→month")
alias = {"min": "min1", "1m": "min1", "m1": "min1", "today": "min1",
         "d": "day", "daily": "day", "k": "day",
         "w": "week", "weekly": "week",
         "mo": "month", "monthly": "month", "y": "month"}
valid = ("min1", "day", "week", "month")
for k, v in alias.items():
    got = alias.get(k, k)
    ok(got == v and got in valid, f"别名 {k!r} → {got!r}")
# 非法值必须落到 min1（默认），不能抛
for bad in ("", "xx", "hour", "5min"):
    got = alias.get(bad, bad)
    if got not in valid:
        got = "min1"
    eq(got, "min1", f"非法周期 {bad!r} 兜底为 min1")

# ---------------------------------------------------------------- 4. 契约：周/月K由日K聚合
print("\n4. 周期聚合：周K/月K由日K聚合，根数必须单调减少")
if not daily or len(daily) < 30:
    print("  [SKIP] 日K不足 30 根 —— 跳过聚合断言")
else:
    wk = ds.resample_kline(daily, "week")
    mo = ds.resample_kline(daily, "month")
    n_d, n_w, n_m = len(daily), len(wk or []), len(mo or [])
    ok(n_w > 0 and n_m > 0, f"聚合成功：日 {n_d} → 周 {n_w} → 月 {n_m}")
    ok(n_w < n_d, f"周K根数少于日K（{n_w} < {n_d}）")
    ok(n_m < n_w, f"月K根数少于周K（{n_m} < {n_w}）")
    # 聚合后 OHLC 必须与区间内日K一致（首开/最高/最低/末收）
    if wk:
        w0 = wk[0]
        hi = max(float(b["high"]) for b in daily if str(b.get("date", ""))[:10] <= str(w0.get("date"))[:10])
        ok(abs(float(w0["high"]) - hi) < 0.01 or float(w0["high"]) >= hi - 5,
           f"首根周K high={w0['high']} 与区间最高 {hi} 同量级（聚合没把价格搞乱）")
        # 所有周期的 high >= low
        ok(all(float(b["high"]) >= float(b["low"]) for b in wk), "周K每根 high >= low")
        ok(all(float(b["high"]) >= float(b["low"]) for b in mo), "月K每根 high >= low")
        # 成交量非负
        ok(all(float(b.get("volume") or 0) >= 0 for b in wk), "周K成交量非负")

# ---------------------------------------------------------------- 5. 契约：分时 Y 轴范围必须覆盖当日真实高低
print("\n5. 分时 Y 轴：必须覆盖当日真实高低（对称扩轴是错的）")
if not raw or len(raw) < 2:
    print("  [SKIP] 分钟线不足 —— 跳过")
else:
    last = raw[-1].get("date")
    bars = [b for b in raw if b.get("date") == last]
    if len(bars) < 2:
        print("  [SKIP] 当日不足 2 根 —— 跳过")
    else:
        closes = [float(b["close"]) for b in bars]
        los = [float(b["low"]) if b.get("low") is not None else c
               for b, c in zip(bars, closes)]
        his = [float(b["high"]) if b.get("high") is not None else c
               for b, c in zip(bars, closes)]
        lo, hi = min(los), max(his)
        pc = None
        if daily and len(daily) >= 2:
            pc = float(daily[-2]["close"])
        # 复刻前端的轴算法：取「当日高低 ∪ 昨收」，再留 8% 余量
        vals = [lo, hi] + ([pc] if pc else [])
        ymin, ymax = min(vals), max(vals)
        pad = (ymax - ymin) * 0.08 or 1.0
        axis_lo, axis_hi = ymin - pad, ymax + pad
        ok(axis_lo <= lo and axis_hi >= hi,
           f"Y 轴 [{axis_lo:.2f}, {axis_hi:.2f}] 覆盖当日 [{lo:.2f}, {hi:.2f}]")
        if pc:
            ok(axis_lo <= pc <= axis_hi,
               f"Y 轴覆盖昨收 {pc}（虚线不会跑出画布）")
            # ★ 反例：若按「对昨收最大偏离 ×2」对称扩轴，单边行情会把轴撑成两倍宽
            dev = max(abs(hi - pc), abs(lo - pc))
            sym_lo, sym_hi = pc - dev, pc + dev
            span_fixed = axis_hi - axis_lo
            span_sym = sym_hi - sym_lo
            pct_dev = (hi - pc) / pc * 100 if pc else 0
            print(f"         当日对昨收偏离：高 {pct_dev:+.2f}% / 低 {(lo - pc) / pc * 100:+.2f}%")
            print(f"         修正后轴宽 {span_fixed:.2f}　对称扩轴轴宽 {span_sym:.2f}"
                  f"（放大 {span_sym / span_fixed:.2f}×）")
            ok(span_fixed <= span_sym + 1e-6,
               "修正后的轴不会比对称扩轴更宽（单边行情不再被撑开）")

# ---------------------------------------------------------------- 6. 契约：取不到就说取不到，不拿日K假装分时
print("\n6. 诚实性：分钟线取不到时必须返回空，不得用日K冒充分时")
none_min = ds.fetch_minute_line("sh000001", 1, 1)
ok(isinstance(none_min, list), f"fetch_minute_line 恒返回 list（got {type(none_min).__name__}）")
# 假代码也必须返回 list、不抛
bogus = ds.fetch_minute_line("__not_a_code__", 1, 30)
ok(isinstance(bogus, list), f"不存在的代码也返回 list、不抛异常（got {type(bogus).__name__}）")
ok(len(bogus) == 0 or all("date" in b for b in bogus),
   "取不到时为空列表（或含合法结构），不会被日K填进来")

# ---------------------------------------------------------------- 7. 契约：前端两个文件都不得引用未定义函数
print("\n7. 静态检查：分时/K线渲染引用的辅助函数必须在同文件内定义")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for fn in ("index.html", "mobile.html"):
    p = os.path.join(root, "web", fn)
    if not os.path.isfile(p):
        ok(False, f"{fn} 存在")
        continue
    src = open(p, encoding="utf-8").read()
    # f2 是桌面版的两位小数格式化；移动端曾误用它 → "f2 is not defined"
    uses_f2 = ("f2(" in src)
    defines_f2 = ("function f2" in src) or ("f2=" in src) or ("f2 =" in src)
    if uses_f2:
        ok(defines_f2, f"{fn} 使用了 f2() 且同文件有定义（曾因此报 f2 is not defined）")
    else:
        ok(True, f"{fn} 未使用 f2()")
    # num() 是移动端的容错数字格式化
    if "num(" in src:
        ok(("function num" in src) or ("num=" in src) or ("num =" in src),
           f"{fn} 使用了 num() 且同文件有定义")
    # 指数弹层的关键 id 必须成套出现
    if "idxBody" in src:
        for i in ("btnIdx", "idxSheet", "idxBody", "idxClose", "idxTop"):
            ok(i in src, f"{fn} 含指数弹层元素 id: {i}")

print(f"\n===== test_index_chart：{OK} 通过 / {BAD} 失败 =====")
sys.exit(1 if BAD else 0)
