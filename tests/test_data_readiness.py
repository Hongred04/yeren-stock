# -*- coding: utf-8 -*-
"""数据就绪闸门回归测试 —— 钉死用户报的顽固 bug（第三次复发的护栏）。

## 被钉的 bug
用户原话：「每次首次启动就数据还未获得齐全就说可以出手，过一会获得数据了就说不能出手」。

机制：冷启动瞬间三池（涨停/跌停/炸板）还没抓到，`evaluate_sentiment` 拿到的是
三个空列表 → total=0 → 亏钱面积 0、无任何退潮信号 → 判成「情绪平稳/可出手」，
界面先喊「可以出手」；几秒后真实数据到位，一看是退潮 → 又喊「不能出手」。
**根因是引擎把「没有数据」当成了「数据好」**，不是市场变了。

## 本测试防守的契约（T1 引擎层 / T2 呈现层）
  · 冷启动空池 → can_trade 必须 False，data_ready=False，not_ready_reason 非空；
  · 数据齐了但退潮 → 与既有口径一致（ebb / can_trade=False）；
  · 数据齐了且高潮 → 正常放行（不能为防 bug 而误伤正常放行）；
  · 三池健康、只有 breadth/index 降级 → 不阻断（降级 ≠ 没数据）；
  · 未就绪态 → position_plan 给不出正常仓位（target_total=0，不是 Day1/锁仓链）；
  · evaluate_sentiment 返回结构不得缺既有字段（前端依赖，T1 改坏就是 NaN/undefined）。

## 独立性
core/stage_stabilizer.py 是**按自然日持久化**的滞回状态（data/stage_state.json），
直接跨用例串状态 —— 不显式复位的话，用例 2（退潮）会把用例 3（高潮）带成退潮，
测试本身就成了"一会儿一变"的 flaky 测试。每个用例开头一律 `stab.reset(date)`。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import datasource as ds       # noqa: E402
from core import engine                 # noqa: E402
from core import stage_stabilizer as stab   # noqa: E402
from core import strategy_config        # noqa: E402
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


# ---------------------------------------------------------------- 造数据

def zt(n: int, boards: int = 1) -> list[dict]:
    """构造涨停池：n 只，前 boards 只给指定连板高度，其余首板。"""
    out = []
    for i in range(n):
        b = boards if i < boards else 1
        out.append({"code": f"sh60{i:04d}", "name": f"涨停{i}", "boards": b,
                    "sector": "测试题材", "pct": 10.0})
    return out


def flat(n: int, kind: str) -> list[dict]:
    return [{"code": f"sz00{i:04d}", "name": f"{kind}{i}", "pct": -10.0}
            for i in range(n)]


CFG = strategy_config.load_config()

# 供仓位用例使用的最小持仓候选（position_plan 只读 len(picks)）
PICKS = [{"code": f"sh60000{i}", "name": f"候选{i}", "pct": 9.9} for i in range(4)]
VOLUME_OK = {"mode": "peak_swing", "amount": 12000.0}
MACRO_OK = {"stage": "neutral"}


def sent(cfg, zt_pool, dt_pool, zb_pool, date, tmin=None):
    """统一入口：每次都显式复位滞回状态，保证用例可重复、顺序无关。"""
    stab.reset(date)
    return engine.evaluate_sentiment(cfg, zt_pool, dt_pool, zb_pool, [],
                                     real_date=date, tmin=tmin)


def main():
    # ------------------------------------------------------------ 用例 1
    print("[1] 冷启动空池 → 绝不可出手（本 bug 的核心回归点）")
    # 防守：三池全空 = 「还没抓到数据」，不是「今天没有跌停」。
    # 修复前这里判成 unknown/calm 且 can_trade=True → 首屏喊「可以出手」。
    s1 = sent(CFG, [], [], [], "20261101")
    ok(s1.get("can_trade") is False,
       f'空池 → can_trade 必须 False（实际 {s1.get("can_trade")!r}，'
       f'stage={s1.get("stage")!r}）')
    ok("data_ready" in s1,
       "返回体含 data_ready 字段（T1 就绪闸门）")
    ok(s1.get("data_ready") is False,
       f'空池 → data_ready 必须 False（实际 {s1.get("data_ready")!r}）')
    r1 = s1.get("not_ready_reason")
    ok(isinstance(r1, str) and r1.strip() != "",
       f"空池 → not_ready_reason 必须是非空字符串，说明为什么不能出手（实际 {r1!r}）")
    # 呈现层同源：note 不得出现"可以出手"式的正向文案
    ok("data_note" in s1, "返回体含 data_note 字段")

    # ------------------------------------------------------------ 用例 2
    print("[2] 数据到位·退潮 → 与既有口径一致（ebb / 不许出手）")
    # 防守：数据齐了之后不能因为闸门误伤 —— 真退潮仍要判退潮。
    # zt=20/dt=8/zb=14：亏钱面积 22/42=52% ≥30% 且跌停≥5 → 强信号直接确认退潮。
    s2 = sent(CFG, zt(20, boards=2), flat(8, "跌停"), flat(14, "炸板"), "20261102")
    ok(s2.get("stage") == "ebb",
       f'zt20/dt8/zb14 → stage 应为 "ebb"（实际 {s2.get("stage")!r}，'
       f'signals={s2.get("signals")}）')
    ok(s2.get("can_trade") is False,
       f'退潮 → can_trade 必须 False（实际 {s2.get("can_trade")!r}）')
    ok(s2.get("data_ready") is True,
       f'数据齐了 → data_ready 应为 True，退潮判断不能靠"挡数据"实现'
       f'（实际 {s2.get("data_ready")!r}）')

    # ------------------------------------------------------------ 用例 3
    print("[3] 数据到位·高潮 → 正常放行（防误伤）")
    # 防守：闸门不能把正常行情也拦住 —— zt=33(max_boards=5)/dt=12/zb=4，
    # 亏钱面积 16/49=33%... 注意：面积恰好 ≥30% 会触发退潮，故这里用
    # 与引擎口径一致的组合（跌停/涨停=0.36），引擎按信号给 ebb。
    s3 = sent(CFG, zt(33, boards=5), flat(12, "跌停"), flat(4, "炸板"), "20261103")
    ok(s3.get("stage") == "climax",
       f'zt33(max_boards=5)/dt12/zb4 → stage 应为 "climax"（实际 {s3.get("stage")!r}，'
       f'signals={s3.get("signals")}）')
    ok(s3.get("can_trade") is True,
       f'高潮 → can_trade 应为 True（实际 {s3.get("can_trade")!r}）')
    ok(s3.get("data_ready") is True,
       f'高潮 → data_ready 应为 True（实际 {s3.get("data_ready")!r}）')

    # ------------------------------------------------------------ 用例 4
    print("[4] 降级但不阻断：三池健康、仅 breadth/index 降级 → 仍放行")
    # 防守：降级（用的是旧快照/次口径）≠ 没数据。若把降级也当未就绪，
    # 行情每次网络抖动都会被拦成"不能出手"，又回到 bug 的另一端。
    ds.reset_health()
    try:
        ds._health_set("pool:ZT", True)
        ds._health_set("pool:DT", True)
        ds._health_set("pool:ZB", True)
        ds._health_set("breadth", False, "测试：涨跌家数降级")
        ds._health_set("index", False, "测试：指数行情降级")
        h = ds.data_health()
        ok(h["ok"] is False, f'health.ok 应为 False（实际 {h["ok"]}，{h["summary"]}）')
        ok(len(h["degraded"]) == 2,
           f'降级项应为 2 个（breadth/index），实际 {len(h["degraded"])}')
        s4 = sent(CFG, zt(33, boards=5), flat(12, "跌停"), flat(4, "炸板"), "20261104")
        ok(s4.get("data_ready") is True,
           f'仅外围降级 → data_ready 仍须 True（实际 {s4.get("data_ready")!r}，'
           f'not_ready_reason={s4.get("not_ready_reason")!r}）')
        ok(s4.get("can_trade") is True,
           f'仅外围降级 → 不阻断出手（实际 can_trade={s4.get("can_trade")!r}）')
        # 三池全挂 → 才该判未就绪
        ds.reset_health()
        ds._health_set("pool:ZT", False, "测试")
        ds._health_set("pool:DT", False, "测试")
        ds._health_set("pool:ZB", False, "测试")
        s4b = sent(CFG, [], [], [], "20261104")
        ok(s4b.get("data_ready") is False,
           f'三池全挂 + 空池 → data_ready 必须 False（实际 {s4b.get("data_ready")!r}）')
    finally:
        ds.reset_health()          # 收尾清理，别污染后续用例

    # ------------------------------------------------------------ 用例 5
    print("[5] 仓位闸门：未就绪 → 不给正常仓位；正常高潮 → 仓位 > 0")
    # 防守：即便前端漏渲染，仓位链也不能在"没数据"时给出 Day1 平铺/锁仓。
    stab.reset("20261105")
    s5 = engine.evaluate_sentiment(CFG, [], [], [], [], real_date="20261105")
    plan5 = trading.position_plan(CFG, picks=PICKS, sentiment=s5,
                                  volume=VOLUME_OK, macro=MACRO_OK)
    ok(plan5.get("target_total") == 0.0,
       f'未就绪 → target_total 必须 0.0（实际 {plan5.get("target_total")!r}，'
       f'chain_stage={plan5.get("chain_stage")!r}）')
    cs5 = str(plan5.get("chain_stage", ""))
    ok(cs5 not in ("Day1", "Day1 平铺", "锁仓", "probe", "climax", "follow"),
       f'未就绪 → chain_stage 不得是正常仓位阶段（实际 {cs5!r}）')
    stages5 = [str(x.get("stage", "")) for x in (plan5.get("chain") or [])]
    ok(not any("Day1" in x or "锁仓" in x for x in stages5),
       f'未就绪 → 仓位链不得出现 Day1/锁仓（实际 {stages5}）')

    s5b = sent(CFG, zt(33, boards=5), flat(12, "跌停"), flat(4, "炸板"), "20261106")
    plan5b = trading.position_plan(CFG, picks=PICKS, sentiment=s5b,
                                   volume=VOLUME_OK, macro=MACRO_OK)
    ok(plan5b.get("target_total", 0) > 0,
       f'正常高潮 + 有候选 → target_total 应 > 0，闸门不得误伤正常仓位'
       f'（实际 {plan5b.get("target_total")!r}，stage={s5b.get("stage")!r}）')

    # ------------------------------------------------------------ 用例 6
    print("[6] 字段完整性：返回结构不得缺前端依赖的键")
    # 防守：T1 加闸门时顺手删/改名既有字段 → 前端 undefined / NaN%。
    s6 = sent(CFG, zt(20, boards=2), flat(8, "跌停"), flat(14, "炸板"), "20261107")
    for k in ("stage", "label", "can_trade", "trial_only", "ebb_trial_weight",
              "max_new_position", "max_boards", "signals", "note", "stability",
              "raw_ebb", "metrics", "history"):
        ok(k in s6, f"返回体含既有字段 {k}")
    m6 = s6.get("metrics") or {}
    for k in ("limit_up", "limit_down", "broken", "hurt_area", "max_boards",
              "seal_rate"):
        ok(k in m6, f"metrics 含既有字段 {k}")
    ok(m6.get("limit_up") == 20 and m6.get("limit_down") == 8
       and m6.get("broken") == 14,
       f'metrics 计数如实反映传入的池子（实际 {m6}）')

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
