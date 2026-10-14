# -*- coding: utf-8 -*-
"""E10 轻量回测的测试（core/backtest.py）。

重点不是「算得对不对」这种自证，而是钉住几条**容易悄悄写错**的口径：
  1. 组合级去重：同一只票同一天命中两个信号，不能给组合贡献两次收益；
  2. 净值聚合：等权平均、按日推进，不是把逐笔收益直接相加；
  3. 回撤：必须是「峰值→谷底」，不是「起点→最低点」；
  4. 夏普/年化：样本不足时**返回 None**，绝不拿十几天数据编年化；
  5. 分档诊断：不去重会让分档结论反转（实测踩过）；
  6. 空输入 / 脏数据不抛异常（回测带崩主扫描是最不可接受的失败）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import backtest as bt        # noqa: E402

PASS = FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


def eq(a, b, msg):
    ok(a == b, f"{msg}（得到 {a!r}，期望 {b!r}）")


def near(a, b, msg, tol=1e-6):
    ok(a is not None and abs(a - b) < tol, f"{msg}（得到 {a!r}，期望 {b!r}）")


def T(date, code, ret, score=None, signal="B0", name="退潮试错仓"):
    return {"date": date, "code": code, "name": code, "sector": "测试",
            "signal": signal, "signal_name": name, "score": score, "ret": ret}


print("=== [1] 净值聚合：等权平均，不是求和 ===")
# 同一天两笔 +10% 和 -10% → 等权平均 0%（不是 0 也不是 +20%）
c = bt.equity_curve([T("20260101", "a", 0.10), T("20260101", "b", -0.10)])
eq(c["dates"], ["20260101"], "两个日期点压成一天")
near(c["daily"][0], 0.0, "等权平均 = 0", tol=1e-9)
near(c["nav"][-1], 1.0, "净值不变")
# 两天各 +10% → 累计 21%（复利，不是 20%）
c2 = bt.equity_curve([T("20260101", "a", 0.10), T("20260102", "b", 0.10)])
near(c2["nav"][-1], 1.21, "两日复利 1.1×1.1=1.21")
eq(c2["span_days"], 2, "两个交易日")

print()
print("=== [2] ★ 组合级去重：同票同日两个信号只算一笔 ===")
# 天融信 20261009 同时命中 B0 与 B1，收益完全相同 —— 合成净值时只能算一次。
# 不去重的话日收益会被同一笔行情绑架（实测胜率 65.3% vs 67.9%）。
dups = [T("20260101", "sz002212", -0.10, signal="B0"),
        T("20260101", "sz002212", -0.10, signal="B1"),
        T("20260101", "sh600000", 0.10, signal="B0")]
c3 = bt.equity_curve(dups)
eq(c3["trades_n"], 2, "去重后 2 笔（原 3 条）")
near(c3["daily"][0], 0.0, "去重后等权平均 = (+10% -10%)/2 = 0")
# 不去重会得到 (-0.10-0.10+0.10)/3 = -3.33% —— 同一个行情被算两次
ok(abs(c3["daily"][0] - (-0.10 + -0.10 + 0.10) / 3) > 1e-6,
   "与「不去重」的结果明显不同（证明去重真的生效了）")

print()
print("=== [3] 最大回撤：峰值→谷底，不是起点→最低 ===")
# 净值 1.0 → 1.2 → 0.9 → 1.0：从峰值 1.2 跌到 0.9 = -25%
mdd, pi, ti = bt.max_drawdown([1.0, 1.2, 0.9, 1.0])
near(mdd, -0.25, "回撤 -25%（峰值 1.2 → 谷底 0.9）", tol=1e-6)
eq(pi, 1, "峰值下标 = 1")
eq(ti, 2, "谷底下标 = 2")
# 只涨不跌 → 0
near(bt.max_drawdown([1.0, 1.1, 1.2])[0], 0.0, "单边上涨回撤为 0")
# 空 → 0，不抛
near(bt.max_drawdown([])[0], 0.0, "空输入回撤 0")
# 起点就最低、后来涨回来：回撤不是拿起点算
near(bt.max_drawdown([0.9, 1.0, 1.1])[0], 0.0, "起点最低不算回撤")

print()
print("=== [4] ★ 样本不足时夏普/年化必须是 None，不许编数 ===")
few = [T("2026010%d" % (i + 1), "c%d" % i, 0.01) for i in range(5)]
c4 = bt.equity_curve(few)
s4 = bt.summarize(c4, [t["ret"] for t in few])
ok(s4["sharpe"] is None, "5 天样本 → 夏普 None")
ok(s4["annual"] is None, "5 天样本 → 年化 None")
ok(s4["calmar"] is None, "年化 None → 卡玛也 None")
eq(s4["min_days_for_annual"], bt.MIN_DAYS_FOR_ANNUAL, "如实告知需要多少天")
# 足够天数才给年化（造 70 个交易日，收益要有波动 —— 每天都是同一个数字
# 的标准差为 0，夏普在数学上无定义，那是**对的**行为，不是 bug）
import random                                                # noqa: E402
rnd = random.Random(42)
many = [T("2026%02d%02d" % (1 + i // 28, 1 + i % 28), "d%d" % i,
          rnd.uniform(-0.01, 0.02)) for i in range(70)]
c5 = bt.equity_curve(many)
s5 = bt.summarize(c5, [t["ret"] for t in many])
ok(s5["annual"] is not None, "70 天样本 → 给年化")
ok(s5["sharpe"] is not None, "70 天有波动的样本 → 给夏普")
eq(s5["daily_n"], 70, "70 个日收益样本")
# 收益完全无波动时夏普应为 None（标准差 0，无定义），而不是编一个 0 或无穷
flat = [T("2026%02d%02d" % (1 + i // 28, 1 + i % 28), "e%d" % i, 0.001)
        for i in range(70)]
ok(bt.summarize(bt.equity_curve(flat),
                [t["ret"] for t in flat])["sharpe"] is None,
   "收益零波动 → 夏普 None（无定义，不编数）")

print()
print("=== [5] 盈亏比 / 成本口径 ===")
near(bt.profit_factor([0.1, 0.1, -0.1]), 2.0, "盈利 0.2 / 亏损 0.1 = 2.0")
ok(bt.profit_factor([0.1, 0.2]) is None, "没有亏损样本 → 盈亏比 None（不是无穷大）")
near(bt.profit_factor([-0.1]), 0.0, "全是亏损 → 盈亏比 0（不是 None）")
ok(bt.profit_factor([]) is None, "空 → None")

print()
print("=== [6] ★ 分档诊断：不去重会得出反向结论 ===")
# 造一个「评分越高收益越高」的干净样本，验证能被识别为 up
clean = []
for i in range(20):
    clean.append(T("20260101", "e%d" % i, 0.01 * i, score=60 + i * 2))
b = bt.by_score_band(clean)
ok(len(b) == 4, "分成 4 档")
eq(b[0]["_diag"]["monotonic"], "up", "评分越高收益越高 → 识别为 up")
ok(b[0]["_diag"]["conclusive"] is True, "极差足够大 → 结论有效")
ok(b[0]["_diag"]["ok"] is True, "方向一致 → ok")
# 造一个反向的
rev = []
for i in range(20):
    rev.append(T("20260101", "f%d" % i, -0.005 * i, score=60 + i * 2))
b2 = bt.by_score_band(rev)
eq(b2[0]["_diag"]["monotonic"], "down", "评分越高越亏 → 识别为 down")
ok(b2[0]["_diag"]["ok"] is False,
   "★ 方向是反的 → ok=False（踩过：原来 conclusive 对 up/down 都真，"
   "反向因子也被判成可用）")
ok("方向是反的" in b2[0]["_diag"]["note"],
   "反向时 note 明说「分档方向是反的」，而不是含糊地说有效")
# 样本太少 → 直接返回空，不给假结论
ok(bt.by_score_band([T("20260101", "g", 0.01, score=70)]) == [],
   "样本不足 → 空列表（宁可不给结论）")

print()
print("=== [7] 分档去重：同票同日只进一档 ===")
# 8 只票，每只两条重复记录 → 去重后 8 笔，够 2 档
dup_band = []
for i in range(8):
    dup_band.append(T("20260101", "h%d" % i, 0.01 * i, score=60 + i, signal="B0"))
    dup_band.append(T("20260101", "h%d" % i, 0.01 * i, score=60 + i, signal="B1"))
bb = bt.by_score_band(dup_band)
if bb:
    eq(sum(x["n"] for x in bb), 8, "去重后每档样本合计 = 8（不是 16）")
    ok(bb[0]["_diag"]["n"] == 8, "诊断里的 n = 8")
else:
    ok(False, "去重后应有 8 笔可分档")

print()
print("=== [8] build()：样本不足要如实说，不硬下结论 ===")
# 注意：hold_days=5 时读的是 ret5 —— 造数据要带上对应字段，
# 否则会被算成"还在跟踪"（第一次就写错了这个，n_trades=0）。
r = bt.build([T("20260101", "x", 0.05)], hold_days=5, min_samples=10)
eq(r["enough"], False, "1 笔 < 10 笔 → enough=False")
ok("不足以当结论" in r["note"], "note 里明说样本不够")
eq(r["n_trades"], 0, "只有 ret1、没有 ret5 → 这一笔还算「跟踪中」")
eq(r["n_pending"], 1, "n_pending 如实记 1 笔")
# 带上 ret5 才进组合
r1 = bt.build([{**T("20260101", "x", 0.05), "ret5": 0.05}],
              hold_days=5, min_samples=10)
eq(r1["n_trades"], 1, "带 ret5 → 进样本")
ok(r1["enough"] is False, "1 笔仍不够")
# hold_days 不合法 → 回退 5
eq(bt.build([], hold_days=99)["hold_days"], 5, "非法 hold_days 回退 5")

print()
print("=== [9] build()：脏数据 / 空输入不抛异常 ===")
for bad in ([], None, [{}], [{"date": None, "ret1": None}],
            [{"date": "20260101", "code": None, "ret1": "abc"}],
            [{"date": "bad-date", "code": "z", "ret1": 0.1}]):
    try:
        out = bt.build(bad or [])
        ok(True, f"输入 {str(bad)[:32]}… 不抛异常")
    except Exception as e:                                   # noqa: BLE001
        ok(False, f"输入 {str(bad)[:32]}… 抛了 {type(e).__name__}: {e}")
# run() 在异常时必须返回带 error 的 dict，而不是崩
bad_cfg = {"E10_backtest": {"enabled": True, "hold_days": "不是数字"}}
out = bt.run(bad_cfg, entries=[T("20260101", "a", 0.1)])
ok(isinstance(out, dict), "hold_days 是脏字符串也不崩（run 回退默认值）")
out2 = bt.run({"E10_backtest": {"enabled": False}}, entries=[])
eq(out2["enabled"], False, "enabled=False → 直接返回关闭态")

print()
print("=== [10] 真实档案跑通（有档案就用真的，没有就跳过） ===")
import io                                                    # noqa: E402
import json                                                  # noqa: E402
jp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "signal_journal.json")
if os.path.exists(jp):
    try:
        entries = json.load(io.open(jp, encoding="utf-8")).get("entries") or []
    except Exception:                                        # noqa: BLE001
        entries = []
    if entries:
        rr = bt.build(entries, hold_days=1)
        ok(rr["n_trades"] >= 0, f"真实档案 {len(entries)} 条跑通")
        ok(rr["summary"]["total_ret"] is not None, "有组合总收益")
        # 去重计数必须自洽
        eq(rr["n_uniq"] + rr["n_dup"], rr["n_trades"],
           "去重后 + 重复数 = 总数")
        print(f"       实测：{rr['n_trades']} 笔已结算，"
              f"去重后 {rr['n_uniq']} 笔，重复 {rr['n_dup']} 笔，"
              f"总收益 {rr['summary']['total_ret']*100:+.2f}%")
    else:
        print("       档案为空，跳过")
else:
    print("       没有 signal_journal.json，跳过")

print()
print("=== [11] ★ 自动降档：想要的口径没样本时不许给空白页 ===")
# 实测场景：系统刚上线，ret5 有 0 笔、ret1 有 72 笔。
# 死守 hold_days=5 的话页面永远是空的，用户看不出这功能存在。
only1 = [{**T("20260101", "m%d" % i, 0.01), "ret1": 0.01}
         for i in range(20)]
h, why = bt.pick_hold_days(only1, want=5, min_samples=10)
eq(h, 1, "ret5 没样本 → 自动降到 ret1")
ok("5 日的结果还没到期" in why, "说明文字讲清了为什么降档")
# 想要的那一档够了 → 就用它，不给多余说明
both = [{**T("2026010%d" % (i % 9 + 1), "n%d" % i, 0.01),
         "ret1": 0.01, "ret5": 0.02} for i in range(20)]
h2, why2 = bt.pick_hold_days(both, want=5, min_samples=10)
eq(h2, 5, "ret5 样本够 → 用 ret5")
eq(why2, "", "不需要降档时不给多余说明")
# 全都样本不足 → 仍要给一个（最多样本的）口径，而不是崩或空白
few = [{"date": "20260101", "code": "p", "ret1": 0.01, "ret5": None}]
h3, why3 = bt.pick_hold_days(few, want=5, min_samples=10)
eq(h3, 1, "全都不足 → 用样本最多的那一档")
ok("都还不足" in why3, "如实说明各档样本都不足")
# run() 端到端：want=5、只有 ret1 → 出来的是 1 日口径且带说明
r2 = bt.run({"E10_backtest": {"enabled": True, "hold_days": 5,
                             "min_samples": 10}}, entries=only1)
eq(r2["hold_days"], 1, "run() 自动降档")
eq(r2["hold_days_want"], 5, "原想要的口径如实保留")
ok("1 日口径" in r2["note"], "note 里带上口径说明")
ok(r2["n_trades"] > 0, "降档后真的有样本（不是空白页）")

print()
print(f"===== {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
