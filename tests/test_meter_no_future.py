# -*- coding: utf-8 -*-
"""E1 温度曲线不得被未来日期污染（v1.41.2）。

## 为什么要有这个测试

温度曲线（`sentiment_history.json`）是**实盘序列**：按日期字符串排序后，
最后一个点就是图上的末端。任何用**未来日期或合成日期**调 `meter.build()`
的脚本 / 测试 / 回测，都会把一行永久插到曲线**末尾**。

实测事故（用户报「市场的情况和文字不符」）：
    `tests/test_data_readiness.py` 某历史版本用 `real_date="20261106"` 走
    `meter.build()`，写下 `{'date':'20261106','temperature':24}`。当天是
    2026-10-16，于是曲线末端变成：
        39(10-15) → 58(10-16) → 24(11-06)
    图上呈现**一天之内从 58° 暴跌到 24°**，而同屏标题写「温度 58°（较上日
    +19° 回暖）」—— 看图是断崖下跌、看字是回暖，直接矛盾。

## 本测试钉住的两道护栏

1. `_save_hist()` 拒绝**任何晚于今天**的日期落盘（并在 stderr 留警告，
   不静默吞掉 —— 否则下一个人还要重查一遍）。
2. `heat_series` 只输出**不晚于今天**的点，把历史库里已经躺着的脏行滤掉
   （护栏 1 只管新写入，旧脏行还得靠这道滤网擦掉）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 每个测试文件用独立临时目录，别碰真实 data/
_tmp = tempfile.mkdtemp(prefix="nofuture_")
os.environ["STOCK_DATA_DIR"] = _tmp

from core import sentiment_meter as sm   # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


ZT = [{"code": "sh600001", "name": "甲", "boards": 3, "first_time": 93000,
       "fund": 100, "amount": 1000}]
CFG = {"E1_meter": {"enabled": True}}


def _stub_net():
    """把会碰磁盘/网络的入口打桩，只留 _save_hist / _load_hist 走真实现。"""
    sm.ds.fetch_breadth = lambda *a, **k: {}
    sm.ds.fetch_yesterday_zt_pool = lambda *a, **k: []
    sm.ds.pool_history_dates = lambda *a, **k: []
    sm.stab.peek = lambda *a, **k: {"confirmed": False, "state": "calm",
                                    "hurt_area": 0.0, "note": ""}


def _hist_path() -> str:
    return os.path.join(_tmp, "sentiment_history.json")


def main():
    _stub_net()
    today = time.strftime("%Y%m%d")
    future = time.strftime("%Y", time.localtime()) + "1231"
    if future <= today:                      # 12-31 当天跑的话换个更远的
        future = str(int(time.strftime("%Y")) + 1) + "0101"

    print(f"[0] 前置：今天 = {today}，未来日期样本 = {future}")
    ok(future > today, "未来日期样本确实晚于今天")

    print("[1] 未来日期调 build() → 不落盘（护栏 1）")
    sm.build(ZT, [], [], {}, future, CFG)
    exists = os.path.exists(_hist_path())
    rows = json.loads(io.open(_hist_path(), encoding="utf-8").read()) if exists else []
    ok(not any((r.get("date") or "") == future for r in rows),
       f"未来日期 {future} 不得出现在曲线里（实际写入 {len(rows)} 行）")

    print("[2] 今天 / 过去日期 → 正常落盘（护栏不得误伤）")
    past = "20200102"
    sm.build(ZT, [], [], {}, past, CFG)
    sm.build(ZT, [], [], {}, today, CFG)
    rows = json.loads(io.open(_hist_path(), encoding="utf-8").read())
    dates = [r.get("date") for r in rows]
    ok(past in dates, f"过去日期 {past} 正常写入")
    ok(today in dates, f"今天 {today} 正常写入")
    ok(future not in dates, f"未来日期 {future} 仍不在（阈值是严格 >，不是 >=）")

    print("[3] 拒绝时必须留痕（stderr 警告，不静默吞）")
    buf = io.StringIO()
    _old = sys.stderr
    sys.stderr = buf
    try:
        sm.build(ZT, [], [], {}, future, CFG)
    finally:
        sys.stderr = _old
    warn = buf.getvalue()
    ok("拒绝" in warn and future in warn,
       f"stderr 出现拒绝警告（实际 {warn.strip()[:80]!r}）")

    print("[4] 已躺着的未来脏行 → heat_series 必须滤掉（护栏 2）")
    # 手工把一行未来日期塞进库，模拟护栏 1 上线前留下的脏数据
    rows = json.loads(io.open(_hist_path(), encoding="utf-8").read())
    rows.append({"date": future, "temperature": 24, "limit_up": 20,
                 "limit_down": 8, "broken": 14, "max_boards": 1,
                 "promote_rate": 0.167, "broken_ratio": 0.412, "src": "live"})
    rows.sort(key=lambda r: r.get("date") or "")
    io.open(_hist_path(), "w", encoding="utf-8", newline="\n").write(
        json.dumps(rows, ensure_ascii=False))
    m = sm.build(ZT, [], [], {}, today, CFG)
    hs = m["heat_series"]
    ok(not any(h["date"] == future for h in hs),
       f"heat_series 里没有未来日期（共 {len(hs)} 点）")
    ok(max(h["date"] for h in hs) <= today,
       f"heat_series 末端日期 {max(h['date'] for h in hs)} 不晚于今天 {today}")

    print("[5] 曲线末端必须是真实的一天，且温度与标题同源")
    last = hs[-1]
    ok(last["date"] == today,
       f"末端点 = 今天（{last['date']}），不再是幽灵行")
    ok(last["temperature"] == m["temperature"],
       f"末端温度 {last['temperature']}° == 标题温度 {m['temperature']}°"
       f"（看图与看字一致）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    sys.exit(1 if badc else 0)


if __name__ == "__main__":
    main()
