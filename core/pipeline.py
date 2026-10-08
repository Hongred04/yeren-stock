# -*- coding: utf-8 -*-
"""分析管线：编排数据 → 策略判定 → 选股 → 买卖点，输出统一 JSON。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import datasource as ds
from . import engine
from . import lhb as lhb_mod
from . import mainline
from . import moneyflow as flow_mod
from . import portfolio as portfolio_mod
from . import sentiment_meter
from . import strategy_config as sc
from . import trading
from . import universe
from .version import APP_VERSION

_lock = threading.Lock()          # 仅保护结果缓存的读写
_compute_lock = threading.Lock()  # 防止重复计算（后台任务串行）
_result_cache: dict = {}
_result_ts: float = 0
RESULT_TTL = 180  # 3分钟
_progress_cb = None  # 全局进度回调


def set_progress(cb):
    """注册进度回调 cb(stage, current, total)，用于前端实时反馈。"""
    global _progress_cb
    _progress_cb = cb


def _prog(stage, cur=0, total=0):
    if _progress_cb:
        try:
            _progress_cb(stage, cur, total)
        except Exception:
            pass


def has_cached() -> bool:
    """是否有可用结果缓存（不触发任何计算）。"""
    with _lock:
        return bool(_result_cache) and (time.time() - _result_ts) < RESULT_TTL


def get_cached() -> dict | None:
    """直接取当前结果缓存，不触发计算。"""
    with _lock:
        if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
            return _result_cache
    return None


def invalidate():
    """使结果缓存失效。"""
    global _result_ts
    _result_ts = 0


def _snapshot_prefilter(snaps: dict, cfg: dict, zt: list[dict], limit: int,
                        preopen: bool = False) -> list[str]:
    """快照层预筛 + 排序，返回优先拉K线的候选。

    preopen：盘前/集合竞价/休市时全市场换手与成交额都是 0（当日数据
    未形成），流动性门槛会让候选清空 —— 放开这两道门槛，名单按
    昨收状态作观察名单。
    """
    a = cfg.get("M4_aesthetic", {})
    ex = set(a.get("exclude_boards", []))
    pmin, pmax = a.get("price_min", 5.0), a.get("price_max", 10.0)
    mv_max, mv_mid = a.get("mv_max", 100.0), a.get("mv_mid_threshold", 500.0)
    zt_map = {r["code"]: r for r in zt}

    pool = []
    for code, s in snaps.items():
        if a.get("exclude_st") and ("ST" in s["name"].upper() or "退" in s["name"]):
            continue
        if universe.board_of(code) in ex:
            continue
        to = s.get("turnover", 0)
        if not preopen and (to < a.get("min_turnover", 1.0) or to > a.get("max_turnover", 25.0)):
            continue
        if not preopen and s.get("amount", 0) <= 0:
            continue
        mv = s.get("float_mv", 0) / 1e8
        if mv > mv_mid:
            continue
        pre = 0.0
        if pmin <= s["price"] <= pmax:
            pre += 40
        elif pmin * 0.7 <= s["price"] <= pmax * 1.4:
            pre += 24
        if mv <= mv_max:
            pre += 40
        elif mv <= mv_max * 2:
            pre += 20
        if code in zt_map:
            pre += 25 + min(zt_map[code].get("boards", 1), 7) * 4
        if to >= 3:
            pre += 10
        pool.append((pre, code))
    pool.sort(reverse=True)
    return [c for _, c in pool[:limit]]


def analyze(cfg: dict | None = None, date: str = "", kline_limit: int = 260,
            top_n: int = 20, use_cache: bool = True) -> dict:
    """完整分析。返回可直接 JSON 序列化的报告。

    并发约定：结果缓存的读写用 _lock（极短临界区），实际计算不持锁，
    避免长任务把 /api/status 等轻量接口一起卡死。
    """
    global _result_cache, _result_ts

    # 1) 缓存检查（持锁，极短）
    if use_cache:
        with _lock:
            if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
                return _result_cache

    # 2) 实际计算（不持缓存锁，用独立计算锁防止重复劳动）
    with _compute_lock:
        # 双检：等锁期间可能已有其他线程算完
        if use_cache:
            with _lock:
                if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
                    return _result_cache

        cfg = cfg or sc.load_config()
        t0 = time.time()
        date = date or ds.last_trade_date()

        # ---- 数据层 ----
        _prog("拉取情绪池数据…", 0, 100)
        zt = ds.fetch_limit_up_pool(date)
        dt = ds.fetch_limit_down_pool(date)
        zb = ds.fetch_broken_board_pool(date)
        # 池接口忽略 date 入参，以接口自报的 qdate 为准
        real_date = ds.pool_qdate() or date
        zth = ds.fetch_limit_up_history(10, real_date)
        idx = ds.fetch_index_snapshots()
        amount = sum(v["amount"] for v in idx.values()
                     if v["name"] in ("上证指数", "深证成指"))
        hist = ds.market_amount_history(20)

        # ---- 决策链 ----
        _prog("评估宏观/量能/情绪…", 0, 100)
        sess = trading.market_session()
        preopen = sess["phase"] in ("pre_open", "auction", "rest")
        macro = engine.evaluate_macro(cfg)
        amt_yi = amount / 1e8 if amount > 1e5 else amount
        volume = engine.evaluate_volume(cfg, amount, hist, session=sess,
                                        fallback_amount=ds.load_last_complete_amount(real_date))
        if sess["phase"] in ("just_closed", "after_close", "rest") and amt_yi > 0:
            ds.save_market_amount(real_date, amt_yi)
        sentiment = engine.evaluate_sentiment(cfg, zt, dt, zb, zth)

        # ---- 选股 ----
        codes = ds.load_valid_codes()
        from_enum = not codes
        if from_enum:
            codes = universe.enumerate_all()
        _prog("扫描全市场快照…", 0, 100)
        snap_batch = 80
        snaps = ds.fetch_snapshots(codes, batch=snap_batch, workers=24,
                                   progress=lambda d, t: _prog(
                                       f"扫描全市场 {d*snap_batch}/{t*snap_batch} 只…", d, t))
        if from_enum and snaps:
            # 当日首扫：把真实标的名单落盘，同日重扫少发 ~8 倍请求
            ds.save_valid_codes(sorted(snaps))
        _prog(f"粗筛候选（已扫 {len(snaps)} 只）…", 0, 100)
        pre = _snapshot_prefilter(snaps, cfg, zt, kline_limit, preopen=preopen)
        _prog(f"拉取 {len(pre)} 只K线…", 0, len(pre))
        kl = ds.fetch_klines(pre, n=70, workers=24,
                             progress=lambda d, t: _prog(
                                 f"拉取K线 {d}/{t}…", d, t))
        _prog("打分选股…", 0, 100)
        picks = engine.screen(cfg, {c: snaps[c] for c in kl if c in snaps}, kl,
                              zt, macro, volume, sentiment, top_n=top_n,
                              preopen=preopen)

        # ---- 买卖点 ----
        for p in picks:
            bars = kl.get(p["code"], [])
            p["buy_signals"] = trading.buy_signals(p, bars, sentiment, volume, cfg)
            p["sell_signals"] = trading.sell_signals(p, bars, sentiment, volume, cfg)
            p["kline_tail"] = bars[-30:]

        plan = trading.position_plan(cfg, picks, sentiment, volume, macro)

        # ---- M9 题材主线五信号（消息定级→合力→中军→老龙→次日验证） ----
        _prog("评估题材主线五信号…", 0, 100)
        ml = mainline.evaluate(cfg, zt=zt, snaps=snaps, real_date=real_date)

        # ---- 业绩披露期 ----
        m7 = engine.evaluate_earnings(cfg)
        m6 = engine.evaluate_message(cfg, zt)

        # ---- E1 情绪温度计（连板梯队/炸板率/晋级率/涨跌家数） ----
        _prog("构建情绪温度计…", 0, 100)
        meter = sentiment_meter.build(zt, dt, zb, snaps, real_date, cfg)

        # ---- E2 龙虎榜·游资席位 ----
        _prog("拉取龙虎榜…", 0, 100)
        lhb_data = lhb_mod.evaluate(cfg, real_date, zt)
        seat_hits = lhb_data.get("seat_hits", {})
        if seat_hits:
            for p in picks:
                tags = seat_hits.get(p["code"])
                if tags:
                    p["lhb_tags"] = tags

        # ---- E3 主力资金流（板块 + 候选股） ----
        _prog("拉取主力资金流…", 0, 100)
        flow = flow_mod.evaluate(cfg, picks, snaps, ml.get("theme", ""), zt)

        # ---- 持仓体检（本地持仓/自选） ----
        _prog("持仓体检…", 0, 100)
        port_raw = portfolio_mod.load()
        hold_codes = [h["code"] for h in port_raw.get("holdings", [])]
        kl_hold = ds.fetch_klines(hold_codes, n=70, workers=8) if hold_codes else {}
        port = portfolio_mod.evaluate(snaps, kl_hold, sentiment, volume, cfg)
        port["raw"] = port_raw   # 原始记录（代码/成本/股数），前端按钮状态与面板要用

        # ---- M8 纪律体检：并入 E1-E3 与持仓的告警 ----
        m8 = engine.evaluate_discipline(cfg, sentiment, volume, macro, m7, ml)
        extra = list(lhb_data.get("checks", [])) + list(flow.get("checks", [])) \
            + list(port.get("checks", []))
        if extra:
            m8["checks"] = m8["checks"] + extra
            worst = m8.get("status", "green")
            for c in extra:
                if c.get("level") == "red":
                    worst = "red"
                    break
                if c.get("level") == "yellow" and worst != "red":
                    worst = "yellow"
            m8["status"] = worst

        report = {
            "meta": {
                "version": APP_VERSION,
                "date": real_date,
                "requested_date": date,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "session": sess,
                "preset": cfg.get("preset"),
                "preset_name": cfg.get("name", ""),
                "elapsed": round(time.time() - t0, 1),
                "scanned": len(snaps),
                "scan_list": "全量枚举" if from_enum else "当日有效名单",
                "kline_ok": len(kl),
                "candidate": len(pre),
            },
            "index": idx,
            "macro": macro,
            "macro_events": engine.MACRO_EVENTS,
            "volume": volume,
            "volume_history": [{"date": b["date"], "volume": b["volume"]} for b in hist[-20:]],
            "sentiment": sentiment,
            "meter": meter,
            "mainline": ml,
            "lhb": lhb_data,
            "flow": flow,
            "portfolio": port,
            "picks": picks,
            "position": plan,
            "earnings": m7,
            "message": m6,
            "discipline": m8,
            "limit_up_pool": sorted(zt, key=lambda x: -x.get("boards", 1))[:30],
            "config": cfg,
        }
        # 写缓存（持锁，极短）
        with _lock:
            _result_cache = report
            _result_ts = time.time()
        return report
