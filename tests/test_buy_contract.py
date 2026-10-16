# -*- coding: utf-8 -*-
"""买卖点契约测试 —— 详情页「买点清晰化」的回归护栏。

背景（用户实测反馈）：股票详情页「买点不清晰、逻辑有错误」。
根因与修复见 core/trading.py 顶部注释，本测试把四条契约钉死：

  1. B1 首板试错的门禁必须与「今日结论」一致：页面对 probe/follow/climax
     都写着"可以出手 / 小仓位试水"，就必须都给首板买点；ebb 不给（退潮
     只允许试错仓）。以前只在 probe/follow 给，climax 期一个买点都没有。
  2. buy_signals 用 kind 区分「可执行买点(buy)」与「禁买提示(block)」，
     且可执行买点永远排在前面 —— 消费者不能把 block 当买点展示。
  3. B1 的「分两笔」口径自洽：高开>5% 时第一笔根本没成交，不能写
     "补另一半"（原文的逻辑错误）。
  4. 当日涨停时首板仍给 B1（次日口径），同时给一条 block 提示，
     两者并存且互不矛盾。

  5. 【10-13 补】候选/买点的**状态自洽**：退潮期只给**真首板**可买（kind=buy
     且写明"不追当日价、按次日口径"）；尚未涨停的票只能给 kind=watch 的
     "等首板"观察提示，**不得冒充可买**；连板 ≥2 一律 block。
     （原实现 `boards <= 1` 把未涨停的票也标成 kind=buy 的「退潮试错仓」，
     而它的文案却写着"不是首板就别碰" —— 候选列表因此整屏是马后炮。）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="buycontract_")

from core import strategy_config as sc   # noqa: E402
from core import trading                # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


CFG = sc.default_config() if hasattr(sc, "default_config") else None
if CFG is None:
    CFG = sc.load_config()

BARS = [{"date": f"2026-09-{i:02d}", "open": 10, "close": 10 + i * 0.01,
         "high": 10.1, "low": 9.9, "volume": 100000} for i in range(1, 40)]

VOL = {"mode": "comfort_short", "advice": "量能舒适", "red_flag": False}


def sent(stage, can_trade=True, trial_only=False, tw=0.05):
    return {"stage": stage, "can_trade": can_trade, "trial_only": trial_only,
            "ebb_trial_weight": tw, "note": "note", "label": stage, "signals": []}


def stock(boards=1, pct=10.0):
    return {"code": "sh600000", "name": "测试", "sector": "电池",
            "price": 10.4, "pct": pct, "zt_boards": boards,
            "turnover": 5.0, "fund_style": {"type": "unknown"}}


def kinds(sigs):
    return [(s.get("type"), s.get("kind"), bool(s.get("blocked"))) for s in sigs]


def main():
    print("[1] 首板：probe / follow / climax 都给 B1（与「可以出手」一致）")
    for st in ("probe", "follow", "climax"):
        sig = trading.buy_signals(stock(1), BARS, sent(st), VOL, CFG)
        first = sig[0] if sig else {}
        ok(first.get("type") == "B1" and first.get("kind") == "buy",
           f"{st} → 首条是可执行的 B1（实际 {kinds(sig)[:1]}）")

    print("\n[2] 首板 + 退潮：只给试错仓 B0，不给 B1")
    s = sent("ebb", can_trade=False, trial_only=True)
    sig = trading.buy_signals(stock(1), BARS, s, VOL, CFG)
    ok(len(sig) == 1 and sig[0].get("type") == "B0", "退潮首板 → 唯一一条 B0 试错仓")
    ok(sig[0].get("kind") == "buy" and sig[0].get("trial") is True, "B0 是可执行买点且标记试错")

    print("\n[3] 连板（2板）+ climax：不给买点，只给 block 提示")
    sig = trading.buy_signals(stock(2), BARS, sent("climax"), VOL, CFG)
    ok(sig and sig[0].get("type") == "HOLD", "2板 → HOLD")
    ok(sig[0].get("kind") == "block" and sig[0].get("blocked") is True, "标记为 block/blocked")

    print("\n[4] 契约：kind ∈ {buy, block}，blocked ⟺ kind=='block'，买点永远在前")
    cases = [(stock(1), sent("climax")), (stock(2), sent("climax")),
             (stock(1), sent("ebb", can_trade=False, trial_only=True))]
    for stk, se in cases:
        sig = trading.buy_signals(stk, BARS, se, VOL, CFG)
        ok(all(s.get("kind") in ("buy", "block") for s in sig),
           f"kind 合法：{kinds(sig)}")
        ok(all((s.get("kind") == "block") == bool(s.get("blocked")) for s in sig),
           "block ⟺ blocked")
        seen_block = False
        bad_order = False
        for s in sig:
            if s.get("kind") == "block":
                seen_block = True
            elif seen_block:
                bad_order = True
        ok(not bad_order, "可执行买点没有被 block 提示挤到后面")

    print("\n[5] B1 进场口径：只做低开，高开一律放弃（v1.41.5 按实测重写）")
    # ★ 这一节原来断言的是「① 高开 0~3% 先买一半；② 高开 3~5% 先观望补另一半」——
    #   那是**方向反了**的旧口径，2026-10-16 用 data/signal_journal.json 的
    #   201 笔实测把它推翻了（按 entry_gap 分档）：
    #     低开<-3%  n=33  胜率42.4% 均+1.18%  ← 唯一正期望，旧文案叫「放弃」
    #     低开-3~0% n=122 胜率28.7% 均-1.46%
    #     高开0~3%  n=36  胜率30.6% 均-1.64%  ← 旧文案叫「先买一半」
    #     高开3~5%  n=7   胜率 0%   均-8.08%  ← 旧文案还叫「回踩补另一半」
    #   信号是 T 日收盘后给的，T+1 开盘是全市场对同一消息的第一反应：
    #   高开=你出价最高、别人在卖；低开=恐慌盘把价打下来、你在接便宜筹码。
    #   所以契约改成：低开才建仓，高开 >3% 一律放弃。
    sig = trading.buy_signals(stock(1), BARS, sent("climax"), VOL, CFG)
    e = sig[0].get("entry", "")
    ok("低开 ≤-3%" in e and "直接买第一笔" in e,
       "第一笔：低开 ≤-3% 直接买（唯一实测正期望的档）")
    ok("高开 0~3%" in e and "不买" in e,
       "高开 0~3% 明确不买（旧口径在这里叫『先买一半』，实为负期望）")
    ok("高开 >3%" in e and "放弃" in e,
       "高开 >3% 一律放弃（3~5% 档实测 7 笔全亏、均 -8.08%）")
    ok("抢筹过头" not in e,
       "不再把高开描述成「抢筹过头」这种模糊措辞（要说清是别人在出货）")

    print("\n[6] 当日涨停：首板 B1 与「涨停不追」并存且不矛盾")
    sig = trading.buy_signals(stock(1, pct=10.04), BARS, sent("climax"), VOL, CFG)
    types = [s.get("type") for s in sig]
    ok(types[0] == "B1" and "HOLD" in types, f"既有 B1 也有 HOLD：{types}")
    hold = next(s for s in sig if s.get("type") == "HOLD")
    ok(any(k in hold.get("entry", "") for k in ("次日", "今日", "下个交易日")),
       "「涨停不追」给出次日/下一场的观察口径")

    print("\n[7] 止损必须落在现价下方（否则会把止损写成止盈）")
    # 结构位在现价之上（破位票的 10 日线）：不能取 max 得到一个高于现价的"止损"
    sp = trading._stop_price(12.0, 10.0)
    ok(sp < 10.0, f"_stop_price(结构位12, 现价10) = {sp:.2f} < 现价")
    ok(trading._stop_price(9.0, 10.0) < 10.0, "正常结构位也在现价下方")
    ok(trading._loss_pct(9.04, 8.75) == "—",
       "止损高于现价 → 百分比显示 '—'（不是 +3.3%）")
    ok(trading._loss_pct(8.14, 8.75) == "-7.0%", "正常止损 → 负数百分比")

    print("\n[8] B2 龙头回踩：现价跌破 10 日线时不算回踩（否则与 S3 破位止损打架）")
    closes = [10.4] * 5 + [8.6, 9.46, 10.4, 9.5, 9.5]
    b2b = [{"date": f"2026-09-{i + 1:02d}", "open": c, "close": c,
            "high": c * 1.005, "low": c * 0.995, "volume": 100000}
           for i, c in enumerate(closes)]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    close = closes[-1]
    ok(close < ma10 and ma5 * 0.95 <= close <= ma5 * 1.03,
       f"构造成立：现价 {close} 在 5 日线带内、但已在 10 日线 {ma10:.2f} 下方")
    sig = trading.buy_signals(stock(0, pct=-1.0), b2b, sent("climax"), VOL, CFG)
    ok(not any(s.get("type") == "B2" for s in sig),
       f"现价在 10 日线下方 → 不出 B2（实际 {[s.get('type') for s in sig]}）")

    test_candidate_state_consistency()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


def test_candidate_state_consistency():
    """[5] 买点与票的**当前状态**必须自洽（退潮期只认首板）。"""
    print("\n[5] 退潮期：可买只给真首板，未涨停只能「等首板」观察")
    ebb = sent("ebb", can_trade=False, trial_only=True)

    s0 = trading.buy_signals(stock(0, pct=1.2), BARS, ebb, VOL, CFG)
    ks0 = kinds(s0)
    ok(not any(k[1] == "buy" for k in ks0),
       f"未涨停(0板) → 不给 kind=buy（原来会给 B0 可买）：{ks0}")
    ok(any(k[1] == "watch" for k in ks0),
       f"未涨停(0板) → 给 kind=watch 的观察提示：{ks0}")

    s1 = trading.buy_signals(stock(1, pct=10.0), BARS, ebb, VOL, CFG)
    ks1 = kinds(s1)
    ok(any(k[1] == "buy" for k in ks1), f"首板(1板) → 给可执行买点：{ks1}")
    ok("不追当日价" in s1[0]["entry"],
       "首板已封板 → 必须写明「不追当日价」（否则等于让用户追明牌）")
    ok("次日" in s1[0]["entry"], "首板已封板 → 明确是次日竞价口径")

    s3 = trading.buy_signals(stock(3, pct=10.0), BARS, ebb, VOL, CFG)
    ks3 = kinds(s3)
    ok(all(k[1] == "block" for k in ks3), f"3 连板 → 只有 block：{ks3}")

    print("\n[5b] screen：候选股不得含连板 ≥2（体系一律不接力）")
    from core import engine
    cfg2 = dict(CFG)
    a = dict((cfg2.get("M4_aesthetic") or {}))
    a.update({"enabled": True, "price_min": 1, "price_max": 100, "mv_max": 100000,
              "min_turnover": 0.1, "max_turnover": 100, "exclude_st": False,
              "weight_graph": 35, "weight_price_mv": 30, "weight_imagination": 35})
    cfg2["M4_aesthetic"] = a
    snaps, kl = {}, {}
    for i, bd in enumerate((0, 1, 2, 3)):
        code = f"sh60000{i}"
        snaps[code] = {"code": code, "name": f"票{i}", "price": 8.0,
                       "pct": 10.0 if bd else 1.0,
                       "turnover": 5.0, "amount": 1e8, "float_mv": 3e9}
        kl[code] = BARS
    zt = [{"code": "sh600001", "boards": 1, "sector": "电池"},
          {"code": "sh600002", "boards": 2, "sector": "电池"},
          {"code": "sh600003", "boards": 3, "sector": "电池"}]
    mkt = {"stage": "probe", "label": "d", "risk": "low", "focus": []}
    sel = engine.screen(cfg2, snaps, kl, zt, mkt, VOL, sent("probe"), top_n=10)
    got = {p["code"]: int(p.get("zt_boards") or 0) for p in sel}
    ok(all(v <= 1 for v in got.values()),
       f"候选里最大连板数 ≤ 1：{got}")
    ok("sh600002" not in got and "sh600003" not in got, "2/3 连板被排除在候选外")
    a["max_candidate_boards"] = 3
    sel2 = engine.screen(cfg2, snaps, kl, zt, mkt, VOL, sent("probe"), top_n=10)
    ok(len(sel2) == 4, f"把上限调到 3 → 连板票可入选（可配置，得到 {len(sel2)} 只）")

    print("\n[5c] 可打的首板加权：同审美下首板排前，质量差的首板不占便宜")
    a["max_candidate_boards"] = 1
    a["first_board_bonus"] = 10
    snaps3, kl3 = {}, {}
    for i in range(4):                      # 4 只票的 K 线完全一样 → 审美分相同
        code = f"sh60010{i}"
        snaps3[code] = {"code": code, "name": f"票{i}", "price": 8.0,
                        "pct": 10.0 if i < 3 else 1.0,
                        "turnover": 5.0, "amount": 1e8, "float_mv": 3e9}
        kl3[code] = BARS
    zt3 = [
        # 0 号：完美首板（早封/未炸/封单厚）
        {"code": "sh600100", "boards": 1, "sector": "电池",
         "first_time": 92500, "break_count": 0, "fund": 3e7},
        # 1 号：质量差的首板（晚封 + 炸过板 + 无封单）
        {"code": "sh600101", "boards": 1, "sector": "电池",
         "first_time": 143000, "break_count": 3, "fund": 0},
    ]
    sel3 = engine.screen(cfg2, snaps3, kl3, zt3, mkt, VOL, sent("probe"), top_n=10)
    by = {p["code"]: p for p in sel3}
    ok(sel3[0]["code"] == "sh600100",
       f"完美首板排第一（实际第一是 {sel3[0].get('name')}）")
    ok(by["sh600100"]["score"] > by["sh600103"]["score"],
       f"首板加权高于同审美的普通票：{by['sh600100']['score']} vs {by['sh600103']['score']}")
    ok(abs(by["sh600100"].get("first_board_bonus", 0) - 10.0) < 0.01,
       f"加分如实暴露 first_board_bonus={by['sh600100'].get('first_board_bonus')}")
    ok(any("首板可打加权" in r for r in (by["sh600100"].get("reasons") or [])),
       "理由里写明为什么加分")
    ok(abs(by["sh600101"].get("first_board_bonus", 0)) < 0.01,
       "质量差的首板不加分（加的是量价配合，不是「只要是首板」）")
    a["first_board_bonus"] = 0
    sel4 = engine.screen(cfg2, snaps3, kl3, zt3, mkt, VOL, sent("probe"), top_n=10)
    ok(all(abs(p.get("first_board_bonus", 0)) < 0.01 for p in sel4),
       "加权设为 0 → 完全关闭（可配置）")


if __name__ == "__main__":
    raise SystemExit(main())
