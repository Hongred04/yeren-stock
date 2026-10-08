# -*- coding: utf-8 -*-
"""端到端自检：跑完整决策链，输出报告。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core import datasource as ds
from core import engine
from core import mainline
from core import strategy_config as sc
from core import trading
from core import universe
from core.version import APP_VERSION


def main(date: str = "", preset: str = "wildman", top: int = 12):
    cfg = sc.load_config()
    if preset != cfg.get("preset"):
        cfg = sc.apply_preset(cfg, preset)

    t0 = time.time()
    date = date or ds.last_trade_date()
    print("=" * 78)
    print(f"  智能股票监控系统 v{APP_VERSION} · 自检报告   数据日期 {date}   策略 {cfg['preset']}")
    print("=" * 78)

    zt = ds.fetch_limit_up_pool(date)
    dt = ds.fetch_limit_down_pool(date)
    zb = ds.fetch_broken_board_pool(date)
    zth = ds.fetch_limit_up_history(10, date)
    idx = ds.fetch_index_snapshots()
    amount = sum(v["amount"] for v in idx.values() if v["name"] in ("上证指数", "深证成指"))
    hist = ds.market_amount_history(20)
    print(f"\n[数据] 涨停{len(zt)} 跌停{len(dt)} 炸板{len(zb)}  两市成交{amount/1e8:.0f}亿  ({round(time.time()-t0,1)}s)")

    mac = engine.evaluate_macro(cfg)
    vol = engine.evaluate_volume(cfg, amount, hist)
    sen = engine.evaluate_sentiment(cfg, zt, dt, zb, zth)

    print(f"\n[指数]")
    for c in ("sh000001", "sz399001", "sz399006"):
        if c in idx:
            v = idx[c]
            print(f"  {v['name']:<8}{v['price']:>10.2f}  {v['pct']:>6.2f}%")

    print(f"\n[M1 宏观周期] {mac['label']}   风险={mac['risk']}")
    print(f"  {mac['note']}  关注: {'/'.join(mac['focus']) or '—'}")

    print(f"\n[M2 量能模式] {vol['label']}  ({vol['amount']:.0f}亿)")
    print(f"  {vol['advice']}")

    print(f"\n[M3 情绪周期] {sen['label']}   可出手={sen['can_trade']}")
    m = sen["metrics"]
    print(f"  涨停{m['limit_up']} 跌停{m['limit_down']} 炸板{m['broken']} 最高{m['max_boards']}板 "
          f"封板率{m['seal_rate']*100:.0f}% 亏钱面积{m['hurt_area']*100:.0f}%")
    if sen["signals"]:
        for s in sen["signals"]:
            print(f"  ⚠ {s}")
    if sen.get("trend"):
        print(f"  ⚠ {sen['trend']}")

    # 全市场扫描
    t1 = time.time()
    codes = universe.enumerate_all()
    snaps = ds.fetch_snapshots(codes, batch=60, workers=16)
    print(f"\n[扫描] 全市场 {len(snaps)} 只  ({round(time.time()-t1,1)}s)")

    # M9 题材主线五信号（消息定级→合力→中军→老龙→次日验证）
    ml = mainline.evaluate(cfg, zt=zt, snaps=snaps, real_date=ds.pool_qdate() or date)
    print(f"\n[M9 题材主线五信号] {ml['label']}   主线题材: {ml.get('theme') or '—'}")
    for s in ml.get("signals", []):
        mark = {"pass": "✓", "weak": "△", "pending": "…", "unknown": "?", "fail": "✕"}.get(
            s.get("state"), " ")
        print(f"  {mark} {s['id']} {s['name']}：{s['evidence']}")
        for d in s.get("detail", []):
            print(f"      · {d}")
    print(f"  判定：{ml.get('verdict_note', '')}")

    # 粗筛：快照层过滤（零额外请求）
    a = cfg["M4_aesthetic"]
    ex = set(a.get("exclude_boards", []))
    pmin, pmax = a.get("price_min", 5.0), a.get("price_max", 10.0)
    mv_max, mv_mid = a.get("mv_max", 100.0), a.get("mv_mid_threshold", 500.0)
    zt_codes = {r["code"] for r in zt}
    zt_map = {r["code"]: r for r in zt}

    pool = []
    for code, s in snaps.items():
        if a.get("exclude_st") and ("ST" in s["name"].upper() or "退" in s["name"]):
            continue
        if universe.board_of(code) in ex:
            continue
        to = s.get("turnover", 0)
        if to < a.get("min_turnover", 1.0) or to > a.get("max_turnover", 25.0):
            continue
        if s.get("amount", 0) <= 0:
            continue
        mv = s.get("float_mv", 0) / 1e8
        if mv > mv_mid:
            continue
        # 快照层预评分：价格市值契合度 + 涨停池加权 + 换手活跃度
        pre = 0.0
        if pmin <= s["price"] <= pmax:
            pre += 40
        elif pmin * 0.7 <= s["price"] <= pmax * 1.4:
            pre += 24
        if mv <= mv_max:
            pre += 40
        elif mv <= mv_max * 2:
            pre += 20
        if code in zt_codes:
            pre += 25 + min(zt_map[code].get("boards", 1), 7) * 4
        if to >= 3:
            pre += 10
        pool.append((pre, code))

    pool.sort(reverse=True)
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 320
    pre_codes = [c for _, c in pool[:limit]]
    print(f"[粗筛] 候选 {len(pool)} 只 → 取前 {len(pre_codes)} 拉K线")

    t2 = time.time()
    kl = ds.fetch_klines(pre_codes, n=70, workers=12)
    print(f"[K线] 成功 {len(kl)} 只  ({round(time.time()-t2,1)}s)")

    picks = engine.screen(cfg, {c: snaps[c] for c in kl}, kl, zt, mac, vol, sen, top_n=top)
    plan = trading.position_plan(cfg, picks, sen, vol, mac)

    print(f"\n{'='*78}")
    print(f"  [M4 选股结果] 审美三要素 TOP{len(picks)}   总耗时 {round(time.time()-t0,1)}s")
    print("=" * 78)
    if not picks:
        print("  当前条件下无符合审美标的 —— 空仓也是一种仓位")
    else:
        print(f"  {'代码':<9}{'名称':<9}{'评分':>6}{'图形':>6}{'价市值':>7}{'想象':>6}"
              f"{'股价':>8}{'涨幅':>7}{'换手':>7}{'流通亿':>8}{'连板':>5}")
        print("  " + "-" * 88)
        for p in picks:
            sc_ = p["scores"]
            print(f"  {p['code']:<9}{p['name']:<9}{p['score']:>6.1f}{sc_['graph']:>6.0f}"
                  f"{sc_['price_mv']:>7.0f}{sc_['imagination']:>6.0f}"
                  f"{p['price']:>8.2f}{p['pct']:>7.2f}{p['turnover']:>7.2f}"
                  f"{p['float_mv']:>8.1f}{p['zt_boards']:>5}")

    print(f"\n[TOP5 详解]")
    for p in picks[:5]:
        print(f"\n  ▌{p['code']} {p['name']}  评分 {p['score']}  [{p['board']}]"
              f"{'  ★涨停池 '+p['sector'] if p['in_zt'] else ''}")
        g = p["graph_detail"]
        print(f"    图形: 区间位置{g['base_pos']*100:.0f}% 量能压缩至{g['shrink']*100:.0f}% "
              f"均线多头={g['ma_aligned']} 洗盘{g['washout']:.1f} "
              f"近{d['recent_limitups'] if False else p['graph_detail']['recent_limitups']}涨停 史高{g['history_max_boards']}板")
        fs = p.get("fund_style") or {}
        if fs.get("type") and fs["type"] != "unknown":
            print(f"    主导资金: {fs['label']}  {fs.get('speed','')}")
            print(f"      离场口径: {fs.get('hold_note','')}")
        for r in p["reasons"][:6]:
            print(f"    + {r}")
        for w in p["warnings"]:
            print(f"    ! {w}")
        bs = trading.buy_signals(p, kl.get(p["code"], []), sen, vol, cfg)
        ss = trading.sell_signals(p, kl.get(p["code"], []), sen, vol, cfg)
        for b in bs[:3]:
            print(f"    【买 {b['priority']}】{b['name']}：{b['trigger']}")
            print(f"        进场 {b['entry']}")
            print(f"        止损 {b['stop']}  |  仓位 {b['position']}")
        for s in ss[:2]:
            print(f"    【卖 {s['priority']}】{s['name']}：{s['trigger']} → {s['action']}")

    print(f"\n[M5 仓位链] 目标总仓位 {plan.get('target_total',0)*100:.0f}%  ({plan.get('chain_stage','')})")
    for c in plan.get("chain", []):
        print(f"  {c['stage']:<14}{c['action']:<40} 单票{c['weight']*100:>4.0f}%  {c['note']}")

    print(f"\n[M8 纪律清单]")
    for r in cfg.get("M8_discipline", {}).get("rules", []):
        if r.get("on"):
            print(f"  ✓ {r['text']}")

    print(f"\n{'='*78}\n  提示：本系统为量化工具输出，不构成投资建议。保住本金是第一原则。")
    print("=" * 78)


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else ""
    p = sys.argv[2] if len(sys.argv) > 2 else "wildman"
    main(d, p)
