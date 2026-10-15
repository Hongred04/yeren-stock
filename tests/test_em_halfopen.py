# -*- coding: utf-8 -*-
"""东财接口级「半开探测」自测（v1.40.0）。

背景：家族级早有半开探测（_push2_gate / PUSH2_PROBE_AFTER），接口级一直缺 ——
冷却期内 _push2_json 直接 return {}，于是"接口级封顶 5 分钟"是硬等满 5 分钟。
东财的掐是间歇性的，隔几十秒重试就可能恢复，硬等纯属浪费。

本测试**不发任何真实请求**，只验证闸门与冷却账本的纯逻辑：
  1. 冷却中，未到探测窗口 → 不放行
  2. 冷却中，到了探测窗口 → 放行一次，且**同一窗口内不会连续放行**
  3. 探测放行后计时刷新，下一个窗口之前不再放行
  4. 冷却已过 → 正常放行，且探测计时被清掉
  5. 探测失败 → 冷却重算为封顶值（而不是被退避逻辑越顶越长）
  6. em_blocked() 报出 next_probe_in；em_reset_blocks() 把探测账本也清干净
"""
import sys
import time
import importlib

sys.path.insert(0, __file__.rsplit("tests", 1)[0].rstrip("\\/"))

from core import datasource as ds


def ok(cond, msg):
    if cond:
        print(f"  [OK]   {msg}")
    else:
        print(f"  [FAIL] {msg}")
        raise AssertionError(msg)


def reset():
    ds.em_reset_blocks()
    with ds._em_lock:
        ds._em_probe.clear()


print("=== 1. 冷却中、未到探测窗口 → 不放行 ===")
reset()
key = "/api/qt/clist/get"
with ds._em_lock:
    ds._em_down_until[key] = time.time() + 300
    ds._em_fail[key] = 3
    ds._em_probe[key] = time.time()          # 刚刚探过
ok(ds._em_probe_allow(key) is False, "刚探过 → 本窗口内不再放行")


print("\n=== 2. 冷却中、到了探测窗口 → 放行一次 ===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() + 300
    ds._em_fail[key] = 3
    ds._em_probe[key] = time.time() - (ds.EM_PROBE_AFTER + 1)   # 上次探测已过期
ok(ds._em_probe_allow(key) is True, "距上次探测 > EM_PROBE_AFTER → 放行")
ok(ds._em_probe_allow(key) is False, "放行后立刻再问 → 不放行（窗口已刷新）")


print("\n=== 3. 冷却已过 → 正常放行且清理探测计时 ===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() - 1     # 已过期
    ds._em_probe[key] = time.time()
ok(ds._em_probe_allow(key) is True, "冷却已过 → 放行")
with ds._em_lock:
    ok(key not in ds._em_probe, "放行同时清掉了探测计时")


print("\n=== 4. 探测失败 → 冷却重算为封顶值，不被退避顶长 ===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() + 100
    ds._em_fail[key] = 1
ds._em_probe_note(key)
with ds._em_lock:
    left = ds._em_down_until[key] - time.time()
    ok(ds.EM_BLOCK_MAX - 2 <= left <= ds.EM_BLOCK_MAX + 2,
       f"冷却被重算为封顶 {ds.EM_BLOCK_MAX}s（实测剩 {left:.0f}s）")
    ok(ds._em_fail.get(key) == 1, "探测失败**不**增加正式失败轮数")


print("\n=== 5. em_blocked() 报出 next_probe_in ===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() + 200
    ds._em_fail[key] = 2
    ds._em_probe[key] = time.time() - 10
bl = ds.em_blocked()
ok(len(bl) == 1, "冷却中接口被列出")
ok(bl[0]["api"] == key, "接口 key 正确")
ok(0 <= bl[0]["next_probe_in"] <= ds.EM_PROBE_AFTER, "next_probe_in 在合理区间")
ok(bl[0]["seconds_left"] > 0, "seconds_left > 0")


print("\n=== 6. em_reset_blocks() 清干净探测账本 ===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() + 200
    ds._em_fail[key] = 2
    ds._em_probe[key] = time.time()
n = ds.em_reset_blocks()
with ds._em_lock:
    ok(ds._em_probe == {}, "探测账本被清空")
    ok(ds._em_down_until == {}, "冷却账本被清空")


print("\n=== 7. 过期冷却在 em_blocked() 里被顺手清掉（含探测计时）===")
reset()
with ds._em_lock:
    ds._em_down_until[key] = time.time() - 5
    ds._em_fail[key] = 4
    ds._em_probe[key] = time.time() - 99
ok(ds.em_blocked() == [], "过期项不出现在列表里")
with ds._em_lock:
    ok(key not in ds._em_probe, "过期项的探测计时一并清理")


print("\n===== 7 通过 / 0 失败 =====")
