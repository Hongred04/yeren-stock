# -*- coding: utf-8 -*-
"""静默刷新链路回归 —— 「没变化啊还是昨天的数据」（用户 10-13）。

**根因**：`/api/live` 只读 `pipeline.get_cached()`，而它有 **3 分钟 TTL**
（`RESULT_TTL=180`）。过期后 `get_cached()` 返回 None →
`_live_view` 既拿不到 `result_ts`、也拿不到 `picks`（于是 `quotes` 也是空的）。

而前端 `silentRefresh()` 换新报告的条件正是：

    if(live.result_ts && _lastTs && String(live.result_ts)!==String(_lastTs))

`result_ts` 为 undefined → 条件**永远不成立** → 页面打开后就冻结在那一份报告上，
行情也不再更新。用户看到的就是「没变化、还是昨天的数据」。

**修法**：`_live_view` 回退到磁盘那份报告（`/api/result` 本来就是这么兜底的），
保证 `result_ts` / `picks` / `quotes` 始终有值；顺带给 `get_disk_report()`
加 mtime 记忆（两百多 KB 的 JSON 不能每分钟重新解析）。

另外桌面端原来**完全不处理 `stale`**（手机端有）—— 端出「上次结果」时既不提示
也不补扫，这里一并加上（由 tests/test_live_refresh.js 覆盖前端侧）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="liveref_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds      # noqa: E402
from core import pipeline              # noqa: E402
from core import trading               # noqa: E402
import server                          # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def write_report(ts: float, date: str = "20261013") -> None:
    rep = {
        "meta": {"date": date, "ts": ts, "time": "2026-10-13 09:58:54",
                 "version": "test", "scanned": 100, "elapsed": 1.0,
                 "preset": "wildman", "preset_name": "野人体系"},
        "config": {"name": "野人体系", "desc": ""},
        "picks": [{"code": "sh600519", "name": "贵州茅台", "price": 1288.0, "pct": 1.2},
                  {"code": "sz000001", "name": "平安银行", "price": 11.0, "pct": -0.5}],
        "sentiment": {"stage": "follow", "can_trade": True, "metrics": {}},
        "volume": {"mode": "peak_swing", "label": "峰值量能"},
        "discipline": {}, "meter": {"metrics": {}}, "macro": {}, "index": {},
        "sectors": {}, "flow": {}, "mainline": {}, "outlook": {}, "portfolio": {},
        "journal": {}, "history": {}, "earnings": {}, "message": {},
        "limit_up_pool": [], "macro_events": [], "intraday": {}, "volume_history": [],
        "data_health": {}, "position": {},
    }
    p = pipeline._disk_cache_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "report": rep}, f, ensure_ascii=False)
    pipeline._disk_memo = None


def main():
    TS = 1791856734.1140833
    write_report(TS)

    print("[1] 内存缓存为空时（3 分钟 TTL 过期的真实状态），磁盘回退必须生效")
    pipeline.invalidate()
    ok(pipeline.get_cached() is None, "get_cached() 已失效（模拟 TTL 过期）")
    disk = pipeline.get_disk_report()
    ok(disk is not None, "get_disk_report() 读到了落盘结果")
    ok(disk.get("stale") is True, "落盘结果标了 stale（前端据此提示「上次结果」）")
    ok((disk.get("meta") or {}).get("ts") == TS, "磁盘报告带 meta.ts")
    ok(len(disk.get("picks") or []) == 2, "磁盘报告带 picks")

    print("\n[2] get_disk_report 按 mtime 记忆（两百多 KB 不能每次重解析）")
    a = pipeline.get_disk_report()
    ok(a is pipeline.get_disk_report(), "同一文件两次调用返回同一对象（命中记忆）")
    write_report(TS + 1)
    b = pipeline.get_disk_report()
    ok(b is not a, "文件变了 → 重新读取")
    ok((b.get("meta") or {}).get("ts") == TS + 1, "读到的是新内容")
    write_report(TS)

    print("\n[3] /api/live 在缓存过期时仍给出 result_ts 与 quotes（端到端）")
    # 把网络调用换成固定值，测试不依赖外网
    ds.fetch_snapshots = lambda codes, **kw: {
        c: {"price": 10.0, "pct": 1.0, "turnover": 2.0,
            "volume_ratio": 1.5, "amount": 3.0} for c in codes}
    ds.fetch_limit_up_pool = lambda *a, **k: []
    ds.fetch_limit_down_pool = lambda *a, **k: []
    ds.fetch_broken_board_pool = lambda *a, **k: []
    ds.pool_qdate = lambda *a, **k: "20261013"
    trading.market_session = lambda *a, **k: {
        "phase": "morning", "label": "早盘", "tmin": 30, "is_trade_day": True}

    srv, _th = server.start_background(8798)
    time.sleep(0.6)

    def get(path, t=30):
        with urllib.request.urlopen("http://127.0.0.1:8798" + path, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        pipeline.invalidate()
        live = get("/api/live")
        ok(live.get("result_ts") is not None,
           f'缓存过期时 result_ts 仍有值（{live.get("result_ts")}）')
        ok(str(live.get("result_ts")) == str(TS), "result_ts == 报告的 meta.ts")
        ok(len(live.get("quotes") or {}) == 2,
           f'quotes 非空（{len(live.get("quotes") or {})} 条）')
        ok("sh600519" in (live.get("quotes") or {}), "候选股的行情在里面")

        res = get("/api/result?top=20&limit=260")
        ok(str(live.get("result_ts")) == str((res.get("meta") or {}).get("ts")),
           "★ 前端能据此判断「该换新报告了」（两者一致）")
        ok(res.get("stale") is True, "端出的是上次结果，带 stale 标记")
    finally:
        srv.shutdown()

    print("\n[4] 静态守卫：不能再退回「只读内存缓存」的写法")
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "server.py"), encoding="utf-8") as f:
        s = f.read()
    body = s.split("def _live_view")[1].split("def _portfolio_view")[0]
    ok("pipeline.get_disk_report()" in body,
       "_live_view 里有磁盘回退（只有 get_cached 会再次丢掉 result_ts）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
