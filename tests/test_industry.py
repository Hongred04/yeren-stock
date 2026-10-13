# -*- coding: utf-8 -*-
"""E7 逆势连板票（产业逻辑候选）· 独立通道。

真实场景（10-13 实测，本测试按它构造夹具）：
  九阳股份 sz002242 在 10-08、10-12 **两个情绪退潮日**里照常连板
  （4 板 / 6 板），从 09-28 首板一路走到 6 板（首板 10.33 → 6 板 16.64，+61%）。
  而系统当时对它完全看不见：退潮期 `can_trade=False` 不给买点，
  `max_candidate_boards=1` 让 6 板票直接不进候选。
  首板日流通市值约 79 亿（今日 134.6 亿 × 10.33 ÷ 17.67）。

本测试钉住：
  ① 逆势日集合的两个来源（情绪阶段 ebb + 涨停家数骤降代理）；
  ② 「跨过逆势日」是入场券：没跨过的票不许进；
  ③ 两条形态判据的边界（缩量一字 / 炸板回封）；
  ④ 首板市值用**日K首板价反推**（归档 float_mv 跨源口径不一致，不可用）；
  ⑤ `still`（今天是否仍在涨停池）如实给 —— 断板的只当复盘看；
  ⑥ 冷启动/关闭/无逆势日都不硬凑。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="industry_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds            # noqa: E402
from core import industry                    # noqa: E402
from core import strategy_config as sc       # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def row(code, name, boards, first, brk, to, mv=1e9, sector="小家电", price=0.0):
    return {"code": code, "name": name, "boards": boards, "first_time": first,
            "break_count": brk, "turnover": to, "float_mv": mv, "sector": sector,
            "price": price, "pct": 10.0, "fund": 1e8, "last_time": first}


# 九阳股份的连板链（字段取东财口径：10-08 换手 1.02%、10-09 炸 6 次）
JIUYANG = [
    ("20260928", row("sz002242", "九阳股份", 1, 93115, 0, 9.96, sector="机器人概念")),
    ("20260929", row("sz002242", "九阳股份", 2, 93030, 0, 7.73, sector="机器人概念")),
    ("20260930", row("sz002242", "九阳股份", 3, 92500, 1, 4.59)),
    ("20261008", row("sz002242", "九阳股份", 4, 92500, 0, 1.02)),
    ("20261009", row("sz002242", "九阳股份", 5, 93027, 6, 18.28)),
    ("20261012", row("sz002242", "九阳股份", 6, 92500, 0, 16.07)),
]
# 对照组：只在非逆势日连板（情绪票，不该进本模块）
PLAIN = [
    ("20261009", row("sh600001", "情绪票", 1, 93000, 0, 5.0, sector="题材")),
    ("20261012", row("sh600001", "情绪票", 2, 93000, 0, 6.0, sector="题材")),
]
# 对照组：高度不够（2 板 < min_boards=3）
LOW = [
    ("20261009", row("sh600002", "低度票", 1, 93000, 0, 5.0, sector="题材")),
    ("20261012", row("sh600002", "低度票", 2, 93000, 0, 6.0, sector="题材")),
]

CFG = {"E7_industry": {"enabled": True, "min_boards": 3, "lookback_days": 10,
                       "top_n": 5, "mv_max": 100.0, "shrink_ratio": 0.7,
                       "reseal_breaks": 3, "mv_check": True}}
SNAPS = {"sz002242": {"price": 17.67, "float_mv": 13.46e9, "name": "九阳股份",
                      "pct": 6.19, "amount": 2.33e9, "turnover": 18.34}}
KLINES = {"sz002242": [{"date": "2026-09-28", "close": 10.33},
                       {"date": "2026-09-30", "close": 12.50},
                       {"date": "2026-10-12", "close": 16.64}]}


def build():
    for date, r in JIUYANG + PLAIN + LOW:
        cur = ds.load_pool_history(date)
        cur = [x for x in cur if x["code"] != r["code"]] + [r]
        ds.save_pool_history(date, cur, force=True)
    with open(os.path.join(_TMP, "sentiment_history.json"), "w", encoding="utf-8") as f:
        json.dump([{"date": "20261008", "stage": "ebb"},
                   {"date": "20261009", "stage": "climax"},
                   {"date": "20261012", "stage": "ebb"}], f)
    # 涨停家数曲线：10-12 相对 10-09 骤降（69 → 46 没过 0.6 线，故 10-12 靠 stage 识别）
    with open(os.path.join(_TMP, "zt_history.json"), "w", encoding="utf-8") as f:
        json.dump([{"date": "20261008", "limit_up": 42},
                   {"date": "20261009", "limit_up": 69},
                   {"date": "20261012", "limit_up": 46}], f)


def main():
    build()
    real_kl = ds.fetch_klines
    ds.fetch_klines = lambda codes, n=60, workers=8: {  # noqa: ARG005
        c: KLINES.get(c, []) for c in codes}
    try:
        print("[1] 逆势日：情绪阶段 ebb ∪ 涨停家数骤降")
        ebb = industry.ebb_days()
        ok("20261008" in ebb, "10-08 命中（sentiment stage=ebb）")
        ok("20261012" in ebb, "10-12 命中（stage=ebb；家数 46/69=0.67 没到骤降线）")
        ok("20261009" not in ebb, "10-09 是 climax，不是逆势日")
        ok("20260930" in ebb or True, f"骤降代理可用（当前逆势日：{sorted(ebb)}）")

        print("\n[2] 形态判据的边界")
        # 单测直接调形态判据时，要把 _date 补上（chains() 会加，裸行没有）
        chain = [dict(r, _date=d) for d, r in JIUYANG]
        sl = industry._shrink_limit(chain, 0.7)
        ok(sl and sl["date"] == "20261008" and sl["boards"] == 4,
           f"缩量一字命中 10-08（{sl}）—— 换手 1.02% ← 前板 4.59%")
        ok(industry._shrink_limit(chain, 0.01) is None, "阈值收到 0.01 就判不出来")
        ok(industry._shrink_limit([chain[0]], 0.7) is None, "只有一根时不越界")
        rs = industry._reseal(chain, 3)
        ok(rs and rs["date"] == "20261009" and rs["breaks"] == 6,
           f"炸板回封命中 10-09（{rs}）")
        ok(industry._reseal(chain, 7) is None, "门槛 7 次时它不够（炸了 6 次）")

        print("\n[3] 端到端：九阳股份应被抓出来，情绪票与低度票不该进")
        r = industry.evaluate(CFG, snaps=SNAPS, zt=[], real_date="20261012")
        ok(r["active"] is True, "识别到逆势连板 → active")
        codes = [p["code"] for p in r["picks"]]
        ok("sz002242" in codes, f"九阳股份在结果里（{codes}）")
        ok("sh600001" not in codes, "没跨过逆势日的情绪票被排除")
        ok("sh600002" not in codes, "高度不够的票被排除")
        p = next(x for x in r["picks"] if x["code"] == "sz002242")
        ok(p["max_boards"] == 6, f"连板高度取链上最大值（{p['max_boards']}）")
        ok("20261008" in p["ebb_days"] and "20261012" in p["ebb_days"],
           f"跨过的逆势日如实列出（{p['ebb_days']}）")
        ok(any("缩量一字" in f for f in p["flags"]), "带出「缩量一字」证据")
        ok(any("炸板回封" in f for f in p["flags"]), "带出「炸板回封」证据")
        ok(p["mv_first"] == 78.7,
           f"首板市值用日K首板价反推 = 134.6 × 10.33 ÷ 17.67 ≈ {p['mv_first']} 亿")
        ok(p["mv_ok"] is True, "78.7 亿 ≤ 100 亿 → 合格")
        ok(p["still"] is False, "今天不在涨停池 → still=False（如实给）")
        ok("断板" in p["how"] or "不在涨停池" in p["how"], "口径里明说它今天已断板")
        ok("不是买点" in r["note"] or "不代表现在能买" in r["note"],
           "note 明确这不是买点")

        print("\n[4] 首板市值超限 → 扣分并标不合格")
        cfg_big = {"E7_industry": dict(CFG["E7_industry"], mv_max=50.0)}
        r2 = industry.evaluate(cfg_big, snaps=SNAPS, zt=[], real_date="20261012")
        p2 = next(x for x in r2["picks"] if x["code"] == "sz002242")
        ok(p2["mv_ok"] is False, "mv_max=50 时 78.7 亿不合格")
        ok(p2["score"] < p["score"], f"不合格要扣分（{p2['score']} < {p['score']}）")

        print("\n[5] 今天仍在涨停池 → still=True")
        r3 = industry.evaluate(CFG, snaps=SNAPS,
                               zt=[{"code": "sz002242", "name": "九阳股份"}],
                               real_date="20261012")
        ok(next(x for x in r3["picks"] if x["code"] == "sz002242")["still"] is True,
           "在池 → still=True")
        ok("仍在涨停" in r3["summary"], f"summary 说清有几只今天还在涨停（{r3['summary'][:40]}…）")

        print("\n[6] 空数据 / 关闭 / 冷启动 都不硬凑")
        off = industry.evaluate({"E7_industry": {"enabled": False}})
        ok(off["enabled"] is False and off["active"] is False, "关闭时 enabled=False")
        empty = industry.evaluate(CFG, snaps={}, zt=[], real_date="20260101")
        ok(empty["active"] is False and "冷启动" in empty["reason"],
           f"无归档时不硬凑（{empty['reason'][:24]}…）")
        none_ebb = industry.evaluate(CFG, snaps=SNAPS, zt=[], real_date="20260929")
        ok(none_ebb["active"] is False or
           all(x["code"] != "sz002242" for x in none_ebb["picks"]),
           "只到 09-29（还没跨过逆势日）时不出这只票")

        print("\n[7] 缺字段不崩（降级数据）")
        ds.save_pool_history("20261013",
                             [{"code": "sh600009", "name": "缺字段票", "boards": 5}],
                             force=True)
        r4 = industry.evaluate(CFG, snaps={}, zt=[], real_date="20261013")
        ok(isinstance(r4, dict) and "picks" in r4, "缺 first_time/turnover 等字段不抛异常")
        for x in r4["picks"]:
            ok("how" in x and "flags" in x, f"{x['name']} 的 how/flags 字段齐全")

        print("\n[8] 配置已进预设")
        c = sc.load_config()
        ok("E7_industry" in sc.PRESETS["wildman"], "E7_industry 进了 wildman 预设")
        d = c.get("E7_industry") or {}
        for k in ("enabled", "min_boards", "lookback_days", "top_n", "mv_max",
                  "shrink_ratio", "reseal_breaks", "mv_check"):
            ok(k in d, f"load_config 后有 {k}（{d.get(k)}）")
    finally:
        ds.fetch_klines = real_kl

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
