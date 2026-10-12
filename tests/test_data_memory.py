# -*- coding: utf-8 -*-
"""数据健壮性测试 —— 「数据偶尔丢失」的回归护栏。

用户实测：「实时盘中成交额偶尔丢失」，并要求自查其他数据会不会偶尔丢。
根因是同一类问题：**关键抓取一次抖动就返回空，而空值被当成"今天真的没有"**。
最危险的是跌停池/炸板池为空 —— 亏钱面积会算成 0，系统在真退潮日得出"可以出手"。

本测试钉死四件事：
  ① `_fetch_memo` 失败时回退**当日最后一份好数据**，且绝不用空值覆盖缓存；
  ② 接口"正常回答但就是空"（合法空值）与"接口失败"必须区分开 ——
     东财池接口 `data=null` 是失败，不是"今天没有跌停"；
  ③ 两市成交额取**当日高水位**（累计额只会增不会减，能挡住"只回上证没回深证"的腰斩）；
  ④ 盘中预估全天额用**近 N 日实测占比**校准，样本不足才退回经验曲线。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="datamem_")

from core import datasource as ds   # noqa: E402
from core import engine            # noqa: E402
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


def clean():
    """每个用例都从零开始：内存 + **落盘记忆**都要清 ——
    只清内存的话，上一个用例写下的"当日好数据"会被下一个用例的回退命中。"""
    ds._cache.clear()
    ds._cache_ts.clear()
    ds._mem.clear()
    ds.reset_health()
    for p in (ds.MEM_PATH, ds.CURVE_PATH, ds.MARKET_AMOUNT_PATH):
        try:
            os.remove(p)
        except OSError:
            pass


def main():
    real_get = ds._get

    print("[1] 抓取成功 → 写缓存 + 写记忆 + health 正常")
    clean()
    v = ds._fetch_memo("pool:DT", "20261012", 0, lambda: (True, [{"code": "sh600000"}]),
                       empty=[])
    ok(v == [{"code": "sh600000"}], "返回抓到的值")
    ok(ds.data_health()["ok"] is True, "health 标记正常")

    print("\n[2] 抓取失败 → 回退当日最后一份好数据，且不把空值写进缓存")
    clean()
    ds._fetch_memo("pool:DT", "20261012", 0, lambda: (True, [{"code": "sh600000"}]), empty=[])
    v2 = ds._fetch_memo("pool:DT", "20261012", 0, lambda: (False, None), empty=[])
    ok(v2 == [{"code": "sh600000"}], "失败时回退到当日好数据（没变成空）")
    h = ds.data_health()
    ok(h["ok"] is False, "health 如实标记降级")
    ok("跌停池" in h["summary"], f"summary 指名道姓：{h['summary']}")
    ok(h["degraded"][0].get("stale_seconds") is not None, "带「多久之前」的快照时长")
    # 关键：缓存里存的必须是好数据 —— 否则下一次 ttl 内又拿到空
    ok(ds._cache.get("pool:DT") == [{"code": "sh600000"}],
       "缓存里是好数据（不是空值）")

    print("\n[3] 失败且当日没有历史快照 → 返回空，但 health 说明原因")
    clean()
    v3 = ds._fetch_memo("pool:ZB", "20261012", 0, lambda: (False, None), empty=[])
    ok(v3 == [], "返回空")
    ok(ds.data_health()["ok"] is False, "health 标记降级")
    ok("还没有可用快照" in (ds.data_health()["degraded"][0]["note"]), "说明是首次且失败")

    print("\n[4] 池子接口：data=null 是失败，不是「今天没有跌停」")
    clean()
    cases = [
        (json.dumps({"data": None}), False, "data=null（日期非法/被限速）"),
        (json.dumps({"data": {"qdate": 20261012, "pool": []}}), True, "合法空池（今天真没有）"),
        (json.dumps({"data": {"qdate": 20261012, "pool": [
            {"c": "600000", "n": "浦发银行", "m": 1, "p": 10000, "zdp": -10.0,
             "lbc": 1, "hybk": "银行"}]}}), True, "有数据的池"),
        ("not json at all", False, "非 JSON 响应"),
    ]
    for body, expect_ok, label in cases:
        clean()
        ds._get = lambda *a, **k: body.encode("utf-8")
        ds._cache.clear()
        got = ds.fetch_limit_down_pool("20261012")
        h = ds.data_health()
        ok(h["ok"] is expect_ok, f"{label} → health {'正常' if expect_ok else '降级'}")
        if not expect_ok:
            ok(got == [], f"{label} → 不把失败当数据（返回空并标记降级）")
    ds._get = real_get

    print("\n[5] 两市成交额取当日高水位（挡住「只回上证没回深证」的腰斩）")
    clean()
    a = ds.market_amount_hwm("20261012", 19000.0)
    ok(a == 19000.0, "首次记录")
    b = ds.market_amount_hwm("20261012", 9500.0)     # 部分响应：腰斩
    ok(b == 19000.0, f"更小的值不会把高水位拉低（{b}）")
    c = ds.market_amount_hwm("20261012", 19500.0)
    ok(c == 19500.0, "更大的值正常抬升")
    ok(ds.market_amount_hwm("20261013", 100.0) == 100.0, "换日重新计（不串日期）")

    print("\n[6] 日内量能曲线：近 N 日实测占比（样本不足退回经验曲线）")
    clean()
    # 造 3 天样本：全天 1000 亿，截至 60 分钟分别成交 280 / 300 / 320 亿
    for date, cum in (("20261009", 280.0), ("20261010", 300.0), ("20261013", 320.0)):
        ds.save_market_amount(date, 1000.0, final=True)
        ds.save_intraday_amount(date, 60, cum)
    fr = ds.intraday_fraction(60)
    ok(fr is not None and abs(fr - 0.30) < 0.01, f"60 分钟实测占比 ≈ 0.30（得到 {fr}）")
    ok(ds.intraday_fraction(60, min_days=5) is None, "样本不足 min_days → 返回 None（退回经验曲线）")

    print("\n[7] evaluate_volume：实测曲线优先 + 上一交易日基准偏低要说明")
    cfg = sc.load_config()
    sess = {"phase": "morning", "tmin": 60}
    v_exp = engine.evaluate_volume(cfg, 3000.0, [], session=sess)          # 3000 亿（已成交）
    v_emp = engine.evaluate_volume(cfg, 3000.0, [], session=sess, frac_hint=0.30)
    ok("经验曲线" in v_exp["basis"], f"无实测样本 → 用经验曲线：{v_exp['basis']}")
    ok("近N日实测" in v_emp["basis"], f"有实测样本 → 用实测曲线：{v_emp['basis']}")
    ok(abs(v_emp["amount"] - 10000.0) < 1.0,
       f"3000 亿 ÷ 0.30 = 10000 亿（得到 {v_emp['amount']}）")
    ok(v_emp["amount"] != v_exp["amount"],
       f"两条曲线确实不同（经验 {v_exp['amount']} vs 实测 {v_emp['amount']}）")
    pre = {"phase": "pre_open", "tmin": 0}
    v_f = engine.evaluate_volume(cfg, 0, [], session=pre, fallback_amount=19000.0, fallback_final=False)
    ok("盘中快照" in v_f["basis"], f"只扫到盘中的基准要说明偏低：{v_f['basis']}")
    v_t = engine.evaluate_volume(cfg, 0, [], session=pre, fallback_amount=19000.0, fallback_final=True)
    ok("全天" in v_t["basis"], f"收盘终值才叫全天：{v_t['basis']}")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
