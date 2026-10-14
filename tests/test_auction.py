# -*- coding: utf-8 -*-
"""E9 竞价早读（集合竞价 09:15–09:25 → 今天该盯什么）。

真实场景（10-14 集合竞价窗口实测）：
  腾讯快照在竞价时段**能读的**只有竞价匹配价（[3] price）、昨收（[4]）、
  涨跌幅（[32]）、买卖一量（[9]/[10]/[12]）、时间戳（[30]）；
  **读不到的**是今开（[5]=0.00，还没开盘）、成交量/额/换手/量比（全 0）。
  实测 09:20–09:25 全市场 6173 只样本，0~2% 档占 89%。

  踩过的坑（本测试要钉住）：
    · 用 `open` 当竞价匹配价 → 全市场 0.00，整块算成 0。
    · 用 买/(买+卖) 算"委比" → 竞价卖侧几乎恒 0，得到 ~99% 假失衡。
    · 写死 9.9% 判涨停 → 20cm 的创业板/科创板全漏。

本测试钉住：
  ① 竞价匹配价取 price 不取 open；开盘后回看取 open。
  ② 涨停判定按个股自己的 limit_up（主板/20cm/ST 都能认）。
  ③ 高开档位边界（含 ±2% / ±5% / 涨跌停）。
  ④ 市场偏向按净差占比，阈值可配；噪声底不构成信号。
  ⑤ 昨涨停池今晨的六态（一字/高开/小幅/低开/大幅低开/无数据）。
  ⑥ 异动三榜（一字/大高开/大低开）与 big_gap 可配。
  ⑦ 关闭 / 无快照 / 昨收无效 三种空态都不硬凑。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="auction_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import auction as au                 # noqa: E402
from core import strategy_config as sc         # noqa: E402
from core import trading                       # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def snap(code, name, price, prev, lu, ld=0.0, open_=0.0, t="20261014092033"):
    """构造一只票的竞价快照 —— 字段名对齐 datasource._parse_tx_line。"""
    return {"code": code, "name": name, "price": price, "prev_close": prev,
            "open": open_, "volume": 0.0, "limit_up": lu, "limit_down": ld,
            "time": t}


# 竞价场景：主板 10cm、创业板 20cm、ST 5cm、低开、平开
SNAPS = {
    # 3 板龙头，竞价一字（匹配价 == 涨停价 13.20）
    "sz002436": snap("sz002436", "兴森科技", 13.20, 12.00, 13.20),
    # 20cm 票涨停：+20%，若写死 9.9% 会被误判成"高开"而非涨停
    "sz300750": snap("sz300750", "宁德时代", 216.00, 180.00, 216.00),
    # ST 5cm 涨停
    "sh600001": snap("sh600001", "ST 某某", 5.25, 5.00, 5.25),
    # 高开 8.3%
    "sz002242": snap("sz002242", "九阳股份", 21.68, 20.02, 22.02),
    # 高开 3.0%（落在 gap2 档：2% ≤ gap < 5%）
    "sh603259": snap("sh603259", "药明康德", 51.50, 50.00, 55.00),
    # 小幅高开 1.5%（落在 flat 档）
    "sh601138": snap("sh601138", "工业富联", 21.32, 21.00, 23.10),
    # 平开
    "sz000001": snap("sz000001", "平安银行", 11.00, 11.00, 12.10),
    # 低开 -3.3%
    "sz000725": snap("sz000725", "京东方Ａ", 3.87, 4.00, 4.40),
    # 大低开 -8.6%
    "sh600519": snap("sh600519", "贵州茅台", 1389.00, 1520.00, 1672.00),
    # 跌停
    "sz002594": snap("sz002594", "比亚迪", 90.00, 100.00, 110.00, ld=90.00),
    # 昨收为 0（无效应排除出统计）
    "sz000999": snap("sz000999", "坏数据", 10.00, 0.0, 11.00),
}

# 昨日涨停池（归档口径）
PREV_ZT = [
    {"code": "sz002436", "name": "兴森科技", "boards": 3, "sector": "PCB"},
    {"code": "sz002242", "name": "九阳股份", "boards": 6, "sector": "家电"},
    {"code": "sh601138", "name": "工业富联", "boards": 2, "sector": "算力"},
    {"code": "sz000725", "name": "京东方Ａ", "boards": 1, "sector": "面板"},
    {"code": "sh600519", "name": "贵州茅台", "boards": 1, "sector": "白酒"},
    {"code": "sz000998", "name": "停牌股", "boards": 2, "sector": "农业"},
]


def band(r, key):
    return {b["key"]: b for b in r["temperature"]["bands"]}.get(key)


def main():
    print("===== E9 竞价早读：匹配价 / 档位 / 偏向 / 昨涨停今竞价 / 异动 =====\n")

    cfg = sc.default_config()

    # [1] 配置进了预设
    print("[1] 配置")
    ok("E9_auction" in sc.PRESETS["wildman"], "E9_auction 进了 wildman 预设")
    d = cfg.get("E9_auction") or {}
    ok(d.get("enabled") is True, "默认开启")
    ok(d.get("top_n") == 10 and d.get("top_prev") == 12, "默认 top_n=10 / top_prev=12")
    ok(abs(d.get("big_gap", 0) - 5.0) < 1e-9, "默认 big_gap=5.0")
    print()

    # [2] 竞价匹配价：price 而非 open
    print("[2] 竞价匹配价")
    ok(au.match_price(SNAPS["sz002436"]) == 13.20,
       "竞价时段取 price（匹配价）")
    ok(au.match_price({"price": 13.20, "open": 0.0}) == 13.20,
       "open=0 时绝不拿 open 当匹配价（实测竞价今开恒为 0）")
    # 开盘后回看：拿 open
    _sess = trading.market_session
    trading.market_session = lambda *a, **k: {"phase": "morning", "label": "早盘"}
    try:
        ok(au.match_price({"price": 13.50, "open": 13.30}) == 13.30,
           "开盘后回看取 open（当日真实开盘价）")
        ok(au.match_price({"price": 13.50, "open": 0.0}) == 13.50,
           "开盘后 open 取不到 → 退回 price，不给 0")
    finally:
        trading.market_session = _sess
    print()

    # [3] 涨停判定按个股自己的涨停价
    print("[3] 涨停判定")
    ok(au.is_limit_up(SNAPS["sz002436"]), "主板 10cm 一字 → 涨停")
    ok(au.is_limit_up(SNAPS["sz300750"]),
       "创业板 20cm 一字 → 涨停（写死 9.9% 会漏掉）")
    ok(au.is_limit_up(SNAPS["sh600001"]), "ST 5cm 一字 → 涨停")
    ok(not au.is_limit_up(SNAPS["sz002242"]), "高开 8.3% 但没到涨停价 → 不是涨停")
    ok(au.is_limit_down(SNAPS["sz002594"]), "跌停能认出来")
    print()

    # [4] 档位边界
    print("[4] 高开档位")
    ok(au.band_of(10.0, SNAPS["sz300750"]) == "limit_up", "匹配价=涨停价 → limit_up 档")
    ok(au.band_of(5.0, SNAPS["sz002242"]) == "gap5", "恰好 +5% 归高开≥5%")
    ok(au.band_of(4.9, SNAPS["sz002242"]) == "gap2", "+4.9% 归高开2-5%")
    ok(au.band_of(2.0, SNAPS["sz002242"]) == "gap2", "恰好 +2% 归高开2-5%")
    ok(au.band_of(1.9, SNAPS["sz002242"]) == "flat", "+1.9% 归平开")
    ok(au.band_of(-1.9, SNAPS["sz002242"]) == "flat", "-1.9% 归平开")
    ok(au.band_of(-2.0, SNAPS["sz002242"]) == "down2", "恰好 -2% 归低开2-5%")
    ok(au.band_of(-5.0, SNAPS["sz002242"]) == "down5", "恰好 -5% 归低开≥5%")
    print()

    # [5] 正常评估
    print("[5] 正常评估")
    r = au.evaluate(cfg, snaps=SNAPS, prev_zt=PREV_ZT, real_date="2026-10-14")
    ok(r["active"] is True, "有快照 → active")
    ok(r["snapshot_time"] == "09:20:33", "时间戳解析成 HH:MM:SS（取自 [30]）")
    temp = r["temperature"]
    # 9 只有效（昨收为 0 的那只被排除）
    ok(temp["valid"] == 10, f"无效应排除：有效 {temp['valid']} 只（含昨收为 0 的那只被剔除）")
    ok(temp["limit_up"] == 3, f"竞价涨停 3 只（10cm+20cm+ST），实得 {temp['limit_up']}")
    ok(temp["limit_down"] == 1, "竞价跌停 1 只")
    ok(band(r, "gap5")["n"] == 1, "高开≥5% 1 只")
    ok(band(r, "gap2")["n"] == 1, "高开2-5% 1 只")
    ok(band(r, "flat")["n"] == 2, "平开 2 只（+1.52% 与 0%）")
    ok(band(r, "down2")["n"] == 1, "低开2-5% 1 只（-3.25%）")
    ok(band(r, "down5")["n"] == 1, "低开≥5% 1 只（-8.62%）")
    ok(band(r, "limit_down")["n"] == 1, "竞价跌停 1 只（-10% 归跌停档，不归低开≥5%）")
    ok(sum(b["n"] for b in temp["bands"]) == 10, "各档家数合计 = 有效数")
    ok(0 <= max(b["pct"] for b in temp["bands"]) <= 100, "百分比在合理区间")
    print()

    # [6] 偏向阈值可配
    print("[6] 市场偏向")
    ok(r["temperature"]["bias"] in ("risk_on", "risk_off", "neutral"), "偏向取值合法")
    net = r["temperature"]["net_pct"]
    c_strict = sc.default_config()
    c_strict["E9_auction"]["bias_th"] = abs(net) + 10
    r_strict = au.evaluate(c_strict, snaps=SNAPS, prev_zt=PREV_ZT,
                           real_date="2026-10-14")
    ok(r_strict["temperature"]["bias"] == "neutral",
       "阈值抬高到净差之上 → 判中性（说明阈值真的在读配置）")
    print()

    # [7] 昨涨停池今晨竞价
    print("[7] 昨涨停今竞价")
    rev = r["review"]
    ok(rev["n"] == 6, f"昨日涨停 6 只全覆盖，实得 {rev['n']}")
    by = {x["name"]: x for x in rev["rows"]}
    ok(by["兴森科技"]["state"] == "sealed", "一字封死")
    ok(by["九阳股份"]["state"] == "strong", "高开抢筹")
    ok(by["工业富联"]["state"] == "mild", "小幅高开")
    ok(by["京东方Ａ"]["state"] == "weak", "低开示弱")
    ok(by["贵州茅台"]["state"] == "dead", "大幅低开")
    ok(by["停牌股"]["state"] == "missing", "快照里没有 → 无竞价数据（不硬凑成 0%）")
    ok(by["停牌股"]["gap"] is None, "无数据的 gap 是 None 而不是 0")
    ok(by["停牌股"]["price"] is None, "无数据的 price 也是 None")
    # 排序：一字在前，大幅低开在后
    order = [x["state"] for x in rev["rows"]]
    ok(order.index("sealed") < order.index("weak") < order.index("dead"),
       "排序：一字 → 高开 → 平 → 低开 → 大幅低开")
    ok(r["review"]["strong"] == 2 and r["review"]["dead"] == 1, "聚合计数正确")
    ok(all(x["why"] for x in rev["rows"]), "每只都给了一句读法")
    print()

    # [8] 异动三榜
    print("[8] 异动三榜")
    mv = r["movers"]
    ok(mv["n_sealed"] == 3, f"一字榜 3 只，实得 {mv['n_sealed']}")
    ok(mv["n_highs"] == 1, f"大高开榜 1 只（8.3%），实得 {mv['n_highs']}")
    ok(mv["n_lows"] == 2, f"大低开榜 2 只（-8.6% 与跌停），实得 {mv['n_lows']}")
    ok(all(x["why"] for x in mv["highs"] + mv["lows"]), "异动都给了解释")
    # big_gap 抬高 → 大高开榜清空
    c_hi = sc.default_config()
    c_hi["E9_auction"]["big_gap"] = 15.0
    r_hi = au.evaluate(c_hi, snaps=SNAPS, prev_zt=PREV_ZT, real_date="2026-10-14")
    ok(r_hi["movers"]["n_highs"] == 0,
       "big_gap=15 后 8.3% 不再算大高开（阈值在读配置）")
    ok(r_hi["movers"]["n_sealed"] == 3, "但涨停一字的判定不受 big_gap 影响")
    # top_n 截断
    c_top = sc.default_config()
    c_top["E9_auction"]["top_n"] = 1
    r_top = au.evaluate(c_top, snaps=SNAPS, prev_zt=PREV_ZT, real_date="2026-10-14")
    ok(len(r_top["movers"]["sealed"]) == 1, "top_n=1 → 一字榜只留 1 只")
    ok(r_top["movers"]["n_sealed"] == 3, "但总数如实报 3（截断不改变事实）")
    print()

    # [9] 空态
    print("[9] 空态")
    r_off = au.evaluate({"E9_auction": {"enabled": False}}, snaps=SNAPS,
                        prev_zt=PREV_ZT, real_date="2026-10-14")
    ok(r_off["active"] is False and "关闭" in r_off["reason"], "关闭 → 不激活")
    r_none = au.evaluate(cfg, snaps={}, prev_zt=PREV_ZT, real_date="2026-10-14")
    ok(r_none["active"] is False and "快照" in r_none["reason"], "无快照 → 说明原因")
    bad = {"sz000001": snap("sz000001", "X", 0.0, 0.0, 0.0)}
    r_bad = au.evaluate(cfg, snaps=bad, prev_zt=[], real_date="2026-10-14")
    ok(r_bad["active"] is False, "昨收全无效 → 不激活（不硬凑成 0%）")
    r_nozt = au.evaluate(cfg, snaps=SNAPS, prev_zt=[], real_date="2026-10-14")
    ok(r_nozt["active"] is True, "没有昨涨停池也照样出全市场分布")
    ok(r_nozt["review"]["n"] == 0, "但昨涨停那栏如实为空")
    ok("note" in r and r["note"], "非空态都带口径说明（note）")
    print()

    # [10] 指标锚点：绝不凭 0 值算指标
    print("[10] 0 值护栏")
    # 竞价时段成交量/额/换手/量比恒为 0，模块不得据此**下结论**说放量、流入。
    # 注意：note 里为了说明限制**提到了**这些词（"给不出来"），那是解释不是结论，
    # 所以这里只查会当结论读的 summary 与各行 why，不能全文搜字符串。
    concl = (r["summary"] or "")
    for x in r["review"]["rows"]:
        concl += x.get("why") or ""
    for k in ("sealed", "highs", "lows"):
        for x in r["movers"][k]:
            concl += x.get("why") or ""
    for w in ("放量", "资金流入", "净流入", "承接资金", "主力"):
        ok(w not in concl, f"结论里不出现「{w}」——竞价没有成交，给不出这种话")
    ok("委比" not in concl, "结论里不出现委比（竞价卖侧恒 0，算出来是假的）")
    ok("委比" in (r["note"] or ""), "但 note 里如实说明为什么不给委比")
    ok("不是买入指令" in (r["note"] or ""), "note 说明定位：观察不是买入指令")
    print()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
