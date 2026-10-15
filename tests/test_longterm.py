# -*- coding: utf-8 -*-
"""E11 大金融中长线（1~3 年）卖出纪律 —— 自检。

用户 2026-10-15：持仓大金融属性的股票不要用当前市场的判断给卖点，这类票
一般是持有 1~3 年的中长线，短期市场变化对它没用。

本文件钉死三件事：
  ① 识别：大金融（银行/券商/保险/多元金融）走中长线口径，其余走短线；
  ② 隔离：退潮清仓等短线信号**不得**出现在中长线持仓的结论里（这是用户
     投诉的那句"一会能买一会不能买/乱给卖点"的根因）；
  ③ 不编数：估值取不到时不给估值类结论，且宽幅止损而非短线均线止损。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import longterm as lt          # noqa: E402
from core import portfolio as pf         # noqa: E402
from core import strategy_config as sc   # noqa: E402

OK = []
FAIL = []


def ok(cond, msg):
    (OK if cond else FAIL).append(msg)


# ---------------------------------------------------------------- ① 识别
def t_identify():
    for ind, name, want in [
        ("证券", "中国银河", True),
        ("银行", "招商银行", True),
        ("保险", "中国平安", True),
        ("多元金融", "东方财富", True),
        ("半导体", "中芯国际", False),
        ("白酒", "贵州茅台", False),
        ("", "", False),
    ]:
        got, why = lt.is_longterm({"code": "sh600000", "name": name}, ind)
        ok(got == want, "识别 %s/%s -> %s（%s）" % (ind, name, got, why[:24]))

    # 名称兜底：行业字段缺失时靠名字
    got, _ = lt.is_longterm({"name": "中信证券"}, "")
    ok(got, "行业缺失时按名称兜底识别券商")
    got, _ = lt.is_longterm({"name": "某人寿"}, "")
    ok(got, "行业缺失时按名称兜底识别保险")

    # 明确不该被误判成金融的
    for nm in ["东方财富网科技", "保险柜制造"]:
        got, _ = lt.is_longterm({"name": nm}, "")
        ok(not got, "不误判：%s" % nm)


# ---------------------------------------------------------------- ② 隔离
def t_isolation():
    """核心契约：退潮清仓不得进中长线结论。"""
    cfg = sc.load_config()
    snaps = {"sh601881": {"price": 11.80, "pct": 1.45, "name": "中国银河"}}
    # 最坏的市场状态：退潮 + 不可买 + 缩量
    sent = {"stage": "ebb", "can_trade": False, "trial_only": True,
            "label": "退潮期 · 只做首板试错仓"}
    vol = {"mode": "dry"}
    r = pf.evaluate(snaps, {"sh601881": []}, sent, vol, cfg)
    h = r["holdings"][0]

    ok(h["longterm"] is True, "中国银河被标为中长线")
    ok(r["n_longterm"] == 1, "组合层统计 n_longterm=1")

    txt = (h["plain"] or "") + " " + " ".join(
        (s.get("name", "") + s.get("action", "")) for s in h["signals"])
    for bad in ["无条件清仓", "今天就走", "别再等"]:
        ok(bad not in txt, "退潮话术「%s」未出现在中长线结论里" % bad)
    ok("中长线" in (h["plain"] or ""), "结论明确写明是中长线口径")

    # 止损必须是宽幅灾难位，不是短线均线止损
    ep = h["exit_plan"]
    ok(ep.get("stop_pct") == -25.0,
       "止损为宽幅 -25%%（实得 %s）" % ep.get("stop_pct"))
    ok(ep.get("stop") == round(12.07 * 0.75, 2),
       "灾难位价格 = 成本×0.75（实得 %s）" % ep.get("stop"))
    note = ep.get("note", "")
    ok("中长线" in note and "短线均线" in note, "卖出参考价注明不用短线均线")

    # 组合 note 必须说明口径差异
    ok("中长线" in r["note"] and "1~3 年" in r["note"], "组合 note 说明中长线口径")
    ok("不套用" in r["note"], "组合 note 明确不与短线信号混用")

    # 非中长线持仓仍走短线体系（不能被这次改动误伤）。
    # ★ 注意：portfolio.evaluate 内部 load() 真实持仓文件，snaps 参数**不决定**
    #   持仓集合 —— 所以"非金融不被标中长线"必须直接测 longterm 这一层，
    #   不能靠喂一个不存在的 snaps 去测（那样测的其实是真实持仓）。
    for code, nm in [("sz002836", "某票"), ("sh600519", "贵州茅台"),
                     ("sz300750", "宁德时代")]:
        lt_flag, why = lt.is_longterm({"code": code, "name": nm}, "")
        ok(lt_flag is False, "非金融 %s(%s) 不判中长线" % (nm, code))
    r2 = lt.evaluate([{"code": "sz002836", "name": "某票",
                       "cost": 10, "shares": 100}], {}, cfg)
    ok(r2["items"] == {}, "非金融持仓在中长线报告里不产出条目")
    # ★ 识别**依赖行业字段**（这是设计约束，必须钉住）：
    #   "中国银河"光看名字看不出是券商（不含 证券/银行/保险 等后缀），
    #   只有拿到行业（datacenter-web 的 BOARD_NAME="证券"）才能判。
    #   所以 val 为空时命中不了是**预期行为**，不是 bug —— 但不能因此崩。
    bare = {"code": "sh601881", "name": "中国银河", "cost": 12.07,
            "shares": 200, "date": "2026-10-15"}
    lt_noind, _ = lt.is_longterm(bare, "")
    ok(lt_noind is False, "无行业字段时名字看不出券商 → 不误判（宁可不认）")
    ok(lt.evaluate([bare], {}, cfg)["items"] == {},
       "无行业字段时中长线报告为空（不猜）")
    # 拿到行业就必须命中 —— 这是用户实际在意的那个持仓
    lt_withind, why = lt.is_longterm(bare, "证券")
    ok(lt_withind is True, "拿到行业(证券)后中国银河命中中长线")
    r3 = lt.evaluate([bare],
                     {"stocks": {"601881": {"industry": "证券"}}}, cfg)
    ok("sh601881" in r3["items"], "带行业信息时真实持仓命中中长线")


# ---------------------------------------------------------------- ③ 不编数
def t_no_fabrication():
    cfg = sc.load_config()
    # 归档清空 + 上游断供 → 不给估值结论，但也不能崩
    import core.longterm as L
    old_read, old_fetch = L._read_history, None
    import core.datasource as ds
    old_fetch = ds.fetch_board_valuation
    try:
        L._read_history = lambda: {}
        ds.fetch_board_valuation = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("upstream down"))
        val = lt.valuation_view([{"code": "sh601881", "name": "中国银河"}],
                                cfg)
        ok(val["boards"] == {}, "上游断供时板块估值为空（不编）")
        r = lt.evaluate([{"code": "sh601881", "name": "中国银河",
                          "cost": 12.07, "shares": 200, "date": "2026-10-15"}],
                        val, cfg)
        rows = r.get("holdings") or []
        if rows:
            joined = " ".join(s.get("name", "") for s in rows[0]["signals"])
            ok("估值" not in joined or "没取到" in joined,
               "无估值时不产出估值类卖出信号")
    finally:
        L._read_history = old_read
        ds.fetch_board_valuation = old_fetch

    # 分位样本不足就不给分位（宁可不给，不给噪声）
    few = [{"date": "2026-10-%02d" % (i + 1), "pb": 1.0 + i * 0.01}
           for i in range(10)]
    p = ds.valuation_percentile(few, "pb")
    ok(p["pct"] is None, "样本 10 < min_samples 时不给分位（实得 %s）" % p["pct"])
    ok(p["n"] == 10, "但如实报样本数 n=10")


# ---------------------------------------------------------------- ④ 分位计算
def t_percentile():
    import core.datasource as ds
    # 线性序列：最新值最大 → 分位接近 100
    ser = [{"pb": 1.0 + i * 0.01} for i in range(200)][::-1]  # 新→旧，新值最大
    p = ds.valuation_percentile(ser, "pb")
    ok(p["pct"] == 100.0, "最新值最大 → 分位 100（实得 %s）" % p["pct"])
    ser2 = [{"pb": 1.0 + i * 0.01} for i in range(200)]       # 新→旧，新值最小
    p2 = ds.valuation_percentile(ser2, "pb")
    ok(p2["pct"] == 0.5, "最新值最小 → 分位 0.5（实得 %s）" % p2["pct"])
    ok(p2["value"] == 1.0, "value 取序列首个（最新）")
    # 非法值被剔除，不参与分位
    ser3 = [{"pb": 2.0}] + [{"pb": None}, {"pb": -1}, {"pb": 0}] * 30 + \
           [{"pb": 1.0 + i * 0.001} for i in range(100)]
    p3 = ds.valuation_percentile(ser3, "pb")
    ok(p3["n"] == 101, "None/负数/0 被剔除（实得 n=%s）" % p3["n"])


if __name__ == "__main__":
    for fn in [t_identify, t_isolation, t_no_fabrication, t_percentile]:
        try:
            fn()
        except Exception as e:
            FAIL.append("%s 抛异常: %r" % (fn.__name__, e))
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    for m in FAIL:
        print("  ✗", m)
    if not FAIL:
        for m in OK:
            print("  ✓", m)
    sys.exit(1 if FAIL else 0)
