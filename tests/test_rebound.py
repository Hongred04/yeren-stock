# -*- coding: utf-8 -*-
"""E5 低位承接方向（用户 10-13 要求「给我添加进现有体系中」）。

模块要回答的问题：**退潮期，机构在高位兑现的那笔钱去哪了？**
三个闸门同时成立才输出：退潮期（M3/E1）+ 存量博弈（M2）+ 机构专用席位净卖出（E2）。

来历：10-12 退潮日野人判断「明日白酒老登股有一波小波段」，
次日白酒Ⅱ 涨停 2 家、酿酒概念主力 +1.23 亿由负转正。这条判断的每一步
都能用系统已有数据复现，只是原体系没有模块把它们串起来 —— 本模块补的是这一步。

本测试钉住：闸门行为、评分区分度（不能一缩量日就全体同分）、
「价没动·钱在进」这个核心信号、以及配套的结构化净额与跨命名体系匹配。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="rebound_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import cls as cls_mod                 # noqa: E402
from core import moneyflow as flow_mod          # noqa: E402
from core import rebound                        # noqa: E402
from core import strategy_config as sc          # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


CFG = {"E5_rebound": {"enabled": True, "max_pct": 1.0, "min_main": 0.3,
                      "pos_days": 3, "top_n": 5, "min_score": 55,
                      "trial_weight": 0.05}}
SEN_EBB = {"stage": "ebb", "temperature": 49, "can_trade": False}
VOL_STOCK = {"mode": "peak_swing", "label": "峰值量能"}
LHB_OUT = {"available": True, "hotmoney_net": 1.85,
           "hotmoney": [{"tag": "桑田路", "stocks": [{"name": "某股"}]}],
           "institutions": {"net": -7.05}, "northbound": {"net": 6.38}}


def board(name, pct, main, main_pct=3.0, streak=1):
    return {"name": name, "pct": pct, "main": main,
            "main_pct": main_pct, "streak": streak, "lead": "示例股"}


def main():
    print("[1] 配置：E5 已进预设，字段齐全")
    cfg = sc.load_config()
    ok("E5_rebound" in sc.PRESETS["wildman"], "E5_rebound 进了 wildman 预设")
    d = cfg.get("E5_rebound") or {}
    for k in ("enabled", "max_pct", "min_main", "pos_days", "top_n",
              "min_score", "trial_weight"):
        ok(k in d, f"load_config 后有 {k}（值是 {d.get(k)}）")

    print("\n[2] 三个闸门：缺一不出，且说明缺的是哪个")
    boards = [board("甲板块", 0.1, 2.0, 5.0, 1)]
    base = dict(cfg=CFG, flow={"capital": {"inst_net": -7.05, "hotmoney_net": 1.85},
                              "boards_all": boards},
                sectors={"items": []}, lhb=LHB_OUT)
    r = rebound.evaluate(**base, sentiment=SEN_EBB, volume=VOL_STOCK, real_date="20261013")
    ok(r["active"] is True, "三个闸门全满足 → active")
    ok(len(r["candidates"]) >= 1, "输出至少一个方向")
    ok(r["summary"] and r["note"], "有总结句和免责说明")
    for c in r["candidates"]:
        for k in ("name", "pct", "main", "main_pct", "streak", "cum", "role",
                  "catalyst", "score", "tags", "how", "size"):
            if k not in c:
                ok(False, f"候选缺字段 {k}")
                break
        else:
            continue
        break
    else:
        ok(True, "每个方向都带齐 name/pct/main/streak/cum/role/catalyst/score/tags/how/size")

    # 缺退潮
    r2 = rebound.evaluate(**base, sentiment={"stage": "climax", "temperature": 84},
                          volume=VOL_STOCK, real_date="20261013")
    ok(r2["active"] is False and r2["candidates"] == [], "非退潮期 → 不输出")
    ok("退潮期" in r2["reason"], f"reason 说清缺退潮（{r2['reason']}）")
    # 缺存量（量能舒适 = 有增量，属主升场景）
    r3 = rebound.evaluate(**base, sentiment=SEN_EBB,
                          volume={"mode": "comfort_short", "label": "量能舒适"},
                          real_date="20261013")
    ok(r3["active"] is False, "有增量资金（量能舒适）→ 不在本模块射程")
    ok("存量博弈" in r3["reason"], f"reason 说清缺存量（{r3['reason']}）")
    # 缺机构净卖出
    r4 = rebound.evaluate(cfg=CFG, flow={"capital": {"inst_net": 3.0, "hotmoney_net": 1.0},
                                         "boards_all": boards},
                          sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, real_date="20261013")
    ok(r4["active"] is False, "机构在净买 -> 不是「兑现」场景")
    ok("机构净卖出" in r4["reason"], f"reason 说清缺机构净卖（{r4['reason']}）")

    print("\n[3] 模块关闭 / 空数据不崩")
    off = rebound.evaluate(cfg={"E5_rebound": {"enabled": False}})
    ok(off["enabled"] is False and off["active"] is False, "关闭时 enabled=False")
    r5 = rebound.evaluate(cfg=CFG, flow={"capital": {"inst_net": -7.05}, "boards_all": []},
                          sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, real_date="20261013")
    ok(r5["active"] is False and ("没有" in r5["reason"] or "噪音" in r5["reason"]),
       f"没有净流入板块时说明白（{r5['reason']}）")

    print("\n[4] 评分有区分度（缩量日不能全体同分）")
    # 构造 6 个体量差距明显的板块
    many = [board("大体量", -0.4, 5.0, 8.0, 1),
            board("小体量", -0.4, 1.0, 2.0, 1),
            board("中体量", -0.4, 3.0, 5.0, 1),
            board("抬升高", 0.9, 3.0, 5.0, 1),
            board("涨太多", 4.0, 3.0, 5.0, 1),
            board("连续两日", -0.4, 3.0, 5.0, 2)]
    r6 = rebound.evaluate(cfg=dict(CFG, E5_rebound=dict(CFG["E5_rebound"], top_n=99,
                                             min_score=0)),
                          flow={"capital": {"inst_net": -7.05}, "boards_all": many},
                          sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, real_date="20261013")
    by = {c["name"]: c for c in r6["candidates"]}
    ok(len({c["score"] for c in r6["candidates"]}) == len(r6["candidates"]),
       f"没有同分（{[round(c['score'], 1) for c in r6['candidates']]}）")
    ok(by["大体量"]["score"] > by["小体量"]["score"], "主力净体量大的分高")
    ok(by["抬升高"]["score"] < by["大体量"]["score"], "价格抬起来了要降分")
    ok(any("不追" in t for t in by["涨太多"]["tags"]), "涨幅过大标记「不追」")
    ok(any("连续" in t for t in by["连续两日"]["tags"]), "多日净流入标注天数")
    for c in r6["candidates"]:
        if "价没动·钱在进" not in " ".join(c["tags"]) and "不追" not in " ".join(c["tags"]):
            ok(False, f"{c['name']} 既没标吸筹也没标不追")
            break
    else:
        ok(True, "每个方向都明确标了「吸筹」或「不追高」")

    print("\n[5] min_main 过滤噪音")
    noise = [board("有量", -0.2, 1.5, 4.0, 1), board("噪音", -0.2, 0.05, 0.1, 1)]
    r7 = rebound.evaluate(cfg=CFG, flow={"capital": {"inst_net": -7.05},
                                         "boards_all": noise},
                          sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, real_date="20261013")
    names = [c["name"] for c in r7["candidates"]]
    ok("有量" in names and "噪音" not in names, f"0.05 亿被当噪音滤掉（{names}）")

    print("\n[6] 已是主线要降权、支线要加权")
    secs = {"items": [{"name": "主线Ⅱ", "role": "主线", "level": "强",
                       "facts": [], "judge": ""},
                      {"name": "支线Ⅱ", "role": "支线", "level": "弱",
                       "facts": [], "judge": ""}]}
    two = [board("主线Ⅱ", -0.3, 3.0, 6.0, 1), board("支线Ⅱ", -0.3, 3.0, 6.0, 1)]
    r8 = rebound.evaluate(cfg=CFG, flow={"capital": {"inst_net": -7.05},
                                         "boards_all": two},
                          sectors=secs, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, ml={"theme": "化学制品"}, real_date="20261013")
    b8 = {c["name"]: c for c in r8["candidates"]}
    ok(b8["支线Ⅱ"]["score"] > b8["主线Ⅱ"]["score"],
       f"同数据下支线分高于主线（{b8['支线Ⅱ']['score']} vs {b8['主线Ⅱ']['score']}）")
    ok(any("支线" in t for t in b8["支线Ⅱ"]["tags"]), "支线标出「小波段定位」")
    ok(any("主线" in t for t in b8["主线Ⅱ"]["tags"]), "主线标出「不追」")

    print("\n[7] 板块位置：跌得多加分、已抬升减分（用板块资金记忆算）")
    hist = {"20261012": [{"name": "甲板块", "pct": -1.0, "main": 1.0},
                         {"name": "乙板块", "pct": 1.0, "main": 1.0}],
            "20261013": [{"name": "甲板块", "pct": -0.5, "main": 2.0},
                         {"name": "乙板块", "pct": 1.2, "main": 2.0}]}
    with open(flow_mod.BOARD_HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f)
    cum = flow_mod.board_cum_pct("20261013", 3)
    ok(cum.get("甲板块") == -1.5, f"累计跌幅算对（{cum.get('甲板块')}）")
    ok(cum.get("乙板块") == 2.2, f"累计涨幅算对（{cum.get('乙板块')}）")
    r9 = rebound.evaluate(cfg=CFG, flow={"capital": {"inst_net": -7.05},
                                         "boards_all": [board("甲板块", 0.1, 3.0, 6.0),
                                                        board("乙板块", 0.1, 3.0, 6.0)]},
                          sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                          lhb=LHB_OUT, real_date="20261013")
    b9 = {c["name"]: c for c in r9["candidates"]}
    ok(b9["甲板块"]["score"] > b9["乙板块"]["score"],
       f"近几日跌的分更高（{b9['甲板块']['score']} vs {b9['乙板块']['score']}）")
    ok("位置偏低" in " ".join(b9["甲板块"]["tags"]), "位置低的标出「位置偏低」")
    ok("已抬升" in " ".join(b9["乙板块"]["tags"]), "已抬升的标出「已抬升」")
    ok(flow_mod.board_cum_pct("20261013", 99).get("甲板块") == -1.5,
       "days 大于档案数时用全部档案（不报错）")

    print("\n[8] top_n 生效")
    r10 = rebound.evaluate(cfg=dict(CFG, E5_rebound=dict(CFG["E5_rebound"], top_n=2)),
                           flow={"capital": {"inst_net": -7.05},
                                 "boards_all": many},
                           sectors={"items": []}, sentiment=SEN_EBB, volume=VOL_STOCK,
                           lhb=LHB_OUT, real_date="20261013")
    ok(len(r10["candidates"]) == 2, f"top_n=2 只出 2 个（实际 {len(r10['candidates'])}）")

    print("\n[9] 机构/游资净额的结构化字段（别再解析中文文案）")
    cap = flow_mod.capital_view({}, LHB_OUT, None)
    ok(cap.get("inst_net") == -7.05, f"capital.inst_net = {cap.get('inst_net')}")
    ok(cap.get("hotmoney_net") == 1.85, f"capital.hotmoney_net = {cap.get('hotmoney_net')}")
    ok(cap.get("north_net") == 6.38, f"capital.north_net = {cap.get('north_net')}")
    ok("机构兑现" in (cap.get("headline") or ""), "headline 仍是那句结论（没变）")
    # lhb 不可用时结构化字段应为 None，而不是 0（0 会被误读成"净买 0"）
    cap2 = flow_mod.capital_view({}, {"available": False}, None)
    ok(cap2.get("inst_net") is None, "lhb 不可用时 inst_net 是 None（不是 0）")

    print("\n[10] 跨命名体系匹配（东财「酿酒概念」↔ 财联社「白酒」）")
    ok(rebound._f("x", 9) == 9.0, "_f 兜底")
    fake_wind = [{"name": "白酒", "catalyst": "渠道库存去化 10%-20%"}]
    ok((cls_mod.match_heat("酿酒概念", fake_wind) or {}).get("name") == "白酒",
       "酿酒概念 → 白酒（催化接上）")
    ok((cls_mod.match_heat("食品饮料", fake_wind) or {}).get("name") == "白酒",
       "食品饮料 → 白酒")
    ok((cls_mod.match_heat("白酒Ⅱ", fake_wind) or {}).get("name") == "白酒",
       "白酒Ⅱ → 白酒")
    ok(cls_mod.match_heat("完全不相干的板块", fake_wind) is None, "不相干的不误匹配")
    ok(cls_mod._norm_board("白酒Ⅱ") == "白酒", "归一：去掉罗马数字后缀")
    ok(cls_mod._norm_board("酿酒概念") == "酿酒", "归一：去掉「概念」后缀")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
