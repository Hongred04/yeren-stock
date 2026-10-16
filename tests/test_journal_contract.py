# -*- coding: utf-8 -*-
"""E4 信号复盘档案 —— 回测口径回归测试（钉死两个会误导决策的口径 bug）。

## 被钉的 bug（v1.41.4 修复）

### BUG A：持有期 off-by-one —— 「1日」其实是当日日内涨跌
`core/journal.py` 里进场价取 `bars[idx+1].open`（T+1 开盘，正确：信号是收盘后
给的，实盘只能次日开盘买）。但结算循环原写 `j = idx + h`：
  · h=1 → 卖出点 `bars[idx+1].close`，与**进场同一天** → 算出来的是「进场当日
    日内涨跌」，不是持有 1 日的收益；
  · h=3 → 实际持有 2 个交易日；
  · h=5 → 实际持有 4 个交易日。
而界面列名标着「1日 / 3日 / 5日 胜率/均值」（`web/index.html:3778`），
用户看到的口径是错的。实测同一批 28 笔样本：
    旧「1日」      胜率 14.3%  均 -1.80%
    真·持有 1 日    胜率 32.1%  均 -0.08%
胜率被低估一半以上，且 `_traps`（踩坑雷达）正是拿 `ret1 < 0` 判断
「该不该停用这个信号」—— 口径错会导致误停用。
修法：`j = idx + 1 + h`（卖出点在进场日之后第 h 个交易日）。

### BUG B：overall 把同一票重复计入
`record()` 的去重键是 `f"{date}:{code}:{sig.type}"`，**含信号类型**，
所以同一只票同一天被 B0/B1/B2 多路命中就记成多条收益完全相同的记录。
按信号分组统计时这是对的（每个信号各算各的命中率），但 `overall` 直接
`_agg(entries)` 会把同一笔交易重复加权：实测 201 条记录只有 158 只唯一票，
样本虚增 21%。`backtest.py:89` 早就记过这条教训却没同步到 journal。
修法：`overall` 走 `_dedup_by_stock()`。

## 本测试守的契约
  · ret_h 的卖出点必须是 idx+1+h（用构造 K 线精确断言，不看实现细节）；
  · ret1 必须等于「持有 1 个交易日」而非「进场当日日内」；
  · 持有期越长卖出点越靠后（单调性，防止再写回 idx+h）；
  · _dedup_by_stock 按 (日期,代码) 去重，同日同票只留 1 条；
  · 去重保留信号优先级最高的那条（B2 > W1 > W2 > B1 > B0）；
  · stats() 的 overall 用去重后的集合，但 rows（分信号）不去重；
  · ret5 还没到时 status=partial，ret5 出来才 settled（既有口径不许改坏）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import journal  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# ---------------------------------------------------------------- 造 K 线

def mk(code: str, date: str, o: float, c: float) -> dict:
    return {"code": code, "date": date, "open": o, "close": c,
            "high": max(o, c), "low": min(o, c), "volume": 1000.0}


def bars_for(code: str, prices: list[tuple[float, float]]) -> list[dict]:
    """按 (open, close) 列表造连续交易日的 K 线，日期固定递增。"""
    out = []
    for i, (o, c) in enumerate(prices):
        # 2026-10-01 起连续交易日（结算只按列表下标找日期，不真的算日历）
        d = f"2026-10-{i + 1:02d}"
        out.append(mk(code, d, o, c))
    return out


def entry_of(prices: list[tuple[float, float]]) -> dict:
    """造一条以 prices[0] 为信号日、进场在 prices[1].open 的档案。

    字段与 `record()` 写出的结构一致（`_settle_one` 只会填 entry_open/entry_gap/
    ret_h，其余键由 record 预置；测试里必须自己补齐，否则会 KeyError）。
    """
    return {"id": "t:sh600000:B1", "date": "2026-10-01", "code": "sh600000",
            "name": "测试股", "sector": "测试", "signal": "B1",
            "signal_name": "首板试错", "score": 80, "close": prices[0][1],
            "pct": 10.0, "status": "open", "ret1": None, "ret3": None,
            "ret5": None, "entry_open": None, "entry_gap": None,
            "skip_reason": ""}


# ================================================================ BUG A

print("=" * 62)
print("BUG A：持有期口径（ret_h 的卖出点必须是 idx+1+h）")
print("=" * 62)

# 造 8 根 K 线，让每一根的收盘价都不同，这样"卖在哪一天"一眼可辨。
# idx=0 是信号日，idx=1 是进场日（进场价 = 第 2 根的 open）。
prices = [
    (10.0, 10.5),   # idx0 信号日
    (11.0, 12.0),   # idx1 进场日：进场 11.0，当日收 12.0
    (12.0, 13.0),   # idx2 持有 1 日 → 收 13.0
    (13.0, 14.0),   # idx3 持有 2 日
    (14.0, 15.0),   # idx4 持有 3 日 → 收 15.0
    (15.0, 16.0),   # idx5 持有 4 日
    (16.0, 17.0),   # idx6 持有 5 日 → 收 17.0
    (17.0, 18.0),   # idx7 富余
]
code = "sh600000"
bars = bars_for(code, prices)
e = entry_of(prices)
journal._settle_one(e, bars, "2026-10-20", 0.0)     # cost=0 便于精确断言

print(f"  进场价 entry_open = {e['entry_open']}（期望 11.0 = idx1 的 open）")
ok(e["entry_open"] == 11.0, "进场价取 idx+1 的 open（T+1 开盘，既有正确口径）")

# ret1 必须 = 13.0/11.0-1（持有 1 个交易日），而不是 12.0/11.0-1（当日日内）
exp_r1 = 13.0 / 11.0 - 1
exp_r3 = 15.0 / 11.0 - 1
exp_r5 = 17.0 / 11.0 - 1
# journal 落盘前 round(x, 4)，所以断言按 4 位小数的精度比对（不是实现细节，
# 是已知的存储粒度；用 1e-6 会因为四舍五入误报失败）
TOL = 1e-4
print(f"  ret1 = {e['ret1']:.6f}   期望 {exp_r1:.6f}（13.0/11.0-1，持有1日）")
print(f"  ret3 = {e['ret3']:.6f}   期望 {exp_r3:.6f}（15.0/11.0-1，持有3日）")
print(f"  ret5 = {e['ret5']:.6f}   期望 {exp_r5:.6f}（17.0/11.0-1，持有5日）")
ok(abs(e["ret1"] - exp_r1) < TOL,
   "ret1 = 持有 1 个交易日的收益（idx+2 收盘 / idx+1 开盘）")
ok(abs(e["ret3"] - exp_r3) < TOL, "ret3 = 持有 3 个交易日的收益")
ok(abs(e["ret5"] - exp_r5) < TOL, "ret5 = 持有 5 个交易日的收益")

# 反面：绝不能等于"进场当日日内"
wrong_r1 = 12.0 / 11.0 - 1
ok(abs(e["ret1"] - wrong_r1) > TOL,
   "ret1 不等于进场当日日内涨跌（12.0/11.0-1）—— 这是被修掉的旧 bug")
ok(e["ret1"] > wrong_r1,
   f"持有 1 日（{exp_r1:.4f}）> 当日日内（{wrong_r1:.4f}）：天数越多卖出点越靠后")

# 状态机：ret5 出来 → settled
ok(e["status"] == "settled", "K线够长时 ret5 算出来 → status=settled")

# 窗口不足：只给到 idx3，ret5 算不出 → partial，且 ret1/ret3 仍要正确
e2 = entry_of(prices)
journal._settle_one(e2, bars[:4], "2026-10-20", 0.0)   # 只有 idx0..3
print(f"  窗口不足时：ret1={e2['ret1']} ret3={e2['ret3']} ret5={e2['ret5']} status={e2['status']}")
ok(e2["ret1"] is not None and abs(e2["ret1"] - exp_r1) < TOL,
   "窗口不足时 ret1 仍按新口径算对")
ok(e2["ret3"] is None, "idx4 不在窗口内 → ret3 不硬凑（宁可缺也不给错值）")
ok(e2["ret5"] is None and e2["status"] == "partial",
   "ret5 未出 → status=partial（既有口径未被改坏）")

# ================================================================ BUG C

print()
print("=" * 62)
print("BUG C：信号日比对必须归一化（带连字符的日期不许匹配失败）")
print("=" * 62)

# 同一个信号日，两种写法都必须能结算出结果。
# 原代码 `_norm_date(b.date) == d0` 右边没归一化 → 带连字符时恒不等。
hyphen = entry_of(prices)                       # date = '2026-10-01'
hyphen["date"] = "2026-10-01"
compact = entry_of(prices)
compact["date"] = "20261001"

r_h = journal._settle_one(hyphen, bars, "2026-10-20", 0.0)
r_c = journal._settle_one(compact, bars, "2026-10-20", 0.0)
print(f"  带连字符 '2026-10-01' → {r_h}, ret1={hyphen.get('ret1')}")
print(f"  紧凑     '20261001'   → {r_c}, ret1={compact.get('ret1')}")
ok(hyphen.get("ret1") is not None,
   "带连字符的信号日也能结算（原 bug：恒匹配失败 → 永远 pending/skip）")
ok(hyphen.get("ret1") == compact.get("ret1"),
   "两种日期写法结算结果完全一致（归一化后跑同一条逻辑）")
ok(hyphen.get("status") == "settled", "带连字符时同样走到 settled")

# 反向护栏：日期真的不在 K 线里时，仍必须诚实返回 pending/skip，不许瞎算
miss = entry_of(prices)
miss["date"] = "2026-09-01"                     # K线里没有这天
r_m = journal._settle_one(miss, bars, "2026-10-20", 0.0)
print(f"  不存在的信号日 '2026-09-01' → {r_m}, ret1={miss.get('ret1')}")
ok(miss.get("ret1") is None,
   "信号日不在K线里时不给收益（归一化不能修成『随便匹配一个』）")
ok(r_m in ("pending", "skip"), "信号日缺失时诚实返回 pending/skip")

# ================================================================ BUG B

print()
print("=" * 62)
print("BUG B：overall 必须按 (日期,代码) 去重")
print("=" * 62)


def E(date, code, sig, ret1):
    return {"id": f"{date}:{code}:{sig}", "date": date, "code": code,
            "name": "x", "signal": sig, "signal_name": sig,
            "status": "partial", "ret1": ret1, "ret3": None, "ret5": None}


mix = [
    E("20261013", "sh600000", "B0", -0.05),
    E("20261013", "sh600000", "B1", -0.05),   # 同一票同一天，重复
    E("20261013", "sh600000", "B2", -0.05),   # 再来一条，重复
    E("20261013", "sh600001", "B1", +0.03),
    E("20261014", "sh600000", "B1", +0.02),   # 不同日期，不算重复
]
ded = journal._dedup_by_stock(mix)
print(f"  输入 {len(mix)} 条 → 去重后 {len(ded)} 条（期望 3）")
ok(len(ded) == 3, "同 (日期,代码) 只留 1 条，不同日期各自保留")

kept = [x for x in ded if x["date"] == "20261013" and x["code"] == "sh600000"]
ok(len(kept) == 1, "20261013/sh600000 保留且仅保留 1 条")
if kept:
    print(f"  保留的信号 = {kept[0]['signal']}（期望 B2，优先级最高）")
    ok(kept[0]["signal"] == "B2",
       "保留信号优先级最高的那条（B2 > W1 > W2 > B1 > B0）")

# stats 的两个口径：overall 去重，rows 不去重
st = journal.stats(mix)
print(f"  stats()['overall']['n'] = {st['overall']['n']}（期望 3，去重后）")
ok(st["overall"]["n"] == 3, "stats() 的 overall 走去重后的样本数")

n_rows = sum(r["n"] for r in st["stats"])
print(f"  分信号 rows 样本合计 = {n_rows}（期望 5，不去重）")
ok(n_rows == 5, "分信号 rows 保持不去重（每个信号各算各的命中率）")

# 回到真实的 journal 数据：去重前后必须真的有差（否则测试是空转）
import json  # noqa: E402
jp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "signal_journal.json")
if os.path.exists(jp):
    with open(jp, "r", encoding="utf-8") as f:
        real = (json.load(f) or {}).get("entries") or []
    sel = [x for x in real
           if x.get("status") in ("settled", "partial") and x.get("ret1") is not None]
    if sel:
        n_uniq = len(journal._dedup_by_stock(sel))
        print(f"  真实档案：记录 {len(sel)} → 唯一票 {n_uniq}")
        ok(n_uniq <= len(sel), "真实档案去重后样本数不会变多")
        if n_uniq < len(sel):
            ok(True, f"真实档案确实存在重复票（虚增 {len(sel) - n_uniq} 条），BUG B 非空想")
        else:
            print("  ..  真实档案当前无重复票（去重逻辑仍已就位）")
else:
    print("  ..  无 data/signal_journal.json（运行期数据），跳过真实数据校验")

# ================================================================ 汇总

print()
print("=" * 62)
print(f"通过 {okc} / 失败 {badc}")
print("=" * 62)
sys.exit(1 if badc else 0)
