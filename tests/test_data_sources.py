# -*- coding: utf-8 -*-
"""扩展数据源回归测试：财联社（cls）与开盘红（kph）。

纯逻辑离线测试：签名算法 / 字段映射 / 板块热度匹配 / 归档回补调度 /
情绪温度计的「打板溢价」兜底分支。不请求网络 —— 外部接口的可用性
由运行时容错 + probes 脚本保证。测试钉住的是协议知识与调度逻辑。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="dsc_")

from core import cls                      # noqa: E402
from core import datasource as ds         # noqa: E402
from core import kph                      # noqa: E402
from core import sentiment_meter as sm    # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def main():
    print("[1] 财联社签名算法（已知向量）")
    ok(cls._sign({"a": "1", "b": "2"}) == "4e13ecb1934db939e390065a9b63ecd8",
       "md5(sha1('a=1&b=2')) 与预期一致")
    ok(cls._sign({"sv": "8.7.4", "app": "cailianpress"})
       == "dddf20546c32e0d2ed78ba60b6cad383", "参数按键名排序，顺序无关")

    print("[2] 板块热度匹配（精确/别名/唯一包含/歧义）")
    heats = [{"name": "化工", "rank": 3, "rank_change": 5, "is_new": False, "heat": 100.0},
             {"name": "半导体芯片", "rank": 9, "rank_change": -2, "is_new": False, "heat": 80.0},
             {"name": "电池", "rank": 5, "rank_change": -3, "is_new": False, "heat": 90.0},
             {"name": "固态电池", "rank": 2, "rank_change": 1, "is_new": True, "heat": 95.0}]
    ok(cls.match_heat("电池", heats)["rank"] == 5, "精确匹配优先（不被'固态电池'抢走）")
    ok(cls.match_heat("化学制品", heats)["name"] == "化工", "别名表命中（化学制品→化工）")
    ok(cls.match_heat("半导体", heats)["name"] == "半导体芯片", "唯一包含匹配")
    ok(cls.match_heat("跨境支付", heats) is None, "无匹配返回 None")
    ok(cls.match_heat("", heats) is None, "空名返回 None")

    print("[3] 开盘红封板时间归一（epoch ↔ HHMMSS）")
    ok(kph._hms(1790748529) > 0, "epoch 秒转为 HHMMSS")
    ok(kph._hms(93000) == 93000, "HHMMSS 原样返回")
    ok(kph._hms(0) == 0 and kph._hms(None) == 0, "空值返回 0")

    print("[4] 历史池行转换与 ST 过滤")
    raw = ["600131", "国网信通", 0, "", 0, 0, 1790748529, 0, 26475540, "首板", 1,
           "智能电网、基础建设", 64260307, 401873347, 5.41, 7654631283,
           "智能电网", "北京国资"]
    row = kph._to_pool_row(raw)
    ok(row and row["code"] == "sh600131" and row["name"] == "国网信通",
       "代码补前缀 / 名称映射")
    ok(row["boards"] == 1 and row["sector"] == "智能电网", "连板数 / 板块映射")
    ok(row["first_time"] == kph._hms(1790748529), "封板时间映射")
    ok(kph._to_pool_row(["000001", "*ST测试", 0, "", 0, 0, 1790748529, 0, 1,
                         "首板", 1, "x", 1, 1, 1, 1, "y", "z"]) is None,
       "ST 过滤（对齐东财口径）")
    ok(kph._to_pool_row(["600000"]) is None, "行过短返回 None")

    print("[5] 归档回补调度（monkeypatch 数据层）")
    ds.save_pool_history("20260921", [{"code": "sh600001", "name": "已有",
                                       "boards": 1}])

    def fake_pool(date):
        if date == "20260922":
            return [{"code": "sh600002", "name": "新补", "boards": 1,
                     "first_time": 93000, "sector": "电池"}]
        return []
    kph.history_pool = fake_pool
    r = kph.autobackfill("20260925", lookback=4, max_try=4)
    ok("20260922" in r["filled"], "缺失日期被补上")
    got = ds.load_pool_history("20260921")
    ok(len(got) == 1 and got[0]["name"] == "已有", "已有归档不被覆盖")
    r2 = kph.autobackfill("20260925", lookback=4, max_try=4)
    ok(r2["filled"] == [], "补过/试过的日期二轮零请求")

    print("[6] 情绪温度计：打板溢价东财口径优先")
    ds.fetch_breadth = lambda *a, **k: {}
    ds.fetch_yesterday_zt_pool = lambda *a, **k: [
        {"code": "sh600001", "name": "甲", "pct": 5.0, "boards_y": 1,
         "yfbt": 93000, "sector": "X"},
        {"code": "sh600002", "name": "乙", "pct": -3.0, "boards_y": 2,
         "yfbt": 93000, "sector": "X"},
    ]
    m = sm.build([], [], [], {}, "20260920", {"E1_meter": {"enabled": True}})
    me = m["metrics"]
    ok(me["money_effect"] == 1.0, "打板溢价 = 东财均值 (5 + -3)/2 = 1.0")
    ok(me["height_premium"] == -3.0, "连板溢价只统计昨日≥2板")
    ok(me["premium_src"] == "em", "来源标记 em")

    print("[7] 全零涨幅视为无效（盘前特征），不进口径")
    ds.fetch_yesterday_zt_pool = lambda *a, **k: [
        {"code": "sh600001", "name": "甲", "pct": 0.0, "boards_y": 1,
         "yfbt": 93000, "sector": "X"},
    ]
    m2 = sm.build([], [], [], {}, "20260921", {"E1_meter": {"enabled": True}})
    ok(m2["metrics"]["money_effect"] is None
       and m2["metrics"]["premium_src"] == "",
       "全零不进口径（无本地兜底数据时为空）")

    print()
    print(f"===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    sys.exit(main())
