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
  ⑤ 高水位只收「当日真实累计」—— 盘前/集合竞价的指数快照给的是**上一交易日**
     全天额，绝不能写进今日档案（否则今日一开盘就被昨收全天额锁死）；
     且"上一交易日基准"不得取当日自己的盘中快照。
  ⑥ 节奏对比（"比近期同期快/慢"）必须**钱对钱**：基准只能取档案里实测的同期
     累计额，不许拿"全天额中位数 × 写死的经验曲线"顶替；样本不足就明说在积累。
     同期基准的样本边界：排除当日自身（比值恒≈1 的自证样本）、排除采样时刻与
     查询时刻对不上的日子；阈值判定用原始比值，不被四舍五入挪动。
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
    # 内存键含日期（2026-10-15 起 "pool:DT@20261012"），从这里取。
    ok(ds._cache.get("pool:DT@20261012") == [{"code": "sh600000"}],
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

    print("\n[5b] 盘前读数不得写进「今日高水位」（2026-10-13 实测：昨收被锁死）")
    # 盘前指数快照返回的是**上一交易日**全天额（15378 亿）。旧实现不看时段直接取
    # 高水位 → 今日开盘就被昨收全天额锁死，11:29 实际已成交仅 9987 亿仍显示 15378。
    clean()
    r_pre = ds.market_amount_hwm("20261013", 15378.3, live=False)
    ok(r_pre == 0.0, f"盘前读数 → 返回 0，不入档（得到 {r_pre}）")
    ok(ds._mem_read("mkt_amount", "20261013") == (None, 0), "今日高水位档案保持为空")
    r_am = ds.market_amount_hwm("20261013", 9987.06)
    ok(abs(r_am - 9987.06) < 0.01, f"早盘真实读数正常入库（{r_am}）")
    # 自愈：旧格式（裸数值）无法证明是"今日累计" → 丢弃重算，不被 max 锁死
    ds._mem.clear()
    ds._mem_write("mkt_amount", "20261013", 15378.3)
    r_heal = ds.market_amount_hwm("20261013", 9987.06)
    ok(abs(r_heal - 9987.06) < 0.01,
       f"历史遗留的裸数值污染值被丢弃并重算（得到 {r_heal}，而非 15378.3）")

    print("\n[5c] 上一交易日基准不得取「当日自己的盘中快照」")
    ds.save_market_amount("20261012", 15378.3, final=True)
    ds.save_market_amount("20261013", 9987.06, final=False)   # 今天的盘中快照
    fb = ds.load_last_amount_meta("20261013")
    ok(fb["date"] == "20261012",
       f"当日盘中快照不算「上一交易日」（得到 {fb['date']}）")
    ok(fb["final"] is True, "取到的是完整交易日（final=True）")
    # 但当日已收盘的完整记录必须能被采用（周末 real_date 就落在最近收盘日）
    ds.save_market_amount("20261013", 17000.0, final=True)
    ok(ds.load_last_amount_meta("20261013")["date"] == "20261013",
       "当日已收盘（final=True）→ 可作完整交易日被采用")

    print("\n[6] 日内量能曲线：近 N 日实测占比（样本不足退回经验曲线）")
    clean()
    # 造 3 天样本：全天 1000 亿，截至 60 分钟分别成交 280 / 300 / 320 亿
    for date, cum in (("20261009", 280.0), ("20261010", 300.0), ("20261013", 320.0)):
        ds.save_market_amount(date, 1000.0, final=True)
        ds.save_intraday_amount(date, 60, cum)
    fr = ds.intraday_fraction(60)
    ok(fr is not None and abs(fr - 0.30) < 0.01, f"60 分钟实测占比 ≈ 0.30（得到 {fr}）")
    ok(ds.intraday_fraction(60, min_days=5) is None, "样本不足 min_days → 返回 None（退回经验曲线）")

    print("\n[7] evaluate_volume：盘中**不给全天外推数字**，只报已成交 + 节奏")
    cfg = sc.load_config()
    sess = {"phase": "morning", "tmin": 60}
    v = engine.evaluate_volume(cfg, 3000.0, [], session=sess, frac_hint=0.30)
    ok(v["amount"] is None, "盘中 amount 必须是 None（拿平均曲线外推全天没有把握）")
    ok(v["cum_amount"] == 3000.0, "cum_amount = 已成交 3000 亿（事实）")
    ok(v["intraday"] is True, "标记为盘中")
    ok(v["pace"] is None and "积累中" in v["pace_txt"], "同期基准不够时明说在积累")
    ok("暂判" in v["basis"] and "累计" in v["basis"], f"口径写明是累计口径+暂判：{v['basis']}")
    # ★ 期望值随 v1.26.0 的量能阶梯修正而更新：老配置 comfort=5000 是**倒挂**的
    #   （低于 shrink=12000），会把 10000 亿判成"量能舒适"；修正后阶梯为
    #   峰值 15000 ≥ 舒适 12000 ≥ 缩量 8000，10000 亿正确落在"缩量存量"。
    #   本用例传 hist=[]，相对口径缺样本 → 退回纯绝对阈值判定。
    ok(v["mode"] == "shrink_short",
       f"3000 亿÷30% = 10000 亿 → 缩量存量档（得到 {v['mode']}）")
    v_big = engine.evaluate_volume(cfg, 6000.0, [], session=sess, frac_hint=0.30)
    ok(v_big["mode"] == "peak_swing",
       f"6000 亿÷30% = 20000 亿 → 峰值档（得到 {v_big['mode']}）")
    # 节奏对比：给了近期基准才出现，且是"事实型"表述
    v_p = engine.evaluate_volume(cfg, 3000.0, [], session=sess,
                                 frac_hint=0.30, ref_amount=10000.0)
    ok(v_p["pace"] is not None and abs(v_p["pace"] - 1.0) < 0.01,
       f"基准 10000×30% = 3000 → pace ≈ 1.0（得到 {v_p['pace']}）")
    ok("相当" in v_p["pace_txt"], f"节奏文案：{v_p['pace_txt']}")
    v_f = engine.evaluate_volume(cfg, 5000.0, [], session=sess,
                                 frac_hint=0.30, ref_amount=10000.0)
    ok(v_f["pace"] > 1.25 and "放量" in v_f["pace_txt"],
       f"快 67% → 明显放量：{v_f['pace_txt']}")
    # 收盘后才是事实
    vc = engine.evaluate_volume(cfg, 19000.0, [], session={"phase": "after_close", "tmin": 240})
    ok(vc["amount"] == 19000.0 and vc["intraday"] is False,
       "收盘后 amount = 全天终值（事实，不再是预测）")
    pre = {"phase": "pre_open", "tmin": 0}
    v_fb = engine.evaluate_volume(cfg, 0, [], session=pre, fallback_amount=19000.0,
                                  fallback_final=False)
    ok("盘中快照" in v_fb["basis"], f"只扫到盘中的基准要说明偏低：{v_fb['basis']}")
    v_ft = engine.evaluate_volume(cfg, 0, [], session=pre, fallback_amount=19000.0,
                                  fallback_final=True)
    ok("全天" in v_ft["basis"], f"收盘终值才叫全天：{v_ft['basis']}")

    print("\n[8] 节奏对比必须「钱对钱」：基准只能取实测同期累计")
    # 原 bug（2026-10-13 实测）：分支条件是 `ref_amount > 0 and amt_raw > 0`，漏了
    # "必须有实测同期基准"这一条 —— frac_hint 为 None 时仍拿"全天额中位数 × 写死的
    # 经验曲线"造出一个看着确定的 pace：经验曲线给 0.45、档案实测是 0.672，差 36%，
    # 足以把"与近期同期相当"渲染成"快 33%（明显放量）"。
    clean()
    rn = engine.evaluate_volume(cfg, 10082.0, [], session=sess,
                                ref_amount=16821.3, frac_hint=None)
    ok(rn["pace"] is None,
       "只有全天额中位数、没有实测同期基准 → 不给 pace（不再用经验曲线造数）")
    ok("积累中" in rn["pace_txt"], f"明说在积累：{rn['pace_txt']}")
    ok(rn["gauge"] is None, "gauge 一并空缺（前端不得拿写死的 50% 冒充读数）")

    rn2 = engine.evaluate_volume(cfg, 10082.0, [], session=sess,
                                 ref_amount=16821.3, ref_cum=10329.9, ref_cum_days=1)
    ok(rn2["pace_src"] == "实测同期", f"基准口径 = 实测同期（{rn2['pace_src']}）")
    ok(abs(rn2["pace"] - 0.98) < 0.01, f"钱对钱 → 0.98「相当」（得到 {rn2['pace']}）")
    ok("相当" in rn2["pace_txt"] and "明显放量" not in rn2["pace_txt"],
       f"结论被纠正为：{rn2['pace_txt']}")
    ok("仅供参考" in rn2["pace_txt"], "同期样本仅 1 天 → 文案标注天数")
    ok(rn2["gauge"] is not None and rn2["gauge"] < 70,
       f"进度条不再顶格（{rn2['gauge']}）")

    print("\n[8b] 阈值按**原始比值**判定，不被四舍五入挪动边界")
    r_fast = engine.evaluate_volume(cfg, 12500.0, [], session=sess,
                                    ref_cum=10000.0, ref_cum_days=3)
    r_edge = engine.evaluate_volume(cfg, 12499.0, [], session=sess,
                                    ref_cum=10000.0, ref_cum_days=3)
    r_slow = engine.evaluate_volume(cfg, 8000.0, [], session=sess,
                                    ref_cum=10000.0, ref_cum_days=3)
    r_over = engine.evaluate_volume(cfg, 8001.0, [], session=sess,
                                    ref_cum=10000.0, ref_cum_days=3)
    ok("明显放量" in r_fast["pace_txt"], f"1.25 恰好触发：{r_fast['pace_txt']}")
    ok("相当" in r_edge["pace_txt"],
       f"1.2499 不触发（旧实现先 round→1.25 会误判放量）：{r_edge['pace_txt']}")
    ok("明显缩量" in r_slow["pace_txt"], f"0.80 恰好触发：{r_slow['pace_txt']}")
    ok("相当" in r_over["pace_txt"], f"0.8001 不触发：{r_over['pace_txt']}")

    print("\n[8c] 同期基准的样本边界：当日自身、时刻对不上的样本都不得进来")
    clean()
    for d, cum in (("20261009", 6000.0), ("20261010", 7000.0)):
        ds.save_market_amount(d, 10000.0, final=True)
        ds.save_intraday_amount(d, 60, cum)
    ds.save_market_amount("20261013", 10000.0, final=False)   # 今天：仅盘中快照
    ds.save_intraday_amount("20261013", 60, 10000.0)
    bb = ds.intraday_baseline(60)                # 故意不传 exclude_date
    ok(bb["days"] == 2, f"当日自身被排除（样本 {bb['days']} 天，不是 3 天）")
    ok(abs(bb["cum"] - 6500.0) < 0.1, f"中位数未被今天的值拉高（{bb['cum']}）")
    ok(bb["frac"] is None, "分母必须是收盘终值 → 今天不参与占比")
    clean()
    ds.save_market_amount("20261010", 10000.0, final=True)
    ds.save_intraday_amount("20261010", 50, 5000.0)          # 只采到 tmin=50
    ok(ds.intraday_baseline(90)["days"] == 0,
       "采样时刻差 40 分钟 > tol → 弃用（否则基准被压低→假放量）")
    ok(ds.intraday_baseline(55)["days"] == 1, "差 5 分钟 → 采信")

    print("\n[9] 量能降档文案不得自相矛盾，也不得把同一个名字连读两遍")
    # 背景（实测 20261014）：绝对值层与相对分位层各答各的问题，但原来拼在一句里
    # 成了「成交额绝对值够（14144亿），但…相对近期明显偏低」—— 先说"够"再降档，
    # 读的人只会困惑"到底够不够"。而绝对阈值 12000 亿在当前市场已长期不达标，
    # 说"够"本身就是拿一个过期常量当结论。
    #
    # 另一处：rel_adj 结尾是「由量能舒适降为缩量存量」，紧跟的 advice 又以
    # 「缩量存量，」开头 → 同一个档位名连着出现两次（像卡带）。
    # 这两条都是**文案契约**，改措辞很容易再犯，钉在这里。
    vcfg = {"M2_volume": {"enabled": True, "peak_threshold": 15000,
                          "comfort_threshold": 12000, "shrink_threshold": 8000,
                          "lookback": 20, "peak_pct": 0.80,
                          "peak_trend_min": 1.00, "shrink_pct": 0.45}}
    # 20 天高量 + 今天低量 → 绝对落"舒适"，相对分位极低 → 降档
    _h = [{"date": "d%02d" % i, "open": 100, "high": 101, "low": 99,
           "close": 100.5, "volume": 5.0e8} for i in range(20)]
    _h.append({"date": "today", "open": 100, "high": 101, "low": 99,
               "close": 100.5, "volume": 3.0e8})
    _sess = {"phase": "after_close", "tmin": 240}
    v_dn = engine.evaluate_volume(vcfg, 14144.0, _h, session=_sess)
    ok(v_dn["mode"] == "shrink_short",
       f"14144 亿绝对落舒适、相对分位 0% → 应降为缩量存量（得到 {v_dn['mode']}）")
    ok("绝对值够" not in v_dn["advice"],
       f"不得再说「绝对值够」（那是拿过期阈值当结论）：{v_dn['advice'][:80]}")
    ok("降为缩量存量" in v_dn["advice"], "要如实说明是怎么降下来的")
    # 档位名连读检查：降档句说明的档位名，不应紧接着出现在 advice 尾部
    _after = v_dn["advice"].split("降为缩量存量", 1)[-1]
    ok(not _after.lstrip("；;，, ").startswith("缩量存量"),
       f"降档句后面不得再以同一个档位名开头：{_after.lstrip('；;，, ')[:40]}")
    ok("资金撬不动大票" in v_dn["advice"], "去掉重复的档位名后，应对打法仍要保留")

    # 峰值档同理：绝对过峰值线但相对分位低 → 降档，且不得说"达标"
    _h2 = [{"date": "d%02d" % i, "open": 100, "high": 101, "low": 99,
            "close": 100.5, "volume": 5.0e8} for i in range(20)]
    _h2.append({"date": "today", "open": 100, "high": 101, "low": 99,
                "close": 100.5, "volume": 3.0e8})
    v_pk = engine.evaluate_volume(vcfg, 30000.0, _h2, session=_sess)
    ok("绝对值达标" not in v_pk["advice"],
       f"峰值档也不得说「绝对值达标」：{v_pk['advice'][:80]}")
    ok("不构成峰值量能" in v_pk["advice"], "峰值档降下来要说明为何不算峰值")

    # 不降档时，advice 必须自带档位名（信息完整，不能因为去重被削掉）
    _h3 = [{"date": "d%02d" % i, "open": 100, "high": 101, "low": 99,
            "close": 100.5, "volume": 3.0e8} for i in range(20)]
    _h3.append({"date": "today", "open": 100, "high": 101, "low": 99,
                "close": 100.5, "volume": 9.0e8})
    v_up = engine.evaluate_volume(vcfg, 30000.0, _h3, session=_sess)
    ok(v_up["mode"] == "peak_swing", f"相对也高 → 维持峰值档（得到 {v_up['mode']}）")
    ok("降为" not in v_up["advice"], "没降档就不该出现降档措辞")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
