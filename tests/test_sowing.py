# -*- coding: utf-8 -*-
"""E8 题材播种（事件 → 受益 → 标的）· 前置题材池。

真实场景（10-13 实测，本测试按它构造夹具）：
  九阳股份 sz002242 在近 30 个交易日里**两次启动** —— 09-11（首板，标签
  「家电+机器人概念」）→ 沉寂 → 09-28（首板，标签「机器人概念+家电」）→
  一路连板到 6 板，期间标签漂移为「小家电」再漂移到「电子商务+家电」。
  这正是"打板要提前找题材"的那个窗口：第二次启动 + 标签漂移 = 逻辑在接力。

本测试钉住：
  ① 标签解析：reason「；」前那段 = 题材标签；「家电+机器人概念」与
     「机器人概念+家电」应视为**同一标签**（词集合相同，只是顺序不同）。
  ② 题材日历：新播种 / 加固 / 持平 / 减弱 / 熄火 五档边界。
  ③ 独苗题材：今日只有 1 只、且是首板，才算"题材刚冒头"。
  ④ 重复启动：两次启动之间必须有空档（一路连板不算）；标签漂移按词集合判。
  ⑤ 冷启动 / 关闭 / 空池都不硬凑。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="sowing_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds            # noqa: E402
from core import sowing                      # noqa: E402
from core import strategy_config as sc       # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def row(code, name, boards, sector, reason, first=93000, brk=0):
    return {"code": code, "name": name, "boards": boards, "sector": sector,
            "reason": reason, "first_time": first, "break_count": brk,
            "pct": 10.0, "turnover": 5.0, "float_mv": 3e9}


def save(date, rows):
    ds.save_pool_history(date, rows, force=True)


def main():
    print("===== E8 题材播种：标签解析 / 题材日历 / 独苗 / 重复启动 =====\n")

    # [1] 标签解析
    print("[1] 标签解析")
    ok(sowing.label_of("机器人概念+家电；1. 机器人概念：据…") == "机器人概念+家电",
       "取「；」前那段")
    ok(sowing.label_of("电子商务+家电；10月8日新闻联播…") == "电子商务+家电",
       "取「；」前那段（事件段被截掉）")
    ok(sowing.label_of("") == "", "空 reason → 空标签")
    ok(sowing.events_of("10月8日新闻联播：激活消费大市场") == ["新闻联播"],
       "识别新闻联播")
    ok(sowing.events_of("商务部：经中美双方商定…经贸磋商") == ["经贸/关税"],
       "识别经贸/关税")
    ok(sowing.events_of("据2026年半年报…") == ["业绩"], "识别业绩")
    ok(sowing.events_of("") == [], "空 reason → 无事件")
    print()

    # [2] 标签漂移按词集合判 —— 顺序不同不算漂移
    print("[2] 标签漂移按词集合判")
    save("20260911", [row("sz002242", "九阳", 1, "家电", "家电+机器人概念；据…")])
    save("20260928", [row("sz002242", "九阳", 1, "机器人概念", "机器人概念+家电；据…")])
    r = sowing.evaluate({"E8_sowing": {"lookback_days": 30, "top_restarts": 10}},
                        real_date="20260928")
    jy = [x for x in r["restarts"] if x["code"] == "sz002242"]
    ok(bool(jy), "九阳进了重复启动列表")
    if jy:
        ok(jy[0]["drifted"] is False,
           f"「家电+机器人概念」vs「机器人概念+家电」顺序不同 → 不算漂移（{jy[0]['labels']}）")
        ok(jy[0]["launch_count"] == 2, f"两次启动（{jy[0]['launch_count']}）")
    print()

    # [3] 题材日历五档
    print("[3] 题材日历五档")
    # 历史：老题材/退题材/稳题材/加题材 都出现过。关键在「上一交易日」20260918
    # 这一天（today=20260922 的 prev_date）要写全对照题材的昨日家数，否则 prev_n=0
    # 会把"今天还在"误判成"加固"。
    save("20260915", [row("sh0001", "A", 1, "老题材", "老题材；据…")])
    save("20260916", [row("sh0002", "B", 1, "退题材", "退题材；据…")])
    # 上一交易日 20260918：老题材 1 / 退题材 1 / 稳题材 1 / 加题材 1
    save("20260918", [row("sh0003", "C0", 1, "老题材", "老题材；据…"),
                      row("sh0004", "D0", 1, "退题材", "退题材；据…"),
                      row("sh0005", "E0", 1, "稳题材", "稳题材；据…"),
                      row("sh0006", "F0", 1, "加题材", "加题材；据…")])
    # 今日 20260922：新题材(今天才有→new) / 老题材(1 只=hold) / 退题材(0 只=gone)
    # 加题材(2 只=add) / 稳题材(1 只=hold)
    save("20260922", [row("sh0007", "A2", 1, "新题材", "新题材；据…"),
                      row("sh0008", "B2", 1, "老题材", "老题材；据…"),
                      row("sh0009", "E1", 1, "加题材", "加题材；据…"),
                      row("sh0010", "E2", 1, "加题材", "加题材；据…"),
                      row("sh0011", "D2", 1, "稳题材", "稳题材；据…")])
    r = sowing.evaluate({"E8_sowing": {"lookback_days": 30}}, real_date="20260922")
    by = {t["theme"]: t["label_cn"] for t in r["themes"]}
    ok(by.get("新题材") == "新播种", f"新题材=新播种（{by.get('新题材')}）")
    ok(by.get("老题材") == "持平", f"老题材今天还在=持平（{by.get('老题材')}）")
    ok(by.get("退题材") == "熄火", f"退题材今天没了=熄火（{by.get('退题材')}）")
    ok(by.get("加题材") == "加固中", f"加题材今天更多=加固中（{by.get('加题材')}）")
    ok(by.get("稳题材") == "持平", f"稳题材不变=持平（{by.get('稳题材')}）")
    print()

    # [4] 独苗题材：只有 1 只且首板（新旧题材都算，新旧只影响打分/文案）
    print("[4] 独苗题材")
    lonely = {x["name"]: x for x in r["lonely"]}
    ok("A2" in lonely, "全新题材今天只有 1 只首板 → 独苗")
    ok("B2" in lonely, "老题材今天也只有 1 只首板 → 同样算独苗（老树新芽）")
    ok(all(x["score"] > 0 for x in r["lonely"]), "独苗都有分")
    if "A2" in lonely and "B2" in lonely:
        ok(lonely["A2"]["new_theme"] is True, "A2 标全新题材")
        ok(lonely["B2"]["new_theme"] is False, "B2 标老树新芽")
    print()

    # [5] 冷启动 / 关闭
    print("[5] 冷启动 / 关闭")
    # 清空归档
    for d in list(ds.pool_history_dates()):
        p = os.path.join(_TMP, "pool_history", f"{d}.json")
        if os.path.exists(p):
            os.remove(p)
    r = sowing.evaluate({}, real_date="20260922")
    ok(r["active"] is False and "冷启动" in r["reason"],
       "无归档 → 冷启动提示")
    r = sowing.evaluate({"E8_sowing": {"enabled": False}}, real_date="20260922")
    ok(r["active"] is False and "关闭" in r["reason"], "关闭 → 不激活")
    print()

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
