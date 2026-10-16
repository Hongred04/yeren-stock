# -*- coding: utf-8 -*-
"""信号复盘档案测试 —— 胜率闭环 + 回测正确性清单。

要点：
  · 只记可执行的买入信号（HOLD/涨停不追不记）；
  · t+1 开盘进场 + 成本扣除；
  · ★ 持有 h 日 = 卖在 **T+1+h 的收盘**（进场日也算持有第 1 天）；
  · 次日一字板开盘 → 「未成交」不计入胜率；
  · 次日无K线（停牌）超期 → 「未成交」；
  · 同日同股同信号去重。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="journal_")

from core import journal                # noqa: E402
from core import strategy_config as sc  # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def bar(d, o, c, h, l):
    return {"date": d, "open": o, "close": c, "high": h, "low": l, "volume": 100000}


def main():
    cfg = sc.default_config()
    picks = [
        {"code": "sh600001", "name": "测试A", "sector": "电池", "score": 80,
         "price": 10.0, "pct": 10.0,
         "buy_signals": [{"type": "B1", "name": "首板试错", "blocked": False}]},
        {"code": "sz000002", "name": "测试B", "sector": "电池", "score": 75,
         "price": 20.0, "pct": 10.0,
         "buy_signals": [{"type": "B1", "name": "首板试错", "blocked": False}]},
        {"code": "sz000003", "name": "测试C", "sector": "机器人", "score": 70,
         "price": 8.0, "pct": 5.0,
         "buy_signals": [{"type": "HOLD", "name": "涨停不追", "blocked": True}]},
        {"code": "sz000004", "name": "测试D", "sector": "机器人", "score": 66,
         "price": 8.0, "pct": 2.0,
         "buy_signals": [{"type": "B2", "name": "龙头回踩", "blocked": False},
                         {"type": "B3", "name": "缩量红盘", "blocked": False}]},
    ]
    kl = {
        "sh600001": [
            bar("2026-10-09", 9.2, 10.0, 10.0, 9.1),      # 信号日
            bar("2026-10-12", 10.0, 10.3, 10.4, 9.9),      # T+1 进场（开盘 10.0）
            bar("2026-10-13", 10.3, 10.2, 10.5, 10.1),     # 持有1日
            bar("2026-10-14", 10.2, 10.4, 10.6, 10.1),
            bar("2026-10-15", 10.4, 10.5, 10.7, 10.3),     # 持有3日
            bar("2026-10-16", 10.5, 10.9, 11.0, 10.4),
            bar("2026-10-19", 10.9, 11.2, 11.3, 10.8),     # 持有5日
        ],
        "sz000002": [
            bar("2026-10-09", 18.5, 20.0, 20.0, 18.2),
            bar("2026-10-12", 21.96, 21.96, 21.96, 21.96),   # 一字板开盘
            bar("2026-10-13", 22.0, 23.0, 23.0, 21.9),
        ],
        "sz000004": [
            bar("2026-10-09", 9.0, 9.2, 9.3, 8.9),           # 信号日之后无K线 → 停牌判定
        ],
    }

    print("[1] 记录：只记可执行信号，同信号取第一个")
    r1 = journal.run(cfg, "20261009", picks, kl)
    entries = journal._load()
    ok(r1["n_new"] == 3, f"记 3 条（HOLD 不记；B2 优先于 B3）（实际 {r1['n_new']}）")
    ok(any(e["signal"] == "B2" for e in entries) and
       not any(e["signal"] == "B3" for e in entries), "B2 被记录、B3 未重复记录")

    print("\n[2] 结算：t+1 开盘 + 成本，一字板与停牌记未成交")
    r2 = journal.run(cfg, "20261020", [], kl)
    em = {e["code"]: e for e in journal._load()}
    a = em["sh600001"]
    ok(a["status"] == "settled", f"A 已结算（{a['status']}）")
    # ★ 持有期口径（v1.41.4 修正）：进场 = T+1 开盘 10.0（10-12），
    #   「持有 h 日」= 卖在 T+1+h 的收盘 —— 进场日计为持有第 1 天。
    #   1日 → 10-13 收盘 10.2 → 10.2/10.0-1-0.002 = +1.8%
    #   5日 → 10-19 收盘 11.2 → 11.2/10.0-1-0.002 = +11.8%
    #   旧代码是 j=idx+h：1日卖在 10-12 收盘 10.3，**与进场同日**，
    #   算出来的是「进场当日日内涨跌」却标着「1日」—— 已被钉死。
    ok(abs(a["ret1"] - (10.2 / 10.0 - 1 - 0.002)) < 1e-6,
       f"A ret1 含成本、且卖在 T+2 收盘（实际 {a['ret1']}）")
    ok(abs(a["ret5"] - (11.2 / 10.0 - 1 - 0.002)) < 1e-6,
       f"A ret5 含成本、且卖在 T+6 收盘（实际 {a['ret5']}）")
    # 同一份数据不许被算成「进场当日的日内涨跌」——旧 bug 的值是 0.028
    ok(a["ret1"] is not None and abs(a["ret1"] - 0.028) > 1e-9,
       "ret1 不得等于 10.3/10.0-1-0.002（那是进场当日日内，不是持有1日）")
    b = em["sz000002"]
    ok(b["status"] == "skip" and "涨停" in b["skip_reason"],
       f"B 一字板记未成交（{b.get('skip_reason')}）")
    ok(b["ret1"] is None, "未成交不进收益统计")
    d = em["sz000004"]
    ok(d["status"] == "skip" and "停牌" in d["skip_reason"],
       f"D 无K线超期记未成交（{d.get('skip_reason')}）")

    print("\n[3] 统计与去重")
    row = next(s for s in r2["stats"] if s["type"] == "B1")
    ok(row["n"] == 2 and row["n1"] == 1, f"B1 样本 2、可结算 1（实际 n={row['n']} n1={row['n1']}）")
    ok(row["win1"] == 1.0, f"B1 1日胜率 100%（只算能成交的）（实际 {row['win1']}）")
    ok(r2["overall"]["n_skip"] == 2, f"总体未成交 2（实际 {r2['overall']['n_skip']}）")
    r3 = journal.run(cfg, "20261009", picks, kl)
    ok(r3["n_new"] == 0, f"同批重跑不重复记录（实际 {r3['n_new']}）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
