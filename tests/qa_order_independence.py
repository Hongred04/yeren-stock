# -*- coding: utf-8 -*-
"""顺序无关性验证器（T3-R1 辅助工具，非交付测试本体）。

## 为什么需要
`tests/test_cache_first_screen.py` 的 7 个用例共享三类**进程级/文件级全局态**：
  · core.datasource 的 `_health` 记账与 `_health_last_round_bad` 兜底；
  · core.stage_stabilizer 的按自然日持久化滞回状态（data/stage_state.json）；
  · core.pipeline 的内存结果缓存与 data/last_report.json 磁盘缓存。

上一轮就是在这里翻车：用例 2 的状态串进用例 3，测试自己变成 flaky。
光靠"正序跑一遍看着对"证明不了顺序无关 —— 必须**真的换序**跑。

## 做法
本文件把每个用例当作独立函数，按传入的 order 逐个执行，每执行完一个就
检查全局态是否已复位；正序、逆序、随机序各跑一遍，比对每个用例的
通过/失败计数是否完全一致。计数不一致 = 用例之间存在污染。

用法：
    python tests/qa_order_independence.py
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_cache_first_screen as T          # noqa: E402
from core import datasource as ds            # noqa: E402
from core import pipeline as pl              # noqa: E402

CASES = ["case1_same_day_cache", "case2_overnight_rejected",
         "case3_live_marker", "case4_backward_compat",
         "case5_readiness_gate", "case6_resolve_health",
         "case7_silent_replace"]


def _clean() -> None:
    """跑任何用例之前，把三类全局态强制恢复到"出厂状态"。"""
    T._deep_clean_health()
    ds._health_last_round_bad = []
    ds._health_last_round_date = ""
    ds._health_last_round_seen = 0
    ds._health_round_seen = False
    pl.invalidate()
    with pl._lock:
        pl._result_cache = {}
        pl._result_ts = 0
    pl._disk_memo = None


def _residue() -> list[str]:
    """用例跑完后不该留下的东西。返回残留清单（空 = 干净）。"""
    out = []
    if ds._health:
        out.append(f"_health 未清空：{list(ds._health)}")
    if ds._health_last_round_bad:
        out.append(f"上一轮降级兜底未清："
                   f"{[d.get('key') for d in ds._health_last_round_bad]}")
    with pl._lock:
        if pl._result_cache:
            out.append("pipeline 内存缓存未清空")
    return out


def run_case(name: str, quiet: bool = True) -> tuple[int, int, list[str]]:
    """单独跑一个用例，返回 (通过, 失败, 残留)。"""
    _clean()
    T.okc = T.badc = 0
    real_main = T.main
    # 用例本体的实现挂在 main() 里；这里按名字路由到单独的探针。
    fn = globals().get("_" + name)
    if fn is None:
        return (0, 0, [f"未知用例 {name}"])
    fn()
    return (T.okc, T.badc, _residue())


# ---------------------------------------------------------------- 用例探针
# 每个探针 = test_cache_first_screen.main() 里对应那一段，原样复制调用，
# 保证"换序验证"验的就是交付测试本身的行为。

def _case1_same_day_cache():
    import time
    from datetime import datetime
    with T.DiskCache() as dc, T.MemCache():
        try:
            T._deep_clean_health()
            ts = time.time()
            date = T._weekday(datetime.now())
            dc.write(T.make_report(date, ts), ts=ts)
            rep2 = pl.get_disk_report()
            T.ok(rep2 is not None, "同日缓存 → 读得回来")
            f = T._fields_of(rep2 or {})
            T.ok(f.get("data_source") in ("cache", "disk"),
                 f'同日缓存 → data_source（实际 {f.get("data_source")!r}）')
            T.ok(f.get("data_ts") is not None, "同日缓存 → 带 data_ts")
            T.ok(f.get("stale") is not True, "同日缓存 → 不得标 stale")
        finally:
            T._deep_clean_health()
def _case2_overnight_rejected():
    with T.DiskCache() as dc, T.MemCache():
        try:
            T._deep_clean_health()
            yday = T.prev_trading_day_str()
            ts = __import__("time").time() - 86400
            dc.write(T.make_report(yday, ts), ts=ts)
            rep2 = pl.get_disk_report()
            out = T._fields_of(rep2 or {}) if rep2 else {}
            if rep2 is None:
                out["cached"] = False
            got, ev = T._looks_like_wait_or_stale(out)
            T.ok(got, f"隔夜缓存 → 等待/过期信号（{ev}）")
            # 与交付测试同口径：判「不得带可执行的结论」，不是「不得带键」。
            sen = (rep2 or {}).get("sentiment") or {}
            T.ok(sen.get("can_trade") is not True,
                 f'隔夜缓存 → 不得说可以出手（实际 {sen.get("can_trade")!r}）')
            T.ok(sen.get("data_ready") is False,
                 f'隔夜缓存 → data_ready 须恰为 False（实际 {sen.get("data_ready")!r}）'
                 f'—— 前端 sen.data_ready===false 靠它打叉')
        finally:
            T._deep_clean_health()
def _case3_live_marker():
    with T.MemCache() as mc:
        try:
            T._deep_clean_health()
            from datetime import datetime
            mc.seed(T.make_report(T._weekday(datetime.now()), __import__("time").time()))
            got = pl.get_cached()
            T.ok(got is not None, "实时路径 → get_cached 拿得到")
            T.ok(T._fields_of(got or {}).get("data_source") == "live",
                 f'实时路径 → data_source == "live"'
                 f'（实际 {T._fields_of(got or {}).get("data_source")!r}）')
        finally:
            T._deep_clean_health()
def _case4_backward_compat():
    import time
    from datetime import datetime
    with T.DiskCache() as dc, T.MemCache():
        try:
            T._deep_clean_health()
            legacy = T.make_report(T._weekday(datetime.now()), time.time())
            legacy.pop("data_source", None)
            legacy.pop("data_ts", None)
            dc.write(legacy, ts=time.time())
            try:
                pl.get_disk_report()
                pl.get_cached()
                raised = None
            except Exception as e:                  # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
            T.ok(raised is None, f"旧结构 → 不抛异常（实际 {raised!r}）")
        finally:
            T._deep_clean_health()
def _case5_readiness_gate():
    try:
        T._deep_clean_health()
        T.stab.reset("20261120")
        s = T.engine.evaluate_sentiment(T.CFG, [], [], [], [], real_date="20261120")
        T.ok(s.get("data_ready") is False, "空池 → data_ready False")
        T.ok(s.get("can_trade") is False, "空池 → can_trade False")
        T.two_pools_healthy({"breadth": "t", "index": "t"})
        T.stab.reset("20261121")
        s2 = T.engine.evaluate_sentiment(T.CFG, T.zt(33, boards=5),
                                         T.flat(12, "跌停"), T.flat(4, "炸板"),
                                         [], real_date="20261121")
        T.ok(s2.get("data_ready") is True, "外围降级 → data_ready True")
        T.ok(s2.get("can_trade") is True, "外围降级 → can_trade True")
        plan = T.trading.position_plan(T.CFG, picks=T.PICKS, sentiment=s2,
                                       volume=T.VOLUME_OK, macro=T.MACRO_OK)
        T.ok(plan.get("target_total", 0) > 0, "高潮 → target_total > 0")
    finally:
        T._deep_clean_health()


def _case6_resolve_health():
    try:
        T._deep_clean_health()
        ds._health_set("pool:ZT", False, "测试")
        T._deep_clean_health()
        h = T.engine._resolve_health(33, 12, 4)
        keys = [str(d.get("key") or "") for d in (h.get("degraded") or [])]
        T.ok(not [k for k in keys if k.startswith("pool:")],
             f"reset 后不得含 pool:* 降级（实际 {keys}）")
    finally:
        T._deep_clean_health()


def _case7_silent_replace():
    import time
    from datetime import datetime
    with T.MemCache() as mc:
        try:
            T._deep_clean_health()
            d = T._weekday(datetime.now())
            mc.seed(T.make_report(d, time.time() - 300))
            first = pl.get_cached()
            fresh = T.make_report(d, time.time())
            fresh["data_source"] = "live"
            mc.seed(fresh)
            second = pl.get_cached()
            T.ok((second or {}).get("meta", {}).get("ts") !=
                 (first or {}).get("meta", {}).get("ts"), "刷新后 ts 变了")
            T.ok(T._fields_of(second or {}).get("data_source") == "live",
                 "刷新后 data_source=live")
        finally:
            T._deep_clean_health()
def main():
    print("=" * 62)
    print("顺序无关性验证：正序 / 逆序 / 随机序，逐用例计数必须一致")
    print("=" * 62)

    orders = {
        "正序  forward": list(CASES),
        "逆序  reverse": list(reversed(CASES)),
    }
    rnd = list(CASES)
    random.Random(20261015).shuffle(rnd)
    orders["随机  shuffle"] = rnd

    results = {}
    residue_any = False
    for label, order in orders.items():
        print(f"\n--- {label} ---")
        per = {}
        for name in order:
            p, f, res = run_case(name)
            per[name] = (p, f)
            mark = "干净" if not res else "残留!! " + "；".join(res)
            print(f"  {name:26s} 通过 {p:2d} / 失败 {f:2d}   [{mark}]")
            if res:
                residue_any = True
        results[label] = per

    # 比对：每个用例在三种顺序下的计数必须完全一致
    print("\n--- 一致性比对 ---")
    baseline = results["正序  forward"]
    consistent = True
    for label, per in results.items():
        for name in CASES:
            if per.get(name) != baseline.get(name):
                consistent = False
                print(f"  XX  {name} 在「{label}」下 {per.get(name)} "
                      f"≠ 正序 {baseline.get(name)}")
    if consistent:
        print("  OK  三种顺序下每个用例的 通过/失败 计数完全一致 → 顺序无关")
    if residue_any:
        print("  XX  有用例跑完留下全局态残留（可能污染别的用例）")
    else:
        print("  OK  每个用例跑完全局态均已复位（无残留）")

    total_pass = sum(v[0] for v in baseline.values())
    total_fail = sum(v[1] for v in baseline.values())
    print(f"\n合计（以正序为准）：{total_pass} 通过 / {total_fail} 失败")
    return 0 if (consistent and not residue_any) else 1


if __name__ == "__main__":
    raise SystemExit(main())
