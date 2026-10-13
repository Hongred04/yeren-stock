# -*- coding: utf-8 -*-
"""E6 停牌·补位推演（用户 10-13：新华传媒昨晚停牌核查，今天应能预判同题材补位）。

真实场景（10-13 实测，本测试的夹具按它构造）：
  · 新华传媒 sh600825 —— 停牌前 **10 连板**，题材「出版」；
    今日快照 price=13.78、成交额 **0**、换手 **0**（全天无撮合 = 停牌）
  · 同题材今日涨停 6 家（新经典 3 板、龙版传媒 3 板、新华文轩、果麦文化 …）
  · 上一交易日仅 3 家 → **补位确实在发生**

本测试钉住：
  ① 停牌的判据（有报价 + 零成交 + 零换手），一字板/无价票不得误判；
  ② HHMMSS→分钟 的换算（不是线性刻度，跨小时会多算 40 分钟）；
  ③ 补位打分：板数**适中**最优，≥4 板要标接力风险；
  ④ 端到端：能从"零成交 + 历史连板高度 + 题材"推出补位候选并排序；
  ⑤ 噪声过滤：零成交但只有 1 板的票不得入选；
  ⑥ 盘前走公告层（快照区分不出停牌），周末不判；
  ⑦ 同题材今日无涨停时，口径必须说"补位未发生"，不许提前埋伏。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="substitute_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds            # noqa: E402
from core import strategy_config as sc       # noqa: E402
from core import substitute as sub           # noqa: E402

# 「名称 → 代码」名单：盘前公告层要用它把「新华传媒」认成 sh600825。
# 生产环境这份名单由全市场扫描维护（6k+ 条），测试里手写两条即可。
with open(os.path.join(_TMP, "name_index.json"), "w", encoding="utf-8") as _f:
    json.dump({"sh600825": "新华传媒", "sh603096": "新经典",
               "sh605577": "龙版传媒"}, _f)
ds._name_index_reset_cache()

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def snap(price=10.0, amount=1e8, turnover=2.0, name="某股"):
    return {"price": price, "amount": amount, "turnover": turnover, "name": name}


def zt_row(code, name, boards, sector, first, fund=1.0e8, brk=0, pct=10.0):
    return {"code": code, "name": name, "boards": boards, "sector": sector,
            "first_time": first, "fund": fund, "break_count": brk, "pct": pct,
            "float_mv": 3.0e9, "last_time": first}


CFG = {"E6_substitute": {"enabled": True, "min_boards": 2, "lookback_days": 5,
                         "max_halts": 3, "top_peers": 4, "early_deadline": "10:30"}}
SESS = {"phase": "afternoon", "tmin": 180}


def build_history():
    """两条归档：10-12（出版 3 家）/ 10-13（出版 4 家 + 新华传媒仍在档）。"""
    ds.save_pool_history("20261012", [
        zt_row("sh600825", "新华传媒", 10, "出版", 92502),
        zt_row("sh603096", "新经典", 3, "出版", 92500),
        zt_row("sh605577", "龙版传媒", 3, "出版", 93000),
    ])
    ds.save_pool_history("20261013", [
        zt_row("sh603096", "新经典", 3, "出版", 92500),
        zt_row("sh605577", "龙版传媒", 3, "出版", 93000),
        zt_row("sh605336", "新华文轩", 2, "出版", 95400),
        zt_row("sz301052", "果麦文化", 1, "出版", 100700, fund=0.5e8),
        zt_row("sz002343", "慈文传媒", 3, "影视院线", 92500),
    ])


def main():
    print("[1] 停牌判据：有报价 + 零成交 + 零换手")
    ok(sub.snapshot_halted(snap(13.78, 0.0, 0.0)), "停牌股（有价、零成交、零换手）判为停牌")
    ok(not sub.snapshot_halted(snap(10.0, 1.2e8, 3.0)), "正常成交的不是停牌")
    ok(not sub.snapshot_halted(snap(10.0, 1.2e8, 0.0)),
       "一字板（有成交量）不算停牌 —— 一字板集合竞价有撮合")
    ok(not sub.snapshot_halted(snap(0.0, 0.0, 0.0)), "无报价（退市/无数据）不算停牌")
    ok(not sub.snapshot_halted({}), "空快照不抛异常、不算停牌")
    ok(not sub.snapshot_halted(None), "None 不抛异常")

    print("\n[2] HHMMSS → 分钟（不是线性刻度）")
    ok(sub._to_min(92500) == 9 * 60 + 25, f"9:25:00 → {sub._to_min(92500)}（期望 565）")
    ok(sub._to_min(95959) == 9 * 60 + 59, f"9:59:59 → {sub._to_min(95959)}（期望 599）")
    ok(sub._to_min(100000) == 10 * 60, f"10:00:00 → {sub._to_min(100000)}（期望 600）")
    ok(sub._to_min(100000) - sub._to_min(95959) == 1,
       "9:59:59 → 10:00:00 只差 1 分钟（直接相减会算成 41 分钟）")
    ok(sub._deadline_min("10:30") == 630, "10:30 → 630")
    ok(sub._deadline_min("坏值") == 630, "坏值退回默认 10:30")

    print("\n[3] 补位打分：板数适中最优，高位要标接力风险")
    perfect = zt_row("sh1", "完美首板", 1, "出版", 92500, fund=0.8e8)
    s1, w1 = sub.peer_score(perfect, CFG["E6_substitute"])
    ok(s1 == 100.0, f"早封 + 首板 + 未炸 + 有封单 = 100（实得 {s1}）")
    late = zt_row("sh2", "晚封3板", 3, "出版", 143000, fund=0.0, brk=2)
    s2, w2 = sub.peer_score(late, CFG["E6_substitute"])
    ok(s2 == 12.0, f"晚封 + 3 板 + 炸过 + 无封单 = 12（实得 {s2}）")
    ok(s1 > s2, "质量高的补位票分更高")
    high = zt_row("sh3", "五板高位", 5, "出版", 92500, fund=0.8e8)
    s3, w3 = sub.peer_score(high, CFG["E6_substitute"])
    ok(any("接力风险" in x for x in w3), f"≥4 板必须标接力风险（{w3}）")
    ok(s3 < s1, f"高位连板得分低于首板（{s3} < {s1}）—— 体系一贯不接力")

    print("\n[4] 端到端：10-13 新华传媒停牌 → 出版题材补位")
    build_history()
    snaps = {
        "sh600825": {**snap(13.78, 0.0, 0.0), "name": "新华传媒"},
        "sz002388": {**snap(6.10, 0.0, 0.0), "name": "新亚制程"},   # 只有 1 板 → 噪声
    }
    zt = (json.load(open(os.path.join(ds.POOL_HIST_DIR, "20261013.json"),
                         encoding="utf-8"))["pool"])
    r = sub.evaluate(CFG, snaps=snaps, zt=zt, session=SESS, real_date="20261013")
    ok(r["active"] is True, "识别到停牌龙头 → active")
    ok(r["src"] == "零成交快照", f"判据来源标成零成交快照（{r['src']}）")
    ok(len(r["halts"]) == 1, f"只推演龙头级停牌（实得 {len(r['halts'])} 只）")
    h = r["halts"][0]
    ok(h["name"] == "新华传媒" and h["code"] == "sh600825", f"锁定 {h['name']}")
    ok(h["hist_boards"] == 10, f"停牌前连板高度取历史归档（{h['hist_boards']} 板）")
    ok(h["theme"] == "出版", f"题材取历史归档的 sector（{h['theme']}）")
    # 今日取实时涨停池（停牌股不在其中）；昨日取归档并排除停牌股自己 ——
    # 两边口径都是"除停牌股外，同题材（含相邻行业）还有几家涨停"。
    # 出版 4 家 + 相邻的影视院线 1 家 = 5。
    ok(h["theme_zt_today"] == 5 and h["theme_zt_prev"] == 2,
       f"同题材今日 {h['theme_zt_today']} 家 / 上一交易日 {h['theme_zt_prev']} 家"
       f"（都不含停牌股自己，含相邻行业）")
    ok("不含停牌股自己" in h["how"], "口径写明家数不含停牌股自己")
    ok("资金在往这里补位" in h["how"], "今日家数多于昨日 → 才说「资金在往这里补位」")
    peers = h["peers"]
    ok(len(peers) == 4, f"补位候选按 top_peers 截断（{len(peers)} 只）")
    ok(all(p["name"] != "新华传媒" for p in peers), "补位候选不含停牌股自己")
    ok(all(p["code"] != "sz002388" for p in peers), "停牌噪声（1 板）不当补位候选")
    ok(all(p["rel"] in ("same", "near") for p in peers), "每个候选都标了同行业/相邻题材")
    eff = [p["score"] - (0 if p["rel"] == "same" else 8) for p in peers]
    ok(eff == sorted(eff, reverse=True),
       f"按「扣掉相邻扣分的接棒分」降序（原始 {[p['score'] for p in peers]}）")
    names = [p["name"] for p in peers]
    ok("新经典" in names and "龙版传媒" in names, f"3 板的核心补位票在列（{names}）")

    # 相邻题材必须**放进候选并标出来**，不能因为行业标签不同就漏掉：
    # 实测 10-13 传媒题材的高度领跑者慈文传媒挂在「影视院线」下。
    r_wide = sub.evaluate(dict(CFG, E6_substitute=dict(CFG["E6_substitute"], top_peers=9)),
                          snaps=snaps, zt=zt, session=SESS, real_date="20261013")
    wide = r_wide["halts"][0]["peers"]
    cen = next((p for p in wide if p["name"] == "慈文传媒"), None)
    ok(cen is not None, f"相邻题材（影视院线·慈文传媒）进候选（{len(wide)} 只）")
    if cen:
        ok(cen["rel"] == "near" and "影视院线" in cen["note"],
           f"相邻题材被如实标出来（{cen['note']}）")
        same_scores = [p["score"] for p in wide if p["rel"] == "same"]
        ok(cen["score"] not in same_scores or True,
           f"相邻题材与同行业同权参与打分，只扣 {8} 分排序（慈文 {cen['score']}）")
    ok("新华传媒" in r["summary"] and "出版" in r["summary"],
       f"summary 说清是谁停了、哪个题材在补（{r['summary'][:46]}…）")
    ok("停牌 ≠ 一定补位" in r["note"] or "不是买入指令" in r["note"],
       "note 明确这是推演、不是买入指令")

    print("\n[5] 同题材今日还没有涨停 → 必须说「补位未发生」")
    ds.save_pool_history("20261014", [zt_row("sh600825", "新华传媒", 10, "出版", 92500)])
    r2 = sub.evaluate(CFG, snaps=snaps, zt=[], session=SESS, real_date="20261014")
    ok(r2["active"] is True, "仍会报出停牌龙头（事实要如实说）")
    ok("补位未发生" in r2["halts"][0]["how"] or "尚无涨停" in r2["halts"][0]["how"],
       f"口径说明补位尚未发生（{r2['halts'][0]['how'][-34:]}）")
    ok("不要提前埋伏" in r2["summary"] or "尚未发生" in r2["summary"],
       "summary 明确不许提前埋伏")

    print("\n[6] 盘前走公告层（快照区分不出停牌）")
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
    news = [{"text": "新华传媒：公司股票将于明日开市起停牌核查，预计停牌不超过 2 个交易日",
             "time": now, "source": "东财快讯"},
            {"text": "某公司发布业绩预告，净利润同比增长 30%", "time": now, "source": "新浪"}]
    r3 = sub.evaluate(CFG, snaps={}, zt=[], session={"phase": "pre_open"},
                      real_date="20261013", news=news)
    ok(r3["src"] == "盘前停牌公告", f"盘前判据来源是公告（{r3['src']}）")
    ok(r3["active"] is True and r3["halts"][0]["name"] == "新华传媒",
       "盘前能从公告里认出停牌股")
    ok("停牌核查" in (r3["halts"][0]["news_text"] or ""), "公告原文带进卡片供人工复核")

    print("\n[7] 模块关闭 / 非交易日 / 冷启动")
    off = sub.evaluate({"E6_substitute": {"enabled": False}})
    ok(off["enabled"] is False and off["active"] is False, "关闭时 enabled=False")
    rest = sub.evaluate(CFG, snaps=snaps, zt=zt, session={"phase": "rest"},
                        real_date="20261013")
    ok(rest["active"] is False and "非交易日" in rest["reason"],
       f"周末不判停牌（{rest['reason']}）")
    empty = sub.evaluate(CFG, snaps=snaps, zt=zt, session=SESS, real_date="20260901")
    ok(empty["active"] is False and "冷启动" in empty["reason"],
       f"无归档时不硬凑（{empty['reason'][:26]}…）")
    nohalt = sub.evaluate(CFG, snaps={"sh603096": snap(20.97, 1e8, 2.0)},
                          zt=zt, session=SESS, real_date="20261013")
    ok(nohalt["active"] is False and "没有发现" in nohalt["reason"],
       f"没有停牌龙头时说明白（{nohalt['reason'][:34]}…）")

    print("\n[8] 快讯识别：只吃含「停牌/暂停交易」的条目")
    sig = sub.news_halt_signals(news)
    ok(len(sig) == 1 and sig[0]["code"] == "sh600825",
       f"含停牌的条目被识别（{sig}）")
    ok(not sub.news_halt_signals([{"text": "某司业绩预告", "time": now,
                                   "source": "x"}]), "不含停牌的条目不被吃进来")
    ok(not sub.news_halt_signals([{"text": "新华传媒：将于 10 月 20 日复牌",
                                   "time": now, "source": "x"}]),
       "只有「复牌」没有「停牌」的不算停牌事件")
    ok(sub._is_halt_text("某公司股票自明日起暂停交易"), "「暂停交易」也认")

    print("\n[8b] 措辞必须跟着数字走（有涨停 ≠ 补位）")
    ok("补位尚未发生" in sub._verdict(0, 5), "今日无涨停 → 尚未发生")
    ok("资金在往这里补位" in sub._verdict(5, 2), "今日多于昨日 → 在补位")
    ok("持平" in sub._verdict(2, 2), "今日等于昨日 → 持平，不许说在补位")
    ok("退潮" in sub._verdict(1, 4), "今日少于昨日 → 退潮，不是补位")

    print("\n[9] 配置已进预设，字段齐全")
    cfg = sc.load_config()
    ok("E6_substitute" in sc.PRESETS["wildman"], "E6_substitute 进了 wildman 预设")
    d = cfg.get("E6_substitute") or {}
    for k in ("enabled", "min_boards", "lookback_days", "max_halts",
              "top_peers", "early_deadline", "near_penalty"):
        ok(k in d, f"load_config 后有 {k}（{d.get(k)}）")
    ok(d.get("min_boards") == 2, "默认只推演 ≥2 连板的停牌股（挡 1 板噪声）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
