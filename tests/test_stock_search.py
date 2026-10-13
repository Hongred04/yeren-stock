# -*- coding: utf-8 -*-
"""股票名称搜索的回归护栏 —— 「加自选」从只输代码改成可搜名称。

需求（用户 10-13）：自选股原来只能输代码，记不住代码就加不进去；
改为**输入名称关键词即可匹配并添加**，同时**保留按代码添加**。

设计要点（本测试钉死这些契约）：

  ① **双源合并**，缺一不可：
     · 本地名单（全市场 6000+ 只，名称子串匹配）—— 召回完整；
     · 腾讯 smartbox（唯一支持**拼音首字母**，且名称比冷启动的板块表新）。
     只用在线源会漏：它是**联想**接口，结果条数有限且**前缀优先**，搜"银行"
     会被「银行ETF…」占满、真正的招商银行一个都出不来；且不覆盖全部北交所
     （实测"贝特瑞"直接返回无结果）。
  ② **只认 A 股**：港股(GP)/美股/ETF/基金/权证一律剔除 —— 加进自选股查不到行情。
  ③ **排序稳定可复现**：名称前缀 > 名称包含 > 拼音前缀 > 拼音包含；
     同级短名优先、再按代码升序。否则同一关键词两次顺序不同，用户点错就加错票。
  ④ **空/无匹配/接口异常** 三种情况都要有明确出口，不能静默失败。
  ⑤ 冷启动名单必须剔除「(已切换)」的**作废旧代码**（北交所 835xxx → 920xxx），
     否则用户搜出来加进去的是一只查不到行情的死票。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="stocksearch_")
os.environ["STOCK_DATA_DIR"] = _TMP

from core import datasource as ds      # noqa: E402
from core import universe              # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


# 造一份假的板块成员表当冷启动源（离线、确定性）
_BOARDS = {"members": {"-": [
    {"code": "sh600519", "name": "贵州茅台"},
    {"code": "sh600036", "name": "招商银行"},
    {"code": "sh600000", "name": "浦发银行"},
    {"code": "sz300750", "name": "宁德时代"},
    {"code": "bj920185", "name": "贝特瑞"},
    {"code": "bj835185", "name": "贝特瑞(已切换)"},     # 作废旧代码，必须剔除
    {"code": "sz000001", "name": "平安银行"},
]}}


def write_boards():
    os.makedirs(_TMP, exist_ok=True)
    with open(os.path.join(_TMP, "board_members.json"), "w", encoding="utf-8") as f:
        json.dump(_BOARDS, f, ensure_ascii=False)


def reset_index():
    """清掉名单文件与内存缓存 —— 每个用例从零开始。"""
    for p in (ds.NAME_INDEX_PATH, ds.NAME_INDEX_PATH + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
    ds._name_index_reset_cache()


def fake_get_factory(payload: str):
    def _fake(url, timeout=12, referer=""):
        return payload.encode("utf-8")
    return _fake


def fake_get_map(mapping: dict):
    """按关键词分支的假接口 —— 真实 smartbox 是"给什么词回什么结果"，
    假接口一律回同一份会让"多源合并"的断言失真。"""
    def _fake(url, timeout=12, referer=""):
        for k, v in mapping.items():
            if k in urllib.parse.unquote(url):
                return v.encode("utf-8")
        return b'v_hint="N";'
    return _fake


def fake_get_boom(url, timeout=12, referer=""):
    raise OSError("模拟断网")


def hint(records: list[tuple]) -> str:
    """按 smartbox 的格式拼一个 v_hint 响应。"""
    body = "^".join("~".join(r) for r in records)
    return 'v_hint="%s";' % body


def main():
    write_boards()
    real_get = ds._get

    print("[1] 解析 smartbox 响应：只留 A 股，剔除港股/美股/ETF/基金/权证")
    raw = hint([
        ("sh", "600519", "\\u8d35\\u5dde\\u8305\\u53f0", "gzmt", "GP-A"),
        ("sh", "688187", "\\u65f6\\u4ee3\\u7535\\u6c14", "sddq", "GP-A-KCB"),  # 科创板
        ("hk", "02318", "\\u4e2d\\u56fd\\u5e73\\u5b89", "zgpa", "GP"),          # 港股
        ("us", "pngay.ps", "\\u4e2d\\u56fd\\u5e73\\u5b89", "zgpa", "GP"),       # 美股
        ("sh", "512800", "\\u94f6\\u884cETF", "yhetf", "ETF"),                   # ETF
        ("sz", "167002", "\\u5e73\\u5b89\\u9f0e\\u8d8aLOF", "pady", "LOF"),     # LOF
        ("jj", "014283", "\\u5317\\u4ea4\\u6240\\u7cbe\\u9009", "bjs", "KJ-CX"),  # 基金
    ])
    rows = ds._parse_search_hint(raw)
    codes = [r["code"] for r in rows]
    ok(codes == ["sh600519", "sh688187"], f"只留 2 只 A 股（实际 {codes}）")
    ok(rows[0]["name"] == "贵州茅台", f"\\uXXXX 还原成中文（{rows[0]['name']}）")
    ok(rows[0]["pinyin"] == "gzmt", "拼音首字母解析出来")
    ok(rows[0]["board"] == "沪市主板", f"板块标注正确（{rows[0]['board']}）")
    ok(rows[1]["board"] == "科创板", "科创板（GP-A-KCB）也算 A 股")
    ok(ds._parse_search_hint('v_hint="N";') == [], '无结果标记 v_hint="N" → 空列表')
    ok(ds._parse_search_hint("") == [], "空响应 → 空列表（不抛异常）")

    print("\n[2] 排序：名称前缀 > 名称包含 > 拼音前缀 > 拼音包含；同级短名优先")
    rows = [
        {"code": "sz300750", "name": "宁德时代", "pinyin": "ndsd"},
        {"code": "sh600458", "name": "时代新材", "pinyin": "sdxc"},   # 前缀命中
        {"code": "sh600519", "name": "贵州茅台", "pinyin": "gzmt"},
        {"code": "sz000001", "name": "平安银行", "pinyin": "payh"},
    ]
    r1 = ds._rank_search(rows, "时代", 10)
    ok(r1[0]["name"] == "时代新材", f"前缀命中排最前（{r1[0]['name']}）")
    ok(r1[1]["name"] == "宁德时代", "名称包含排第二")
    r2 = ds._rank_search(rows, "gzmt", 10)
    ok(r2 and r2[0]["name"] == "贵州茅台", "拼音首字母能命中")
    # _rank_search 是**排序器**不是过滤器：过滤由数据源各自负责（本地名单先
    # 子串筛，在线接口只回相关结果），这里只保证"匹配得越靠前越前"。
    r3 = ds._rank_search(rows, "银行", 10)
    ok(r3[0]["name"] == "平安银行", f"命中项排最前（{r3[0]['name']}）")
    ok(len(r3) == len(rows), "排序器不丢行（过滤交给调用方/数据源）")
    # 稳定性：同样的输入跑两次，顺序必须一致
    ok([x["code"] for x in ds._rank_search(rows, "e", 10)] ==
       [x["code"] for x in ds._rank_search(rows, "e", 10)], "同输入两次排序一致")
    ok(len(ds._rank_search(rows, "银行", 1)) == 1, "limit 生效")

    print("\n[3] 冷启动名单：从板块成员表建立，剔除「(已切换)」的作废旧代码")
    reset_index()
    idx = ds._name_index_load()
    ok(len(idx) == 6, f"6 只有效标的进名单（实际 {len(idx)}）")
    ok("bj835185" not in idx, "作废旧代码 bj835185 被剔除")
    ok(idx.get("bj920185") == "贝特瑞", "新代码 bj920185 保留")
    ok(os.path.exists(ds.NAME_INDEX_PATH), "冷启动名单已落盘")

    print("\n[4] 空关键词：不发请求、不报错、返回空")
    called = {"n": 0}

    def _count_get(url, timeout=12, referer=""):
        called["n"] += 1
        return b""

    ds._get = _count_get
    try:
        r = ds.search_stocks("", 8)
        ok(r["ok"] is True and r["results"] == [], "空关键词 → 空结果")
        ok(r["source"] == "none", f'source="none"（{r["source"]}）')
        ok(called["n"] == 0, "空关键词不发网络请求")
        r = ds.search_stocks("   ", 8)
        ok(r["results"] == [] and called["n"] == 0, "纯空格同样按空处理")
    finally:
        ds._get = real_get

    print("\n[5] 在线命中：双源合并（本地召回 + 在线补拼音）")
    # 模拟真实 smartbox：搜"银行"只回 ETF（会被过滤掉），真正的银行股只能靠本地名单
    ds._get = fake_get_map({
        "茅台": hint([("sh", "600519", "\\u8d35\\u5dde\\u8305\\u53f0", "gzmt", "GP-A")]),
        "gzmt": hint([("sh", "600519", "\\u8d35\\u5dde\\u8305\\u53f0", "gzmt", "GP-A")]),
        "银行": hint([("sh", "512800", "\\u94f6\\u884cETF", "yhetf", "ETF")]),
    })
    try:
        r = ds.search_stocks("茅台", 8)
        ok(r["ok"] and r["results"], "搜「茅台」有结果")
        ok(r["results"][0]["code"] == "sh600519", "命中贵州茅台")
        ok(r["source"] == "live+local", f'两源都有 → live+local（{r["source"]}）')
        r2 = ds.search_stocks("gzmt", 8)          # 拼音：只有在线源能给
        ok(r2["results"] and r2["results"][0]["code"] == "sh600519", "拼音搜「gzmt」命中")
        ok(r2["source"] == "live", f"仅在线源 → live（{r2['source']}）")
        r3 = ds.search_stocks("银行", 8)          # 在线只回 ETF → 靠本地名单召回银行股
        names = [x["name"] for x in r3["results"]]
        ok(len(names) == 3, f"「银行」靠本地名单召回 3 只（实际 {names}）")
        ok(all("银行" in n for n in names), "结果全部是银行股（ETF 已被剔除）")
        ok("512800" not in [x["code"] for x in r3["results"]], "在线回的 ETF 没混进来")
    finally:
        ds._get = real_get

    print("\n[6] 在线失败 → 用本地名单兜底，并如实说明")
    ds._get = fake_get_boom
    try:
        r = ds.search_stocks("银行", 8)
        ok(r["ok"] is True and len(r["results"]) == 3, "断网时仍能搜到 3 只银行")
        ok(r["source"] == "local", f'source="local"（{r["source"]}）')
        ok("没响应" in r["note"], f'note 说明接口异常：{r["note"]}')
        r2 = ds.search_stocks("不存在的名字", 8)
        ok(r2["ok"] is False, "断网且本地无匹配 → ok=False")
        ok(r2["source"] == "error", f'source="error"（{r2["source"]}）')
        ok("600519" in r2["note"], "提示可以改用代码添加")
    finally:
        ds._get = real_get

    print("\n[7] 无匹配（接口正常）：明确返回空结果，不是错误")
    ds._get = fake_get_factory('v_hint="N";')
    try:
        r = ds.search_stocks("不存在的名字xyz", 8)
        ok(r["ok"] is True and r["results"] == [], "接口正常但无匹配 → 空结果、ok=True")
        ok(r["source"] == "live+local", f'来源如实标注（{r["source"]}）')
    finally:
        ds._get = real_get

    print("\n[8] remember_names：扫描后更新名单（新增/更名），无变化不写盘")
    reset_index()
    ds._name_index_load()                       # 先冷启动（6 条）
    n0 = ds.name_index_size()
    ok(n0 == 6, f"冷启动基线 6 条（实际 {n0}）")
    # 只有**全市场扫描**才更新名单（<100 只的调用一律忽略），所以这里要凑够样本
    snaps = {"sh600519": {"name": "贵州茅台"}, "sh600036": {"name": "招商银行"},
             "sh600000": {"name": "浦发银行"}, "sz300750": {"name": "宁德时代"},
             "bj920185": {"name": "贝特瑞"}, "sz000001": {"name": "平安银行"}}
    snaps.update({f"sh601{i:03d}": {"name": f"测试股{i}"} for i in range(1, 115)})
    ok(len(snaps) == 120, f"样本 {len(snaps)} 只（>100 才生效）")
    added = ds.remember_names(snaps)
    ok(added == 114, f"新增 114 只，实际 {added}")
    ok(ds.name_index_size() == n0 + 114, "名单条数 +114")
    ok(ds._name_index_load().get("sh601001") == "测试股1", "新标的写进名单")
    # 更名（加 ST 前缀）要跟上 —— 行情源的名字比冷启动的板块表新
    renamed = dict(snaps)
    renamed["sh600519"] = {"name": "ST贵州茅台"}
    ds.remember_names(renamed)
    ok(ds._name_index_load().get("sh600519") == "ST贵州茅台", "更名能更新")
    mt = os.path.getmtime(ds.NAME_INDEX_PATH)
    ok(ds.remember_names(renamed) == 0, "无变化返回 0")
    ok(os.path.getmtime(ds.NAME_INDEX_PATH) == mt, "无变化不重写文件")
    ok(ds.remember_names({"sh600519": {"name": "X"}}) == 0,
       "样本 <100 只（单股详情那种调用）不更新名单")
    ok(ds._name_index_load().get("sh600519") == "ST贵州茅台", "名单没被小样本污染")

    print("\n[9] 代码识别：sh600519 / 600519 / 600519.SH / SH600519 都算代码")
    for raw, want in (("600519", "sh600519"), ("sh600519", "sh600519"),
                      ("600519.SH", "sh600519"), ("SH600519", "sh600519"),
                      ("000001", "sz000001"), ("300750", "sz300750"),
                      ("920185", "bj920185"), ("835185", "bj835185")):
        got = universe.normalize(raw)
        ok(got == want, f"{raw} → {got}（期望 {want}）")
    ok(universe.normalize("茅台") == "茅台", "名称不会被误判成代码（原样返回）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
