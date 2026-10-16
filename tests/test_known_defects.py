# -*- coding: utf-8 -*-
"""已知缺陷的回归护栏（用户 10-13：逐一分析并修复）。

  ⑧ 全量审计新增（2026-10-13）：三处「命名说 A、实现做 B」的窗口/取样错误 ——
     `indicators.count_limit_ups` 取成「最后 N 个涨停日」（界面显示「近20日涨停 4 次」）、
     `datasource.recent_amount_median` 取成「最大的 N 天」（基准被抬高）、
     `datasource._board_members_index` 对**已排序**名单直接 `[:1200]`
     （实测只剩沪+北、**深市为 0**，题材成分表整个失效）；另加连板计算的除零守卫。

逐条对应：

  ① `core/pipeline.py` —— 「低位 + 主力净流入刚转正」这条理由**从未产生过**：
     判断写在候选循环里，而 `p["moneyflow"]` 是循环**之后**的
     `flow_mod.evaluate()` 才写进去的 → `_main` 恒为 0；且单位是亿却按元 /1e8。
  ② `core/journal.py` —— 踩坑雷达 `_traps` 恒空：按 ("win","loss") 过滤，
     而 `_settle_one` 只写 open/settled/partial/skip，从来没有 win/loss。
  ③ `core/strategy_config.py` —— `E2_lhb` 缺 `top_n`，但 lhb.py 读它、
     配置面板也渲染它 → 输入框显示 `undefined`。
  ④ `web/index.html` + `core/strategy_config.py` —— `M11_swing.stall_pct` 默认值
     本身就是百分数（3.0），却用 `pctf`（data-pct，保存时 ÷100）渲染 →
     用户改一次就存成 0.03 → `abs(pct) > stall_pct` 恒真 → WS1 离场点几乎不触发。
  ⑤ `core/trading.py` —— B3 缩量红盘：盘中 `volume["amount"]` 恒为 None，
     `f"{None:.0f}"` 抛 TypeError → **打断整轮扫描**。
  ⑥ `core/trading.py` —— 关闭 M5 时 `position_plan` 返回缺 `target_total` /
     `chain_stage` → 前端 `undefined*100` → 显示 `NaN%`。
  ⑦ `core/cls.py` —— `sector_rotation()` 全库无调用点（死代码）→ 接入
     pipeline 与两端界面（前端渲染由 tests/test_known_defects.js 覆盖）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="knowndef_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import journal as journal_mod      # noqa: E402
from core import pipeline                    # noqa: E402
from core import strategy_config as sc       # noqa: E402
from core import trading                     # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def src(rel: str) -> str:
    with io.open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- ① 资金流理由

def test_moneyflow_reason():
    print("[1] pipeline：低位+主力净流入刚转正 —— 时序与单位")
    picks = [
        # 低位（pos 0.25）+ 主力净流入 +1.8 亿 → 应该补上理由
        {"code": "sh600000", "reasons": [],
         "graph_detail": {"base_pos": 0.25}, "moneyflow": {"main": 1.8}},
        # 位置偏高（0.8）→ 不补
        {"code": "sh600001", "reasons": [],
         "graph_detail": {"base_pos": 0.80}, "moneyflow": {"main": 2.0}},
        # 主力净流出 → 不补
        {"code": "sh600002", "reasons": [],
         "graph_detail": {"base_pos": 0.20}, "moneyflow": {"main": -0.5}},
        # 没取到资金流 → 不补、不崩
        {"code": "sh600003", "reasons": [], "graph_detail": {"base_pos": 0.10}},
    ]
    n = pipeline._annotate_moneyflow_reasons(picks)
    ok(n == 1, f"只补 1 条（实际 {n}）")
    r = picks[0]["reasons"][0]
    ok("低位（60日位置 25%）" in r, f"理由含位置：{r}")
    ok("+1.8 亿" in r, f"金额单位是**亿**、没被 /1e8（{r}）")
    ok(not picks[1]["reasons"] and not picks[2]["reasons"] and not picks[3]["reasons"],
       "位置高 / 净流出 / 无数据 都不补")
    # 缺 reasons 键也不能崩
    p5 = [{"code": "sh600004", "graph_detail": {"base_pos": 0.2},
           "moneyflow": {"main": 3.0}}]
    try:
        ok(pipeline._annotate_moneyflow_reasons(p5) == 1, "缺 reasons 键时自动创建")
    except Exception as e:                            # noqa: BLE001
        ok(False, f"缺 reasons 键抛异常：{type(e).__name__}: {e}")

    # ★ 静态顺序守卫：调用必须排在 flow_mod.evaluate 之后（回归的主要形态就是
    #   "又被挪回候选循环里" —— 那样 _main 恒为 0，理由再次消失）。
    s = src("core/pipeline.py")
    i_flow = s.find("flow = flow_mod.evaluate(")
    i_call = s.find("_annotate_moneyflow_reasons(picks)")
    ok(i_flow > 0 and i_call > 0, "两处调用都在源码里")
    ok(i_flow < i_call, f"资金流调用({i_flow}) 在补理由({i_call}) 之前")
    ok("_main/1e8" not in s, "不再对已是「亿」的金额做 /1e8")


# ---------------------------------------------------------------- ② 踩坑雷达

def test_traps():
    print("\n[2] journal：踩坑雷达不再恒空")
    def ent(i, status, ret1):
        return {"id": f"e{i}", "date": f"202610{i:02d}", "signal": "B1",
                "signal_name": "首板低吸", "status": status, "ret1": ret1}

    entries = [ent(i, "settled", -0.05) for i in range(1, 7)]     # 6 笔全亏
    tr = journal_mod._traps(entries)
    ok(len(tr) == 1, f"6 笔全亏 → 命中 1 类（实际 {len(tr)}）")
    ok(tr and tr[0]["recent_losses"] == 6, "亏损笔数统计正确")
    ok(tr and tr[0]["type"] == "B1", "信号类型带上")
    ok(tr and "反复踩同一个坑" not in tr[0]["note"] or tr and tr[0]["note"],
       "note 非空")
    # ★ 文案契约变了（v1.41.5）：旧文案一律写「反复踩同一个坑，先停用」，
    #   但实测「最近 8 笔亏几笔」里**大部分是当日大盘的账** —— 20261009-1016
    #   六个交易日有 4 天全市场均值为负（10-13 单日 -4.23%），那天闭着眼买
    #   什么都亏。现在 note 必须带**扣掉大盘后的超额**，并给出停用/不停用判定，
    #   否则会把被大盘拖累的信号（实测 B0 超额 +3.38%）一起错杀。
    ok(tr and ("超额" in tr[0]["note"]), "文案给出扣掉大盘后的超额（不再只报绝对亏损）")
    ok(tr and tr[0].get("verdict") in ("suspend", "market"),
       f"给出停用/该修判定：{tr[0].get('verdict') if tr else None}")
    # 这一组是「每天都跑输大盘」的构造：所有票同一天同收益 → 超额为 0，
    # 既不跑赢也不跑输，按阈值（负超额才算该停用）应落在 market。
    ok(tr and tr[0].get("verdict") == "market",
       "全部同收益 → 无超额劣势 → 判 market（不是无脑停用）")

    # partial（1/3 日已出、5 日未到）也要参与 —— 它有 ret1
    part = [ent(i, "partial", -0.04) for i in range(1, 6)]
    ok(len(journal_mod._traps(part)) == 1, "partial 状态也计入（ret1 已可算）")

    # skip（买不到/停牌）不参与
    ok(journal_mod._traps([ent(i, "skip", None) for i in range(1, 7)]) == [],
       "skip（买不到/停牌）不计入")
    # open（还没结算）不参与
    ok(journal_mod._traps([ent(i, "open", None) for i in range(1, 7)]) == [],
       "open（未结算）不计入")
    # 样本不足 5 笔不报
    ok(journal_mod._traps([ent(i, "settled", -0.05) for i in range(1, 5)]) == [],
       "不足 5 笔不报（不吓唬人）")
    # 赢多亏少不报
    mix = [ent(i, "settled", 0.03) for i in range(1, 5)] + \
          [ent(9, "settled", -0.01)]
    ok(journal_mod._traps(mix) == [], "亏损不足 5 笔不报")
    # 旧的 win/loss 口径必须彻底不再被**代码**依赖（注释里解释历史不算）
    body = src("core/journal.py").split("def _traps")[1].split("def stats")[0]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    ok('"win"' not in code and '"settled", "partial"' in code,
       "判定口径是 settled/partial，代码里不再依赖不存在的 win/loss")


# ---------------------------------------------------------------- ③ E2 配置键

def test_e2_top_n():
    print("\n[3] strategy_config：E2 补上 top_n（前端曾显示 undefined）")
    ok(sc.PRESETS["wildman"]["E2_lhb"].get("top_n") == 12,
       f'默认值有 top_n=12（{sc.PRESETS["wildman"]["E2_lhb"]}）')
    cfg = sc.load_config()
    ok(cfg["E2_lhb"].get("top_n") == 12, "load_config 后仍有 top_n（落盘/合并不丢）")
    s = src("web/index.html")
    ok("num(k,'top_n'" in s, "配置面板仍渲染这个字段")
    ok("m.top_n" in s, "面板取值 m.top_n —— 有默认值才不会是 undefined")


# ---------------------------------------------------------------- ④ stall_pct

def test_stall_pct():
    print("\n[4] stall_pct：渲染器改回 num + 还原被 ÷100 写坏的存档")
    s = src("web/index.html")
    ok("num(k,'stall_pct'" in s, "用 num 渲染（不带 data-pct）")
    ok("pctf(k,'stall_pct'" not in s, "不再用 pctf（保存会 ÷100）")

    # 迁移：被写坏的 0.03 → 3.0
    bad = {"M11_swing": {"stall_pct": 0.03}}
    ok(sc._fix_pct_fields(bad) is True, "识别出被 ÷100 写坏的值")
    ok(bad["M11_swing"]["stall_pct"] == 3.0, f'0.03 → 3.0（实际 {bad["M11_swing"]["stall_pct"]}）')
    good = {"M11_swing": {"stall_pct": 3.0}}
    ok(sc._fix_pct_fields(good) is False, "正常值不动")
    ok(good["M11_swing"]["stall_pct"] == 3.0, "正常值保持不变")
    # 用户自己填 0.5%（= 被写坏后的 0.005）→ 还原成 0.5，保留意图
    tiny = {"M11_swing": {"stall_pct": 0.005}}
    sc._fix_pct_fields(tiny)
    ok(abs(tiny["M11_swing"]["stall_pct"] - 0.5) < 1e-9, "用户填的 0.5% 也能正确还原")

    # 端到端：存档里的坏值经 load_config 修好并落盘
    p = sc.CONFIG_PATH
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump({"preset": "wildman", "M11_swing": {"stall_pct": 0.03}},
                  f, ensure_ascii=False)
    cfg = sc.load_config()
    ok(cfg["M11_swing"]["stall_pct"] == 3.0, "load_config 后已还原")
    with io.open(p, encoding="utf-8") as f:
        ok(json.load(f)["M11_swing"]["stall_pct"] == 3.0, "已落盘")


# ---------------------------------------------------------------- ⑤ B3 崩溃

def test_b3_amount_none():
    print("\n[5] trading：B3 盘中 amount=None 不再抛 TypeError")
    bars = [{"date": "2026-10-12", "open": 10.0, "close": 10.0,
             "high": 10.2, "low": 9.8, "volume": 1000.0}] * 30
    stock = {"code": "sh600000", "name": "测试股", "pct": 1.2, "price": 10.0}
    sentiment = {"stage": "probe", "can_trade": True}
    cfg = sc.PRESETS["wildman"]
    # 盘中：amount 是 None（engine 的既有契约），cum_amount 是事实值
    vol = {"mode": "shrink_red", "red_flag": "缩量红盘", "short_only": True,
           "amount": None, "cum_amount": 3000.0, "intraday": True}
    try:
        sigs = trading.buy_signals(stock, bars, sentiment, vol, cfg)
        ok(True, "盘中（amount=None）调用不抛异常")
    except TypeError as e:
        sigs = []
        ok(False, f"仍然抛 TypeError：{e}")
    b3 = [s for s in sigs if s.get("type") == "B3"]
    ok(bool(b3), f"B3 信号正常产出（{len(b3)} 条）")
    ok(b3 and "3000亿" in b3[0]["trigger"],
       f"盘中退回已成交额（{b3[0]['trigger'] if b3 else '—'}）")
    # 收盘后：amount 有值 → 用它
    vol2 = dict(vol, amount=3200.0, intraday=False)
    b3b = [s for s in trading.buy_signals(stock, bars, sentiment, vol2, cfg)
           if s.get("type") == "B3"]
    ok(b3b and "3200亿" in b3b[0]["trigger"], "收盘后用全天额")


# ---------------------------------------------------------------- ⑥ M5 关闭 NaN

def test_position_disabled():
    print("\n[6] trading：关闭 M5 时返回结构齐全（前端不再 NaN%）")
    cfg = {"M5_position": {"enabled": False}}
    plan = trading.position_plan(cfg, [], {"stage": "probe"}, {"mode": "peak_swing"}, {})
    ok("target_total" in plan, "有 target_total")
    ok("chain_stage" in plan, "有 chain_stage")
    ok(plan["target_total"] == 0.0, "target_total = 0.0")
    ok(plan["chain_stage"] == "disabled", f'chain_stage = "disabled"（{plan["chain_stage"]}）')
    ok(plan.get("chain") == [], "chain 为空列表")
    # 前端所有分支都必须拿到同形的键 —— 逐个对照正常分支
    normal = trading.position_plan(sc.PRESETS["wildman"], [],
                                   {"stage": "probe"}, {"mode": "peak_swing"}, {})
    for k in ("target_total", "chain_stage", "chain", "note"):
        ok(k in normal and k in plan, f"键 {k} 两边都有")
    s = src("web/index.html")
    ok("pos.target_total" in s, "前端确实读 pos.target_total（缺了就是 NaN%）")


# ---------------------------------------------------------------- ⑦ 板块轮动

def test_rotation():
    print("\n[7] 板块轮动：死代码已接入（后端）")
    s = src("core/pipeline.py")
    ok('sectors["rotation"] = cls_mod.sector_rotation(4)' in s,
       "pipeline 已调用 sector_rotation 并挂到 sectors")
    ok("sectors[\"rotation\"] = []" in s.replace("'", '"'),
       "抓取失败时留空列表（界面据此隐藏，不编造方向）")
    # 真的能取到数据（离线时返回 []，也不算失败）
    from core import cls as cls_mod
    try:
        rot = cls_mod.sector_rotation(4)
        ok(isinstance(rot, list), f"sector_rotation 返回列表（{len(rot)} 天）")
        if rot:
            ok(all("date" in d and "plates" in d for d in rot), "每项含 date / plates")
    except Exception as e:                            # noqa: BLE001
        ok(False, f"抛异常：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- ⑧ 窗口/取样语义

def test_window_semantics():
    """⑧ 三处「窗口/取样」语义错误（2026-10-13 全量审计）。

    共同特征：**命名说 A、实现做 B**，而且都直接反映到界面数字上。
    """
    import collections

    from core import datasource as ds
    from core import indicators as ind

    print("\n[8a] count_limit_ups：「近 N 日」不是「最后 N 个涨停日」")
    bars, px = [], 10.0
    for i in range(70):
        px *= 1.10 if i < 5 else 1.0          # 涨停只发生在最久远的第 1~5 根
        bars.append({"open": px * 0.99, "close": px, "high": px * 1.01,
                     "low": px * 0.98, "volume": 1000.0})
    ok(len(ind.count_limit_ups(bars, "sh600000", lookback=20)) == 0,
       "近 20 日无涨停 → 0（原来返回 4，界面跟着写「近20日涨停 4 次」并加分）")
    ok(len(ind.count_limit_ups(bars, "sh600000", lookback=4)) == 0,
       "窗口 4 日同样为 0")
    ok(len(ind.count_limit_ups(bars, "sh600000", lookback=69)) == 4,
       "窗口放大到 69 日才该看到那 4 次涨停")

    print("\n[8b] 连板/最大连板：收盘价为 0 时不得除零崩掉")
    zeros = [{"open": 1.0, "close": 0.0, "high": 1.0, "low": 0.0, "volume": 1.0}] * 5
    try:
        mb = ind.max_boards(zeros, "sh600000")
        cb = ind.consecutive_boards(zeros, "sh600000")
        ok(mb == 0 and cb == 0, f"返回 0 而不是抛异常（{mb}/{cb}）")
    except ZeroDivisionError:
        ok(False, "仍抛 ZeroDivisionError")

    print("\n[8c] recent_amount_median：「近 N 日」不是「最大的 N 天」")
    from core import paths
    amt_path = paths.data_path("market_amount.json")
    try:
        os.remove(amt_path)
    except OSError:
        pass
    for i in range(6):
        ds.save_market_amount(f"202609{i + 1:02d}", 25000.0, final=True)
    for i in range(6):
        ds.save_market_amount(f"202610{i + 1:02d}", 12000.0, final=True)
    got = ds.recent_amount_median(8)
    ok(abs(got - 12000.0) < 0.1,
       f"量能下台阶后取最近 8 天 → 12000（原实现取最大的 8 天 → 25000，"
       f"基准被抬高会把正常量能判成缩量；得到 {got}）")

    print("\n[8d] 板块成分抽样：排序名单直接切片会整段切掉一个市场")
    codes = (["bj" + f"{i:06d}" for i in range(600)]
             + ["sh" + f"{600000 + i}" for i in range(2400)]
             + ["sz" + f"{i:06d}" for i in range(3000)])
    picked = ds._sample_codes_by_market(codes, 1200)
    dist = collections.Counter(c[:2] for c in picked)
    old = collections.Counter(c[:2] for c in codes[:1200])
    ok(set(dist) == {"bj", "sh", "sz"},
       f"三个市场都有（旧切片 [:1200] 只有 {dict(old)}，深市恒为 0；"
       f"现在 {dict(dist)}）")
    ok(dist["sz"] > 500, f"深市占比接近全市场（深 {dist['sz']}/1200）")
    ok(len(picked) == 1200 and len(set(picked)) == 1200, "条数正确且无重复")
    ok(ds._sample_codes_by_market(codes, 99999) == codes, "limit ≥ 总数 → 原样返回")
    ok(ds._sample_codes_by_market([], 1200) == [], "空名单 → 空")


def main():
    test_moneyflow_reason()
    test_traps()
    test_e2_top_n()
    test_stall_pct()
    test_b3_amount_none()
    test_position_disabled()
    test_rotation()
    test_window_semantics()
    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
