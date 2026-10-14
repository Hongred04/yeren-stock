# -*- coding: utf-8 -*-
"""两市成交额长序列（tests/test_amount.py）。

★ 断网时跳过：本模块的网络函数拿不到数据就返回 {} / []，
  不抛异常。所以这里主要测**解析口径**与**空数据兜底**，
  联网断言只在拿得到数据时才跑（拿不到不判失败 —— 否则测试
  会随网络状况红绿不定）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import amount  # noqa: E402

_p = _f = 0


def ok(name, cond):
    global _p, _f
    if cond:
        _p += 1
        print(f"  [OK] {name}")
    else:
        _f += 1
        print(f"  [XX] {name}")


def main():
    print("=" * 70)
    print("两市成交额序列（core/amount.py）")
    print("=" * 70)

    # §1 _ths_amounts 解析：喂假页面
    row = "20261013,3760.80,3822.11,3741.53,3800.86,48330591000,779302290000.00,1.010,,,0"
    fake = 'quotebridge_v6_line_hs_1A0001_01_last({"total":"8740","name":"上证指数","data":"%s"})' % row
    orig_urlopen = amount.urllib.request.urlopen

    class _R:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    amount.urllib.request.urlopen = lambda req, timeout=0: _R(fake.encode("gbk"))
    try:
        got = amount._ths_amounts("1A0001")
        ok("解析出 1 个日期", len(got) == 1)
        ok("日期键正确", "20261013" in got)
        # ★ 关键：取的是第 7 个字段（成交额），不是第 6 个（成交量手）
        ok("取成交额字段而非成交量", got.get("20261013") == 779302290000.00)
        ok("不是成交量 48330591000", got.get("20261013") != 48330591000)
    finally:
        amount.urllib.request.urlopen = orig_urlopen

    # §2 脏数据不抛异常
    amount.urllib.request.urlopen = lambda req, timeout=0: _R(b"garbage no parens")
    try:
        ok("无括号的响应返回空字典（不抛）", amount._ths_amounts("x") == {})
    finally:
        amount.urllib.request.urlopen = orig_urlopen

    amount.urllib.request.urlopen = lambda req, timeout=0: _R(b"f({bad json})")
    try:
        ok("坏 JSON 返回空字典（不抛）", amount._ths_amounts("x") == {})
    finally:
        amount.urllib.request.urlopen = orig_urlopen

    def _boom(req, timeout=0):
        raise OSError("network down")

    amount.urllib.request.urlopen = _boom
    try:
        ok("网络异常返回空字典（不抛）", amount._ths_amounts("x") == {})
        ok("two_market_series 网络异常返回 []", amount.two_market_series() == [])
        s = amount.summary()
        ok("summary 网络异常给 ok=False", s.get("ok") is False)
        ok("summary 失败时带原因", bool(s.get("reason")))
    finally:
        amount.urllib.request.urlopen = orig_urlopen

    # §3 短路：字段不足的行被跳过
    rows = ["20260101,1,2,3,4,5",                      # 只有 6 字段 → 跳过
            "20260102,1,2,3,4,5,6,7"]                  # 7 字段 → 收
    fake2 = ('f({"data":"%s"})' % ";".join(rows))
    amount.urllib.request.urlopen = lambda req, timeout=0: _R(fake2.encode("gbk"))
    try:
        got = amount._ths_amounts("x")
        ok("字段不足的行被跳过", len(got) == 1 and "20260102" in got)
    finally:
        amount.urllib.request.urlopen = orig_urlopen

    # §4 真网络（拿不到就跳过，不判失败）
    real = amount.two_market_series(10)
    if not real:
        print("  [--] 真网络不可用，跳过联网断言")
    else:
        ok("真网络至少回 5 天", len(real) >= 5)
        ok("升序排列", all(real[i]["date"] < real[i + 1]["date"]
                          for i in range(len(real) - 1)))
        ok("total = sh + sz", all(abs(r["total"] - (r["sh"] + r["sz"])) < 1
                                 for r in real))
        ok("yi = total/1e8", all(abs(r["yi"] - r["total"] / 1e8) < 1e-6
                                for r in real))
        # 两市成交额合理量级：几千亿到几万亿之间（元）
        ok("量级合理（千亿~十万亿）",
           all(1e11 < r["total"] < 1e13 for r in real))
        s = amount.summary()
        ok("summary ok=True", s.get("ok") is True)
        ok("summary 有 latest", isinstance(s.get("latest"), (int, float)))
        ok("summary 有分位", 0 <= (s.get("pct_rank") or 0) <= 100)
        ok("summary 距峰值 <= 0", (s.get("from_peak_pct") or 0) <= 0)

    print(f"\n===== {_p} 通过 / {_f} 失败 =====")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
