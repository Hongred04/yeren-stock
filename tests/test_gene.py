# -*- coding: utf-8 -*-
"""涨停基因（E11）测试。

这个模块最容易出的错不是算错，而是**用未来数据算历史** —— 一旦发生，
回测里的因子会"变得特别有效"，而实盘毫无用处，且很难发现。
所以本测试的重点是钉住那条防线，以及钉住几个刻意的取舍（哪些因子接了、
哪些没接、为什么），防止后来者"顺手补全"。

钉死的东西：
  [1] Wilson 下界对样本量收缩（1 中 1 不能给 100%）
  [2] 逐日快照的时点语义 —— 第 k 天快照不得含第 k 天及以后的记录
  [3] 全量表 vs 快照表的用途区分（拿全量表评历史样本 = 未来函数）
  [4] 次日缺席判定与 min_pool 守卫
  [5] seal_score 的门槛式行为（够顶档给分、不够 0 分、非首板不给、历史不足不给）
  [6] freq 有意不接入（它的效应在同板数内部反向 —— 接了就是错的）
  [7] 脏数据/空输入不抛异常
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gene   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def mkrow(code, boards=1, name="测试"):
    return {"code": code, "name": name, "boards": boards,
            "price": 10.0, "pct": 10.0}


def mkpool(codes, boards=1):
    return [mkrow(c, boards) for c in codes]


def pad(codes, n=12, boards=1):
    """把 codes 补到 n 只 —— 池子小于 gene.MIN_POOL 时该日会被跳过，
    所以造数据必须给到真实量级的池子规模，否则测的是守卫而不是逻辑。"""
    out = list(codes)
    i = 0
    while len(out) < n:
        c = f"pad{i}"
        if c not in out:
            out.append(c)
        i += 1
    return mkpool(out, boards)


def main():
    global okc, badc
    okc = badc = 0

    # ── [1] Wilson 下界 ─────────────────────────────────────────
    print("\n[1] Wilson 下界收缩")
    ok(gene.wilson_lower(0, 0) == 0.0, "n=0 → 0（不除零）")
    ok(gene.wilson_lower(1, 1) < 0.35,
       f"1 中 1 → {gene.wilson_lower(1, 1):.2f}（不是 100%，小样本收缩）")
    ok(gene.wilson_lower(10, 20) > gene.wilson_lower(1, 2),
       "同样 50% 命中率，样本多的下界更高（样本量被正确计价）")
    ok(gene.wilson_lower(5, 5) > gene.wilson_lower(1, 1),
       "5 中 5 的下界高于 1 中 1（更多证据 = 更高置信）")
    ok(0.0 <= gene.wilson_lower(0, 5) <= 0.5, "0 中 5 → 非负且合理")

    # ── [2][3] 逐日快照的时点语义（最重要） ──────────────────────
    print("\n[2] 逐日快照 —— 不得含当天及以后的记录")
    # 造：A 在第 0、1 天连续涨停，第 2 天不在池
    #     B 只在第 3 天涨停
    dates = ["d0", "d1", "d2", "d3", "d4"]
    pools = {
        "d0": pad(["A"]),
        "d1": pad(["A"], boards=2),
        "d2": pad(["C"]),
        "d3": pad(["B"]),
        "d4": pad(["C"]),
    }
    snaps = {}
    table = gene.build({}, dates, pools, snapshots=snaps)
    ok(set(snaps.keys()) == set(dates), f"每天都出快照（{len(snaps)} 份）")

    # A 在第 0 天的快照里必须"没有任何历史"（它的第一次涨停发生在 d0 当天）
    s0 = snaps["d0"]
    ok("A" not in s0 or s0["A"]["n_zt"] == 0,
       "★ d0 快照里 A 的历史为 0 —— 当天记录尚未计入（防未来函数）")
    # 到 d2 的快照，A 在 d0、d1 的两次记录应已计入
    s2 = snaps["d2"]
    ok(s2.get("A", {}).get("n_zt") == 2,
       f"d2 快照里 A 已含 d0/d1 两次记录（实得 {s2.get('A', {}).get('n_zt')}）")
    # B 在 d3 当天才涨停，d3 快照里必须还没有 B
    ok("B" not in snaps["d3"] or snaps["d3"]["B"]["n_zt"] == 0,
       "★ d3 快照里 B 的历史为 0 —— 它是当天才涨停的")
    ok(snaps["d4"].get("B", {}).get("n_zt") == 1,
       "d4 快照里 B 的历史已计入")

    # 全量表（返回值）反映的是截至最后一天 —— 与快照用途不同
    print("\n[3] 全量表 vs 快照表用途区分")
    ok(table.get("A", {}).get("n_zt") == 2,
       "全量表里 A 的历史是 2（截至最后一天）")
    ok(snaps["d0"].get("A", {}).get("n_zt", 0) != table.get("A", {}).get("n_zt"),
       "★ d0 快照 ≠ 全量表 —— 两者必须能区分（混用即未来函数）")

    # ── [4] 次日缺席与 min_pool ─────────────────────────────────
    print("\n[4] 次日缺席判定与 min_pool 守卫")
    # 池子小于 min_pool 时「缺席」不作数：造一个当日只有 5 只的池
    d2s = ["x%d" % i for i in range(5)]
    dates2 = ["e0", "e1", "e2"]
    pools2 = {
        "e0": mkpool(d2s),                 # 5 只 < MIN_POOL(10) → 该日应被跳过
        "e1": pad(["zz"]),                 # 次日池够大
        "e2": pad(["zz"]),
    }
    t2 = gene.build({}, dates2, pools2)
    # 池子 < min_pool 的当天整日跳过 → 那天的票不应带着记录进表
    ok("x0" not in t2 and "x1" not in t2,
       "★ 当天池子 < min_pool → 该日整日跳过，其票不进表（缺席不可信）")
    ok(t2.get("zz", {}).get("n_zt") == 1,
       "正常规模的日子的票照常进表（守卫没有误伤）")

    # 正常规模下，缺席 = 未涨停，应被计入命中率分母
    big_a = ["a%d" % i for i in range(12)]
    big_b = ["b%d" % i for i in range(12)]
    dates3 = ["f0", "f1", "f2", "f3"]
    pools3 = {
        "f0": mkpool(big_a),
        "f1": mkpool(big_b),          # a* 全部缺席
        "f2": mkpool(big_b),
        "f3": mkpool(big_b),
    }
    t3 = gene.build({}, dates3, pools3)
    a0 = t3.get("a0", {})
    ok(a0.get("n_first") == 1, "a0 有 1 次首板记录")
    ok(a0.get("n_first_hit") == 0, "★ a0 次日缺席 → 记为未命中（不是不计）")

    # ── [5] seal_score 门槛行为 ─────────────────────────────────
    print("\n[5] seal_score 门槛式行为")
    hi = {"known": True, "seal": 0.20, "n_first": 5, "n_first_hit": 2}
    lo = {"known": True, "seal": 0.02, "n_first": 5, "n_first_hit": 1}
    s_hi, why_hi = gene.seal_score(hi, 1)
    s_lo, why_lo = gene.seal_score(lo, 1)
    ok(s_hi == gene.SEAL_W, f"够顶档 → 满分 {s_hi}（门槛式，非线性）")
    ok(s_lo == 0.0 and why_lo == "", "不够顶档 → 0 分且不给理由")
    ok(bool(why_hi), "够档时写出理由")
    ok(gene.seal_score(hi, 2)[0] == 0.0,
       "★ 2 板不给分（候选口径只要首板，给分无可执行含义）")
    ok(gene.seal_score(hi, 0)[0] == 0.0, "boards=0 → 不给分")
    ok(gene.seal_score({"known": True, "seal": 0.9, "n_first": 1,
                        "n_first_hit": 1}, 1)[0] == 0.0,
       "★ 只有 1 次历史 → 不给分（1 个样本编不出结论）")
    ok(gene.seal_score({"known": False, "seal": 0.5}, 1)[0] == 0.0,
       "无历史 → 不给分")
    # 门槛边界
    ok(gene.seal_score({"known": True, "seal": gene.TOP_SEAL,
                        "n_first": 3, "n_first_hit": 1}, 1)[0] == gene.SEAL_W,
       "恰好等于门槛 → 给分（闭区间，避免浮点卡边界）")
    ok(gene.seal_score({"known": True, "seal": gene.TOP_SEAL - 1e-9,
                        "n_first": 3, "n_first_hit": 1}, 1)[0] == 0.0,
       "略低于门槛 → 不给分")

    # ── [8] 配置覆盖真的生效（不是假旋钮） ──────────────────────
    print("\n[8] 门槛/样本/权重可由配置覆盖")
    gg = {"known": True, "seal": 0.15, "n_first": 5, "n_first_hit": 2}
    # 默认（不传）＝用模块标定常量
    ok(gene.seal_score(gg, 1)[0] == gene.SEAL_W, "不传参数 → 用标定常量（默认行为不变）")
    # 抬高门槛 → 0.15 已经不够了
    ok(gene.seal_score(gg, 1, top_seal=0.30)[0] == 0.0,
       "★ 抬高 gene_top_seal → 该票不再得分（配置真的生效）")
    # 降低门槛 → 原本不够的票也能得分
    ok(gene.seal_score(gg, 1, top_seal=0.05)[0] == gene.SEAL_W,
       "★ 降低 gene_top_seal → 原本不够的票得分")
    # 抬高最小样本 → 5 次不够
    ok(gene.seal_score(gg, 1, min_first=10)[0] == 0.0,
       "★ 抬高 gene_min_first → 样本不够不给分")
    # 权重可改
    ok(gene.seal_score(gg, 1, weight=20)[0] == 20.0, "权重可由配置覆盖")
    ok(gene.seal_score(gg, 1, weight=0)[0] == 0.0, "权重可关成 0")
    # 理由里的百分比仍然按原始命中率算（不能因为门槛变了就改口径）
    _, why = gene.seal_score(gg, 1)
    ok("40%" in why, f"理由仍写原始命中率 2/5=40%（实得 {why}）")

    # ── [6] freq 有意不接入 ─────────────────────────────────────
    print("\n[6] 涨停频率有意不接入评分")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "core", "gene.py"),
        encoding="utf-8").read()
    ok("def freq_score" not in src and "def gene_score" not in src,
       "★ 没有为 freq 提供评分函数（它同板数内方向会翻，接了就是错的）")
    ok('"freq"' in src, "freq 仍然算出来并保留在表里（供展示/诊断，不参与打分）")

    # ── [7] 脏数据 / 空输入 ─────────────────────────────────────
    print("\n[7] 脏数据与空输入不抛异常")
    ok(gene.build({}, [], {}) == {}, "空日期 → 空表")
    ok(gene.build({}, ["d0"], {"d0": mkpool(["A"])}) == {},
       "只有一天 → 无法判次日，返回空表（不抛异常）")
    ok(gene.of("nope", {})["known"] is False, "of() 未知代码 → known=False")
    ok(gene.of("x", None)["known"] is False, "of() 表为 None → 不抛异常")
    ok(gene.summary(None) != "", "summary(None) → 仍给一句人话")
    # 缺 code / boards 缺失的行
    bad_pool = [{"name": "无代码", "boards": 1}, {"code": "Z"}]
    try:
        gene.build({}, ["g0", "g1"], {"g0": bad_pool, "g1": mkpool(["Y"])})
        ok(True, "缺 code 的行被跳过，不抛异常")
    except Exception as e:                                   # noqa: BLE001
        ok(False, f"缺 code 的行导致异常：{e}")
    # boards 为 None / 字符串
    try:
        r = gene.build({}, ["h0", "h1", "h2"],
                       {"h0": [{"code": "Q", "boards": None}],
                        "h1": mkpool(["Q"]), "h2": mkpool(["Q"])})
        ok(isinstance(r, dict), "boards=None 不抛异常")
    except Exception as e:                                   # noqa: BLE001
        ok(False, f"boards=None 导致异常：{e}")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
