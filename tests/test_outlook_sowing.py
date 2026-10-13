# -*- coding: utf-8 -*-
"""outlook（次日预案）消费 E8 题材播种信号的集成契约。

用户 10-13：「把 E8 的「新播种 + 独苗 + 重复启动」接进 outlook，让它不只单独
一张卡，而是参与明天该盯哪几条线的排序。」

本测试钉住 outlook.build 的 sowing 入参契约：
  ① 有新播种/独苗/漂移 → points 出现「题材播种」维度，值含三类信号；
  ② watch 出现「新播种题材」「标签漂移」两条前瞻盯线；
  ③ 退潮期**不**加"新播种题材接力"情景（退潮不接力），非退潮期加；
  ④ 空 sowing / sowing inactive → 不出现「题材播种」维度，也不崩。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="outlook_sow_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import outlook                      # noqa: E402
from core import strategy_config as sc        # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


CFG = sc.load_config()
SOW = {
    "themes": [
        {"theme": "旅游及景", "trend": "new", "label_cn": "新播种"},
        {"theme": "农化制品", "trend": "new", "label_cn": "新播种"},
        {"theme": "化学制品", "trend": "gone", "label_cn": "熄火"},
    ],
    "lonely": [{"name": "粤桂股份", "theme": "综合"},
               {"name": "荣丰控股", "theme": "房地产"}],
    "restarts": [{"name": "超声电子", "drifted": True},
                 {"name": "福建水泥", "drifted": True}],
}
SENT_EBB = {"stage": "ebb", "label": "退潮期",
            "metrics": {"hurt_area": 0.29, "limit_down": 5}}
SENT_PROBE = {"stage": "probe", "label": "起步期",
              "metrics": {"hurt_area": 0.12, "limit_down": 1}}
VOL = {"mode": "comfort_short", "label": "量能舒适", "amount": 16759.0,
       "basis": "当日全天（已收盘）"}


def main():
    print("===== outlook × E8 题材播种 集成契约 =====\n")

    # [1] 有信号 → points 出现「题材播种」维度
    print("[1] points 出现「题材播种」维度")
    o = outlook.build(CFG, "20261013", SENT_EBB, VOL,
                      ml={"theme": "化学制品", "status": "one_day", "streak": 1},
                      sowing=SOW)
    sow_pt = [p for p in o["points"] if p["k"] == "题材播种"]
    ok(len(sow_pt) == 1, "出现且仅一个「题材播种」维度")
    if sow_pt:
        v = sow_pt[0]["v"]
        ok("新播种" in v and "旅游及景" in v, "含新播种")
        ok("独苗" in v and "粤桂股份" in v, "含独苗")
        ok("标签漂移" in v and "超声电子" in v, "含标签漂移")
        ok("化学制品" not in v, "熄火题材不进前瞻维度（只列新播种/独苗/漂移）")
    print()

    # [2] watch 出现两条前瞻盯线
    print("[2] watch 出现前瞻盯线")
    ok(any("新播种题材" in w for w in o["watch"]), "盯线① 新播种题材")
    ok(any("标签漂移" in w and "逻辑接力" in w for w in o["watch"]),
       "盯线② 标签漂移（逻辑接力/证伪）")
    print()

    # [3] 退潮期不加"新播种接力"情景，非退潮期加
    print("[3] 情景预案的节点门禁")
    ok(all("新播种题材" not in s["cond"] for s in o["scenarios"]),
       "退潮期不出现「新播种题材接力」情景（退潮不接力）")
    o2 = outlook.build(CFG, "20261013", SENT_PROBE, VOL,
                       ml={"theme": "X", "status": "day1_pending", "streak": 1},
                       sowing=SOW)
    ok(any("新播种题材" in s["cond"] for s in o2["scenarios"]),
       "非退潮期出现「新播种题材接力」情景")
    print()

    # [4] 空 sowing / inactive → 不出现维度、不崩
    print("[4] 空 sowing / inactive")
    o3 = outlook.build(CFG, "20261013", SENT_EBB, VOL,
                       ml={"theme": "化学制品", "status": "one_day"},
                       sowing=None)
    ok(all(p["k"] != "题材播种" for p in o3["points"]),
       "sowing=None → 不出现「题材播种」维度")
    o4 = outlook.build(CFG, "20261013", SENT_EBB, VOL,
                       ml={}, sowing={"active": False, "themes": [], "lonely": [],
                                      "restarts": []})
    ok(all(p["k"] != "题材播种" for p in o4["points"]),
       "sowing inactive → 不出现「题材播种」维度")
    ok(o4["date"] == "10-14" and o4["headline"], "空信号仍正常产出次日预案")
    print()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
