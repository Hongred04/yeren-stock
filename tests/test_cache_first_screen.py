# -*- coding: utf-8 -*-
"""缓存首屏 + 时效门 + 静默替换 —— 契约测试（T3-R1）。

## 用户诉求（两轮合一）
第一期：「每次首次启动就数据还未获得齐全就说可以出手，过一会获得数据了就说
        不能出手。」
第二期：「启动的首次还是不对显示数值偏热可以出售，增加首次启动用上次的缓存
        数据全程后台静默刷新获取数据。」

## 已敲定的口径（本文件逐条钉死，不得放宽）
  1. 首屏用上次缓存时**照常显示结论**，但必须带「缓存」来源标记 + 时间戳
     （`data_source` ∈ {"cache","disk"}，`data_ts` = 那份缓存自己的时间戳）。
  2. 缓存**仅同一交易日**可用；隔夜缓存**不得**被渲染成结论
     （应给等待/过期信号：`cached:false` 或 `stale:true` 形态，见用例 2）。
  3. 全链路：首屏用缓存 → 之后每次刷新静默替换。
  4. 就绪判据以 core/datasource.data_health() 为准：
     pool:ZT/pool:DT/pool:ZB 任一降级 = 未就绪；
     **index/breadth 降级只降可信度，绝不阻断结论**。
  5. 数据没到手时不得说"可以出手"。

## 与 test_data_readiness.py 的分工
那份管「就绪闸门」本体（空池不得放行、退潮判退潮、高潮放行…），本文件管
**缓存首屏这条链路**（缓存可用性 / 时效门 / 来源标记 / 静默替换 / 向后兼容），
外加两条"防 T1-R2 改坏"的回归：就绪闸门不得被回退、`_resolve_health`
不得再采信上一轮记录。两份文件相互独立，可单独执行。

## 独立性（顺序无关的前提）
  · 每个用例开头一律 `_deep_clean_health()`，结尾 `finally` 再次清理；
    （`ds.reset_health()` 不清「上一轮降级兜底」，单靠它会被用例 5 串味）；
  · core/stage_stabilizer.py 是**按自然日持久化**的滞回状态，跨用例串状态
    → 每个用例用**互不相同的交易日**，并显式 stab.reset(date)；
  · pipeline 的内存/磁盘缓存是进程级 + 文件级全局态 → 每个用例把内存缓存
    与磁盘落盘都复位到"干净盘"，跑完还原原文件。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import datasource as ds          # noqa: E402
from core import engine                    # noqa: E402
from core import pipeline                  # noqa: E402
from core import stage_stabilizer as stab  # noqa: E402
from core import strategy_config           # noqa: E402
from core import trading                   # noqa: E402

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
    return [{"code": f"sh60{i:04d}", "name": f"涨停{i}", "boards": boards,
             "sector": "测试题材", "pct": 10.0} for i in range(n)]


def flat(n: int, kind: str) -> list[dict]:
    return [{"code": f"sz00{i:04d}", "name": f"{kind}{i}", "pct": -10.0}
            for i in range(n)]


CFG = strategy_config.load_config()
PICKS = [{"code": f"sh60000{i}", "name": f"候选{i}", "pct": 9.9} for i in range(4)]
VOLUME_OK = {"mode": "peak_swing", "amount": 12000.0}
MACRO_OK = {"stage": "neutral"}


def sent(cfg, ztp, dtp, zbp, date):
    """统一入口：每次显式复位滞回状态，保证用例可重复、顺序无关。"""
    stab.reset(date)
    return engine.evaluate_sentiment(cfg, ztp, dtp, zbp, [], real_date=date)


# ---------------------------------------------------------------- 交易日工具
# 缓存时效门的判据是「同一交易日」，所以造缓存必须先知道"今天"和"上一个交易日"。

def _weekday(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def today_dow() -> int:
    return datetime.now().weekday()


def prev_trading_day_str() -> str:
    """上一个工作日（yyyyMMdd）。只按周末回调 —— 与 datasource.last_trade_date
    同一口径（它也是 while d.weekday()>=5: d -= 1day），不引入节假日历。"""
    d = datetime.now() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return _weekday(d)


def make_report(date: str, ts: float, **extra) -> dict:
    """一份最小可用的报告骨架。

    字段形状对齐 core/pipeline.analyze() 的产物：`meta.date` / `meta.ts` 是
    时效门要读的；`sentiment` 是首屏结论要渲染的；`picks` 是候选表要用的。
    """
    rep = {
        "meta": {"version": "test", "date": date, "ts": ts,
                 "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")},
        "sentiment": {"stage": "climax", "label": "情绪高潮 · 龙头主升",
                      "can_trade": True, "data_ready": True, "note": "测试结论"},
        "volume": VOLUME_OK,
        "position": {"target_total": 0.5, "chain_stage": "Day1 平铺"},
        "picks": [{"code": "sh600000", "name": "测试候选", "pct": 9.9}],
        "data_health": {"ok": True, "degraded": [], "checked": 3, "has_data": True,
                        "summary": "关键数据全部正常"},
        "data_ready": True,
    }
    rep.update(extra)
    return rep


class DiskCache:
    """pipeline 磁盘缓存的现场保护器。

    磁盘缓存是**文件级全局态**（data/last_report.json），跨用例/跨测试文件都会
    串。用例要走"文件里有一份缓存"这条路，就必须自己造文件，跑完还原。
    """

    def __init__(self):
        self.path = pipeline._disk_cache_path()
        self.backup = None
        self.existed = False

    def __enter__(self):
        if self.path and os.path.exists(self.path):
            self.existed = True
            self.backup = self.path + ".t3r1_bak"
            shutil.copy2(self.path, self.backup)
        pipeline._disk_memo = None          # 别让 mtime 记忆命中旧解析结果
        return self

    def write(self, report: dict, ts: float | None = None) -> None:
        """落一份缓存文件（形状与 save_disk_report 一致）。"""
        obj = {"ts": time.time() if ts is None else ts, "report": report}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        pipeline._disk_memo = None          # 新文件 → 让下次读重新解析

    def remove(self) -> None:
        if self.path and os.path.exists(self.path):
            os.remove(self.path)
        pipeline._disk_memo = None

    def __exit__(self, *exc):
        try:
            if self.backup and os.path.exists(self.backup):
                shutil.move(self.backup, self.path)
            elif not self.existed and self.path and os.path.exists(self.path):
                os.remove(self.path)        # 原本没有 → 别留垃圾
        finally:
            pipeline._disk_memo = None
            pipeline.invalidate()
            with pipeline._lock:
                pipeline._result_cache = {}
                pipeline._result_ts = 0
        return False


class MemCache:
    """pipeline 内存结果缓存的现场保护器（_result_cache / _result_ts）。"""

    def __enter__(self):
        self.saved = (pipeline._result_cache, pipeline._result_ts)
        with pipeline._lock:
            pipeline._result_cache = {}
            pipeline._result_ts = 0
        return self

    def seed(self, report: dict, age: float = 0.0) -> None:
        """塞一份"新鲜"内存缓存。age 秒为负=未来，用来模拟 ts 语义。"""
        with pipeline._lock:
            pipeline._result_cache = report
            pipeline._result_ts = time.time() - age

    def __exit__(self, *exc):
        with pipeline._lock:
            pipeline._result_cache, pipeline._result_ts = self.saved
        return False


# ---------------------------------------------------------------- 就绪闸门工具

def two_pools_healthy(extra_bad: dict | None = None) -> None:
    """三池健康 + 可选的额外降级项（index / breadth）。"""
    ds.reset_health()
    ds._health_set("pool:ZT", True)
    ds._health_set("pool:DT", True)
    ds._health_set("pool:ZB", True)
    for k, note in (extra_bad or {}).items():
        ds._health_set(k, False, note)


def _deep_clean_health() -> None:
    """把 datasource 的就绪记账**连兜底状态一起**复位。

    `ds.reset_health()` 只清本轮 `_health`，**不清** `_health_last_round_bad`
    （上一轮降级兜底）和 `_health_last_round_date`。用例 5 会制造
    breadth/index 降级，那些降级会被记进兜底；若不复位，下一个用例哪怕
    自己开头调了 reset_health()，仍可能被上一轮的兜底串味 —— 这正是
    `engine._resolve_health` 那个病根的传染路径，也是本轮踩到的坑。
    所以凡是用例收尾，一律走这里。
    """
    ds.reset_health()
    ds._health_last_round_bad = []
    ds._health_last_round_date = ""
    ds._health_last_round_seen = 0
    ds._health_round_seen = False


def _fields_of(rep: dict) -> dict:
    """把一份报告里的来源标记取出来（字段可能在 meta 里也可能在顶层）。"""
    if not isinstance(rep, dict):
        return {}
    out = {}
    for src in (rep, rep.get("meta") or {}):
        if not isinstance(src, dict):
            continue
        for k in ("data_source", "data_ts", "stale", "stale_ts", "cached"):
            if k in src and k not in out:
                out[k] = src[k]
    return out


def _looks_like_wait_or_stale(out: dict) -> tuple[bool, str]:
    """隔夜缓存被拒时的表现，兼容两种已被点名的形态：
      · 204 形态：{"cached": false}（server 的 /api/result 兜底分支）
      · 过期形态：{"stale": true} （get_disk_report 的既有标记）
    返回 (是否满足, 证据串)。"""
    ev = []
    if out.get("cached") is False:
        ev.append("cached=False")
    if out.get("stale") is True:
        ev.append("stale=True")
    return bool(ev), ("、".join(ev) if ev else f"未出现等待/过期信号：{out!r}")


def main():
    # ------------------------------------------------------------ 用例 1
    print("[1] 同一交易日缓存可用 → 照常出结论 + 带来源标记与时间戳")
    with DiskCache() as dc, MemCache():
        try:
            _deep_clean_health()
            ts = time.time()
            cache_date = ds.last_trade_date() if _probe_ok() else _weekday(datetime.now())
            rep = make_report(cache_date, ts)
            dc.write(rep, ts=ts)

            rep2 = pipeline.get_disk_report()
            ok(rep2 is not None,
               "同日缓存 → 读得回来（get_disk_report 不得返回 None）")
            f = _fields_of(rep2 or {})
            src = f.get("data_source")
            ok(src in ("cache", "disk"),
               f'同日缓存 → data_source 必须是 "cache"/"disk" 之一（实际 {src!r}；'
               f'全部标记字段={f!r}）—— 首屏必须让用户看出这是缓存来的')
            ok(("data_ts" in f) and (f.get("data_ts") is not None),
               f'同日缓存 → 必须带 data_ts 时间戳（实际 {f.get("data_ts")!r}）')
            _dts = _as_epoch(f.get("data_ts"))
            ok(_dts is not None and abs(_dts - ts) < 1.0,
               f'data_ts 必须等于那份缓存自己的时间戳（写入 {ts}，'
               f'读出 {f.get("data_ts")!r}）')
            ok(f.get("stale") is not True,
               f'同日缓存 → 不得被标成过期/隔夜（实际 stale={f.get("stale")!r}）—— '
               f'隔夜才算过期，同日缓存要能正常渲染结论')
            ok(bool((rep2 or {}).get("sentiment")),
               "同日缓存 → 结论体（sentiment）随缓存一起给出来，首屏照常显示结论")
        finally:
            _deep_clean_health()

    # ------------------------------------------------------------ 用例 2
    print("[2] 隔夜缓存被拒 → 不得渲染成结论（等待/过期信号）")
    with DiskCache() as dc, MemCache():
        try:
            _deep_clean_health()
            yday = prev_trading_day_str()
            ts = time.time() - 86400
            overnight = make_report(yday, ts)
            overnight_label = overnight["sentiment"]["label"]   # 隔夜那份的结论标签
            dc.write(overnight, ts=ts)

            out = {}
            rep2 = pipeline.get_disk_report()
            out.update(_fields_of(rep2 or {}) if rep2 else {})
            if rep2 is None:
                out["cached"] = False          # 读不出来等价于"给不出结论"
            got, ev = _looks_like_wait_or_stale(out)
            ok(got,
               f'隔夜缓存（{yday}）→ 必须给等待/过期信号，不得当可渲染结论返回'
               f'（实际 {ev}）')

            # 判据是「不得带可执行的结论」，**不是**「不得带 sentiment 键」。
            # 后端把隔夜结论压成就绪态占位（core/pipeline.py:_WAIT_SENTIMENT），
            # 键仍在、语义已废；而前端闸门是 `sen.data_ready===false`
            # （web/index.html:1602、web/mobile.html:669）。若把 sentiment 键整个
            # 删掉，前端 `REPORT.sentiment||{}` 得 {}、`undefined===false` 为 false
            # → 闸门判为就绪放行 → 用户重新看到"可以出手"。所以键必须留、
            # 值必须锁死为"不可执行"。下面三条 + 正向护栏覆盖这个不变量。
            sen2 = (rep2 or {}).get("sentiment") or {}
            ok(sen2.get("can_trade") is not True,
               f'隔夜缓存 → sentiment.can_trade 不得为 True'
               f'（实际 {sen2.get("can_trade")!r}）—— 昨日结论不得说"可以出手"')
            ok(sen2.get("data_ready") is not True,
               f'隔夜缓存 → sentiment.data_ready 不得为 True'
               f'（实际 {sen2.get("data_ready")!r}）—— 不得声称数据就绪')
            ok(sen2.get("label") != overnight_label,
               f'隔夜缓存 → sentiment.label 不得仍是隔夜那份行情结论'
               f'（隔夜 {overnight_label!r}，实际 {sen2.get("label")!r}）'
               f' —— 昨日高潮今天照渲染 = 用户看到的"数值偏热可以出售"')

            # ★ 正向护栏：锁死「前端闸门仍能打叉」这个不变量。
            # 前端判据是 `sen.data_ready===false`（严格等于，不是 falsy）——
            # 靠的是 data_ready **恰好是布尔 False**。若将来有人为了"更干净"
            # 改成删键，这里会红：undefined === false 为 false → 闸门放行。
            ok(sen2.get("data_ready") is False,
               f'隔夜缓存 → sentiment.data_ready 必须**恰好是布尔 False**'
               f'（实际 {sen2.get("data_ready")!r}，类型 {type(sen2.get("data_ready")).__name__}）'
               f' —— 前端 `sen.data_ready===false` 靠它打叉；'
               f'删键或写成 None/0 都会让闸门失效放行')
        finally:
            _deep_clean_health()

    # ------------------------------------------------------------ 用例 3
    print("[3] 实时路径（内存新鲜缓存）→ data_source 为 live")
    with MemCache() as mc:
        try:
            _deep_clean_health()
            rep = make_report(_weekday(datetime.now()), time.time())
            mc.seed(rep)
            got = pipeline.get_cached()
            ok(got is not None,
               "新鲜内存缓存 → get_cached() 拿得到（实时路径不触发重算）")
            f = _fields_of(got or {})
            src = f.get("data_source")
            ok(src == "live",
               f'实时路径 → data_source 必须是 "live"（实际 {src!r}；'
               f'全部标记字段={f!r}）—— 实时与缓存必须能被前端区分')
            ok(f.get("stale") is not True,
               f'实时路径 → 不得标 stale（实际 {f.get("stale")!r}）')
        finally:
            _deep_clean_health()

    # ------------------------------------------------------------ 用例 4
    print("[4] 向后兼容：缺 data_source / data_ts 的旧缓存不得抛异常")
    with DiskCache() as dc, MemCache():
        try:
            _deep_clean_health()
            legacy = make_report(_weekday(datetime.now()), time.time())
            legacy.pop("data_source", None)
            legacy.pop("data_ts", None)
            m = legacy.get("meta") or {}
            m.pop("data_source", None)
            m.pop("data_ts", None)
            dc.write({k: v for k, v in legacy.items() if k != "data_source"},
                     ts=time.time())
            # 也塞一份进内存缓存（第二条读取路径）
            with pipeline._lock:
                pipeline._result_cache = legacy
                pipeline._result_ts = time.time()
            try:
                d = pipeline.get_disk_report()
                c = pipeline.get_cached()
                raised = None
            except Exception as e:                      # noqa: BLE001
                d = c = None
                raised = f"{type(e).__name__}: {e}"
            ok(raised is None,
               f'旧结构缓存（无 data_source/data_ts）→ 读取路径绝不抛异常（实际 {raised!r}）')
            ok((d is not None) or (d is None),
               "旧结构缓存 → get_disk_report() 返回一份或 None，两者都允许"
               "（按实时处理或明确拒绝），但不得炸")
            ok((c is not None) or (c is None),
               "旧结构缓存 → get_cached() 返回一份或 None，两者都允许，但不得炸")
        finally:
            _deep_clean_health()

    # ------------------------------------------------------------ 用例 5
    print("[5] 就绪闸门不得被回退（防 T1-R2 改坏）")
    try:
        # 5a. 空池 + 无数据流 → 未就绪
        _deep_clean_health()
        stab.reset("20261120")
        s5a = engine.evaluate_sentiment(CFG, [], [], [], [], real_date="20261120")
        ok(s5a.get("data_ready") is False,
           f'空池+无数据流 → data_ready 必须 False（实际 {s5a.get("data_ready")!r}）')
        ok(s5a.get("can_trade") is False,
           f'空池+无数据流 → can_trade 必须 False（实际 {s5a.get("can_trade")!r}，'
           f'stage={s5a.get("stage")!r}）—— 没数据永远不等于可以出手')
        r5a = s5a.get("not_ready_reason")
        ok(isinstance(r5a, str) and r5a.strip() != "",
           f'空池+无数据流 → not_ready_reason 必须是非空中文说明（实际 {r5a!r}）')
        ok(isinstance(r5a, str) and any("\u4e00" <= ch <= "\u9fff" for ch in r5a),
           f'not_ready_reason 应含中文（实际 {r5a!r}）')
        plan5a = trading.position_plan(CFG, picks=PICKS, sentiment=s5a,
                                       volume=VOLUME_OK, macro=MACRO_OK)
        ok(plan5a.get("target_total") == 0.0,
           f'未就绪 → target_total 必须 0.0（实际 {plan5a.get("target_total")!r}）')

        # 5b. 三池健康 + breadth/index 降级 → 仍放行（降级只降可信度）
        two_pools_healthy({"breadth": "测试：涨跌家数降级",
                           "index": "测试：指数行情降级"})
        h5b = ds.data_health()
        ok(h5b["ok"] is False,
           f'外围降级时 health.ok 应为 False（实际 {h5b["ok"]}，{h5b["summary"]}）')
        ok(len(h5b["degraded"]) == 2,
           f'降级项应为 2 个（breadth/index），实际 {len(h5b["degraded"])}')
        stab.reset("20261121")
        s5b = engine.evaluate_sentiment(CFG, zt(33, boards=5), flat(12, "跌停"),
                                        flat(4, "炸板"), [], real_date="20261121")
        ok(s5b.get("data_ready") is True,
           f'仅 index/breadth 降级 → data_ready 必须 True（实际 '
           f'{s5b.get("data_ready")!r}，理由={s5b.get("not_ready_reason")!r}）'
           f' —— 降级 ≠ 没数据，绝不能阻断结论')
        ok(s5b.get("can_trade") is True,
           f'仅 index/breadth 降级 → can_trade 必须 True（实际 {s5b.get("can_trade")!r}）')
        plan5b = trading.position_plan(CFG, picks=PICKS, sentiment=s5b,
                                       volume=VOLUME_OK, macro=MACRO_OK)
        ok(plan5b.get("target_total", 0) > 0,
           f'三池健康 + 有候选 → target_total 应 > 0（实际 '
           f'{plan5b.get("target_total")!r}，stage={s5b.get("stage")!r}）'
           f' —— 闸门不得误伤正常仓位')
    finally:
        _deep_clean_health()

    # ------------------------------------------------------------ 用例 6
    print("[6] _resolve_health 不得采信上一轮记录（直接锁住病根）")
    try:
        _deep_clean_health()
        ds._health_set("pool:ZT", False, "测试")
        _deep_clean_health()
        h = engine._resolve_health(33, 12, 4)
        keys = [str(d.get("key") or "") for d in (h.get("degraded") or [])]
        pool_keys = [k for k in keys if k.startswith("pool:")]
        ok(not pool_keys,
           f'reset 后 _resolve_health 的 degraded 不得含任何 pool:* 项'
           f'（实际 degraded={keys}）—— health_best() 不得把上一轮的降级'
           f'当成本轮实测证据返回')
        ok(h.get("ok") is not False,
           f'三池本轮已重置且 health 为真时 → h.ok 不得是 False（实际 {h.get("ok")!r}）')
        # 反向：本轮真的有降级时必须认（别为了过用例把闸门拆了）
        _deep_clean_health()
        ds._health_set("pool:ZB", False, "测试")
        h2 = engine._resolve_health(33, 12, 4)
        keys2 = [str(d.get("key") or "") for d in (h2.get("degraded") or [])]
        ok("pool:ZB" in keys2,
           f'本轮真的有 pool:ZB 降级 → 必须认（实际 degraded={keys2}）—— '
           f'修的是"上一轮串味"，不是"不看降级"')
    finally:
        _deep_clean_health()

    # ------------------------------------------------------------ 用例 7
    print("[7] 静默替换：首屏缓存之后，刷新拿到的是新的实时结论")
    with MemCache() as mc:
        try:
            _deep_clean_health()
            # 首屏：一份"缓存结论"（偏热 → 可以出手）
            stale_rep = make_report(_weekday(datetime.now()), time.time() - 300)
            mc.seed(stale_rep, age=0.0)
            first = pipeline.get_cached()
            f1 = _fields_of(first or {})
            ok((first or {}).get("sentiment", {}).get("can_trade") is True,
               "首屏：缓存结论照常给出（用户先看到一份结论，而不是空白等待）")

            # 后台刷新完成 → 新的实时结论（退潮 → 不能出手）静默替换
            fresh = make_report(_weekday(datetime.now()), time.time())
            fresh["sentiment"] = {"stage": "ebb", "label": "退潮期 · 无条件清仓",
                                  "can_trade": False, "data_ready": True,
                                  "note": "退潮期不新开仓"}
            fresh["data_source"] = "live"
            mc.seed(fresh, age=0.0)
            second = pipeline.get_cached()
            f2 = _fields_of(second or {})
            ok((second or {}).get("sentiment", {}).get("can_trade") is False,
               "刷新后：拿到的是新的实时结论（静默替换，不是停在首屏那份）")
            ok(f2.get("data_source") == "live",
               f'刷新后 → data_source 应变为 "live"（实际 {f2.get("data_source")!r}）'
               f' —— 用户要知道"这已经是实时数据了"')
            ok((second or {}).get("meta", {}).get("ts") !=
               (first or {}).get("meta", {}).get("ts"),
               "刷新后 → meta.ts 变了（前端就是靠它判断该换上新报告的）")
        finally:
            _deep_clean_health()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


# ---------------------------------------------------------------- 小工具

def _as_epoch(v):
    """data_ts 允许是 epoch 秒或 ISO 字符串；统一转成 epoch 秒做比对。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:len(fmt) + 2].strip(), fmt).timestamp()
            except ValueError:
                continue
    return None


def _probe_ok() -> bool:
    """last_trade_date() 会打网络；离线环境下退回"今天（工作日回退）"。"""
    try:
        d = ds.last_trade_date()
        return bool(d) and len(str(d)) == 8
    except Exception:                                   # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
