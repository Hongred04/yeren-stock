# -*- coding: utf-8 -*-
"""信号区分度检验（core/discrimination.py）测试。

这份测试的重点不是"函数能跑"，而是**每个判据真的会在该触发时触发** ——
一个永远返回"可用"的检验器比没有检验器更糟。
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import discrimination as dc       # noqa: E402

PASS = 0
FAIL = 0


def ok(c, m):
    global PASS, FAIL
    if c:
        PASS += 1
        print(f"  ok   {m}")
    else:
        FAIL += 1
        print(f"  FAIL {m}")


def eq(a, b, m):
    ok(a == b, f"{m}（得到 {a!r}，期望 {b!r}）")


def mk(score_ret_pairs):
    return [{"score": s, "ret": r} for s, r in score_ret_pairs]


print("=== [1] 好因子：分数越高收益越好，应判「可用」 ===")
random.seed(7)
rows = []
for i in range(300):
    s = random.uniform(0, 100)
    # 收益与分数正相关 + 噪音
    r = (s / 100.0) * 0.10 - 0.02 + random.gauss(0, 0.02)
    rows.append({"score": s, "ret": r})
res = dc.evaluate(rows, name="好因子")
eq(res["usable"], True, "四检全过")
eq(res["g1_pass"], True, "G1 顶档占比过关")
eq(res["g2_pass"], True, "G2 档数过关")
eq(res["g3_pass"], True, "G3 lift 过关")
eq(res["g4_pass"], True, "G4 单调性过关")
ok("可用" in res["verdict"], f"结论写明可用（{res['verdict']}）")

print()
print("=== [2] 塌缩因子：82.7% 挤在顶档（ticai-daban 实测的 qscore 现象）===")
rows = []
for i in range(300):
    # 270 个都是最高分，30 个是低分 —— 典型塌缩
    s = 99.0 if i < 248 else random.uniform(0, 60)
    r = random.gauss(0.01, 0.03)
    rows.append({"score": s, "ret": r})
res = dc.evaluate(rows, name="塌缩因子")
eq(res["g1_collapsed"], True, "G1 判出塌缩")
eq(res["g1_pass"], False, "G1 不过关")
eq(res["usable"], False, "不能算可用")
ok("塌缩" in res["verdict"], f"结论点名塌缩（{res['verdict']}）")
ok(res["g1_top_share"] > 0.70, f"顶档占比 {res['g1_top_share']:.2f} > 0.70")

print()
print("=== [3] 反向因子：分数越高收益越差，必须点出方向反了 ===")
rows = []
for i in range(300):
    s = random.uniform(0, 100)
    r = -(s / 100.0) * 0.10 + random.gauss(0, 0.02)
    rows.append({"score": s, "ret": r})
res = dc.evaluate(rows, name="反向因子")
# ★ 反向但单调的因子**是**强信号（只是要用负权重），所以 G4 应判过 ——
#   把它判成"不单调"会误导使用者丢掉一个有信息量的因子。
#   真正要区分的是：反向单调 vs 忽高忽低（噪音）。
eq(res["g4_pass"], True, "G4 过关（ρ=-1.0 方向稳定，属强信号）")
eq(res["g4_reversed"], True, "标出方向是反的")
ok(res["g4_rho"] is not None and res["g4_rho"] < -0.8,
   f"秩相关显著为负（{res['g4_rho']}）")
ok("反" in res["verdict"], f"结论点出方向反了（{res['verdict']}）")
ok("负权重" in res["verdict"] or "负权重" in (res.get("g4_note") or ""),
   "提示要用负权重，而不是让人以为这因子没用")
eq(res["usable"], False, "不能直接算可用（需换权重/反向解释）")

print()
print("=== [4] 纯噪音因子：各档收益都一样，应判「无区分度」 ===")
rows = [{"score": random.uniform(0, 100),
         "ret": random.gauss(0.005, 0.03)} for _ in range(400)]
res = dc.evaluate(rows, name="噪音因子")
eq(res["usable"], False, "不能算可用")
eq(res["g3_pass"], False, "G3 不过关（顶档不比底档好）")
eq(res["g4_pass"], False, "G4 不过关（忽高忽低）")
eq(res["g4_reversed"], False, "噪音**不是**反向信号（不许把噪音说成反向）")
ok("方向不一致" in res["verdict"] or "无区分度" in res["verdict"],
   f"结论点明问题（{res['verdict']}）")

print()
print("=== [5] 样本不足时**不许**给结论 ===")
rows = mk([(i, i * 0.001) for i in range(10)])
res = dc.evaluate(rows, name="小样本")
eq(res["usable"], False, "样本不足不算可用")
eq(res["verdict"], "样本不足", "结论直言样本不足")
ok("不可信" in res["note"] or "攒样本" in res["note"],
   f"说明为什么不下结论（{res['note']}）")
# 边界：恰好 40 条（MIN_TOTAL）→ 应开始出结论
rows = mk([(i % 50, (i % 50) * 0.001 + random.gauss(0, 0.005))
           for i in range(dc.MIN_TOTAL)])
res = dc.evaluate(rows, name="边界")
ok(res["verdict"] != "样本不足", f"{dc.MIN_TOTAL} 条时不再说样本不足")

print()
print("=== [6] 脏数据不许抛出异常 ===")
for bad in ([], None, [{}], [{"score": None, "ret": 1}],
            [{"score": "x", "ret": "y"}],
            [{"score": float("nan"), "ret": float("inf")}],
            [{"score": 1, "ret": None}]):
    try:
        r = dc.evaluate(bad)
        ok(r["usable"] is False, f"脏输入 {str(bad)[:28]} 安全返回不可用")
    except Exception as e:                                    # noqa: BLE001
        ok(False, f"脏输入 {str(bad)[:28]} 抛了 {type(e).__name__}: {e}")

print()
print("=== [7] lift 的除零保护：底档均值≈0 时不许给出天文数字 ===")
# 底档收益均值恰好 0
rows = []
for i in range(200):
    if i < 40:
        rows.append({"score": 1.0, "ret": 0.0})
    else:
        rows.append({"score": 100.0 - i * 0.1, "ret": 0.05})
res = dc.evaluate(rows, name="零底档")
ok(res.get("g3_lift") is None or res["g3_lift"] < 1000,
   f"不给爆炸的 lift（{res.get('g3_lift')}）")
ok("g3_delta" in res or res.get("g3_lift") is not None,
   "改用差值或给出合理 lift")

print()
print("=== [8] 自定义字段名与档数 ===")
rows = [{"qscore": random.uniform(0, 10),
         "ret5": random.uniform(0, 10) * 0.02} for _ in range(200)]
res = dc.evaluate(rows, bins=4, name="自定义", score_key="qscore",
                  ret_key="ret5")
eq(len(res["bins"]), 4, "按 4 档切")
eq(res["n"], 200, "样本数正确")
eq(res["bins"][0]["n"] + res["bins"][-1]["n"], 100,
   "分位数切档每档各占 1/4")

print()
print("=== [9] report() 批量汇总 ===")
good = [{"score": random.uniform(0, 100), "ret": random.uniform(0, 100) * 0.001}
        for _ in range(200)]
bad = [{"score": 99.0, "ret": random.gauss(0, 0.01)} for _ in range(200)]
r = dc.report([{"name": "A", "rows": good}, {"name": "B", "rows": bad}])
eq(r["n_tested"], 2, "测了 2 个")
ok(r["n_unusable"] >= 1, "至少认出 1 个不可用")
ok(any(x["name"] == "B" for x in r["unusable"]),
   "不可用清单里点到了 B")

print()
print(f"===== {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
