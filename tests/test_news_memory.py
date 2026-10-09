# -*- coding: utf-8 -*-
"""消息记忆 + 板块资金记忆测试。

背景（用户实测 bug）：七部门印发《新型电池产业发展"十五五"规划》几天后，
快讯池翻不到旧闻，S1 只能看到当天的公司澄清公告（C级），把主线判成一日游。
修复：① 抓取加深（央视 6 页 / 新浪滚动 16 页）；② S/A/B 级定级逐日归档，
有效期内（S 20天 / A 10天 / B 5天）继续参与定级。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="news_mem_")

from core import mainline   # noqa: E402
from core import news       # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


EV = [{"time": "2026-10-07 07:37:50",
       "text": "工业和信息化部、国家发展改革委等七部门联合印发《新型电池产业发展“十五五”规划》",
       "source": "央视新闻"}]


def main():
    print("[1] 归档与查询：A 级在有效期内，过期/别题材/C级不生效")
    news.remember("20261009", "电池", "A", EV)
    mem = news.lookup("电池", "20261009")
    ok(mem and mem["level"] == "A" and mem["days"] == 0, f"当天可查（{mem}）")
    ok(news.lookup("电池", "20261018") is not None, "9 天后仍在有效期内（A级10天）")
    ok(news.lookup("电池", "20261025") is None, "16 天后过期")
    ok(news.lookup("机器人", "20261010") is None, "别的题材查不到")
    news.remember("20261009", "测试题材", "C", EV)
    ok(news.lookup("测试题材", "20261009") is None, "C 级不记（噪声）")

    print("\n[2] 同日同题材：取最高级别、证据合并")
    news.remember("20261009", "电池", "B", [{"time": "2026-10-09 10:00:00",
                                              "text": "某行业数据超预期", "source": "x"}])
    entries = news.memory_entries("电池")
    ok(len(entries) == 1, f"同日同题材只留一条（{len(entries)}）")
    ok(entries[0]["level"] == "A", f"级别取更高的 A（实际 {entries[0]['level']}）")
    ok(len(entries[0]["items"]) == 2, f"证据合并为 2 条（实际 {len(entries[0]['items'])}）")

    print("\n[3] 与今日定级合并：记忆更高时以记忆为准")
    today_c = {"found": True, "level": "C", "scanned": 1617,
               "evidence": [{"time": "2026-10-09 17:55:41", "text": "某公司澄清公告",
                             "source": "新浪7x24"}]}
    merged = news.merge_suggestion(today_c, news.lookup("电池", "20261009"))
    ok(merged["level"] == "A", f"合并后级别 A（实际 {merged['level']}）")
    ok(merged["today_level"] == "C", "今日实际级别留档备查（C）")
    ok(merged["memory"]["date"] == "20261009", "带记忆来源日期")
    today_s = {"found": True, "level": "S", "evidence": EV[:1]}
    merged2 = news.merge_suggestion(today_s, news.lookup("电池", "20261009"))
    ok(merged2["level"] == "S", "今日更高时不降级")
    ok(merged2.get("memory") is not None and merged2.get("today_level") is None,
       "记忆只作佐证时不影响主体")

    print("\n[4] S1 信号：记忆生效 → pass，证据写明「消息记忆」")
    s1 = mainline._signal_message({"message_level": "auto"}, "电池", merged)
    ok(s1["state"] == "pass", f"state=pass（实际 {s1['state']}）")
    ok("消息记忆" in s1["evidence"] and "十五五" in s1["evidence"],
       "证据含记忆来源与原文摘要：" + s1["evidence"][:70])
    ok(any("有效期" in d for d in s1["detail"]), "说明里写清有效期机制")
    s1b = mainline._signal_message({"message_level": "auto"}, "电池", None)
    ok(s1b["state"] == "unknown" and "记忆" in s1b["evidence"],
       "无今日消息且无记忆 → unknown（提示记忆里也没有）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
