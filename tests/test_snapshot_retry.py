# -*- coding: utf-8 -*-
"""行情快照取数的健壮性 —— 「自选股一会能查到一会查不到」的回归护栏。

背景（用户实测）：电脑版自选股点开详情，一会能查到、一会报"可能是停牌/已退市"。
实测 /api/stock 连打 20 次有 6 次误报。根因两处：

  ① `_get` 的坏包检测**只判复用连接**（`if reused and (not data or status != 200)`）：
     新建连接拿到空 body / 非 200 会被当成成功返回 → 解析出 0 条 → 调用方以为
     "这股票不存在"。长驻进程（看板服务）走代理出口时尤其容易踩到。
  ② `fetch_snapshots` 把整批传输失败静默吞掉（`except: return []`），下游无法区分
     「网络抖动」和「代码本身取不到」，只能一律报"停牌/已退市"，还引导用户去删自选。

修法：坏包检测对**所有**连接生效；`fetch_snapshots` 加一次兜底重试 + 用 `diag`
回填失败批次数，让 `/api/stock` 能如实区分两类失败。
"""
from __future__ import annotations

import http.client
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="snapretry_")

from core import datasource as ds   # noqa: E402
from core import pipeline           # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def tx_line(code="sh600000", name="测试股", price="10.00"):
    """造一行合法的腾讯 v_ 行情（_parse_tx_line 要求 >=50 段、有名称、价格>0）。"""
    parts = ["1"] * 55
    parts[1] = name
    parts[2] = code[2:]
    parts[3] = price
    return f'v_{code}="' + "~".join(parts) + '";'


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = body

    def read(self):
        return self._b


class _FakeConn:
    """假连接：模拟"新建连接拿到空 body / 非 200"。"""
    status = 200
    body = b""

    def __init__(self, *a, **k):
        self.sock = None
        self._via_proxy = False

    def set_tunnel(self, *a, **k):
        pass

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return _Resp(_FakeConn.status, _FakeConn.body)

    def close(self):
        pass


def main():
    real_get = ds._get
    real_fetch = ds.fetch_snapshots
    real_conn = http.client.HTTPSConnection

    print("[1] _get 必须校验**所有**连接的响应（不只复用连接）")
    ds._conn_pool.clear()
    try:
        http.client.HTTPSConnection = _FakeConn
        for status, body, label in ((200, b"", "空 body（200）"),
                                    (500, b"<html>oops</html>", "非 200（有 body）"),
                                    (403, b"", "403 空 body")):
            _FakeConn.status, _FakeConn.body = status, body
            ds._conn_pool.clear()
            try:
                ds._get("https://qt.gtimg.cn/q=sh600000", timeout=2)
                ok(False, f"{label} 应当抛错（原来会被当成成功返回）")
            except http.client.BadStatusLine:
                ok(True, f"{label} → BadStatusLine，会触发内部重试")
            except Exception as e:
                ok(False, f"{label} 抛了非预期异常 {type(e).__name__}: {e}")
    finally:
        http.client.HTTPSConnection = real_conn
        ds._conn_pool.clear()

    print("\n[2] fetch_snapshots：传输失败要重试，并把失败批次数记进 diag")
    n = {"c": 0}

    def always_fail(url, timeout=12, referer=""):
        n["c"] += 1
        raise OSError("模拟网络抖动")

    ds._get = always_fail
    try:
        diag = {}
        got = ds.fetch_snapshots(["sh600000"], batch=1, workers=1, diag=diag)
        ok(got == {}, "全失败 → 返回空")
        ok(diag.get("attempts") == 2, f"重试了一次（attempts={diag.get('attempts')}）")
        ok(diag.get("failed_batches") == 1,
           f"diag.failed_batches=1（调用方据此判定「传输失败」）")
    finally:
        ds._get = real_get

    print("\n[3] fetch_snapshots：第一次抖动、第二次成功 → 数据要救回来")
    n["c"] = 0

    def fail_then_ok(url, timeout=12, referer=""):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("模拟第一次抖动")
        return tx_line().encode()

    ds._get = fail_then_ok
    try:
        diag = {}
        got = ds.fetch_snapshots(["sh600000"], batch=1, workers=1, diag=diag)
        ok(bool(got.get("sh600000")), "重试后拿到行情（不再误报「取不到」）")
        ok(diag.get("failed_batches") == 0, "救回来的批次不计入失败")
    finally:
        ds._get = real_get

    print("\n[4] stock_detail：传输失败要说「网络抖动」，不能说「停牌/已退市」")
    def snap_transport_fail(codes, batch=80, workers=16, progress=None, diag=None):
        if diag is not None:
            diag["failed_batches"] = 1
            diag["attempts"] = 2
        return {}

    ds.fetch_snapshots = snap_transport_fail
    try:
        r = pipeline.stock_detail("sh600000")
        ok(r.get("ok") is False, "返回失败")
        ok(r.get("retryable") is True, "标记 retryable（前端据此自动重试）")
        msg = r.get("error", "")
        ok("停牌" not in msg and "退市" not in msg,
           f"不再误导成停牌/退市：{msg[:40]}…")
        ok("重试" in msg, "明确让用户点重试")
        ok("删掉这条自选" not in msg, "不再引导用户去删自选")
    finally:
        ds.fetch_snapshots = real_fetch

    print("\n[5] stock_detail：代码真取不到（传输正常）→ 才提停牌/代码错，且不标 retryable")
    def snap_no_data(codes, batch=80, workers=16, progress=None, diag=None):
        if diag is not None:
            diag["failed_batches"] = 0
            diag["attempts"] = 1
        return {}

    ds.fetch_snapshots = snap_no_data
    try:
        r = pipeline.stock_detail("sh600000")
        ok(r.get("ok") is False, "返回失败")
        ok(not r.get("retryable"), "不标 retryable（重试也没用）")
        ok("停牌" in r.get("error", ""), "如实提示停牌/已退市/代码前缀")
    finally:
        ds.fetch_snapshots = real_fetch

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
